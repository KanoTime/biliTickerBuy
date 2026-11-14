from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from requests import RequestException

import task.goal_adapter as module
from task.goal_config import GoalAccount, GoalTarget
from util.request.exceptions import (
    BiliConnectionError,
    BiliOrderResultUnknownError,
    BiliRateLimitError,
)


class Response:
    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return copy.deepcopy(self.body)


class FakeRequest:
    def __init__(self, source):
        self.nav = {"code": 0, "data": {"mid": 9001, "isLogin": True}}
        self.buyers = copy.deepcopy(source["buyer_info"])
        self.prepare_responses = []
        self.create_responses = []
        self.prepares = []
        self.creates = []
        self.gets = []
        self.order_info = {"errno": 0, "data": {}}
        self.order_list = {"errno": 0, "data": {"list": []}}
        self.before_post = None

    @staticmethod
    def respond(item):
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response(item)

    def get(self, *, url, **_):
        self.gets.append(url)
        if url == module.NAV_URL:
            return self.respond(self.nav)
        if "/buyer/list" in url:
            return Response({"errno": 0, "data": {"list": self.buyers}})
        if "/order/info?" in url:
            return self.respond(self.order_info)
        if "/order/list?" in url:
            return self.respond(self.order_list)
        raise AssertionError("unexpected read-only route")

    def post(self, *, url, data, isJson):
        assert isJson is True
        if "/order/prepare?" in url:
            self.prepares.append(copy.deepcopy(data))
            item = (
                self.prepare_responses.pop(0)
                if self.prepare_responses
                else {
                    "errno": 0,
                    "data": {"token": f"fresh-token-{len(self.prepares)}"},
                }
            )
            return self.respond(item)
        assert "/order/createV2?" in url
        if self.before_post is not None:
            self.before_post()
        self.creates.append(copy.deepcopy(data))
        item = (
            self.create_responses.pop(0)
            if self.create_responses
            else {"errno": 0, "data": {"orderId": 12345}}
        )
        return self.respond(item)

    def get_user_agent(self):
        return "offline-test"


@pytest.fixture
def account_adapter(monkeypatch):
    source = {
        "project_id": 1006859,
        "screen_id": 71,
        "sku_id": 82,
        "count": 3,
        "pay_money": 59400,
        "order_type": 1,
        "detail": "PRIVATE ATTENDEE LABEL",
        "buyer": "PRIVATE CONTACT",
        "tel": "PRIVATE PHONE",
        "buyer_info": [
            {
                "id": 100 + i,
                "name": f"Example {i}",
                "personal_id": f"00000020000101000{i}",
                "accountId": 9001,
                "error_code": 0,
            }
            for i in (1, 2, 3)
        ],
        "deliver_info": {
            "name": "PRIVATE CONTACT",
            "tel": "PRIVATE PHONE",
            "addr_id": 1,
            "addr": "PRIVATE ADDRESS",
        },
        "cookies": [
            {"name": "SESSDATA", "value": "PRIVATE SESSION"},
            {"name": "bili_jct", "value": "PRIVATE CSRF"},
            {"name": "DedeUserID", "value": "9001"},
        ],
    }
    target = GoalTarget(1006859, 71, 82, "2026-11-28", "普通入场券", 19800)
    account = GoalAccount(
        "A",
        "9001",
        Path("offline-A.json"),
        {f"P{i}": str(100 + i) for i in (1, 2, 3)},
        True,
        source,
    )
    request = FakeRequest(source)
    project = {
        "id": 1006859,
        "hotProject": True,
        "has_eticket": True,
        "screen_list": [
            {
                "id": 71,
                "name": "11月28日",
                "start_time_str": "2026-11-28 09:00:00",
                "express_fee": 0,
                "ticket_list": [
                    {"id": 82, "desc": "普通入场券", "price": 19800, "clickable": True}
                ],
            }
        ],
    }
    monkeypatch.setattr(
        module, "fetch_project_payload", lambda **_: copy.deepcopy(project)
    )
    adapter = module.BiliGoalAccount(account, target, request=request)
    return adapter, request, project


def test_read_only_preflight_checks_identity_and_target_without_preparing(
    account_adapter,
):
    adapter, request, _ = account_adapter
    result = adapter.preflight()
    assert result.ready is True
    assert result.account_uid == "9001"
    assert not request.prepares and not request.creates


