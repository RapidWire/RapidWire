from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.joblock import database_now, lease_held_in_tx, make_holder, release, renew, try_acquire
from rapidwire.db.tables import execution, job_lock

NOW = datetime(2026, 5, 4, tzinfo=UTC)
TTL = timedelta(minutes=5)
JOB = "stake_accrual"


async def _row(engine: AsyncEngine, name: str = JOB) -> tuple[str, datetime, datetime] | None:
    async with engine.connect() as conn:
        row = (await conn.execute(select(job_lock.c.holder, job_lock.c.acquired_at, job_lock.c.expires_at).where(job_lock.c.name == name))).one_or_none()
    return None if row is None else (row.holder, row.acquired_at, row.expires_at)


async def _wait_for_lock_wait(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for _ in range(200):
            if (await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.05)
        states = (await conn.execute(text("SELECT id, command, state, info FROM information_schema.processlist WHERE command <> 'Sleep'"))).all()
    raise AssertionError(f"no transaction started waiting for a lock: {states}")


async def test_concurrent_acquirers_get_exactly_one_lease(db: AsyncEngine) -> None:
    holders = [f"worker-{index}" for index in range(8)]
    results = await asyncio.gather(*(try_acquire(db, JOB, holder, TTL, now=NOW) for holder in holders))
    assert sum(results) == 1
    winner = holders[results.index(True)]
    assert await _row(db) == (winner, NOW, NOW + TTL)


async def test_concurrent_takeover_of_an_expired_lease_gets_exactly_one(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "old", TTL, now=NOW)
    later = NOW + TTL
    holders = [f"worker-{index}" for index in range(8)]
    results = await asyncio.gather(*(try_acquire(db, JOB, holder, TTL, now=later) for holder in holders))
    assert sum(results) == 1
    row = await _row(db)
    assert row is not None
    assert row[0] == holders[results.index(True)]


async def test_live_lease_excludes_others_until_expiry(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    assert not await try_acquire(db, JOB, "b", TTL, now=NOW + TTL - timedelta(seconds=1))
    assert await try_acquire(db, JOB, "b", TTL, now=NOW + TTL)
    assert await _row(db) == ("b", NOW + TTL, NOW + 2 * TTL)
    assert not await try_acquire(db, JOB, "a", TTL, now=NOW + TTL)


async def test_reacquire_by_holder_renews_and_keeps_acquired_at(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    assert await try_acquire(db, JOB, "a", TTL, now=NOW + timedelta(minutes=1))
    assert await _row(db) == ("a", NOW, NOW + timedelta(minutes=1) + TTL)


async def test_renewal_with_a_shorter_ttl_keeps_the_later_expiry(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", timedelta(hours=1), now=NOW)
    assert await try_acquire(db, JOB, "a", timedelta(minutes=1), now=NOW + timedelta(minutes=1))
    assert await _row(db) == ("a", NOW, NOW + timedelta(hours=1))
    assert await renew(db, JOB, "a", timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    assert await _row(db) == ("a", NOW, NOW + timedelta(hours=1))
    assert await renew(db, JOB, "a", timedelta(hours=2), now=NOW + timedelta(minutes=2))
    assert await _row(db) == ("a", NOW, NOW + timedelta(minutes=2, hours=2))


async def test_takeover_mid_job_is_seen_by_the_next_job_transaction(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    async with db.begin() as conn:
        assert await lease_held_in_tx(conn, JOB, "a", now=NOW + timedelta(minutes=1))
        assert not await lease_held_in_tx(conn, JOB, "b", now=NOW + timedelta(minutes=1))
    assert await try_acquire(db, JOB, "b", TTL, now=NOW + TTL)
    async with db.begin() as conn:
        assert not await lease_held_in_tx(conn, JOB, "a", now=NOW + TTL)
        assert await lease_held_in_tx(conn, JOB, "b", now=NOW + TTL)


async def test_lease_check_blocks_a_takeover_until_the_job_transaction_ends(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    async with db.connect() as conn:
        await conn.begin()
        assert await lease_held_in_tx(conn, JOB, "a", now=NOW + TTL - timedelta(seconds=1))
        takeover = asyncio.create_task(try_acquire(db, JOB, "b", TTL, now=NOW + TTL))
        await _wait_for_lock_wait(db)
        assert not takeover.done()
        await conn.commit()
    assert await takeover


async def test_database_clock_is_used_without_now(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        db_now = await database_now(conn)
    assert abs(db_now - datetime.now(UTC)) < timedelta(minutes=5)
    assert await try_acquire(db, JOB, "a", TTL)
    row = await _row(db)
    assert row is not None
    assert row[2] - row[1] == TTL
    assert abs(row[1] - db_now) < timedelta(minutes=1)
    assert not await try_acquire(db, JOB, "b", TTL)
    assert await renew(db, JOB, "a", TTL)
    async with db.begin() as conn:
        assert await lease_held_in_tx(conn, JOB, "a")
    assert await try_acquire(db, "stale", "a", TTL, now=datetime(2020, 1, 1, tzinfo=UTC))
    assert await try_acquire(db, "stale", "b", TTL)
    assert not await renew(db, "stale", "a", TTL)
    async with db.begin() as conn:
        assert not await lease_held_in_tx(conn, "stale", "a")


async def test_renew(db: AsyncEngine) -> None:
    assert not await renew(db, JOB, "a", TTL, now=NOW)
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    assert await renew(db, JOB, "a", TTL, now=NOW + timedelta(minutes=4))
    assert await _row(db) == ("a", NOW, NOW + timedelta(minutes=4) + TTL)
    assert await renew(db, JOB, "a", TTL, now=NOW + timedelta(minutes=4))
    assert not await renew(db, JOB, "b", TTL, now=NOW + timedelta(minutes=5))
    assert not await renew(db, JOB, "a", TTL, now=NOW + timedelta(minutes=9))
    assert await try_acquire(db, JOB, "b", TTL, now=NOW + timedelta(minutes=9))
    assert not await renew(db, JOB, "a", TTL, now=NOW + timedelta(minutes=9))


async def test_release_only_by_holder(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    assert not await release(db, JOB, "b")
    assert await _row(db) is not None
    assert await release(db, JOB, "a")
    assert await _row(db) is None
    assert not await release(db, JOB, "a")
    assert await try_acquire(db, JOB, "b", TTL, now=NOW)


async def test_locks_are_independent_by_name_and_write_no_executions(db: AsyncEngine) -> None:
    assert await try_acquire(db, JOB, "a", TTL, now=NOW)
    assert await try_acquire(db, "backup", "b", TTL, now=NOW)
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(execution))).scalar_one() == 0


async def test_invalid_arguments(db: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="ttl"):
        await try_acquire(db, JOB, "a", timedelta(0), now=NOW)
    with pytest.raises(ValueError, match="name"):
        await try_acquire(db, "x" * 65, "a", TTL, now=NOW)
    with pytest.raises(ValueError, match="holder"):
        await release(db, JOB, "")


def test_make_holder_is_unique_per_call() -> None:
    first, second = make_holder(), make_holder()
    assert first != second
    assert 1 <= len(first) <= 255
