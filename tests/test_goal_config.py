from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from task.goal_config import (
    GoalConfig,
    GoalConfigError,
    GoalTarget,
    load_goal_config,
)


@pytest.fixture
def local_goal(tmp_path):
    sources = {}
    for index, alias in enumerate(("A", "B"), start=1):
        source = {
            "project_id": 1006859,
            "screen_id": 71,
            "sku_id": 82,
            "count": 3,
            "pay_money": 59400,
            "order_type": 1,
            "detail": "PRIVATE ATTENDEE LABEL",
            "buyer": "PRIVATE CONTACT NAME",
            "tel": "OFFLINE CONTACT VALUE",
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
                "tel": "OFFLINE CONTACT VALUE",
                "addr_id": 1,
                "addr": "PRIVATE ADDRESS VALUE",
            },
            "cookies": [
                {"name": "DedeUserID", "value": str(9000 + index)},
                {"name": "SESSDATA", "value": "PRIVATE SESSION VALUE"},
                {"name": "bili_jct", "value": "PRIVATE CSRF VALUE"},
            ],
        }
        sources[alias] = source

    data = {
        "schema_version": 1,
        "target": {
            "project_id": 1006859,
            "screen_id": 71,
            "sku_id": 82,
            "admission_date": "2026-11-28",
            "ticket_name": "普通入场券",
            "price_cents": 19800,
        },
        "people": ["P1", "P2", "P3"],
        "accounts": [
            {
                "key": alias,
                "uid": str(9000 + index),
                "config_path": f"{alias}.json",
                "buyer_ids": {
                    f"P{person}": index * 100 + person for person in (1, 2, 3)
                },
                "mapping_confirmed": False,
            }
            for index, alias in enumerate(("A", "B"), start=1)
        ],
        "starts_at": "2026-11-15T18:00:00+08:00",
        "ends_at": "2026-11-15T19:00:00+08:00",
        "interval_seconds": 1,
        "order_poll_seconds": 10,
    }

    def load():
        for alias, source in sources.items():
            (tmp_path / f"{alias}.json").write_text(
                json.dumps(source), encoding="utf-8"
            )
        return GoalConfig.from_mapping(data, base_dir=tmp_path)

    return data, sources, load


def test_two_account_batch_uses_only_selected_identity_and_exact_price(local_goal):
    _, sources, load = local_goal
    goal = load()

    pair = goal.payload_for("A", ("P1", "P3"))
    single = goal.payload_for("B", ("P2",))

    assert pair["count"] == 2
    assert pair["pay_money"] == 39600
    assert [buyer["id"] for buyer in pair["buyer_info"]] == [101, 103]
    assert single["count"] == 1
    assert single["pay_money"] == 19800
    assert [buyer["id"] for buyer in single["buyer_info"]] == [202]
    assert sources["A"]["count"] == 3
    assert goal.account("A").ticket_config["count"] == 3
    pair["buyer_info"][0]["name"] = "changed only in returned payload"
    pair["cookies"][0]["value"] = "changed only in returned payload"
    assert (
        goal.account("A").ticket_config["buyer_info"][0]["name"] == "PRIVATE PERSON 1"
    )
    assert goal.account("A").ticket_config["cookies"][0]["value"] == "9001"


def test_batch_removes_stale_order_tokens_and_personal_detail_label(local_goal):
    _, sources, load = local_goal
    sources["A"].update(token="old", ptoken="old", ctoken="old", timestamp=1, again=1)
    payload = load().payload_for("A", ("P1",))
    assert not {"token", "ptoken", "ctoken", "timestamp", "again"}.intersection(payload)
    assert "PRIVATE" not in payload["detail"]
    assert "P1" in payload["detail"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", 2),
        ("screen_id", 72),
        ("sku_id", 83),
        ("count", 2),
        ("pay_money", 59600),
        ("unit_price", 29800),
        ("order_type", 2),
    ],
)
def test_export_drift_cannot_be_silently_corrected(local_goal, field, value):
    _, sources, load = local_goal
    sources["B"][field] = value
    with pytest.raises(GoalConfigError, match="account B"):
        load()


