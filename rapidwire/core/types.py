"""Enumerations shared by the ledger and the services, and the stored JSON convention of to_json(), which writes every integer as a decimal string so 64-bit IDs and DECIMAL(38,0) amounts survive JavaScript clients."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

type JsonValue = bool | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


class ExecutionType(StrEnum):
    TRANSFER = "transfer"
    TRANSFER_FROM = "transfer_from"
    APPROVE = "approve"
    MINT = "mint"
    BURN = "burn"
    CURRENCY_CREATE = "currency_create"
    CURRENCY_RENOUNCE = "currency_renounce"
    RATE_CHANGE_REQUEST = "rate_change_request"
    RATE_CHANGE_APPLY = "rate_change_apply"
    CURRENCY_DELETE_REQUEST = "currency_delete_request"
    CURRENCY_DELETE_FINALIZE = "currency_delete_finalize"
    DELEGATION_ENABLE = "delegation_enable"
    DELEGATION_DISABLE = "delegation_disable"
    STAKE_DEPOSIT = "stake_deposit"
    STAKE_WITHDRAW = "stake_withdraw"
    INTEREST = "interest"
    CLAIM_CREATE = "claim_create"
    CLAIM_PAY = "claim_pay"
    CLAIM_CANCEL = "claim_cancel"
    NOTIFICATION_ALLOW = "notification_allow"
    NOTIFICATION_DENY = "notification_deny"
    DISCORD_PERMISSION_ALLOW = "discord_permission_allow"
    DISCORD_PERMISSION_DENY = "discord_permission_deny"
    TOKEN_ISSUE = "token_issue"  # noqa: S105
    TOKEN_REVOKE = "token_revoke"  # noqa: S105
    POOL_CREATE = "pool_create"
    LIQUIDITY_ADD = "liquidity_add"
    LIQUIDITY_REMOVE = "liquidity_remove"
    SWAP = "swap"
    CONTRACT_CALL = "contract_call"
    CONTRACT_UPDATE = "contract_update"
    CONTRACT_LOCK = "contract_lock"
    CONTRACT_PAUSE = "contract_pause"
    CONTRACT_RESUME = "contract_resume"
    CONTRACT_DELETE = "contract_delete"
    BACKUP_LIST = "backup_list"
    BACKUP_DOWNLOAD = "backup_download"


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    REVERTED = "reverted"


class TransferKind(StrEnum):
    PRINCIPAL = "principal"
    INTEREST = "interest"
    GAS = "gas"


class AccountKind(StrEnum):
    USER = "user"
    ISSUANCE = "issuance"
    STAKE_CUSTODY = "stake_custody"
    POOL_CUSTODY = "pool_custody"


class ClaimStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    CANCELED = "canceled"


class OutboxStatus(StrEnum):
    """PENDING waits for the dispatcher; SENT, FAILED and SKIPPED (no longer wanted when its turn came, such as a claim paid before its notification) are terminal."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class OutboxKind(StrEnum):
    CLAIM_CREATED = "claim_created"
    DISCORD_SEND = "discord_send"
    DISCORD_ADD_ROLE = "discord_add_role"
    DISCORD_REMOVE_ROLE = "discord_remove_role"


def to_json(value: Any) -> JsonValue:
    """Convert a value to the stored JSON form: ints become decimal strings, aware datetimes become ISO 8601 UTC strings, tuples become lists; floats, naive datetimes, non-string keys and other types raise TypeError."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("naive datetime cannot be stored in JSON")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be str, got {type(key).__name__}")
            result[key] = to_json(item)
        return result
    if isinstance(value, list | tuple):
        return [to_json(item) for item in value]
    raise TypeError(f"{type(value).__name__} cannot be stored in JSON")


def to_json_object(value: Mapping[str, Any]) -> JsonObject:
    """to_json for a mapping, typed as a JSON object."""
    result = to_json(value)
    if not isinstance(result, dict):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return result
