from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from task.goal_coordinator import GoalCoordinator
from task.goal_ledger import GoalLedger
from task.goal_types import AttemptResult, OrderObservation, PreflightResult


class FakeAccount:
    def __init__(self, key, uid):
        self.key = key
        self.preflight_result = PreflightResult(True, account_uid=uid)
        self.actions = []
        self.calls = []
        self.observations = {}
        self.preflight_calls = 0
        self.closed = False

    def preflight(self):
        self.preflight_calls += 1
        if isinstance(self.preflight_result, Exception):
            raise self.preflight_result
        return self.preflight_result

    def attempt(self, people, before_submit):
        self.calls.append(tuple(people))
        if not self.actions:
            pytest.fail(f"unexpected create attempt on {self.key}: {people}")
        action = self.actions.pop(0)
        if callable(action):
            return action(people, before_submit)
        before_submit()
        if isinstance(action, BaseException):
            raise action
        return action

    def reconcile(self, record):
        return self.observations.get(record["order_id"], OrderObservation("unknown"))

    def close(self):
        self.closed = True


@pytest.fixture
def scenario(tmp_path):
    clock = [100.0]
    config = SimpleNamespace(
        accounts=(
            SimpleNamespace(key="A", uid="101"),
            SimpleNamespace(key="B", uid="102"),
        ),
        people=("P1", "P2", "P3"),
        target=SimpleNamespace(key="bilibili:1:2:3:2026-11-28"),
        starts_at=datetime.fromtimestamp(90, timezone.utc),
        ends_at=datetime.fromtimestamp(1000, timezone.utc),
        interval_seconds=1.0,
        order_poll_seconds=1.0,
    )
    ledger = GoalLedger(
        tmp_path / "goal.sqlite3", "bilibili:1:2026-11-28", "fixed-goal", config.people
    )
    accounts = {a.key: FakeAccount(a.key, a.uid) for a in config.accounts}
    events = []
    coordinator = GoalCoordinator(
        config, ledger, accounts, now=lambda: clock[0], event_sink=events.append
    )

    def advance(seconds=1):
        clock[0] += seconds
        return coordinator.step()

    with ledger.runner_lock():
        yield SimpleNamespace(
            config=config,
            ledger=ledger,
            accounts=accounts,
            events=events,
            coordinator=coordinator,
            clock=clock,
            advance=advance,
            start=coordinator.initialize,
        )
    ledger.close()


def known_order(s, people, *, account="A", order_id="500", state="pending"):
    attempt_id = s.ledger.reserve(account, people)
    s.ledger.mark_submitting(attempt_id)
    s.ledger.complete(attempt_id, state, order_id=order_id)
    return attempt_id


def observation(s, people, *, account="A", order_id="500", status="paid", **changes):
    value = OrderObservation(
        status, order_id, tuple(people), account, s.config.target.key, True
    )
    return replace(value, **changes)


def test_first_attempt_prefers_three_and_pending_is_not_paid(scenario):
    s = scenario
    s.accounts["A"].actions = [AttemptResult("created", order_id=101)]
    s.start()
    result = s.coordinator.step()
    for _ in range(3):
        s.advance()
    assert s.accounts["A"].calls == [("P1", "P2", "P3")]
    assert s.accounts["B"].calls == []
    assert result["counts"]["pending"] == 3
    assert result["complete"] is False


def test_shortage_then_pair_and_single_only_completes_on_verified_payment(scenario):
    s = scenario
    s.accounts["A"].actions = [
        AttemptResult("shortage", code=100009, definitive_no_order=True),
        AttemptResult("created", order_id=303),
    ]
    s.accounts["B"].actions = [AttemptResult("created", order_id=202)]
    s.start()
    s.coordinator.step()
    s.advance()
    partial = s.advance()
    assert s.accounts["A"].calls == [("P1", "P2", "P3"), ("P3",)]
    assert s.accounts["B"].calls == [("P1", "P2")]
    assert partial["counts"]["pending"] == 3
    assert partial["counts"]["paid"] == 0
    s.accounts["A"].observations["303"] = observation(s, ("P3",), order_id="303")
    s.accounts["B"].observations["202"] = observation(
        s, ("P1", "P2"), account="B", order_id="202"
    )
    complete = s.advance()
    assert complete["complete"] is True
    assert complete["stop_reason"] == "completed"
    s.advance()
    assert len(s.accounts["A"].calls) + len(s.accounts["B"].calls) == 3