@pytest.mark.parametrize(
    "person_keys", [(), ("P1", "P1"), ("P4",), "P1", ({"name": "private"},)]
)
def test_batch_rejects_duplicates_unknown_people_and_invalid_groups(
    local_goal, person_keys
):
    goal = local_goal[2]()
    with pytest.raises(GoalConfigError):
        goal.payload_for("A", person_keys)


def test_cross_account_mapping_cannot_reassign_a_complete_identity(local_goal):
    data, _, load = local_goal
    data["accounts"][1]["buyer_ids"]["P1"] = 202
    data["accounts"][1]["buyer_ids"]["P2"] = 201
    data["accounts"][1]["mapping_confirmed"] = True
    with pytest.raises(GoalConfigError, match="complete identity fields disagree"):
        load()


def test_same_complete_identity_cannot_be_two_people(local_goal):
    _, sources, load = local_goal
    sources["A"]["buyer_info"][1]["personal_id"] = sources["A"]["buyer_info"][0][
        "personal_id"
    ]
    with pytest.raises(GoalConfigError, match="same complete identity"):
        load()


@pytest.mark.parametrize(
    "identity", ["0000***********001", "123456", "AB123456", "张某某"]
)
def test_masked_or_unverified_document_format_requires_local_confirmation(
    local_goal, identity
):
    data, sources, load = local_goal
    sources["B"]["buyer_info"][0]["personal_id"] = identity
    with pytest.raises(GoalConfigError, match="mapping_confirmed"):
        load()
    data["accounts"][1]["mapping_confirmed"] = True
    assert load().people == ("P1", "P2", "P3")


def test_matching_names_and_masked_suffixes_do_not_automatically_match_people(
    local_goal,
):
    _, sources, load = local_goal
    for source in sources.values():
        source["buyer_info"][0].update(name="same name", personal_id="********1234")
    with pytest.raises(GoalConfigError, match="mapping_confirmed"):
        load()


def test_each_account_requires_all_three_unique_buyer_ids(local_goal):
    data, _, load = local_goal
    data["accounts"][0]["buyer_ids"]["P2"] = 101
    with pytest.raises(GoalConfigError, match="unique ID"):
        load()


def test_export_cannot_hide_an_extra_or_missing_person(local_goal):
    _, sources, load = local_goal
    sources["A"]["buyer_info"].pop()
    with pytest.raises(GoalConfigError, match="exactly match"):
        load()


def test_same_uid_cannot_be_configured_as_two_accounts(local_goal):
    data, sources, load = local_goal
    data["accounts"][1]["uid"] = "9001"
    sources["B"]["cookies"][0]["value"] = "9001"
    with pytest.raises(GoalConfigError, match="different UID"):
        load()


def test_cookie_account_mismatch_is_redacted(local_goal):
    _, sources, load = local_goal
    sources["A"]["cookies"][0]["value"] = "999999"
    with pytest.raises(GoalConfigError, match="cookie UID differs") as caught:
        load()
    assert "999999" not in str(caught.value)
    assert "PRIVATE" not in str(caught.value)


def test_cookie_rotation_does_not_change_goal_fingerprint(local_goal):
    data, sources, load = local_goal
    first = load()
    sources["A"]["cookies"][1]["value"] = "replacement session"
    data["ends_at"] = "2026-11-15T20:00:00+08:00"
    data["interval_seconds"] = 2
    data["order_poll_seconds"] = 20
    second = load()
    assert first.fingerprint == second.fingerprint
    assert first.scope_key == second.scope_key


