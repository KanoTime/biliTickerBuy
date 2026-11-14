import httpx
import pytest
from requests import RequestException

from util.request.BiliRequest import BiliRequest
from util.request.exceptions import (
    BiliConnectionError,
    BiliOrderResultUnknownError,
)


CREATE_URL = "https://show.bilibili.com/api/ticket/order/createV2?project_id=1"
DETAIL_URL = "https://show.bilibili.com/api/ticket/project/getV2?id=1"
PREPARE_URL = "https://show.bilibili.com/api/ticket/order/prepare?project_id=1"


class FakeH2Client:
    def __init__(self, outcomes, calls):
        self.headers = {}
        self.cookies = httpx.Cookies()
        self.outcomes = outcomes
        self.calls = calls
        self.closed = False

    def _send(self, method, url):
        self.calls.append((method, url))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(
            200,
            json={"msg": "", "data": {"ok": True}},
            request=httpx.Request(method, url),
        )

    def get(self, url, *, params=None):
        return self._send("GET", url)

    def post(self, url, *, data=None, json=None):
        return self._send("POST", url)

    def close(self):
        self.closed = True


@pytest.fixture
def make_request(monkeypatch):
    def make(outcomes, **kwargs):
        request = BiliRequest(cookies=[], **kwargs)
        calls = []
        clients = []
        list_outcomes = list(outcomes)

        def build_fake_client():
            client = FakeH2Client(list_outcomes, calls)
            clients.append(client)
            return client

        # Exercise the real send/recovery code without opening a connection.
        # The default case models HTTPX's documented exception semantics;
        # custom-client cases below deliberately do not assume those semantics.
        monkeypatch.setattr(request, "_build_h2_client", build_fake_client)
        return request, calls, clients

    return make


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.TimeoutException,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.LocalProtocolError,
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.DecodingError,
    ],
)
def test_create_order_is_not_replayed_after_ambiguous_failure(make_request, error_type):
    error = error_type("request may have reached the server")
    request, calls, clients = make_request([error, None])

    with pytest.raises(BiliOrderResultUnknownError) as raised:
        request.post(CREATE_URL, data={"project_id": 1})

    assert isinstance(raised.value, RequestException)
    assert raised.value.cause is error
    assert raised.value.__cause__ is error
    assert "订单中心核实" in str(raised.value)
    assert len(calls) == 1
    assert len(clients) == 1
    assert clients[0].closed
    assert request._h2_client is None


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.LocalProtocolError])
def test_read_only_get_rebuilds_connection_and_retries_once(make_request, error_type):
    request, calls, clients = make_request([error_type("connection lost"), None])

    result = request.get(DETAIL_URL)

    assert result.status_code == 200
    assert len(calls) == 2
    assert len(clients) == 2
    assert clients[0].closed
    assert not clients[1].closed


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.LocalProtocolError])
def test_read_only_get_stops_after_second_recovery_failure(make_request, error_type):
    error = error_type("connection lost")
    request, calls, clients = make_request([error, error, None])

    with pytest.raises(BiliConnectionError) as raised:
        request.get(DETAIL_URL)

    assert raised.value.cause is error
    assert len(calls) == 2
    assert all(client.closed for client in clients)


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError]
)
def test_default_httpx_can_retry_create_failure_before_send(make_request, error_type):
    request, calls, clients = make_request([error_type("not connected"), None])

    result = request.post(CREATE_URL, data={})

    assert result.status_code == 200
    assert len(calls) == 2
    assert clients[0].closed


@pytest.mark.parametrize(
    "error_type", [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError]
)
def test_default_httpx_before_send_recovery_is_bounded(make_request, error_type):
    error = error_type("not connected")
    request, calls, clients = make_request([error, error, None])

    with pytest.raises(BiliConnectionError) as raised:
        request.post(CREATE_URL, data={})

    assert "请求尚未发送" in str(raised.value)
    assert raised.value.cause is error
    assert len(calls) == 2
    assert all(client.closed for client in clients)


def test_safe_connection_retry_does_not_allow_replay_after_read_timeout(make_request):
    error = httpx.ReadTimeout("response lost after sending")
    request, calls, _ = make_request(
        [httpx.ConnectTimeout("not connected"), error, None]
    )

    with pytest.raises(BiliOrderResultUnknownError) as raised:
        request.post(CREATE_URL, data={})

    assert raised.value.cause is error
    assert len(calls) == 2


@pytest.mark.parametrize(
    "client_options",
    [
        {"h2_client_type": FakeH2Client},
        {"h2_client_options": {"follow_redirects": True}},
    ],
)
@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
def test_custom_client_errors_do_not_prove_order_was_not_sent(
    make_request, client_options, error_type
):
    request, calls, _ = make_request(
        [error_type("custom transport error"), None], **client_options
    )

    with pytest.raises(BiliOrderResultUnknownError):
        request.post(CREATE_URL, data={})

    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        CREATE_URL,
        "https://show.bilibili.com/api/ticket/order/%63reateV2?project_id=1",
    ],
)
def test_create_route_is_recognized_with_query_and_encoded_path(make_request, url):
    request, calls, _ = make_request([httpx.LocalProtocolError("response lost"), None])

    with pytest.raises(BiliOrderResultUnknownError):
        request.post(url, data={})

    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        PREPARE_URL,
        PREPARE_URL + "&next=/api/ticket/order/createV2",
        "https://show.bilibili.com/api/ticket/order/createV2Preview",
        "https://example.test/api/ticket/order/createV2",
    ],
)
def test_other_routes_keep_existing_recovery_without_create_classification(
    make_request, url
):
    request, calls, _ = make_request(
        [httpx.LocalProtocolError("connection lost"), None]
    )

    assert request.post(url, data={}).status_code == 200
    assert len(calls) == 2


def test_get_on_create_path_is_not_classified_as_create_order(make_request):
    request, calls, _ = make_request([httpx.ReadTimeout("connection lost"), None])

    assert request.get(CREATE_URL).status_code == 200
    assert len(calls) == 2


def test_prepare_retains_bounded_read_timeout_recovery(make_request):
    error = httpx.ReadTimeout("response timed out")
    request, calls, clients = make_request([error, error, None])

    with pytest.raises(BiliConnectionError) as raised:
        request.post(PREPARE_URL, data={})

    assert "网络请求超时" in str(raised.value)
    assert raised.value.cause is error
    assert len(calls) == 2
    assert all(client.closed for client in clients)
