from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.accounts import create_pool_custody_account, ensure_user_accounts, pool_custody_account_id
from rapidwire.core.types import AccountKind
from rapidwire.db.migrate import head_revision, make_config
from rapidwire.db.tables import FEE_DENOMINATOR, FEE_TIERS_BPS, MINIMUM_LIQUIDITY, account, execution, metadata, pool, pool_share
from tests.integration.factories import create_currency

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = 111
BOB = 222


async def _seed(engine: AsyncEngine) -> int:
    """Create currencies 1, 2 and 3 and the users ALICE and BOB, and return an execution ID to reference from pool rows."""
    for currency_id, symbol in ((1, "AAA"), (2, "BBB"), (3, "CCC")):
        await create_currency(engine, currency_id=currency_id, symbol=symbol)
    async with engine.begin() as conn:
        await ensure_user_accounts(conn, [ALICE, BOB], now=NOW)
        result = await conn.execute(insert(execution).values(type="pool_create", initiator_id=ALICE, status="success", input={}, created_at=NOW))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])


def _pool_row(execution_id: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "currency_a": 1,
        "currency_b": 2,
        "fee_bps": 100,
        "reserve_a": 5_000,
        "reserve_b": 7_000,
        "total_shares": MINIMUM_LIQUIDITY + 4_000,
        "locked_shares": MINIMUM_LIQUIDITY,
        "created_by": ALICE,
        "created_execution_id": execution_id,
        "created_at": NOW,
    }
    return {**row, **overrides}


async def _insert_pool(engine: AsyncEngine, execution_id: int, **overrides: Any) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(insert(pool).values(**_pool_row(execution_id, **overrides)))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])


async def _expect_pool_rejected(engine: AsyncEngine, execution_id: int, match: str, **overrides: Any) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match=match):
            await conn.execute(insert(pool).values(**_pool_row(execution_id, **overrides)))
        await conn.rollback()


async def _dissolve(engine: AsyncEngine, pool_id: int, execution_id: int) -> None:
    async with engine.begin() as conn:
        values = {"reserve_a": 0, "reserve_b": 0, "total_shares": 0, "locked_shares": 0, "dissolved_at": NOW, "dissolved_execution_id": execution_id}
        await conn.execute(update(pool).where(pool.c.pool_id == pool_id).values(**values))


def test_fee_constants() -> None:
    assert FEE_DENOMINATOR == 10_000
    assert FEE_TIERS_BPS == (100, 50, 10)
    assert MINIMUM_LIQUIDITY == 1000


def test_tables_sort_without_cycle_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        names = [table.name for table in metadata.sorted_tables]
    assert names.index("account") < names.index("pool") < names.index("pool_share")
    assert names.index("currency") < names.index("pool")
    assert names.index("execution") < names.index("pool")


