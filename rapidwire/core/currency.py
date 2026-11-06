"""Currencies, one per Discord guild with the guild ID as currency ID: creation, mint, burn, renounce, timelocked rate changes and deletion, and delegation of management to Administrators, decided on a GuildSnapshot taken before the transaction."""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import RowMapping, and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

# Importing pools registers the DEX pool dissolver that finalize_delete_in_tx() calls, so no process can delete a currency without it.
from rapidwire.core import allowances, claims, pools  # noqa: F401
from rapidwire.core.accounts import get_or_create_issuance_account
from rapidwire.core.clock import resolve_now
from rapidwire.core.db import Db, reader
from rapidwire.core.errors import (
    AlreadyExists,
    AmountOverflow,
    CurrencyNotFound,
    CurrencyRenounced,
    DomainError,
    InvalidArgument,
    InvalidState,
    NoPendingRequest,
    RequestExpired,
    TimelockActive,
)
from rapidwire.core.hooks import get_pool_dissolver
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, Body, FailureHook, LedgerTx, OperationResult, RerunTransaction, mysql_error_code, run_operation
from rapidwire.core.permissions import GuildInfo, GuildResolver, GuildSnapshot, require_currency_manager, require_guild_owner
from rapidwire.core.staking import AccrualTrigger, accrue_currency_stakes_in_tx
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.core.validation import require_storable_int, validate_guild_id, validate_non_negative_amount, validate_positive_amount, validate_user_account_id
from rapidwire.db.tables import (
    CURRENCY_NAME_MAX,
    CURRENCY_NAME_PATTERN,
    CURRENCY_NAME_UNDERSCORES_MAX,
    CURRENCY_SYMBOL_HYPHENS_MAX,
    CURRENCY_SYMBOL_MAX,
    CURRENCY_SYMBOL_PATTERN,
    RATE_PPM_MAX,
    balance,
    currency,
    stake,
)
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "DELETE_EXPIRY",
    "DELETE_TIMELOCK",
    "RATE_CHANGE_TIMELOCK",
    "CurrencyCreated",
    "CurrencyDeleted",
    "CurrencyInfo",
    "DelegationChanged",
    "DeleteRequested",
    "MintingRenounced",
    "RateChangeApplied",
    "RateChangeRequested",
    "SupplyChanged",
    "apply_rate_change",
    "apply_rate_change_in_tx",
    "burn",
    "burn_in_tx",
    "create_currency",
    "create_currency_in_tx",
    "delete_request_active",
    "disable_delegation",
    "enable_delegation",
    "finalize_delete",
    "finalize_delete_in_tx",
    "get_currency",
    "get_currency_by_symbol",
    "mint",
    "mint_in_tx",
    "renounce",
    "renounce_in_tx",
    "request_delete",
    "request_delete_in_tx",
    "request_rate_change",
    "request_rate_change_in_tx",
    "require_currency",
    "set_delegation_in_tx",
    "validate_currency_name",
    "validate_currency_symbol",
    "validate_hourly_rate",
]

RATE_CHANGE_TIMELOCK = timedelta(days=7)
DELETE_TIMELOCK = timedelta(days=7)
DELETE_EXPIRY = timedelta(days=10)
_NAME_PATTERN = re.compile(CURRENCY_NAME_PATTERN)
_SYMBOL_PATTERN = re.compile(CURRENCY_SYMBOL_PATTERN)
_SYMBOL_CHARACTERS = frozenset(string.ascii_uppercase + "-")


def validate_currency_name(name: str) -> str:
    """Return name when it satisfies the currency table's name check (a letter first, a letter or digit last, at most CURRENCY_NAME_UNDERSCORES_MAX non-consecutive underscores as its only symbol) and CURRENCY_NAME_MAX; else raise InvalidArgument."""
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise InvalidArgument("Names must start with a letter, end with an alphanumeric character, and contain only alphanumeric characters and underscores.")
    if name.count("_") > CURRENCY_NAME_UNDERSCORES_MAX:
        raise InvalidArgument(f"Names can contain at most {CURRENCY_NAME_UNDERSCORES_MAX} underscores.")
    if "__" in name:
        raise InvalidArgument("Names cannot contain consecutive underscores.")
    if len(name) > CURRENCY_NAME_MAX:
        raise InvalidArgument(f"Names can be at most {CURRENCY_NAME_MAX} characters long.")
    return name


