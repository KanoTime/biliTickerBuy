from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from interface import execution


@pytest.fixture
def start_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        execution,
        "validate_config",
        lambda _: SimpleNamespace(
            ok=True, normalized_config={}, to_dict=lambda: {"ok": True}
        ),
    )
    monkeypatch.setattr(execution.time, "sleep", lambda _: None)
    return {"run_id": "example", "runs_root": tmp_path}


def _fake_popen(monkeypatch, *, outcome=None, returncode=None, result_only=False):
    def popen(command, **kwargs):
        run_dir = Path(command[-1])
        if outcome is not None:
            fields = {"status": outcome, "order_id": 12345, "pid": 4321}
            if not result_only:
                status = execution._load_json(run_dir / "status.json")
                execution._dump_json(run_dir / "status.json", {**status, **fields})
            else:
                execution._dump_json(run_dir / "result.json", fields)
        return SimpleNamespace(pid=4321, poll=lambda: returncode)

    monkeypatch.setattr(execution.subprocess, "Popen", popen)


def test_start_cannot_overwrite_order_published_inside_popen(start_setup, monkeypatch):
    _fake_popen(monkeypatch, outcome="order_created")

    result = execution.start_managed_buy({}, **start_setup)
    run_dir = start_setup["runs_root"] / start_setup["run_id"]
    status = execution._load_json(run_dir / "status.json")

    assert result["ok"] is True
    assert status["status"] == result["run"]["status"] == "order_created"
    assert status["order_id"] == 12345
    assert result["run"]["process_running"] is True
    assert execution._load_json(run_dir / "run.json")["pid"] == 4321


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
@pytest.mark.parametrize("returncode", [0, 1])
def test_fast_exit_preserves_order_outcome(
    start_setup, monkeypatch, outcome, returncode
):
    _fake_popen(monkeypatch, outcome=outcome, returncode=returncode)

    result = execution.start_managed_buy({}, **start_setup)
    run_dir = start_setup["runs_root"] / start_setup["run_id"]

    assert result["ok"] is (returncode == 0)
    assert result["run"]["status"] == outcome
    assert result["run"]["process_running"] is False
    assert result["run"]["returncode"] == returncode
    assert execution._load_json(run_dir / "status.json")["status"] == outcome


def test_fast_exit_can_recover_terminal_result_file(start_setup, monkeypatch):
    _fake_popen(monkeypatch, outcome="order_created", returncode=0, result_only=True)
    monkeypatch.setattr(
        execution,
        "_pid_is_running",
        lambda _: pytest.fail("poll already confirmed process exit"),
    )

    result = execution.start_managed_buy({}, **start_setup)

    assert result["ok"] is True
    assert result["run"]["status"] == "order_created"
    assert result["run"]["order_id"] == 12345


def test_normal_fast_completion_is_not_unconditionally_failed(start_setup, monkeypatch):
    _fake_popen(monkeypatch, outcome="completed", returncode=0)

    result = execution.start_managed_buy({}, **start_setup)

    assert result["ok"] is True
    assert result["run"]["status"] == "completed"


def test_started_pid_is_metadata_without_rewriting_child_status(
    start_setup, monkeypatch
):
    _fake_popen(monkeypatch)
    execution.start_managed_buy({}, **start_setup)
    run_dir = start_setup["runs_root"] / start_setup["run_id"]
    before = execution._load_json(run_dir / "status.json")
    assert before["pid"] is None
    checked_pids = []

    def is_running(pid):
        checked_pids.append(pid)
        return True

    monkeypatch.setattr(execution, "_pid_is_running", is_running)
    result = execution.managed_task_status(**start_setup)

    assert checked_pids == [4321]
    assert result["run"]["status"] == "pending"
    assert execution._load_json(run_dir / "status.json") == before


def test_spawn_failure_is_recorded_without_leaving_uncancellable_pending(
    start_setup, monkeypatch
):
    def fail(*args, **kwargs):
        raise OSError("cannot launch runner")

    monkeypatch.setattr(execution.subprocess, "Popen", fail)
    result = execution.start_managed_buy({}, **start_setup)

    assert result["ok"] is False
    assert result["run"]["status"] == "failed"


