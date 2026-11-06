"""The scheduler: job locks keep a job on one worker at a time, a failing job is retried without stopping the others, and shutdown cancels within its bound and releases the leases."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.tables import job_lock
from rapidwire.worker.scheduler import Job, Scheduler

TTL = timedelta(seconds=2)
# Upper bound for conditions the scheduler reaches within a few ticks; only a broken scheduler comes near it.
WITHIN_SECONDS = 15.0
SCHEDULER_LOGGER = "rapidwire.worker.scheduler"
CONTENDED = "job lock is held by another worker"
LOST = "job lock lost; trying to take it again"


async def _lock_rows(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        return [(row.name, row.holder) for row in await conn.execute(select(job_lock.c.name, job_lock.c.holder).order_by(job_lock.c.name))]


async def _until(condition: Callable[[], bool | Awaitable[bool]], what: str) -> None:
    """Wait until condition holds, failing after WITHIN_SECONDS."""
    deadline = time.monotonic() + WITHIN_SECONDS
    while True:
        value = condition()
        if not isinstance(value, bool):
            value = await value
        if value:
            return
        assert time.monotonic() < deadline, f"{what} did not happen within {WITHIN_SECONDS:.0f}s"
        await asyncio.sleep(0.01)


def _count(caplog: pytest.LogCaptureFixture, message: str) -> int:
    return sum(1 for record in caplog.records if record.name == SCHEDULER_LOGGER and record.getMessage() == message)


async def test_a_job_runs_on_one_scheduler_at_a_time(db: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=SCHEDULER_LOGGER)
    runs: list[tuple[str, float, float]] = []

    def job(owner: str) -> Job:
        async def tick() -> None:
            started = time.monotonic()
            await asyncio.sleep(0.05)
            runs.append((owner, started, time.monotonic()))

        return Job("shared", 0.01, tick, lease_ttl=TTL)

    first, second = Scheduler(db, [job("first")], holder="first"), Scheduler(db, [job("second")], holder="second")
    stop_first, stop_second = asyncio.Event(), asyncio.Event()
    first_task = asyncio.create_task(first.run(stop_first))
    await _until(lambda: bool(runs), "the first scheduler's first tick")
    second_task = asyncio.create_task(second.run(stop_second))
    await _until(lambda: _count(caplog, CONTENDED) >= 3, "three refused lock attempts of the second scheduler")
    assert {owner for owner, _, _ in runs} == {"first"}
    assert await _lock_rows(db) == [("shared", "first")]

    stopped_first_at = time.monotonic()
    stop_first.set()
    await first_task
    await _until(lambda: any(owner == "second" for owner, _, _ in runs), "the second scheduler's takeover")
    stop_second.set()
    await second_task

    ordered = sorted(runs, key=lambda run: run[1])
    assert all(earlier[2] <= later[1] for earlier, later in itertools.pairwise(ordered))
    assert all(started >= stopped_first_at for owner, started, _ in runs if owner == "second")
    assert await _lock_rows(db) == []


async def test_a_failing_job_is_retried_and_does_not_stop_others(db: AsyncEngine) -> None:
    counts = {"bad": 0, "good": 0, "unlocked": 0}

    async def bad() -> None:
        counts["bad"] += 1
        raise RuntimeError("boom")

    async def good() -> None:
        counts["good"] += 1

    async def unlocked() -> None:
        counts["unlocked"] += 1

    scheduler = Scheduler(db, [Job("bad", 0.05, bad, lease_ttl=TTL), Job("good", 0.05, good, lease_ttl=TTL), Job("unlocked", 0.05, unlocked, lease_ttl=None)])
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    await _until(lambda: min(counts.values()) >= 3, "three runs of every job")
    stop.set()
    await task

    assert await _lock_rows(db) == []


async def test_shutdown_cancels_stuck_jobs_within_the_bound_and_releases_leases(db: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    drain_seconds, cancel_seconds = 0.3, 60.0
    scheduler = Scheduler(db, [Job("stuck", 1.0, stuck, lease_ttl=timedelta(seconds=120))], holder="me", drain_seconds=drain_seconds, cancel_seconds=cancel_seconds)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    await asyncio.wait_for(started.wait(), timeout=WITHIN_SECONDS)
    assert await _lock_rows(db) == [("stuck", "me")]

    begun = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=cancel_seconds)
    elapsed = time.monotonic() - begun

    # Shutdown waits out the drain, then ends as soon as the cancelled job has released its lease rather than after the cancel bound.
    assert drain_seconds <= elapsed < cancel_seconds / 2
    assert cancelled.is_set()
    assert _count(caplog, "cancelling jobs still running at shutdown") == 1
    assert _count(caplog, "jobs did not stop after cancellation") == 0
    assert await _lock_rows(db) == []


async def test_a_held_lease_is_lost_and_retaken(db: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=SCHEDULER_LOGGER)
    ticks: list[float] = []

    async def tick() -> None:
        ticks.append(time.monotonic())

    scheduler = Scheduler(db, [Job("contested", 0.05, tick, lease_ttl=timedelta(seconds=1))], holder="me")
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))
    await _until(lambda: bool(ticks), "the first tick")
    async with db.begin() as conn:
        await conn.execute(job_lock.update().where(job_lock.c.name == "contested").values(holder="thief", expires_at=datetime.now(UTC) + timedelta(hours=1)))
    await _until(lambda: _count(caplog, LOST) == 1, "the detection of the lost lease")
    paused_from = len(ticks)
    refused = _count(caplog, CONTENDED)
    await _until(lambda: _count(caplog, CONTENDED) >= refused + 3, "three refused attempts to retake the lease")
    assert len(ticks) == paused_from, "no ticks run while another holder has the lease"
    async with db.begin() as conn:
        await conn.execute(job_lock.delete().where(job_lock.c.name == "contested"))
    await _until(lambda: len(ticks) > paused_from, "a tick after the lease was retaken")
    stop.set()
    await task
    assert await _lock_rows(db) == []