def validate_currency_symbol(symbol: str) -> str:
    """Return symbol when it satisfies the currency table's symbol check (uppercase letters joined by at most CURRENCY_SYMBOL_HYPHENS_MAX single hyphens) and CURRENCY_SYMBOL_MAX, else raise InvalidArgument naming the first broken rule (callers uppercase user input)."""
    if not isinstance(symbol, str) or not symbol or not set(symbol) <= _SYMBOL_CHARACTERS:
        raise InvalidArgument("Symbols must contain only letters (A-Z) and hyphens.")
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        if symbol.count("-") > CURRENCY_SYMBOL_HYPHENS_MAX:
            raise InvalidArgument(f"Symbols can contain at most {CURRENCY_SYMBOL_HYPHENS_MAX} hyphens.")
        if "--" in symbol:
            raise InvalidArgument("Symbols cannot contain consecutive hyphens.")
        raise InvalidArgument("Symbols cannot start or end with a hyphen.")
    if len(symbol) > CURRENCY_SYMBOL_MAX:
        raise InvalidArgument(f"Symbols can be at most {CURRENCY_SYMBOL_MAX} characters long.")
    return symbol


def validate_hourly_rate(rate_ppm: int) -> int:
    """Return rate_ppm when it is an int from 0 to RATE_PPM_MAX (100%/h), else raise InvalidArgument."""
    if isinstance(rate_ppm, bool) or not isinstance(rate_ppm, int) or not 0 <= rate_ppm <= RATE_PPM_MAX:
        raise InvalidArgument(f"The hourly interest rate must be an integer from 0 to {RATE_PPM_MAX} ppm (100%/h).")
    return rate_ppm


def delete_request_active(requested_at: datetime | None, now: datetime) -> bool:
    """Whether a delete request made at requested_at still counts at now; after DELETE_EXPIRY it counts as absent everywhere."""
    return requested_at is not None and now - requested_at <= DELETE_EXPIRY


@dataclass(frozen=True)
class CurrencyInfo:
    """A currency with its derived supply; delete_requested_at is None when no request exists or the request has lapsed, and delete_request_lapsed tells a lapsed request, which the next finalize attempt clears, from none."""

    currency_id: int
    name: str
    symbol: str
    issuance_account_id: int
    created_by: int
    created_at: datetime
    supply: int
    hourly_rate_ppm: int
    pending_rate_ppm: int | None
    rate_change_requested_at: datetime | None
    minting_renounced: bool
    delete_requested_at: datetime | None
    delete_request_lapsed: bool
    delegation_enabled: bool
    interest_halted: bool
    deleted_at: datetime | None

    @property
    def rate_change_apply_after(self) -> datetime | None:
        return None if self.rate_change_requested_at is None else self.rate_change_requested_at + RATE_CHANGE_TIMELOCK

    @property
    def delete_finalize_from(self) -> datetime | None:
        return None if self.delete_requested_at is None else self.delete_requested_at + DELETE_TIMELOCK

    @property
    def delete_finalize_until(self) -> datetime | None:
        return None if self.delete_requested_at is None else self.delete_requested_at + DELETE_EXPIRY


def _time_field(op: OperationResult, key: str) -> datetime:
    value = op.output[key]
    if not isinstance(value, str):
        raise KeyError(f"output field {key!r} is not a timestamp string")
    return datetime.fromisoformat(value)


def _optional_int(op: OperationResult, key: str) -> int | None:
    value = op.output.get(key)
    return int(value) if isinstance(value, str) else None


