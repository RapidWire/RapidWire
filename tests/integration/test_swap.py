from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import currency as cur
from rapidwire.core import pools, swap
from rapidwire.core.errors import CurrencyNotFound, DomainError, IdempotencyConflict, InsufficientBalance, InvalidAmount, InvalidArgument, NoRoute, SlippageExceeded
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import MAX_RERUNS, LedgerTx, LedgerUsageError, run_child, run_operation
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.routing import Allocation, Path, PoolState, amount_out, realize
from rapidwire.core.swap import SwapConfig, SwapPlan, SwapResult
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import FEE_TIERS_BPS, account, balance, execution, pool
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.factories import balance_of, create_currency, create_pool, mint

NOW = datetime(2026, 8, 1, tzinfo=UTC)
AT = NOW + timedelta(days=7)
CONFIG = SwapConfig(chunk_count=100, max_routes=32)
OWNER, LP, BOB = 100, 111, 222
A, B, C, D = 1, 2, 3, 4

type Move = tuple[int, int, int, int]


async def _seed(engine: AsyncEngine) -> None:
    for currency_id, symbol in ((A, "AAA"), (B, "BBB"), (C, "CCC"), (D, "DDD")):
        await create_currency(engine, currency_id=currency_id, symbol=symbol, now=NOW)


async def _pool(engine: AsyncEngine, x: int, y: int, reserve_x: int, reserve_y: int, fee_bps: int = FEE_TIERS_BPS[0]) -> pools.PoolCreated:
    return await create_pool(engine, user_id=LP, currency_x=x, currency_y=y, amount_x=reserve_x, amount_y=reserve_y, fee_bps=fee_bps, now=NOW)


async def _reserves(engine: AsyncEngine, pool_id: int) -> tuple[int, int]:
    info = await pools.get_pool(engine, pool_id)
    assert info is not None
    return info.reserve_a, info.reserve_b


async def _moves(engine: AsyncEngine, execution_id: int) -> list[Move]:
    """The transfers of the execution as (currency_id, from_account, to_account, amount) in transfer_id order, all checked to be principal."""
    async with engine.connect() as conn:
        rows = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == execution_id).order_by(transfer_table.c.transfer_id))).mappings().all()
    assert all(row["kind"] == TransferKind.PRINCIPAL for row in rows)
    return [(row["currency_id"], row["from_account"], row["to_account"], int(row["amount"])) for row in rows]


async def _transfer_ids(engine: AsyncEngine, execution_id: int) -> tuple[int, ...]:
    async with engine.connect() as conn:
        return tuple((await conn.execute(select(transfer_table.c.transfer_id).where(transfer_table.c.execution_id == execution_id).order_by(transfer_table.c.transfer_id))).scalars())


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one())


async def _counts(engine: AsyncEngine) -> tuple[int, int, int]:
    async with engine.connect() as conn:
        return (
            int((await conn.execute(select(func.count()).select_from(execution))).scalar_one()),
            int((await conn.execute(select(func.count()).select_from(transfer_table))).scalar_one()),
            int((await conn.execute(select(func.count()).select_from(balance))).scalar_one()),
        )


async def _all_reserves(engine: AsyncEngine) -> dict[int, tuple[int, int]]:
    async with engine.connect() as conn:
        return {row.pool_id: (int(row.reserve_a), int(row.reserve_b)) for row in await conn.execute(select(pool.c.pool_id, pool.c.reserve_a, pool.c.reserve_b))}


async def _check_state(engine: AsyncEngine) -> None:
    """Every ledger invariant holds and every active pool's custody holds exactly its reserves."""
    report = await verify_invariants(engine)
    assert report.ok, report.violations
    async with engine.connect() as conn:
        rows = (await conn.execute(select(pool, account.c.account_id).join(account, account.c.pool_id == pool.c.pool_id).where(pool.c.dissolved_at.is_(None)))).mappings().all()
    for row in rows:
        custody = row["account_id"]
        assert (await balance_of(engine, custody, row["currency_a"]), await balance_of(engine, custody, row["currency_b"])) == (row["reserve_a"], row["reserve_b"])


