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
    def preflight(self) -> PreflightResult: ...

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
        for account in self.config.accounts:
            if self._stopping():
                break
            try:
                result = self.executors[account.key].preflight()
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
                continue
            if not result.ready and result.retryable:
                delay = result.retry_after
                if not isinstance(delay, (int, float)) or not math.isfinite(delay):
                    delay = self.config.interval_seconds
                self.account_ready_at[account.key] = self.now() + max(
                    delay, self.config.interval_seconds
                )
                self._emit(
                    "account_waiting", account_key=account.key, reason=result.reason
                )
                continue
            reason = result.reason or "登录账号与本地配置不一致"
            self.blocked_accounts[account.key] = reason
            self._emit("account_blocked", account_key=account.key, reason=reason)
            if result.blocked_scope == "goal":
                self.ledger.stop("target_needs_review")
                break
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
        self.next_order_poll_at = self.now() + self.config.order_poll_seconds
        for record in self.ledger.attempts(active_only=True):
            if self._stopping():
                return
            if record["state"] not in {"pending", "unknown", "review"}:
                continue
            try:
                observation = self.executors[record["account_key"]].reconcile(record)
            except Exception:
                continue
            if not self._matches_observation(record, observation):
                continue
            previous = record["state"]
            updated = self.ledger.complete(
                record["attempt_id"],
                observation.status,
                order_id=observation.order_id,
                reason="official_order_verified",
            )
            if updated["state"] != previous:
                self._emit(
                    "order_verified",
                    attempt_id=record["attempt_id"],
                    account_key=record["account_key"],
                    person_keys=record["person_keys"],
                    outcome=updated["state"],
                    order_id=updated["order_id"],
                )

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
        self._poll_orders()
        if self._stopping():
            return self.ledger.snapshot(include_history=False)
        if (
            self.now() < self.config.starts_at.timestamp()
            or self.now() < self.next_attempt_at
        ):
            return self.ledger.snapshot(include_history=False)
        available = self.ledger.available_people()
        if not available:
            return self.ledger.snapshot(include_history=False)
        account_key = self._pick_account()
        if account_key is None:
            if len(self.blocked_accounts) == len(self.account_order):
                self.ledger.stop("accounts_need_attention")
            return self.ledger.snapshot(include_history=False)
        people = available[: min(self.batch_cap, len(available))]
        try:
            attempt_id = self.ledger.reserve(account_key, people)
        except LedgerStoppedError:
            return self.ledger.snapshot(include_history=False)

        def before_submit() -> None:
            if self._stopping():
                raise SubmissionStopped("目标已停止，未授权创建订单")
            self.ledger.mark_submitting(attempt_id)

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
            )
            if not isinstance(exc, Exception):
                raise
        else:
            self._accept_result(attempt_id, people, result)
        self.next_attempt_at = self.now() + self.config.interval_seconds
        self._stopping()
        return self.ledger.snapshot(include_history=False)

    def _accept_result(
        self, attempt_id: str, people: tuple[str, ...], result: Any
    ) -> None:
        record = self._record(attempt_id)
        key = record["account_key"]
        if not isinstance(result, AttemptResult):
            result = AttemptResult("unknown", reason="无法识别的执行器结果")
        if result.outcome == "created":
            order_id = normalized_order_id(result.order_id)
            if order_id is not None and record["state"] == "submitting":
                self.ledger.complete(attempt_id, "pending", order_id=order_id)
                self._emit(
                    "order_created",
                    account_key=key,
                    person_keys=people,
                    attempt_id=attempt_id,
                    order_id=order_id,
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
                outcome=result.outcome,
                reason=result.reason,
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
            reason=reason,
            candidate_order_id=candidate,
        )

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
                    self.stop_event.wait(min(self.config.interval_seconds, 1.0))
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
