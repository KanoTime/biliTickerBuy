"""Local purchase-goal configuration and attendee-preserving batch construction.

Ticket exports contain credentials and document numbers. This module does not
log them or include ticket payloads in dataclass representations. Normalized
attendee identity fields participate in the aggregate goal hash; only that final
digest is persisted, never identity plaintext or per-person hashes. Credentials
and scheduling preferences do not participate. Loading makes no platform requests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}\Z")
_MASK_MARKERS = re.compile(r"[*＊•●…]|[xX]{2,}")


class GoalConfigError(ValueError):
    """Invalid local goal configuration; messages never include source values."""


def _decimal_id(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise GoalConfigError(f"{field_name} must be a positive decimal ID")
    text = str(value).strip()
    if len(text) > 64 or not text.isascii() or not text.isdecimal() or int(text) <= 0:
        raise GoalConfigError(f"{field_name} must be a positive decimal ID")
    return str(int(text))


def _positive_int(value: Any, field_name: str) -> int:
    return int(_decimal_id(value, field_name))


def _key(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _KEY_PATTERN.fullmatch(value):
        raise GoalConfigError(
            f"{field_name} must be a short local alias using letters, digits, _ or -"
        )
    return value


def _duration(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GoalConfigError(f"{field_name} must be a positive duration in seconds")
    try:
        result = float(value)
    except OverflowError:
        raise GoalConfigError(
            f"{field_name} must be a positive duration in seconds"
        ) from None
    if not math.isfinite(result) or result <= 0:
        raise GoalConfigError(f"{field_name} must be a positive duration in seconds")
    return result


def _aware_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise GoalConfigError(
                f"{field_name} requires an ISO datetime with an explicit +08:00 offset"
            ) from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise GoalConfigError(
            f"{field_name} requires an ISO datetime with an explicit timezone offset"
        )
    return value.astimezone(SHANGHAI)


def _only_fields(data: Mapping[str, Any], allowed: set[str], section: str) -> None:
    if set(data) - allowed:
        raise GoalConfigError(f"{section} contains an unsupported option")


def _require_mapping(value: Any, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoalConfigError(f"{section} must be a table/object")
    return value


def _buyer_id(buyer: Mapping[str, Any], section: str) -> str:
    primary = buyer.get("id", buyer.get("buyer_id"))
    result = _decimal_id(primary, f"{section}.id")
    if "id" in buyer and "buyer_id" in buyer:
        if _decimal_id(buyer["buyer_id"], f"{section}.buyer_id") != result:
            raise GoalConfigError(f"{section} has conflicting buyer identifiers")
    return result


def _full_identity(buyer: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return comparison evidence only for an unmasked complete-looking value.

    This is not document authenticity validation. Names and masked suffixes are
    deliberately never used as automatic cross-account identity evidence.
    """
    value = buyer.get("personal_id")
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    # An unmarked short suffix is still not a full identity. Other document
    # formats require a local mapping confirmation instead of guessing their
    # format or completeness from names, lengths or suffix matches.
    if _MASK_MARKERS.search(value) or not re.fullmatch(r"[0-9]{17}[0-9X]", value):
        return None
    kind = str(buyer.get("id_type", "unspecified"))
    return kind, value


def _cookie_uid(config: Mapping[str, Any], section: str) -> str | None:
    cookies = config.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise GoalConfigError(f"{section}.cookies must contain the local login cookies")
    found: set[str] = set()
    names: set[str] = set()
    for cookie in cookies:
        if (
            not isinstance(cookie, Mapping)
            or not isinstance(cookie.get("name"), str)
            or not cookie["name"]
            or cookie.get("value") in (None, "")
        ):
            raise GoalConfigError(f"{section}.cookies contains an invalid entry")
        name = cookie["name"]
        if name in {"DedeUserID", "DSUserID"}:
            found.add(_decimal_id(cookie["value"], f"{section}.cookie UID"))
        names.add(name)
    if not {"SESSDATA", "bili_jct"}.issubset(names):
        raise GoalConfigError(f"{section}.cookies requires SESSDATA and bili_jct")
    if len(found) > 1:
        raise GoalConfigError(f"{section}.cookies has conflicting account UIDs")
    return next(iter(found), None)


