from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, Column, MetaData, Table, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.types import AMOUNT_MAX, Amount


@pytest_asyncio.fixture(loop_scope="session")
async def ledger(engine: AsyncEngine) -> AsyncIterator[Table]:
    """A real (non-temporary) table with an Amount column, dropped after the test."""
    table = Table(f"tx_probe_{uuid.uuid4().hex[:8]}", MetaData(), Column("id", BigInteger, primary_key=True, autoincrement=False), Column("v", Amount(), nullable=False))
    async with engine.begin() as conn:
        await conn.run_sync(table.create)
    try:
        yield table
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(table.drop)


async def test_commit_is_visible_to_other_connections(engine: AsyncEngine, ledger: Table) -> None:
    async with engine.begin() as conn:
        await conn.execute(ledger.insert().values(id=1, v=5))
    async with engine.connect() as other:
        assert (await other.execute(select(ledger.c.v).where(ledger.c.id == 1))).scalar_one() == 5


async def test_rollback_is_not_visible(engine: AsyncEngine, ledger: Table) -> None:
    async with engine.connect() as conn:
        await conn.execute(ledger.insert().values(id=1, v=5))
        await conn.rollback()
    async with engine.connect() as other:
        assert (await other.execute(select(func.count()).select_from(ledger))).scalar_one() == 0


async def test_for_update_blocks_then_sees_committed_value(engine: AsyncEngine, ledger: Table) -> None:
    async with engine.begin() as conn:
        await conn.execute(ledger.insert().values(id=1, v=100))

    async with engine.connect() as holder, engine.connect() as waiter:
        await waiter.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        await waiter.commit()

        # A plain read inside an open transaction, to show READ COMMITTED refreshes its snapshot per statement.
        assert (await waiter.execute(select(ledger.c.v).where(ledger.c.id == 1))).scalar_one() == 100

        assert (await holder.execute(select(ledger.c.v).where(ledger.c.id == 1).with_for_update())).scalar_one() == 100
        with pytest.raises(DBAPIError, match="Lock wait timeout"):
            await waiter.execute(select(ledger.c.v).where(ledger.c.id == 1).with_for_update())

        await holder.execute(update(ledger).where(ledger.c.id == 1).values(v=ledger.c.v - 30))
        await holder.commit()

        assert (await waiter.execute(select(ledger.c.v).where(ledger.c.id == 1))).scalar_one() == 70
        assert (await waiter.execute(select(ledger.c.v).where(ledger.c.id == 1).with_for_update())).scalar_one() == 70
        await waiter.rollback()


async def test_amount_arithmetic_overflow_is_an_error(engine: AsyncEngine, ledger: Table) -> None:
    async with engine.begin() as conn:
        await conn.execute(ledger.insert().values(id=1, v=AMOUNT_MAX - 5))
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="Out of range"):
            await conn.execute(update(ledger).where(ledger.c.id == 1).values(v=ledger.c.v + 10))
        await conn.rollback()
    async with engine.connect() as conn:
        assert (await conn.execute(select(ledger.c.v))).scalar_one() == AMOUNT_MAX - 5


async def test_amount_sum_is_exact_int(engine: AsyncEngine, ledger: Table) -> None:
    values = [AMOUNT_MAX // 4, AMOUNT_MAX // 4, 7, -(AMOUNT_MAX // 3)]
    async with engine.begin() as conn:
        await conn.execute(ledger.insert(), [{"id": i, "v": v} for i, v in enumerate(values, start=1)])
    async with engine.connect() as conn:
        total = (await conn.execute(select(func.sum(ledger.c.v)))).scalar_one()
    assert type(total) is int
    assert total == sum(values)
