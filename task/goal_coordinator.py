"""Coordinate one fixed personal goal across independent account clients.

The loop is intentionally small: it issues one bounded attempt at a time, polls
known orders, and returns to the same remaining attendee set on later inventory.
Durable attendee reservations, not a run's log messages, determine what is safe
to submit. Account clients must invoke the submission callback immediately before
their create POST, after preparation and final payload checks.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from task.goal_config import GoalConfig
from task.goal_ledger import GoalLedger, LedgerStoppedError
from task.goal_types import AttemptResult, OrderObservation, PreflightResult


class GoalAccountExecutor(Protocol):
    def preflight(
        self, *, person_keys: tuple[str, ...] | None = None
    ) -> PreflightResult: ...

    def attempt(
        self, person_keys: tuple[str, ...], before_submit: Callable[[], None]
    ) -> AttemptResult: ...

    def reconcile(self, attempt: dict[str, Any]) -> OrderObservation: ...

    def close(self) -> None: ...


class SubmissionStopped(RuntimeError):
    """The goal was stopped or its window ended before creation was authorized."""


def normalized_order_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        if 0 < len(value) <= 64 and value.isascii() and value.isdecimal():
            return value.lstrip("0") or None
    return None


class GoalCoordinator:
    def __init__(
        self,
        config: GoalConfig,
        ledger: GoalLedger,
        executors: Mapping[str, GoalAccountExecutor],
        *,
        now: Callable[[], float] = time.time,
        stop_event: threading.Event | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.ledger = ledger
        self.executors = dict(executors)
        self.account_order = tuple(account.key for account in config.accounts)
        if set(self.executors) != set(self.account_order):
            raise ValueError(
                "account executors must exactly match the goal configuration"
            )
        self.now = now
        self.stop_event = stop_event or threading.Event()
        self.event_sink = event_sink
        self.blocked_accounts: dict[str, str] = {}
        self.account_ready_at = dict.fromkeys(self.account_order, 0.0)
        self.next_attempt_at = 0.0
        self.next_order_poll_at = 0.0
        self.batch_cap = len(config.people)
        self.account_cursor = 0
        self.order_poll_cursor = 0
        self.verification_notices: dict[str, str] = {}
        self.initialized = False

    def _emit(self, kind: str, **fields: Any) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink({"kind": kind, "time": self.now(), **fields})
        except Exception:
            # A terminal/notification sink cannot decide whether to create again.
            pass

    def initialize(self, *, resume: bool = False) -> None:
        """Call only while holding ledger.runner_lock(); run() does this itself."""
        recovered = self.ledger.recover()
        if resume:
            self.ledger.resume()
        self.initialized = True
        self._emit("recovered", recovery=recovered)
        if self._stopping():
            return
        people = self.ledger.available_people()
        if not people:
            # Existing orders are checked by reconcile, without requiring the
            # already occupied attendees to be eligible for a new purchase.
            return
        occupied = {
            record["account_key"] for record in self.ledger.attempts(active_only=True)
        }
        checked = set()
        for account in self.config.accounts:
            if self._stopping():
                break
            if account.key in occupied:
                continue
            checked.add(account.key)
            try:
                result = self.executors[account.key].preflight(person_keys=people)
            except Exception:
                result = PreflightResult(
                    False,
                    "账号预检暂时无法完成",
                    retryable=True,
                    retry_after=self.config.interval_seconds,
                )
            if not isinstance(result, PreflightResult):
                result = PreflightResult(False, "账号预检返回了无法识别的结果")
            if result.ready and str(result.account_uid) == str(account.uid):
                self._emit("account_ready", account_key=account.key)
            elif not result.ready and result.retryable:
                delay = result.retry_after
                if not isinstance(delay, (int, float)) or not math.isfinite(delay):
                    delay = self.config.interval_seconds
                self.account_ready_at[account.key] = self.now() + max(
                    delay, self.config.interval_seconds
                )
                self._emit(
                    "account_waiting", account_key=account.key, reason=result.reason
                )
            else:
                reason = result.reason or "登录账号与本地配置不一致"
                self.blocked_accounts[account.key] = reason
                self._emit("account_blocked", account_key=account.key, reason=reason)
                if result.blocked_scope == "goal":
                    self.ledger.stop("target_needs_review")
                    break
            if self.now() >= self.config.starts_at.timestamp():
                # Every attempt validates its own account. Once sales have
                # started, a backup preflight must not hold up the first one.
                break
        if self.now() >= self.config.starts_at.timestamp() and not self._stopping():
            for account in self.config.accounts:
                if account.key not in occupied and account.key not in checked:
                    self._emit(
                        "account_preflight_deferred",
                        account_key=account.key,
                        reason="开售后在该账号首次尝试时核验",
                    )
        if len(self.blocked_accounts) == len(self.account_order):
            self.ledger.stop("accounts_need_attention")

    def _stopping(self) -> bool:
        snapshot = self.ledger.snapshot(include_history=False)
        if snapshot["counts"]["paid"] == len(self.config.people):
            if not snapshot["stopped"]:
                self.ledger.stop("completed")
                self._emit("completed", paid=len(self.config.people))
            return True
        if snapshot["stopped"]:
            return True
        if self.stop_event.is_set():
            self.ledger.stop("user_stopped")
            return True
        if self.now() >= self.config.ends_at.timestamp():
            self.ledger.stop("deadline")
            self._emit("stopped", reason="到达配置的结束时间")
            return True
        return False

    def _record(self, attempt_id: str) -> dict[str, Any]:
        return self.ledger.attempt(attempt_id)

    def _poll_orders(self) -> None:
        if self.now() < self.next_order_poll_at:
            return
        records = [
            record
            for record in self.ledger.attempts(active_only=True)
            if record["state"] in {"pending", "unknown", "review"}
        ]
        if not records or self._stopping():
            return
        index = self.order_poll_cursor % len(records)
        record = records[index]
        self.order_poll_cursor = (index + 1) % len(records)
        try:
            try:
                observation = self.executors[record["account_key"]].reconcile(record)
            except Exception:
                observation = OrderObservation(
                    "unknown", reason="自动核验暂未完成，请在官方订单中心核对"
                )
            if not self._matches_observation(record, observation):
                reason = (
                    observation.reason
                    if isinstance(observation, OrderObservation) and observation.reason
                    else "自动核验尚无足够的订单归属及状态证据"
                )
                if self.verification_notices.get(record["attempt_id"]) != reason:
                    self.verification_notices[record["attempt_id"]] = reason
                    self._emit(
                        "order_verification_needed",
                        attempt_id=record["attempt_id"],
                        account_key=record["account_key"],
                        person_keys=record["person_keys"],
                        order_id=record["order_id"],
                        reason=reason,
                        action="核对官方订单后执行 stop → confirm → run --resume；现有占用保持不变",
                    )
                return
            self.verification_notices.pop(record["attempt_id"], None)
            previous = record["state"]
            updated = self.ledger.complete(
                record["attempt_id"],
                observation.status,
                order_id=observation.order_id,
                reason="official_order_verified",
            )
            if updated["state"] != previous:
                if updated["state"] == "closed":
                    self.batch_cap = len(self.config.people)
                self._emit(
                    "order_verified",
                    attempt_id=record["attempt_id"],
                    account_key=record["account_key"],
                    person_keys=record["person_keys"],
                    outcome=updated["state"],
                    order_id=updated["order_id"],
                )
        finally:
            # A slow read cannot make the next poll overdue immediately.
            self.next_order_poll_at = self.now() + self.config.order_poll_seconds

    def _matches_observation(self, record: dict[str, Any], observation: Any) -> bool:
        if (
            not isinstance(observation, OrderObservation)
            or observation.verified is not True
        ):
            return False
        if observation.status not in {"pending", "paid", "closed"}:
            return False
        if observation.account_key != record["account_key"]:
            return False
        if observation.target_key != self.config.target.key:
            return False
        if sorted(observation.person_keys) != sorted(record["person_keys"]):
            return False
        found_id = normalized_order_id(observation.order_id)
        expected_id = normalized_order_id(record.get("order_id"))
        return found_id is not None and (expected_id is None or found_id == expected_id)

    def _pick_account(self) -> str | None:
        occupied = {
            attempt["account_key"] for attempt in self.ledger.attempts(active_only=True)
        }
        for offset in range(len(self.account_order)):
            index = (self.account_cursor + offset) % len(self.account_order)
            key = self.account_order[index]
            if key in occupied or key in self.blocked_accounts:
                continue
            if self.now() < self.account_ready_at[key]:
                continue
            self.account_cursor = (index + 1) % len(self.account_order)
            return key
        return None

    def step(self) -> dict[str, Any]:
        """Run at most one bounded attempt, then return durable public state."""
        if not self.initialized:
            raise RuntimeError("initialize the coordinator under its runner lock first")
        if self._stopping():
            return self.ledger.snapshot(include_history=False)
        self._attempt_available()
        if not self._stopping():
            self._poll_orders()
        self._stopping()
        return self.ledger.snapshot(include_history=False)

    def _attempt_available(self) -> None:
        if (
            self.now() < self.config.starts_at.timestamp()
            or self.now() < self.next_attempt_at
        ):
            return
        available = self.ledger.available_people()
        if not available:
            return
        account_key = self._pick_account()
        if account_key is None:
            if len(self.blocked_accounts) == len(self.account_order):
                self.ledger.stop("accounts_need_attention")
            return
        people = available[: min(self.batch_cap, len(available))]
        try:
            attempt_id = self.ledger.reserve(account_key, people)
        except LedgerStoppedError:
            return

        started_at = self.now()
        submission_authorized_at = None

        def before_submit() -> None:
            nonlocal submission_authorized_at
            if self._stopping():
                raise SubmissionStopped("目标已停止，未授权创建订单")
            self.ledger.mark_submitting(attempt_id)
            submission_authorized_at = self.now()

        def timing() -> dict[str, float | None]:
            return {
                "duration_seconds": max(0.0, self.now() - started_at),
                "submission_authorized_at": submission_authorized_at,
            }

        self._emit(
            "attempt_started",
            account_key=account_key,
            person_keys=people,
            attempt_id=attempt_id,
        )
        try:
            result = self.executors[account_key].attempt(people, before_submit)
        except BaseException as exc:
            record = self._record(attempt_id)
            outcome = "no_order" if record["state"] == "preparing" else "unknown"
            self.ledger.complete(attempt_id, outcome, reason="attempt_interrupted")
            self._emit(
                "attempt_interrupted",
                account_key=account_key,
                person_keys=people,
                outcome=outcome,
                **timing(),
            )
            if not isinstance(exc, Exception):
                raise
        else:
            self._accept_result(attempt_id, people, result, timing=timing())
        self.next_attempt_at = self.now() + self.config.interval_seconds

    def _accept_result(
        self,
        attempt_id: str,
        people: tuple[str, ...],
        result: Any,
        *,
        timing: dict[str, float | None],
    ) -> None:
        record = self._record(attempt_id)
        key = record["account_key"]
        if not isinstance(result, AttemptResult):
            result = AttemptResult("unknown", reason="无法识别的执行器结果")
        if result.outcome == "created":
            order_id = normalized_order_id(result.order_id)
            if order_id is not None and record["state"] == "submitting":
                self.ledger.complete(attempt_id, "pending", order_id=order_id)
                self.batch_cap = len(self.config.people)
                if len(self.ledger.attempts(active_only=True)) == 1:
                    self.next_order_poll_at = (
                        self.now() + self.config.order_poll_seconds
                    )
                self._emit(
                    "order_created",
                    account_key=key,
                    person_keys=people,
                    attempt_id=attempt_id,
                    order_id=order_id,
                    **timing,
                )
                return
            result = AttemptResult("unknown", reason="缺少有效订单号或提交记录")

        known_no_order = result.definitive_no_order is True
        if result.outcome in {"retry", "shortage", "sold_out", "blocked"} and (
            known_no_order or record["state"] == "preparing"
        ):
            self.ledger.complete(
                attempt_id, "no_order", reason="explicit_rejection_or_no_submission"
            )
            if result.outcome == "shortage" and known_no_order:
                self.batch_cap = (
                    len(people) - 1 if len(people) > 1 else len(self.config.people)
                )
            elif result.outcome == "sold_out":
                self.batch_cap = len(self.config.people)
            if result.outcome == "blocked":
                self.blocked_accounts[key] = result.reason or "账号需要处理"
                if result.blocked_scope == "goal":
                    self.ledger.stop("target_needs_review")
            delay = result.retry_after
            if isinstance(delay, (int, float)) and math.isfinite(delay) and delay > 0:
                self.account_ready_at[key] = self.now() + delay
            self._emit(
                "waiting",
                account_key=key,
                person_keys=people,
                attempt_id=attempt_id,
                outcome=result.outcome,
                code=result.code,
                reason=result.reason,
                **timing,
            )
            return

        # Unrecognized outcomes or rejections without no-order evidence never
        # release a submitted person set. A second account cannot take it over.
        state = "review" if result.outcome in {"blocked", "review"} else "unknown"
        reason = result.reason or "order_result_unknown"
        candidate = normalized_order_id(result.order_id)
        if candidate:
            # A non-success response may refer to an unrelated existing order.
            # Preserve the hint without binding it to this attendee/target set.
            reason += f" [candidate_order_id={candidate}]"
        self.ledger.complete(attempt_id, state, reason=reason)
        if result.blocked_scope == "goal":
            self.ledger.stop("target_needs_review")
        self._emit(
            "order_unresolved",
            account_key=key,
            person_keys=people,
            attempt_id=attempt_id,
            outcome=state,
            code=result.code,
            reason=reason,
            candidate_order_id=candidate,
            **timing,
        )

    def _wait_seconds(self) -> float:
        """Wake for the next useful deadline, with bounded external-stop latency."""
        now = self.now()
        deadlines = [self.config.ends_at.timestamp()]
        active = self.ledger.attempts(active_only=True)
        occupied = {record["account_key"] for record in active}
        if self.ledger.available_people():
            readiness = [
                self.account_ready_at[key]
                for key in self.account_order
                if key not in occupied and key not in self.blocked_accounts
            ]
            if readiness:
                deadlines.append(
                    max(
                        self.config.starts_at.timestamp(),
                        self.next_attempt_at,
                        min(readiness),
                    )
                )
        if any(
            record["state"] in {"pending", "unknown", "review"} for record in active
        ):
            deadlines.append(self.next_order_poll_at)
        return max(0.0, min(1.0, min(deadlines) - now))

    def run(
        self, *, resume: bool = False, max_steps: int | None = None
    ) -> dict[str, Any]:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        with self.ledger.runner_lock():
            try:
                self.initialize(resume=resume)
                steps = 0
                while not self._stopping():
                    self.step()
                    steps += 1
                    if max_steps is not None and steps >= max_steps:
                        self.ledger.stop("bounded_run_finished")
                        break
                    if self._stopping():
                        break
                    self.stop_event.wait(self._wait_seconds())
                return self.ledger.snapshot(include_history=False)
            except BaseException:
                self.ledger.stop("runner_interrupted")
                raise
            finally:
                for executor in self.executors.values():
                    try:
                        executor.close()
                    except Exception:
                        pass
