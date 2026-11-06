"""The worker's jobs: stake accrual of stale stakes, the scheduled backup under its lease with a retry delay, and serve() running the dispatcher until stopped."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.config import Settings
from rapidwire.core import staking
from rapidwire.core.clock import FixedClock
from rapidwire.core.joblock import try_acquire
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import execution, job_lock, stake
from rapidwire.discord_rest import discord_client
from rapidwire.worker.app import STAKE_ACCRUAL_JOB, build_jobs, serve
from rapidwire.worker.dispatcher import Dispatcher
from rapidwire.worker.jobs import BACKUP_RETRY_DELAY, BackupFunctions, ScheduledBackup
from tests.integration.factories import create_currency, mint
from tests.integration.worker.fake_discord import FakeDiscord
from tests.integration.worker.helpers import PAYER, create_notified_claim, outbox_rows

STALE, FRESH, DUST = 7301, 7302, 7303
CURRENCY = 920_001


def _settings(tmp_path: Path, **values: object) -> Settings:
    return Settings(_env_file=None, db_password="worker-test-password", backup_dir=tmp_path, **values)


async def _last_accrued(engine: AsyncEngine, account_id: int) -> datetime:
    async with engine.connect() as conn:
        value = (await conn.execute(select(stake.c.last_accrued_at).where(stake.c.account_id == account_id))).scalar_one()
    assert isinstance(value, datetime)
    return value


async def test_stake_accrual_tick_accrues_only_stale_stakes(db: AsyncEngine, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    settings = _settings(tmp_path)
    await create_currency(db, currency_id=CURRENCY, symbol="STK", hourly_rate_ppm=10_000, now=now - timedelta(hours=10))
    for account_id, age in ((STALE, timedelta(hours=4)), (FRESH, timedelta(hours=1))):
        await mint(db, CURRENCY, account_id, 1_000_000, now=now - timedelta(hours=10))
        await staking.deposit(db, account_id=account_id, currency_id=CURRENCY, amount=1_000_000, now=now - age)
    fresh_before = await _last_accrued(db, FRESH)
    stale_before = await _last_accrued(db, STALE)

    async with discord_client("unused-token", transport=FakeDiscord().transport()) as client:
        jobs = {job.name: job for job in build_jobs(db, settings, Dispatcher(db, client), asyncio.Event(), holder="test", backup=_never_due())}
        await jobs[STAKE_ACCRUAL_JOB].tick()

    async with db.connect() as conn:
        interest = (await conn.execute(select(execution.c.initiator_id).where(execution.c.type == ExecutionType.INTEREST.value))).scalars().all()
    assert list(interest) == [STALE]
    assert await _last_accrued(db, STALE) > stale_before
    assert await _last_accrued(db, FRESH) == fresh_before


async def test_stake_accrual_log_counts_stakes_without_interest_apart(db: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    settings = _settings(tmp_path)
    await create_currency(db, currency_id=CURRENCY, symbol="STK", hourly_rate_ppm=10_000, now=now - timedelta(hours=10))
    # One minimal unit at 1%/h for 4 hours grows by less than one unit, so that stake only advances.
    for account_id, amount in ((STALE, 1_000_000), (DUST, 1)):
        await mint(db, CURRENCY, account_id, amount, now=now - timedelta(hours=10))
        await staking.deposit(db, account_id=account_id, currency_id=CURRENCY, amount=amount, now=now - timedelta(hours=4))
    dust_before = await _last_accrued(db, DUST)

    async with discord_client("unused-token", transport=FakeDiscord().transport()) as client:
        jobs = {job.name: job for job in build_jobs(db, settings, Dispatcher(db, client), asyncio.Event(), holder="test", backup=_never_due())}
        with caplog.at_level(logging.INFO, logger="rapidwire.worker.jobs"):
            await jobs[STAKE_ACCRUAL_JOB].tick()

    [record] = [record for record in caplog.records if record.getMessage() == "stake accrual finished"]
    assert record.levelno == logging.INFO
    assert (record.__dict__["credited"], record.__dict__["without_interest"], record.__dict__["failed"]) == (1, 1, 0)
    assert "accrued" not in record.__dict__
    assert await _last_accrued(db, DUST) > dust_before
    async with db.connect() as conn:
        interest = (await conn.execute(select(execution.c.initiator_id).where(execution.c.type == ExecutionType.INTEREST.value))).scalars().all()
    assert list(interest) == [STALE]


@dataclass
class _Outcome:
    ok: bool
    invariant_violations: int | None = None


def _never_due() -> BackupFunctions:
    async def run(engine: AsyncEngine, settings: Settings, *, now: datetime | None = None) -> _Outcome:
        raise AssertionError("no backup is due")

    return BackupFunctions(job_name="backup", next_due=lambda directory, interval, now: now + timedelta(days=1), run=run)


class _FakeBackup:
    def __init__(self, engine: AsyncEngine, *, due_in: timedelta = timedelta(0), outcomes: list[bool | _Outcome | Exception] | None = None) -> None:
        self.engine = engine
        self.due_in = due_in
        self.outcomes = outcomes or [True]
        self.runs: list[tuple[datetime | None, list[tuple[str, str]]]] = []

    def functions(self) -> BackupFunctions:
        return BackupFunctions(job_name="backup", next_due=lambda directory, interval, now: now + self.due_in, run=self.run)

    async def run(self, engine: AsyncEngine, settings: Settings, *, now: datetime | None = None) -> _Outcome:
        async with engine.connect() as conn:
            locks = [(row.name, row.holder) for row in await conn.execute(select(job_lock.c.name, job_lock.c.holder))]
        self.runs.append((now, locks))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, _Outcome) else _Outcome(ok=outcome)


async def test_due_backup_runs_under_the_backup_lease(db: AsyncEngine, tmp_path: Path) -> None:
    fake = _FakeBackup(db)
    clock = FixedClock(datetime(2026, 11, 1, tzinfo=UTC))
    job = ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1", clock=clock)

    assert await job.tick()

    assert fake.runs == [(clock.now(), [("backup", "worker-1")])]
    async with db.connect() as conn:
        assert (await conn.execute(select(job_lock.c.name))).all() == []


async def test_backup_not_yet_due_does_not_run(db: AsyncEngine, tmp_path: Path) -> None:
    fake = _FakeBackup(db, due_in=timedelta(seconds=1))
    assert not await ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1").tick()
    assert fake.runs == []


async def test_backup_held_by_another_process_is_skipped(db: AsyncEngine, tmp_path: Path) -> None:
    fake = _FakeBackup(db)
    assert await try_acquire(db, "backup", "on-demand", timedelta(minutes=5))
    assert not await ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1").tick()
    assert fake.runs == []


async def test_failed_backup_waits_for_the_retry_delay(db: AsyncEngine, tmp_path: Path) -> None:
    fake = _FakeBackup(db, outcomes=[False, RuntimeError("dump failed"), True])
    clock = FixedClock(datetime(2026, 11, 1, tzinfo=UTC))
    job = ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1", clock=clock)

    assert await job.tick()
    assert not await job.tick()
    clock.advance(BACKUP_RETRY_DELAY)
    with pytest.raises(RuntimeError, match="dump failed"):
        await job.tick()
    clock.advance(BACKUP_RETRY_DELAY - timedelta(seconds=1))
    assert not await job.tick()
    clock.advance(timedelta(seconds=1))
    assert await job.tick()
    assert len(fake.runs) == 3
    assert job.retry_at is None


async def test_backup_with_invariant_violations_is_logged_as_an_error(db: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeBackup(db, outcomes=[_Outcome(ok=True, invariant_violations=3)])
    job = ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1")

    assert await job.tick()

    [record] = [record for record in caplog.records if record.name == "rapidwire.worker.jobs" and record.levelno == logging.ERROR]
    assert record.getMessage() == "scheduled backup found ledger invariant violations in the exported snapshot"
    assert record.__dict__["invariant_violations"] == 3
    assert job.retry_at is None


async def test_clean_backup_logs_no_error(db: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeBackup(db, outcomes=[_Outcome(ok=True, invariant_violations=0)])
    assert await ScheduledBackup(db, _settings(tmp_path), fake.functions(), holder="worker-1").tick()
    assert not [record for record in caplog.records if record.name == "rapidwire.worker.jobs" and record.levelno >= logging.WARNING]


async def test_serve_dispatches_until_stopped(db: AsyncEngine, tmp_path: Path) -> None:
    await create_notified_claim(db)
    discord = FakeDiscord()
    stop = asyncio.Event()
    settings = _settings(tmp_path, outbox_poll_interval_seconds=0.05)
    task = asyncio.create_task(serve(settings, "fake-token-for-serve", stop, engine=db, transport=discord.transport(), backup=_never_due(), drain_seconds=1.0))
    for _ in range(200):
        if [row["status"] for row in await outbox_rows(db)] == ["sent"]:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=10)

    assert [row["status"] for row in await outbox_rows(db)] == ["sent"]
    assert discord.dm_channels.keys() == {PAYER}
    async with db.connect() as conn:
        assert (await conn.execute(select(job_lock.c.name))).all() == []