@dataclass(frozen=True, slots=True)
class GoalTarget:
    project_id: int
    screen_id: int
    sku_id: int
    admission_date: str
    ticket_name: str
    price_cents: int

    def __post_init__(self) -> None:
        for name in ("project_id", "screen_id", "sku_id", "price_cents"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        value = self.admission_date
        if isinstance(value, date) and not isinstance(value, datetime):
            value = value.isoformat()
        try:
            parsed = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.isoformat() != value:
            raise GoalConfigError("target.admission_date must use YYYY-MM-DD")
        object.__setattr__(self, "admission_date", value)
        if not isinstance(self.ticket_name, str) or not self.ticket_name.strip():
            raise GoalConfigError(
                "target.ticket_name must be the selected ticket label"
            )
        object.__setattr__(self, "ticket_name", self.ticket_name.strip())

    @property
    def key(self) -> str:
        return (
            f"bilibili:{self.project_id}:{self.screen_id}:{self.sku_id}:"
            f"{self.admission_date}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "screen_id": self.screen_id,
            "sku_id": self.sku_id,
            "admission_date": self.admission_date,
            "ticket_name": self.ticket_name,
            "price_cents": self.price_cents,
        }


@dataclass(frozen=True, slots=True)
class GoalAccount:
    key: str
    uid: str
    config_path: Path
    buyer_ids: Mapping[str, str]
    mapping_confirmed: bool
    ticket_config: dict[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _key(self.key, "account.key"))
        object.__setattr__(self, "uid", _decimal_id(self.uid, "account.uid"))
        object.__setattr__(self, "config_path", Path(self.config_path))
        if not isinstance(self.mapping_confirmed, bool):
            raise GoalConfigError("account.mapping_confirmed must be true or false")
        raw_ids = _require_mapping(self.buyer_ids, "account.buyer_ids")
        ids = {
            _key(person, "person key"): _decimal_id(value, "account.buyer_ids entry")
            for person, value in raw_ids.items()
        }
        if not ids or len(set(ids.values())) != len(ids):
            raise GoalConfigError(
                "account.buyer_ids must map each person to a unique ID"
            )
        object.__setattr__(self, "buyer_ids", MappingProxyType(ids))
        if not isinstance(self.ticket_config, dict):
            raise GoalConfigError("account ticket export must be a JSON object")
        object.__setattr__(self, "ticket_config", copy.deepcopy(self.ticket_config))
        cookie_uid = _cookie_uid(self.ticket_config, f"account {self.key}")
        if cookie_uid is not None and cookie_uid != self.uid:
            raise GoalConfigError(
                f"account {self.key}: cookie UID differs from configured UID"
            )

    def _buyers(self) -> dict[str, dict[str, Any]]:
        value = self.ticket_config.get("buyer_info")
        if not isinstance(value, list) or not value:
            raise GoalConfigError(
                f"account {self.key}: buyer_info must be a non-empty list"
            )
        result: dict[str, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                raise GoalConfigError(f"account {self.key}: invalid buyer_info entry")
            buyer_id = _buyer_id(item, f"account {self.key} buyer")
            if buyer_id in result:
                raise GoalConfigError(
                    f"account {self.key}: repeated buyer ID in export"
                )
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise GoalConfigError(f"account {self.key}: buyer name is missing")
            if (
                not isinstance(item.get("personal_id"), str)
                or not item["personal_id"].strip()
            ):
                raise GoalConfigError(
                    f"account {self.key}: buyer identity field is missing"
                )
            result[buyer_id] = item
        if set(result) != set(self.buyer_ids.values()):
            raise GoalConfigError(
                f"account {self.key}: export buyers must exactly match the configured roster"
            )
        return result

    def validate_for(self, target: GoalTarget, people: Sequence[str]) -> None:
        if set(self.buyer_ids) != set(people):
            raise GoalConfigError(
                f"account {self.key}: buyer mapping must cover every person"
            )
        source = self.ticket_config
        for name in ("project_id", "screen_id", "sku_id"):
            if _positive_int(source.get(name), f"account {self.key}.{name}") != getattr(
                target, name
            ):
                raise GoalConfigError(
                    f"account {self.key}: exported {name} differs from target"
                )
        if _positive_int(source.get("count"), f"account {self.key}.count") != len(
            people
        ):
            raise GoalConfigError(
                f"account {self.key}: exported count differs from roster size"
            )
        expected = target.price_cents * len(people)
        if (
            _positive_int(source.get("pay_money"), f"account {self.key}.pay_money")
            != expected
        ):
            raise GoalConfigError(
                f"account {self.key}: exported amount differs from fixed target price"
            )
        if (
            "unit_price" in source
            and _positive_int(source["unit_price"], f"account {self.key}.unit_price")
            != target.price_cents
        ):
            raise GoalConfigError(
                f"account {self.key}: exported unit price differs from target"
            )
        order_type = source.get("order_type", 1)
        if _positive_int(order_type, f"account {self.key}.order_type") != 1:
            raise GoalConfigError(
                f"account {self.key}: only ordinary ticket orders are supported"
            )
        for name in ("buyer", "tel"):
            if not isinstance(source.get(name), str) or not source[name].strip():
                raise GoalConfigError(
                    f"account {self.key}: purchase contact field {name} is missing"
                )
        delivery = source.get("deliver_info")
        if not isinstance(delivery, dict) or any(
            delivery.get(name) in (None, "")
            for name in ("name", "tel", "addr_id", "addr")
        ):
            raise GoalConfigError(f"account {self.key}: deliver_info is incomplete")
        self._buyers()
        cookie_uid = _cookie_uid(source, f"account {self.key}")
        if cookie_uid is not None and cookie_uid != self.uid:
            raise GoalConfigError(
                f"account {self.key}: cookie UID changed; revalidate account mapping"
            )

    def payload_for(
        self, person_keys: Sequence[str], target: GoalTarget
    ) -> dict[str, Any]:
        if not isinstance(person_keys, (tuple, list)) or not all(
            isinstance(person, str) for person in person_keys
        ):
            raise GoalConfigError("batch must list person keys from the fixed roster")
        people = tuple(person_keys)
        if (
            not people
            or len(set(people)) != len(people)
            or not set(people).issubset(self.buyer_ids)
        ):
            raise GoalConfigError(
                "batch must contain distinct people from the fixed roster"
            )
        self.validate_for(target, tuple(self.buyer_ids))
        buyers = self._buyers()
        result = copy.deepcopy(self.ticket_config)
        for name in (
            "token",
            "ptoken",
            "ctoken",
            "timestamp",
            "again",
            "_prepare_buyer_info",
        ):
            result.pop(name, None)
        result["buyer_info"] = [
            copy.deepcopy(buyers[self.buyer_ids[key]]) for key in people
        ]
        result["count"] = len(people)
        result["pay_money"] = target.price_cents * len(people)
        result["order_type"] = 1
        # Existing exports put real attendee names in detail. A local alias is
        # sufficient for this runner and avoids copying that label into logs.
        result["detail"] = f"goal:{target.key}:{self.key}:{','.join(people)}"
        return result


@dataclass(frozen=True, slots=True)
class GoalConfig:
    target: GoalTarget
    people: tuple[str, ...]
    accounts: tuple[GoalAccount, ...]
    starts_at: datetime
    ends_at: datetime
    interval_seconds: float = 1.0
    order_poll_seconds: float = 10.0
    state_path: Path = Path("btb_goal_data/goal.sqlite3")

    def __post_init__(self) -> None:
        if not isinstance(self.target, GoalTarget):
            raise GoalConfigError("target must be a GoalTarget")
        if not isinstance(self.people, (tuple, list)):
            raise GoalConfigError("people must list the fixed local person keys")
        people = tuple(_key(value, "people entry") for value in self.people)
        if not people or len(set(people)) != len(people):
            raise GoalConfigError(
                "people must be a non-empty list of unique person keys"
            )
        object.__setattr__(self, "people", people)
        if not isinstance(self.accounts, (tuple, list)) or not self.accounts:
            raise GoalConfigError("accounts must contain at least one account")
        accounts = tuple(self.accounts)
        if not all(isinstance(account, GoalAccount) for account in accounts):
            raise GoalConfigError("accounts must contain GoalAccount values")
        if len({account.key for account in accounts}) != len(accounts):
            raise GoalConfigError("account keys must be unique")
        if len({account.uid for account in accounts}) != len(accounts):
            raise GoalConfigError("each configured account must have a different UID")
        object.__setattr__(self, "accounts", accounts)
        object.__setattr__(self, "starts_at", _aware_time(self.starts_at, "starts_at"))
        object.__setattr__(self, "ends_at", _aware_time(self.ends_at, "ends_at"))
        if self.ends_at <= self.starts_at:
            raise GoalConfigError("ends_at must be later than starts_at")
        for name in ("interval_seconds", "order_poll_seconds"):
            object.__setattr__(self, name, _duration(getattr(self, name), name))
        object.__setattr__(self, "state_path", Path(self.state_path))
        if any(
            self.state_path.resolve() == account.config_path.resolve()
            for account in accounts
        ):
            raise GoalConfigError("state_path must not overwrite a ticket export")
        for account in accounts:
            account.validate_for(self.target, people)
        self._validate_identities()

    def _validate_identities(self) -> None:
        confirmed: dict[str, tuple[str, str]] = {}
        identity_owners: dict[tuple[str, str], str] = {}
        for account in self.accounts:
            buyers = account._buyers()
            for person in self.people:
                identity = _full_identity(buyers[account.buyer_ids[person]])
                if identity is None:
                    if not account.mapping_confirmed:
                        raise GoalConfigError(
                            f"account {account.key}: masked identities require explicit local mapping_confirmed"
                        )
                    continue
                existing = confirmed.get(person)
                if existing is not None and existing != identity:
                    raise GoalConfigError(
                        f"person {person}: complete identity fields disagree between accounts"
                    )
                other = identity_owners.get(identity)
                if other is not None and other != person:
                    raise GoalConfigError(
                        "the fixed roster contains the same complete identity more than once"
                    )
                confirmed[person] = identity
                identity_owners[identity] = person

    @property
    def scope_key(self) -> str:
        # Daily identity ownership must not be divided by account or run ID.
        return f"bilibili:{self.target.project_id}:{self.target.admission_date}"

    @property
    def fingerprint(self) -> str:
        # Bind the real attendee snapshot as well as account-local buyer IDs.
        # A saved buyer record can be edited without changing its ID; old paid
        # ownership must never silently carry over to that replacement person.
        # These values exist only in memory while computing the aggregate hash.
        identities: dict[str, dict[str, dict[str, str]]] = {}
        for account in self.accounts:
            buyers = account._buyers()
            identities[account.key] = {
                person: {
                    "name": str(buyers[account.buyer_ids[person]]["name"]).strip(),
                    "id_type": str(
                        buyers[account.buyer_ids[person]].get("id_type", "unspecified")
                    ).strip(),
                    "personal_id": str(buyers[account.buyer_ids[person]]["personal_id"])
                    .strip()
                    .upper(),
                }
                for person in self.people
            }
        value = {
            "schema_version": 1,
            "target": self.target.to_dict(),
            "people": list(self.people),
            "attendee_identity_snapshot": identities,
            "accounts": [
                {
                    "key": account.key,
                    "uid": account.uid,
                    "buyer_ids": dict(account.buyer_ids),
                }
                for account in self.accounts
            ],
        }
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def account(self, key: str) -> GoalAccount:
        for account in self.accounts:
            if account.key == key:
                return account
        raise GoalConfigError("unknown account key")

    def payload_for(
        self, account_key: str, person_keys: Sequence[str]
    ) -> dict[str, Any]:
        return self.account(account_key).payload_for(person_keys, self.target)

    def public_summary(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "people": list(self.people),
            "account_keys": [account.key for account in self.accounts],
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "interval_seconds": self.interval_seconds,
            "order_poll_seconds": self.order_poll_seconds,
            "scope_key": self.scope_key,
        }

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], *, base_dir: str | Path = "."
    ) -> GoalConfig:
        data = _require_mapping(data, "goal")
        _only_fields(
            data,
            {
                "schema_version",
                "target",
                "people",
                "accounts",
                "starts_at",
                "ends_at",
                "interval_seconds",
                "order_poll_seconds",
                "state_path",
            },
            "goal",
        )
        if data.get("schema_version", 1) != 1 or isinstance(
            data.get("schema_version"), bool
        ):
            raise GoalConfigError("unsupported goal schema_version")
        target_data = _require_mapping(data.get("target"), "target")
        _only_fields(
            target_data,
            {
                "project_id",
                "screen_id",
                "sku_id",
                "admission_date",
                "ticket_name",
                "price_cents",
            },
            "target",
        )
        try:
            target = GoalTarget(**target_data)
        except TypeError:
            raise GoalConfigError(
                "target must specify all six fixed target fields"
            ) from None
        root = Path(base_dir).expanduser().resolve()
        raw_accounts = data.get("accounts")
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise GoalConfigError("accounts must be a non-empty array")
        accounts: list[GoalAccount] = []
        for raw in raw_accounts:
            raw = _require_mapping(raw, "account")
            _only_fields(
                raw,
                {"key", "uid", "config_path", "buyer_ids", "mapping_confirmed"},
                "account",
            )
            account_key = _key(raw.get("key"), "account.key")
            uid = _decimal_id(raw.get("uid"), f"account {account_key}.uid")
            config_path = raw.get("config_path")
            if not isinstance(config_path, str) or not config_path.strip():
                raise GoalConfigError(f"account {account_key}.config_path is required")
            path = Path(config_path).expanduser()
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            try:
                source = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise GoalConfigError(
                    f"account {account_key}: cannot read its local ticket JSON export"
                ) from None
            accounts.append(
                GoalAccount(
                    key=account_key,
                    uid=uid,
                    config_path=path,
                    buyer_ids=raw.get("buyer_ids"),
                    mapping_confirmed=raw.get("mapping_confirmed", False),
                    ticket_config=source,
                )
            )
        state_path = data.get("state_path", "btb_goal_data/goal.sqlite3")
        if not isinstance(state_path, str) or not state_path.strip():
            raise GoalConfigError("state_path must be a local file path")
        state = Path(state_path).expanduser()
        if not state.is_absolute():
            state = root / state
        return cls(
            target=target,
            people=data.get("people"),
            accounts=tuple(accounts),
            starts_at=data.get("starts_at"),
            ends_at=data.get("ends_at"),
            interval_seconds=data.get("interval_seconds", 1.0),
            order_poll_seconds=data.get("order_poll_seconds", 10.0),
            state_path=state.resolve(),
        )


def load_goal_config(path: str | Path) -> GoalConfig:
    """Load TOML and account JSON exports without contacting the platform."""
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        raise GoalConfigError("cannot read purchase-goal TOML configuration") from None
    result = GoalConfig.from_mapping(data, base_dir=source.parent)
    if result.state_path == source:
        raise GoalConfigError("state_path must not overwrite the goal configuration")
    return result
