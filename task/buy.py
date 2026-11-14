import json
import os
import random
import subprocess
import sys
import time
import uuid
import copy
import webbrowser
from collections.abc import Generator
from dataclasses import dataclass
from json import JSONDecodeError
import shutil
import qrcode
from loguru import logger

from requests import HTTPError, RequestException
from cptoken import (
    generate_browser_window_state,
    init_ctoken_state,
)

from app_cmd.config.BuyConfig import BuyConfig
from interface.project import fetch_project_payload
from util.notifer.Notifier import NotifierManager
from util.proxy.ProxyBackoff import ProxyBackoff
from util.proxy.ProxyApiProvider import fetch_proxy_api
from util.proxy.ProxyManager import ProxyManager
from util.notifer.RandomMessages import get_random_fail_message
from util import time_service
from util.ErrorCodes import ErrorCodes
from util.h2client.constants import H2CLIENT_CONNECTIONS_PER_SOURCE_IP
from task.buy_helpers import (
    BASE_URL as base_url,
    build_order_detail_payment_result,
    build_payment_result_with_fallback,
    build_token_payload as _build_token_payload,
    create_order_terminal_rule as _create_order_terminal_rule,
    extract_order_id as _extract_order_id,
    format_retry_reason as _format_retry_reason,
    format_status_result as _format_status_result,
    handle_proxy_failure as _handle_proxy_failure,
    is_create_success as _is_create_success,
    prepare_create_request as _prepare_create_request,
    summarize_non_json_response as _summarize_non_json_response,
    wait_until_start as _wait_until_start,
)
from task.page_gate import (
    check_ticket_page_availability,
    mobile_ticket_page_url,
    normalize_mobile_ticket_page_url,
)
from task.buy_types import (
    BuyStreamEvent,
    BuyStreamState,
    BuyStreamUpdate,
    BuyStreamWorker,
    CreateOrderTerminalRule,
    RetryOutcome,
)
from util.request.BiliRequest import BiliRequest
from util.request.exceptions import (
    BiliConnectionError,
    BiliOrderResultUnknownError,
    BiliRateLimitError,
)


LOCAL_FANOUT_PROXY_STRATEGY = "local_fanout"


@dataclass(slots=True)
class Buy:
    config: BuyConfig

    def _resolved_tickets_info(self) -> str:
        if self.config.config_file:
            config_path = os.path.expanduser(self.config.config_file)
            with open(config_path, "r", encoding="utf-8") as config_file:
                return config_file.read()
        return self.config.tickets_info

    def resolved_config(self) -> BuyConfig:
        return self.config.with_overrides(
            tickets_info=self._resolved_tickets_info(),
        )

    def stream(self):
        yield from buy_stream(self.resolved_config())

    def start_worker(self) -> BuyStreamWorker:
        return BuyStreamWorker.start_buy_stream_worker(self.stream)

    def to_cli_args(self) -> list[str]:
        if self.config.config_file:
            return [
                "buy",
                "--config-file",
                self.config.config_file,
                *self.config.to_cli_args(),
            ]
        return [
            "buy",
            "--tickets-info",
            self.config.tickets_info,
            *self.config.to_cli_args(),
        ]

    def run(self, on_message=None) -> None:
        worker = self.start_worker()
        for event in worker.iter_events():
            if event.message is not None and on_message is not None:
                on_message(event.message)

    def buy(self) -> None:
        self.run(logger.info)

    def start_new_terminal(
        self,
        *,
        log_file_path: str | None = None,
        log_level: str | None = None,
        log_retention_days: int | None = None,
    ) -> subprocess.Popen:
        command = None

        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            main_py = os.path.join(script_dir, "main.py")

            if os.path.exists(main_py):
                command = [sys.executable, main_py]
            else:
                btb_path = shutil.which("btb")
                if not btb_path:
                    raise RuntimeError("Cannot find main.py or btb command")
                command = [btb_path]
        command.extend(self.to_cli_args())
        env = os.environ.copy()
        env["BTB_PARENT_PID"] = str(os.getpid())
        effective_log_level = log_level or self.config.log_level
        if effective_log_level:
            normalized_log_level = str(effective_log_level).lower()
            if normalized_log_level == "simple":
                env["BTB_LOG_LEVEL"] = "INFO"
                env["BTB_CONSOLE_LOG_LEVEL"] = "INFO"
            elif normalized_log_level == "debug":
                env["BTB_LOG_LEVEL"] = "DEBUG"
                env["BTB_CONSOLE_LOG_LEVEL"] = "DEBUG"
            else:
                env["BTB_LOG_LEVEL"] = "DEBUG"
                env["BTB_CONSOLE_LOG_LEVEL"] = "INFO"
        env["BTB_LOG_RETENTION_DAYS"] = str(
            log_retention_days
            if log_retention_days is not None
            else self.config.log_retention_days
        )
        if log_file_path:
            env["BTB_APP_LOG_NAME"] = os.path.basename(log_file_path)
        else:
            env.setdefault("BTB_APP_LOG_NAME", f"{uuid.uuid4()}.log")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
            )
            env["BTB_HOLD_TERMINAL"] = "1"
        else:
            env["BTB_CHILD_PROCESS"] = "1"
            kwargs["start_new_session"] = True

        if os.name == "nt":
            return subprocess.Popen(command, env=env, **kwargs)

        with open(os.devnull, "r") as devnull_in, open(os.devnull, "a") as devnull_out:
            return subprocess.Popen(
                command,
                env=env,
                stdin=devnull_in,
                stdout=devnull_out,
                stderr=devnull_out,
                **kwargs,
            )

    def buy_new_terminal(
        self,
        *,
        log_file_path: str | None = None,
        log_level: str | None = None,
        log_retention_days: int | None = None,
    ) -> subprocess.Popen:
        return self.start_new_terminal(
            log_file_path=log_file_path,
            log_level=log_level,
            log_retention_days=log_retention_days,
        )


