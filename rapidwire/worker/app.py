"""The worker process: the outbox dispatcher, stake accrual and scheduled backups under one scheduler, stopped gracefully by SIGTERM or SIGINT."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Final

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.config import Settings
from rapidwire.core.clock import Clock
from rapidwire.core.joblock import make_holder
from rapidwire.db.engine import create_engine
from rapidwire.discord_rest import discord_client
from rapidwire.signals import stop_signals
from rapidwire.worker.dispatcher import Dispatcher
from rapidwire.worker.jobs import BACKUP_CHECK_INTERVAL_SECONDS, BackupFunctions, ScheduledBackup, accrue_stakes, default_backup_functions
from rapidwire.worker.scheduler import SHUTDOWN_DRAIN_SECONDS, Job, Scheduler

__all__ = ["BACKUP_CHECK_JOB", "OUTBOX_JOB", "STAKE_ACCRUAL_JOB", "build_jobs", "run_worker", "serve"]

OUTBOX_JOB: Final = "outbox_dispatcher"
STAKE_ACCRUAL_JOB: Final = "stake_accrual"
# The backup check runs on every worker; the backup itself takes the backup lease.
BACKUP_CHECK_JOB: Final = "backup_check"

log = logging.getLogger("rapidwire.worker")


def build_jobs(engine: AsyncEngine, settings: Settings, dispatcher: Dispatcher, stop: asyncio.Event, *, holder: str, backup: BackupFunctions, clock: Clock | None = None) -> list[Job]:
    """The worker's jobs: the dispatcher every outbox_poll_interval_seconds, stake accrual every stake_accrual_interval_seconds and the backup check every BACKUP_CHECK_INTERVAL_SECONDS."""
    return [
        Job(OUTBOX_JOB, settings.outbox_poll_interval_seconds, lambda: dispatcher.run_once(should_stop=stop.is_set)),
        Job(STAKE_ACCRUAL_JOB, float(settings.stake_accrual_interval_seconds), lambda: accrue_stakes(engine, settings), lease_ttl=timedelta(minutes=5)),
        Job(BACKUP_CHECK_JOB, BACKUP_CHECK_INTERVAL_SECONDS, ScheduledBackup(engine, settings, backup, holder=holder, clock=clock).tick, lease_ttl=None),
    ]


async def serve(
    settings: Settings,
    token: str,
    stop: asyncio.Event,
    *,
    engine: AsyncEngine | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    backup: BackupFunctions | None = None,
    clock: Clock | None = None,
    drain_seconds: float = SHUTDOWN_DRAIN_SECONDS,
) -> None:
    """Run the worker's jobs until stop is set; an engine passed in is left open, one created here is disposed on exit, and transport replaces the network for Discord in tests."""
    own_engine = engine is None
    db = engine if engine is not None else create_engine(settings.database_url)
    holder = make_holder()
    try:
        async with discord_client(token, transport=transport) as client:
            dispatcher = Dispatcher(db, client, clock=clock)
            jobs = build_jobs(db, settings, dispatcher, stop, holder=holder, backup=backup or default_backup_functions(), clock=clock)
            await Scheduler(db, jobs, holder=holder, drain_seconds=drain_seconds).run(stop)
    finally:
        if own_engine:
            await db.dispose()


async def _serve_until_signal(settings: Settings, token: str) -> None:
    stop = asyncio.Event()

    def request_stop() -> None:
        if not stop.is_set():
            log.info("stopping worker")
            stop.set()

    with stop_signals(request_stop):
        await serve(settings, token, stop)


def run_worker(settings: Settings) -> int:
    """Run the worker until SIGTERM or SIGINT and return exit code 0; the Discord token is required."""
    token = settings.require_discord_token()
    log.info("starting worker", extra={"outbox_poll_interval_seconds": settings.outbox_poll_interval_seconds, "stake_accrual_interval_seconds": settings.stake_accrual_interval_seconds})
    asyncio.run(_serve_until_signal(settings, token))
    log.info("worker stopped")
    return 0
