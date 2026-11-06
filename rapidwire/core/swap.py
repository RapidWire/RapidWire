"""DEX swaps: quote_swap() prices a swap on one unlocked snapshot of the active pools, and swap() executes it as one swap execution that re-allocates the input over the candidate paths on the locked reserves.

A swap locks, in ledger order, the source, target and every currency of its candidate pools FOR SHARE in one ascending call, then the candidate pools FOR UPDATE in ascending pool_id, then the user's two balances and both custody balances of every usable candidate pool in one lock_balances() call, before allocating.
Paths are enumerated before the transaction, or inside it before any lock in swap_in_tx() without a plan, never while the swap's own locks are held.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple, Self

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.db import Db, consistent_snapshot
from rapidwire.core.errors import CurrencyNotFound, InsufficientBalance, InvalidArgument, NoRoute, SlippageExceeded
from rapidwire.core.ledger import BalanceKey, LedgerTx, LedgerUsageError, OperationResult, RerunTransaction, run_operation, set_failure_output
from rapidwire.core.pools import LockedPool, load_active_pools, lock_pools, set_reserves
from rapidwire.core.routing import MAX_PATH_EXPANSIONS, Allocation, Path, PathSearch, PoolState, Route, allocate, enumerate_paths
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.core.validation import require_storable_int, validate_non_negative_amount, validate_positive_amount, validate_positive_id, validate_user_account_id
from rapidwire.db.tables import currency
from rapidwire.db.types import AMOUNT_MAX

if TYPE_CHECKING:
    from rapidwire.config import Settings

__all__ = [
    "Movement",
    "SwapConfig",
    "SwapHop",
    "SwapPlan",
    "SwapQuote",
    "SwapResult",
    "SwapRoute",
    "allocation_chunk_hops",
    "plan_swap",
    "probe_amount",
    "quote_swap",
    "schedule_transfers",
    "swap",
    "swap_in_tx",
    "swap_movements",
]


@dataclass(frozen=True)
class SwapConfig:
    """The allocation settings of a swap: chunk_count is how many chunks the input is split into, max_routes how many candidate paths are considered and max_expansions the path search budget; all must be positive."""

    chunk_count: int
    max_routes: int
    max_expansions: int = MAX_PATH_EXPANSIONS

    def __post_init__(self) -> None:
        for name in ("chunk_count", "max_routes", "max_expansions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(chunk_count=settings.swap_chunk_count, max_routes=settings.swap_max_routes)


def probe_amount(amount_in: int, chunk_count: int) -> int:
    """The amount paths are ranked by: the size of one chunk, ceil(amount_in / min(chunk_count, amount_in))."""
    count = min(chunk_count, amount_in)
    return -(-amount_in // count)


@dataclass(frozen=True)
class SwapHop:
    """One pool of a route: amount_in of currency_in entered it and amount_out of currency_out left it."""

    pool_id: int
    currency_in: int
    currency_out: int
    fee_bps: int
    amount_in: int
    amount_out: int

    def to_output(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "currency_in": self.currency_in,
            "currency_out": self.currency_out,
            "fee_bps": self.fee_bps,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
        }


@dataclass(frozen=True)
class SwapRoute:
    """The share of a swap that follows one path: amount_in of the source enters its first pool and amount_out of the target leaves its last."""

    amount_in: int
    amount_out: int
    hops: tuple[SwapHop, ...]

    @property
    def pool_ids(self) -> tuple[int, ...]:
        return tuple(hop.pool_id for hop in self.hops)

    @property
    def currencies(self) -> tuple[int, ...]:
        return (self.hops[0].currency_in, *(hop.currency_out for hop in self.hops))

    @property
    def fee_bps(self) -> tuple[int, ...]:
        return tuple(hop.fee_bps for hop in self.hops)

    def to_output(self) -> dict[str, Any]:
        return {
            "pool_ids": self.pool_ids,
            "currencies": self.currencies,
            "fee_bps": self.fee_bps,
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "hops": [hop.to_output() for hop in self.hops],
        }


def _routes(allocation: Allocation, fees: Mapping[int, int]) -> tuple[SwapRoute, ...]:
    def hops(route: Route) -> tuple[SwapHop, ...]:
        return tuple(SwapHop(hop.pool_id, hop.currency_in, hop.currency_out, fees[hop.pool_id], hop.amount_in, hop.amount_out) for hop in route.hops)

    return tuple(SwapRoute(amount_in=route.amount_in, amount_out=route.amount_out, hops=hops(route)) for route in allocation.routes)


def _int(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError(f"stored swap field {value!r} is not an integer string")
    return int(value)


def _routes_from_output(value: Any) -> tuple[SwapRoute, ...]:
    if not isinstance(value, list):
        raise ValueError("stored swap routes are not a list")
    routes: list[SwapRoute] = []
    for item in value:
        hops = tuple(SwapHop(_int(hop["pool_id"]), _int(hop["currency_in"]), _int(hop["currency_out"]), _int(hop["fee_bps"]), _int(hop["amount_in"]), _int(hop["amount_out"])) for hop in item["hops"])
        routes.append(SwapRoute(amount_in=_int(item["amount_in"]), amount_out=_int(item["amount_out"]), hops=hops))
    return tuple(routes)


@dataclass(frozen=True)
class SwapQuote:
    """The best allocation of amount_in over the candidate paths on the current reserves: the total amount_out, the routes best first in path rank, truncated when the path search hit its expansion limit, the symbols of every currency involved, the path expansions the search simulated and the chunk-hops the allocation simulated (see allocation_chunk_hops())."""

    from_currency: int
    to_currency: int
    amount_in: int
    amount_out: int
    routes: tuple[SwapRoute, ...]
    truncated: bool
    symbols: Mapping[int, str]
    expansions: int = 0
    chunk_hops: int = 0

    @property
    def from_symbol(self) -> str:
        return self.symbols[self.from_currency]

    @property
    def to_symbol(self) -> str:
        return self.symbols[self.to_currency]


@dataclass(frozen=True)
class SwapResult:
    """A completed swap: amount_in of from_currency paid in, amount_out of to_currency received over routes, with the transfers recorded; replayed takes no part in equality."""

    execution_id: int
    from_currency: int
    to_currency: int
    amount_in: int
    min_out: int
    amount_out: int
    routes: tuple[SwapRoute, ...]
    transfer_ids: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "amount_in": self.amount_in,
            "min_out": self.min_out,
            "amount_out": self.amount_out,
            "routes": [route.to_output() for route in self.routes],
            "transfer_ids": self.transfer_ids,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> Self:
        names = ("from_currency", "to_currency", "amount_in", "min_out", "amount_out")
        values: dict[str, Any] = {name: op.int_field(name) for name in names}
        transfer_ids = op.output.get("transfer_ids")
        if not isinstance(transfer_ids, list):
            raise ValueError("stored swap transfer_ids are not a list")
        return cls(execution_id=op.execution_id, routes=_routes_from_output(op.output.get("routes")), transfer_ids=tuple(_int(item) for item in transfer_ids), replayed=op.replayed, **values)


@dataclass(frozen=True)
class SwapPlan:
    """The candidate paths of a swap found on an unlocked read of the active pools, best first; truncated and expansions are the path search's flag and count."""

    from_currency: int
    to_currency: int
    paths: tuple[Path, ...]
    truncated: bool
    expansions: int = 0

    @property
    def pool_ids(self) -> tuple[int, ...]:
        return tuple(sorted({pool_id for path in self.paths for pool_id in path.pool_ids}))

    @property
    def currency_ids(self) -> tuple[int, ...]:
        return tuple(sorted({self.from_currency, self.to_currency, *(currency_id for path in self.paths for currency_id in path.currencies)}))


