"""Local entry point: python -m app_cmd.goal --help."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from task.goal_config import GoalConfigError, load_goal_config
from task.goal_coordinator import GoalCoordinator
from task.goal_ledger import GoalLedger, GoalLedgerError


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def _event_sink(*, open_payment: bool = False):
    def emit(event):
        if event.get("kind") == "order_created":
            url = "https://show.bilibili.com/platform/orderDetail.html?order_id=" + str(
                event["order_id"]
            )
            event = {
                **event,
                "payment_url": url,
                "message": "订单已创建，请到官方订单中心核对并付款",
            }
            if open_payment:

                def open_page():
                    try:
                        webbrowser.open(url)
                    except Exception:
                        pass

                threading.Thread(target=open_page, daemon=True).start()
        _print_json(event)

    return emit


def _executors(config):
    # This entry point schedules from an explicit, timezone-aware local window.
    # Avoid legacy import-time NTP calls; the OS clock must be synchronized.
    os.environ.setdefault("BTB_SKIP_INITIAL_TIME_SYNC", "1")
    from task.goal_adapter import BiliGoalAccount

    return {
        account.key: BiliGoalAccount(account, config.target)
        for account in config.accounts
    }


def _person_order(value: str) -> tuple[int, int, int]:
    try:
        positions = tuple(int(part.strip()) for part in value.split(","))
    except ValueError:
        positions = ()
    if sorted(positions) != [1, 2, 3]:
        raise argparse.ArgumentTypeError(
            "请填写本机核对后的 B 账号人员位置，例如 2,1,3；每个位置恰好一次"
        )
    return positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="固定日期票档、固定购票人的 B站个人购买目标"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="离线演示三人优先、分批及回流，不连接平台")
    template = commands.add_parser("template", help="生成本机目标配置模板")
    template.add_argument("--output", default="btb_goal_data/goal.toml")
    imported = commands.add_parser(
        "import-exports", help="从两份本机导出填写 UID 和购票人映射，不连接平台"
    )
    imported.add_argument("--account-a", required=True)
    imported.add_argument("--account-b", required=True)
    imported.add_argument("--ends-at", required=True)
    imported.add_argument("--output", default="btb_goal_data/goal.toml")
    imported.add_argument(
        "--b-order",
        type=_person_order,
        help="本机核对后，B 导出中分别对应 A 的 P1/P2/P3 的位置，例如 2,1,3",
    )
    for name, help_text in (
        ("validate", "仅检查本地配置，不连接平台"),
        ("preflight", "只读核对登录账号和当前目标，不提交订单"),
        ("run", "按配置窗口自动购买并持续守回流"),
        ("status", "查看本机目标及各人的订单状态"),
        ("stop", "停止后续提交，保留已有订单和未决占用"),
        ("confirm", "记录你在官方订单中心核实的结果"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", default="btb_goal_data/goal.toml")
        if name == "run":
            command.add_argument(
                "--resume", action="store_true", help="继续已停止的目标，保留原订单占用"
            )
            command.add_argument(
                "--open-payment", action="store_true", help="创建订单后打开官方付款详情"
            )
        if name == "confirm":
            command.add_argument(
                "--attempt", required=True, help="status 中的 attempt_id"
            )
            command.add_argument(
                "--outcome",
                required=True,
                choices=("pending", "paid", "closed", "no_order"),
            )
            command.add_argument("--order-id", help="官方订单号；未创建订单时不填写")
    return parser


def _write_template(path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1] / "examples" / "purchase-goal.example.toml"
    )
    contents = source.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
    _print_json(
        {
            "template": str(path),
            "message": "本场目标已预填；请填写账号导出、UID、人员映射及结束时间，或用 import-exports 从导出生成配置",
        }
    )


def _read_only_preflight(config) -> int:
    executors = _executors(config)
    checks = []
    started = time.perf_counter()
    try:
        for account in config.accounts:
            account_started = time.perf_counter()
            try:
                result = executors[account.key].preflight()
                ready = result.ready and str(result.account_uid) == account.uid
                checks.append(
                    {
                        "account": account.key,
                        "ready": ready,
                        "reason": result.reason
                        or ("" if ready else "登录账号与配置不一致"),
                        "retryable": result.retryable,
                        "retry_after": result.retry_after,
                    }
                )
            except Exception:
                checks.append(
                    {"account": account.key, "ready": False, "reason": "只读预检未完成"}
                )
            checks[-1]["elapsed_ms"] = round(
                (time.perf_counter() - account_started) * 1000, 1
            )
    finally:
        for executor in executors.values():
            executor.close()
    ok = all(item["ready"] for item in checks)
    _print_json(
        {
            "ok": ok,
            "mode": "read_only_preflight",
            "checks": checks,
            "goal": config.public_summary(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "environment": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        }
    )
    return 0 if ok else 2


def _run(config, ledger, args) -> int:
    stop_event = threading.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: stop_event.set())
    try:
        coordinator = GoalCoordinator(
            config,
            ledger,
            _executors(config),
            stop_event=stop_event,
            event_sink=_event_sink(open_payment=args.open_payment),
        )
        _print_json({"mode": "live", "goal": config.public_summary()})
        summary = coordinator.run(resume=args.resume)
        _print_json({"summary": summary})
        return 0 if summary["counts"]["paid"] == len(config.people) else 3
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            from task.goal_demo import run_demo

            _print_json(
                run_demo(
                    event_sink=lambda event: _print_json(
                        {"mode": "offline_demo", **event}
                    )
                )
            )
            return 0
        if args.command == "template":
            _write_template(Path(args.output))
            return 0
        if args.command == "import-exports":
            from task.goal_setup import import_ticket_exports

            _print_json(
                import_ticket_exports(
                    account_a=args.account_a,
                    account_b=args.account_b,
                    ends_at=args.ends_at,
                    output=args.output,
                    b_order=args.b_order,
                )
            )
            return 0
        config = load_goal_config(args.config)
        if args.command == "validate":
            _print_json(
                {
                    "ok": True,
                    "mode": "local_validation",
                    "goal": config.public_summary(),
                }
            )
            return 0
        if args.command == "preflight":
            return _read_only_preflight(config)
        if (
            args.command in {"status", "stop", "confirm"}
            and not config.state_path.exists()
        ):
            _print_json({"state": "not_started", "message": "目标尚未建立运行记录"})
            return 2 if args.command == "confirm" else 0
        with GoalLedger(
            config.state_path, config.scope_key, config.fingerprint, config.people
        ) as ledger:
            if args.command == "run":
                return _run(config, ledger, args)
            if args.command == "stop":
                ledger.stop("user_stopped")
            elif args.command == "confirm":
                # Manual evidence is entered only after stopping the active
                # coordinator. It is recorded separately from API verification.
                with ledger.runner_lock():
                    if not ledger.snapshot(include_history=False)["stopped"]:
                        raise GoalLedgerError("请先执行 stop，再记录官方核验结果")
                    ledger.recover()
                    ledger.complete(
                        args.attempt,
                        args.outcome,
                        order_id=args.order_id,
                        reason="user_verified:" + args.outcome,
                    )
            _print_json({"summary": ledger.snapshot(include_history=False)})
            return 0
    except (GoalConfigError, GoalLedgerError, ValueError, OSError) as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