async def _expect_failed(engine: AsyncEngine, error: type[DomainError], call: Any, *, reserves_kept: bool = True) -> DomainError:
    """Await call, expect error, and check its execution was recorded as failed with nothing moved and, when reserves_kept, no reserve changed."""
    reserves = await _all_reserves(engine) if reserves_kept else None
    with pytest.raises(error) as raised:
        await call
    assert raised.value.execution_id is not None
    row = await _execution(engine, raised.value.execution_id)
    assert (row["type"], row["status"], row["output"]["error"]["code"]) == (ExecutionType.SWAP, ExecutionStatus.FAILED, error.code)
    assert await _moves(engine, raised.value.execution_id) == []
    if reserves is not None:
        assert await _all_reserves(engine) == reserves
    await _check_state(engine)
    return raised.value


async def _swap(engine: AsyncEngine, from_currency: int, to_currency: int, amount_in: int, min_out: int = 0, config: SwapConfig = CONFIG, **kwargs: Any) -> SwapResult:
    return await swap.swap(engine, user_id=BOB, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, min_out=min_out, config=config, now=NOW, **kwargs)


async def test_single_pool_swap_records_two_transfers(db: AsyncEngine) -> None:
    await _seed(db)
    created = await _pool(db, A, B, 100_000, 200_000)
    custody = created.custody_account_id
    await mint(db, A, BOB, 5_000, now=NOW)
    expected = amount_out(1_000, 100_000, 200_000, FEE_TIERS_BPS[0])

    quote = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=1_000, config=CONFIG)
    assert (quote.amount_in, quote.amount_out, quote.truncated, quote.from_symbol, quote.to_symbol) == (1_000, expected, False, "AAA", "BBB")
    assert len(quote.routes) == 1
    route = quote.routes[0]
    assert (route.pool_ids, route.currencies, route.fee_bps, route.amount_in, route.amount_out) == ((created.pool_id,), (A, B), (FEE_TIERS_BPS[0],), 1_000, expected)

    result = await _swap(db, A, B, 1_000, min_out=quote.amount_out)
    assert (result.amount_in, result.amount_out, result.min_out, result.routes, result.replayed) == (1_000, quote.amount_out, quote.amount_out, quote.routes, False)
    assert await _moves(db, result.execution_id) == [(A, BOB, custody, 1_000), (B, custody, BOB, expected)]
    assert result.transfer_ids == await _transfer_ids(db, result.execution_id)
    assert await _reserves(db, created.pool_id) == (101_000, 200_000 - expected)
    assert (await balance_of(db, BOB, A), await balance_of(db, BOB, B)) == (4_000, expected)
    assert 101_000 * (200_000 - expected) >= 100_000 * 200_000
    row = await _execution(db, result.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.SWAP, ExecutionStatus.SUCCESS, BOB)
    assert row["input"] == {"from_currency": str(A), "to_currency": str(B), "amount_in": "1000", "min_out": str(expected)}
    assert row["output"]["amount_out"] == str(expected)
    assert row["output"]["routes"] == [
        {
            "pool_ids": [str(created.pool_id)],
            "currencies": [str(A), str(B)],
            "fee_bps": [str(FEE_TIERS_BPS[0])],
            "amount_in": "1000",
            "amount_out": str(expected),
            "hops": [{"pool_id": str(created.pool_id), "currency_in": str(A), "currency_out": str(B), "fee_bps": str(FEE_TIERS_BPS[0]), "amount_in": "1000", "amount_out": str(expected)}],
        }
    ]
    assert row["output"]["transfer_ids"] == [str(value) for value in result.transfer_ids]
    await _check_state(db)