def allocation_chunk_hops(paths: Sequence[Path], amount_in: int, chunk_count: int) -> int:
    """The chunk-hops allocate() simulates for these candidate paths: its chunk count, min(chunk_count, amount_in), times the total pool count of the paths."""
    return min(chunk_count, amount_in) * sum(len(path.pool_ids) for path in paths)


def _validate_pair(from_currency: object, to_currency: object) -> tuple[int, int]:
    source = validate_positive_id("from_currency", from_currency)
    target = validate_positive_id("to_currency", to_currency)
    if source == target:
        raise InvalidArgument("A swap needs two different currencies.")
    return source, target


def _search(pools: Sequence[PoolState], from_currency: int, to_currency: int, amount_in: int, config: SwapConfig) -> PathSearch:
    return enumerate_paths(pools, from_currency, to_currency, max_paths=config.max_routes, probe_amount=probe_amount(amount_in, config.chunk_count), max_expansions=config.max_expansions)


def _search_and_allocate(pools: Sequence[PoolState], from_currency: int, to_currency: int, amount_in: int, config: SwapConfig) -> tuple[PathSearch, Allocation]:
    search = _search(pools, from_currency, to_currency, amount_in, config)
    used = {pool_id for path in search.paths for pool_id in path.pool_ids}
    allocation = allocate(search.paths, [state for state in pools if state.pool_id in used], amount_in, chunk_count=config.chunk_count)
    return search, allocation


