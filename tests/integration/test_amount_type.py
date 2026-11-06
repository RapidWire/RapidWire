from __future__ import annotations

import uuid

from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN, Amount


async def test_amount_round_trip(engine: AsyncEngine) -> None:
    probe = Table(f"amount_probe_{uuid.uuid4().hex[:8]}", MetaData(), Column("id", Integer, primary_key=True, autoincrement=False), Column("v", Amount(), nullable=True))
    values = [AMOUNT_MAX, AMOUNT_MIN, 0, 1, -1, 10**18 + 7, None]
    async with engine.begin() as conn:
        await conn.run_sync(probe.create)
    try:
        async with engine.begin() as conn:
            await conn.execute(probe.insert(), [{"id": i, "v": v} for i, v in enumerate(values, start=1)])
        async with engine.connect() as conn:
            rows = (await conn.execute(select(probe.c.id, probe.c.v).order_by(probe.c.id))).all()
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(probe.drop)
    assert [v for _, v in rows] == values
    assert all(type(v) is int for _, v in rows if v is not None)