async def test_currencies_must_be_distinct_and_ordered(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    await _expect_pool_rejected(db, execution_id, "ck_pool_currency_order", currency_a=1, currency_b=1)
    await _expect_pool_rejected(db, execution_id, "ck_pool_currency_order", currency_a=2, currency_b=1)
    await _expect_pool_rejected(db, execution_id, "fk_pool_currency_b_currency", currency_a=1, currency_b=9)


@pytest.mark.parametrize("fee_bps", [0, 1, 30, 101, 10_000, -100])
async def test_invalid_fee_tier_rejected(db: AsyncEngine, fee_bps: int) -> None:
    execution_id = await _seed(db)
    await _expect_pool_rejected(db, execution_id, "ck_pool_fee_tier", fee_bps=fee_bps)


async def test_each_fee_tier_is_a_separate_pool(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    for fee_bps in FEE_TIERS_BPS:
        await _insert_pool(db, execution_id, fee_bps=fee_bps)
    await _insert_pool(db, execution_id, currency_a=2, currency_b=3)
    async with db.connect() as conn:
        rows = (await conn.execute(select(pool.c.currency_a, pool.c.currency_b, pool.c.fee_bps, pool.c.active).order_by(pool.c.pool_id))).all()
    assert [tuple(row) for row in rows] == [(1, 2, 100, True), (1, 2, 50, True), (1, 2, 10, True), (2, 3, 100, True)]


@pytest.mark.parametrize("column", ["reserve_a", "reserve_b", "total_shares", "locked_shares"])
async def test_negative_amounts_rejected(db: AsyncEngine, column: str) -> None:
    execution_id = await _seed(db)
    # Every negative amount also violates the state CHECK, so either constraint may be the one reported.
    await _expect_pool_rejected(db, execution_id, f"ck_pool_{column}_non_negative|ck_pool_state", **{column: -1})


@pytest.mark.parametrize(
    "overrides",
    [
        {"reserve_a": 0},
        {"reserve_b": 0},
        {"reserve_a": 0, "reserve_b": 0},
        {"locked_shares": 0, "total_shares": 0},
        {"locked_shares": MINIMUM_LIQUIDITY - 1},
        {"locked_shares": MINIMUM_LIQUIDITY + 1, "total_shares": MINIMUM_LIQUIDITY + 10},
        {"total_shares": MINIMUM_LIQUIDITY - 1},
    ],
)
async def test_active_pool_state_rejected(db: AsyncEngine, overrides: dict[str, int]) -> None:
    execution_id = await _seed(db)
    await _expect_pool_rejected(db, execution_id, "ck_pool_state", **overrides)


async def test_active_pool_with_only_locked_shares_allowed(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    await _insert_pool(db, execution_id, total_shares=MINIMUM_LIQUIDITY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reserve_a": 1, "reserve_b": 0, "total_shares": 0, "locked_shares": 0},
        {"reserve_a": 0, "reserve_b": 1, "total_shares": 0, "locked_shares": 0},
        {"reserve_a": 0, "reserve_b": 0, "total_shares": MINIMUM_LIQUIDITY, "locked_shares": MINIMUM_LIQUIDITY},
        {"reserve_a": 0, "reserve_b": 0, "total_shares": 0, "locked_shares": MINIMUM_LIQUIDITY},
    ],
)
async def test_dissolved_pool_state_rejected(db: AsyncEngine, overrides: dict[str, int]) -> None:
    execution_id = await _seed(db)
    await _expect_pool_rejected(db, execution_id, "ck_pool_state", dissolved_at=NOW, dissolved_execution_id=execution_id, **overrides)


async def test_dissolution_needs_time_and_execution(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    empty = {"reserve_a": 0, "reserve_b": 0, "total_shares": 0, "locked_shares": 0}
    await _expect_pool_rejected(db, execution_id, "ck_pool_dissolution_pair", dissolved_at=NOW, **empty)
    await _expect_pool_rejected(db, execution_id, "ck_pool_dissolution_pair", dissolved_execution_id=execution_id)
    await _expect_pool_rejected(db, execution_id, "fk_pool_dissolved_execution_id_execution", dissolved_at=NOW, dissolved_execution_id=execution_id + 100, **empty)


async def test_one_active_pool_per_pair_and_tier(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    first = await _insert_pool(db, execution_id)
    with pytest.raises(IntegrityError, match="uq_pool_currency_a_currency_b_fee_bps_active"):
        await _insert_pool(db, execution_id)
    await _dissolve(db, first, execution_id)
    second = await _insert_pool(db, execution_id)
    await _dissolve(db, second, execution_id)
    third = await _insert_pool(db, execution_id)
    async with db.connect() as conn:
        rows = (await conn.execute(select(pool.c.pool_id, pool.c.active).order_by(pool.c.pool_id))).all()
    assert [tuple(row) for row in rows] == [(first, None), (second, None), (third, True)]
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="uq_pool_currency_a_currency_b_fee_bps_active"):
            revived = {"reserve_a": 1, "reserve_b": 1, "total_shares": MINIMUM_LIQUIDITY, "locked_shares": MINIMUM_LIQUIDITY, "dissolved_at": None, "dissolved_execution_id": None}
            await conn.execute(update(pool).where(pool.c.pool_id == first).values(**revived))
        await conn.rollback()


async def test_pool_share_checks_and_keys(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    pool_id = await _insert_pool(db, execution_id)
    async with db.begin() as conn:
        await conn.execute(insert(pool_share).values(pool_id=pool_id, account_id=ALICE, shares=4_000, updated_at=NOW))
    sql = "INSERT INTO pool_share (pool_id, account_id, shares, updated_at) VALUES (:p, :a, :s, UTC_TIMESTAMP(6))"
    for shares in (0, -1):
        async with db.connect() as conn:
            with pytest.raises(DBAPIError, match="ck_pool_share_shares_positive"):
                await conn.execute(text(sql), {"p": pool_id, "a": BOB, "s": shares})
            await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="Duplicate entry"):
            await conn.execute(text(sql), {"p": pool_id, "a": ALICE, "s": 1})
        await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_pool_share_pool_id_pool"):
            await conn.execute(text(sql), {"p": pool_id + 100, "a": BOB, "s": 1})
        await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_pool_share_account_id_account"):
            await conn.execute(text(sql), {"p": pool_id, "a": 333, "s": 1})
        await conn.rollback()


async def test_pool_custody_account_references_its_pool(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    pool_id = await _insert_pool(db, execution_id)
    async with db.begin() as conn:
        custody = await create_pool_custody_account(conn, pool_id, now=NOW)
        assert await create_pool_custody_account(conn, pool_id, now=NOW) == custody
        assert await pool_custody_account_id(conn, pool_id) == custody
    assert custody < -1
    async with db.connect() as conn:
        row = (await conn.execute(select(account.c.kind, account.c.pool_id).where(account.c.account_id == custody))).one()
    assert (AccountKind(row.kind), row.pool_id) == (AccountKind.POOL_CUSTODY, pool_id)
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_account_pool_id_pool"):
            await create_pool_custody_account(conn, pool_id + 100, now=NOW)
        await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="fk_account_pool_id_pool"):
            await conn.execute(text("DELETE FROM pool WHERE pool_id = :p"), {"p": pool_id})
        await conn.rollback()


@pytest.mark.parametrize("run", ["first", "second"])
async def test_each_test_starts_with_empty_pool_tables(db: AsyncEngine, run: str) -> None:
    # Both runs assert emptiness and then leave pool rows behind, so whichever runs second fails unless the fixture truncated the pool tables.
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM pool"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT COUNT(*) FROM pool_share"))).scalar_one() == 0
    execution_id = await _seed(db)
    pool_id = await _insert_pool(db, execution_id)
    async with db.begin() as conn:
        await create_pool_custody_account(conn, pool_id, now=NOW)
        await conn.execute(insert(pool_share).values(pool_id=pool_id, account_id=ALICE, shares=4_000, updated_at=NOW))


def _downgrade_on(connection: Connection, revision: str) -> None:
    command.downgrade(make_config(connection=connection), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set((await conn.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))).scalars().all())


async def test_dex_migration_downgrades_and_upgrades(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.run_sync(_downgrade_on, "0002")
    names = await _table_names(db)
    assert "pool" not in names
    assert "pool_share" not in names
    async with db.connect() as conn:
        fks = (await conn.execute(text("SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'account'"))).scalar_one()
        assert fks == 0
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0002"
    async with db.begin() as conn:
        await conn.run_sync(_upgrade_on, "head")
    assert {"pool", "pool_share"} <= await _table_names(db)
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head_revision()
