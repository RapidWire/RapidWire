from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import pools
from rapidwire.core.accounts import ensure_user_accounts
from rapidwire.core.errors import (
    AmountOverflow,
    CurrencyNotFound,
    DomainError,
    InsufficientBalance,
    InsufficientLiquidity,
    InsufficientShares,
    InvalidAmount,
    InvalidArgument,
    PoolAlreadyExists,
    PoolNotFound,
    SlippageExceeded,
)
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.types import AccountKind, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import FEE_TIERS_BPS, MINIMUM_LIQUIDITY, account, execution, pool, pool_share
from rapidwire.db.tables import transfer as transfer_table
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import balance_of, create_currency, create_pool, mint

NOW = datetime(2026, 8, 1, tzinfo=UTC)
ALICE = 111
BOB = 222
CAROL = 333
DAVE = 444
TIER = FEE_TIERS_BPS[0]


async def _seed(engine: AsyncEngine) -> None:
    for currency_id, symbol in ((1, "AAA"), (2, "BBB"), (3, "CCC")):
        await create_currency(engine, currency_id=currency_id, symbol=symbol, now=NOW)


async def _executions(engine: AsyncEngine, type: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).where(execution.c.type == type).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one())


async def _transfers(engine: AsyncEngine, execution_id: int) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == execution_id).order_by(transfer_table.c.transfer_id))).mappings().all()
    return [dict(row) for row in rows]


async def _shares(engine: AsyncEngine, pool_id: int) -> dict[int, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(pool_share.c.account_id, pool_share.c.shares).where(pool_share.c.pool_id == pool_id))
        return {row.account_id: int(row.shares) for row in rows}


async def _execution_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(execution))).scalar_one())


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def _expect_failed(engine: AsyncEngine, error: type[DomainError], call: Any) -> dict[str, Any]:
    """Await call, expect error, and return its recorded failed execution after checking that it moved nothing."""
    with pytest.raises(error) as raised:
        await call
    assert raised.value.execution_id is not None
    row = await _execution(engine, raised.value.execution_id)
    assert row["status"] == ExecutionStatus.FAILED
    assert row["output"]["error"]["code"] == error.code
    assert await _transfers(engine, raised.value.execution_id) == []
    return row


async def test_create_pool_moves_both_currencies_into_custody(db: AsyncEngine) -> None:
    await _seed(db)
    await mint(db, 1, ALICE, 10_000, now=NOW)
    await mint(db, 2, ALICE, 50_000, now=NOW)
    # The caller names the pair as (2, 1); the pool stores it as (1, 2).
    created = await pools.create_pool(db, user_id=ALICE, currency_x=2, currency_y=1, fee_bps=TIER, amount_x=40_000, amount_y=10_000, now=NOW)
    assert (created.currency_a, created.currency_b, created.currency_x, created.currency_y) == (1, 2, 2, 1)
    assert (created.amount_a, created.amount_b, created.amount_x, created.amount_y) == (10_000, 40_000, 40_000, 10_000)
    assert (created.total_shares, created.locked_shares, created.shares, created.provider_shares) == (20_000, MINIMUM_LIQUIDITY, 19_000, 19_000)
    assert (created.reserve_a, created.reserve_b, created.fee_bps, created.replayed) == (10_000, 40_000, TIER, False)
    custody = created.custody_account_id
    assert custody < -1

    moves = await _transfers(db, created.execution_id)
    assert [(m["currency_id"], m["from_account"], m["to_account"], m["amount"], m["kind"]) for m in moves] == [
        (1, ALICE, custody, 10_000, TransferKind.PRINCIPAL),
        (2, ALICE, custody, 40_000, TransferKind.PRINCIPAL),
    ]
    assert created.transfer_ids == tuple(m["transfer_id"] for m in moves)
    row = await _execution(db, created.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.POOL_CREATE, ExecutionStatus.SUCCESS, ALICE)
    assert row["input"] == {"currency_x": "2", "currency_y": "1", "fee_bps": str(TIER), "amount_x": "40000", "amount_y": "10000"}
    output = row["output"]
    assert all(isinstance(value, str) for key, value in output.items() if key != "transfer_ids")
    assert (output["pool_id"], output["total_shares"], output["custody_account_id"], output["amount_x"]) == (str(created.pool_id), "20000", str(custody), "40000")
    assert output["transfer_ids"] == [str(value) for value in created.transfer_ids]

    assert (await balance_of(db, ALICE, 1), await balance_of(db, ALICE, 2)) == (0, 10_000)
    assert (await balance_of(db, custody, 1), await balance_of(db, custody, 2)) == (10_000, 40_000)
    assert await _shares(db, created.pool_id) == {ALICE: 19_000}
    async with db.connect() as conn:
        stored = (await conn.execute(select(pool).where(pool.c.pool_id == created.pool_id))).mappings().one()
        kind = (await conn.execute(select(account.c.kind, account.c.pool_id).where(account.c.account_id == custody))).one()
    assert (stored["created_by"], stored["created_execution_id"], stored["created_at"], stored["dissolved_at"]) == (ALICE, created.execution_id, NOW, None)
    assert (kind.kind, kind.pool_id) == (AccountKind.POOL_CUSTODY, created.pool_id)
    await _assert_invariants(db)