@pytest.mark.parametrize(
    "change",
    [
        "uid",
        "logged_out",
        "date",
        "screen",
        "sku",
        "price",
        "name",
        "buyer_id",
        "buyer_identity",
        "buyer_owner",
        "buyer_error",
    ],
)
def test_drift_blocks_before_any_create(account_adapter, change):
    adapter, request, project = account_adapter
    screen = project["screen_list"][0]
    ticket = screen["ticket_list"][0]
    if change == "uid":
        request.nav["data"]["mid"] = 9999
    elif change == "logged_out":
        request.nav["data"]["isLogin"] = False
    elif change == "date":
        screen["start_time_str"] = "2026-11-29 09:00:00"
    elif change in {"screen", "sku"}:
        (screen if change == "screen" else ticket)["id"] += 1
    elif change == "price":
        ticket["price"] = 29800
    elif change == "name":
        ticket["desc"] = "快速入场券"
    elif change == "buyer_id":
        request.buyers[0]["id"] = 999
    elif change == "buyer_identity":
        request.buyers[0]["personal_id"] = "DIFFERENT PERSON"
    elif change == "buyer_owner":
        request.buyers[0]["accountId"] = 9999
    else:
        request.buyers[0]["error_code"] = 1
    result = adapter.attempt(("P1",), lambda: pytest.fail("must not submit"))
    assert result.outcome == "blocked"
    assert result.definitive_no_order is True
    assert not request.prepares and not request.creates


def test_prepare_and_submit_use_exact_fresh_attendee_group(account_adapter):
    adapter, request, _ = account_adapter
    boundaries = []

    def before_submit():
        boundaries.append(len(request.prepares))
        assert len(request.prepares) == len(request.creates) + 1

    def check_boundary():
        assert len(boundaries) == len(request.creates) + 1

    request.before_post = check_boundary
    first = adapter.attempt(("P1", "P2"), before_submit)
    second = adapter.attempt(("P3",), before_submit)
    assert first.outcome == second.outcome == "created"
    assert boundaries == [1, 2]
    assert [p["count"] for p in request.prepares] == [2, 1]
    assert [p["count"] for p in request.creates] == [2, 1]
    assert [p["pay_money"] for p in request.creates] == [39600, 19800]
    assert [p["token"] for p in request.creates] == ["fresh-token-1", "fresh-token-2"]
    assert [b["id"] for b in json.loads(request.creates[1]["buyer_info"])] == [103]
    assert all("cookies" not in payload for payload in request.creates)


def test_ledger_or_cancellation_boundary_failure_never_sends(account_adapter):
    adapter, request, _ = account_adapter

    def fail_boundary():
        raise RuntimeError("cancelled or persistence failed")

    with pytest.raises(RuntimeError, match="cancelled"):
        adapter.attempt(("P1",), fail_boundary)
    assert len(request.prepares) == 1
    assert request.creates == []


@pytest.mark.parametrize(
    "code,outcome,no_order,scope",
    [
        (100009, "shortage", True, "account"),
        (100001, "retry", True, "account"),
        (100034, "blocked", True, "goal"),
        (100044, "blocked", True, "account"),
        (100003, "review", False, "account"),
        (100048, "review", False, "account"),
        (100079, "review", False, "account"),
        (100051, "retry", True, "account"),
        (987654321, "unknown", False, "account"),
    ],
)
def test_create_error_classification_never_mutates_price_or_retries_inside_attempt(
    account_adapter, code, outcome, no_order, scope
):
    adapter, request, _ = account_adapter
    request.create_responses = [{"errno": code, "data": {"pay_money": 99999}}]
    result = adapter.attempt(("P1", "P2", "P3"), lambda: None)
    assert (result.outcome, result.definitive_no_order, result.blocked_scope) == (
        outcome,
        no_order,
        scope,
    )
    assert len(request.creates) == len(request.prepares) == 1
    assert request.creates[0]["pay_money"] == 59400
    assert adapter.account.ticket_config["pay_money"] == 59400


@pytest.mark.parametrize(
    "response",
    [
        {"errno": 0, "data": {}},
        {"errno": 0, "data": {"orderId": True}},
        {"errno": 0, "data": {"orderId": 0}},
        {"data": {"orderId": 12345}},
        [],
        Response(ValueError("PRIVATE RESPONSE CONTENT")),
        BiliOrderResultUnknownError("PRIVATE REQUEST DETAILS"),
        RequestException("PRIVATE REQUEST DETAILS"),
        Response({}, 412),
        {"errno": 100009, "data": {"orderId": 12345}},
    ],
)
def test_uncertain_create_is_single_send_and_freezes_group(account_adapter, response):
    adapter, request, _ = account_adapter
    request.create_responses = [response]
    result = adapter.attempt(("P1",), lambda: None)
    assert result.outcome == "unknown"
    assert result.definitive_no_order is False
    assert len(request.creates) == 1
    assert "PRIVATE" not in result.reason


