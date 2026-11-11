"""DEX pools: creation, adding and removing liquidity, the pool reads, the lock helpers swaps build on, and the dissolver the currency deletion cascade calls, registered process-wide when this module is imported.

A pool is identified by its ordered currency pair (currency_a < currency_b) and fee tier, callers name the pair in either order as currency_x and currency_y, and results report both forms.
Lock rule: both pool currencies (ascending, through LedgerTx.lock_currencies()) come first, then pool rows FOR UPDATE in ascending pool_id, then pool_share rows FOR UPDATE in (pool_id, account_id) order, then every balance in one lock_balances() call.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Self

from sqlalchemy import ColumnElement, Row, RowMapping, Select, delete, insert, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.accounts import create_pool_custody_account, pool_custody_account_id
from rapidwire.core.db import Db, reader
from rapidwire.core.errors import AmountOverflow, InsufficientLiquidity, InsufficientShares, InvalidArgument, PoolAlreadyExists, PoolNotFound, SlippageExceeded
from rapidwire.core.hooks import PoolDissolution, register_pool_dissolver
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, BalanceKey, LedgerTx, LedgerUsageError, OperationResult, mysql_error_code, run_operation
from rapidwire.core.paging import POOLS_PER_PAGE_DEFAULT, POOLS_PER_PAGE_MAX, POSITIONS_PER_PAGE_DEFAULT, POSITIONS_PER_PAGE_MAX, Page, read_page
from rapidwire.core.routing import PoolState
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.core.validation import (
    require_int_in_range,
    require_storable_int,
    validate_bigint_id,
    validate_non_negative_amount,
    validate_positive_amount,
    validate_positive_id,
    validate_user_account_id,
)
from rapidwire.db.tables import FEE_TIERS_BPS, MINIMUM_LIQUIDITY, account, currency, pool, pool_share
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "POOLS_PER_PAGE_DEFAULT",
    "POOLS_PER_PAGE_MAX",
    "POOL_DISSOLVER",
    "POSITIONS_PER_PAGE_DEFAULT",
    "POSITIONS_PER_PAGE_MAX",
    "DexPoolDissolver",
    "LiquidityAdded",
    "LiquidityChange",
    "LiquidityRemoved",
    "LockedPool",
    "PoolCreated",
    "PoolInfo",
    "PoolPair",
    "ProviderPosition",
    "add_liquidity",
    "add_liquidity_in_tx",
    "canonical_pair",
    "create_pool",
    "create_pool_in_tx",
    "custody_account_id",
    "deposit_shares",
    "get_pool",
    "get_pools_for_pair",
    "initial_shares",
    "list_pools",
    "list_provider_positions",
    "load_active_pools",
    "lock_active_pool",
    "lock_pools",
    "optimal_deposit",
    "pro_rata_payouts",
    "remove_liquidity",
    "remove_liquidity_in_tx",
    "set_reserves",
    "share_percent",
    "validate_fee_tier",
    "withdrawal_amounts",
]

_PERCENT_SCALE = 10_000


def validate_fee_tier(fee_bps: object) -> int:
    """Return fee_bps when it is one of FEE_TIERS_BPS, else raise InvalidArgument."""
    if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or fee_bps not in FEE_TIERS_BPS:
        tiers = ", ".join(str(tier) for tier in FEE_TIERS_BPS)
        raise InvalidArgument(f"The fee tier must be one of {tiers} basis points.")
    return fee_bps


@dataclass(frozen=True)
class PoolPair:
    """A caller's currency pair mapped onto the pool's ordered pair; swapped is True when currency_x is the pool's currency_b."""

    currency_x: int
    currency_y: int

    @property
    def swapped(self) -> bool:
        return self.currency_x > self.currency_y

    @property
    def currency_a(self) -> int:
        return min(self.currency_x, self.currency_y)

    @property
    def currency_b(self) -> int:
        return max(self.currency_x, self.currency_y)

    def to_ab(self, value_x: int, value_y: int) -> tuple[int, int]:
        """Reorder a pair of values given for (x, y) into (a, b)."""
        return (value_y, value_x) if self.swapped else (value_x, value_y)

    def to_xy(self, value_a: int, value_b: int) -> tuple[int, int]:
        """Reorder a pair of values given for (a, b) into (x, y)."""
        return (value_b, value_a) if self.swapped else (value_a, value_b)


def canonical_pair(currency_x: object, currency_y: object) -> PoolPair:
    """Return the pair when both are currency IDs (positive BIGINTs) and differ, else raise InvalidArgument."""
    x = validate_positive_id("currency_x", currency_x)
    y = validate_positive_id("currency_y", currency_y)
    if x == y:
        raise InvalidArgument("A pool needs two different currencies.")
    return PoolPair(x, y)


def initial_shares(amount_a: int, amount_b: int) -> int:
    """The total shares minted by a pool's first deposit: floor(sqrt(amount_a * amount_b))."""
    return math.isqrt(amount_a * amount_b)


def optimal_deposit(desired_a: int, desired_b: int, reserve_a: int, reserve_b: int) -> tuple[int, int]:
    """The amounts a deposit uses at the pool's price: (desired_a, floor(desired_a * reserve_b / reserve_a)) when that fits desired_b, else (floor(desired_b * reserve_a / reserve_b), desired_b)."""
    if reserve_a <= 0 or reserve_b <= 0:
        raise ValueError("an active pool has positive reserves")
    optimal_b = desired_a * reserve_b // reserve_a
    if optimal_b <= desired_b:
        return desired_a, optimal_b
    return desired_b * reserve_a // reserve_b, desired_b