async def test_create_pool_rules(db: AsyncEngine) -> None:
    await _seed(db)
    first = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=5_000, amount_y=5_000, now=NOW)
    await mint(db, 1, BOB, 100_000, now=NOW)
    await mint(db, 2, BOB, 100_000, now=NOW)

    await _expect_failed(db, PoolAlreadyExists, pools.create_pool(db, user_id=BOB, currency_x=2, currency_y=1, fee_bps=TIER, amount_x=2_000, amount_y=2_000, now=NOW))
    other_tier = await pools.create_pool(db, user_id=BOB, currency_x=2, currency_y=1, fee_bps=FEE_TIERS_BPS[1], amount_x=2_000, amount_y=2_000, now=NOW)
    assert other_tier.pool_id != first.pool_id and other_tier.custody_account_id != first.custody_account_id

    before = await _execution_count(db)
    with pytest.raises(InvalidArgument, match="two different currencies"):
        await pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=1, fee_bps=TIER, amount_x=2_000, amount_y=2_000, now=NOW)
    with pytest.raises(InvalidArgument, match="fee tier"):
        await pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=30, amount_x=2_000, amount_y=2_000, now=NOW)
    with pytest.raises(InvalidAmount):
        await pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=TIER, amount_x=2_000, amount_y="5", now=NOW)  # type: ignore[arg-type]
    assert await _execution_count(db) == before

    await _expect_failed(db, InvalidAmount, pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=TIER, amount_x=0, amount_y=2_000, now=NOW))
    # floor(sqrt(1000 * 1000)) = 1000 does not exceed the locked minimum.
    await mint(db, 3, BOB, 100_000, now=NOW)
    await _expect_failed(db, InsufficientLiquidity, pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=TIER, amount_x=1_000, amount_y=1_000, now=NOW))
    smallest = await pools.create_pool(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=TIER, amount_x=1_001, amount_y=1_001, now=NOW)
    assert (smallest.total_shares, smallest.shares) == (1_001, 1)
    await _expect_failed(db, InsufficientBalance, pools.create_pool(db, user_id=CAROL, currency_x=2, currency_y=3, fee_bps=TIER, amount_x=2_000, amount_y=2_000, now=NOW))
    await _expect_failed(db, CurrencyNotFound, pools.create_pool(db, user_id=BOB, currency_x=2, currency_y=99, fee_bps=TIER, amount_x=2_000, amount_y=2_000, now=NOW))
    await create_currency(db, currency_id=4, symbol="DDD", deleted_at=NOW, now=NOW)
    await _expect_failed(db, CurrencyNotFound, pools.create_pool(db, user_id=BOB, currency_x=4, currency_y=2, fee_bps=TIER, amount_x=2_000, amount_y=2_000, now=NOW))
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(pool))).scalar_one() == 3
    await _assert_invariants(db)