def _seed_run(tmp_path, *, outcome="running", pid=4321, metadata_pid=4321):
    run_dir = tmp_path / "example"
    run_dir.mkdir()
    status = {
        "run_id": "example",
        "status": outcome,
        "pid": pid,
        "order_id": 12345 if outcome == "order_created" else None,
        "updated_at": execution.time.time(),
        "last_message": "previous order event",
    }
    execution._dump_json(run_dir / "status.json", status)
    execution._dump_json(run_dir / "run.json", {"pid": metadata_pid})
    return run_dir


@pytest.fixture
def process_control(monkeypatch):
    control = SimpleNamespace(alive=True, terminated=[])

    def terminate(pid):
        control.terminated.append(pid)
        control.alive = False

    monkeypatch.setattr(
        execution, "_pid_is_running", lambda pid: bool(pid) and control.alive
    )
    monkeypatch.setattr(execution, "_terminate_pid", terminate)
    monkeypatch.setattr(execution, "_wait_for_pid_exit", lambda pid: not control.alive)
    return control


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
def test_cancel_stops_assistance_process_and_preserves_order(
    tmp_path, process_control, outcome
):
    run_dir = _seed_run(tmp_path, outcome=outcome)

    result = execution.cancel_managed_buy("example", runs_root=tmp_path)

    assert process_control.terminated == [4321]
    assert result["ok"] is True
    assert result["cancelled"] is False
    assert result["process_stopped"] is True
    assert result["run"]["status"] == outcome
    assert execution._load_json(run_dir / "status.json")["status"] == outcome
    assert execution._load_json(run_dir / "result.json")["status"] == outcome


def test_cancel_reloads_order_published_during_termination(
    tmp_path, process_control, monkeypatch
):
    run_dir = _seed_run(tmp_path)

    def terminate(pid):
        status = execution._load_json(run_dir / "status.json")
        status.update(status="order_created", order_id=12345)
        execution._dump_json(run_dir / "status.json", status)
        process_control.alive = False

    monkeypatch.setattr(execution, "_terminate_pid", terminate)
    result = execution.cancel_managed_buy("example", runs_root=tmp_path)

    assert result["run"]["status"] == "order_created"
    assert result["run"]["order_id"] == 12345
    assert result["cancelled"] is False


def test_cancel_uses_parent_pid_when_child_has_not_written_pid(
    tmp_path, process_control
):
    _seed_run(tmp_path, outcome="pending", pid=None)

    result = execution.cancel_managed_buy("example", runs_root=tmp_path)

    assert process_control.terminated == [4321]
    assert result["run"]["status"] == "cancelled"
    assert result["process_stopped"] is True


def test_cancel_timeout_does_not_claim_cancelled_or_overwrite_order(
    tmp_path, monkeypatch, process_control
):
    run_dir = _seed_run(tmp_path, outcome="order_result_unknown")
    before = (run_dir / "status.json").read_text()
    monkeypatch.setattr(execution, "_terminate_pid", lambda pid: None)

    result = execution.cancel_managed_buy("example", runs_root=tmp_path)

    assert result["ok"] is False
    assert result["process_stopped"] is False
    assert result["cancelled"] is False
    assert (run_dir / "status.json").read_text() == before
    assert not (run_dir / "result.json").exists()


def test_force_delete_waits_for_actual_process_exit(tmp_path, process_control):
    run_dir = _seed_run(tmp_path, outcome="order_created", pid=None)

    result = execution.delete_managed_buy("example", runs_root=tmp_path, force=True)

    assert result["ok"] is True
    assert result["cancelled"]["run"]["status"] == "order_created"
    assert process_control.terminated == [4321]
    assert process_control.alive is False
    assert not run_dir.exists()


def test_force_delete_preserves_files_if_process_cannot_stop(
    tmp_path, monkeypatch, process_control
):
    run_dir = _seed_run(tmp_path, outcome="order_created")
    monkeypatch.setattr(execution, "_terminate_pid", lambda pid: None)

    result = execution.delete_managed_buy("example", runs_root=tmp_path, force=True)

    assert result["ok"] is False
    assert run_dir.exists()
    assert execution._load_json(run_dir / "status.json")["status"] == "order_created"


def test_force_delete_independently_rechecks_pid_after_cancel(
    tmp_path, monkeypatch, process_control
):
    run_dir = _seed_run(tmp_path, outcome="order_created")
    monkeypatch.setattr(execution, "cancel_managed_buy", lambda *a, **k: {"ok": True})

    result = execution.delete_managed_buy("example", runs_root=tmp_path, force=True)

    assert result["ok"] is False
    assert run_dir.exists()