def deposit_shares(amount_a: int, amount_b: int, reserve_a: int, reserve_b: int, total_shares: int) -> int:
    """The shares a deposit mints: min(floor(amount_a * total / reserve_a), floor(amount_b * total / reserve_b))."""
    if reserve_a <= 0 or reserve_b <= 0:
        raise ValueError("an active pool has positive reserves")
    return min(amount_a * total_shares // reserve_a, amount_b * total_shares // reserve_b)


def withdrawal_amounts(shares: int, reserve_a: int, reserve_b: int, total_shares: int) -> tuple[int, int]:
    """The amounts burning shares returns: floor(shares * reserve / total) of each currency."""
    if total_shares <= 0:
        raise ValueError("total_shares must be positive")
    return shares * reserve_a // total_shares, shares * reserve_b // total_shares


def pro_rata_payouts(amount: int, holdings: Mapping[int, int]) -> dict[int, int]:
    """Split amount among account IDs in proportion to their positive holdings, each part floored, and add the remainder to the largest holder (the lowest account ID among equals); empty holdings yield {}."""
    if amount < 0 or any(value <= 0 for value in holdings.values()):
        raise ValueError("amount must be non-negative and every holding positive")
    if not holdings:
        return {}
    total = sum(holdings.values())
    payouts = {account_id: amount * holdings[account_id] // total for account_id in sorted(holdings)}
    largest = min(holdings, key=lambda account_id: (-holdings[account_id], account_id))
    payouts[largest] += amount - sum(payouts.values())
    return payouts


def share_percent(shares: int, total_shares: int) -> str:
    """shares / total_shares as a percentage with two decimals rounded half to even in exact integer arithmetic, the same digits as formatting the exact ratio times 100 with ":.2f" (2/3 gives "66.67")."""
    if total_shares <= 0 or shares < 0:
        raise ValueError("total_shares must be positive and shares non-negative")
    basis, remainder = divmod(shares * _PERCENT_SCALE, total_shares)
    if 2 * remainder > total_shares or (2 * remainder == total_shares and basis % 2 == 1):
        basis += 1
    return f"{basis // 100}.{basis % 100:02d}"


def _int_tuple(value: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in value if isinstance(item, str)) if isinstance(value, list) else ()


@dataclass(frozen=True, kw_only=True)
class LiquidityChange:
    """A completed change of a pool's liquidity: the a/b amounts moved, the shares minted or burned, and the provider's shares, pool total and reserves afterwards; replayed takes no part in equality."""

    execution_id: int
    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    currency_x: int
    currency_y: int
    amount_a: int
    amount_b: int
    shares: int
    provider_shares: int
    total_shares: int
    reserve_a: int
    reserve_b: int
    transfer_ids: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    @property
    def amount_x(self) -> int:
        return self.amount_a if self.currency_x == self.currency_a else self.amount_b

    @property
    def amount_y(self) -> int:
        return self.amount_a if self.currency_y == self.currency_a else self.amount_b

    def to_output(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "currency_a": self.currency_a,
            "currency_b": self.currency_b,
            "fee_bps": self.fee_bps,
            "currency_x": self.currency_x,
            "currency_y": self.currency_y,
            "amount_a": self.amount_a,
            "amount_b": self.amount_b,
            "amount_x": self.amount_x,
            "amount_y": self.amount_y,
            "shares": self.shares,
            "provider_shares": self.provider_shares,
            "total_shares": self.total_shares,
            "reserve_a": self.reserve_a,
            "reserve_b": self.reserve_b,
            "transfer_ids": self.transfer_ids,
        }

    @classmethod
    def _fields(cls, op: OperationResult) -> dict[str, Any]:
        names = ("pool_id", "currency_a", "currency_b", "fee_bps", "currency_x", "currency_y", "amount_a", "amount_b", "shares", "provider_shares", "total_shares", "reserve_a", "reserve_b")
        values: dict[str, Any] = {name: op.int_field(name) for name in names}
        return {"execution_id": op.execution_id, "transfer_ids": _int_tuple(op.output.get("transfer_ids")), "replayed": op.replayed, **values}

    @classmethod
    def from_operation(cls, op: OperationResult) -> Self:
        return cls(**cls._fields(op))


@dataclass(frozen=True, kw_only=True)
class PoolCreated(LiquidityChange):
    """A created pool: shares is what the creator received (the initial shares minus the locked MINIMUM_LIQUIDITY) and custody_account_id holds the reserves."""

    locked_shares: int
    custody_account_id: int

    def to_output(self) -> dict[str, Any]:
        return {**super().to_output(), "locked_shares": self.locked_shares, "custody_account_id": self.custody_account_id}

    @classmethod
    def _fields(cls, op: OperationResult) -> dict[str, Any]:
        return {**super()._fields(op), "locked_shares": op.int_field("locked_shares"), "custody_account_id": op.int_field("custody_account_id")}


@dataclass(frozen=True, kw_only=True)
class LiquidityAdded(LiquidityChange):
    """A deposit into an existing pool; shares is what it minted."""


@dataclass(frozen=True, kw_only=True)
class LiquidityRemoved(LiquidityChange):
    """A withdrawal from a pool; shares is what it burned and amount_a/amount_b what it paid out."""


@dataclass(frozen=True)
class LockedPool:
    """A pool row as read under its FOR UPDATE lock, with its custody account."""

    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    reserve_a: int
    reserve_b: int
    total_shares: int
    locked_shares: int
    custody_account_id: int
    dissolved_at: datetime | None

    @property
    def active(self) -> bool:
        return self.dissolved_at is None

    def state(self) -> PoolState:
        return PoolState(pool_id=self.pool_id, currency_a=self.currency_a, currency_b=self.currency_b, fee_bps=self.fee_bps, reserve_a=self.reserve_a, reserve_b=self.reserve_b)

    def other_currency(self, currency_id: int) -> int:
        if currency_id == self.currency_a:
            return self.currency_b
        if currency_id == self.currency_b:
            return self.currency_a
        raise ValueError(f"currency {currency_id} is not in pool {self.pool_id}")


def _contains(currency_id: int) -> ColumnElement[bool]:
    return or_(pool.c.currency_a == currency_id, pool.c.currency_b == currency_id)


# The custody account lookup of accounts, under the name the DEX uses: custody_account_id(conn, pool_id) -> int | None.
custody_account_id = pool_custody_account_id


def _locked_pool(row: RowMapping, custody: int) -> LockedPool:
    return LockedPool(
        pool_id=row["pool_id"],
        currency_a=row["currency_a"],
        currency_b=row["currency_b"],
        fee_bps=row["fee_bps"],
        reserve_a=int(row["reserve_a"]),
        reserve_b=int(row["reserve_b"]),
        total_shares=int(row["total_shares"]),
        locked_shares=int(row["locked_shares"]),
        custody_account_id=custody,
        dissolved_at=row["dissolved_at"],
    )


async def _require_custody(conn: AsyncConnection, pool_id: int) -> int:
    custody = await pool_custody_account_id(conn, pool_id)
    if custody is None:
        raise LedgerUsageError(f"pool {pool_id} has no custody account")
    return custody


async def load_active_pools(db: Db) -> list[PoolState]:
    """Every active pool in ascending pool_id, read without locks, for quoting and for choosing which pools a swap must lock."""
    stmt = select(pool.c.pool_id, pool.c.currency_a, pool.c.currency_b, pool.c.fee_bps, pool.c.reserve_a, pool.c.reserve_b).where(pool.c.active == true()).order_by(pool.c.pool_id)
    async with reader(db) as conn:
        rows = (await conn.execute(stmt)).all()
    return [PoolState(pool_id=row.pool_id, currency_a=row.currency_a, currency_b=row.currency_b, fee_bps=row.fee_bps, reserve_a=int(row.reserve_a), reserve_b=int(row.reserve_b)) for row in rows]


async def lock_pools(tx: LedgerTx, pool_ids: Iterable[int]) -> dict[int, LockedPool]:
    """Lock the pool rows FOR UPDATE in ascending pool_id and return the ones that exist, dissolved pools included (check LockedPool.active).

    Both currencies of every requested pool must already be locked by tx.lock_currencies(), and balances must be locked only afterwards; a pool whose currencies are not locked raises LedgerUsageError before any pool row is locked.
    """
    ids = sorted(set(pool_ids))
    if not ids:
        return {}
    # A pool's currencies never change, so an unlocked read can check them before the FOR UPDATE.
    for row in await tx.conn.execute(select(pool.c.pool_id, pool.c.currency_a, pool.c.currency_b).where(pool.c.pool_id.in_(ids))):
        for currency_id in (row.currency_a, row.currency_b):
            if tx.currency_lock(currency_id) is None:
                raise LedgerUsageError(f"currency {currency_id} of pool {row.pool_id} must be locked with lock_currencies() before the pool")
    rows = (await tx.conn.execute(select(pool).where(pool.c.pool_id.in_(ids)).order_by(pool.c.pool_id).with_for_update())).mappings().all()
    return {row["pool_id"]: _locked_pool(row, await _require_custody(tx.conn, row["pool_id"])) for row in rows}


async def _active_pool_id(conn: AsyncConnection, currency_a: int, currency_b: int, fee_bps: int) -> int | None:
    stmt = select(pool.c.pool_id).where(pool.c.currency_a == currency_a, pool.c.currency_b == currency_b, pool.c.fee_bps == fee_bps, pool.c.dissolved_at.is_(None))
    return (await conn.execute(stmt)).scalar_one_or_none()


async def lock_active_pool(tx: LedgerTx, pair: PoolPair, fee_bps: int) -> LockedPool:
    """Lock the active pool of the pair and fee tier FOR UPDATE, after the pair's currencies; raises PoolNotFound when there is none."""
    pool_id = await _active_pool_id(tx.conn, pair.currency_a, pair.currency_b, fee_bps)
    locked = None if pool_id is None else (await lock_pools(tx, [pool_id])).get(pool_id)
    if locked is None or not locked.active:
        raise PoolNotFound(f"No pool exists for currencies {pair.currency_a} and {pair.currency_b} with a fee of {fee_bps} basis points.")
    return locked


def _check_room(*values: int) -> None:
    if any(value > AMOUNT_MAX for value in values):
        raise AmountOverflow("The pool's reserves or shares would exceed the supported range.")


async def set_reserves(tx: LedgerTx, pool_id: int, reserve_a: int, reserve_b: int) -> None:
    """Store new reserves of a pool locked by lock_pools(); reserves above AMOUNT_MAX raise AmountOverflow and non-positive ones LedgerUsageError, both before writing."""
    if reserve_a <= 0 or reserve_b <= 0:
        raise LedgerUsageError(f"pool {pool_id} reserves must stay positive")
    _check_room(reserve_a, reserve_b)
    await tx.conn.execute(update(pool).where(pool.c.pool_id == pool_id).values(reserve_a=reserve_a, reserve_b=reserve_b))


async def _lock_share(tx: LedgerTx, pool_id: int, account_id: int) -> int:
    stmt = select(pool_share.c.shares).where(pool_share.c.pool_id == pool_id, pool_share.c.account_id == account_id).with_for_update()
    value = (await tx.conn.execute(stmt)).scalar_one_or_none()
    return 0 if value is None else int(value)


async def _store_share(tx: LedgerTx, pool_id: int, account_id: int, *, before: int, after: int) -> None:
    where = (pool_share.c.pool_id == pool_id, pool_share.c.account_id == account_id)
    if after == 0:
        await tx.conn.execute(delete(pool_share).where(*where))
    elif before == 0:
        await tx.conn.execute(insert(pool_share).values(pool_id=pool_id, account_id=account_id, shares=after, updated_at=tx.now))
    else:
        await tx.conn.execute(update(pool_share).where(*where).values(shares=after, updated_at=tx.now))


async def create_pool_in_tx(tx: LedgerTx, execution_id: int, *, user_id: int, currency_x: int, currency_y: int, fee_bps: int, amount_x: int, amount_y: int) -> PoolCreated:
    """Create the pool of the pair and fee tier with the user's deposit as its first liquidity, locking MINIMUM_LIQUIDITY of the floor(sqrt(a * b)) initial shares forever and giving the rest to the user.

    Raises PoolAlreadyExists for an active pool of the same pair and tier, InsufficientLiquidity when the initial shares do not exceed MINIMUM_LIQUIDITY, and CurrencyNotFound, InsufficientBalance or AmountOverflow for a missing or deleted currency, an unaffordable deposit or a custody balance above AMOUNT_MAX.
    """
    validate_user_account_id(user_id)
    pair = canonical_pair(currency_x, currency_y)
    validate_fee_tier(fee_bps)
    validate_positive_amount(amount_x)
    validate_positive_amount(amount_y)
    amount_a, amount_b = pair.to_ab(amount_x, amount_y)
    a, b = pair.currency_a, pair.currency_b
    await tx.lock_currencies([a, b])
    duplicate = PoolAlreadyExists(f"A pool already exists for currencies {a} and {b} with a fee of {fee_bps} basis points.")
    if await _active_pool_id(tx.conn, a, b, fee_bps) is not None:
        raise duplicate
    total = initial_shares(amount_a, amount_b)
    if total <= MINIMUM_LIQUIDITY:
        raise InsufficientLiquidity(f"The initial deposit must mint more than {MINIMUM_LIQUIDITY} shares (floor(sqrt(amount_a * amount_b)) is {total}).")
    await tx.require_accounts([user_id])
    values = {
        "currency_a": a,
        "currency_b": b,
        "fee_bps": fee_bps,
        "reserve_a": amount_a,
        "reserve_b": amount_b,
        "total_shares": total,
        "locked_shares": MINIMUM_LIQUIDITY,
        "created_by": user_id,
        "created_execution_id": execution_id,
        "created_at": tx.now,
    }
    # A concurrent creator's uncommitted row makes this insert wait and then fail on the unique pair and tier; the savepoint keeps the transaction usable to record the failure.
    savepoint = await tx.conn.begin_nested()
    try:
        inserted = await tx.conn.execute(insert(pool).values(**values))
    except IntegrityError as exc:
        await savepoint.rollback()
        if mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
            raise duplicate from exc
        raise
    await savepoint.commit()
    key = inserted.inserted_primary_key
    if key is None:
        raise LedgerUsageError("pool insert returned no primary key")
    pool_id = int(key[0])
    custody = await create_pool_custody_account(tx.conn, pool_id, now=tx.now)
    await tx.lock_balances([(user_id, a), (user_id, b), (custody, a), (custody, b)])
    transfer_ids = (
        await tx.move(execution_id, a, user_id, custody, amount_a, TransferKind.PRINCIPAL),
        await tx.move(execution_id, b, user_id, custody, amount_b, TransferKind.PRINCIPAL),
    )
    provider_shares = total - MINIMUM_LIQUIDITY
    await _store_share(tx, pool_id, user_id, before=0, after=provider_shares)
    return PoolCreated(
        execution_id=execution_id,
        pool_id=pool_id,
        currency_a=a,
        currency_b=b,
        fee_bps=fee_bps,
        currency_x=pair.currency_x,
        currency_y=pair.currency_y,
        amount_a=amount_a,
        amount_b=amount_b,
        shares=provider_shares,
        provider_shares=provider_shares,
        total_shares=total,
        reserve_a=amount_a,
        reserve_b=amount_b,
        transfer_ids=transfer_ids,
        locked_shares=MINIMUM_LIQUIDITY,
        custody_account_id=custody,
    )


async def add_liquidity_in_tx(
    tx: LedgerTx,
    execution_id: int,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    fee_bps: int,
    amount_x_desired: int,
    amount_y_desired: int,
    amount_x_min: int = 0,
    amount_y_min: int = 0,
    min_shares: int = 1,
) -> LiquidityAdded:
    """Deposit into the active pool of the pair and tier at its current price, using all of one desired amount and at most the other (see optimal_deposit()), and mint the shares deposit_shares() gives.

    Raises PoolNotFound, InsufficientLiquidity when an amount or the minted shares would be 0, SlippageExceeded when an amount falls below its minimum or the shares below min_shares, AmountOverflow, and InsufficientBalance.
    """
    validate_user_account_id(user_id)
    pair = canonical_pair(currency_x, currency_y)
    validate_fee_tier(fee_bps)
    validate_positive_amount(amount_x_desired)
    validate_positive_amount(amount_y_desired)
    validate_non_negative_amount(amount_x_min, "The minimum amount must be a non-negative integer number of minimal units.")
    validate_non_negative_amount(amount_y_min, "The minimum amount must be a non-negative integer number of minimal units.")
    require_int_in_range("min_shares", min_shares, 1, AMOUNT_MAX)
    desired_a, desired_b = pair.to_ab(amount_x_desired, amount_y_desired)
    min_a, min_b = pair.to_ab(amount_x_min, amount_y_min)
    a, b = pair.currency_a, pair.currency_b
    await tx.lock_currencies([a, b])
    locked = await lock_active_pool(tx, pair, fee_bps)
    held = await _lock_share(tx, locked.pool_id, user_id)
    amount_a, amount_b = optimal_deposit(desired_a, desired_b, locked.reserve_a, locked.reserve_b)
    minted = deposit_shares(amount_a, amount_b, locked.reserve_a, locked.reserve_b, locked.total_shares)
    if amount_a == 0 or amount_b == 0 or minted == 0:
        raise InsufficientLiquidity("The deposit is too small to mint any shares at the pool's current price.")
    if amount_a < min_a or amount_b < min_b:
        raise SlippageExceeded("The deposit at the pool's current price would use less than the minimum amount.")
    if minted < min_shares:
        raise SlippageExceeded(f"The deposit would mint {minted} shares, fewer than the minimum of {min_shares}.")
    reserve_a, reserve_b, total = locked.reserve_a + amount_a, locked.reserve_b + amount_b, locked.total_shares + minted
    _check_room(reserve_a, reserve_b, total)
    custody = locked.custody_account_id
    await tx.require_accounts([user_id])
    await tx.lock_balances([(user_id, a), (user_id, b), (custody, a), (custody, b)])
    transfer_ids = (
        await tx.move(execution_id, a, user_id, custody, amount_a, TransferKind.PRINCIPAL),
        await tx.move(execution_id, b, user_id, custody, amount_b, TransferKind.PRINCIPAL),
    )
    await set_reserves(tx, locked.pool_id, reserve_a, reserve_b)
    await tx.conn.execute(update(pool).where(pool.c.pool_id == locked.pool_id).values(total_shares=total))
    await _store_share(tx, locked.pool_id, user_id, before=held, after=held + minted)
    return LiquidityAdded(
        execution_id=execution_id,
        pool_id=locked.pool_id,
        currency_a=a,
        currency_b=b,
        fee_bps=fee_bps,
        currency_x=pair.currency_x,
        currency_y=pair.currency_y,
        amount_a=amount_a,
        amount_b=amount_b,
        shares=minted,
        provider_shares=held + minted,
        total_shares=total,
        reserve_a=reserve_a,
        reserve_b=reserve_b,
        transfer_ids=transfer_ids,
    )


async def remove_liquidity_in_tx(
    tx: LedgerTx,
    execution_id: int,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    fee_bps: int,
    shares: int,
    amount_x_min: int = 0,
    amount_y_min: int = 0,
) -> LiquidityRemoved:
    """Burn the user's shares in the active pool of the pair and tier and pay out floor(shares * reserve / total) of each currency; a provider left with no shares loses its share row.

    Raises PoolNotFound, InsufficientShares when the user holds fewer shares, InsufficientLiquidity when an amount would be 0, and SlippageExceeded when an amount falls below its minimum.
    """
    validate_user_account_id(user_id)
    pair = canonical_pair(currency_x, currency_y)
    validate_fee_tier(fee_bps)
    validate_positive_amount(shares)
    validate_non_negative_amount(amount_x_min, "The minimum amount must be a non-negative integer number of minimal units.")
    validate_non_negative_amount(amount_y_min, "The minimum amount must be a non-negative integer number of minimal units.")
    min_a, min_b = pair.to_ab(amount_x_min, amount_y_min)
    a, b = pair.currency_a, pair.currency_b
    await tx.lock_currencies([a, b])
    locked = await lock_active_pool(tx, pair, fee_bps)
    held = await _lock_share(tx, locked.pool_id, user_id)
    if held < shares:
        raise InsufficientShares(f"You hold {held} shares of this pool, fewer than {shares}.")
    amount_a, amount_b = withdrawal_amounts(shares, locked.reserve_a, locked.reserve_b, locked.total_shares)
    if amount_a == 0 or amount_b == 0:
        raise InsufficientLiquidity("Burning this many shares would pay out nothing of one currency.")
    if amount_a < min_a or amount_b < min_b:
        raise SlippageExceeded("The withdrawal would pay out less than the minimum amount.")
    reserve_a, reserve_b, total = locked.reserve_a - amount_a, locked.reserve_b - amount_b, locked.total_shares - shares
    if total < locked.locked_shares or reserve_a <= 0 or reserve_b <= 0:
        raise LedgerUsageError(f"pool {locked.pool_id} would fall below its locked minimum liquidity")
    custody = locked.custody_account_id
    await tx.require_accounts([user_id])
    await tx.lock_balances([(user_id, a), (user_id, b), (custody, a), (custody, b)])
    transfer_ids = (
        await tx.move(execution_id, a, custody, user_id, amount_a, TransferKind.PRINCIPAL),
        await tx.move(execution_id, b, custody, user_id, amount_b, TransferKind.PRINCIPAL),
    )
    await set_reserves(tx, locked.pool_id, reserve_a, reserve_b)
    await tx.conn.execute(update(pool).where(pool.c.pool_id == locked.pool_id).values(total_shares=total))
    await _store_share(tx, locked.pool_id, user_id, before=held, after=held - shares)
    return LiquidityRemoved(
        execution_id=execution_id,
        pool_id=locked.pool_id,
        currency_a=a,
        currency_b=b,
        fee_bps=fee_bps,
        currency_x=pair.currency_x,
        currency_y=pair.currency_y,
        amount_a=amount_a,
        amount_b=amount_b,
        shares=shares,
        provider_shares=held - shares,
        total_shares=total,
        reserve_a=reserve_a,
        reserve_b=reserve_b,
        transfer_ids=transfer_ids,
    )


def _prevalidate(currency_x: object, currency_y: object, fee_bps: object, *amounts: object) -> None:
    """The checks made before run_operation(), so malformed requests record nothing."""
    canonical_pair(currency_x, currency_y)
    validate_fee_tier(fee_bps)
    for amount in amounts:
        require_storable_int(amount)


async def create_pool(
    engine: AsyncEngine,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    fee_bps: int,
    amount_x: int,
    amount_y: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> PoolCreated:
    """Create a pool as one pool_create execution by user_id, who deposits amount_x and amount_y as its first liquidity."""
    _prevalidate(currency_x, currency_y, fee_bps, amount_x, amount_y)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await create_pool_in_tx(tx, execution_id, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, amount_x=amount_x, amount_y=amount_y)
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.POOL_CREATE,
        initiator_id=user_id,
        input={"currency_x": currency_x, "currency_y": currency_y, "fee_bps": fee_bps, "amount_x": amount_x, "amount_y": amount_y},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return PoolCreated.from_operation(op)


async def add_liquidity(
    engine: AsyncEngine,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    fee_bps: int,
    amount_x_desired: int,
    amount_y_desired: int,
    amount_x_min: int = 0,
    amount_y_min: int = 0,
    min_shares: int = 1,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> LiquidityAdded:
    """Add liquidity to an existing pool as one liquidity_add execution by user_id (see add_liquidity_in_tx())."""
    _prevalidate(currency_x, currency_y, fee_bps, amount_x_desired, amount_y_desired, amount_x_min, amount_y_min, min_shares)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await add_liquidity_in_tx(
            tx,
            execution_id,
            user_id=user_id,
            currency_x=currency_x,
            currency_y=currency_y,
            fee_bps=fee_bps,
            amount_x_desired=amount_x_desired,
            amount_y_desired=amount_y_desired,
            amount_x_min=amount_x_min,
            amount_y_min=amount_y_min,
            min_shares=min_shares,
        )
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.LIQUIDITY_ADD,
        initiator_id=user_id,
        input={
            "currency_x": currency_x,
            "currency_y": currency_y,
            "fee_bps": fee_bps,
            "amount_x_desired": amount_x_desired,
            "amount_y_desired": amount_y_desired,
            "amount_x_min": amount_x_min,
            "amount_y_min": amount_y_min,
            "min_shares": min_shares,
        },
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return LiquidityAdded.from_operation(op)


async def remove_liquidity(
    engine: AsyncEngine,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    fee_bps: int,
    shares: int,
    amount_x_min: int = 0,
    amount_y_min: int = 0,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> LiquidityRemoved:
    """Remove liquidity from a pool as one liquidity_remove execution by user_id (see remove_liquidity_in_tx())."""
    _prevalidate(currency_x, currency_y, fee_bps, shares, amount_x_min, amount_y_min)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await remove_liquidity_in_tx(
            tx, execution_id, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, shares=shares, amount_x_min=amount_x_min, amount_y_min=amount_y_min
        )
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.LIQUIDITY_REMOVE,
        initiator_id=user_id,
        input={"currency_x": currency_x, "currency_y": currency_y, "fee_bps": fee_bps, "shares": shares, "amount_x_min": amount_x_min, "amount_y_min": amount_y_min},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return LiquidityRemoved.from_operation(op)


@dataclass(frozen=True)
class PoolInfo:
    """A pool with its currencies and custody account; a dissolved pool has zero reserves and shares and its dissolution recorded."""

    pool_id: int
    currency_a: int
    currency_b: int
    # The currencies' current symbols, so a dissolved pool of a deleted and later revived currency shows the revived symbol.
    symbol_a: str
    symbol_b: str
    # Whether each currency is deleted now.
    deleted_a: bool
    deleted_b: bool
    fee_bps: int
    reserve_a: int
    reserve_b: int
    total_shares: int
    locked_shares: int
    custody_account_id: int
    created_by: int
    created_at: datetime
    created_execution_id: int
    dissolved_at: datetime | None
    dissolved_execution_id: int | None

    @property
    def active(self) -> bool:
        return self.dissolved_at is None


@dataclass(frozen=True)
class ProviderPosition:
    """A provider's shares in an active pool, the amounts burning all of them would pay out now, and shares / pool.total_shares as share_percent (see share_percent())."""

    pool: PoolInfo
    account_id: int
    shares: int
    withdrawable_a: int
    withdrawable_b: int
    share_percent: str


_currency_a = currency.alias("pool_currency_a")
_currency_b = currency.alias("pool_currency_b")


def _pool_query() -> Select[Any]:
    return (
        select(
            pool,
            _currency_a.c.symbol.label("symbol_a"),
            _currency_b.c.symbol.label("symbol_b"),
            _currency_a.c.deleted_at.label("deleted_at_a"),
            _currency_b.c.deleted_at.label("deleted_at_b"),
            account.c.account_id.label("custody_account_id"),
        )
        .join(_currency_a, _currency_a.c.currency_id == pool.c.currency_a)
        .join(_currency_b, _currency_b.c.currency_id == pool.c.currency_b)
        .join(account, account.c.pool_id == pool.c.pool_id)
    )


def _pool_info(row: Row[Any]) -> PoolInfo:
    values = row._mapping
    return PoolInfo(
        pool_id=values["pool_id"],
        currency_a=values["currency_a"],
        currency_b=values["currency_b"],
        symbol_a=values["symbol_a"],
        symbol_b=values["symbol_b"],
        deleted_a=values["deleted_at_a"] is not None,
        deleted_b=values["deleted_at_b"] is not None,
        fee_bps=values["fee_bps"],
        reserve_a=int(values["reserve_a"]),
        reserve_b=int(values["reserve_b"]),
        total_shares=int(values["total_shares"]),
        locked_shares=int(values["locked_shares"]),
        custody_account_id=values["custody_account_id"],
        created_by=values["created_by"],
        created_at=values["created_at"],
        created_execution_id=values["created_execution_id"],
        dissolved_at=values["dissolved_at"],
        dissolved_execution_id=values["dissolved_execution_id"],
    )


def _position(row: Row[Any]) -> ProviderPosition:
    info = _pool_info(row)
    shares = int(row._mapping["shares"])
    withdrawable_a, withdrawable_b = withdrawal_amounts(shares, info.reserve_a, info.reserve_b, info.total_shares)
    return ProviderPosition(
        pool=info,
        account_id=row._mapping["account_id"],
        shares=shares,
        withdrawable_a=withdrawable_a,
        withdrawable_b=withdrawable_b,
        share_percent=share_percent(shares, info.total_shares),
    )


async def list_pools(db: Db, *, page: int = 1, per_page: int = POOLS_PER_PAGE_DEFAULT) -> Page[PoolInfo]:
    """One page of the active pools in ascending pool_id; paging is checked by validate_paging()."""
    stmt = _pool_query().where(pool.c.active == true()).order_by(pool.c.pool_id)
    return await read_page(db, stmt, key=pool.c.pool_id, page=page, per_page=per_page, per_page_max=POOLS_PER_PAGE_MAX, convert=_pool_info)


async def get_pools_for_pair(db: Db, currency_x: int, currency_y: int) -> tuple[PoolInfo, ...]:
    """The active pools of the pair, given in either order, one per fee tier in ascending fee_bps; raises InvalidArgument for identical or malformed currency IDs."""
    pair = canonical_pair(currency_x, currency_y)
    stmt = _pool_query().where(pool.c.currency_a == pair.currency_a, pool.c.currency_b == pair.currency_b, pool.c.active == true()).order_by(pool.c.fee_bps)
    async with reader(db) as conn:
        rows = (await conn.execute(stmt)).all()
    return tuple(_pool_info(row) for row in rows)


async def get_pool(db: Db, pool_id: int) -> PoolInfo | None:
    """The pool with this ID, active or dissolved, or None."""
    validate_bigint_id("pool_id", pool_id)
    async with reader(db) as conn:
        row = (await conn.execute(_pool_query().where(pool.c.pool_id == pool_id))).one_or_none()
    return None if row is None else _pool_info(row)


async def list_provider_positions(db: Db, account_id: int, *, page: int = 1, per_page: int = POSITIONS_PER_PAGE_DEFAULT) -> Page[ProviderPosition]:
    """One page of the account's pool positions in ascending pool_id, with the withdrawable estimates and share percentages of ProviderPosition."""
    validate_bigint_id("account_id", account_id)
    stmt = (
        _pool_query()
        .add_columns(pool_share.c.account_id, pool_share.c.shares)
        .join(pool_share, pool_share.c.pool_id == pool.c.pool_id)
        .where(pool_share.c.account_id == account_id)
        .order_by(pool.c.pool_id)
    )
    return await read_page(db, stmt, key=pool_share.c.pool_id, page=page, per_page=per_page, per_page_max=POSITIONS_PER_PAGE_MAX, convert=_position)


class DexPoolDissolver:
    """The hooks.PoolDissolver of the DEX: deleting a currency burns its reserve in every active pool containing it and pays the partner reserve out to the providers pro rata by shares (see pro_rata_payouts()), burning it into the partner's issuance account when no provider is left."""

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        rows = await conn.execute(select(pool.c.currency_a, pool.c.currency_b).where(_contains(currency_id), pool.c.dissolved_at.is_(None)))
        return {row.currency_b if row.currency_a == currency_id else row.currency_a for row in rows}

    async def _pool_ids(self, conn: AsyncConnection, currency_id: int) -> list[int]:
        rows = await conn.execute(select(pool.c.pool_id).where(_contains(currency_id), pool.c.dissolved_at.is_(None)).order_by(pool.c.pool_id))
        return [row.pool_id for row in rows]

    async def _holdings(self, conn: AsyncConnection, pool_ids: list[int], *, for_update: bool) -> dict[int, dict[int, int]]:
        holdings: dict[int, dict[int, int]] = {pool_id: {} for pool_id in pool_ids}
        if not pool_ids:
            return holdings
        stmt = select(pool_share.c.pool_id, pool_share.c.account_id, pool_share.c.shares).where(pool_share.c.pool_id.in_(pool_ids)).order_by(pool_share.c.pool_id, pool_share.c.account_id)
        for row in await conn.execute(stmt.with_for_update() if for_update else stmt):
            holdings[row.pool_id][row.account_id] = int(row.shares)
        return holdings

    async def _issuance(self, conn: AsyncConnection, currency_ids: Iterable[int]) -> dict[int, int]:
        rows = await conn.execute(select(currency.c.currency_id, currency.c.issuance_account_id).where(currency.c.currency_id.in_(sorted(set(currency_ids)))))
        return {row.currency_id: row.issuance_account_id for row in rows}

    async def _active_locked(self, tx: LedgerTx, currency_id: int, *, lock: bool) -> list[LockedPool]:
        # The pool set is stable here because every pool creation locks both its currencies first and the cascade holds currency_id FOR UPDATE.
        ids = await self._pool_ids(tx.conn, currency_id)
        if lock:
            locked = await lock_pools(tx, ids)
        else:
            rows = (await tx.conn.execute(select(pool).where(pool.c.pool_id.in_(ids)).order_by(pool.c.pool_id))).mappings().all() if ids else []
            locked = {row["pool_id"]: _locked_pool(row, await _require_custody(tx.conn, row["pool_id"])) for row in rows}
        return [locked[pool_id] for pool_id in ids if pool_id in locked and locked[pool_id].active]

    async def balance_keys(self, tx: LedgerTx, currency_id: int) -> set[BalanceKey]:
        """Lock the active pools containing currency_id FOR UPDATE in ascending pool_id, then their share rows in (pool_id, account_id) order, and return the custody balances of both currencies, each provider's partner balance and the issuance balances the dissolution moves into."""
        pools_ = await self._active_locked(tx, currency_id, lock=True)
        holdings = await self._holdings(tx.conn, [locked.pool_id for locked in pools_], for_update=True)
        if not pools_:
            return set()
        issuance = await self._issuance(tx.conn, {currency_id, *(locked.other_currency(currency_id) for locked in pools_)})
        keys: set[BalanceKey] = {(issuance[currency_id], currency_id)}
        for locked in pools_:
            partner = locked.other_currency(currency_id)
            keys |= {(locked.custody_account_id, locked.currency_a), (locked.custody_account_id, locked.currency_b)}
            keys |= {(account_id, partner) for account_id in holdings[locked.pool_id]}
            if not holdings[locked.pool_id]:
                keys.add((issuance[partner], partner))
        return keys

    async def dissolve_pools_for_currency(self, tx: LedgerTx, execution_id: int, currency_id: int) -> PoolDissolution:
        """Dissolve the pools locked by balance_keys() in ascending pool_id, recording every move on execution_id and skipping zero amounts, then delete their share rows and zero and close their pool rows; the result counts the pools and the custody balances of currency_id burned."""
        pools_ = await self._active_locked(tx, currency_id, lock=False)
        if not pools_:
            return PoolDissolution()
        holdings = await self._holdings(tx.conn, [locked.pool_id for locked in pools_], for_update=False)
        issuance = await self._issuance(tx.conn, {currency_id, *(locked.other_currency(currency_id) for locked in pools_)})
        balances_burned = burned_amount = 0
        for locked in pools_:
            custody, partner = locked.custody_account_id, locked.other_currency(currency_id)
            burned = tx.balance(custody, currency_id)
            if burned > 0:
                await tx.move(execution_id, currency_id, custody, issuance[currency_id], burned, TransferKind.PRINCIPAL)
                balances_burned += 1
                burned_amount += burned
            reserve = tx.balance(custody, partner)
            providers = holdings[locked.pool_id]
            payouts = pro_rata_payouts(reserve, providers) if providers else {issuance[partner]: reserve}
            for account_id, amount in sorted(payouts.items()):
                if amount > 0:
                    await tx.move(execution_id, partner, custody, account_id, amount, TransferKind.PRINCIPAL)
            await tx.conn.execute(delete(pool_share).where(pool_share.c.pool_id == locked.pool_id))
            closed = {"reserve_a": 0, "reserve_b": 0, "total_shares": 0, "locked_shares": 0, "dissolved_at": tx.now, "dissolved_execution_id": execution_id}
            await tx.conn.execute(update(pool).where(pool.c.pool_id == locked.pool_id).values(**closed))
        return PoolDissolution(pools_dissolved=len(pools_), balances_burned=balances_burned, burned_amount=burned_amount)


POOL_DISSOLVER: Final = DexPoolDissolver()
register_pool_dissolver(POOL_DISSOLVER)