async def test_create_pool_replays_its_idempotency_key(db: AsyncEngine) -> None:
    await _seed(db)
    await mint(db, 1, ALICE, 10_000, now=NOW)
    await mint(db, 2, ALICE, 10_000, now=NOW)
    args: dict[str, Any] = {"user_id": ALICE, "currency_x": 1, "currency_y": 2, "fee_bps": TIER, "amount_x": 4_000, "amount_y": 4_000, "now": NOW, "idempotency_key": "create-1"}
    first = await pools.create_pool(db, **args)
    again = await pools.create_pool(db, **args)
    assert again == first and again.replayed and not first.replayed
    assert len(await _executions(db, ExecutionType.POOL_CREATE)) == 1
    assert await balance_of(db, ALICE, 1) == 6_000
    added = await pools.add_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=1_000, amount_y_desired=1_000, now=NOW, idempotency_key="add-1")
    assert await pools.add_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=1_000, amount_y_desired=1_000, now=NOW, idempotency_key="add-1") == added
    removed = await pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=500, now=NOW, idempotency_key="remove-1")
    replay = await pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=500, now=NOW, idempotency_key="remove-1")
    assert replay == removed and replay.replayed
    assert await _shares(db, first.pool_id) == {ALICE: 3_000 + 1_000 - 500}

    # A failed creation replays as the same error class with the original execution.
    await mint(db, 1, BOB, 5_000, now=NOW)
    await mint(db, 2, BOB, 5_000, now=NOW)
    duplicate: dict[str, Any] = {**args, "user_id": BOB, "idempotency_key": "dup-1"}
    with pytest.raises(PoolAlreadyExists) as failed:
        await pools.create_pool(db, **duplicate)
    with pytest.raises(PoolAlreadyExists) as replayed:
        await pools.create_pool(db, **duplicate)
    assert type(replayed.value) is PoolAlreadyExists
    assert replayed.value.execution_id == failed.value.execution_id is not None
    assert replayed.value.message == failed.value.message
    assert len(await _executions(db, ExecutionType.POOL_CREATE)) == 2
    await _assert_invariants(db)


async def test_concurrent_creators_of_one_pool(db: AsyncEngine) -> None:
    await _seed(db)
    creators = [ALICE, BOB, CAROL, DAVE]
    for user in creators:
        await mint(db, 1, user, 5_000, now=NOW)
        await mint(db, 2, user, 5_000, now=NOW)
    results = await asyncio.gather(
        *(pools.create_pool(db, user_id=user, currency_x=1 + index % 2, currency_y=2 - index % 2, fee_bps=TIER, amount_x=3_000, amount_y=3_000, now=NOW) for index, user in enumerate(creators)),
        return_exceptions=True,
    )
    created = [result for result in results if isinstance(result, pools.PoolCreated)]
    failed = [result for result in results if isinstance(result, BaseException)]
    assert len(created) == 1
    assert len(failed) == 3 and all(isinstance(error, PoolAlreadyExists) and error.execution_id is not None for error in failed)
    rows = await _executions(db, ExecutionType.POOL_CREATE)
    assert sorted(row["status"] for row in rows) == [ExecutionStatus.FAILED] * 3 + [ExecutionStatus.SUCCESS]
    winner = created[0]
    winner_id = (await _execution(db, winner.execution_id))["initiator_id"]
    assert await _shares(db, winner.pool_id) == {winner_id: 2_000}
    for user in creators:
        assert await balance_of(db, user, 1) == (2_000 if user == winner_id else 5_000)
    await _assert_invariants(db)


