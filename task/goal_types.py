"""Small, explicit result contracts for a coordinated personal purchase goal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AttemptOutcome = Literal[
    "created", "shortage", "sold_out", "retry", "blocked", "review", "unknown"
]
OrderStatus = Literal["pending", "paid", "closed", "unknown"]
BlockedScope = Literal["account", "goal"]


@dataclass(frozen=True, slots=True)
class AttemptResult:
    outcome: AttemptOutcome
    order_id: int | str | None = None
    code: int | None = None
    reason: str = ""
    retry_after: float = 0.0
    definitive_no_order: bool = False
    blocked_scope: BlockedScope = "account"


@dataclass(frozen=True, slots=True)
class OrderObservation:
    status: OrderStatus
    order_id: int | str | None = None
    person_keys: tuple[str, ...] = ()
    account_key: str = ""
    target_key: str = ""
    verified: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PreflightResult:
    ready: bool
    reason: str = ""
    blocked_scope: BlockedScope = "account"
    account_uid: str | None = None
    retryable: bool = False
    retry_after: float = 0.0
