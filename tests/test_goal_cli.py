from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_cmd import goal as cli
from task.goal_config import load_goal_config
from task.goal_ledger import GoalLedger
from task.goal_types import PreflightResult


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def reject_network(monkeypatch):
    def blocked(*_, **__):
        pytest.fail("CLI regression tests must not contact a network service")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)


@pytest.fixture
def local_cli_goal(tmp_path):
    account_sections = []
    sources = {}
    for index, key in enumerate(("A", "B"), start=1):
        source = {
            "project_id": 1006859,
            "screen_id": 71,
            "sku_id": 82,
            "count": 3,
            "pay_money": 59400,
            "order_type": 1,
            "detail": "PRIVATE-ATTENDEE-LABEL",
            "buyer": "PRIVATE-CONTACT-NAME",
            "tel": "PRIVATE-CONTACT-PHONE",
            "buyer_info": [
                {
                    "id": index * 100 + person,
                    "name": f"PRIVATE-PERSON-{person}",
                    "personal_id": f"00000020000101000{person}",
                    "id_type": 0,
                }
                for person in (1, 2, 3)
            ],
            "deliver_info": {
                "name": "PRIVATE-CONTACT-NAME",
                "tel": "PRIVATE-CONTACT-PHONE",
                "addr_id": 1,
                "addr": "PRIVATE-ADDRESS",
            },
            "cookies": [
                {"name": "DedeUserID", "value": str(9000 + index)},
                {"name": "SESSDATA", "value": f"PRIVATE-SESSION-{key}"},
                {"name": "bili_jct", "value": f"PRIVATE-CSRF-{key}"},
            ],
        }
        sources[key] = source
        (tmp_path / f"account-{key}.json").write_text(json.dumps(source))
        account_sections.append(
            f"""
[[accounts]]
key = "{key}"
uid = "{9000 + index}"
config_path = "account-{key}.json"
mapping_confirmed = false
[accounts.buyer_ids]
P1 = "{index * 100 + 1}"
P2 = "{index * 100 + 2}"
P3 = "{index * 100 + 3}"
"""
        )
    config_path = tmp_path / "goal.toml"
    config_path.write_text(
        """
schema_version = 1
people = ["P1", "P2", "P3"]
starts_at = "2026-11-15T18:00:00+08:00"
ends_at = "2026-11-15T20:00:00+08:00"
interval_seconds = 1.0
order_poll_seconds = 10.0
state_path = "goal.sqlite3"
[target]
project_id = 1006859
screen_id = 71
sku_id = 82
admission_date = "2026-11-28"
ticket_name = "普通入场券"
price_cents = 19800
"""
        + "".join(account_sections),
        encoding="utf-8",
    )
    return config_path, load_goal_config(config_path), sources


def _open_goal(config):
    return GoalLedger(
        config.state_path, config.scope_key, config.fingerprint, config.people
    )


def _unknown_attempt(ledger):
    attempt = ledger.reserve("A", ("P1",))
    ledger.mark_submitting(attempt)
    ledger.complete(attempt, "unknown", reason="test_transport_result_unknown")
    return attempt