def test_goal_fingerprint_binds_uid_mapping_target_and_price(local_goal):
    data, sources, load = local_goal
    first = load()
    sources["A"]["cookies"][0]["value"] = "9003"
    data["accounts"][0]["uid"] = "9003"
    assert load().fingerprint != first.fingerprint
    data["accounts"][0]["uid"] = "9001"
    sources["A"]["cookies"][0]["value"] = "9001"
    data["target"]["price_cents"] = 19900
    for source in sources.values():
        source["pay_money"] = 59700
    priced = load()
    assert priced.scope_key == first.scope_key
    assert priced.fingerprint != first.fingerprint


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "REPLACEMENT ATTENDEE"),
        ("id_type", 7),
        ("personal_id", "000000200001019999"),
    ],
)
def test_identity_change_with_stable_buyer_ids_changes_aggregate_fingerprint(
    local_goal, field, value
):
    _, sources, load = local_goal
    original = load()
    for source in sources.values():
        source["buyer_info"][0][field] = value
    changed = load()

    assert changed.scope_key == original.scope_key
    assert changed.account("A").uid == original.account("A").uid
    assert changed.account("A").buyer_ids == original.account("A").buyer_ids
    assert changed.fingerprint != original.fingerprint
    assert len(changed.fingerprint) == 64
    public = repr(changed.public_summary())
    assert "attendee_identity_snapshot" not in public
    assert "personal_id" not in public
    assert "id_type" not in public
    if isinstance(value, str):
        assert value not in public


def test_replacing_person_cannot_reuse_the_old_paid_goal(local_goal):
    from task.goal_ledger import GoalConfigMismatchError, GoalLedger

    _, sources, load = local_goal
    original = load()
    with GoalLedger(
        original.state_path,
        original.scope_key,
        original.fingerprint,
        original.people,
    ) as ledger:
        attempt = ledger.reserve("A", ("P1",))
        ledger.mark_submitting(attempt)
        ledger.complete(attempt, "paid", order_id=12345)

    for source in sources.values():
        source["buyer_info"][0]["personal_id"] = "000000200001019999"
    replacement = load()
    with pytest.raises(GoalConfigMismatchError):
        GoalLedger(
            replacement.state_path,
            replacement.scope_key,
            replacement.fingerprint,
            replacement.people,
        )
    with GoalLedger(
        original.state_path,
        original.scope_key,
        original.fingerprint,
        original.people,
    ) as ledger:
        assert ledger.snapshot()["counts"]["paid"] == 1


def test_identity_snapshot_normalizes_outer_whitespace_and_document_case(local_goal):
    _, sources, load = local_goal
    for source in sources.values():
        source["buyer_info"][0]["personal_id"] = "00000020000101000X"
    original = load()
    for source in sources.values():
        source["buyer_info"][0]["personal_id"] = " 00000020000101000x "
        source["buyer_info"][0]["name"] = " PRIVATE PERSON 1 "
        source["buyer_info"][0]["id_type"] = "0"
    assert load().fingerprint == original.fingerprint


def test_cookie_uid_change_after_load_is_checked_before_batch_creation(local_goal):
    goal = local_goal[2]()
    goal.account("A").ticket_config["cookies"][0]["value"] = "9002"
    with pytest.raises(GoalConfigError, match="cookie UID changed"):
        goal.payload_for("A", ("P1",))


def test_repr_summary_and_fingerprint_do_not_expose_ticket_payload(local_goal):
    goal = local_goal[2]()
    public = repr(goal) + json.dumps(goal.public_summary()) + goal.fingerprint
    assert "PRIVATE" not in public
    assert "000000200001010001" not in public
    assert "SESSDATA" not in public


def test_window_uses_explicit_timezone_and_accepts_already_expired_configuration(
    local_goal,
):
    data, _, load = local_goal
    data["starts_at"] = "2020-01-01T10:00:00Z"
    data["ends_at"] = "2020-01-01T11:00:00Z"
    goal = load()
    assert goal.starts_at.hour == 18
    assert goal.starts_at.utcoffset() == timedelta(hours=8)
    assert goal.ends_at.year == 2020


@pytest.mark.parametrize(
    "field,value",
    [
        ("ends_at", ""),
        ("starts_at", "2026-11-15T18:00:00"),
        ("ends_at", "2026-11-15T17:00:00+08:00"),
        ("interval_seconds", 0),
        ("interval_seconds", float("nan")),
        ("order_poll_seconds", True),
    ],
)
def test_invalid_window_or_polling_configuration_is_rejected(local_goal, field, value):
    data, _, load = local_goal
    data[field] = value
    with pytest.raises(GoalConfigError, match=field):
        load()