async def _wait_for_lock_wait(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for _ in range(200):
            if (await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.05)
    raise AssertionError("no transaction started waiting for a lock")


async def test_creator_losing_the_unique_key_race_records_pool_already_exists(db: AsyncEngine) -> None:
    await _seed(db)
    await mint(db, 1, ALICE, 5_000, now=NOW)
    await mint(db, 2, ALICE, 5_000, now=NOW)
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [BOB], now=NOW)
    async with db.connect() as other:
        await other.begin()
        made = await other.execute(insert(execution).values(type=ExecutionType.POOL_CREATE, initiator_id=BOB, status=ExecutionStatus.SUCCESS, input={}, output={}, created_at=NOW))
        assert made.inserted_primary_key is not None
        values = {"currency_a": 1, "currency_b": 2, "fee_bps": TIER, "reserve_a": 1, "reserve_b": 1, "total_shares": MINIMUM_LIQUIDITY, "locked_shares": MINIMUM_LIQUIDITY}
        await other.execute(insert(pool).values(created_by=BOB, created_execution_id=made.inserted_primary_key[0], created_at=NOW, **values))
        task = asyncio.create_task(pools.create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, amount_x=3_000, amount_y=3_000, now=NOW))
        await _wait_for_lock_wait(db)
        await other.commit()
    with pytest.raises(PoolAlreadyExists) as raised:
        await task
    assert raised.value.execution_id is not None
    row = await _execution(db, raised.value.execution_id)
    assert (row["status"], row["output"]["error"]["code"]) == (ExecutionStatus.FAILED, "pool_already_exists")
    assert await _transfers(db, raised.value.execution_id) == []
    assert await balance_of(db, ALICE, 1) == 5_000
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(pool))).scalar_one() == 1


async def test_add_liquidity_at_the_pool_price(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)
    custody = created.custody_account_id
    await mint(db, 1, BOB, 1_000, now=NOW)
    await mint(db, 2, BOB, 5_000, now=NOW)
    # All of currency 1 is used and b_opt = 1000 * 40000 / 10000 = 4000 of currency 2 fits the 5000 offered.
    bob = await pools.add_liquidity(db, user_id=BOB, currency_x=2, currency_y=1, fee_bps=TIER, amount_x_desired=5_000, amount_y_desired=1_000, now=NOW)
    assert (bob.amount_x, bob.amount_y, bob.amount_a, bob.amount_b) == (4_000, 1_000, 1_000, 4_000)
    assert (bob.shares, bob.provider_shares, bob.total_shares, bob.reserve_a, bob.reserve_b) == (2_000, 2_000, 22_000, 11_000, 44_000)
    moves = await _transfers(db, bob.execution_id)
    assert [(m["currency_id"], m["from_account"], m["to_account"], m["amount"]) for m in moves] == [(1, BOB, custody, 1_000), (2, BOB, custody, 4_000)]
    assert (await balance_of(db, BOB, 1), await balance_of(db, BOB, 2)) == (0, 1_000)
    row = await _execution(db, bob.execution_id)
    assert row["input"] == {
        "currency_x": "2",
        "currency_y": "1",
        "fee_bps": str(TIER),
        "amount_x_desired": "5000",
        "amount_y_desired": "1000",
        "amount_x_min": "0",
        "amount_y_min": "0",
        "min_shares": "1",
    }

    await mint(db, 1, CAROL, 1_000, now=NOW)
    await mint(db, 2, CAROL, 2_000, now=NOW)
    carol_args: dict[str, Any] = {"user_id": CAROL, "currency_x": 1, "currency_y": 2, "fee_bps": TIER, "amount_x_desired": 1_000, "amount_y_desired": 2_000, "now": NOW}
    # Currency 2 is short (b_opt 4000 > 2000), so all 2000 are used with a_opt = 2000 * 11000 / 44000 = 500 of currency 1.
    await _expect_failed(db, SlippageExceeded, pools.add_liquidity(db, **carol_args, amount_x_min=501))
    await _expect_failed(db, SlippageExceeded, pools.add_liquidity(db, **carol_args, min_shares=1_001))
    await _expect_failed(db, InvalidArgument, pools.add_liquidity(db, **carol_args, min_shares=0))
    assert (await balance_of(db, CAROL, 1), await balance_of(db, CAROL, 2)) == (1_000, 2_000)
    carol = await pools.add_liquidity(db, **carol_args, amount_x_min=500, amount_y_min=2_000, min_shares=1_000)
    assert (carol.amount_x, carol.amount_y, carol.shares, carol.total_shares, carol.reserve_a, carol.reserve_b) == (500, 2_000, 1_000, 23_000, 11_500, 46_000)
    assert (await balance_of(db, CAROL, 1), await balance_of(db, CAROL, 2)) == (500, 0)
    assert await _shares(db, created.pool_id) == {ALICE: 19_000, BOB: 2_000, CAROL: 1_000}
    await _assert_invariants(db)


