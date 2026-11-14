from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from interface import execution, managed_runner
from task.buy_types import BuyStreamEvent, BuyStreamState


def event(status="running", order_id=None, message=None):
    return BuyStreamEvent(
        kind="status",
        message=message,
        state=BuyStreamState(status=status, order_id=order_id),
    )


def test_log_wording_is_not_order_evidence():
    assert (
        execution._order_outcome_from_event(
            event(message="未抢票成功，请重试"), "completed"
        )
        == "completed"
    )


@pytest.mark.parametrize("order_id", [12345, "12345"])
def test_order_without_log_message_is_recognized(order_id):
    assert (
        execution._order_outcome_from_event(
            event("order_created", order_id), "completed"
        )
        == "order_created"
    )


@pytest.mark.parametrize("order_id", [None, "", 0, "0", -1, True, "bad-id"])
def test_success_state_without_valid_order_id_is_unknown(order_id):
    assert (
        execution._order_outcome_from_event(
            event("succeeded", order_id, "抢票成功"), "completed"
        )
        == "order_result_unknown"
    )


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
def test_terminal_order_outcome_survives_later_running_event(outcome):
    assert execution._order_outcome_from_event(event(), outcome) == outcome


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
def test_managed_reconciliation_preserves_order_outcome(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(
        execution,
        "_pid_is_running",
        lambda _: pytest.fail("must not downgrade order state"),
    )
    state = {"status": outcome, "run_id": "example", "pid": 123}
    assert execution._reconcile_managed_run(tmp_path, state) is state


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
def test_status_query_keeps_order_written_during_exit(tmp_path, monkeypatch, outcome):
    run_dir = tmp_path / "example"
    run_dir.mkdir()
    state = {"status": "running", "run_id": "example", "pid": 123}
    status_path = run_dir / "status.json"
    status_path.write_text(json.dumps(state))

    def exit_with_order(_):
        state.update(
            status=outcome, order_id=12345 if outcome == "order_created" else None
        )
        status_path.write_text(json.dumps(state))
        return False

    monkeypatch.setattr(execution, "_pid_is_running", exit_with_order)

    result = execution.managed_task_status("example", runs_root=tmp_path)

    assert result["run"]["status"] == outcome
    assert result["run"]["order_id"] == state["order_id"]
    assert json.loads(status_path.read_text())["status"] == outcome
    assert not (run_dir / "result.json").exists()


def _install_fake_buy(monkeypatch, events, error=None):
    import task.buy

    class FakeBuy:
        def __init__(self, **_):
            pass

        def stream(self):
            yield from events
            if error is not None:
                raise error

    monkeypatch.setattr(task.buy, "Buy", FakeBuy)


def test_sync_result_uses_structured_event_without_message(monkeypatch):
    _install_fake_buy(monkeypatch, [event("order_created", 12345)])
    monkeypatch.setattr(
        execution,
        "validate_config",
        lambda _: SimpleNamespace(
            ok=True, normalized_config={}, to_dict=lambda: {"ok": True}
        ),
    )
    result = execution.run_buy_sync({})
    assert result["status"] == "order_created"
    assert result["order_id"] == 12345


@pytest.mark.parametrize(
    "outcome,order_id",
    [
        ("order_created", 12345),
        ("order_result_unknown", None),
        ("needs_order_review", None),
    ],
)
def test_sync_exception_preserves_order_evidence(monkeypatch, outcome, order_id):
    _install_fake_buy(
        monkeypatch, [event(outcome, order_id)], RuntimeError("auxiliary failure")
    )
    monkeypatch.setattr(
        execution,
        "validate_config",
        lambda _: SimpleNamespace(
            ok=True, normalized_config={}, to_dict=lambda: {"ok": True}
        ),
    )

    result = execution.run_buy_sync({})

    assert result["ok"] is False
    assert result["status"] == outcome
    assert result["order_id"] == order_id
    assert "auxiliary failure" in result["error"]


def test_sync_unrelated_failure_keeps_existing_exception_behavior(monkeypatch):
    _install_fake_buy(monkeypatch, [], RuntimeError("startup failure"))
    monkeypatch.setattr(
        execution,
        "validate_config",
        lambda _: SimpleNamespace(
            ok=True, normalized_config={}, to_dict=lambda: {"ok": True}
        ),
    )

    with pytest.raises(RuntimeError, match="startup failure"):
        execution.run_buy_sync({})


@pytest.mark.parametrize(
    "outcome,order_id",
    [
        ("order_created", 12345),
        ("order_result_unknown", None),
        ("needs_order_review", None),
    ],
)
def test_managed_runner_persists_structured_outcome(
    tmp_path, monkeypatch, outcome, order_id
):
    _install_fake_buy(monkeypatch, [event(outcome, order_id)])
    for name, payload in {
        "run.json": {"run_id": "example", "created_at": 1},
        "config.json": {},
        "runtime.json": {},
        "status.json": {},
    }.items():
        (tmp_path / name).write_text(json.dumps(payload))

    assert managed_runner.main(str(tmp_path)) == 0
    status = json.loads((tmp_path / "status.json").read_text())
    result = json.loads((tmp_path / "result.json").read_text())
    assert status["status"] == result["status"] == outcome
    assert status["order_id"] == result["order_id"] == order_id


def test_runner_exception_after_order_does_not_erase_it(tmp_path, monkeypatch):
    _install_fake_buy(
        monkeypatch, [event("order_created", 12345)], RuntimeError("auxiliary failure")
    )
    for name, payload in {
        "run.json": {"run_id": "example", "created_at": 1},
        "config.json": {},
        "runtime.json": {},
        "status.json": {},
    }.items():
        (tmp_path / name).write_text(json.dumps(payload))

    assert managed_runner.main(str(tmp_path)) == 1
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "order_created"
    assert result["order_id"] == 12345
    assert "auxiliary failure" in result["error"]
