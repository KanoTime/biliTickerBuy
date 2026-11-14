from __future__ import annotations

import importlib
import json
from json import JSONDecodeError
from types import SimpleNamespace

import pytest
from requests import RequestException

from app_cmd.config.BuyConfig import BuyConfig
from task.buy_helpers import extract_order_id, is_create_success
from util.request.exceptions import BiliConnectionError, BiliOrderResultUnknownError


buy_module = importlib.import_module("task.buy")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def buyer(monkeypatch):
    """Run the real stream with no network, browser, QR display or notifiers."""
    request = SimpleNamespace(
        prepare_count=0,
        create_count=0,
        create_responses=[{"errno": 0, "data": {"orderId": 12345}}],
        prepare_responses=[],
        current_proxy_status=lambda: "direct",
        proxy_pool_status=lambda: "direct",
        get_user_agent=lambda: "test",
        set_100001_handler=lambda handler: None,
        prewarm_h2_connection=lambda url: None,
        handle_100001=lambda err: None,
        describe_non_json_response=lambda response: "status=200, content_type=text/html",
    )

    def post(*, url, **kwargs):
        if "/prepare?" in url:
            request.prepare_count += 1
            if request.prepare_count > 3:
                pytest.fail("stream unexpectedly re-entered order preparation")
            payload = (
                request.prepare_responses.pop(0)
                if request.prepare_responses
                else {"errno": 0, "data": {"token": "test-token"}}
            )
            return _Response(payload)
        assert "/createV2" in url
        request.create_count += 1
        if not request.create_responses:
            pytest.fail("stream attempted an unexpected second order")
        payload = request.create_responses.pop(0)
        if isinstance(payload, (RequestException, BiliConnectionError)):
            raise payload
        return _Response(payload)

    request.post = post
    request.get = lambda url: _Response(
        {"errno": 0, "data": {"code_url": "weixin://test-payment"}}
    )
    notifier = SimpleNamespace(
        start_all=lambda: None,
        join_all=lambda **kwargs: None,
        send_all_sync=lambda **kwargs: {},
    )
    monkeypatch.setattr(buy_module, "BiliRequest", lambda **kwargs: request)
    monkeypatch.setattr(
        buy_module, "fetch_project_payload", lambda **kwargs: {"hotProject": False}
    )
    monkeypatch.setattr(buy_module, "generate_browser_window_state", lambda: None)
    monkeypatch.setattr(
        buy_module,
        "init_ctoken_state",
        lambda **kwargs: SimpleNamespace(
            snapshot=lambda **kwargs: SimpleNamespace(
                generate_prepare_ctoken=lambda: "test-ctoken"
            )
        ),
    )
    monkeypatch.setattr(
        buy_module,
        "_prepare_create_request",
        lambda *args, **kwargs: (
            "https://show.bilibili.com/api/ticket/order/createV2",
            {},
        ),
    )
    monkeypatch.setattr(buy_module, "_precheck_notifiers", lambda config: None)
    monkeypatch.setattr(buy_module, "_wait_until_start", lambda *args, **kwargs: ())
    monkeypatch.setattr(buy_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        buy_module.NotifierManager, "create_from_config", lambda **kwargs: notifier
    )
    monkeypatch.setattr(buy_module.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        buy_module.qrcode,
        "QRCode",
        lambda: SimpleNamespace(
            add_data=lambda data: None,
            make=lambda **kwargs: None,
            make_image=lambda: SimpleNamespace(show=lambda: None),
        ),
    )
    config = BuyConfig(
        tickets_info=json.dumps(
            {
                "detail": "offline test",
                "cookies": [],
                "buyer_info": [{"id": 1}, {"id": 2}, {"id": 3}],
                "deliver_info": {},
                "count": 3,
                "screen_id": 1,
                "project_id": 1,
                "sku_id": 1,
                "pay_money": 59400,
            }
        ),
        show_qrcode=False,
        auto_open_payment_url=False,
        show_random_message=False,
        refresh_interval_max_count=0,
        create_retry_limit=1,
    )
    return SimpleNamespace(config=config, request=request, notifier=notifier)


@pytest.mark.parametrize("order_id", [12345, "12345", " 12345 "])
def test_create_success_requires_a_valid_order_id(order_id):
    response = {"errno": 0, "data": {"orderId": order_id}}
    assert is_create_success(response, 0)
    assert extract_order_id(response) in (12345, "12345")


@pytest.mark.parametrize(
    "order_id", [None, "", "0", 0, -1, "-1", True, False, 1.5, {}, [], "invalid"]
)
def test_invalid_order_ids_are_not_success(order_id):
    response = {"errno": 0, "data": {"orderId": order_id}}
    assert not is_create_success(response, 0)
    assert extract_order_id(response) is None


