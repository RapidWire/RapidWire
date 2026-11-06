from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.joblock import LeaseLost, LeaseUnavailable, hold_lease, try_acquire
from rapidwire.db.tables import job_lock

JOB = "lease_test"


async def _holder(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        value = (await conn.execute(select(job_lock.c.holder).where(job_lock.c.name == JOB))).scalar_one_or_none()
    return value if isinstance(value, str) else None


async def test_acquire_holds_the_row_and_releases_it_on_exit(db: AsyncEngine) -> None:
    async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=30)):
        assert await _holder(db) == "a"
    assert await _holder(db) is None


async def test_contention_raises_lease_unavailable(db: AsyncEngine) -> None:
    async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=30)):
        with pytest.raises(LeaseUnavailable):
            async with hold_lease(db, JOB, holder="b", ttl=timedelta(seconds=30)):
                pytest.fail("the body must not run")
    async with hold_lease(db, JOB, holder="b", ttl=timedelta(seconds=30)):
        assert await _holder(db) == "b"


async def test_renewal_keeps_the_lease_past_its_ttl(db: AsyncEngine) -> None:
    ttl = timedelta(seconds=1)
    async with hold_lease(db, JOB, holder="a", ttl=ttl, renew_interval=timedelta(seconds=0.2)):
        await asyncio.sleep(2.5)
        assert not await try_acquire(db, JOB, "b", ttl)
        assert await _holder(db) == "a"
    assert await _holder(db) is None


async def test_loss_cancels_the_body_and_raises_lease_lost(db: AsyncEngine) -> None:
    reached_end = False
    with pytest.raises(LeaseLost):
        async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=2), renew_interval=timedelta(seconds=0.2)):
            async with db.begin() as conn:
                await conn.execute(update(job_lock).where(job_lock.c.name == JOB).values(holder="thief"))
            await asyncio.sleep(30)
            reached_end = True
    assert not reached_end
    assert await _holder(db) == "thief"
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0


async def test_body_exception_propagates_and_releases(db: AsyncEngine) -> None:
    with pytest.raises(ZeroDivisionError):
        async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=30)):
            raise ZeroDivisionError
    assert await _holder(db) is None


async def test_external_cancellation_is_not_reported_as_loss(db: AsyncEngine) -> None:
    entered = asyncio.Event()

    async def body() -> None:
        async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=30)):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _holder(db) is None


async def test_invalid_renew_interval(db: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="renew interval"):
        async with hold_lease(db, JOB, holder="a", ttl=timedelta(seconds=5), renew_interval=timedelta(seconds=5)):
            pass