def test_confirmed_zero_waits_without_trying_two_or_one(scenario):
    s = scenario
    s.accounts["A"].actions = [AttemptResult("sold_out", definitive_no_order=True)]
    s.accounts["B"].actions = [AttemptResult("created", order_id=202)]
    s.start()
    s.coordinator.step()
    s.coordinator.step()  # no time has passed: no immediate retry cascade
    s.advance()
    assert s.accounts["B"].calls == [("P1", "P2", "P3")]


def test_explicit_retry_does_not_reduce_group_size(scenario):
    s = scenario
    s.accounts["A"].actions = [
        AttemptResult("retry", code=100001, definitive_no_order=True)
    ]
    s.accounts["B"].actions = [AttemptResult("created", order_id=202)]
    s.start()
    s.coordinator.step()
    s.advance()
    assert s.accounts["B"].calls == [s.config.people]


@pytest.mark.parametrize(
    "result",
    [
        AttemptResult("unknown"),
        AttemptResult("retry"),
        AttemptResult("shortage"),
        AttemptResult("created", order_id=0),
        AttemptResult("created", order_id=True),
        object(),
    ],
)
def test_uncertain_result_freezes_same_people_on_both_accounts(scenario, result):
    s = scenario
    s.accounts["A"].actions = [result]
    s.start()
    s.coordinator.step()
    s.advance(100)
    assert s.ledger.snapshot()["counts"]["unknown"] == 3
    assert s.accounts["B"].calls == []


def test_unknown_pair_does_not_block_disjoint_person_on_other_account(scenario):
    s = scenario
    attempt_id = s.ledger.reserve("A", ("P1", "P2"))
    s.ledger.mark_submitting(attempt_id)
    s.ledger.complete(attempt_id, "unknown", reason="response_lost")
    s.accounts["B"].actions = [AttemptResult("created", order_id=303)]
    s.start()
    result = s.coordinator.step()
    assert s.accounts["A"].calls == []
    assert s.accounts["B"].calls == [("P3",)]
    assert result["counts"]["unknown"] == 2
    assert result["counts"]["pending"] == 1


@pytest.mark.parametrize("state", ["pending", "paid"])
def test_partial_order_only_leaves_unoccupied_people_for_purchase(scenario, state):
    s = scenario
    known_order(s, ("P1",), state=state)
    # Depending on payment state, A may become available again; both clients
    # must receive the same remaining roster rather than the original trio.
    for account in s.accounts.values():
        account.actions = [AttemptResult("created", order_id=600)]
    s.start()
    s.coordinator.step()
    calls = [people for account in s.accounts.values() for people in account.calls]
    assert calls == [("P2", "P3")]


@pytest.mark.parametrize(
    "changes",
    [
        {"verified": False},
        {"person_keys": ("P1", "P2")},
        {"account_key": "B"},
        {"target_key": "wrong-date-or-tier"},
        {"order_id": "501"},
        {"status": "unknown"},
    ],
)
def test_incomplete_or_mismatched_order_observation_never_marks_paid(scenario, changes):
    s = scenario
    known_order(s, s.config.people)
    s.accounts["A"].observations["500"] = observation(s, s.config.people, **changes)
    s.start()
    result = s.coordinator.step()
    assert result["counts"]["pending"] == 3
    assert result["counts"]["paid"] == 0


def test_verified_closed_order_releases_roster_and_returns_to_waiting(scenario):
    s = scenario
    known_order(s, s.config.people)
    s.accounts["A"].observations["500"] = observation(
        s, s.config.people, status="closed"
    )
    s.accounts["A"].actions = [AttemptResult("sold_out", definitive_no_order=True)]
    s.start()
    result = s.coordinator.step()
    assert s.accounts["A"].calls == [s.config.people]
    assert result["counts"]["available"] == 3
    assert result["stopped"] is False


@pytest.mark.parametrize("reason", ["stop", "deadline"])
def test_prepare_cannot_cross_stop_or_end_boundary_into_create(scenario, reason):
    s = scenario
    create_sent = []

    def prepare_then_submit(people, before_submit):
        if reason == "stop":
            s.ledger.stop("user_stopped")
        else:
            s.clock[0] = s.config.ends_at.timestamp()
        before_submit()
        create_sent.append(people)
        return AttemptResult("created", order_id=99)

    s.accounts["A"].actions = [prepare_then_submit]
    s.start()
    result = s.coordinator.step()
    assert create_sent == []
    assert result["stopped"] is True
    assert result["counts"]["available"] == 3


def test_exception_before_submit_releases_but_after_submit_stays_unknown(scenario):
    s = scenario

    def preparation_failure(*_):
        raise RuntimeError("before network create")

    s.accounts["A"].actions = [preparation_failure]
    s.accounts["B"].actions = [RuntimeError("after network create")]
    s.start()
    first = s.coordinator.step()
    second = s.advance()
    assert first["counts"]["available"] == 3
    assert second["counts"]["unknown"] == 3


