"""The worker's periodic jobs besides the outbox dispatcher: stake accrual and scheduled backups."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.backup.jobs import BACKUP_LEASE_TTL
from rapidwire.config import Settings
from rapidwire.core.clock import Clock, get_clock
from rapidwire.core.joblock import LeaseUnavailable, hold_lease
from rapidwire.core.staking import StaleAccrualResult, accrue_stale_stakes

__all__ = [
    "BACKUP_CHECK_INTERVAL_SECONDS",
    "BACKUP_LEASE_RENEW_INTERVAL",
    "BACKUP_LEASE_TTL",
    "BACKUP_RETRY_DELAY",
    "BackupFunctions",
    "BackupOutcome",
    "ScheduledBackup",
    "accrue_stakes",
    "default_backup_functions",
]

# How often the worker checks whether a scheduled backup is due.
BACKUP_CHECK_INTERVAL_SECONDS: Final = 60.0
# A backup runs for several minutes under the backup lease of rapidwire.backup.jobs (BACKUP_LEASE_TTL), renewed every minute.
BACKUP_LEASE_RENEW_INTERVAL: Final = timedelta(minutes=1)
# The wait after a scheduled backup that failed or did not complete before the next attempt.
BACKUP_RETRY_DELAY: Final = timedelta(minutes=15)

log = logging.getLogger("rapidwire.worker.jobs")


async def accrue_stakes(engine: AsyncEngine, settings: Settings) -> StaleAccrualResult:
    """Accrue every stake last accrued at least stake_accrual_staleness_seconds ago and log how many were credited with interest, how many only advanced without interest and how many failed."""
    result = await accrue_stale_stakes(engine, older_than=timedelta(seconds=settings.stake_accrual_staleness_seconds))
    level = logging.WARNING if result.failed else logging.INFO
    log.log(level, "stake accrual finished", extra={"credited": result.credited, "without_interest": result.without_interest, "failed": result.failed})
    return result


class BackupOutcome(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def invariant_violations(self) -> int | None: ...


class RunBackup(Protocol):
    async def __call__(self, engine: AsyncEngine, settings: Settings, *, now: datetime | None = None) -> BackupOutcome: ...


@dataclass(frozen=True)
class BackupFunctions:
    """The backup job's lock name, its due-time rule and the backup run itself (rapidwire.backup.jobs in production)."""

    job_name: str
    next_due: Callable[[Path, timedelta, datetime], datetime]
    run: RunBackup


def default_backup_functions() -> BackupFunctions:
    """The production backup job from rapidwire.backup.jobs."""
    from rapidwire.backup.jobs import BACKUP_JOB_NAME, next_backup_due, run_backup

    return BackupFunctions(job_name=BACKUP_JOB_NAME, next_due=next_backup_due, run=run_backup)


class ScheduledBackup:
    """The scheduled backup job of one worker: tick() takes a backup under the backup lease when one is due, and after a run that failed or did not complete waits BACKUP_RETRY_DELAY before trying again, because a failed run does not move the due time."""

    def __init__(self, engine: AsyncEngine, settings: Settings, functions: BackupFunctions, *, holder: str, clock: Clock | None = None) -> None:
        self.engine = engine
        self.settings = settings
        self.functions = functions
        self.holder = holder
        self.clock = clock
        self.retry_at: datetime | None = None

    async def tick(self) -> bool:
        """Returns whether a backup ran; a tick is skipped while another process (such as an on-demand rapidwire backup) holds the backup lease."""
        now = (self.clock or get_clock()).now()
        if self.retry_at is not None and now < self.retry_at:
            return False
        due = await asyncio.to_thread(self.functions.next_due, Path(self.settings.backup_dir), timedelta(seconds=self.settings.backup_interval_seconds), now)
        if due > now:
            return False
        try:
            async with hold_lease(self.engine, self.functions.job_name, holder=self.holder, ttl=BACKUP_LEASE_TTL, renew_interval=BACKUP_LEASE_RENEW_INTERVAL):
                log.info("scheduled backup started")
                outcome = await self.functions.run(self.engine, self.settings, now=now)
        except LeaseUnavailable:
            log.info("scheduled backup skipped; another process holds the backup lock")
            return False
        except Exception:
            self.retry_at = now + BACKUP_RETRY_DELAY
            raise
        if outcome.invariant_violations:
            log.error("scheduled backup found ledger invariant violations in the exported snapshot", extra={"invariant_violations": outcome.invariant_violations})
        if outcome.ok:
            self.retry_at = None
            log.info("scheduled backup finished")
        else:
            self.retry_at = now + BACKUP_RETRY_DELAY
            log.warning("scheduled backup did not complete; it is tried again later", extra={"retry_in_seconds": BACKUP_RETRY_DELAY.total_seconds()})
        return True