async def test_add_liquidity_failures(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)
    # a_opt = floor(1 * 10000 / 40000) = 0, so nothing could be deposited.
    await mint(db, 1, BOB, 10, now=NOW)
    await mint(db, 2, BOB, 10, now=NOW)
    await _expect_failed(db, InsufficientLiquidity, pools.add_liquidity(db, user_id=BOB, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=1, amount_y_desired=1, now=NOW))
    await _expect_failed(db, InsufficientBalance, pools.add_liquidity(db, user_id=DAVE, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=100, amount_y_desired=400, now=NOW))
    await _expect_failed(db, PoolNotFound, pools.add_liquidity(db, user_id=BOB, currency_x=1, currency_y=2, fee_bps=FEE_TIERS_BPS[2], amount_x_desired=10, amount_y_desired=10, now=NOW))
    await _expect_failed(db, PoolNotFound, pools.add_liquidity(db, user_id=BOB, currency_x=1, currency_y=3, fee_bps=TIER, amount_x_desired=10, amount_y_desired=10, now=NOW))
    assert await _shares(db, created.pool_id) == {ALICE: 19_000}
    await _assert_invariants(db)


async def test_add_liquidity_refuses_a_total_beyond_the_amount_range(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)
    # Balances cannot exceed AMOUNT_MAX, so only an inflated share total can reach the limit; the deposit would mint about a tenth of it.
    async with db.begin() as conn:
        await conn.execute(update(pool).where(pool.c.pool_id == created.pool_id).values(total_shares=AMOUNT_MAX - 5))
    await mint(db, 1, BOB, 1_000, now=NOW)
    await mint(db, 2, BOB, 4_000, now=NOW)
    await _expect_failed(db, AmountOverflow, pools.add_liquidity(db, user_id=BOB, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=1_000, amount_y_desired=4_000, now=NOW))
    assert (await balance_of(db, BOB, 1), await balance_of(db, BOB, 2)) == (1_000, 4_000)
    assert await _shares(db, created.pool_id) == {ALICE: 19_000}