@dataclass(frozen=True)
class CurrencyCreated:
    """A created or revived currency and the transfer of its initial supply (None when it was 0); replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    name: str
    symbol: str
    hourly_rate_ppm: int
    initial_supply: int
    transfer_id: int | None
    revived: bool
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "currency_id": self.currency_id,
            "name": self.name,
            "symbol": self.symbol,
            "hourly_rate_ppm": self.hourly_rate_ppm,
            "initial_supply": self.initial_supply,
            "transfer_id": self.transfer_id,
            "revived": self.revived,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> CurrencyCreated:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            name=str(op.output["name"]),
            symbol=str(op.output["symbol"]),
            hourly_rate_ppm=op.int_field("hourly_rate_ppm"),
            initial_supply=op.int_field("initial_supply"),
            transfer_id=_optional_int(op, "transfer_id"),
            revived=op.output.get("revived") is True,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class SupplyChanged:
    """The result of a mint (issuance to account) or a burn (account to issuance); replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    account_id: int
    amount: int
    transfer_id: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"currency_id": self.currency_id, "account_id": self.account_id, "amount": self.amount, "transfer_id": self.transfer_id}

    @classmethod
    def from_operation(cls, op: OperationResult) -> SupplyChanged:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            account_id=op.int_field("account_id"),
            amount=op.int_field("amount"),
            transfer_id=op.int_field("transfer_id"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class MintingRenounced:
    """The result of renounce, which always changes the currency (a second renounce raises CurrencyRenounced); replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"currency_id": self.currency_id}

    @classmethod
    def from_operation(cls, op: OperationResult) -> MintingRenounced:
        return cls(execution_id=op.execution_id, currency_id=op.int_field("currency_id"), replayed=op.replayed)


@dataclass(frozen=True)
class DelegationChanged:
    """The result of a delegation toggle; changed is False when delegation already had the requested state, and replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    enabled: bool
    changed: bool
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"currency_id": self.currency_id, "enabled": self.enabled, "changed": self.changed}

    @classmethod
    def from_operation(cls, op: OperationResult) -> DelegationChanged:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            enabled=op.output.get("enabled") is True,
            changed=op.output.get("changed") is True,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class RateChangeRequested:
    """A scheduled rate change that can be applied from apply_after on; replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    new_rate_ppm: int
    requested_at: datetime
    apply_after: datetime
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"currency_id": self.currency_id, "new_rate_ppm": self.new_rate_ppm, "requested_at": self.requested_at, "apply_after": self.apply_after}

    @classmethod
    def from_operation(cls, op: OperationResult) -> RateChangeRequested:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            new_rate_ppm=op.int_field("new_rate_ppm"),
            requested_at=_time_field(op, "requested_at"),
            apply_after=_time_field(op, "apply_after"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class RateChangeApplied:
    """An applied rate change with the interest executions that accrued the stakes at the old rate; replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    old_rate_ppm: int
    new_rate_ppm: int
    stakes_accrued: int
    interest_execution_ids: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "currency_id": self.currency_id,
            "old_rate_ppm": self.old_rate_ppm,
            "new_rate_ppm": self.new_rate_ppm,
            "stakes_accrued": self.stakes_accrued,
            "interest_execution_ids": self.interest_execution_ids,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> RateChangeApplied:
        ids = op.output.get("interest_execution_ids")
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            old_rate_ppm=op.int_field("old_rate_ppm"),
            new_rate_ppm=op.int_field("new_rate_ppm"),
            stakes_accrued=op.int_field("stakes_accrued"),
            interest_execution_ids=tuple(int(value) for value in ids if isinstance(value, str)) if isinstance(ids, list) else (),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class DeleteRequested:
    """A delete request that can be finalized from finalize_from until finalize_until; replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    requested_at: datetime
    finalize_from: datetime
    finalize_until: datetime
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"currency_id": self.currency_id, "requested_at": self.requested_at, "finalize_from": self.finalize_from, "finalize_until": self.finalize_until}

    @classmethod
    def from_operation(cls, op: OperationResult) -> DeleteRequested:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            requested_at=_time_field(op, "requested_at"),
            finalize_from=_time_field(op, "finalize_from"),
            finalize_until=_time_field(op, "finalize_until"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class CurrencyDeleted:
    """The result of finalizing a deletion: what the cascade burned (pool custody balances of the currency included), canceled, removed and dissolved; replayed takes no part in equality."""

    execution_id: int
    currency_id: int
    burned_amount: int
    balances_burned: int
    stakes_burned: int
    claims_canceled: int
    allowances_deleted: int
    pools_dissolved: int = 0
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "currency_id": self.currency_id,
            "burned_amount": self.burned_amount,
            "balances_burned": self.balances_burned,
            "stakes_burned": self.stakes_burned,
            "claims_canceled": self.claims_canceled,
            "allowances_deleted": self.allowances_deleted,
            "pools_dissolved": self.pools_dissolved,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> CurrencyDeleted:
        return cls(
            execution_id=op.execution_id,
            currency_id=op.int_field("currency_id"),
            burned_amount=op.int_field("burned_amount"),
            balances_burned=op.int_field("balances_burned"),
            stakes_burned=op.int_field("stakes_burned"),
            claims_canceled=op.int_field("claims_canceled"),
            allowances_deleted=op.int_field("allowances_deleted"),
            pools_dissolved=op.int_field("pools_dissolved"),
            replayed=op.replayed,
        )


def _require_manager(guild: GuildSnapshot, row: RowMapping, user_id: int) -> None:
    """Allow the guild owner, or an Administrator while the locked row has delegation enabled."""
    require_currency_manager(guild, row["currency_id"], user_id, delegation_enabled=bool(row["delegation_enabled"]))


async def _lock_managed(tx: LedgerTx, guild: GuildSnapshot, currency_id: int, user_id: int) -> RowMapping:
    """Lock the live currency FOR UPDATE and check that user_id may manage it."""
    row = (await tx.lock_currencies([currency_id], for_update=True))[currency_id]
    _require_manager(guild, row, user_id)
    return row


async def _set(tx: LedgerTx, currency_id: int, **values: Any) -> None:
    await tx.conn.execute(update(currency).where(currency.c.currency_id == currency_id).values(**values))


async def _run_managed(
    engine: AsyncEngine,
    *,
    type: ExecutionType,
    user_id: int,
    input: Mapping[str, Any],
    body: Body,
    now: datetime | None,
    idempotency_key: str | None,
    guild: GuildResolver | None = None,
    on_failure: FailureHook | None = None,
) -> OperationResult:
    """Run a currency operation initiated by user_id, resolving the guild snapshot only when no idempotent replay answers the request."""
    before = None if guild is None else guild.resolve
    return await run_operation(engine, type=type, initiator_id=user_id, input=input, body=body, now=now, idempotency_key=idempotency_key, on_failure=on_failure, before=before)


async def create_currency_in_tx(
    tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, guild_id: int, user_id: int, name: str, symbol: str, initial_supply: int, hourly_rate_ppm: int = 0
) -> CurrencyCreated:
    """Create the guild's currency (only its owner may) and mint initial_supply to the creator; a soft-deleted currency of the guild is revived in place with fresh attributes and its flags and requests reset."""
    validate_currency_name(name)
    validate_currency_symbol(symbol)
    validate_hourly_rate(hourly_rate_ppm)
    validate_non_negative_amount(initial_supply, "The initial supply must be a non-negative integer number of minimal units.")
    validate_guild_id(guild_id)
    validate_user_account_id(user_id)
    require_guild_owner(guild, guild_id, user_id)
    existing = (await tx.conn.execute(select(currency).where(currency.c.currency_id == guild_id).with_for_update())).mappings().one_or_none()
    if existing is not None and existing["deleted_at"] is None:
        raise AlreadyExists("A currency already exists for this server.")
    taken = (await tx.conn.execute(select(currency.c.currency_id).where(currency.c.active_symbol == symbol))).first()
    if taken is not None:
        raise AlreadyExists(f"The symbol {symbol} is already in use.")
    await tx.require_accounts([user_id])
    issuance = await get_or_create_issuance_account(tx.conn, guild_id, now=tx.now)
    attributes: dict[str, Any] = {
        "name": name,
        "symbol": symbol,
        "created_by": user_id,
        "created_at": tx.now,
        "minting_renounced": False,
        "hourly_rate_ppm": hourly_rate_ppm,
        "pending_rate_ppm": None,
        "rate_change_requested_at": None,
        "delete_requested_at": None,
        "delegation_enabled": False,
        "interest_halted": False,
        "deleted_at": None,
    }
    try:
        if existing is None:
            await tx.conn.execute(insert(currency).values(currency_id=guild_id, issuance_account_id=issuance, **attributes))
        else:
            await _set(tx, guild_id, **attributes)
    except IntegrityError as exc:
        # A concurrent create took the same guild or symbol; MySQL rolled back only the failed statement.
        if mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
            raise AlreadyExists("A currency already exists for this server, or the symbol is already in use.") from exc
        raise
    transfer_id: int | None = None
    if initial_supply > 0:
        await tx.lock_currencies([guild_id], for_update=True)
        await tx.lock_balances([(issuance, guild_id), (user_id, guild_id)])
        transfer_id = await tx.move(execution_id, guild_id, issuance, user_id, initial_supply, TransferKind.PRINCIPAL)
    return CurrencyCreated(
        execution_id=execution_id,
        currency_id=guild_id,
        name=name,
        symbol=symbol,
        hourly_rate_ppm=hourly_rate_ppm,
        initial_supply=initial_supply,
        transfer_id=transfer_id,
        revived=existing is not None,
    )


