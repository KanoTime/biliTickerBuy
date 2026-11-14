from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from task.goal_ledger import (
    GoalConfigMismatchError,
    GoalLedger,
    LedgerConflictError,
    LedgerStoppedError,
)


SCOPE = "bili:1006859:2026-11-28"
FINGERPRINT = "fixed-target-and-person-mapping"
PEOPLE = ("P1", "P2", "P3")


def open_ledger(path, *, scope=SCOPE, fingerprint=FINGERPRINT, people=PEOPLE):
    return GoalLedger(path, scope, fingerprint, people)


@pytest.fixture
def ledger(tmp_path):
    with open_ledger(tmp_path / "goal.sqlite3") as value:
        yield value


def submitting(ledger, account="A", people=("P1",)):
    attempt_id = ledger.reserve(account, people)
    ledger.mark_submitting(attempt_id)
    return attempt_id


def test_reservation_owns_the_entire_group_across_accounts(ledger):
    first = ledger.reserve("A", ("P1", "P2"))
    with open_ledger(ledger.path) as second:
        with pytest.raises(LedgerConflictError):
            second.reserve("B", ("P2", "P3"))
        assert second.available_people() == ("P3",)
        assert len(second.attempts()) == 1
        assert second.snapshot()["people"]["P1"]["attempt_id"] == first


def test_distinct_account_can_reserve_the_disjoint_remaining_attendee(ledger):
    first = submitting(ledger, "A", ("P1", "P2"))
    ledger.complete(first, "pending", order_id=12345)
    second = ledger.reserve("B", ("P3",))
    snapshot = ledger.snapshot()
    assert snapshot["counts"]["pending"] == 2
    assert snapshot["people"]["P3"]["attempt_id"] == second
    assert snapshot["complete"] is False


def test_direct_attempt_lookup_retains_history_and_scope_without_a_list_scan(
    ledger, monkeypatch
):
    rejected = ledger.reserve("A", ("P1",))
    ledger.complete(rejected, "no_order")
    current = submitting(ledger, "B", ("P2",))
    ledger.complete(current, "pending", order_id=12345)
    monkeypatch.setattr(
        ledger,
        "attempts",
        lambda **_: pytest.fail("direct lookup must not list history"),
    )

    assert ledger.attempt(rejected)["state"] == "no_order"
    assert ledger.attempt(current)["order_id"] == "12345"
    assert ledger.attempt(current)["person_keys"] == ["P2"]
    with pytest.raises(LedgerConflictError):
        ledger.attempt("missing_attempt")
    with open_ledger(ledger.path, scope="bili:1006859:2026-11-29") as next_day:
        with pytest.raises(LedgerConflictError):
            next_day.attempt(current)


@pytest.mark.parametrize(
    "active_state", ["preparing", "submitting", "pending", "unknown", "review"]
)
def test_active_attempt_query_excludes_paid_closed_and_rejected_history(
    ledger, active_state
):
    paid = submitting(ledger, "A", ("P1",))
    ledger.complete(paid, "paid", order_id=12345)
    rejected = ledger.reserve("B", ("P2",))
    ledger.complete(rejected, "no_order")
    closed = submitting(ledger, "B", ("P2",))
    ledger.complete(closed, "closed", order_id=56789, reason="verified_unpaid_closure")
    active = ledger.reserve("B", ("P2",))
    if active_state != "preparing":
        ledger.mark_submitting(active)
        if active_state != "submitting":
            ledger.complete(
                active,
                active_state,
                order_id=99887 if active_state == "pending" else None,
            )

    assert [item["attempt_id"] for item in ledger.attempts(active_only=True)] == [
        active
    ]
    assert len(ledger.attempts()) == 4
    compact = ledger.snapshot(include_history=False)
    full = ledger.snapshot()
    assert compact["attempts"] == ledger.attempts(active_only=True)
    assert len(full["attempts"]) == 4
    assert {key: value for key, value in compact.items() if key != "attempts"} == {
        key: value for key, value in full.items() if key != "attempts"
    }
    assert compact["counts"]["paid"] == 1
    assert compact["people"]["P1"]["order_id"] == "12345"
    assert compact["counts"][active_state] == 1
    assert compact["counts"]["available"] == 1


def test_compact_snapshot_retains_completed_goal_without_listing_paid_history(ledger):
    attempt = submitting(ledger, people=PEOPLE)
    ledger.complete(attempt, "paid", order_id=12345)

    compact = ledger.snapshot(include_history=False)

    assert compact["complete"] is True
    assert compact["counts"]["paid"] == 3
    assert compact["attempts"] == []
    assert all(person["order_id"] == "12345" for person in compact["people"].values())
    assert ledger.attempt(attempt)["state"] == "paid"