@pytest.mark.parametrize(
    "field,value",
    [
        ("screen_id", 0),
        ("sku_id", True),
        ("price_cents", 198.0),
        ("admission_date", "20261128"),
    ],
)
def test_target_requires_exact_ids_integer_cents_and_iso_date(local_goal, field, value):
    data, _, load = local_goal
    data["target"][field] = value
    with pytest.raises(GoalConfigError):
        load()


def test_exact_target_key_and_shared_daily_scope_have_different_purposes(local_goal):
    goal = local_goal[2]()
    changed = replace(goal.target, sku_id=99)
    assert goal.target.key != changed.key
    assert str(goal.target.project_id) in goal.scope_key
    assert goal.target.admission_date in goal.scope_key
    assert "9001" not in goal.scope_key


def test_default_state_file_is_relative_to_goal_config_directory(local_goal, tmp_path):
    assert local_goal[2]().state_path == tmp_path / "btb_goal_data" / "goal.sqlite3"


def test_state_file_cannot_overwrite_account_config(local_goal):
    data, _, load = local_goal
    data["state_path"] = "A.json"
    with pytest.raises(GoalConfigError, match="overwrite"):
        load()


def test_unrecognised_options_do_not_silently_change_purchasing_intent(local_goal):
    data, _, load = local_goal
    data["allow_price_change"] = True
    with pytest.raises(GoalConfigError, match="unsupported option"):
        load()


def test_toml_loader_resolves_account_paths_without_network(local_goal, tmp_path):
    local_goal[2]()
    path = tmp_path / "goal.toml"
    path.write_text(
        """schema_version = 1
people = ["P1", "P2", "P3"]
starts_at = "2026-11-15T18:00:00+08:00"
ends_at = "2026-11-15T19:00:00+08:00"
[target]
project_id = 1006859
screen_id = 71
sku_id = 82
admission_date = "2026-11-28"
ticket_name = "普通入场券"
price_cents = 19800
[[accounts]]
key = "A"
uid = "9001"
config_path = "A.json"
mapping_confirmed = false
[accounts.buyer_ids]
P1 = "101"
P2 = "102"
P3 = "103"
[[accounts]]
key = "B"
uid = "9002"
config_path = "B.json"
mapping_confirmed = false
[accounts.buyer_ids]
P1 = "201"
P2 = "202"
P3 = "203"
""",
        encoding="utf-8",
    )
    goal = load_goal_config(path)
    assert goal.account("A").config_path == tmp_path / "A.json"
    assert goal.target.price_cents == 19800


def test_sample_pins_public_target_but_requires_local_account_ids():
    sample = (
        Path(__file__).resolve().parents[1] / "examples" / "purchase-goal.example.toml"
    )
    with pytest.raises(GoalConfigError, match="account A.uid"):
        load_goal_config(sample)


def test_error_from_private_export_parse_does_not_echo_content(local_goal, tmp_path):
    data, _, _ = local_goal
    (tmp_path / "A.json").write_text(
        '{"cookie": "PRIVATE SESSION VALUE" BROKEN', encoding="utf-8"
    )
    with pytest.raises(GoalConfigError) as caught:
        GoalConfig.from_mapping(copy.deepcopy(data), base_dir=tmp_path)
    assert "PRIVATE" not in str(caught.value)


def test_direct_constructor_rejects_naive_datetime(local_goal):
    goal = local_goal[2]()
    with pytest.raises(GoalConfigError, match="timezone"):
        replace(goal, starts_at=datetime(2026, 11, 15, 18))
    aware = replace(goal, starts_at=datetime(2026, 11, 15, 9, tzinfo=timezone.utc))
    assert aware.starts_at.hour == 17


def test_goal_target_constructor_is_generic_but_keeps_exact_declared_price():
    target = GoalTarget(123, 456, 789, "2027-01-02", "普通票", 12300)
    assert target.price_cents == 12300