async def create_currency(
    engine: AsyncEngine,
    guild_info: GuildInfo,
    *,
    guild_id: int,
    user_id: int,
    name: str,
    symbol: str,
    initial_supply: int,
    hourly_rate_ppm: int = 0,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> CurrencyCreated:
    """Create the currency of guild_id as a currency_create execution by the guild owner, minting initial_supply (may be 0) to them."""
    require_storable_int(initial_supply)
    require_storable_int(hourly_rate_ppm)
    require_storable_int(guild_id)
    if not isinstance(name, str) or not isinstance(symbol, str):
        raise InvalidArgument("The name and symbol must be strings.")
    guild = GuildResolver(guild_info, guild_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await create_currency_in_tx(
            tx, execution_id, guild.snapshot, guild_id=guild_id, user_id=user_id, name=name, symbol=symbol, initial_supply=initial_supply, hourly_rate_ppm=hourly_rate_ppm
        )
        return result.to_output()

    op = await _run_managed(
        engine,
        type=ExecutionType.CURRENCY_CREATE,
        user_id=user_id,
        guild=guild,
        input={"guild_id": guild_id, "name": name, "symbol": symbol, "initial_supply": initial_supply, "hourly_rate_ppm": hourly_rate_ppm},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return CurrencyCreated.from_operation(op)


async def mint_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int, amount: int) -> SupplyChanged:
    """Mint amount into the acting manager's wallet; raises CurrencyRenounced after renounce and AmountOverflow when the supply would exceed AMOUNT_MAX."""
    validate_positive_amount(amount)
    validate_user_account_id(user_id)
    row = await _lock_managed(tx, guild, currency_id, user_id)
    if row["minting_renounced"]:
        raise CurrencyRenounced("Minting capability has been renounced for this currency.")
    issuance = row["issuance_account_id"]
    balances = await tx.lock_balances([(issuance, currency_id), (user_id, currency_id)])
    if -balances[(issuance, currency_id)] + amount > AMOUNT_MAX:
        raise AmountOverflow("Minting this amount would exceed the maximum supply.")
    transfer_id = await tx.move(execution_id, currency_id, issuance, user_id, amount, TransferKind.PRINCIPAL)
    return SupplyChanged(execution_id=execution_id, currency_id=currency_id, account_id=user_id, amount=amount, transfer_id=transfer_id)


async def burn_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, currency_id: int, amount: int) -> SupplyChanged:
    """Burn amount of the holder's own balance back into the issuance account; needs no management rights and works whether or not the bot is in the guild."""
    validate_positive_amount(amount)
    validate_user_account_id(account_id)
    row = (await tx.lock_currencies([currency_id]))[currency_id]
    issuance = row["issuance_account_id"]
    await tx.lock_balances([(issuance, currency_id), (account_id, currency_id)])
    transfer_id = await tx.move(execution_id, currency_id, account_id, issuance, amount, TransferKind.PRINCIPAL)
    return SupplyChanged(execution_id=execution_id, currency_id=currency_id, account_id=account_id, amount=amount, transfer_id=transfer_id)


async def mint(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, amount: int, now: datetime | None = None, idempotency_key: str | None = None) -> SupplyChanged:
    """Mint amount into the acting manager's wallet as a mint execution."""
    require_storable_int(amount)
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await mint_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id, amount=amount)).to_output()

    op = await _run_managed(engine, type=ExecutionType.MINT, user_id=user_id, guild=guild, input={"currency_id": currency_id, "amount": amount}, body=body, now=now, idempotency_key=idempotency_key)
    return SupplyChanged.from_operation(op)