async def test_remove_liquidity(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)
    custody = created.custody_account_id
    await create_pool_add(db)
    # Pool now holds 11000 / 44000 with 22000 shares; Bob owns 2000.
    partial = await pools.remove_liquidity(db, user_id=BOB, currency_x=2, currency_y=1, fee_bps=TIER, shares=1_000, now=NOW)
    assert (partial.amount_a, partial.amount_b, partial.amount_x, partial.amount_y) == (500, 2_000, 2_000, 500)
    assert (partial.shares, partial.provider_shares, partial.total_shares, partial.reserve_a, partial.reserve_b) == (1_000, 1_000, 21_000, 10_500, 42_000)
    moves = await _transfers(db, partial.execution_id)
    assert [(m["currency_id"], m["from_account"], m["to_account"], m["amount"]) for m in moves] == [(1, custody, BOB, 500), (2, custody, BOB, 2_000)]
    full = await pools.remove_liquidity(db, user_id=BOB, currency_x=1, currency_y=2, fee_bps=TIER, shares=1_000, amount_x_min=500, amount_y_min=2_000, now=NOW)
    assert (full.amount_a, full.amount_b, full.provider_shares) == (500, 2_000, 0)
    assert await _shares(db, created.pool_id) == {ALICE: 19_000}
    assert (await balance_of(db, BOB, 1), await balance_of(db, BOB, 2)) == (1_000, 5_000)

    await _expect_failed(db, InsufficientShares, pools.remove_liquidity(db, user_id=BOB, currency_x=1, currency_y=2, fee_bps=TIER, shares=1, now=NOW))
    await _expect_failed(db, InsufficientShares, pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=19_001, now=NOW))
    await _expect_failed(db, SlippageExceeded, pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=1_000, amount_x_min=501, now=NOW))
    await _expect_failed(db, PoolNotFound, pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=FEE_TIERS_BPS[1], shares=1, now=NOW))
    await _expect_failed(db, InvalidAmount, pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=0, now=NOW))

    # The creator can withdraw everything except the locked minimum liquidity, which keeps both reserves positive.
    everything = await pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=2, fee_bps=TIER, shares=19_000, now=NOW)
    assert (everything.total_shares, everything.reserve_a, everything.reserve_b) == (MINIMUM_LIQUIDITY, 500, 2_000)
    assert await _shares(db, created.pool_id) == {}
    info = await pools.get_pool(db, created.pool_id)
    assert info is not None and info.active and (info.reserve_a, info.reserve_b, info.total_shares) == (500, 2_000, MINIMUM_LIQUIDITY)
    await _assert_invariants(db)


async def create_pool_add(engine: AsyncEngine) -> None:
    await mint(engine, 1, BOB, 1_000, now=NOW)
    await mint(engine, 2, BOB, 5_000, now=NOW)
    await pools.add_liquidity(engine, user_id=BOB, currency_x=1, currency_y=2, fee_bps=TIER, amount_x_desired=1_000, amount_y_desired=5_000, now=NOW)


async def test_remove_paying_nothing_of_one_side_is_refused(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=3, amount_x=1_000_000, amount_y=1_002, fee_bps=FEE_TIERS_BPS[1], now=NOW)
    # One of 31654 shares is worth floor(1002 / 31654) = 0 of currency 3.
    assert created.total_shares == 31_654
    await _expect_failed(db, InsufficientLiquidity, pools.remove_liquidity(db, user_id=ALICE, currency_x=1, currency_y=3, fee_bps=FEE_TIERS_BPS[1], shares=1, now=NOW))
    await _assert_invariants(db)