async def plan_swap(db: Db, *, from_currency: int, to_currency: int, amount_in: int, config: SwapConfig) -> SwapPlan:
    """Read the active pools without locks and enumerate up to config.max_routes paths ranked for one chunk of amount_in, in a worker thread; raises InvalidArgument, InvalidAmount or AmountOverflow for malformed arguments."""
    source, target = _validate_pair(from_currency, to_currency)
    validate_positive_amount(amount_in)
    pools = await load_active_pools(db)
    search = await asyncio.to_thread(_search, pools, source, target, amount_in, config)
    return SwapPlan(from_currency=source, to_currency=target, paths=search.paths, truncated=search.truncated, expansions=search.expansions)


async def _endpoint_symbols(conn: AsyncConnection, currency_ids: Iterable[int], required: Iterable[int]) -> dict[int, str]:
    """The symbols of the live currencies among currency_ids; a missing or deleted one in required raises CurrencyNotFound."""
    ids = sorted(set(currency_ids))
    rows = await conn.execute(select(currency.c.currency_id, currency.c.symbol).where(currency.c.currency_id.in_(ids), currency.c.deleted_at.is_(None)))
    symbols = {row.currency_id: row.symbol for row in rows}
    for currency_id in required:
        if currency_id not in symbols:
            raise CurrencyNotFound(f"Currency {currency_id} was not found.")
    return symbols


async def quote_swap(db: Db, *, from_currency: int, to_currency: int, amount_in: int, config: SwapConfig, on_search: Callable[[int, int], None] | None = None) -> SwapQuote:
    """Quote swapping amount_in of from_currency into to_currency on the pools and symbols of one consistent snapshot, released before the CPU-bound search, writing nothing.

    Raises InvalidArgument, InvalidAmount, AmountOverflow, CurrencyNotFound for a missing or deleted endpoint, and NoRoute when nothing can be received.
    on_search, when given, receives the search's expansions and the allocation's chunk-hops (the SwapQuote fields of the same names) once both have run, before NoRoute is raised, so a caller can charge for a search that found no route.
    """
    source, target = _validate_pair(from_currency, to_currency)
    validate_positive_amount(amount_in)
    async with consistent_snapshot(db) as conn:
        pools = await load_active_pools(conn)
        pooled = {currency_id for state in pools for currency_id in (state.currency_a, state.currency_b)}
        known = await _endpoint_symbols(conn, {source, target, *pooled}, (source, target))
    search, allocation = await asyncio.to_thread(_search_and_allocate, pools, source, target, amount_in, config)
    if on_search is not None:
        on_search(search.expansions, allocation_chunk_hops(search.paths, amount_in, config.chunk_count))
    if allocation.is_empty:
        raise _no_route(search.paths, source, target)
    involved = {source, target, *(currency_id for route in allocation.routes for currency_id in route.path.currencies)}
    symbols = {currency_id: known[currency_id] for currency_id in sorted(involved)}
    fees = {state.pool_id: state.fee_bps for state in pools}
    return SwapQuote(
        from_currency=source,
        to_currency=target,
        amount_in=amount_in,
        amount_out=allocation.amount_out,
        routes=_routes(allocation, fees),
        truncated=search.truncated,
        symbols=symbols,
        expansions=search.expansions,
        chunk_hops=allocation_chunk_hops(search.paths, amount_in, config.chunk_count),
    )


def _no_route(paths: Sequence[Path], from_currency: int, to_currency: int) -> NoRoute:
    """NoRoute for an empty allocation: with paths the output rounds down to zero (zero_output, also recorded in the failure output), without them nothing connects the currencies."""
    if not paths:
        return NoRoute(f"No swap route connects currency {from_currency} to currency {to_currency}.")
    error = NoRoute(f"The amount is too small to receive any of currency {to_currency}.", zero_output=True)
    set_failure_output(error, {NoRoute.ZERO_OUTPUT_FIELD: True})
    return error