def test_confirmed_pre_send_connection_failure_is_retryable(account_adapter):
    adapter, request, _ = account_adapter
    request.create_responses = [BiliConnectionError("not sent")]
    result = adapter.attempt(("P1",), lambda: None)
    assert result.outcome == "retry"
    assert result.definitive_no_order is True
    assert len(request.creates) == 1


def test_429_respects_retry_after_without_internal_replay(account_adapter):
    adapter, request, _ = account_adapter
    request.create_responses = [
        BiliRateLimitError("limited", response=Response({}, 429, {"Retry-After": "17"}))
    ]
    result = adapter.attempt(("P1",), lambda: None)
    assert result.outcome == "retry"
    assert result.retry_after == 17
    assert len(request.creates) == 1


@pytest.mark.parametrize(
    "prepared",
    [
        {"errno": 0, "data": {}},
        Response(ValueError("bad prepare JSON")),
        {"errno": 0, "data": {"token": "token", "shield": {"verifyMethod": "geetest"}}},
    ],
)
def test_incomplete_or_challenged_prepare_never_crosses_submit_boundary(
    account_adapter, prepared
):
    adapter, request, _ = account_adapter
    request.prepare_responses = [prepared]
    result = adapter.attempt(("P1",), lambda: pytest.fail("must not submit"))
    assert result.definitive_no_order is True
    assert result.outcome in {"retry", "blocked"}
    assert request.creates == []


@pytest.mark.parametrize("code", [100003, 100048, 100079])
def test_prepare_existing_order_warning_keeps_people_reserved_for_review(
    account_adapter, code
):
    adapter, request, _ = account_adapter
    request.prepare_responses = [{"errno": code, "data": {"orderId": 98765}}]
    result = adapter.attempt(("P1", "P2", "P3"), lambda: pytest.fail("must not submit"))
    assert result.outcome == "review"
    assert result.definitive_no_order is False
    assert result.order_id == 98765  # Candidate only, not a successful creation.
    assert len(request.prepares) == 1
    assert request.creates == []


@pytest.mark.parametrize("flag", [4, 8])
def test_explicit_sold_out_waits_without_preparing_a_smaller_group(
    account_adapter, flag
):
    adapter, request, project = account_adapter
    project["screen_list"][0]["ticket_list"][0]["sale_flag_number"] = flag
    result = adapter.attempt(("P1", "P2", "P3"), lambda: pytest.fail("must not submit"))
    assert result.outcome == "sold_out"
    assert request.prepares == request.creates == []


def pending_attempt():
    return {
        "attempt_id": "attempt-1",
        "account_key": "A",
        "person_keys": ["P1", "P2"],
        "state": "pending",
        "order_id": "12345",
    }


def official_order(status=2):
    return {
        "order_id": "12345",
        "uid": "9001",
        "item_id": 1006859,
        "item_info": {"screen_id": "71", "screen_start_time": "2026-11-28 09:00:00"},
        "count": 2,
        "pay_money": 39600,
        "total_money": 39600,
        "status": status,
        "sub_status": 1,
        "refund_status": 0,
        "pay_time": 0 if status != 2 else 1794744000,
    }


@pytest.mark.parametrize(
    "status,expected", [(1, "pending"), (2, "paid"), (4, "closed")]
)
def test_known_created_order_is_reconciled_only_after_exact_binding_matches(
    account_adapter, status, expected
):
    adapter, request, _ = account_adapter
    request.order_info = {"errno": 0, "data": official_order(status)}
    observation = adapter.reconcile(pending_attempt())
    assert observation.verified is True
    assert observation.status == expected
    assert observation.person_keys == ("P1", "P2")
    assert observation.account_key == "A"
    assert observation.target_key == adapter.target.key


@pytest.mark.parametrize(
    "field,value",
    [
        ("order_id", "12346"),
        ("uid", "9002"),
        ("item_id", 7),
        ("count", 1),
        ("pay_money", 19800),
        ("total_money", 59400),
        ("refund_status", 1),
        ("pay_time", 0),
        ("sku_id", 999),
    ],
)
def test_wrong_order_or_refund_never_counts_as_paid(account_adapter, field, value):
    adapter, request, _ = account_adapter
    order = official_order()
    order[field] = value
    request.order_info = {"errno": 0, "data": order}
    observation = adapter.reconcile(pending_attempt())
    assert observation.status == "unknown"
    assert observation.verified is False