async def test_multi_hop_route_moves_through_each_pool(db: AsyncEngine) -> None:
    await _seed(db)
    first = await _pool(db, A, B, 100_000, 100_000)
    second = await _pool(db, C, B, 50_000, 100_000)
    await mint(db, A, BOB, 2_000, now=NOW)
    quote = await swap.quote_swap(db, from_currency=A, to_currency=C, amount_in=2_000, config=CONFIG)
    middle = amount_out(2_000, 100_000, 100_000, FEE_TIERS_BPS[0])
    final = amount_out(middle, 100_000, 50_000, FEE_TIERS_BPS[0])
    assert [(route.pool_ids, route.currencies) for route in quote.routes] == [((first.pool_id, second.pool_id), (A, B, C))]
    assert quote.amount_out == final
    assert [(hop.amount_in, hop.amount_out) for hop in quote.routes[0].hops] == [(2_000, middle), (middle, final)]
    assert quote.symbols == {A: "AAA", B: "BBB", C: "CCC"}

    result = await _swap(db, A, C, 2_000, min_out=final)
    assert (result.amount_out, result.routes) == (quote.amount_out, quote.routes)
    assert await _moves(db, result.execution_id) == [
        (A, BOB, first.custody_account_id, 2_000),
        (B, first.custody_account_id, second.custody_account_id, middle),
        (C, second.custody_account_id, BOB, final),
    ]
    assert await _reserves(db, first.pool_id) == (102_000, 100_000 - middle)
    # The second pool is (B, C) with C as currency_b.
    assert await _reserves(db, second.pool_id) == (100_000 + middle, 50_000 - final)
    assert await balance_of(db, BOB, C) == final
    await _check_state(db)


async def test_split_across_fee_tiers_beats_the_best_single_pool(db: AsyncEngine) -> None:
    await _seed(db)
    tiers = {fee: await _pool(db, A, B, 10_000, 10_000, fee) for fee in FEE_TIERS_BPS}
    await mint(db, A, BOB, 3_010, now=NOW)
    best_single = max(amount_out(3_000, 10_000, 10_000, fee) for fee in FEE_TIERS_BPS)

    quote = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=3_000, config=CONFIG)
    assert len(quote.routes) == 3
    assert quote.amount_out > best_single
    assert sum(route.amount_in for route in quote.routes) == 3_000
    assert sum(route.amount_out for route in quote.routes) == quote.amount_out
    small = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=10, config=CONFIG)
    assert small.amount_out >= max(amount_out(10, 10_000, 10_000, fee) for fee in FEE_TIERS_BPS)

    result = await _swap(db, A, B, 3_000, min_out=quote.amount_out)
    assert (result.amount_out, result.routes) == (quote.amount_out, quote.routes)
    custody = {created.pool_id: created.custody_account_id for created in tiers.values()}
    expected: list[Move] = []
    for route in result.routes:
        (pool_id,) = route.pool_ids
        expected += [(A, BOB, custody[pool_id], route.amount_in), (B, custody[pool_id], BOB, route.amount_out)]
    assert await _moves(db, result.execution_id) == expected
    for route in result.routes:
        assert await _reserves(db, route.pool_ids[0]) == (10_000 + route.amount_in, 10_000 - route.amount_out)
    assert await balance_of(db, BOB, B) == quote.amount_out
    await _check_state(db)


