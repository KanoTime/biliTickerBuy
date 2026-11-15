"""Local, transactional ownership of attendees across account executors.

The ledger contains only stable local identifiers and order evidence. Callers
must pass sanitized reasons, not response bodies, cookies or attendee details.
``no_order`` and ``closed`` require verified evidence from the caller: elapsed
time, an empty order-list page or a transport error do not provide that proof.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ACTIVE_STATES = frozenset({"preparing", "submitting", "pending", "unknown", "review"})
PERSON_STATES = (
    "available",
    "preparing",
    "submitting",
    "pending",
    "unknown",
    "review",
    "paid",
)
OUTCOMES = frozenset({"pending", "unknown", "review", "paid", "closed", "no_order"})
TERMINAL_STATES = frozenset({"paid", "closed", "no_order"})
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RUNNER_LOCKS: set[str] = set()
_RUNNER_LOCKS_GUARD = threading.Lock()


class GoalLedgerError(RuntimeError):
    """The goal state cannot be safely read or changed."""


class LedgerConflictError(GoalLedgerError):
    """An account/person is occupied or an attempt no longer owns its group."""


class LedgerStoppedError(GoalLedgerError):
    """A persisted stop requires explicit resume before creating more orders."""


class GoalConfigMismatchError(GoalLedgerError):
    """The saved target or stable attendee mapping differs from this run."""


def _key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(f"{label} must be a stable ASCII identifier")
    return value


def _people(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("person_keys must be a nonempty sequence")
    people = tuple(_key(value, "person_key") for value in values)
    if not people or len(set(people)) != len(people):
        raise ValueError("person_keys must be nonempty and unique")
    return people


def _reason(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512 or "\0" in value:
        raise ValueError("reason must be a sanitized string of at most 512 characters")
    return value


def _order_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("order_id must be a positive decimal identifier")
    if isinstance(value, int):
        value = str(value)
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not value.isascii()
        or not value.isdecimal()
        or not value.lstrip("0")
    ):
        raise ValueError("order_id must be a positive decimal identifier")
    return value.lstrip("0")


class GoalLedger:
    """A single SQLite file with atomic attendee-group reservations.

    ``scope_key`` identifies the admission event/date, not an account or run.
    ``fingerprint`` binds the exact target and local identity mapping. Reopening
    with a different fingerprint or roster is rejected rather than resetting it.

    Acquire ``runner_lock()`` before running/recovering a coordinator. ``stop``
    remains usable by a separate CLI process while that lock is held.
    """

    def __init__(
        self,
        path: str | Path,
        scope_key: str,
        fingerprint: str,
        person_keys: Sequence[str],
    ) -> None:
        if not isinstance(scope_key, str) or not scope_key or len(scope_key) > 512:
            raise ValueError("scope_key must be a nonempty event/date identifier")
        if "\0" in scope_key:
            raise ValueError("scope_key must not contain a NUL character")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or len(fingerprint) > 512
        ):
            raise ValueError("fingerprint must be a nonempty stable target digest")
        self.person_keys = _people(person_keys)
        self.scope_key = scope_key
        self.fingerprint = fingerprint
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            if not self.path.is_file() or self.path.stat().st_nlink != 1:
                raise GoalLedgerError(
                    "ledger must be a regular file without hard links"
                )
        else:
            os.close(descriptor)
        self._lock = threading.RLock()
        self._closed = False
        self._runner_lock_owned = False
        self._runner_pid: int | None = None
        self._db = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS goal_meta (
                    scope_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    person_keys TEXT NOT NULL,
                    stopped INTEGER NOT NULL DEFAULT 0,
                    stop_reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS goal_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES goal_meta(scope_key),
                    account_key TEXT NOT NULL,
                    person_keys TEXT NOT NULL,
                    state TEXT NOT NULL,
                    order_id TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS goal_attempts_account
                    ON goal_attempts(scope_key, account_key, state);
                CREATE INDEX IF NOT EXISTS goal_attempts_state
                    ON goal_attempts(scope_key, state, created_at, attempt_id);
                CREATE UNIQUE INDEX IF NOT EXISTS goal_attempts_order_identity
                    ON goal_attempts(order_id) WHERE order_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS goal_people (
                    scope_key TEXT NOT NULL REFERENCES goal_meta(scope_key),
                    person_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'available',
                    attempt_id TEXT REFERENCES goal_attempts(attempt_id),
                    PRIMARY KEY (scope_key, person_key)
                );
                """
            )
            with self._transaction() as db:
                existing = db.execute(
                    "SELECT * FROM goal_meta WHERE scope_key=?", (scope_key,)
                ).fetchone()
                roster = json.dumps(self.person_keys, separators=(",", ":"))
                if existing is not None:
                    if (
                        existing["fingerprint"] != fingerprint
                        or existing["person_keys"] != roster
                    ):
                        raise GoalConfigMismatchError(
                            "saved target/person mapping differs; preserve the existing ledger"
                        )
                else:
                    db.execute(
                        "INSERT INTO goal_meta(scope_key,fingerprint,person_keys,updated_at) "
                        "VALUES(?,?,?,?)",
                        (scope_key, fingerprint, roster, time.time()),
                    )
                    db.executemany(
                        "INSERT INTO goal_people(scope_key,person_key,ordinal) VALUES(?,?,?)",
                        (
                            (scope_key, person, i)
                            for i, person in enumerate(self.person_keys)
                        ),
                    )
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def __enter__(self) -> GoalLedger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise GoalLedgerError("ledger is closed")
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _meta(self, db: sqlite3.Connection) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM goal_meta WHERE scope_key=?", (self.scope_key,)
        ).fetchone()
        if row is None:
            raise GoalLedgerError("goal metadata is missing")
        return row

    def _ensure_running(self, db: sqlite3.Connection) -> None:
        if self._meta(db)["stopped"]:
            raise LedgerStoppedError(
                "goal is stopped; explicitly resume its existing state"
            )

    def _attempt(self, db: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        _key(attempt_id, "attempt_id")
        row = db.execute(
            "SELECT * FROM goal_attempts WHERE scope_key=? AND attempt_id=?",
            (self.scope_key, attempt_id),
        ).fetchone()
        if row is None:
            raise LedgerConflictError("attempt does not belong to this goal")
        return row

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["person_keys"] = json.loads(result["person_keys"])
        return result

    def _assert_owned(self, db: sqlite3.Connection, attempt: sqlite3.Row) -> None:
        people = json.loads(attempt["person_keys"])
        for person in people:
            row = db.execute(
                "SELECT state,attempt_id FROM goal_people WHERE scope_key=? AND person_key=?",
                (self.scope_key, person),
            ).fetchone()
            if (
                row is None
                or row["attempt_id"] != attempt["attempt_id"]
                or row["state"] != attempt["state"]
            ):
                raise LedgerConflictError(
                    "attempt no longer owns its complete attendee group"
                )

    def _transition(
        self,
        db: sqlite3.Connection,
        attempt: sqlite3.Row,
        state: str,
        *,
        order_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        self._assert_owned(db, attempt)
        db.execute(
            "UPDATE goal_attempts SET state=?,order_id=?,reason=?,updated_at=? "
            "WHERE attempt_id=? AND scope_key=?",
            (
                state,
                order_id,
                reason,
                time.time(),
                attempt["attempt_id"],
                self.scope_key,
            ),
        )
        release = state in {"closed", "no_order"}
        db.execute(
            "UPDATE goal_people SET state=?,attempt_id=? WHERE scope_key=? AND attempt_id=?",
            (
                "available" if release else state,
                None if release else attempt["attempt_id"],
                self.scope_key,
                attempt["attempt_id"],
            ),
        )
        return self._attempt_dict(self._attempt(db, attempt["attempt_id"]))

    def reserve(self, account_key: str, person_keys: Sequence[str]) -> str:
        account_key = _key(account_key, "account_key")
        people = _people(person_keys)
        if not set(people).issubset(self.person_keys):
            raise ValueError("reservation contains an attendee outside this goal")
        with self._transaction() as db:
            self._ensure_running(db)
            active = db.execute(
                "SELECT 1 FROM goal_attempts WHERE scope_key=? AND account_key=? "
                "AND state IN (?,?,?,?,?) LIMIT 1",
                (self.scope_key, account_key, *sorted(ACTIVE_STATES)),
            ).fetchone()
            if active is not None:
                raise LedgerConflictError("account has an unresolved attempt")
            for person in people:
                row = db.execute(
                    "SELECT state,attempt_id FROM goal_people WHERE scope_key=? AND person_key=?",
                    (self.scope_key, person),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "available"
                    or row["attempt_id"] is not None
                ):
                    raise LedgerConflictError(
                        "attendee group is not entirely available"
                    )
            attempt_id = uuid.uuid4().hex
            now = time.time()
            db.execute(
                "INSERT INTO goal_attempts "
                "(attempt_id,scope_key,account_key,person_keys,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    self.scope_key,
                    account_key,
                    json.dumps(people, separators=(",", ":")),
                    "preparing",
                    now,
                    now,
                ),
            )
            db.executemany(
                "UPDATE goal_people SET state='preparing',attempt_id=? "
                "WHERE scope_key=? AND person_key=?",
                ((attempt_id, self.scope_key, person) for person in people),
            )
            return attempt_id

    def mark_submitting(self, attempt_id: str) -> dict[str, Any]:
        """Commit the intent before the first create POST; never authorize twice."""
        with self._transaction() as db:
            self._ensure_running(db)
            attempt = self._attempt(db, attempt_id)
            if attempt["state"] != "preparing":
                raise LedgerConflictError(
                    "create submission was already authorized or stopped"
                )
            return self._transition(db, attempt, "submitting")

    def complete(
        self,
        attempt_id: str,
        outcome: str,
        order_id: int | str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise ValueError("unsupported order outcome")
        reason = _reason(reason)
        supplied_order_id = _order_id(order_id)
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            previous = attempt["state"]
            existing_order_id = attempt["order_id"]
            if (
                supplied_order_id is not None
                and existing_order_id is not None
                and supplied_order_id != existing_order_id
            ):
                raise LedgerConflictError(
                    "order identity cannot change within an attempt"
                )
            resolved_order_id = supplied_order_id or existing_order_id
            if resolved_order_id is not None:
                other = db.execute(
                    "SELECT attempt_id FROM goal_attempts WHERE order_id=? AND attempt_id<>?",
                    (resolved_order_id, attempt_id),
                ).fetchone()
                if other is not None:
                    raise LedgerConflictError(
                        "order is already attributed to another attempt"
                    )
            if outcome in {"pending", "paid", "closed"} and resolved_order_id is None:
                raise ValueError(f"{outcome} requires a valid order_id")
            if previous in TERMINAL_STATES:
                if outcome != previous:
                    raise LedgerConflictError(
                        "terminal attempt cannot change or release a new owner"
                    )
                return self._attempt_dict(attempt)
            if outcome == previous:
                if supplied_order_id is None or supplied_order_id == existing_order_id:
                    return self._attempt_dict(attempt)
            if previous == "preparing" and outcome in {"pending", "paid", "closed"}:
                raise LedgerConflictError(
                    "an order outcome requires a recorded submission intent"
                )
            if previous == "pending" and outcome in {"unknown", "review", "no_order"}:
                raise LedgerConflictError(
                    "known pending order cannot be treated as missing"
                )
            if outcome == "no_order" and resolved_order_id is not None:
                raise LedgerConflictError(
                    "a known order must be verified closed, not treated as absent"
                )
            if (
                outcome in {"closed", "no_order"}
                and previous != "preparing"
                and not reason.strip()
            ):
                raise ValueError(
                    "releasing a submitted attempt requires a verified reason"
                )
            return self._transition(
                db, attempt, outcome, order_id=resolved_order_id, reason=reason
            )

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        """Read one owned attempt directly without walking the retry history."""
        with self._transaction(write=False) as db:
            return self._attempt_dict(self._attempt(db, attempt_id))

    def _attempt_rows(
        self, db: sqlite3.Connection, *, active_only: bool
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM goal_attempts WHERE scope_key=?"
        parameters = (self.scope_key,)
        if active_only:
            query += " AND state IN (?,?,?,?,?)"
            parameters += tuple(sorted(ACTIVE_STATES))
        query += " ORDER BY created_at,attempt_id"
        return db.execute(query, parameters).fetchall()

    def attempts(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._transaction(write=False) as db:
            return [
                self._attempt_dict(row)
                for row in self._attempt_rows(db, active_only=active_only)
            ]

    def available_people(self) -> tuple[str, ...]:
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT person_key FROM goal_people WHERE scope_key=? AND state='available' "
                "ORDER BY ordinal",
                (self.scope_key,),
            ).fetchall()
            return tuple(row["person_key"] for row in rows)

    def snapshot(self, *, include_history: bool = True) -> dict[str, Any]:
        """Read complete person counts, optionally omitting completed attempts.

        Coordinators should use ``include_history=False`` during polling so an
        increasing history of rejected attempts does not increase polling work.
        """
        with self._transaction(write=False) as db:
            meta = self._meta(db)
            people: dict[str, Any] = {}
            counts = dict.fromkeys(PERSON_STATES, 0)
            rows = db.execute(
                "SELECT p.person_key,p.state,p.attempt_id,a.account_key,a.order_id "
                "FROM goal_people p LEFT JOIN goal_attempts a ON p.attempt_id=a.attempt_id "
                "WHERE p.scope_key=? ORDER BY p.ordinal",
                (self.scope_key,),
            ).fetchall()
            for row in rows:
                item = dict(row)
                person = item.pop("person_key")
                if item["state"] not in counts:
                    raise GoalLedgerError("unknown persisted attendee state")
                people[person] = item
                counts[item["state"]] += 1
            attempts = [
                self._attempt_dict(row)
                for row in self._attempt_rows(db, active_only=not include_history)
            ]
            return {
                "scope_key": self.scope_key,
                "fingerprint": self.fingerprint,
                "stopped": bool(meta["stopped"]),
                "stop_reason": meta["stop_reason"],
                "people": people,
                "attempts": attempts,
                "counts": counts,
                "total": len(self.person_keys),
                "complete": counts["paid"] == len(self.person_keys),
            }

    def stop(self, reason: str = "user_stopped") -> None:
        reason = _reason(reason)
        with self._transaction() as db:
            db.execute(
                "UPDATE goal_meta SET stopped=1,stop_reason=?,updated_at=? WHERE scope_key=?",
                (reason, time.time(), self.scope_key),
            )

    def resume(self) -> None:
        """Resume the same goal; never clear attendee ownership or order evidence."""
        with self._transaction() as db:
            db.execute(
                "UPDATE goal_meta SET stopped=0,stop_reason='',updated_at=? WHERE scope_key=?",
                (time.time(), self.scope_key),
            )

    def recover(self) -> dict[str, list[str]]:
        if not self._runner_lock_owned or self._runner_pid != os.getpid():
            raise LedgerConflictError(
                "recovery requires this process to hold runner_lock()"
            )
        released: list[str] = []
        uncertain: list[str] = []
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM goal_attempts WHERE scope_key=? AND state IN ('preparing','submitting')",
                (self.scope_key,),
            ).fetchall()
            for row in rows:
                if row["state"] == "preparing":
                    self._transition(
                        db, row, "no_order", reason="restart_before_submission"
                    )
                    released.append(row["attempt_id"])
                else:
                    self._transition(
                        db, row, "unknown", reason="restart_during_submission"
                    )
                    uncertain.append(row["attempt_id"])
        return {"released_preparing": released, "marked_unknown": uncertain}

    @contextmanager
    def runner_lock(self) -> Iterator[GoalLedger]:
        """Exclusive coordinator lock; account/person claims remain transactional."""
        if self._closed:
            raise GoalLedgerError("ledger is closed")
        lock_key = str(self.path)
        with _RUNNER_LOCKS_GUARD:
            if lock_key in _RUNNER_LOCKS:
                raise LedgerConflictError(
                    "another coordinator already owns this ledger"
                )
            _RUNNER_LOCKS.add(lock_key)
        handle = None
        acquired = False
        try:
            descriptor = os.open(
                self.path.with_name(self.path.name + ".runner.lock"),
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            handle = os.fdopen(descriptor, "r+b")
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            self._runner_lock_owned = True
            self._runner_pid = os.getpid()
            yield self
        except (BlockingIOError, PermissionError) as exc:
            if acquired:
                raise
            raise LedgerConflictError(
                "another coordinator already owns this ledger"
            ) from exc
        except OSError as exc:
            if acquired:
                raise
            raise GoalLedgerError(
                "could not acquire the local coordinator lock"
            ) from exc
        finally:
            self._runner_lock_owned = False
            self._runner_pid = None
            try:
                if handle is not None:
                    try:
                        if acquired:
                            if os.name == "nt":
                                import msvcrt

                                handle.seek(0)
                                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl

                                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
            finally:
                with _RUNNER_LOCKS_GUARD:
                    _RUNNER_LOCKS.discard(lock_key)
