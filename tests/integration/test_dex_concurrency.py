"""DEX concurrency tests: storms of swaps in both directions over shared pools and multi-hop routes, interleaved with liquidity changes, pool creation and a currency deletion, plus overspending and same-key swap storms, run on separate pooled connections, must never spend a balance twice and must keep every pool's custody equal to its reserves, every balance non-negative and every ledger invariant."""

from __future__ import annotations

import asyncio
import contextvars
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import currency as cur
from rapidwire.core import ledger, pools, swap
from rapidwire.core.errors import CurrencyNotFound, DomainError, InsufficientBalance, InsufficientShares, NoRoute, PoolAlreadyExists, PoolNotFound, SlippageExceeded
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.swap import SwapConfig, SwapPlan, SwapResult
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import FEE_TIERS_BPS, account, balance, execution, pool
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.factories import balance_of, create_currency, mint, supply_of
from tests.integration.opmodel import assert_failed_execution

T0 = datetime(2026, 9, 1, tzinfo=UTC)
AT = T0 + timedelta(days=7)
STORM_TIMEOUT_SECONDS = 90
CONFIG = SwapConfig(chunk_count=100, max_routes=32)
OWNER, LP, LP2 = 100, 101, 102
TRADERS = list(range(201, 209))
NEWCOMERS = list(range(301, 307))
SPENDER = 401
A, B, C, D = 11, 12, 13, 14
HIGH, MID, LOW = FEE_TIERS_BPS
FUNDING = 1_000_000