def _offline_subprocess(tmp_path, arguments):
    guard_dir = tmp_path / "guard"
    guard_dir.mkdir()
    report_path = tmp_path / "guard-report.json"
    (guard_dir / "sitecustomize.py").write_text(
        """
import atexit
import json
import os
import socket
import sys

events = {'network_attempts': [], 'legacy_attempts': []}

def reject_network(*args, **kwargs):
    events['network_attempts'].append('blocked')
    raise AssertionError('offline CLI attempted a network operation')

socket.create_connection = reject_network
socket.getaddrinfo = reject_network
socket.socket.connect = reject_network
socket.socket.connect_ex = reject_network
socket.socket.sendto = reject_network

class RejectLegacy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'util' or fullname.startswith('util.'):
            events['legacy_attempts'].append(fullname)
            raise AssertionError('offline CLI imported legacy util')
        return None

sys.meta_path.insert(0, RejectLegacy())

@atexit.register
def record_imports():
    events['util_loaded'] = any(
        name == 'util' or name.startswith('util.') for name in sys.modules
    )
    events['adapter_loaded'] = 'task.goal_adapter' in sys.modules
    with open(os.environ['BTB_CLI_GUARD_REPORT'], 'w') as handle:
        json.dump(events, handle)
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BTB_CLI_GUARD_REPORT"] = str(report_path)
    env["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(ROOT)])
    result = subprocess.run(
        [sys.executable, "-m", "app_cmd.goal", *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(report_path.read_text())
    assert report == {
        "network_attempts": [],
        "legacy_attempts": [],
        "util_loaded": False,
        "adapter_loaded": False,
    }
    return result


def test_real_help_process_is_local_and_does_not_import_legacy_util(tmp_path):
    result = _offline_subprocess(tmp_path, ["--help"])
    assert "preflight" in result.stdout
    assert "confirm" in result.stdout
    assert "demo" in result.stdout


def test_real_demo_process_finishes_partial_return_flow_without_network(tmp_path):
    result = _offline_subprocess(tmp_path, ["demo"])
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    final = records[-1]
    assert final["mode"] == "offline_demo"
    assert final["summary"]["counts"]["paid"] == 3
    assert final["summary"]["complete"] is True
    assert final["summary"]["stopped"] is True
    counts = [len(call["people"]) for call in final["calls"]]
    assert counts[0] == 3
    assert 2 in counts
    assert counts.count(1) >= 2
    assert "DEMO-ONLY" not in result.stdout


def test_template_has_private_permissions_and_never_overwrites(tmp_path, capsys):
    destination = tmp_path / "personal" / "goal.toml"
    assert cli.main(["template", "--output", str(destination)]) == 0
    assert destination.read_text(encoding="utf-8")
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600
    sentinel = "preserve existing private settings"
    destination.write_text(sentinel)

    assert cli.main(["template", "--output", str(destination)]) == 2
    assert destination.read_text() == sentinel
    assert sentinel not in capsys.readouterr().out


def test_default_template_uses_the_local_data_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["template"]) == 0
    assert (tmp_path / "btb_goal_data" / "goal.toml").is_file()


@pytest.mark.parametrize("valid", [True, False])
def test_validate_stays_local_and_does_not_print_private_export_values(
    local_cli_goal, monkeypatch, capsys, valid
):
    path, config, sources = local_cli_goal
    monkeypatch.setattr(
        cli, "_executors", lambda _: pytest.fail("validation built clients")
    )
    if not valid:
        sources["A"]["pay_money"] = 1
        (path.parent / "account-A.json").write_text(json.dumps(sources["A"]))

    assert cli.main(["validate", "--config", str(path)]) == (0 if valid else 2)

    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is valid
    assert "PRIVATE-" not in output
    assert "000000200001010001" not in output
    assert not config.state_path.exists()


@pytest.mark.parametrize("command", ["status", "stop"])
def test_reading_or_stopping_an_unstarted_goal_does_not_create_state(
    local_cli_goal, capsys, command
):
    path, config, _ = local_cli_goal
    assert cli.main([command, "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "not_started"
    assert not config.state_path.exists()


def test_stop_works_with_live_runner_lock_and_preserves_unknown_people(local_cli_goal):
    path, config, _ = local_cli_goal
    with _open_goal(config) as ledger, ledger.runner_lock():
        attempt = _unknown_attempt(ledger)
        assert cli.main(["stop", "--config", str(path)]) == 0
        snapshot = ledger.snapshot(include_history=False)
        assert snapshot["stopped"] is True
        assert snapshot["stop_reason"] == "user_stopped"
        assert snapshot["people"]["P1"]["state"] == "unknown"
        assert snapshot["people"]["P1"]["attempt_id"] == attempt


def test_manual_confirmation_requires_stopped_goal_and_releases_only_verified_people(
    local_cli_goal, capsys
):
    path, config, _ = local_cli_goal
    with _open_goal(config) as ledger:
        attempt = _unknown_attempt(ledger)
        command = [
            "confirm",
            "--config",
            str(path),
            "--attempt",
            attempt,
            "--outcome",
            "no_order",
        ]
        assert cli.main(command) == 2
        assert ledger.snapshot()["people"]["P1"]["state"] == "unknown"
        assert cli.main(["stop", "--config", str(path)]) == 0
        assert cli.main(command) == 0
        assert ledger.available_people() == config.people
        assert ledger.attempt(attempt)["reason"] == "user_verified:no_order"
        assert ledger.snapshot()["stopped"] is True
        assert config.state_path.is_file()
    assert "PRIVATE-" not in capsys.readouterr().out


def test_manual_confirmation_cannot_run_while_stopped_runner_is_still_alive(
    local_cli_goal,
):
    path, config, _ = local_cli_goal
    with _open_goal(config) as ledger, ledger.runner_lock():
        attempt = _unknown_attempt(ledger)
        ledger.stop("user_stopped")

        result = cli.main(
            [
                "confirm",
                "--config",
                str(path),
                "--attempt",
                attempt,
                "--outcome",
                "no_order",
            ]
        )

        assert result == 2
        assert ledger.attempt(attempt)["state"] == "unknown"
        assert ledger.available_people() == ("P2", "P3")


def test_manual_paid_result_stays_paid_and_cannot_be_reset_by_confirmation(
    local_cli_goal,
):
    path, config, _ = local_cli_goal
    with _open_goal(config) as ledger:
        attempt = _unknown_attempt(ledger)
        ledger.stop("user_stopped")
        base = ["confirm", "--config", str(path), "--attempt", attempt]
        assert cli.main(base + ["--outcome", "paid", "--order-id", "12345"]) == 0
        assert ledger.snapshot()["counts"]["paid"] == 1
        assert cli.main(base + ["--outcome", "no_order"]) == 2
        assert ledger.snapshot()["counts"]["paid"] == 1
        assert ledger.attempt(attempt)["order_id"] == "12345"


def test_preflight_uses_only_read_business_operations_and_closes_clients(
    local_cli_goal, monkeypatch, capsys
):
    import interface.project as project_module
    import task.goal_adapter as adapter_module

    path, config, _ = local_cli_goal
    calls = []

    class ReadRequest:
        def __init__(self, account):
            self.account = account
            self.closed = False

        def get(self, *, url, **_):
            calls.append(("GET", url))
            if url == adapter_module.NAV_URL:
                body = {"code": 0, "data": {"mid": self.account.uid, "isLogin": True}}
            elif "/buyer/list?" in url:
                body = {
                    "errno": 0,
                    "data": {"list": self.account.ticket_config["buyer_info"]},
                }
            else:
                pytest.fail("preflight attempted an unexpected endpoint")
            return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(body))

        def post(self, *, url, **_):
            calls.append(("POST", url))
            # This POST reads the new project-detail API. Order preparation,
            # creation and payment operations must never run in preflight.
            assert url == project_module.NEW_PROJECT_DETAIL_URL
            return SimpleNamespace(status_code=200, json=lambda: {})

        def _invalidate_h2_client(self):
            self.closed = True

    project = {
        "id": 1006859,
        "has_eticket": True,
        "screen_list": [
            {
                "id": 71,
                "name": "11月28日",
                "start_time_str": "2026-11-28 09:00:00",
                "ticket_list": [{"id": 82, "desc": "普通入场券", "price": 19800}],
            }
        ],
    }

    def read_project(*, request, project_id):
        assert project_id == 1006859
        request.post(url=project_module.NEW_PROJECT_DETAIL_URL, data={}, isJson=True)
        return copy.deepcopy(project)

    requests = [ReadRequest(account) for account in config.accounts]
    executors = {
        account.key: adapter_module.BiliGoalAccount(
            account, config.target, request=request
        )
        for account, request in zip(config.accounts, requests)
    }
    monkeypatch.setattr(adapter_module, "fetch_project_payload", read_project)
    monkeypatch.setattr(cli, "_executors", lambda _: executors)

    assert cli.main(["preflight", "--config", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "read_only_preflight"
    assert all(check["ready"] for check in output["checks"])
    assert calls
    assert all("/order/" not in url for _, url in calls)
    assert all(request.closed for request in requests)
    assert not config.state_path.exists()


def test_preflight_error_is_sanitized_and_every_client_is_closed(
    local_cli_goal, monkeypatch, capsys
):
    path, config, _ = local_cli_goal
    closed = []

    class Executor:
        def __init__(self, account):
            self.account = account

        def preflight(self):
            if self.account.key == "A":
                raise RuntimeError("PRIVATE-SESSION-A must never be echoed")
            return PreflightResult(True, account_uid="123")

        def close(self):
            closed.append(self.account.key)

    monkeypatch.setattr(
        cli, "_executors", lambda _: {a.key: Executor(a) for a in config.accounts}
    )

    assert cli.main(["preflight", "--config", str(path)]) == 2

    text = capsys.readouterr().out
    assert "PRIVATE-SESSION-A" not in text
    assert sorted(closed) == ["A", "B"]
    assert not any(check["ready"] for check in json.loads(text)["checks"])


def test_lazy_task_exports_preserve_the_existing_stream_types():
    import task
    from task.buy_types import BuyStreamEvent, BuyStreamState

    assert task.BuyStreamEvent is BuyStreamEvent
    assert task.BuyStreamState is BuyStreamState