async def burn(engine: AsyncEngine, *, account_id: int, currency_id: int, amount: int, now: datetime | None = None, idempotency_key: str | None = None) -> SupplyChanged:
    """Burn amount of the account's own balance as a burn execution."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await burn_in_tx(tx, execution_id, account_id=account_id, currency_id=currency_id, amount=amount)).to_output()

    op = await _run_managed(engine, type=ExecutionType.BURN, user_id=account_id, input={"currency_id": currency_id, "amount": amount}, body=body, now=now, idempotency_key=idempotency_key)
    return SupplyChanged.from_operation(op)


async def renounce_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int) -> MintingRenounced:
    """Permanently give up minting and rate changes, dropping any pending rate change; raises CurrencyRenounced when already renounced."""
    row = await _lock_managed(tx, guild, currency_id, user_id)
    if row["minting_renounced"]:
        raise CurrencyRenounced("Minting and rate changes have already been renounced.")
    await _set(tx, currency_id, minting_renounced=True, pending_rate_ppm=None, rate_change_requested_at=None)
    return MintingRenounced(execution_id=execution_id, currency_id=currency_id)


async def renounce(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> MintingRenounced:
    """Renounce minting and rate changes as a currency_renounce execution."""
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await renounce_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id)).to_output()

    op = await _run_managed(engine, type=ExecutionType.CURRENCY_RENOUNCE, user_id=user_id, guild=guild, input={"currency_id": currency_id}, body=body, now=now, idempotency_key=idempotency_key)
    return MintingRenounced.from_operation(op)


async def request_rate_change_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int, new_rate_ppm: int) -> RateChangeRequested:
    """Schedule a new hourly rate that can be applied from RATE_CHANGE_TIMELOCK on and never expires; only one request may be pending and a renounced currency refuses."""
    validate_hourly_rate(new_rate_ppm)
    row = await _lock_managed(tx, guild, currency_id, user_id)
    if row["minting_renounced"]:
        raise CurrencyRenounced("Rate changes have been renounced for this currency.")
    if row["rate_change_requested_at"] is not None:
        raise InvalidState("An interest rate change is already pending.")
    await _set(tx, currency_id, pending_rate_ppm=new_rate_ppm, rate_change_requested_at=tx.now)
    return RateChangeRequested(execution_id=execution_id, currency_id=currency_id, new_rate_ppm=new_rate_ppm, requested_at=tx.now, apply_after=tx.now + RATE_CHANGE_TIMELOCK)


async def request_rate_change(
    engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, new_rate_ppm: int, now: datetime | None = None, idempotency_key: str | None = None
) -> RateChangeRequested:
    """Request an hourly rate change as a rate_change_request execution."""
    require_storable_int(new_rate_ppm)
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await request_rate_change_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id, new_rate_ppm=new_rate_ppm)).to_output()

    op = await _run_managed(
        engine,
        type=ExecutionType.RATE_CHANGE_REQUEST,
        user_id=user_id,
        guild=guild,
        input={"currency_id": currency_id, "new_rate_ppm": new_rate_ppm},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return RateChangeRequested.from_operation(op)


async def apply_rate_change_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int) -> RateChangeApplied:
    """Accrue every stake of the currency at the old rate up to now, then switch to the pending rate and clear the request; refused before RATE_CHANGE_TIMELOCK and after renounce."""
    row = await _lock_managed(tx, guild, currency_id, user_id)
    if row["minting_renounced"]:
        raise CurrencyRenounced("Rate changes have been renounced for this currency.")
    requested_at, new_rate = row["rate_change_requested_at"], row["pending_rate_ppm"]
    if requested_at is None or new_rate is None:
        raise NoPendingRequest("No interest rate change is pending.")
    if tx.now - requested_at < RATE_CHANGE_TIMELOCK:
        raise TimelockActive(f"The timelock period for the interest rate change has not passed yet. It can be applied from {(requested_at + RATE_CHANGE_TIMELOCK).isoformat()}.")
    accruals = await accrue_currency_stakes_in_tx(tx, execution_id, currency_id=currency_id, trigger=AccrualTrigger.RATE_APPLY)
    await _set(tx, currency_id, hourly_rate_ppm=new_rate, pending_rate_ppm=None, rate_change_requested_at=None)
    return RateChangeApplied(
        execution_id=execution_id,
        currency_id=currency_id,
        old_rate_ppm=row["hourly_rate_ppm"],
        new_rate_ppm=new_rate,
        stakes_accrued=len(accruals),
        interest_execution_ids=tuple(accrual.interest_execution_id for accrual in accruals if accrual.interest_execution_id is not None),
    )


async def apply_rate_change(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> RateChangeApplied:
    """Apply the pending rate change as a rate_change_apply execution; the interest accrued at the old rate is recorded as separate interest executions in the same transaction."""
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await apply_rate_change_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id)).to_output()

    op = await _run_managed(engine, type=ExecutionType.RATE_CHANGE_APPLY, user_id=user_id, guild=guild, input={"currency_id": currency_id}, body=body, now=now, idempotency_key=idempotency_key)
    return RateChangeApplied.from_operation(op)


async def request_delete_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int) -> DeleteRequested:
    """Request deletion, finalizable from DELETE_TIMELOCK to DELETE_EXPIRY later; a lapsed request is replaced and an active one raises InvalidState."""
    row = await _lock_managed(tx, guild, currency_id, user_id)
    if delete_request_active(row["delete_requested_at"], tx.now):
        raise InvalidState("A delete request is already pending.")
    await _set(tx, currency_id, delete_requested_at=tx.now)
    return DeleteRequested(execution_id=execution_id, currency_id=currency_id, requested_at=tx.now, finalize_from=tx.now + DELETE_TIMELOCK, finalize_until=tx.now + DELETE_EXPIRY)


async def request_delete(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> DeleteRequested:
    """Request deletion of the currency as a currency_delete_request execution."""
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await request_delete_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id)).to_output()

    op = await _run_managed(engine, type=ExecutionType.CURRENCY_DELETE_REQUEST, user_id=user_id, guild=guild, input={"currency_id": currency_id}, body=body, now=now, idempotency_key=idempotency_key)
    return DeleteRequested.from_operation(op)


async def finalize_delete_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int) -> CurrencyDeleted:
    """Run the deletion cascade and soft-delete the currency: dissolve its pools, cancel its pending claims, delete its allowances, and burn every balance and stake into the issuance account.

    Locks follow the ledger order: the currency FOR UPDATE and the pools' partner currencies FOR SHARE in ascending ID order, then claims, allowances, stakes and pool rows, then every balance in one call.
    A partner that appears under the lock but is not locked raises RerunTransaction, which reruns the whole transaction with every known partner pre-locked, while tx.rerun_allowed, and InvalidState otherwise.
    It is not callable from run_child(), because the cascade must lock its currencies before any balance.
    """
    dissolver = get_pool_dissolver()
    partners = await dissolver.partner_currencies(tx.conn, currency_id) - {currency_id}
    rows: dict[int, RowMapping] = {}
    for locked_id in sorted(partners | {currency_id}):
        deleted = locked_id == currency_id
        rows.update(await tx.lock_currencies([locked_id], for_update=deleted, include_deleted=not deleted))
    row = rows[currency_id]
    _require_manager(guild, row, user_id)
    requested_at = row["delete_requested_at"]
    if requested_at is None:
        raise NoPendingRequest("Delete request has not been initiated.")
    elapsed = tx.now - requested_at
    if elapsed < DELETE_TIMELOCK:
        remaining = int((DELETE_TIMELOCK - elapsed).total_seconds())
        raise TimelockActive(f"Time lock not expired. {remaining} seconds remaining.")
    if elapsed > DELETE_EXPIRY:
        raise RequestExpired("Delete request expired.")
    current = await dissolver.partner_currencies(tx.conn, currency_id) - {currency_id}
    if any(tx.currency_lock(partner) is None for partner in current):
        if tx.rerun_allowed:
            raise RerunTransaction(f"currency {currency_id} gained a pool partner during deletion", exclusive={currency_id}, pre_lock={currency_id, *partners, *current})
        raise InvalidState("The pools of this currency changed while it was being deleted; try again.")

    claims_canceled = await claims.cancel_pending_claims_for_currency(tx, execution_id, currency_id=currency_id)
    allowances_deleted = await allowances.delete_allowances_for_currency(tx, execution_id, currency_id=currency_id)
    stake_rows = (await tx.conn.execute(select(stake.c.account_id).where(stake.c.currency_id == currency_id).order_by(stake.c.account_id).with_for_update())).all()
    pool_keys = await dissolver.balance_keys(tx, currency_id)
    issuance = row["issuance_account_id"]
    holders = {held.account_id for held in await tx.conn.execute(select(balance.c.account_id).where(balance.c.currency_id == currency_id, balance.c.account_id != issuance))}
    holders |= {account_id for account_id, key_currency in pool_keys if key_currency == currency_id and account_id != issuance}
    await tx.lock_balances({(issuance, currency_id), *((account_id, currency_id) for account_id in holders), *pool_keys})

    dissolution = await dissolver.dissolve_pools_for_currency(tx, execution_id, currency_id)
    balances_burned, burned_amount = dissolution.balances_burned, dissolution.burned_amount
    for account_id in sorted(holders):
        amount = tx.balance(account_id, currency_id)
        if amount > 0:
            await tx.move(execution_id, currency_id, account_id, issuance, amount, TransferKind.PRINCIPAL)
            balances_burned += 1
            burned_amount += amount
    await tx.conn.execute(delete(stake).where(stake.c.currency_id == currency_id))
    await _set(tx, currency_id, deleted_at=tx.now, delete_requested_at=None, pending_rate_ppm=None, rate_change_requested_at=None)
    return CurrencyDeleted(
        execution_id=execution_id,
        currency_id=currency_id,
        burned_amount=burned_amount,
        balances_burned=balances_burned,
        stakes_burned=len(stake_rows),
        claims_canceled=claims_canceled,
        allowances_deleted=allowances_deleted,
        pools_dissolved=dissolution.pools_dissolved,
    )


async def finalize_delete(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> CurrencyDeleted:
    """Finalize deletion as one currency_delete_finalize execution.

    A lapsed request raises RequestExpired, and the failure hook clears it in the transaction that records the failed execution, which the body's savepoint rollback does not undo.
    When a pool gains an unlocked partner currency mid-run, the transaction is rolled back with nothing recorded and rerun, up to MAX_RERUNS times, and the last run fails with InvalidState.
    """
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def on_failure(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        if isinstance(error, RequestExpired):
            await tx.conn.execute(update(currency).where(and_(currency.c.currency_id == currency_id, currency.c.delete_requested_at < tx.now - DELETE_EXPIRY)).values(delete_requested_at=None))

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await finalize_delete_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id)).to_output()

    op = await _run_managed(
        engine,
        type=ExecutionType.CURRENCY_DELETE_FINALIZE,
        user_id=user_id,
        guild=guild,
        input={"currency_id": currency_id},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
        on_failure=on_failure,
    )
    return CurrencyDeleted.from_operation(op)


async def set_delegation_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, currency_id: int, user_id: int, enabled: bool) -> DelegationChanged:
    """Turn management delegation to Administrators on or off; only the guild owner may do this."""
    row = (await tx.lock_currencies([currency_id], for_update=True))[currency_id]
    require_guild_owner(guild, currency_id, user_id)
    changed = bool(row["delegation_enabled"]) != enabled
    if changed:
        await _set(tx, currency_id, delegation_enabled=enabled)
    return DelegationChanged(execution_id=execution_id, currency_id=currency_id, enabled=enabled, changed=changed)


async def _set_delegation(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, enabled: bool, now: datetime | None, idempotency_key: str | None) -> DelegationChanged:
    guild = GuildResolver(guild_info, currency_id, user_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await set_delegation_in_tx(tx, execution_id, guild.snapshot, currency_id=currency_id, user_id=user_id, enabled=enabled)).to_output()

    op = await _run_managed(
        engine,
        type=ExecutionType.DELEGATION_ENABLE if enabled else ExecutionType.DELEGATION_DISABLE,
        user_id=user_id,
        guild=guild,
        input={"currency_id": currency_id},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return DelegationChanged.from_operation(op)


async def enable_delegation(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> DelegationChanged:
    """Let Administrators of the guild manage the currency, as a delegation_enable execution by the owner."""
    return await _set_delegation(engine, guild_info, currency_id=currency_id, user_id=user_id, enabled=True, now=now, idempotency_key=idempotency_key)


async def disable_delegation(engine: AsyncEngine, guild_info: GuildInfo, *, currency_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> DelegationChanged:
    """Return management to the owner alone, as a delegation_disable execution by the owner."""
    return await _set_delegation(engine, guild_info, currency_id=currency_id, user_id=user_id, enabled=False, now=now, idempotency_key=idempotency_key)


async def _read_currency(db: Db, condition: Any, *, include_deleted: bool, now: datetime | None) -> CurrencyInfo | None:
    stmt = (
        select(currency, balance.c.amount.label("issuance_balance"))
        .select_from(currency.outerjoin(balance, and_(balance.c.account_id == currency.c.issuance_account_id, balance.c.currency_id == currency.c.currency_id)))
        .where(condition)
    )
    if not include_deleted:
        stmt = stmt.where(currency.c.deleted_at.is_(None))
    async with reader(db) as conn:
        row = (await conn.execute(stmt)).mappings().first()
    if row is None:
        return None
    at = resolve_now(now)
    requested = row["delete_requested_at"]
    active = delete_request_active(requested, at)
    return CurrencyInfo(
        currency_id=row["currency_id"],
        name=row["name"],
        symbol=row["symbol"],
        issuance_account_id=row["issuance_account_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        supply=-int(row["issuance_balance"] or 0),
        hourly_rate_ppm=row["hourly_rate_ppm"],
        pending_rate_ppm=row["pending_rate_ppm"],
        rate_change_requested_at=row["rate_change_requested_at"],
        minting_renounced=bool(row["minting_renounced"]),
        delete_requested_at=requested if active else None,
        delete_request_lapsed=requested is not None and not active,
        delegation_enabled=bool(row["delegation_enabled"]),
        interest_halted=bool(row["interest_halted"]),
        deleted_at=row["deleted_at"],
    )


async def get_currency(db: Db, currency_id: int, *, include_deleted: bool = False, now: datetime | None = None) -> CurrencyInfo | None:
    """The currency with this ID, or None; deleted currencies only with include_deleted, and now decides whether a delete request has lapsed."""
    return await _read_currency(db, currency.c.currency_id == currency_id, include_deleted=include_deleted, now=now)


async def get_currency_by_symbol(db: Db, symbol: str, *, now: datetime | None = None) -> CurrencyInfo | None:
    """The live currency with this symbol (exact match), or None."""
    return await _read_currency(db, currency.c.active_symbol == symbol, include_deleted=False, now=now)


async def require_currency(db: Db, currency_id: int, *, now: datetime | None = None) -> CurrencyInfo:
    """get_currency for a live currency, raising CurrencyNotFound when there is none."""
    info = await get_currency(db, currency_id, now=now)
    if info is None:
        raise CurrencyNotFound(f"Currency {currency_id} was not found.")
    return info