@pytest_asyncio.fixture(loop_scope="session")
async def wide(db: AsyncEngine, migrated_url: URL) -> AsyncIterator[AsyncEngine]:
    """An engine on the emptied test database with a pool large enough for every storm to hold its own connections."""
    eng = create_engine(migrated_url, pool_size=40, max_overflow=40)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every deadlock or lock wait timeout retry: retry_delay() is replaced by a counting wrapper in each rapidwire module that imported it."""
    counted: list[int] = []
    original = ledger.retry_delay

    def counting(attempt: int) -> float:
        counted.append(attempt)
        return original(attempt)

    patched = [module for name, module in list(sys.modules.items()) if name.startswith("rapidwire.") and getattr(module, "retry_delay", None) is original]
    assert ledger in patched
    for module in patched:
        monkeypatch.setattr(module, "retry_delay", counting)
    return counted


async def _storm(calls: Iterable[Awaitable[Any]]) -> list[Any]:
    """Run the calls concurrently and return each result or DomainError; any other exception fails the test."""
    async with asyncio.timeout(STORM_TIMEOUT_SECONDS):
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, DomainError):
            raise outcome
    return outcomes


async def _check_failures(engine: AsyncEngine, outcomes: Iterable[Any], allowed: tuple[type[DomainError], ...]) -> None:
    for outcome in outcomes:
        if isinstance(outcome, DomainError):
            assert isinstance(outcome, allowed), f"unexpected {type(outcome).__name__}: {outcome.message}"
            await assert_failed_execution(engine, outcome)


async def _assert_dex_ok(engine: AsyncEngine, currencies: Iterable[int]) -> None:
    """Every ledger invariant holds, no user balance is negative, every currency's balances sum to 0 and every active pool's custody holds exactly its reserves."""
    report = await verify_invariants(engine)
    assert report.ok, report.violations
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(balance).where(balance.c.account_id > 0, balance.c.amount < 0))).scalar_one() == 0
        for currency_id in currencies:
            assert (await conn.execute(select(func.coalesce(func.sum(balance.c.amount), 0)).where(balance.c.currency_id == currency_id))).scalar_one() == 0
        rows = (await conn.execute(select(pool, account.c.account_id).join(account, account.c.pool_id == pool.c.pool_id).where(pool.c.dissolved_at.is_(None)))).mappings().all()
    for row in rows:
        custody = row["account_id"]
        assert (await balance_of(engine, custody, row["currency_a"]), await balance_of(engine, custody, row["currency_b"])) == (row["reserve_a"], row["reserve_b"])


async def _seed(engine: AsyncEngine, currencies: dict[int, str]) -> None:
    for currency_id, symbol in currencies.items():
        await create_currency(engine, currency_id=currency_id, symbol=symbol, now=T0)
        for user_id in (LP, LP2, *TRADERS):
            await mint(engine, currency_id, user_id, FUNDING, now=T0)


async def _create(engine: AsyncEngine, x: int, y: int, fee_bps: int, amount_x: int, amount_y: int, *, user_id: int = LP, now: datetime = T0) -> pools.PoolCreated:
    return await pools.create_pool(engine, user_id=user_id, currency_x=x, currency_y=y, fee_bps=fee_bps, amount_x=amount_x, amount_y=amount_y, now=now)


def _check_swaps(planned: list[tuple[str, tuple[Any, ...]]], outcomes: list[Any]) -> list[SwapResult]:
    """Every successful swap paid at least its min_out and spent exactly its input; return the successes."""
    done: list[SwapResult] = []
    for (kind, args), outcome in zip(planned, outcomes, strict=True):
        if kind == "swap" and isinstance(outcome, SwapResult):
            _, source, target, amount_in, min_out = args
            assert (outcome.from_currency, outcome.to_currency, outcome.amount_in) == (source, target, amount_in)
            assert outcome.amount_out >= min_out
            assert sum(route.amount_in for route in outcome.routes) == amount_in
            done.append(outcome)
    return done


async def test_swap_and_liquidity_storm_keeps_pools_consistent(wide: AsyncEngine, retries: list[int]) -> None:
    await _seed(wide, {A: "AAA", B: "BBB", C: "CCC", D: "DDD"})
    for x, y, fee, amount_x, amount_y in (
        (A, B, HIGH, 200_000, 200_000),
        (A, B, MID, 100_000, 120_000),
        (B, C, HIGH, 150_000, 100_000),
        (A, C, LOW, 80_000, 80_000),
        (C, D, HIGH, 100_000, 100_000),
        (B, D, MID, 90_000, 110_000),
    ):
        await _create(wide, x, y, fee, amount_x, amount_y)
    pairs = [(A, B), (B, A), (A, C), (C, A), (A, D), (D, A), (B, D), (D, C), (C, B)]
    rng = random.Random(9101)  # noqa: S311
    planned: list[tuple[str, tuple[Any, ...]]] = []
    for index in range(48):
        source, target = pairs[index % len(pairs)]
        amount_in = rng.randrange(100, 5_000)
        quote = await swap.quote_swap(wide, from_currency=source, to_currency=target, amount_in=amount_in, config=CONFIG)
        # Every third swap demands its pre-storm quote less 1%, so some of them fall short once others move the price.
        min_out = quote.amount_out * 99 // 100 if index % 3 == 0 else 0
        planned.append(("swap", (TRADERS[index % len(TRADERS)], source, target, amount_in, min_out)))
    # Newcomers hold only A, so each of their swaps creates the target balance row inside lock_balances().
    for user_id in NEWCOMERS:
        await mint(wide, A, user_id, 20_000, now=T0)
    async with wide.connect() as conn:
        held = (await conn.execute(select(func.count()).select_from(balance).where(balance.c.account_id.in_(NEWCOMERS), balance.c.currency_id != A))).scalar_one()
    assert held == 0
    for index, user_id in enumerate(NEWCOMERS):
        for target in (B, C, D)[index % 3 :] + (B, C, D)[: index % 3]:
            planned.append(("swap", (user_id, A, target, rng.randrange(100, 5_000), 0)))
    for x, y, fee in ((A, B, HIGH), (C, B, HIGH), (D, C, HIGH), (B, D, MID)):
        planned += [("add", (LP2, x, y, fee, rng.randrange(1_000, 20_000), rng.randrange(1_000, 20_000))) for _ in range(3)]
        planned += [("remove", (LP, x, y, fee, rng.randrange(100, 5_000))) for _ in range(2)]
    planned += [("create", (A, D, HIGH)), ("create", (D, A, HIGH)), ("create", (C, A, MID))]
    planned.append(("swap", (TRADERS[0], A, B, 10 * FUNDING, 0)))
    rng.shuffle(planned)
    waits_before = await _row_lock_waits(wide)

    def call(kind: str, args: tuple[Any, ...]) -> Awaitable[Any]:
        if kind == "swap":
            user_id, source, target, amount_in, min_out = args
            return swap.swap(wide, user_id=user_id, from_currency=source, to_currency=target, amount_in=amount_in, min_out=min_out, config=CONFIG, now=T0)
        if kind == "add":
            user_id, x, y, fee, amount_x, amount_y = args
            return pools.add_liquidity(wide, user_id=user_id, currency_x=x, currency_y=y, fee_bps=fee, amount_x_desired=amount_x, amount_y_desired=amount_y, now=T0)
        if kind == "remove":
            user_id, x, y, fee, shares = args
            return pools.remove_liquidity(wide, user_id=user_id, currency_x=x, currency_y=y, fee_bps=fee, shares=shares, now=T0)
        x, y, fee = args
        return _create(wide, x, y, fee, 50_000, 50_000, user_id=LP2)

    outcomes = await _storm(call(kind, args) for kind, args in planned)

    assert await _row_lock_waits(wide) > waits_before
    assert retries == []
    await _check_failures(wide, outcomes, (SlippageExceeded, InsufficientBalance, PoolAlreadyExists))
    done = _check_swaps(planned, outcomes)
    assert len(done) >= 45
    for user_id in NEWCOMERS:
        for target in (B, C, D):
            assert await balance_of(wide, user_id, target) > 0
    assert any(len(route.pool_ids) > 1 for result in done for route in result.routes)
    assert any(len(result.routes) > 1 for result in done)
    assert sum(1 for (kind, _), outcome in zip(planned, outcomes, strict=True) if kind == "create" and isinstance(outcome, pools.PoolCreated)) == 2
    assert sum(1 for outcome in outcomes if isinstance(outcome, InsufficientBalance)) >= 1
    assert sum(1 for (kind, _), outcome in zip(planned, outcomes, strict=True) if kind in ("add", "remove") and not isinstance(outcome, DomainError)) == 20
    await _assert_dex_ok(wide, (A, B, C, D))


async def _exclusive_lock_seen(engine: AsyncEngine, currency_id: int, done: asyncio.Event) -> bool:
    """Return True once some transaction holds the currency row FOR UPDATE, or False once done is set first."""
    stmt = text("SELECT COUNT(*) FROM performance_schema.data_locks WHERE OBJECT_NAME = 'currency' AND LOCK_TYPE = 'RECORD' AND LOCK_MODE LIKE 'X%' AND LOCK_DATA = :id")
    async with engine.connect() as conn:
        while not done.is_set():
            if (await conn.execute(stmt, {"id": str(currency_id)})).scalar_one():
                return True
            await conn.rollback()
            await asyncio.sleep(0.002)
    return False


async def _row_lock_waits(engine: AsyncEngine) -> int:
    """The server-wide count of row lock waits since start."""
    async with engine.connect() as conn:
        row = (await conn.execute(text("SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits'"))).one()
    return int(row[1])


_LATE: contextvars.ContextVar[bool] = contextvars.ContextVar("late_swap", default=False)


async def test_currency_deletion_racing_swaps(wide: AsyncEngine, retries: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(wide, {A: "AAA", B: "BBB", C: "CCC", D: "DOOM"})
    guilds = InMemoryGuildInfo()
    guilds.add_guild(D, OWNER)
    # D is a cheap hub between A and B and between A and C, so routes through it are preferred until it is deleted.
    doomed = [await _create(wide, *spec) for spec in ((A, D, LOW, 300_000, 300_000), (D, B, LOW, 300_000, 300_000), (C, D, LOW, 300_000, 300_000))]
    for spec in ((A, B, HIGH, 100_000, 100_000), (B, C, HIGH, 100_000, 100_000)):
        await _create(wide, *spec)
    await cur.request_delete(wide, guilds, currency_id=D, user_id=OWNER, now=T0)
    doomed_ids = {created.pool_id for created in doomed}
    pairs = [(A, B), (B, A), (A, C), (C, B), (A, D), (D, B)]
    rng = random.Random(9102)  # noqa: S311
    started = asyncio.Event()
    finished = asyncio.Event()

    def swap_call(index: int) -> Awaitable[Any]:
        source, target = pairs[index % len(pairs)]
        return swap.swap(wide, user_id=TRADERS[index % len(TRADERS)], from_currency=source, to_currency=target, amount_in=rng.randrange(100, 3_000), min_out=0, config=CONFIG, now=AT)

    def liquidity_call(index: int) -> Awaitable[Any]:
        if index % 2:
            return pools.add_liquidity(wide, user_id=LP2, currency_x=D, currency_y=B, fee_bps=LOW, amount_x_desired=2_000, amount_y_desired=2_000, now=AT)
        return pools.remove_liquidity(wide, user_id=LP, currency_x=A, currency_y=D, fee_bps=LOW, shares=1_000, now=AT)

    async def early(index: int) -> Any:
        result = await (swap_call(index) if index % 5 else liquidity_call(index))
        started.set()
        return result

    async def finalize() -> cur.CurrencyDeleted:
        await started.wait()
        try:
            return await cur.finalize_delete(wide, guilds, currency_id=D, user_id=OWNER, now=AT)
        finally:
            finished.set()

    seen: list[bool] = []
    late_plans: list[SwapPlan] = []
    original_plan = swap.plan_swap

    async def recording_plan(*args: Any, **kwargs: Any) -> SwapPlan:
        plan = await original_plan(*args, **kwargs)
        if _LATE.get():
            late_plans.append(plan)
        return plan

    monkeypatch.setattr(swap, "plan_swap", recording_plan)

    async def late(index: int) -> Any:
        seen.append(await _exclusive_lock_seen(wide, D, finished))
        _LATE.set(True)
        return await swap_call(index)

    early_range, late_range = range(30), range(30, 60)
    outcomes = await _storm([finalize(), *(early(index) for index in early_range), *(late(index) for index in late_range)])

    deleted, early_outcomes, late_outcomes = outcomes[0], outcomes[1 : 1 + len(early_range)], outcomes[1 + len(early_range) :]
    assert isinstance(deleted, cur.CurrencyDeleted)
    assert any(seen)
    # Late swaps planned on the pre-deletion pools while the deletion held its lock, then dropped the dissolved pools under their own locks.
    assert any(doomed_ids & set(plan.pool_ids) for plan in late_plans)
    assert retries == []
    await _check_failures(wide, early_outcomes, (CurrencyNotFound, PoolNotFound, InsufficientShares))
    await _check_failures(wide, late_outcomes, (CurrencyNotFound, NoRoute))
    assert sum(1 for outcome in early_outcomes if isinstance(outcome, SwapResult)) >= 1
    for index, outcome in zip(late_range, late_outcomes, strict=True):
        source, target = pairs[index % len(pairs)]
        if D in (source, target):
            assert isinstance(outcome, CurrencyNotFound)
        else:
            assert isinstance(outcome, SwapResult), outcome
            assert not doomed_ids & {pool_id for route in outcome.routes for pool_id in route.pool_ids}
    for created in doomed:
        info = await pools.get_pool(wide, created.pool_id)
        assert info is not None and not info.active
    assert await supply_of(wide, D) == 0
    await _assert_dex_ok(wide, (A, B, C, D))


async def test_overspend_storm_succeeds_exactly_as_often_as_the_balance_covers(wide: AsyncEngine, retries: list[int]) -> None:
    await _seed(wide, {A: "AAA", B: "BBB", C: "CCC"})
    # Two direct tiers and a cheap two-hop path through C, so the swaps split and take multi-hop routes.
    for spec in ((A, B, HIGH, 100_000, 100_000), (A, B, MID, 100_000, 100_000), (A, C, LOW, 100_000, 100_000), (C, B, LOW, 100_000, 100_000)):
        await _create(wide, *spec)
    covered, amount_in = 10, 1_000
    await mint(wide, A, SPENDER, covered * amount_in, now=T0)

    outcomes = await _storm(swap.swap(wide, user_id=SPENDER, from_currency=A, to_currency=B, amount_in=amount_in, min_out=0, config=CONFIG, now=T0) for _ in range(3 * covered))

    assert retries == []
    await _check_failures(wide, outcomes, (InsufficientBalance,))
    done = [outcome for outcome in outcomes if isinstance(outcome, SwapResult)]
    assert (len(done), sum(1 for outcome in outcomes if isinstance(outcome, InsufficientBalance))) == (covered, 2 * covered)
    assert all(result.amount_in == amount_in == sum(route.amount_in for route in result.routes) for result in done)
    assert any(len(result.routes) > 1 for result in done)
    assert any(len(route.pool_ids) > 1 for result in done for route in result.routes)
    assert await balance_of(wide, SPENDER, A) == 0
    assert await balance_of(wide, SPENDER, B) == sum(result.amount_out for result in done)
    await _assert_dex_ok(wide, (A, B, C))


async def test_swap_idempotency_key_storms_execute_once(wide: AsyncEngine) -> None:
    await _seed(wide, {A: "AAA", B: "BBB"})
    await _create(wide, A, B, MID, 100_000, 100_000)
    await mint(wide, A, SPENDER, 5_000, now=T0)

    def keyed(key: str, amount_in: int) -> Awaitable[Any]:
        return swap.swap(wide, user_id=SPENDER, from_currency=A, to_currency=B, amount_in=amount_in, min_out=0, config=CONFIG, now=T0, idempotency_key=key)

    swapped = await _storm(keyed("storm-swap", 1_000) for _ in range(20))
    assert all(isinstance(outcome, SwapResult) for outcome in swapped)
    first = swapped[0]
    # SwapResult equality ignores replayed, so every caller sees the one execution's result and all but one are replays.
    assert all(outcome == first for outcome in swapped)
    assert sum(1 for outcome in swapped if not outcome.replayed) == 1

    refused = await _storm(keyed("storm-refused", 10**6) for _ in range(20))
    assert all(isinstance(outcome, InsufficientBalance) for outcome in refused)
    assert len({outcome.execution_id for outcome in refused}) == 1
    await _check_failures(wide, refused, (InsufficientBalance,))

    async with wide.connect() as conn:
        keyed_rows = (await conn.execute(select(execution.c.idempotency_key, func.count()).where(execution.c.idempotency_key.is_not(None)).group_by(execution.c.idempotency_key))).all()
        moved = (await conn.execute(select(func.count()).select_from(transfer_table).where(transfer_table.c.execution_id == first.execution_id))).scalar_one()
    assert dict(keyed_rows) == {"storm-swap": 1, "storm-refused": 1}
    assert moved == len(first.transfer_ids)
    assert await balance_of(wide, SPENDER, A) == 5_000 - 1_000
    assert await balance_of(wide, SPENDER, B) == first.amount_out
    await _assert_dex_ok(wide, (A, B))