class Movement(NamedTuple):
    """amount of currency_id moving from from_account to to_account."""

    from_account: int
    to_account: int
    currency_id: int
    amount: int


def swap_movements(allocation: Allocation, user_id: int, custody: Mapping[int, int]) -> list[Movement]:
    """The swap's movements summed per directed (from_account, to_account, currency_id) in order of first appearance over routes and hops: user to first pool, pool to next pool, last pool to user."""
    totals: dict[tuple[int, int, int], int] = {}
    for route in allocation.routes:
        source = user_id
        for hop in route.hops:
            key = (source, custody[hop.pool_id], hop.currency_in)
            totals[key] = totals.get(key, 0) + hop.amount_in
            source = custody[hop.pool_id]
        last = route.hops[-1]
        key = (source, user_id, last.currency_out)
        totals[key] = totals.get(key, 0) + last.amount_out
    return [Movement(from_account, to_account, currency_id, amount) for (from_account, to_account, currency_id), amount in totals.items()]


def schedule_transfers(movements: Sequence[Movement], balances: Mapping[BalanceKey, int]) -> list[Movement]:
    """Order the movements so no source balance goes negative, starting from balances keyed (account_id, currency_id).

    It always progresses when every final balance is non-negative and every account a movement leaves keeps a positive final balance (a pool keeps positive reserves), and raises LedgerUsageError otherwise.
    """
    held = dict(balances)
    remaining = [movement.amount for movement in movements]
    if any(amount <= 0 for amount in remaining):
        raise ValueError("every movement amount must be positive")
    ordered: list[Movement] = []
    while any(remaining):
        chosen: tuple[int, int] | None = None
        # Make the first movement in the given order that its source can cover in full.
        for index, movement in enumerate(movements):
            if remaining[index] and held[(movement.from_account, movement.currency_id)] >= remaining[index]:
                chosen = (index, remaining[index])
                break
        if chosen is None:
            # Otherwise the first movement whose source holds anything moves that much and keeps the rest pending.
            for index, movement in enumerate(movements):
                available = held[(movement.from_account, movement.currency_id)]
                if remaining[index] and available > 0:
                    chosen = (index, available)
                    break
        if chosen is None:
            raise LedgerUsageError("the swap's transfers cannot be ordered without a negative balance")
        index, amount = chosen
        movement = movements[index]
        held[(movement.from_account, movement.currency_id)] -= amount
        held[(movement.to_account, movement.currency_id)] += amount
        remaining[index] -= amount
        ordered.append(movement._replace(amount=amount))
    return ordered


def _usable(plan: SwapPlan, locked: Mapping[int, LockedPool]) -> tuple[list[Path], list[PoolState]]:
    """The plan's paths whose pools all still exist and are active under the lock, and the locked state of those pools."""
    paths = [path for path in plan.paths if all(pool_id in locked and locked[pool_id].active for pool_id in path.pool_ids)]
    used = sorted({pool_id for path in paths for pool_id in path.pool_ids})
    return paths, [locked[pool_id].state() for pool_id in used]