def test_non_force_delete_refuses_live_terminal_order(tmp_path, process_control):
    run_dir = _seed_run(tmp_path, outcome="needs_order_review")

    result = execution.delete_managed_buy("example", runs_root=tmp_path)

    assert result["ok"] is False
    assert process_control.terminated == []
    assert run_dir.exists()


@pytest.mark.parametrize("outcome", sorted(execution.ORDER_OUTCOME_STATUSES))
@pytest.mark.parametrize("timestamp_key", ["finished_at", "process_stopped_at"])
def test_cancel_does_not_signal_historical_pid_of_finished_order(
    tmp_path, process_control, outcome, timestamp_key
):
    run_dir = _seed_run(tmp_path, outcome=outcome)
    status = execution._load_json(run_dir / "status.json")
    status[timestamp_key] = 1
    execution._dump_json(run_dir / "status.json", status)

    result = execution.cancel_managed_buy("example", runs_root=tmp_path)

    assert result["ok"] is True
    assert result["run"]["status"] == outcome
    assert process_control.terminated == []
    assert execution._load_json(run_dir / "status.json") == status


def test_force_delete_does_not_kill_reused_historical_pid(tmp_path, process_control):
    run_dir = _seed_run(tmp_path, outcome="order_created")
    execution._update_managed_status(run_dir, finished_at=1)

    result = execution.delete_managed_buy("example", runs_root=tmp_path, force=True)

    assert result["ok"] is False
    assert process_control.terminated == []
    assert run_dir.exists()


def test_windows_exit_code_query_failure_does_not_prove_exit(monkeypatch):
    closed_handles = []
    kernel = SimpleNamespace(
        OpenProcess=lambda *args: 42,
        GetExitCodeProcess=lambda *args: 0,
        CloseHandle=lambda handle: closed_handles.append(handle),
    )
    monkeypatch.setattr(execution, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        execution,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(kernel32=kernel),
            c_ulong=execution.ctypes.c_ulong,
            byref=execution.ctypes.byref,
        ),
    )

    assert execution._pid_is_running(4321) is True
    assert closed_handles == [42]


@pytest.mark.parametrize("operation", ["cancel_managed_buy", "delete_managed_buy"])
def test_startup_without_recorded_pid_does_not_allow_cancel_or_delete(
    tmp_path, monkeypatch, operation
):
    run_dir = _seed_run(tmp_path, outcome="pending", pid=None, metadata_pid=None)
    monkeypatch.setattr(
        execution, "_terminate_pid", lambda _: pytest.fail("no known runner PID")
    )

    result = getattr(execution, operation)("example", runs_root=tmp_path)

    assert result["ok"] is False
    assert run_dir.exists()
    assert execution._load_json(run_dir / "status.json")["status"] == "pending"


def test_reconcile_startup_before_pid_metadata_does_not_mark_failed(tmp_path):
    run_dir = _seed_run(tmp_path, outcome="pending", pid=None, metadata_pid=None)

    result = execution.managed_task_status("example", runs_root=tmp_path)

    assert result["run"]["status"] == "pending"
    assert execution._load_json(run_dir / "status.json")["status"] == "pending"


def test_stale_heartbeat_does_not_overwrite_live_runner_status(
    tmp_path, process_control
):
    run_dir = _seed_run(tmp_path)
    status = execution._load_json(run_dir / "status.json")
    status["updated_at"] = 1
    execution._dump_json(run_dir / "status.json", status)

    result = execution.managed_task_status("example", runs_root=tmp_path)

    assert result["run"]["heartbeat_stale"] is True
    assert result["run"]["status"] == "running"
    assert execution._load_json(run_dir / "status.json") == status


@pytest.mark.skipif(execution.os.name == "nt", reason="POSIX child reaping")
def test_wait_for_exit_reaps_own_terminated_child(monkeypatch):
    monkeypatch.setattr(execution.os, "waitpid", lambda pid, options: (pid, 0))
    monkeypatch.setattr(
        execution,
        "_pid_is_running",
        lambda _: pytest.fail("reaped child is already known to have exited"),
    )

    assert execution._wait_for_pid_exit(4321) is True