def _extract_prepare_token(result: dict | None) -> str | None:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if token is None:
        return None
    token = str(token).strip()
    return token or None


def _format_reprepare_reason(reason: str) -> str:
    return f"重新准备订单，原因：{reason}"


def _resolve_notify_retry_params(notifier_config) -> tuple[int, float]:
    """从 NotifierConfig 解析 (retries, backoff)，非法/缺失回退到 (3, 0.5)。"""
    try:
        retries = int(getattr(notifier_config, "notify_retries", 3))
    except (TypeError, ValueError):
        retries = 3
    retries = max(1, retries)

    try:
        backoff = float(getattr(notifier_config, "notify_backoff", 0.5))
    except (TypeError, ValueError):
        backoff = 0.5
    if backoff < 0:
        backoff = 0.5

    return retries, backoff


def _precheck_notifiers(notifier_config) -> None:
    """进入等待/倒计时前，对已配置通知渠道做一次轻量探测并打日志。

    仅对已配置的渠道创建实例并尝试发送一条预检消息（同步、带 timeout+重试），
    结果（成功/失败/耗时）打进日志。任何失败只告警，不抛出、不阻断抢票流程。
    """
    try:
        manager = NotifierManager.create_from_config(
            config=notifier_config,
            title="抢票预检",
            content="biliTickerBuy 通知链路预检，收到说明推送渠道可用。",
            include_audio=False,
        )
    except Exception as exc:
        logger.warning(f"通知渠道预检初始化失败（不影响抢票）: {exc}")
        return

    names = manager.list_notifiers()
    if not names:
        logger.info("通知渠道预检：未配置任何推送渠道，跳过")
        return

    # 预检仍固定 retries=1 轻探（不阻断抢票），但退避沿用配置值。
    _, backoff = _resolve_notify_retry_params(notifier_config)

    logger.info(f"通知渠道预检开始，已配置渠道: {names}")
    for name, notifer in manager.notifier_dict.items():
        start = time.time()
        try:
            ok = notifer.send_once_sync(
                "抢票预检",
                "biliTickerBuy 通知链路预检，收到说明推送渠道可用。",
                retries=1,
                backoff=backoff,
            )
            elapsed = time.time() - start
            if ok:
                logger.info(f"通知渠道预检成功: {name}（耗时 {elapsed:.2f}s）")
            else:
                logger.warning(
                    f"通知渠道预检失败: {name}（耗时 {elapsed:.2f}s，仅告警不阻断抢票）"
                )
        except Exception as exc:
            elapsed = time.time() - start
            logger.warning(
                f"通知渠道预检异常: {name} - {exc}（耗时 {elapsed:.2f}s，仅告警不阻断抢票）"
            )