async def test_routes_sharing_a_pool_aggregate_its_transfers(db: AsyncEngine) -> None:
    await _seed(db)
    deep = await _pool(db, A, B, 1_000_000, 1_000_000, FEE_TIERS_BPS[2])
    left = await _pool(db, B, C, 10_000, 10_000, FEE_TIERS_BPS[0])
    right = await _pool(db, B, C, 10_000, 10_000, FEE_TIERS_BPS[1])
    await mint(db, A, BOB, 4_000, now=NOW)
    quote = await swap.quote_swap(db, from_currency=A, to_currency=C, amount_in=4_000, config=CONFIG)
    assert sorted(route.pool_ids for route in quote.routes) == sorted([(deep.pool_id, left.pool_id), (deep.pool_id, right.pool_id)])

    result = await _swap(db, A, C, 4_000, min_out=quote.amount_out)
    assert (result.amount_out, result.routes) == (quote.amount_out, quote.routes)
    moves = await _moves(db, result.execution_id)
    hub = deep.custody_account_id
    by_pool = {route.pool_ids[1]: route for route in result.routes}
    assert len(moves) == 5
    assert moves[0] == (A, BOB, hub, 4_000)
    assert sorted(moves[1:]) == sorted(
        [
            (B, hub, left.custody_account_id, by_pool[left.pool_id].hops[1].amount_in),
            (C, left.custody_account_id, BOB, by_pool[left.pool_id].amount_out),
            (B, hub, right.custody_account_id, by_pool[right.pool_id].hops[1].amount_in),
            (C, right.custody_account_id, BOB, by_pool[right.pool_id].amount_out),
        ]
    )
    sent = sum(route.hops[0].amount_out for route in result.routes)
    assert await _reserves(db, deep.pool_id) == (1_004_000, 1_000_000 - sent)
    await _check_state(db)