@pytest.mark.parametrize(
    "state", ["preparing", "submitting", "pending", "unknown", "review"]
)
def test_account_has_only_one_unresolved_attempt(ledger, state):
    first = ledger.reserve("A", ("P1",))
    if state != "preparing":
        ledger.mark_submitting(first)
        if state != "submitting":
            ledger.complete(
                first, state, order_id=12345 if state == "pending" else None
            )
    with pytest.raises(LedgerConflictError):
        ledger.reserve("A", ("P2",))
    assert ledger.available_people() == ("P2", "P3")


def test_contending_connections_never_obtain_part_of_an_overlapping_group(ledger):
    barrier = threading.Barrier(2)
    with open_ledger(ledger.path) as other:

        def reserve(value, account, people):
            barrier.wait(timeout=2)
            try:
                return value.reserve(account, people)
            except LedgerConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reserve, ledger, "A", ("P1", "P2"))
            second = pool.submit(reserve, other, "B", ("P2", "P3"))
            results = [first.result(timeout=3), second.result(timeout=3)]
    assert sum(result is not None for result in results) == 1
    assert ledger.snapshot()["counts"]["preparing"] == 2
    assert ledger.snapshot()["counts"]["available"] == 1
    assert len(ledger.attempts()) == 1


def test_failed_group_update_rolls_back_attempt_and_all_people(ledger):
    with sqlite3.connect(ledger.path) as database:
        database.execute(
            "CREATE TRIGGER injected_failure BEFORE UPDATE ON goal_people "
            "WHEN NEW.person_key='P2' AND NEW.state='preparing' "
            "BEGIN SELECT RAISE(ABORT,'injected storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        ledger.reserve("A", ("P1", "P2", "P3"))
    assert ledger.available_people() == PEOPLE
    assert ledger.attempts() == []


def test_submit_intent_is_visible_to_a_separate_connection_before_network(ledger):
    attempt = submitting(ledger, people=PEOPLE)
    with open_ledger(ledger.path) as observer:
        assert observer.attempts()[0]["state"] == "submitting"
        assert observer.snapshot()["counts"]["submitting"] == 3
        with pytest.raises(LedgerConflictError):
            observer.reserve("B", PEOPLE)
    with pytest.raises(LedgerConflictError):
        ledger.mark_submitting(attempt)


def test_recovery_requires_exclusive_coordinator_lock(ledger):
    ledger.reserve("A", ("P1",))
    with pytest.raises(LedgerConflictError, match="runner_lock"):
        ledger.recover()
    assert ledger.snapshot()["counts"]["preparing"] == 1


def test_restart_releases_only_preparing_and_freezes_submitting(tmp_path):
    path = tmp_path / "goal.sqlite3"
    with open_ledger(path) as original:
        preparing = original.reserve("A", ("P1",))
        sent = submitting(original, "B", ("P2",))
        pending = submitting(original, "C", ("P3",))
        original.complete(pending, "pending", order_id="998877")

    with open_ledger(path) as resumed, resumed.runner_lock():
        recovered = resumed.recover()
        assert recovered == {
            "released_preparing": [preparing],
            "marked_unknown": [sent],
        }
        snapshot = resumed.snapshot()
        assert snapshot["people"]["P1"]["state"] == "available"
        assert snapshot["people"]["P2"]["state"] == "unknown"
        assert snapshot["people"]["P3"]["state"] == "pending"
        assert snapshot["people"]["P3"]["order_id"] == "998877"
        assert resumed.recover() == {"released_preparing": [], "marked_unknown": []}
        with pytest.raises(LedgerConflictError):
            resumed.reserve("D", ("P2",))


@pytest.mark.parametrize("state", ["unknown", "review", "pending"])
def test_time_and_restarts_do_not_release_unresolved_people(ledger, monkeypatch, state):
    attempt = submitting(ledger, people=PEOPLE)
    ledger.complete(attempt, state, order_id=12345 if state == "pending" else None)
    monkeypatch.setattr("task.goal_ledger.time.time", lambda: 9_999_999_999)
    with open_ledger(ledger.path) as resumed, resumed.runner_lock():
        resumed.recover()
        assert resumed.available_people() == ()
        assert resumed.snapshot()["counts"][state] == 3
        assert resumed.attempts()[0]["state"] == state


def test_late_old_attempt_cannot_change_or_release_the_new_owner(ledger):
    old = submitting(ledger)
    ledger.complete(old, "no_order", reason="server_explicitly_rejected")
    new = submitting(ledger, "B")

    with pytest.raises(LedgerConflictError):
        ledger.complete(old, "pending", order_id=12345)
    ledger.complete(old, "no_order", reason="duplicate_rejection_callback")

    assert ledger.snapshot()["people"]["P1"]["attempt_id"] == new
    assert ledger.snapshot()["people"]["P1"]["state"] == "submitting"


def test_paid_people_are_counted_once_and_cannot_be_released(ledger):
    attempt = submitting(ledger, people=("P1", "P2"))
    ledger.complete(attempt, "pending", order_id="12345")
    ledger.complete(attempt, "paid", order_id=12345)
    ledger.complete(attempt, "paid", order_id=12345)
    assert ledger.snapshot()["counts"]["paid"] == 2
    assert ledger.snapshot()["complete"] is False
    assert ledger.available_people() == ("P3",)
    with pytest.raises(LedgerConflictError):
        ledger.complete(attempt, "closed", reason="untrusted_late_callback")
    with pytest.raises(LedgerConflictError):
        ledger.reserve("B", ("P1",))
    final = submitting(ledger, "B", ("P3",))
    ledger.complete(final, "pending", order_id=56789)
    assert ledger.snapshot()["complete"] is False
    ledger.complete(final, "paid")
    assert ledger.snapshot()["complete"] is True
    assert ledger.snapshot()["counts"]["paid"] == 3


def test_verified_order_closure_releases_people_and_preserves_order_history(ledger):
    attempt = submitting(ledger, people=("P1", "P2"))
    ledger.complete(attempt, "pending", order_id=12345)
    with pytest.raises(LedgerConflictError):
        ledger.complete(attempt, "no_order", reason="empty_order_list")
    with pytest.raises(ValueError, match="verified reason"):
        ledger.complete(attempt, "closed")
    ledger.complete(attempt, "closed", reason="official_order_confirmed_unpaid_closed")
    assert ledger.available_people() == PEOPLE
    assert ledger.attempts()[0]["order_id"] == "12345"
    assert ledger.attempts(active_only=True) == []


def test_unknown_can_be_reconciled_only_with_an_explicit_release_reason(ledger):
    attempt = submitting(ledger)
    ledger.complete(attempt, "unknown")
    with pytest.raises(ValueError, match="verified reason"):
        ledger.complete(attempt, "no_order")
    assert ledger.available_people() == ("P2", "P3")
    ledger.complete(attempt, "no_order", reason="user_verified_no_order")
    assert ledger.available_people() == PEOPLE


def test_order_identity_is_immutable_and_normalized(ledger):
    attempt = submitting(ledger)
    ledger.complete(attempt, "pending", order_id="00012345")
    assert ledger.attempts()[0]["order_id"] == "12345"
    with pytest.raises(LedgerConflictError):
        ledger.complete(attempt, "paid", order_id=55555)
    assert ledger.snapshot()["counts"]["pending"] == 1


def test_one_order_cannot_fulfil_different_attempts_or_people(ledger):
    first = submitting(ledger, "A", ("P1",))
    ledger.complete(first, "paid", order_id=12345)
    second = submitting(ledger, "B", ("P2",))

    with pytest.raises(LedgerConflictError, match="another attempt"):
        ledger.complete(second, "paid", order_id="00012345")

    snapshot = ledger.snapshot()
    assert snapshot["counts"]["paid"] == 1
    assert snapshot["people"]["P2"]["state"] == "submitting"
    assert snapshot["complete"] is False


@pytest.mark.parametrize(
    "order_id", [None, True, False, 0, -1, 1.5, "", "0", "bad-id", "１２３"]
)
def test_pending_requires_valid_order_id(ledger, order_id):
    attempt = submitting(ledger)
    with pytest.raises(ValueError):
        ledger.complete(attempt, "pending", order_id=order_id)
    assert ledger.snapshot()["people"]["P1"]["state"] == "submitting"


def test_pending_requires_submission_intent(ledger):
    attempt = ledger.reserve("A", ("P1",))
    with pytest.raises(LedgerConflictError, match="submission intent"):
        ledger.complete(attempt, "pending", order_id=12345)
    assert ledger.snapshot()["counts"]["preparing"] == 1


@pytest.mark.parametrize("change", ["fingerprint", "roster", "order"])
def test_existing_goal_rejects_configuration_drift_without_erasing_orders(
    ledger, change
):
    attempt = submitting(ledger)
    ledger.complete(attempt, "unknown")
    kwargs = {
        "fingerprint": {"fingerprint": "different-target"},
        "roster": {"people": ("P1", "P2", "P4")},
        "order": {"people": ("P3", "P2", "P1")},
    }[change]
    with pytest.raises(GoalConfigMismatchError):
        open_ledger(ledger.path, **kwargs)
    assert ledger.snapshot()["people"]["P1"]["state"] == "unknown"


def test_another_date_does_not_count_as_this_goal(ledger):
    attempt = submitting(ledger, people=PEOPLE)
    ledger.complete(attempt, "paid", order_id=12345)
    with open_ledger(ledger.path, scope="bili:1006859:2026-11-29") as next_day:
        assert next_day.available_people() == PEOPLE
        assert next_day.snapshot()["counts"]["paid"] == 0
        next_day.reserve("A", PEOPLE)
    assert ledger.snapshot()["counts"]["paid"] == 3


def test_stop_from_a_second_connection_blocks_send_but_preserves_existing_state(ledger):
    reserved = ledger.reserve("A", ("P1",))
    with ledger.runner_lock(), open_ledger(ledger.path) as controller:
        controller.stop("user_stop")
        with pytest.raises(LedgerStoppedError):
            ledger.mark_submitting(reserved)
        with pytest.raises(LedgerStoppedError):
            ledger.reserve("B", ("P2",))
        ledger.complete(reserved, "no_order", reason="stopped_before_send")
    assert ledger.snapshot()["stopped"] is True
    assert ledger.snapshot()["stop_reason"] == "user_stop"


def test_stop_persists_and_resume_never_clears_unknown_people(ledger):
    attempt = submitting(ledger, people=PEOPLE)
    ledger.stop("deadline")
    ledger.complete(attempt, "unknown", reason="response_lost_while_stopping")
    with open_ledger(ledger.path) as resumed, resumed.runner_lock():
        resumed.recover()
        assert resumed.snapshot()["stopped"] is True
        resumed.resume()
        assert resumed.snapshot()["stopped"] is False
        assert resumed.snapshot()["counts"]["unknown"] == 3
        assert resumed.available_people() == ()


def test_same_process_instances_cannot_run_two_coordinators(ledger):
    with open_ledger(ledger.path) as other:
        with ledger.runner_lock():
            with pytest.raises(LedgerConflictError):
                with other.runner_lock():
                    pytest.fail("second coordinator entered")
        with other.runner_lock():
            other.recover()


def test_runner_lock_released_after_body_exception(ledger):
    with pytest.raises(RuntimeError, match="injected"):
        with ledger.runner_lock():
            raise RuntimeError("injected")
    with ledger.runner_lock():
        assert ledger.recover() == {"released_preparing": [], "marked_unknown": []}


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlock error injection")
def test_unlock_error_still_closes_handle_and_clears_local_claim(ledger, monkeypatch):
    import fcntl

    original = fcntl.flock

    def fail_unlock(descriptor, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        return original(descriptor, operation)

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", fail_unlock)
        with pytest.raises(OSError, match="injected unlock failure"):
            with ledger.runner_lock():
                pass

    with ledger.runner_lock():
        assert ledger.recover() == {"released_preparing": [], "marked_unknown": []}


def test_runner_lock_is_exclusive_across_processes(ledger):
    code = """
import sys
sys.path.insert(0, sys.argv[4])
from task.goal_ledger import GoalLedger, LedgerConflictError
with GoalLedger(sys.argv[1], sys.argv[2], sys.argv[3], ('P1','P2','P3')) as ledger:
    try:
        with ledger.runner_lock():
            print('acquired')
    except LedgerConflictError:
        print('occupied')
"""
    probe_path = ledger.path.parent / "lock-probe.py"
    probe_path.write_text(code)
    repository = Path(__file__).resolve().parents[1]

    def probe():
        return subprocess.run(
            [
                sys.executable,
                str(probe_path),
                str(ledger.path),
                SCOPE,
                FINGERPRINT,
                str(repository),
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

    with ledger.runner_lock():
        assert probe() == "occupied"
    assert probe() == "acquired"


@pytest.mark.parametrize("people", [(), ("P1", "P1"), ("P1", "P4"), "P1", ("",)])
def test_invalid_or_duplicate_attendee_group_does_not_write(ledger, people):
    with pytest.raises(ValueError):
        ledger.reserve("A", people)
    assert ledger.attempts() == []
    assert ledger.available_people() == PEOPLE


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode assertion")
def test_new_ledger_file_is_private(ledger):
    assert ledger.path.stat().st_mode & 0o777 == 0o600