def buy_stream(config: BuyConfig):
    state = BuyStreamState()
    known_order_id: int | str | None = None

    def emit(
        kind: str,
        message: str | None,
        update: BuyStreamUpdate | None = None,
    ):
        if update is not None:
            update.apply_to(state)
        if message is not None:
            state.last_message = message

        return BuyStreamEvent(
            kind=kind,
            message=message,
            state=copy.deepcopy(state),
            data=update.to_dict() if update is not None else {},
        )

    def stop_for_unknown_order(reason: str, *, attempt: int | None = None):
        return emit(
            "order_result_unknown",
            f"创建订单结果尚未确认，已停止下单；请先核对官方订单中心。原因：{reason}",
            BuyStreamUpdate(
                status="order_result_unknown",
                stage="订单结果待核实",
                attempt_current=attempt,
            ),
        )

    def emit_payment_details(
        payment_result: dict,
        *,
        status: str,
    ):
        update = BuyStreamUpdate(
            order_id=payment_result.get("order_id"),
            order_detail_url=payment_result.get("order_detail_url"),
            payment_code_url=payment_result.get("payment_code_url"),
            payment_qr_url=payment_result.get("payment_qr_url"),
            status=status,
        )

        markers = [
            ("ORDER_ID", payment_result.get("order_id")),
            ("ORDER_DETAIL_URL", payment_result.get("order_detail_url")),
            ("PAYMENT_CODE_URL", payment_result.get("payment_code_url")),
            ("PAYMENT_QR_URL", payment_result.get("payment_qr_url")),
        ]
        for marker, value in markers:
            if value in (None, ""):
                continue
            yield emit("payment_qr", f"{marker}={value}", update)

    def emit_order_assistance(order_id: int | str, *, status: str, notify: bool):
        """Payment UI and notifications never decide whether to create again."""
        payment_result = build_order_detail_payment_result(order_id)
        try:
            payment_result, payment_error = build_payment_result_with_fallback(
                _request, order_id
            )
            if payment_error is not None:
                yield emit(
                    "status",
                    "未获取支付二维码，请在官方订单详情核对付款状态；保持停止下单",
                    BuyStreamUpdate(order_id=order_id, status=status),
                )
        except Exception:
            yield emit(
                "status",
                "支付信息处理失败，请在官方订单详情核对付款状态；保持停止下单",
                BuyStreamUpdate(order_id=order_id, status=status),
            )
        yield from emit_payment_details(payment_result, status=status)

        if config.auto_open_payment_url:
            try:
                webbrowser.open(payment_result["order_detail_url"])
                yield emit(
                    "status",
                    "已自动打开订单详情链接",
                    BuyStreamUpdate(order_id=order_id, status=status),
                )
            except Exception:
                yield emit(
                    "status",
                    "自动打开订单链接失败，请手动查看官方订单中心；保持停止下单",
                    BuyStreamUpdate(order_id=order_id, status=status),
                )

        if config.show_qrcode and payment_result.get("payment_code_url"):
            try:
                qr_gen = qrcode.QRCode()
                qr_gen.add_data(payment_result["payment_code_url"])
                qr_gen.make(fit=True)
                qr_gen.make_image().show()
            except Exception:
                yield emit(
                    "status",
                    "显示支付二维码失败，请手动查看官方订单中心；保持停止下单",
                    BuyStreamUpdate(order_id=order_id, status=status),
                )

        if not notify:
            return
        try:
            notifier_manager = NotifierManager.create_from_config(
                config=config.notifier_config,
                title="订单已创建，待核对付款",
                content=f"bilibili会员购，请前往官方订单中心核对并付款: {detail}",
            )
            notifier_manager.start_all()
            # Keep synchronous delivery before stream exit, but notification
            # failures must never re-enter the order creation loop.
            notifier_manager.join_all(timeout=15)
            notify_retries, notify_backoff = _resolve_notify_retry_params(
                config.notifier_config
            )
            results = notifier_manager.send_all_sync(
                title="订单已创建，待核对付款",
                content=f"bilibili会员购，请前往官方订单中心核对并付款: {detail}",
                retries=notify_retries,
                backoff=notify_backoff,
            )
            if results and not any(results.values()):
                yield emit(
                    "status",
                    "通知渠道发送失败，请手动查看官方订单中心；保持停止下单",
                    BuyStreamUpdate(order_id=order_id, status=status),
                )
        except Exception:
            yield emit(
                "status",
                "订单通知处理失败，请手动查看官方订单中心；保持停止下单",
                BuyStreamUpdate(order_id=order_id, status=status),
            )

    def handle_proxy_failure(
        reason: str,
        *,
        attempt: int | None = None,
    ):
        def replenish_proxy_pool():
            if not str(config.proxy_api_url or "").strip():
                return False, None
            try:
                request_count = int(config.proxy_api_request_count or 0)
            except (TypeError, ValueError):
                request_count = 0
            if request_count <= 0:
                request_count = max(
                    1,
                    len(
                        [
                            proxy
                            for proxy in _request.proxy_manager.proxy_list
                            if proxy.lower() != "none"
                        ]
                    ),
                )
            try:
                result = fetch_proxy_api(
                    config.proxy_api_url,
                    count=request_count,
                    protocol=config.proxy_api_protocol,
                )
                _request.replace_proxy_pool(",".join(result.proxies))
                return (
                    True,
                    f"已从代理 API 自动获取 {len(result.proxies)} 个新代理",
                )
            except Exception as exc:
                logger.warning(f"代理 API 自动获取失败: {exc}")
                return False, f"代理 API 自动获取失败: {exc}"

        immediate_message, delay_seconds = _handle_proxy_failure(
            _request,
            reason,
            proxy_backoff,
            config.notifier_config,
            replenish_proxy_pool=replenish_proxy_pool,
        )
        attempt_total = (
            effective_retry_limit if attempt is not None else state.attempt_total
        )
        if immediate_message:
            for message in immediate_message.splitlines():
                yield emit(
                    "proxy",
                    message,
                    BuyStreamUpdate(
                        current_proxy=_request.current_proxy_status(),
                        proxy_pool=_request.proxy_pool_status(),
                        cooldown_remaining=None,
                        status="running",
                        attempt_current=attempt,
                        attempt_total=attempt_total,
                    ),
                )
        if delay_seconds is None:
            return
        for remaining in range(delay_seconds, 0, -1):
            yield emit(
                "state",
                None,
                BuyStreamUpdate(
                    current_proxy=_request.current_proxy_status(),
                    proxy_pool=_request.proxy_pool_status(),
                    cooldown_remaining=remaining,
                    status="cooldown",
                    attempt_current=attempt,
                    attempt_total=attempt_total,
                ),
            )
            time.sleep(1)
        if _request.ensure_active_proxy():
            proxy_backoff.reset()
            yield emit(
                "state",
                None,
                BuyStreamUpdate(
                    current_proxy=_request.current_proxy_status(),
                    proxy_pool=_request.proxy_pool_status(),
                    cooldown_remaining=None,
                    status="running",
                    attempt_current=attempt,
                    attempt_total=attempt_total,
                ),
            )

    def handle_non_json_response(
        prefix: str,
        response,
        *,
        attempt: int | None = None,
    ) -> Generator[object, None, bool]:
        diagnostic = _request.describe_non_json_response(response)
        summary = _summarize_non_json_response(prefix, diagnostic)
        # 出现 412 风控时，走代理失败处理，切换代理或进入冷却等待。
        if "412 风控" in summary:
            yield emit(
                "proxy",
                f"{prefix}触发 412 风控",
                BuyStreamUpdate(
                    current_proxy=_request.current_proxy_status(),
                    proxy_pool=_request.proxy_pool_status(),
                    attempt_current=attempt,
                    attempt_total=(
                        effective_retry_limit
                        if attempt is not None
                        else state.attempt_total
                    ),
                ),
            )
            yield from handle_proxy_failure(f"{prefix} 412 风控", attempt=attempt)
            return True
        yield emit(
            "attempt" if attempt is not None else "error",
            summary,
            BuyStreamUpdate(
                current_proxy=_request.current_proxy_status(),
                proxy_pool=_request.proxy_pool_status(),
                attempt_current=attempt,
                attempt_total=(
                    effective_retry_limit
                    if attempt is not None
                    else state.attempt_total
                ),
            ),
        )
        return False

    isRunning = True
    tickets_info = json.loads(config.tickets_info)
    detail = tickets_info["detail"]
    cookies = tickets_info["cookies"]
    tickets_info.pop("cookies", None)
    tickets_info["_prepare_buyer_info"] = copy.deepcopy(tickets_info["buyer_info"])
    tickets_info["buyer_info"] = json.dumps(tickets_info["buyer_info"])
    tickets_info["deliver_info"] = json.dumps(tickets_info["deliver_info"])
    masked_proxies = ProxyManager.mask_proxy_string(config.https_proxys)
    logger.info(f"目前已配置代理：{masked_proxies or '直连'}")
    h2_client_type = None
    h2_client_options = None
    if config.create_request_proxy_strategy == LOCAL_FANOUT_PROXY_STRATEGY:
        from util.h2client.ja_h2_client import ProxyPoolCreateV2FanoutJA3H2Client

        proxy_pool = ProxyManager.parse_proxy_list(config.https_proxys)
        if not proxy_pool:
            proxy_pool = ["none"]
        try:
            h2_connections_per_source_ip = max(
                1,
                int(config.h2_connections_per_source_ip),
            )
        except (TypeError, ValueError):
            h2_connections_per_source_ip = H2CLIENT_CONNECTIONS_PER_SOURCE_IP
        h2_client_type = ProxyPoolCreateV2FanoutJA3H2Client
        h2_client_options = {
            "proxy_pool": proxy_pool,
            "connections_per_source_ip": h2_connections_per_source_ip,
        }
    _request = BiliRequest(
        cookies=cookies,
        proxy=config.https_proxys,
        proxy_failure_threshold=config.proxy_max_consecutive_failures,
        proxy_cooldown_seconds=config.proxy_cooldown_seconds,
        h2_client_type=h2_client_type,
        h2_client_options=h2_client_options,
    )
    proxy_backoff = ProxyBackoff(max_seconds=config.proxy_backoff_max_seconds)
    is_hot_project = bool(tickets_info.get("is_hot_project", False))
    # use_local_token = bool(config.use_local_token)
    browser_window_state = generate_browser_window_state()
    token_payload = _build_token_payload(tickets_info)
    request_interval = max(1, int(config.interval or 1000))
    effective_retry_limit = max(1, int(config.create_retry_limit))
    effective_batch_size = max(1, int(config.create_request_batch_size))
    rate_limit_delay_ms = max(0, int(config.rate_limit_delay_ms))
    wait_for_buy_button = bool(config.wait_for_buy_button)
    page_status_check = None
    page_check_before_seconds = max(0, int(config.buy_page_check_before_seconds))
    page_timeout_seconds = max(1, int(config.buy_page_timeout_seconds))
    if wait_for_buy_button:
        configured_page_url = str(config.buy_page_url or "").strip()
        try:
            buy_page_url = (
                normalize_mobile_ticket_page_url(configured_page_url)
                if configured_page_url
                else mobile_ticket_page_url(tickets_info["project_id"])
            )
        except ValueError as exc:
            buy_page_url = configured_page_url
            logger.warning("购票页链接无效，将在校验阶段等待至超时：{}", exc)

        def page_status_check():
            return check_ticket_page_availability(_request, buy_page_url)

        logger.info(
            "已开启立即购票校验：移动端链接={}，提前{}秒开始，超时{}秒",
            buy_page_url,
            page_check_before_seconds,
            page_timeout_seconds,
        )

    def emit_reprepare(reason: str):
        message = _format_reprepare_reason(reason)
        logger.info(message)
        return emit("status", message)

    def refresh_hot_and_warm():
        nonlocal is_hot_project
        logger.info("预热/复检：开始拉取项目详情并预热连接")
        payload = fetch_project_payload(
            request=_request, project_id=int(tickets_info["project_id"])
        )
        if bool(payload["hotProject"]) and not is_hot_project:
            is_hot_project = True
            tickets_info["is_hot_project"] = True
            logger.info("预热/复检：检测到 hotProject=True，已升级为 hot 抢票策略")
        else:
            logger.info("预热/复检完成。")
        _request.prewarm_h2_connection(f"{base_url}/")

    # 循环内主动复检项目详情：按随机 create 次数触发纯拉取，与 100001 路径共享计数。
    # fetch 落在两次 create 的 sleep 窗口，不与 create 并发。
    refresh_min_count = max(0, int(config.refresh_interval_min_count))
    refresh_max_count = max(0, int(config.refresh_interval_max_count))
    refresh_count_enabled = (
        refresh_max_count > 0 and refresh_min_count <= refresh_max_count
    )
    refresh_counter = 0
    refresh_target = (
        random.randint(refresh_min_count, refresh_max_count)
        if refresh_count_enabled
        else None
    )

    def _reset_refresh_counter():
        """重置计数器并重抽下一次目标次数。定时与 100001 两路径共用。"""
        nonlocal refresh_counter, refresh_target
        refresh_counter = 0
        if refresh_count_enabled:
            refresh_target = random.randint(refresh_min_count, refresh_max_count)

    def _on_100001():
        refresh_hot_and_warm()
        _reset_refresh_counter()

    _request.set_100001_handler(_on_100001)

    refresh_hot_and_warm()

    yield emit(
        "proxy",
        f"当前代理: {_request.current_proxy_status()}",
        BuyStreamUpdate(
            current_proxy=_request.current_proxy_status(),
            proxy_pool=_request.proxy_pool_status(),
        ),
    )

    # 进入等待/倒计时前，对已配置的通知渠道做一次轻量预检并打日志。
    # 失败只告警不阻断抢票（避免通知配置问题耽误抢票）。
    _precheck_notifiers(config.notifier_config)

    for wait_state in _wait_until_start(
        config.time_start,
        warmup=refresh_hot_and_warm,
        page_status_check=page_status_check,
        page_check_before_seconds=page_check_before_seconds,
        page_timeout_seconds=page_timeout_seconds,
    ):
        wait_message = wait_state.get("message")
        countdown_value = wait_state.get("countdown")
        countdown_seconds = wait_state.get("countdown_seconds")
        if wait_state.get("page_gate_timeout"):
            yield emit(
                "error",
                wait_message,
                BuyStreamUpdate(status="failed", stage="购票页校验超时"),
            )
            return
        stage_value = None
        if isinstance(wait_message, str) and wait_message.startswith("0)"):
            stage_value = "等待开票"
        yield emit(
            "status",
            wait_message,
            BuyStreamUpdate(
                stage=stage_value or state.stage,
                countdown=countdown_value or state.countdown,
                countdown_seconds=(
                    countdown_seconds
                    if countdown_seconds is not None
                    else state.countdown_seconds
                ),
            ),
        )
    while isRunning:
        try:
            request_result: dict | None = None
            ticket_collection_t = time_service.current_time_ms()
            ticket_state = init_ctoken_state(
                browser_window_state=browser_window_state,
                href_length=len(
                    f"https://mall.bilibili.com/neul-next/ticket-renovation/detail.html?id={tickets_info['project_id']}"
                ),
                user_agent_length=len(_request.get_user_agent()),
                ticket_collection_t=ticket_collection_t,
            )
            # if is_hot_project:
            # hot
            yield emit("stage", "开始准备订单", BuyStreamUpdate(stage="订单准备"))
            prepare_ctoken_state = ticket_state.snapshot(now_ms=ticket_collection_t)
            token_payload["token"] = prepare_ctoken_state.generate_prepare_ctoken()
            request_result_normal = _request.post(
                url=f"{base_url}/api/ticket/order/prepare?project_id={tickets_info['project_id']}",
                data=token_payload,
                isJson=True,
            )

            request_result = request_result_normal.json()
            proxy_backoff.reset()
            yield emit(
                "status",
                _format_status_result(
                    "订单准备结果",
                    request_result,  # type: ignore
                ),
            )
            order_token = _extract_prepare_token(request_result)
            if not order_token:
                yield emit_reprepare("订单准备未返回有效 token")
                continue
            # else:
            #     # normal
            #     yield emit("status", None, BuyStreamUpdate(stage="订单准备"))
            #     if use_local_token:
            #         order_token = _build_order_token(tickets_info)
            #         yield emit(
            #             "status",
            #             "已启用本地 token 模式，跳过 prepare",
            #         )
            #     else:
            #         request_result_normal = _request.post(
            #             url=f"{base_url}/api/ticket/order/prepare?project_id={tickets_info['project_id']}",
            #             data=token_payload,
            #             isJson=True,
            #         )
            #         request_result = request_result_normal.json()
            #         proxy_backoff.reset()
            #         yield emit(
            #             "status",
            #             _format_status_result("订单准备结果", request_result),
            #         )
            #         order_token = _extract_prepare_token(request_result)
            #         if not order_token:
            #             yield emit_reprepare("订单准备未返回有效 token")
            #             time.sleep(request_interval / 1000)
            #             continue

            yield emit(
                "stage",
                "开始创建订单",
                BuyStreamUpdate(
                    stage="创建订单",
                    attempt_current=None,
                    attempt_total=effective_retry_limit,
                ),
            )
            result = None
            retry_outcome = RetryOutcome()
            token_expired = False
            terminal_result: tuple[int, dict, CreateOrderTerminalRule] | None = None
            attempt = 1
            while attempt <= effective_retry_limit:
                batch_end = min(
                    attempt + effective_batch_size - 1,
                    effective_retry_limit,
                )
                url, payload = _prepare_create_request(
                    tickets_info,
                    order_token,
                    is_hot_project=is_hot_project,
                    request_result=request_result,
                    ticket_state=ticket_state,
                )
                while attempt <= batch_end:
                    if not isRunning:
                        yield emit("status", "抢票结束")
                        break
                    should_sleep_before_next_attempt = False
                    try:
                        create_response = _request.post(
                            url=url,
                            data=payload,
                            isJson=True,
                        )
                        ret = create_response.json()
                        if not isinstance(ret, dict):
                            yield stop_for_unknown_order(
                                "创建响应不是对象", attempt=attempt
                            )
                            return
                        raw_err = ret.get("errno", ret.get("code"))
                        if isinstance(raw_err, bool) or not isinstance(
                            raw_err, (int, str)
                        ):
                            yield stop_for_unknown_order(
                                "创建响应缺少有效结果码", attempt=attempt
                            )
                            return
                        try:
                            err = int(raw_err)
                        except ValueError:
                            yield stop_for_unknown_order(
                                "创建响应结果码无法解析", attempt=attempt
                            )
                            return
                        proxy_backoff.reset()
                        retry_outcome.set_response(err, ret)
                        if _is_create_success(ret, err):
                            known_order_id = _extract_order_id(ret)
                            payment_result = build_order_detail_payment_result(
                                known_order_id
                            )
                            result = (ret, err)
                            yield emit(
                                "order_created",
                                "订单已创建，请核对官方订单并付款；本任务已停止下单",
                                BuyStreamUpdate(
                                    order_id=known_order_id,
                                    order_detail_url=payment_result["order_detail_url"],
                                    payment_qr_url=payment_result["payment_qr_url"],
                                    status="order_created",
                                    stage="订单已创建，待核对付款",
                                    attempt_current=attempt,
                                    attempt_total=effective_retry_limit,
                                ),
                            )
                            break
                        if err == 0:
                            yield stop_for_unknown_order(
                                "成功码未伴随可信的有效订单号", attempt=attempt
                            )
                            return
                        _request.handle_100001(err)
                        yield emit(
                            "attempt",
                            ErrorCodes.format_attempt_result(err, ret),
                            BuyStreamUpdate(
                                attempt_current=attempt,
                                attempt_total=effective_retry_limit,
                            ),
                        )
                        terminal_rule = _create_order_terminal_rule(err)
                        if terminal_rule is not None:
                            terminal_result = (err, ret, terminal_rule)
                            yield emit(
                                "status",
                                ErrorCodes.append_response_message(
                                    err,
                                    terminal_rule.message,
                                    ret,
                                ),
                                BuyStreamUpdate(
                                    attempt_current=attempt,
                                    attempt_total=effective_retry_limit,
                                    status=terminal_rule.status,
                                    stage="待核对官方订单",
                                ),
                            )
                            break
                        if err == 100051:
                            yield emit_reprepare("token过期")
                            token_expired = True
                            break
                        if err == 100034:
                            yield emit(
                                "status",
                                f"更新票价为：{ret['data']['pay_money'] / 100}",
                                BuyStreamUpdate(
                                    attempt_current=attempt,
                                    attempt_total=effective_retry_limit,
                                ),
                            )
                            tickets_info["pay_money"] = ret["data"]["pay_money"]
                        should_sleep_before_next_attempt = True
                    except BiliOrderResultUnknownError:
                        yield stop_for_unknown_order(
                            "提交后的网络响应不确定", attempt=attempt
                        )
                        return
                    except JSONDecodeError:
                        yield stop_for_unknown_order(
                            "创建响应不是有效 JSON", attempt=attempt
                        )
                        return
                    except BiliConnectionError as e:
                        # For create POSTs the request layer only uses this
                        # exception when the default transport has not sent
                        # the request. Possibly sent failures use the distinct
                        # BiliOrderResultUnknownError and must never reach here.
                        retry_outcome.set_exception(e)
                        yield emit(
                            "attempt",
                            "连接失败，创建请求尚未发送；按请求间隔继续尝试",
                            BuyStreamUpdate(
                                attempt_current=attempt,
                                attempt_total=effective_retry_limit,
                            ),
                        )
                        should_sleep_before_next_attempt = True
                    except BiliRateLimitError as e:
                        retry_outcome.set_exception(e)
                        yield emit(
                            "attempt",
                            (
                                f"{e}，延迟 {rate_limit_delay_ms}ms 后继续"
                                if rate_limit_delay_ms > 0
                                else str(e)
                            ),
                            BuyStreamUpdate(
                                attempt_current=attempt,
                                attempt_total=effective_retry_limit,
                            ),
                        )
                        if rate_limit_delay_ms > 0:
                            time.sleep(rate_limit_delay_ms / 1000)
                        continue  # 不需要sleep
                    except RequestException as e:
                        yield stop_for_unknown_order(
                            f"创建请求异常({e.__class__.__name__})", attempt=attempt
                        )
                        return
                    except Exception as e:
                        if known_order_id is not None:
                            yield emit(
                                "status",
                                "订单号已取得，后续处理异常；保持停止下单，请核对官方订单",
                                BuyStreamUpdate(
                                    order_id=known_order_id,
                                    status="order_created",
                                ),
                            )
                        else:
                            yield stop_for_unknown_order(
                                f"创建响应处理异常({e.__class__.__name__})",
                                attempt=attempt,
                            )
                        return
                    finally:
                        attempt += 1

                    if (
                        result is not None
                        or token_expired
                        or terminal_result is not None
                    ):
                        break
                    # 按随机 create 次数主动复检项目详情（纯拉取，落在 sleep 窗口，不与 create 并发）
                    if refresh_count_enabled and refresh_target is not None:
                        refresh_counter += 1
                        if refresh_counter >= refresh_target:
                            try:
                                refresh_hot_and_warm()
                            except Exception as exc:
                                logger.warning(f"循环内项目详情复检失败（忽略）：{exc}")
                            _reset_refresh_counter()
                    if should_sleep_before_next_attempt:
                        time.sleep(request_interval / 1000)

                if (
                    result is not None
                    or token_expired
                    or terminal_result is not None
                    or not isRunning
                ):
                    break

            else:
                if config.show_random_message:
                    yield emit("status", f"群友说👴： {get_random_fail_message()}")
                yield emit(
                    "status",
                    None,
                    BuyStreamUpdate(
                        attempt_total=effective_retry_limit,
                    ),
                )
                continue
            if result is None:
                if terminal_result is not None:
                    errno, terminal_ret, terminal_rule = terminal_result
                    order_id = _extract_order_id(terminal_ret)
                    if terminal_rule.expose_payment_url and order_id is not None:
                        try:
                            yield from emit_order_assistance(
                                order_id, status=terminal_rule.status, notify=False
                            )
                        except Exception:
                            yield emit(
                                "status",
                                "现有订单的辅助处理失败，请核对官方订单；保持停止下单",
                                BuyStreamUpdate(
                                    order_id=order_id,
                                    status=terminal_rule.status,
                                ),
                            )
                    return
                reason = _format_retry_reason(retry_outcome)
                yield emit(
                    "status",
                    f"本轮创建订单未成功，{reason}",
                )
                yield emit_reprepare(reason)
                continue
            # A valid order ID was latched before any optional assistance. No
            # notification, browser or QR failure may return to creation.
            if known_order_id is not None:
                try:
                    yield from emit_order_assistance(
                        known_order_id, status="order_created", notify=True
                    )
                except Exception:
                    yield emit(
                        "status",
                        "订单号已取得，辅助处理异常；保持停止下单，请核对官方订单",
                        BuyStreamUpdate(
                            order_id=known_order_id,
                            status="order_created",
                        ),
                    )
                return
            yield stop_for_unknown_order("创建结果缺少有效订单号")
            return
        except BiliOrderResultUnknownError:
            yield stop_for_unknown_order("提交后的网络响应不确定")
            return
        except (HTTPError, RequestException) as e:
            logger.exception(e)
            yield emit("error", f"请求错误: {e}")
            for message in handle_proxy_failure(
                f"订单准备请求异常({e.__class__.__name__})"
            ):
                yield message
        except JSONDecodeError:
            yield from handle_non_json_response(
                "准备订单接口",
                request_result_normal,
                attempt=0,
            )
        except BiliRateLimitError as e:
            logger.warning(str(e))
            yield emit(
                "error",
                (
                    f"{e}，延迟 {rate_limit_delay_ms}ms 后重试准备订单"
                    if rate_limit_delay_ms > 0
                    else str(e)
                ),
            )
            if rate_limit_delay_ms > 0:
                time.sleep(rate_limit_delay_ms / 1000)
            yield emit_reprepare("订单准备阶段触发 HTTP 429")
        except BiliConnectionError as e:
            logger.warning(str(e))
            yield emit(
                "error",
                str(e),
            )
        except Exception as e:
            logger.exception(e)
            yield emit(
                "error",
                f"程序异常: {repr(e)}",
                BuyStreamUpdate(status="failed"),
            )


def buy_new_terminal(
    config: BuyConfig,
    log_file_path: str | None = None,
    log_level: str | None = None,
    log_retention_days: int | None = None,
) -> subprocess.Popen:
    return Buy(config=config).buy_new_terminal(
        log_file_path=log_file_path,
        log_level=log_level,
        log_retention_days=log_retention_days,
    )
