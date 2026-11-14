"""An offline, deterministic example of partial purchase and returning stock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from task.goal_config import GoalAccount, GoalConfig, GoalTarget
from task.goal_coordinator import GoalCoordinator
from task.goal_ledger import GoalLedger
from task.goal_types import AttemptResult, OrderObservation, PreflightResult


def run_demo(event_sink=None):
    current = [datetime(2026, 11, 15, 10, tzinfo=timezone.utc).timestamp()]
    target = GoalTarget(1006859, 71, 82, "2026-11-28", "普通入场券", 19800)
    accounts = []
    for index, key in enumerate(("A", "B"), start=1):
        buyers = [
            {
                "id": index * 100 + person,
                "name": f"Demo {person}",
                "personal_id": f"DEMO-{person}",
            }
            for person in (1, 2, 3)
        ]
        accounts.append(
            GoalAccount(
                key,
                str(index),
                Path(f"demo-{key}.json"),
                {f"P{person}": str(index * 100 + person) for person in (1, 2, 3)},
                True,
                {
                    "project_id": target.project_id,
                    "screen_id": target.screen_id,
                    "sku_id": target.sku_id,
                    "count": 3,
                    "pay_money": 59400,
                    "buyer_info": buyers,
                    "buyer": "Demo",
                    "tel": "DEMO",
                    "deliver_info": {
                        "name": "Demo",
                        "tel": "DEMO",
                        "addr_id": 1,
                        "addr": "DEMO",
                    },
                    "cookies": [
                        {"name": "DedeUserID", "value": str(index)},
                        {"name": "SESSDATA", "value": "DEMO-ONLY"},
                        {"name": "bili_jct", "value": "DEMO-ONLY"},
                    ],
                },
            )
        )
    config = GoalConfig(
        target,
        ("P1", "P2", "P3"),
        tuple(accounts),
        datetime.fromtimestamp(current[0], timezone.utc),
        datetime.fromtimestamp(current[0], timezone.utc) + timedelta(minutes=5),
        order_poll_seconds=1,
    )
    calls = []
    single_waited = [False]

    class DemoAccount:
        def __init__(self, account):
            self.account = account

        def preflight(self):
            return PreflightResult(True, account_uid=self.account.uid)

        def attempt(self, people, before_submit):
            calls.append({"account": self.account.key, "people": list(people)})
            if len(people) == 1 and not single_waited[0]:
                single_waited[0] = True
                return AttemptResult(
                    "sold_out", definitive_no_order=True, reason="演示：目标暂时售罄"
                )
            before_submit()
            if len(people) == 3:
                return AttemptResult(
                    "shortage",
                    code=100009,
                    definitive_no_order=True,
                    reason="演示：三张余票不足",
                )
            return AttemptResult("created", order_id=10000 + len(calls))

        def reconcile(self, record):
            return OrderObservation(
                "paid",
                order_id=record["order_id"],
                person_keys=tuple(record["person_keys"]),
                account_key=self.account.key,
                target_key=target.key,
                verified=True,
                reason="演示中的模拟付款结果",
            )

        def close(self):
            pass

    with TemporaryDirectory(prefix="btb-goal-demo-") as directory:
        with GoalLedger(
            Path(directory) / "demo.sqlite3",
            config.scope_key,
            config.fingerprint,
            config.people,
        ) as ledger:
            coordinator = GoalCoordinator(
                config,
                ledger,
                {a.key: DemoAccount(a) for a in accounts},
                now=lambda: current[0],
                event_sink=event_sink,
            )
            with ledger.runner_lock():
                coordinator.initialize()
                for _ in range(20):
                    snapshot = coordinator.step()
                    if snapshot["stopped"]:
                        break
                    current[0] += 1
                else:
                    raise RuntimeError("offline demonstration did not reach completion")
            return {"mode": "offline_demo", "calls": calls, "summary": snapshot}
