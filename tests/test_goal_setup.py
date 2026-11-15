from __future__ import annotations

import copy
import json
import stat

import pytest

from task.goal_config import GoalConfigError, load_goal_config
from task.goal_setup import GoalSetupError, import_ticket_exports


@pytest.fixture
def exports(tmp_path):
    paths = {}
    sources = {}
    export_dir = tmp_path / 'exports "quoted"'
    export_dir.mkdir()
    for index, key in enumerate(("A", "B"), start=1):
        source = {
            "project_id": 1006859,
            "screen_id": 1035758,
            "sku_id": 960028,
            "count": 3,
            "pay_money": 59400,
            "order_type": 1,
            "detail": "PRIVATE ATTENDEE LABEL",
            "buyer": "PRIVATE CONTACT NAME",
            "tel": "PRIVATE PHONE VALUE",
            "buyer_info": [
                {
                    "id": index * 100 + person,
                    "name": f"PRIVATE PERSON {person}",
                    "personal_id": f"00000020000101000{person}",
                    "id_type": 0,
                }
                for person in (1, 2, 3)
            ],
            "deliver_info": {
                "name": "PRIVATE CONTACT NAME",
                "tel": "PRIVATE PHONE VALUE",
                "addr_id": 1,
                "addr": "PRIVATE ADDRESS VALUE",
            },
            "cookies": [
                {"name": "DedeUserID", "value": str(9000 + index)},
                {"name": "SESSDATA", "value": "PRIVATE SESSION VALUE"},
                {"name": "bili_jct", "value": "PRIVATE CSRF VALUE"},
            ],
        }
        if key == "B":
            source["buyer_info"] = [source["buyer_info"][i] for i in (2, 0, 1)]
        sources[key] = source
        paths[key] = export_dir / f"account {key}.json"
    output = tmp_path / "local goal" / "goal.toml"

    def run(**kwargs):
        for key in ("A", "B"):
            paths[key].write_text(json.dumps(sources[key]), encoding="utf-8")
        options = {
            "account_a": paths["A"],
            "account_b": paths["B"],
            "ends_at": "2026-11-15T23:00:00+08:00",
            "output": output,
        }
        options.update(kwargs)
        return import_ticket_exports(**options)

    return sources, paths, output, run


def test_real_export_shapes_import_and_round_trip_without_identity_leaks(exports):
    sources, paths, output, run = exports
    original = copy.deepcopy(sources)
    result = run()
    goal = load_goal_config(output)

    assert goal.account("A").uid == "9001"
    assert goal.account("B").uid == "9002"
    assert dict(goal.account("A").buyer_ids) == {"P1": "101", "P2": "102", "P3": "103"}
    assert dict(goal.account("B").buyer_ids) == {"P1": "201", "P2": "202", "P3": "203"}
    assert goal.account("A").config_path == paths["A"].resolve()
    assert goal.account("B").config_path == paths["B"].resolve()
    assert not goal.account("A").mapping_confirmed
    assert not goal.account("B").mapping_confirmed
    assert goal.payload_for("B", ("P3",))["pay_money"] == 19800
    assert result["mapping_mode"] == "full_identity"
    assert result["goal"] == goal.public_summary()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not goal.state_path.exists()
    for source, path in ((original[key], paths[key]) for key in ("A", "B")):
        assert json.loads(path.read_text()) == source

    public = output.read_text() + json.dumps(result)
    assert "PRIVATE" not in public
    assert "SESSDATA" not in public
    assert "bili_jct" not in public
    assert "000000200001010001" not in public
    assert "personal_id" not in public


@pytest.mark.parametrize("identity", ["********0001", "0001", "UNKNOWN-DOCUMENT"])
def test_masked_short_or_unknown_identity_never_matches_by_name_or_order(
    exports, identity
):
    sources, _, output, run = exports
    for source in sources.values():
        for buyer in source["buyer_info"]:
            buyer.update(name="SAME PRIVATE NAME", personal_id=identity)
    with pytest.raises(GoalSetupError, match="--b-order") as exc:
        run()
    assert identity not in str(exc.value)
    assert "SAME PRIVATE NAME" not in str(exc.value)
    assert not output.exists()


def test_explicit_order_records_local_confirmation_for_masked_exports(exports):
    sources, _, output, run = exports
    for source in sources.values():
        for buyer in source["buyer_info"]:
            buyer["personal_id"] = "********" + buyer["personal_id"][-4:]
    result = run(b_order=(2, 3, 1))
    goal = load_goal_config(output)
    assert result["mapping_mode"] == "locally_confirmed_order"
    assert dict(goal.account("B").buyer_ids) == {"P1": "201", "P2": "202", "P3": "203"}
    assert all(account.mapping_confirmed for account in goal.accounts)