async def test_pool_queries(db: AsyncEngine) -> None:
    await _seed(db)
    first = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)
    tier_10 = await create_pool(db, user_id=ALICE, currency_x=2, currency_y=1, amount_x=9_000, amount_y=4_000, fee_bps=10, now=NOW)
    tier_50 = await create_pool(db, user_id=BOB, currency_x=1, currency_y=2, amount_x=2_000, amount_y=2_000, fee_bps=50, now=NOW)
    other = await create_pool(db, user_id=BOB, currency_x=3, currency_y=1, amount_x=3_000, amount_y=3_000, now=NOW)
    await create_pool_add(db)

    page = await pools.list_pools(db, per_page=3)
    assert (page.total, page.pages, [item.pool_id for item in page.items]) == (4, 2, [first.pool_id, tier_10.pool_id, tier_50.pool_id])
    second = await pools.list_pools(db, page=2, per_page=3)
    assert [item.pool_id for item in second.items] == [other.pool_id]
    with pytest.raises(InvalidArgument):
        await pools.list_pools(db, per_page=pools.POOLS_PER_PAGE_MAX + 1)

    pair = await pools.get_pools_for_pair(db, 2, 1)
    assert [(item.pool_id, item.fee_bps) for item in pair] == [(tier_10.pool_id, 10), (tier_50.pool_id, 50), (first.pool_id, 100)]
    assert pair == await pools.get_pools_for_pair(db, 1, 2)
    assert await pools.get_pools_for_pair(db, 2, 3) == ()
    with pytest.raises(InvalidArgument):
        await pools.get_pools_for_pair(db, 2, 2)

    info = await pools.get_pool(db, first.pool_id)
    assert info is not None
    assert (info.currency_a, info.currency_b, info.symbol_a, info.symbol_b, info.deleted_a, info.deleted_b, info.fee_bps) == (1, 2, "AAA", "BBB", False, False, TIER)
    assert (info.reserve_a, info.reserve_b, info.total_shares, info.locked_shares) == (11_000, 44_000, 22_000, MINIMUM_LIQUIDITY)
    assert (info.custody_account_id, info.created_by, info.created_at, info.created_execution_id) == (first.custody_account_id, ALICE, NOW, first.execution_id)
    assert (info.dissolved_at, info.dissolved_execution_id, info.active) == (None, None, True)
    assert await pools.get_pool(db, 999) is None
    async with db.connect() as conn:
        assert await pools.custody_account_id(conn, first.pool_id) == first.custody_account_id
        assert await pools.custody_account_id(conn, 999) is None

    positions = await pools.list_provider_positions(db, ALICE)
    assert [item.pool.pool_id for item in positions.items] == [first.pool_id, tier_10.pool_id]
    main = positions.items[0]
    assert (main.account_id, main.shares, main.pool.total_shares) == (ALICE, 19_000, 22_000)
    assert (main.withdrawable_a, main.withdrawable_b, main.share_percent) == (9_500, 38_000, "86.36")
    bob = await pools.list_provider_positions(db, BOB, per_page=1)
    assert (bob.total, len(bob.items)) == (3, 1)
    bob_all = await pools.list_provider_positions(db, BOB)
    by_pool = {item.pool.pool_id: item for item in bob_all.items}
    assert (by_pool[first.pool_id].shares, by_pool[first.pool_id].share_percent) == (2_000, "9.09")
    assert (by_pool[first.pool_id].withdrawable_a, by_pool[first.pool_id].withdrawable_b) == (1_000, 4_000)
    assert (await pools.list_provider_positions(db, DAVE)).total == 0

    states = await pools.load_active_pools(db)
    assert [(state.pool_id, state.currency_a, state.currency_b, state.fee_bps, state.reserve_a, state.reserve_b) for state in states] == [
        (first.pool_id, 1, 2, 100, 11_000, 44_000),
        (tier_10.pool_id, 1, 2, 10, 4_000, 9_000),
        (tier_50.pool_id, 1, 2, 50, 2_000, 2_000),
        (other.pool_id, 1, 3, 100, 3_000, 3_000),
    ]
    await _assert_invariants(db)


async def test_lock_helpers(db: AsyncEngine) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=1, currency_y=2, amount_x=10_000, amount_y=40_000, now=NOW)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([1, 2])
        locked = await pools.lock_pools(tx, [created.pool_id, 999])
        assert list(locked) == [created.pool_id]
        entry = locked[created.pool_id]
        assert (entry.active, entry.custody_account_id, entry.state().reserve_of(2)) == (True, created.custody_account_id, 40_000)
        with pytest.raises(Exception, match="positive"):
            await pools.set_reserves(tx, created.pool_id, 0, 1)
        with pytest.raises(Exception, match="supported range"):
            await pools.set_reserves(tx, created.pool_id, 10**38, 1)
        return {}

    await run_operation(db, type=ExecutionType.SWAP, initiator_id=ALICE, input={}, body=body, now=NOW)

    async def unlocked(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await pools.lock_pools(tx, [created.pool_id])
        return {}

    with pytest.raises(Exception, match="must be locked"):
        await run_operation(db, type=ExecutionType.SWAP, initiator_id=ALICE, input={}, body=unlocked, now=NOW)
    await _assert_invariants(db)