async def test_a_pool_crossed_in_both_directions_keeps_custody_equal_to_reserves(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    p1 = await _pool(db, A, B, 50_000, 50_000, FEE_TIERS_BPS[0])
    p2 = await _pool(db, B, C, 50_000, 50_000, FEE_TIERS_BPS[0])
    p3 = await _pool(db, A, B, 50_000, 60_000, FEE_TIERS_BPS[1])
    p4 = await _pool(db, A, C, 50_000, 50_000, FEE_TIERS_BPS[2])
    await mint(db, A, BOB, 5_000, now=NOW)
    forced_routes = [(p1.pool_id, p2.pool_id), (p3.pool_id, p1.pool_id, p4.pool_id)]

    def forced(paths: Sequence[Path], states: Sequence[PoolState], amount_in: int, *, chunk_count: int) -> Allocation:
        index = {path.pool_ids: position for position, path in enumerate(paths)}
        return realize(paths, states, [(index[forced_routes[0]], 3_000), (index[forced_routes[1]], amount_in - 3_000)])

    monkeypatch.setattr(swap, "allocate", forced)
    result = await _swap(db, A, C, 5_000)
    assert sorted(route.pool_ids for route in result.routes) == sorted(forced_routes)
    moves = await _moves(db, result.execution_id)
    hub = p1.custody_account_id
    assert len(moves) == 7
    assert (A, BOB, hub, 3_000) in moves
    assert {(currency_id, source, target) for currency_id, source, target, _ in moves if hub in (source, target)} == {
        (A, BOB, hub),
        (B, hub, p2.custody_account_id),
        (B, p3.custody_account_id, hub),
        (A, hub, p4.custody_account_id),
    }
    assert await balance_of(db, BOB, C) == result.amount_out
    await _check_state(db)


async def test_min_out_above_the_quote_fails_without_moving(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    quote = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=1_000, config=CONFIG)
    await _expect_failed(db, SlippageExceeded, _swap(db, A, B, 1_000, min_out=quote.amount_out + 1))
    assert (await balance_of(db, BOB, A), await balance_of(db, BOB, B)) == (1_000, 0)


async def test_no_route_and_an_amount_too_small(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    with pytest.raises(NoRoute, match="No swap route"):
        await swap.quote_swap(db, from_currency=A, to_currency=D, amount_in=100, config=CONFIG)
    await _expect_failed(db, NoRoute, _swap(db, A, D, 100))
    with pytest.raises(NoRoute, match="too small"):
        await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=1, config=CONFIG)
    await _expect_failed(db, NoRoute, _swap(db, A, B, 1))


async def test_missing_or_deleted_endpoints(db: AsyncEngine) -> None:
    await _seed(db)
    await create_currency(db, currency_id=5, symbol="GONE", now=NOW, deleted_at=NOW)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    for source, target in ((A, 5), (5, A), (A, 99), (99, B)):
        with pytest.raises(CurrencyNotFound):
            await swap.quote_swap(db, from_currency=source, to_currency=target, amount_in=100, config=CONFIG)
        await _expect_failed(db, CurrencyNotFound, _swap(db, source, target, 100))


async def test_malformed_arguments(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    before = await _counts(db)
    for source, target in ((A, A), (0, B), (A, -1)):
        with pytest.raises(InvalidArgument):
            await swap.quote_swap(db, from_currency=source, to_currency=target, amount_in=100, config=CONFIG)
        with pytest.raises(InvalidArgument):
            await _swap(db, source, target, 100)
    bad: Any
    for bad in ("1", 1.5, True):
        with pytest.raises(InvalidAmount):
            await _swap(db, A, B, bad)
        with pytest.raises(InvalidAmount):
            await _swap(db, A, B, 100, min_out=bad)
    assert await _counts(db) == before
    await mint(db, A, BOB, 1_000, now=NOW)
    await _expect_failed(db, InvalidAmount, _swap(db, A, B, 0))
    await _expect_failed(db, InvalidAmount, _swap(db, A, B, 100, min_out=-1))


async def test_insufficient_balance_is_recorded(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 500, now=NOW)
    error = await _expect_failed(db, InsufficientBalance, _swap(db, A, B, 1_000))
    assert "500" in error.message
    # A user who cannot pay is told so before any routing or slippage failure.
    await _expect_failed(db, InsufficientBalance, _swap(db, A, B, 1_000, min_out=10**9))
    await _expect_failed(db, InsufficientBalance, _swap(db, A, D, 1_000))
    await _expect_failed(db, CurrencyNotFound, _swap(db, A, 99, 1_000))
    assert await balance_of(db, BOB, A) == 500


async def test_idempotent_replay_swaps_once(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 2_000, now=NOW)
    first = await _swap(db, A, B, 1_000, idempotency_key="swap-once")
    again = await _swap(db, A, B, 1_000, idempotency_key="swap-once")
    assert again == first
    assert (first.replayed, again.replayed) == (False, True)
    assert await balance_of(db, BOB, A) == 1_000
    with pytest.raises(IdempotencyConflict):
        await _swap(db, A, B, 1_000, min_out=1, idempotency_key="swap-once")
    async with db.connect() as conn:
        swaps = (await conn.execute(select(func.count()).select_from(execution).where(execution.c.type == ExecutionType.SWAP.value))).scalar_one()
    assert swaps == 1
    await _check_state(db)


async def test_max_routes_caps_the_split(db: AsyncEngine) -> None:
    await _seed(db)
    for fee in FEE_TIERS_BPS:
        await _pool(db, A, B, 10_000, 10_000, fee)
    await mint(db, A, BOB, 6_000, now=NOW)
    one = SwapConfig(chunk_count=100, max_routes=1)
    two = SwapConfig(chunk_count=100, max_routes=2)
    single = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=3_000, config=one)
    pair = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=3_000, config=two)
    full = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=3_000, config=CONFIG)
    assert (len(single.routes), len(pair.routes), len(full.routes)) == (1, 2, 3)
    assert single.amount_out < pair.amount_out < full.amount_out
    # The best single path is the cheapest tier.
    assert single.routes[0].fee_bps == (FEE_TIERS_BPS[2],)
    result = await _swap(db, A, B, 3_000, config=one)
    assert (result.amount_out, result.routes) == (single.amount_out, single.routes)
    assert len(await _moves(db, result.execution_id)) == 2
    await _check_state(db)