def test_explicit_order_cannot_override_complete_identity_contradiction(exports):
    _, _, output, run = exports
    with pytest.raises(GoalConfigError, match="identity fields disagree"):
        run(b_order=(1, 2, 3))
    assert not output.exists()


@pytest.mark.parametrize("b_order", [None, (2, 3, 1)])
def test_replaced_person_is_rejected_even_with_explicit_confirmation(exports, b_order):
    sources, _, output, run = exports
    sources["B"]["buyer_info"][1]["personal_id"] = "000000200001019999"
    with pytest.raises(GoalConfigError):
        run(b_order=b_order)
    assert not output.exists()


@pytest.mark.parametrize("b_order", [None, (2, 3, 1)])
def test_duplicate_complete_identity_is_rejected(exports, b_order):
    sources, _, output, run = exports
    sources["A"]["buyer_info"][1]["personal_id"] = sources["A"]["buyer_info"][0][
        "personal_id"
    ]
    with pytest.raises(GoalConfigError):
        run(b_order=b_order)
    assert not output.exists()


@pytest.mark.parametrize(
    "b_order", [(0, 1, 2), (1, 1, 3), (1, 2), (True, 2, 3), "2,3,1"]
)
def test_local_confirmation_requires_a_complete_one_based_permutation(exports, b_order):
    _, _, output, run = exports
    with pytest.raises(GoalSetupError, match="一基序号"):
        run(b_order=b_order)
    assert not output.exists()


@pytest.mark.parametrize("change", ["missing_uid", "conflicting_uid", "same_uid"])
def test_uid_must_come_unambiguously_from_two_distinct_accounts(exports, change):
    sources, _, output, run = exports
    if change == "missing_uid":
        sources["A"]["cookies"].pop(0)
    elif change == "conflicting_uid":
        sources["A"]["cookies"].append({"name": "DSUserID", "value": "9999"})
    else:
        sources["B"]["cookies"][0]["value"] = "9001"
    with pytest.raises(GoalConfigError):
        run()
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", 123),
        ("screen_id", 71),
        ("sku_id", 82),
        ("count", 2),
        ("pay_money", 59600),
    ],
)
def test_existing_goal_validation_rejects_target_count_or_price_drift(
    exports, field, value
):
    sources, _, output, run = exports
    sources["A"][field] = value
    with pytest.raises(GoalConfigError):
        run()
    assert not output.exists()


def test_export_must_have_exactly_three_selected_buyers(exports):
    sources, _, output, run = exports
    sources["A"]["buyer_info"].pop()
    with pytest.raises(GoalSetupError, match="恰好预选三位"):
        run()
    assert not output.exists()


@pytest.mark.parametrize(
    "ends_at", ["", "2026-11-15T23:00:00", "2026-11-15T17:00:00+08:00"]
)
def test_end_time_is_explicit_aware_and_after_the_sale_start(exports, ends_at):
    _, _, output, run = exports
    with pytest.raises(GoalConfigError, match="ends_at"):
        run(ends_at=ends_at)
    assert not output.exists()


def test_existing_config_and_ledger_are_preserved(exports):
    _, _, output, run = exports
    output.parent.mkdir()
    output.write_text("existing goal")
    with pytest.raises(GoalSetupError, match="目标配置已存在"):
        run()
    assert output.read_text() == "existing goal"

    output.unlink()
    ledger = output.parent / "goal.sqlite3"
    ledger.write_bytes(b"existing ledger")
    with pytest.raises(GoalSetupError, match="已有订单状态库"):
        run()
    assert ledger.read_bytes() == b"existing ledger"
    assert not output.exists()


def test_dangling_output_symlink_is_not_followed(exports):
    _, _, output, run = exports
    output.parent.mkdir()
    missing = output.parent / "must-not-be-created.toml"
    output.symlink_to(missing)
    with pytest.raises(GoalSetupError, match="目标配置已存在"):
        run()
    assert output.is_symlink()
    assert not missing.exists()


def test_output_cannot_take_the_ledger_filename(exports):
    _, _, output, run = exports
    with pytest.raises(GoalSetupError, match="订单状态库路径"):
        run(output=output.with_name("goal.sqlite3"))
    assert not output.parent.exists()


def test_unreadable_export_error_does_not_include_private_source_values(tmp_path):
    path = tmp_path / "PRIVATE PERSON.json"
    path.write_text('{"PRIVATE SESSION VALUE": broken JSON}')
    with pytest.raises(GoalSetupError) as exc:
        import_ticket_exports(
            account_a=path,
            account_b=path,
            ends_at="2026-11-15T23:00:00+08:00",
            output=tmp_path / "goal.toml",
        )
    assert "PRIVATE" not in str(exc.value)
    assert not (tmp_path / "goal.toml").exists()
