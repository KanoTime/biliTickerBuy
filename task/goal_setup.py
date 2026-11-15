"""Create the fixed FES goal from two local UI exports, without network access.

Only account and buyer identifiers are written into the goal file. Cookies and
attendee identity fields remain in the referenced exports. Full identity values
are compared only in memory and never included in returned summaries or errors.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from task.goal_config import (
    GoalAccount,
    GoalConfig,
    GoalConfigError,
    GoalTarget,
    _buyer_id,
    _cookie_uid,
    _full_identity,
)


_PEOPLE = ("P1", "P2", "P3")
_TARGET = GoalTarget(1006859, 1035758, 960028, "2026-11-28", "普通入场券", 19800)
_STARTS_AT = "2026-11-15T18:00:00+08:00"


class GoalSetupError(GoalConfigError):
    """A local import could not be completed; messages omit source values."""


def _read_export(path: str | Path, account: str) -> tuple[Path, dict[str, Any]]:
    try:
        resolved = Path(path).expanduser().resolve()
        source = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise GoalSetupError(
            f"账号 {account}：无法读取本机 UI 导出 JSON，请检查文件路径与格式"
        ) from None
    if not isinstance(source, dict):
        raise GoalSetupError(f"账号 {account}：UI 导出必须是 JSON 对象")
    buyers = source.get("buyer_info")
    if (
        not isinstance(buyers, list)
        or len(buyers) != 3
        or not all(isinstance(buyer, dict) for buyer in buyers)
    ):
        raise GoalSetupError(f"账号 {account}：UI 导出必须恰好预选三位购票人")
    return resolved, source


def _selected_b_order(
    a_buyers: list[dict[str, Any]],
    b_buyers: list[dict[str, Any]],
    b_order: tuple[int, int, int] | None,
) -> tuple[tuple[int, int, int], str]:
    if b_order is not None:
        if (
            not isinstance(b_order, (tuple, list))
            or len(b_order) != 3
            or any(type(index) is not int for index in b_order)
            or set(b_order) != {1, 2, 3}
        ):
            raise GoalSetupError("b_order 必须是 1、2、3 各出现一次的一基序号排列")
        # This is a local confirmation, not permission to bypass complete
        # identity contradictions. GoalConfig checks those before any write.
        return tuple(b_order), "locally_confirmed_order"

    a_identities = [_full_identity(buyer) for buyer in a_buyers]
    b_identities = [_full_identity(buyer) for buyer in b_buyers]
    if None in a_identities or None in b_identities:
        raise GoalSetupError(
            "导出包含脱敏或无法自动核对的证件；请在本机逐人核对 A、B 导出，"
            "再通过 --b-order 明确 B 中对应 A 第 1、2、3 人的一基序号；"
            "不能按同名、尾号或列表顺序猜测"
        )
    if len(set(a_identities)) != 3 or len(set(b_identities)) != 3:
        raise GoalSetupError("导出的完整证件不唯一，请在本机核对三位购票人")
    if set(a_identities) != set(b_identities):
        raise GoalSetupError("两份导出的完整证件集合不一致，请在本机核对并重新导出")
    return tuple(
        b_identities.index(value) + 1 for value in a_identities
    ), "full_identity"


def _toml_string(value: str) -> str:
    # JSON's string quoting also covers the basic TOML string escapes needed
    # here, including local filenames containing spaces, quotes or backslashes.
    return json.dumps(value, ensure_ascii=False)


def _render_goal(goal: GoalConfig, output: Path) -> str:
    lines = [
        "# 本机 UI 导出生成；凭据与实名原文保留在引用的 JSON 文件中。",
        "# A 导出顺序定义 P1/P2/P3；已有目标请保留此映射及原状态库。",
        "schema_version = 1",
        'people = ["P1", "P2", "P3"]',
        f"starts_at = {_toml_string(goal.starts_at.isoformat())}",
        f"ends_at = {_toml_string(goal.ends_at.isoformat())}",
        f"interval_seconds = {goal.interval_seconds}",
        f"order_poll_seconds = {goal.order_poll_seconds}",
        'state_path = "goal.sqlite3"',
        "",
        "[target]",
    ]
    for key, value in goal.target.to_dict().items():
        rendered = _toml_string(value) if isinstance(value, str) else str(value)
        lines.append(f"{key} = {rendered}")
    for account in goal.accounts:
        relative_path = os.path.relpath(account.config_path, output.parent)
        lines.extend(
            [
                "",
                "[[accounts]]",
                f"key = {_toml_string(account.key)}",
                f"uid = {_toml_string(account.uid)}",
                f"config_path = {_toml_string(relative_path)}",
                f"mapping_confirmed = {str(account.mapping_confirmed).lower()}",
                "[accounts.buyer_ids]",
                *[
                    f"{person} = {_toml_string(account.buyer_ids[person])}"
                    for person in _PEOPLE
                ],
            ]
        )
    return "\n".join(lines) + "\n"


def import_ticket_exports(
    *,
    account_a: str | Path,
    account_b: str | Path,
    ends_at: str | datetime,
    output: str | Path = "btb_goal_data/goal.toml",
    b_order: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """Create a new FES goal; never overwrite existing configuration or state.

    A's exported buyer order defines P1/P2/P3. ``b_order=(2, 3, 1)`` means
    A's first, second and third people are B's second, third and first entries.
    Supplying this parameter asserts the user verified that mapping locally.
    Without it, all three full identities must match uniquely across exports.
    ``ends_at`` is required and must have an explicit timezone.
    """
    try:
        requested_output = Path(output).expanduser()
        # Preserve the leaf itself so even a dangling output symlink is refused.
        destination = requested_output.parent.resolve() / requested_output.name
    except (OSError, ValueError, TypeError):
        raise GoalSetupError("output 必须是可用的本机目标配置路径") from None
    state_path = destination.parent / "goal.sqlite3"
    if destination == state_path:
        raise GoalSetupError("output 不能使用订单状态库路径")
    if destination.exists() or destination.is_symlink():
        raise GoalSetupError("目标配置已存在；请保留原人员映射和状态库，不会覆盖")
    if state_path.exists() or state_path.is_symlink():
        raise GoalSetupError("目标目录已有订单状态库；请使用原配置恢复，不会新建目标")

    a_path, a_source = _read_export(account_a, "A")
    b_path, b_source = _read_export(account_b, "B")
    selected_order, mapping_mode = _selected_b_order(
        a_source["buyer_info"], b_source["buyer_info"], b_order
    )
    confirmed = b_order is not None
    accounts: list[GoalAccount] = []
    for key, path, source, order in (
        ("A", a_path, a_source, (1, 2, 3)),
        ("B", b_path, b_source, selected_order),
    ):
        uid = _cookie_uid(source, f"account {key}")
        if uid is None:
            raise GoalSetupError(
                f"账号 {key}：导出 Cookie 缺少可核对的 UID，请重新登录并导出"
            )
        mapping = {
            person: _buyer_id(source["buyer_info"][index - 1], f"account {key} buyer")
            for person, index in zip(_PEOPLE, order, strict=True)
        }
        accounts.append(GoalAccount(key, uid, path, mapping, confirmed, source))

    goal = GoalConfig(
        target=_TARGET,
        people=_PEOPLE,
        accounts=tuple(accounts),
        starts_at=_STARTS_AT,
        ends_at=ends_at,
        state_path=state_path,
    )
    contents = _render_goal(goal, destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state_path.exists() or state_path.is_symlink():
            raise GoalSetupError("目标目录已有订单状态库；请使用原配置恢复")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
    except FileExistsError:
        raise GoalSetupError("目标配置已存在；不会覆盖，请使用原配置") from None
    except OSError:
        raise GoalSetupError(
            "无法完整写入目标配置；请在本机检查输出目录与文件"
        ) from None
    return {
        "ok": True,
        "mode": "local_export_import",
        "output": str(destination),
        "mapping_mode": mapping_mode,
        "goal": goal.public_summary(),
    }