async def test_truncated_search_is_surfaced(db: AsyncEngine) -> None:
    await _seed(db)
    for fee in FEE_TIERS_BPS:
        await _pool(db, A, B, 10_000, 10_000, fee)
    await _pool(db, A, C, 10_000, 10_000)
    await _pool(db, C, B, 10_000, 10_000)
    full = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=100, config=CONFIG)
    assert not full.truncated
    # Four expansions cover the four pools leaving A, so the two-hop path through C is never examined.
    bounded = SwapConfig(chunk_count=100, max_routes=32, max_expansions=4)
    cut = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=100, config=bounded)
    assert cut.truncated
    assert cut.routes
    assert all(len(route.pool_ids) == 1 for route in cut.routes)
    plan = await swap.plan_swap(db, from_currency=A, to_currency=B, amount_in=100, config=bounded)
    assert plan.truncated
    assert len(plan.paths) == 3


async def test_quote_writes_nothing(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await _pool(db, B, C, 100_000, 100_000)
    before = await _counts(db)
    reserves = await _all_reserves(db)
    quote = await swap.quote_swap(db, from_currency=A, to_currency=C, amount_in=5_000, config=CONFIG)
    assert quote.amount_out > 0
    assert await _counts(db) == before
    assert await _all_reserves(db) == reserves
    async with db.connect() as conn:
        quoted_on_open = await swap.quote_swap(conn, from_currency=A, to_currency=C, amount_in=5_000, config=CONFIG)
        assert not conn.in_transaction()
    assert quoted_on_open == quote


@pytest.fixture
def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    for currency_id in (A, B, C, D):
        info.add_guild(currency_id, OWNER)
    return info


@pytest.mark.parametrize("with_direct_pool", [True, False])
async def test_intermediate_currency_deleted_after_planning(db: AsyncEngine, guilds: InMemoryGuildInfo, monkeypatch: pytest.MonkeyPatch, with_direct_pool: bool) -> None:
    await _seed(db)
    first = await _pool(db, A, B, 1_000_000, 1_000_000)
    second = await _pool(db, B, C, 1_000_000, 1_000_000)
    direct = await _pool(db, A, C, 10_000, 10_000) if with_direct_pool else None
    await mint(db, A, BOB, 1_000, now=NOW)
    await cur.request_delete(db, guilds, currency_id=B, user_id=OWNER, now=NOW)
    original = swap.plan_swap
    plans: list[SwapPlan] = []

    async def planning(*args: Any, **kwargs: Any) -> SwapPlan:
        plan = await original(*args, **kwargs)
        plans.append(plan)
        if len(plans) == 1:
            await cur.finalize_delete(db, guilds, currency_id=B, user_id=OWNER, now=AT)
        return plan

    monkeypatch.setattr(swap, "plan_swap", planning)
    if direct is None:
        await _expect_failed(db, NoRoute, _swap(db, A, C, 1_000), reserves_kept=False)
    else:
        result = await _swap(db, A, C, 1_000)
        assert [route.pool_ids for route in result.routes] == [(direct.pool_id,)]
        assert await _moves(db, result.execution_id) == [(A, BOB, direct.custody_account_id, 1_000), (C, direct.custody_account_id, BOB, result.amount_out)]
    # The dropped pools make the transaction rerun once and plan again on the post-deletion pools.
    planned, replanned = plans
    assert (first.pool_id, second.pool_id) in [path.pool_ids for path in planned.paths]
    assert [path.pool_ids for path in replanned.paths] == ([] if direct is None else [(direct.pool_id,)])
    info = await pools.get_pool(db, first.pool_id)
    assert info is not None and not info.active
    await _check_state(db)


async def test_fees_stay_with_the_liquidity_providers(db: AsyncEngine) -> None:
    await _seed(db)
    created = await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 50_000, now=NOW)
    (start,) = (await pools.list_provider_positions(db, LP)).items
    k = 100_000 * 100_000
    for _ in range(5):
        there = await _swap(db, A, B, 10_000)
        back = await _swap(db, B, A, there.amount_out)
        for _result in (there, back):
            reserve_a, reserve_b = await _reserves(db, created.pool_id)
            assert reserve_a * reserve_b >= k
            k = reserve_a * reserve_b
    (end,) = (await pools.list_provider_positions(db, LP)).items
    assert end.shares == start.shares
    assert end.withdrawable_a > start.withdrawable_a
    assert end.withdrawable_b >= start.withdrawable_b
    await _check_state(db)