async def swap_in_tx(
    tx: LedgerTx,
    execution_id: int,
    *,
    user_id: int,
    from_currency: int,
    to_currency: int,
    amount_in: int,
    min_out: int,
    config: SwapConfig,
    plan: SwapPlan | None = None,
) -> SwapResult:
    """Swap amount_in of the user's from_currency into to_currency over the plan's paths, re-allocated on the reserves read under the pool locks, recording every movement as a principal transfer of execution_id and storing the pools' new reserves.

    Raises, in this order of precedence, CurrencyNotFound for a missing or deleted endpoint, InsufficientBalance when the user holds less than amount_in, NoRoute when no path survives the lock or nothing can be received, and SlippageExceeded when the total falls below min_out.
    """
    validate_user_account_id(user_id)
    source, target = _validate_pair(from_currency, to_currency)
    validate_positive_amount(amount_in)
    validate_non_negative_amount(min_out, "The minimum amount out must be a non-negative integer number of minimal units.")
    if plan is None:
        # An unlocked read of the active pools plus the CPU-bound search, bounded by config.max_expansions, before anything is locked.
        plan = await plan_swap(tx.conn, from_currency=source, to_currency=target, amount_in=amount_in, config=config)
    if (plan.from_currency, plan.to_currency) != (source, target):
        raise LedgerUsageError("the swap plan is for a different currency pair")
    rows = await tx.lock_currencies(plan.currency_ids, include_deleted=True)
    for currency_id in (source, target):
        if rows[currency_id]["deleted_at"] is not None:
            raise CurrencyNotFound(f"Currency {currency_id} was not found.")
    locked = await lock_pools(tx, plan.pool_ids)
    paths, states = _usable(plan, locked)
    # A planned pool missing or dissolved under the lock reruns the transaction so the caller plans again; once no rerun is left, the paths through it are skipped.
    if len(paths) < len(plan.paths) and tx.rerun_allowed:
        raise RerunTransaction("a pool planned for the swap was dissolved before it was locked")
    custody = {state.pool_id: locked[state.pool_id].custody_account_id for state in states}
    keys: set[BalanceKey] = {(user_id, source), (user_id, target)}
    for state in states:
        keys |= {(custody[state.pool_id], state.currency_a), (custody[state.pool_id], state.currency_b)}
    await tx.lock_balances(keys)
    held = tx.balance(user_id, source)
    if held < amount_in:
        raise InsufficientBalance(f"You hold {held} of currency {source}, less than {amount_in}.")
    if not paths:
        raise _no_route((), source, target)
    allocation = await asyncio.to_thread(allocate, paths, states, amount_in, chunk_count=config.chunk_count)
    if allocation.is_empty:
        raise _no_route(paths, source, target)
    if allocation.amount_in != amount_in:
        raise LedgerUsageError(f"the allocation spends {allocation.amount_in} instead of {amount_in}")
    if allocation.amount_out < min_out:
        raise SlippageExceeded(f"The swap would pay out {allocation.amount_out}, less than the minimum of {min_out}.")

    movements = swap_movements(allocation, user_id, custody)
    ordered = schedule_transfers(movements, {key: tx.balance(*key) for key in keys})
    transfer_ids = [await tx.move(execution_id, item.currency_id, item.from_account, item.to_account, item.amount, TransferKind.PRINCIPAL) for item in ordered]
    for pool_id, (reserve_a, reserve_b) in allocation.final_reserves.items():
        pool_row = locked[pool_id]
        if (tx.balance(pool_row.custody_account_id, pool_row.currency_a), tx.balance(pool_row.custody_account_id, pool_row.currency_b)) != (reserve_a, reserve_b):
            raise LedgerUsageError(f"pool {pool_id} custody balances do not match its new reserves")
        await set_reserves(tx, pool_id, reserve_a, reserve_b)
    fees = {state.pool_id: state.fee_bps for state in states}
    return SwapResult(
        execution_id=execution_id,
        from_currency=source,
        to_currency=target,
        amount_in=amount_in,
        min_out=min_out,
        amount_out=allocation.amount_out,
        routes=_routes(allocation, fees),
        transfer_ids=tuple(transfer_ids),
    )


async def swap(
    engine: AsyncEngine,
    *,
    user_id: int,
    from_currency: int,
    to_currency: int,
    amount_in: int,
    min_out: int,
    config: SwapConfig,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> SwapResult:
    """Swap as one swap execution initiated by user_id, planning the paths on an unlocked read before the transaction and again inside it after a RerunTransaction (see swap_in_tx()); malformed currency IDs or non-integer amounts raise before anything is recorded."""
    _validate_pair(from_currency, to_currency)
    require_storable_int(amount_in)
    require_storable_int(min_out)
    planned: list[SwapPlan] = []
    replan = False

    async def before() -> None:
        if 1 <= amount_in <= AMOUNT_MAX:
            planned.append(await plan_swap(engine, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, config=config))
        else:
            planned.append(SwapPlan(from_currency=from_currency, to_currency=to_currency, paths=(), truncated=False))

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        nonlocal replan
        plan = None if replan else planned[0]
        try:
            result = await swap_in_tx(tx, execution_id, user_id=user_id, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, min_out=min_out, config=config, plan=plan)
        except RerunTransaction:
            replan = True
            raise
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.SWAP,
        initiator_id=user_id,
        input={"from_currency": from_currency, "to_currency": to_currency, "amount_in": amount_in, "min_out": min_out},
        body=body,
        before=before,
        now=now,
        idempotency_key=idempotency_key,
    )
    return SwapResult.from_operation(op)
