"""Bounded Bilibili account operations for the personal purchase coordinator.

An attempt prepares its exact attendee set afresh and exposes the final submit
boundary to the durable ledger. This module never loops creation internally or
solves verification challenges. See docs/goal-api-evidence.md for order-query
evidence and the conservative schema requirements used below.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from cptoken import generate_browser_window_state, init_ctoken_state
from interface.project import fetch_project_payload
from task.buy_helpers import (
    build_token_payload,
    extract_order_id,
    is_create_success,
    prepare_create_request,
)
from task.goal_config import GoalAccount, GoalConfigError, GoalTarget
from task.goal_types import AttemptResult, OrderObservation, PreflightResult
from util import time_service
from util.Constant import BASE_URL, BEIJING_TZ
from util.request.BiliRequest import BiliRequest
from util.request.exceptions import (
    BiliConnectionError,
    BiliOrderResultUnknownError,
    BiliRateLimitError,
)


NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BUYERS_URL = f"{BASE_URL}/api/ticket/buyer/list"
ORDER_INFO_URL = f"{BASE_URL}/api/ticket/order/info"
ORDER_LIST_URL = f"{BASE_URL}/api/ticket/order/list"


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    return None


def _code(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    return _integer(body.get("errno", body.get("code")))


def _id(value: Any) -> str | None:
    integer = _integer(value)
    return str(integer) if integer is not None and integer > 0 else None


def _buyer_id(buyer: dict[str, Any]) -> str | None:
    primary = _id(buyer.get("id", buyer.get("buyer_id")))
    if "id" in buyer and "buyer_id" in buyer and _id(buyer["buyer_id"]) != primary:
        return None
    return primary


def _platform_date(value: Any) -> str | None:
    """Only accept a full date or a timestamp; never guess a missing year."""
    if value in (None, "", 0, "0") or isinstance(value, bool):
        return None
    timestamp = _integer(value)
    if timestamp is not None:
        if timestamp > 10**11:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp, BEIJING_TZ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(BEIJING_TZ)
        return parsed.date().isoformat()
    except ValueError:
        match = re.search(
            r"(?<![0-9])(20[0-9]{2})[-/年]([0-9]{1,2})[-/月]([0-9]{1,2})(?:日|\b)", text
        )
        if match:
            try:
                return datetime(*map(int, match.groups())).date().isoformat()
            except ValueError:
                pass
    return None


def _retry_after(response: Any = None, default: float = 3.0) -> float:
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After", headers.get("retry-after"))
    if value is not None:
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(str(value)).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = default
        if math.isfinite(seconds) and seconds > 0:
            return max(default, seconds)
    return default


class _CheckFailure(ValueError):
    def __init__(self, reason: str, *, scope: str = "account", retryable: bool = False):
        super().__init__(reason)
        self.scope = scope
        self.retryable = retryable


class _ProjectReadGuard:
    """Keep legacy detail fallback from bypassing a rate limit/challenge."""

    def __init__(self, request: Any):
        self.request = request
        self.blocked: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.request, name)

    def _call(self, method: str, *args, **kwargs):
        if self.blocked is not None:
            raise self.blocked
        try:
            response = getattr(self.request, method)(*args, **kwargs)
        except BiliRateLimitError as exc:
            self.blocked = exc
            raise
        status = getattr(response, "status_code", 200)
        if status == 412:
            self.blocked = _CheckFailure("平台要求完成验证；请在官方页面处理")
        elif status == 429:
            self.blocked = BiliRateLimitError("只读校验被限流", response=response)
        if self.blocked is not None:
            raise self.blocked
        return response

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call("post", *args, **kwargs)


class BiliGoalAccount:
    def __init__(
        self,
        account: GoalAccount,
        target: GoalTarget,
        *,
        request: Any = None,
    ) -> None:
        self.account = account
        self.target = target
        # Each instance has its own in-memory cookie store and default HTTPX
        # client. No process-global current-account store or fanout transport.
        self._request = (
            request
            if request is not None
            else BiliRequest(
                cookies=copy.deepcopy(account.ticket_config["cookies"]), proxy="none"
            )
        )
        self._window_state = generate_browser_window_state()
        self._ticket: dict[str, Any] = {}
        self._hot_project = False
        self._preflight_retryable = False
        self._preflight_delay = 3.0

    @staticmethod
    def _body(response: Any) -> dict[str, Any]:
        status = getattr(response, "status_code", 200)
        if status == 412:
            raise _CheckFailure("平台要求完成验证；请在官方页面处理")
        if status == 429:
            raise BiliRateLimitError("只读校验被限流", response=response)
        if status != 200:
            raise _CheckFailure(f"只读校验返回 HTTP {status}", retryable=status >= 500)
        body = response.json()
        if not isinstance(body, dict):
            raise _CheckFailure("平台返回的校验结构无法识别")
        return body

    def _login_uid(self) -> str:
        body = self._body(self._request.get(url=NAV_URL))
        data = body.get("data")
        if (
            _code(body) != 0
            or not isinstance(data, dict)
            or data.get("isLogin") is not True
        ):
            raise _CheckFailure("账号未处于已验证登录状态，请重新登录")
        uid = _id(data.get("mid"))
        if uid != self.account.uid:
            raise _CheckFailure("实际登录 UID 与本机账号映射不符")
        return uid

    def _target_check(self) -> None:
        guard = _ProjectReadGuard(self._request)
        try:
            project = fetch_project_payload(
                request=guard, project_id=self.target.project_id
            )
        except Exception:
            if guard.blocked is not None:
                raise guard.blocked
            raise
        if _integer(project.get("id")) != self.target.project_id:
            raise _CheckFailure("平台活动 ID 与固定目标不符", scope="goal")
        screens = project.get("screen_list")
        if not isinstance(screens, list):
            raise _CheckFailure("平台未返回可核对的场次列表", scope="goal")
        matches = [
            s
            for s in screens
            if isinstance(s, dict) and _integer(s.get("id")) == self.target.screen_id
        ]
        if len(matches) != 1:
            raise _CheckFailure("固定场次不存在或不唯一", scope="goal")
        screen = matches[0]
        date = next(
            (
                parsed
                for field in ("start_time", "start_time_str", "dateStr", "name")
                if (parsed := _platform_date(screen.get(field))) is not None
            ),
            None,
        )
        if date != self.target.admission_date:
            raise _CheckFailure(
                "平台场次日期与固定入场日期不符或无法核实", scope="goal"
            )
        tickets = screen.get("ticket_list")
        if not isinstance(tickets, list):
            raise _CheckFailure("平台未返回可核对的票档列表", scope="goal")
        matches = [
            t
            for t in tickets
            if isinstance(t, dict) and _integer(t.get("id")) == self.target.sku_id
        ]
        if len(matches) != 1:
            raise _CheckFailure("固定票档不存在或不唯一", scope="goal")
        ticket = matches[0]
        if str(ticket.get("desc", "")).strip() != self.target.ticket_name:
            raise _CheckFailure("平台票档名称与固定目标不符", scope="goal")
        if _integer(ticket.get("price")) != self.target.price_cents:
            raise _CheckFailure("平台票面单价与固定金额不符", scope="goal")
        for item in (screen, ticket):
            if (
                "project_id" in item
                and _integer(item["project_id"]) != self.target.project_id
            ):
                raise _CheckFailure("票档所属活动不符", scope="goal")
        if (
            "screen_id" in ticket
            and _integer(ticket["screen_id"]) != self.target.screen_id
        ):
            raise _CheckFailure("票档所属场次不符", scope="goal")
        if (
            not project.get("has_eticket")
            and (_integer(screen.get("express_fee")) or 0) > 0
        ):
            raise _CheckFailure("目标包含额外配送费用，需先核对结算金额", scope="goal")
        self._ticket = copy.deepcopy(ticket)
        self._hot_project = bool(project.get("hotProject"))

    def _buyers_check(self) -> None:
        body = self._body(
            self._request.get(
                url=f"{BUYERS_URL}?is_default&projectId={self.target.project_id}"
            )
        )
        if _code(body) != 0 or not isinstance(body.get("data"), dict):
            raise _CheckFailure("无法核对该账号的购票人列表")
        buyers = body["data"].get("list")
        if not isinstance(buyers, list):
            raise _CheckFailure("平台购票人列表结构无法识别")
        expected = self.account.payload_for(tuple(self.account.buyer_ids), self.target)[
            "buyer_info"
        ]
        for person, buyer_id in self.account.buyer_ids.items():
            matches = [
                buyer
                for buyer in buyers
                if isinstance(buyer, dict) and _buyer_id(buyer) == buyer_id
            ]
            if len(matches) != 1:
                raise _CheckFailure(f"购票人 {person} 的账号映射不存在或不唯一")
            live = matches[0]
            stored = next(buyer for buyer in expected if _buyer_id(buyer) == buyer_id)
            if any(
                str(live.get(field, "")).strip() != str(stored.get(field, "")).strip()
                for field in ("name", "personal_id")
            ):
                raise _CheckFailure(
                    f"购票人 {person} 的实名信息已变化，请在本机重新核对"
                )
            if "accountId" in live and _id(live["accountId"]) != self.account.uid:
                raise _CheckFailure(f"购票人 {person} 不属于当前账号映射")
            if "error_code" in live and _integer(live["error_code"]) != 0:
                raise _CheckFailure(f"平台提示购票人 {person} 的信息需要处理")

    def preflight(self) -> PreflightResult:
        """Read-only identity, current target, price, and attendee checks."""
        self._ticket = {}
        self._preflight_retryable = False
        self._preflight_delay = 3.0
        try:
            uid = self._login_uid()
            self._target_check()
            self._buyers_check()
            return PreflightResult(True, account_uid=uid)
        except _CheckFailure as exc:
            self._preflight_retryable = exc.retryable
            return PreflightResult(
                False,
                reason=str(exc),
                blocked_scope=exc.scope,
                retryable=exc.retryable,
                retry_after=self._preflight_delay if exc.retryable else 0.0,
            )
        except GoalConfigError as exc:
            return PreflightResult(False, reason=str(exc), blocked_scope="goal")
        except BiliRateLimitError as exc:
            self._preflight_retryable = True
            self._preflight_delay = _retry_after(exc.response)
            return PreflightResult(
                False,
                reason="只读校验被限流，需退避后重试",
                retryable=True,
                retry_after=self._preflight_delay,
            )
        except Exception as exc:
            self._preflight_retryable = True
            # Exception strings can contain request details; only expose class.
            return PreflightResult(
                False,
                reason=f"只读校验暂未完成（{type(exc).__name__}）",
                retryable=True,
                retry_after=self._preflight_delay,
            )

    def _validate_payload(
        self, payload: dict[str, Any], people: tuple[str, ...]
    ) -> None:
        for field in ("project_id", "screen_id", "sku_id"):
            if _integer(payload.get(field)) != getattr(self.target, field):
                raise _CheckFailure("待提交目标与固定配置不符", scope="goal")
        if _integer(payload.get("count")) != len(people) or _integer(
            payload.get("pay_money")
        ) != self.target.price_cents * len(people):
            raise _CheckFailure("待提交人数或金额与本次人组不符", scope="goal")
        buyers = payload.get("buyer_info")
        if isinstance(buyers, str):
            buyers = json.loads(buyers)
        expected = self.account.payload_for(people, self.target)["buyer_info"]
        if not isinstance(buyers, list) or buyers != expected:
            raise _CheckFailure("待提交购票人集合与本次人组不符", scope="goal")
        if "cookies" in payload:
            raise _CheckFailure("凭据不能进入订单正文", scope="goal")

    @staticmethod
    def _failure(body: dict[str, Any], *, before_create: bool) -> AttemptResult:
        code = _code(body)
        data_order_id = extract_order_id(body)
        if not before_create and data_order_id is not None:
            return AttemptResult(
                "unknown",
                order_id=data_order_id,
                code=code,
                reason="非成功响应携带订单号，需核对官方订单",
            )
        if code == 100009:
            return AttemptResult(
                "shortage",
                code=code,
                reason="平台明确返回所选数量库存不足",
                definitive_no_order=True,
            )
        if code == 100034:
            return AttemptResult(
                "blocked",
                code=code,
                reason="平台要求改变应付金额，固定价格目标已暂停",
                definitive_no_order=True,
                blocked_scope="goal",
            )
        if code in {100003, 100048, 100079}:
            return AttemptResult(
                "review",
                order_id=data_order_id,
                code=code,
                reason="平台提示限购、未完成或重复订单，需核对官方订单",
            )
        if code in {100044, -101, -111}:
            return AttemptResult(
                "blocked",
                code=code,
                reason="需在官方页面完成登录或验证",
                definitive_no_order=True,
            )
        if code in {100016, 100017, 100039}:
            return AttemptResult(
                "blocked",
                code=code,
                reason="平台提示固定目标不可售，需核对售卖状态",
                definitive_no_order=True,
                blocked_scope="goal",
            )
        if code in {3, 219, 221, 100001, 100041, 100051, 900001, 900002}:
            return AttemptResult(
                "retry",
                code=code,
                reason="平台明确未完成本次创建，保持原人数退避重试",
                retry_after=3.0,
                definitive_no_order=True,
            )
        return AttemptResult(
            "blocked" if before_create else "unknown",
            code=code,
            reason="平台响应码无法可靠解释，停止当前创建",
            definitive_no_order=before_create,
        )

    def attempt(
        self, person_keys: tuple[str, ...], before_submit: Callable[[], None]
    ) -> AttemptResult:
        people = tuple(person_keys)
        preflight = self.preflight()
        if not preflight.ready:
            return AttemptResult(
                "retry" if self._preflight_retryable else "blocked",
                reason=preflight.reason,
                retry_after=self._preflight_delay if self._preflight_retryable else 0.0,
                definitive_no_order=True,
                blocked_scope=preflight.blocked_scope,
            )
        flag = _integer(self._ticket.get("sale_flag_number"))
        if flag is None and isinstance(self._ticket.get("sale_flag"), dict):
            flag = _integer(self._ticket["sale_flag"].get("number"))
        if flag in {4, 8}:
            return AttemptResult(
                "sold_out",
                reason="固定票档明确售罄，等待回流",
                retry_after=3.0,
                definitive_no_order=True,
            )
        if flag == 101:
            return AttemptResult(
                "retry",
                reason="固定票档尚未开始售卖",
                retry_after=3.0,
                definitive_no_order=True,
            )
        try:
            info = self.account.payload_for(people, self.target)
            info.pop("cookies", None)
            self._validate_payload(info, people)
            now_ms = time_service.current_time_ms()
            token_state = init_ctoken_state(
                browser_window_state=self._window_state,
                href_length=len(
                    f"https://mall.bilibili.com/neul-next/ticket-renovation/detail.html?id={self.target.project_id}"
                ),
                user_agent_length=len(self._request.get_user_agent()),
                ticket_collection_t=now_ms,
            )
            token_payload = build_token_payload(info)
            token_payload["token"] = token_state.snapshot(
                now_ms=now_ms
            ).generate_prepare_ctoken()
            response = self._request.post(
                url=f"{BASE_URL}/api/ticket/order/prepare?project_id={self.target.project_id}",
                data=token_payload,
                isJson=True,
            )
            prepared = self._body(response)
            if _code(prepared) != 0:
                return self._failure(prepared, before_create=True)
            data = prepared.get("data")
            if not isinstance(data, dict):
                raise _CheckFailure("准备订单响应缺少数据")
            shield = data.get("shield")
            if isinstance(shield, dict) and shield.get("verifyMethod"):
                raise _CheckFailure("准备订单要求人工验证，请到官方页面处理")
            token = data.get("token")
            if not isinstance(token, str) or not token.strip():
                return AttemptResult(
                    "retry",
                    reason="准备订单未返回有效 token",
                    retry_after=3.0,
                    definitive_no_order=True,
                )
            info["_prepare_buyer_info"] = copy.deepcopy(info["buyer_info"])
            info["buyer_info"] = json.dumps(info["buyer_info"], ensure_ascii=False)
            info["deliver_info"] = json.dumps(info["deliver_info"], ensure_ascii=False)
            url, payload = prepare_create_request(
                info, token.strip(), self._hot_project, prepared, token_state
            )
            self._validate_payload(payload, people)
        except _CheckFailure as exc:
            return AttemptResult(
                "retry" if exc.retryable else "blocked",
                reason=str(exc),
                retry_after=3.0 if exc.retryable else 0.0,
                definitive_no_order=True,
                blocked_scope=exc.scope,
            )
        except BiliRateLimitError as exc:
            return AttemptResult(
                "retry",
                reason="准备阶段被限流",
                retry_after=_retry_after(exc.response),
                definitive_no_order=True,
            )
        except Exception as exc:
            return AttemptResult(
                "retry",
                reason=f"创建前的准备尚未完成（{type(exc).__name__}）",
                retry_after=3.0,
                definitive_no_order=True,
            )

        # The caller persists the exact account/person group and checks its
        # cancellation/deadline immediately here. Callback errors propagate and
        # must not be converted into another network attempt.
        before_submit()
        try:
            response = self._request.post(url=url, data=payload, isJson=True)
            if getattr(response, "status_code", 200) != 200:
                return AttemptResult(
                    "unknown", reason="创建响应 HTTP 状态不确定，需核对官方订单"
                )
            body = response.json()
            code = _code(body)
            if code is None:
                return AttemptResult("unknown", reason="创建响应结构或结果码不确定")
            if is_create_success(body, code):
                return AttemptResult(
                    "created",
                    order_id=extract_order_id(body),
                    code=0,
                    reason="订单已创建，等待官方付款核对",
                )
            if code == 0:
                return AttemptResult(
                    "unknown", code=0, reason="成功码缺少可信的有效订单号"
                )
            return self._failure(body, before_create=False)
        except BiliConnectionError:
            return AttemptResult(
                "retry",
                reason="默认连接在发送前失败，可重新准备后再试",
                retry_after=3.0,
                definitive_no_order=True,
            )
        except BiliRateLimitError as exc:
            return AttemptResult(
                "retry",
                reason="创建被 HTTP 429 拒绝，按服务器要求等待",
                retry_after=_retry_after(exc.response),
                definitive_no_order=True,
            )
        except BiliOrderResultUnknownError:
            return AttemptResult("unknown", reason="创建请求可能已送达，需核对官方订单")
        except Exception as exc:
            return AttemptResult(
                "unknown", reason=f"创建结果尚未确认（{type(exc).__name__}）"
            )

    def _known_order_observation(
        self, order: Any, attempt: dict[str, Any]
    ) -> OrderObservation | None:
        if not isinstance(order, dict):
            return None
        people = tuple(attempt.get("person_keys", ()))
        order_id = _id(attempt.get("order_id"))
        if (
            not order_id
            or _id(order.get("order_id")) != order_id
            or _integer(order.get("item_id")) != self.target.project_id
            or _integer(order.get("count")) != len(people)
            or _integer(order.get("pay_money")) != self.target.price_cents * len(people)
        ):
            return None
        if "uid" in order and _id(order["uid"]) != self.account.uid:
            return None
        item = order.get("item_info")
        if (
            not isinstance(item, dict)
            or _integer(item.get("screen_id")) != self.target.screen_id
        ):
            return None
        if (
            item.get("screen_start_time") not in (None, "")
            and _platform_date(item["screen_start_time"]) != self.target.admission_date
        ):
            return None
        # People and SKU are linked by the successfully created, durable
        # pending attempt, not inferred from an unrelated order-list candidate.
        if "sku_id" in order and _integer(order["sku_id"]) != self.target.sku_id:
            return None
        if "total_money" in order and _integer(
            order["total_money"]
        ) != self.target.price_cents * len(people):
            return None
        status = _integer(order.get("status"))
        if status == 1 and _integer(order.get("sub_status")) == 1:
            outcome = "pending"
        elif (
            status == 2
            and _integer(order.get("refund_status")) == 0
            and (_integer(order.get("pay_time")) or 0) > 0
        ):
            outcome = "paid"
        elif (
            status == 4
            and _integer(order.get("refund_status")) == 0
            and _integer(order.get("pay_time")) == 0
        ):
            outcome = "closed"
        else:
            return None
        return OrderObservation(
            outcome,
            order_id=order_id,
            person_keys=people,
            account_key=self.account.key,
            target_key=self.target.key,
            verified=True,
            reason="精确已知订单与原创建绑定及平台状态一致",
        )

    def reconcile(self, attempt: dict[str, Any]) -> OrderObservation:
        unknown = OrderObservation(
            "unknown",
            account_key=self.account.key,
            target_key=self.target.key,
            reason="订单证据不足，保留现有占用；请核对官方订单",
        )
        people = attempt.get("person_keys")
        if (
            attempt.get("account_key") != self.account.key
            or attempt.get("state") != "pending"
            or not isinstance(people, (list, tuple))
            or not people
            or not all(isinstance(person, str) for person in people)
            or len(set(people)) != len(people)
            or not set(people).issubset(self.account.buyer_ids)
            or _id(attempt.get("order_id")) is None
        ):
            # Lists cannot prove who belongs to an unknown submission. An empty
            # list likewise cannot prove that no order was created.
            return unknown
        try:
            self._login_uid()
            order_id = _id(attempt["order_id"])
            response = self._body(
                self._request.get(url=f"{ORDER_INFO_URL}?order_id={order_id}")
            )
            if _code(response) == 0:
                observation = self._known_order_observation(
                    response.get("data"), attempt
                )
                if observation is not None:
                    return observation
            # A bounded alternate view of the same known ID; never choose the
            # first order, match on count alone, or infer absence from this page.
            response = self._body(
                self._request.get(url=f"{ORDER_LIST_URL}?page=0&page_size=20")
            )
            data = response.get("data")
            if (
                _code(response) == 0
                and isinstance(data, dict)
                and isinstance(data.get("list"), list)
            ):
                candidates = [
                    o
                    for o in data["list"]
                    if isinstance(o, dict) and _id(o.get("order_id")) == order_id
                ]
                if len(candidates) == 1:
                    observation = self._known_order_observation(candidates[0], attempt)
                    if observation is not None:
                        return observation
        except Exception:
            pass
        return unknown

    def close(self) -> None:
        invalidate = getattr(self._request, "_invalidate_h2_client", None)
        if invalidate is not None:
            invalidate()
        session = getattr(self._request, "session", None)
        if session is not None:
            session.close()
        manager = getattr(self._request, "cookieManager", None)
        if manager is not None:
            manager.db.close()