async def _swap_moves(engine: AsyncEngine, execution_id: int) -> tuple[dict[str, Any], list[Move]]:
    row = await _execution(engine, execution_id)
    assert row["status"] == ExecutionStatus.SUCCESS
    return row, await _moves(engine, execution_id)


@pytest.mark.parametrize("mode", ["top_level", "after_balance_lock", "child"])
async def test_swap_in_tx_without_a_plan_enumerates_inside_the_transaction(db: AsyncEngine, mode: str) -> None:
    await _seed(db)
    first = await _pool(db, A, B, 100_000, 100_000)
    second = await _pool(db, B, C, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    await mint(db, D, BOB, 10, now=NOW)
    quote = await swap.quote_swap(db, from_currency=A, to_currency=C, amount_in=1_000, config=CONFIG)

    async def inner(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await swap.swap_in_tx(tx, execution_id, user_id=BOB, from_currency=A, to_currency=C, amount_in=1_000, min_out=quote.amount_out, config=CONFIG)
        return result.to_output()

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        if mode == "top_level":
            return await inner(tx, execution_id)
        # An unrelated balance is locked first, as a contract body may do before calling the swap builtin.
        await tx.lock_currencies([D])
        await tx.lock_balances([(BOB, D)])
        if mode == "after_balance_lock":
            return await inner(tx, execution_id)
        child = await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.SWAP, initiator_id=BOB, input={"from_currency": A, "to_currency": C}, body=inner)
        return {"child_execution_id": child.execution_id}

    parent_type = ExecutionType.CONTRACT_CALL if mode == "child" else ExecutionType.SWAP
    op = await run_operation(db, type=parent_type, initiator_id=BOB, input={"mode": mode}, body=body, now=NOW, late_currency_locks=mode == "after_balance_lock")
    swap_execution = op.int_field("child_execution_id") if mode == "child" else op.execution_id
    row, moves = await _swap_moves(db, swap_execution)
    assert row["output"]["amount_out"] == str(quote.amount_out)
    assert row["parent_execution_id"] == (op.execution_id if mode == "child" else None)
    assert [(currency_id, source, target) for currency_id, source, target, _ in moves] == [
        (A, BOB, first.custody_account_id),
        (B, first.custody_account_id, second.custody_account_id),
        (C, second.custody_account_id, BOB),
    ]
    assert await balance_of(db, BOB, C) == quote.amount_out
    await _check_state(db)


async def test_swap_in_tx_after_a_balance_lock_needs_late_currency_locks(db: AsyncEngine) -> None:
    await _seed(db)
    await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    before = await _counts(db)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([D])
        await tx.lock_balances([(BOB, D)])
        return (await swap.swap_in_tx(tx, execution_id, user_id=BOB, from_currency=A, to_currency=B, amount_in=1_000, min_out=0, config=CONFIG)).to_output()

    with pytest.raises(LedgerUsageError, match="after balance rows"):
        await run_operation(db, type=ExecutionType.SWAP, initiator_id=BOB, input={}, body=body, now=NOW)
    assert await _counts(db) == before


async def test_a_pool_dissolved_and_recreated_after_planning_is_found_by_replanning(db: AsyncEngine, guilds: InMemoryGuildInfo, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    old = await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    await cur.request_delete(db, guilds, currency_id=B, user_id=OWNER, now=NOW)
    original = swap.plan_swap
    plans: list[SwapPlan] = []
    recreated: list[pools.PoolCreated] = []

    async def planning(*args: Any, **kwargs: Any) -> SwapPlan:
        plan = await original(*args, **kwargs)
        plans.append(plan)
        if len(plans) == 1:
            await cur.finalize_delete(db, guilds, currency_id=B, user_id=OWNER, now=AT)
            await cur.create_currency(db, guilds, guild_id=B, user_id=OWNER, name="Again", symbol="BBB", initial_supply=0, now=AT)
            recreated.append(await create_pool(db, user_id=LP, currency_x=A, currency_y=B, amount_x=50_000, amount_y=50_000, now=AT))
        return plan

    monkeypatch.setattr(swap, "plan_swap", planning)
    result = await _swap(db, A, B, 1_000)
    (new,) = recreated
    assert [path.pool_ids for path in plans[0].paths] == [(old.pool_id,)]
    assert [path.pool_ids for path in plans[1].paths] == [(new.pool_id,)]
    assert [route.pool_ids for route in result.routes] == [(new.pool_id,)]
    assert result.amount_out == amount_out(1_000, 50_000, 50_000, FEE_TIERS_BPS[0])
    async with db.connect() as conn:
        swaps = (await conn.execute(select(func.count()).select_from(execution).where(execution.c.type == ExecutionType.SWAP.value))).scalar_one()
    assert swaps == 1
    await _check_state(db)


async def test_the_last_run_proceeds_with_the_usable_paths(db: AsyncEngine) -> None:
    await _seed(db)
    real = await _pool(db, A, B, 100_000, 100_000)
    await mint(db, A, BOB, 1_000, now=NOW)
    # Pool 999 does not exist, so every run sees a planned pool missing under the lock.
    stale = SwapPlan(from_currency=A, to_currency=B, paths=(Path(pool_ids=(999,), currencies=(A, B)), Path(pool_ids=(real.pool_id,), currencies=(A, B))), truncated=False)
    runs: list[bool] = []

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        runs.append(tx.rerun_allowed)
        return (await swap.swap_in_tx(tx, execution_id, user_id=BOB, from_currency=A, to_currency=B, amount_in=1_000, min_out=0, config=CONFIG, plan=stale)).to_output()

    op = await run_operation(db, type=ExecutionType.SWAP, initiator_id=BOB, input={}, body=body, now=NOW)
    assert runs == [True] * MAX_RERUNS + [False]
    _, moves = await _swap_moves(db, op.execution_id)
    assert moves == [(A, BOB, real.custody_account_id, 1_000), (B, real.custody_account_id, BOB, amount_out(1_000, 100_000, 100_000, FEE_TIERS_BPS[0]))]
    await _check_state(db)


@pytest.mark.parametrize("seed", range(10))
async def test_swap_pays_at_least_the_best_single_path(db: AsyncEngine, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    await _seed(db)
    pairs = [(x, y) for x in (A, B, C, D) for y in (A, B, C, D) if x < y]
    for x, y in pairs:
        # Every pair gets one to three tiers, so routes share pools and run over several hops.
        for fee in rng.sample(FEE_TIERS_BPS, rng.randint(1, 3)):
            await _pool(db, x, y, rng.randrange(5_000, 200_000), rng.randrange(5_000, 200_000), fee)
    source, target = rng.sample([A, B, C, D], 2)
    amount_in = rng.randrange(100, 100_000)
    await mint(db, source, BOB, amount_in, now=NOW)
    states = await pools.load_active_pools(db)
    plan = await swap.plan_swap(db, from_currency=source, to_currency=target, amount_in=amount_in, config=CONFIG)
    assert any(path.hop_count > 1 for path in plan.paths)
    best = max(realize(plan.paths, states, [(index, amount_in)]).amount_out for index in range(len(plan.paths)))
    quote = await swap.quote_swap(db, from_currency=source, to_currency=target, amount_in=amount_in, config=CONFIG)

    result = await _swap(db, source, target, amount_in)
    assert result.amount_out >= best
    assert (result.amount_out, result.routes) == (quote.amount_out, quote.routes)
    assert await balance_of(db, BOB, target) == result.amount_out
    await _check_state(db)