def test_restart_converts_inflight_to_unknown_without_secondary_purchase(scenario):
    s = scenario
    attempt_id = s.ledger.reserve("A", s.config.people)
    s.ledger.mark_submitting(attempt_id)
    s.start()
    result = s.coordinator.step()
    assert result["counts"]["unknown"] == 3
    assert all(not account.calls for account in s.accounts.values())


def test_explicit_resume_retains_unknown_occupancy(scenario):
    s = scenario
    attempt_id = s.ledger.reserve("A", s.config.people)
    s.ledger.mark_submitting(attempt_id)
    s.ledger.stop("user_stopped")
    s.start()
    assert all(account.preflight_calls == 0 for account in s.accounts.values())
    s.coordinator.initialize(resume=True)
    result = s.coordinator.step()
    assert result["counts"]["unknown"] == 3
    assert not result["stopped"]
    assert all(not account.calls for account in s.accounts.values())


def test_notification_exception_does_not_recreate_known_order(scenario):
    s = scenario
    s.coordinator.event_sink = lambda event: (_ for _ in ()).throw(
        RuntimeError("display unavailable")
    )
    s.accounts["A"].actions = [AttemptResult("created", order_id=99)]
    s.start()
    s.coordinator.step()
    s.advance()
    assert s.ledger.snapshot()["counts"]["pending"] == 3
    assert len(s.accounts["A"].calls) == 1


def test_sale_start_is_respected_after_early_preflight(scenario):
    s = scenario
    s.config.starts_at = datetime.fromtimestamp(110, timezone.utc)
    s.accounts["A"].actions = [AttemptResult("created", order_id=99)]
    s.start()
    s.coordinator.step()
    assert not s.accounts["A"].calls
    s.advance(10)
    assert s.accounts["A"].calls == [s.config.people]


def test_retry_after_allows_other_account_without_downgrading(scenario):
    s = scenario
    s.accounts["A"].actions = [
        AttemptResult("retry", retry_after=30, definitive_no_order=True)
    ]
    s.accounts["B"].actions = [AttemptResult("created", order_id=99)]
    s.start()
    s.coordinator.step()
    s.advance()
    assert s.accounts["B"].calls == [s.config.people]
    assert s.coordinator.account_ready_at["A"] == 130


def test_transient_preflight_failure_defers_without_persistently_stopping_goal(
    scenario,
):
    s = scenario
    for account in s.accounts.values():
        account.preflight_result = PreflightResult(
            False, "temporary rate limit", retryable=True, retry_after=5
        )
        account.actions = [AttemptResult("created", order_id=99)]
    s.start()
    assert not s.ledger.snapshot()["stopped"]
    s.coordinator.step()
    assert all(not account.calls for account in s.accounts.values())
    s.advance(5)
    assert s.accounts["A"].calls == [s.config.people]


def test_target_mismatch_stops_every_account(scenario):
    s = scenario
    s.accounts["A"].preflight_result = PreflightResult(
        False, "wrong target", blocked_scope="goal"
    )
    s.start()
    result = s.coordinator.step()
    assert result["stop_reason"] == "target_needs_review"
    assert all(not account.calls for account in s.accounts.values())


def test_wrong_login_uid_blocks_only_that_account(scenario):
    s = scenario
    s.accounts["A"].preflight_result = PreflightResult(True, account_uid="999")
    s.accounts["B"].actions = [AttemptResult("created", order_id=99)]
    s.start()
    s.coordinator.step()
    assert not s.accounts["A"].calls
    assert s.accounts["B"].calls == [s.config.people]


def test_existing_order_review_before_create_keeps_people_reserved(scenario):
    s = scenario
    s.accounts["A"].actions = [
        lambda *_: AttemptResult("review", code=100048, reason="existing order")
    ]
    s.start()
    s.coordinator.step()
    result = s.advance()
    assert result["counts"]["review"] == 3
    assert s.accounts["B"].calls == []


def test_candidate_order_hint_does_not_bind_unverified_order_to_goal(scenario):
    s = scenario
    s.accounts["A"].actions = [
        AttemptResult("unknown", order_id="999", reason="candidate")
    ]
    s.start()
    result = s.coordinator.step()
    record = result["attempts"][0]
    assert record["state"] == "unknown"
    assert record["order_id"] is None
    assert "candidate_order_id=999" in record["reason"]
    # A user may establish that this candidate belonged to another date and
    # explicitly confirm that the present attempt never created an order.
    s.ledger.complete(record["attempt_id"], "no_order", reason="user_verified:no_order")
    assert s.ledger.available_people() == s.config.people