@pytest.mark.parametrize(
    "response",
    [
        {"errno": 0},
        {"errno": 0, "data": {"orderId": True}},
        {"errno": 0, "data": {"orderId": ""}},
        {"errno": 0, "msg": "defaultBBR"},
        {"data": {"orderId": 12345}},
        {"errno": False},
        {"errno": "invalid"},
        {"errno": None},
        [],
        None,
        JSONDecodeError("invalid JSON", "<html>", 0),
        BiliOrderResultUnknownError("response lost after send"),
        RequestException("unclassified create failure"),
    ],
)
def test_uncertain_create_stops_without_retry_or_success(buyer, response):
    buyer.request.create_responses = [response]
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.create_count == 1
    assert buyer.request.prepare_count == 1
    assert events[-1].state.status == "order_result_unknown"
    assert events[-1].state.order_id is None
    assert not any(
        event.state.status in {"succeeded", "completed", "order_created"}
        for event in events
    )


def test_order_id_is_published_before_optional_assistance(buyer, monkeypatch):
    calls = []

    def payment_result(request, order_id):
        calls.append("payment")
        return buy_module.build_order_detail_payment_result(order_id), None

    monkeypatch.setattr(
        buy_module, "build_payment_result_with_fallback", payment_result
    )
    stream = buy_module.buy_stream(buyer.config)
    for event in stream:
        if event.kind == "order_created":
            assert event.state.status == "order_created"
            assert event.state.order_id == 12345
            assert calls == []
            break
    else:
        pytest.fail("stream did not publish a confirmed order ID")

    remaining = list(stream)
    assert calls == ["payment"]
    assert remaining[-1].state.status == "order_created"
    assert buyer.request.create_count == 1


@pytest.mark.parametrize(
    "failure_point",
    [
        "payment",
        "browser",
        "qr",
        "notify_create",
        "notify_start",
        "notify_join",
        "notify_sync",
    ],
)
def test_assistance_failure_preserves_order_and_never_recreates(
    buyer, monkeypatch, failure_point
):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated optional assistance failure")

    if failure_point == "payment":
        monkeypatch.setattr(buy_module, "build_payment_result_with_fallback", fail)
    elif failure_point == "browser":
        buyer.config.auto_open_payment_url = True
        monkeypatch.setattr(buy_module.webbrowser, "open", fail)
    elif failure_point == "qr":
        buyer.config.show_qrcode = True
        monkeypatch.setattr(buy_module.qrcode, "QRCode", fail)
    elif failure_point == "notify_create":
        monkeypatch.setattr(buy_module.NotifierManager, "create_from_config", fail)
    else:
        method = {
            "notify_start": "start_all",
            "notify_join": "join_all",
            "notify_sync": "send_all_sync",
        }[failure_point]
        monkeypatch.setattr(buyer.notifier, method, fail)

    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.create_count == 1
    assert buyer.request.prepare_count == 1
    assert events[-1].state.status == "order_created"
    assert events[-1].state.order_id == 12345
    assert events[-1].state.order_detail_url.endswith("order_id=12345")
    assert not any(
        event.state.status in {"succeeded", "completed", "failed"} for event in events
    )


@pytest.mark.parametrize("errno", [100003, 100048, 100079])
def test_existing_order_or_limit_requires_review_not_goal_completion(buyer, errno):
    buyer.request.create_responses = [{"errno": errno, "data": {"orderId": 12345}}]
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.create_count == 1
    assert buyer.request.prepare_count == 1
    assert events[-1].state.status == "needs_order_review"
    assert not any(
        event.state.status in {"succeeded", "completed", "order_created"}
        for event in events
    )


def test_existing_order_assistance_failure_cannot_restart_creation(buyer, monkeypatch):
    buyer.request.create_responses = [{"errno": 100048, "data": {"orderId": 12345}}]

    def fail(*args, **kwargs):
        raise RuntimeError("payment lookup failed")

    monkeypatch.setattr(buy_module, "build_payment_result_with_fallback", fail)
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.create_count == 1
    assert events[-1].state.status == "needs_order_review"
    assert events[-1].state.order_id == 12345


def test_prepare_parse_failure_does_not_mean_order_result_unknown(buyer):
    buyer.request.prepare_responses = [JSONDecodeError("invalid JSON", "<html>", 0)]
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.prepare_count == 2
    assert buyer.request.create_count == 1
    assert events[-1].state.status == "order_created"
    assert not any(event.state.status == "order_result_unknown" for event in events)


def test_explicit_token_expiry_can_reprepare_before_success(buyer):
    buyer.request.create_responses.insert(0, {"errno": 100051})
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.prepare_count == 2
    assert buyer.request.create_count == 2
    assert events[-1].state.status == "order_created"


def test_confirmed_pre_send_connection_failure_can_retry(buyer):
    buyer.request.create_responses.insert(
        0, BiliConnectionError("default transport failed before sending create")
    )
    events = list(buy_module.buy_stream(buyer.config))

    assert buyer.request.prepare_count == 2
    assert buyer.request.create_count == 2
    assert events[-1].state.status == "order_created"
    assert events[-1].state.order_id == 12345
    assert not any(event.state.status == "order_result_unknown" for event in events)