@pytest.mark.parametrize(
    "field,value", [("screen_id", "72"), ("screen_start_time", "2026-11-29 09:00:00")]
)
def test_wrong_admission_target_is_not_adopted(account_adapter, field, value):
    adapter, request, _ = account_adapter
    order = official_order()
    order["item_info"][field] = value
    request.order_info = {"errno": 0, "data": order}
    assert adapter.reconcile(pending_attempt()).verified is False


def test_list_fallback_selects_exact_known_id_not_first_matching_quantity(
    account_adapter,
):
    adapter, request, _ = account_adapter
    wrong = official_order()
    wrong["order_id"] = "11111"
    request.order_list = {"errno": 0, "data": {"list": [wrong, official_order()]}}
    observation = adapter.reconcile(pending_attempt())
    assert observation.verified is True
    assert observation.order_id == "12345"


@pytest.mark.parametrize(
    "state,order_id", [("unknown", None), ("unknown", "12345"), ("review", "12345")]
)
def test_unknown_or_review_attempt_cannot_adopt_an_unbound_list_order(
    account_adapter, state, order_id
):
    adapter, request, _ = account_adapter
    request.order_list = {"errno": 0, "data": {"list": [official_order()]}}
    attempt = pending_attempt()
    attempt.update(state=state, order_id=order_id)
    observation = adapter.reconcile(attempt)
    assert observation.verified is False
    assert request.gets == []


def test_empty_list_never_proves_no_order(account_adapter):
    adapter, _, _ = account_adapter
    observation = adapter.reconcile(pending_attempt())
    assert observation.status == "unknown"
    assert observation.verified is False


def test_missing_payment_or_refund_evidence_keeps_pending_occupancy(account_adapter):
    adapter, request, _ = account_adapter
    order = official_order()
    order.pop("refund_status")
    request.order_info = {"errno": 0, "data": order}
    assert adapter.reconcile(pending_attempt()).verified is False


def test_two_default_accounts_have_separate_in_memory_cookies(account_adapter):
    original, _, _ = account_adapter
    source = copy.deepcopy(original.account.ticket_config)
    source["cookies"][0]["value"] = "SECOND PRIVATE SESSION"
    second_account = GoalAccount(
        "B",
        "9001",
        Path("offline-B.json"),
        dict(original.account.buyer_ids),
        True,
        source,
    )
    first = module.BiliGoalAccount(original.account, original.target)
    second = module.BiliGoalAccount(second_account, original.target)
    try:
        assert (
            first._request.cookieManager.get_cookies_value("SESSDATA")
            == "PRIVATE SESSION"
        )
        assert (
            second._request.cookieManager.get_cookies_value("SESSDATA")
            == "SECOND PRIVATE SESSION"
        )
        assert first._request._h2_client_type is None
        assert second._request._h2_client_type is None
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("failure", ["raised_429", "http_429", "http_412"])
def test_project_detail_fallback_cannot_skip_rate_limit_or_challenge(
    account_adapter, monkeypatch, failure
):
    from interface.project import NEW_PROJECT_DETAIL_URL, fetch_project_payload

    adapter, request, _ = account_adapter
    calls = []
    status = 412 if failure == "http_412" else 429
    response = Response({}, status_code=status, headers={"Retry-After": "17"})

    def detail_post(*, url, data, isJson):
        assert url == NEW_PROJECT_DETAIL_URL
        calls.append(url)
        if failure == "raised_429":
            raise BiliRateLimitError("limited", response=response)
        return response

    monkeypatch.setattr(module, "fetch_project_payload", fetch_project_payload)
    monkeypatch.setattr(request, "post", detail_post)
    result = adapter.preflight()
    assert result.ready is False
    assert result.retryable is (status == 429)
    assert result.retry_after == (17.0 if status == 429 else 0.0)
    assert calls == [NEW_PROJECT_DETAIL_URL]
    assert request.gets == [module.NAV_URL]
    assert not request.prepares and not request.creates


@pytest.mark.parametrize(
    "failure,delay",
    [
        (Response({}, status_code=503), 3.0),
        (Response({}, status_code=429, headers={"Retry-After": "11"}), 11.0),
        (RequestException("PRIVATE REQUEST DETAILS"), 3.0),
    ],
)
def test_preflight_exposes_retry_metadata_without_sensitive_exception_text(
    account_adapter, failure, delay
):
    adapter, request, _ = account_adapter
    request.nav = failure
    result = adapter.preflight()
    assert result.ready is False
    assert result.retryable is True
    assert result.retry_after == delay
    assert "PRIVATE" not in result.reason
    assert not request.prepares and not request.creates
