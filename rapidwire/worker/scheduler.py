"""The worker's scheduler: every job runs as its own loop in one event loop, a job with a lease TTL runs only while this process holds that job's database lease, and a failing run is logged and retried at the next tick without stopping the other jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.joblock import LeaseLost, LeaseUnavailable, hold_lease, make_holder

__all__ = ["DEFAULT_LEASE_TTL", "SHUTDOWN_CANCEL_SECONDS", "SHUTDOWN_DRAIN_SECONDS", "Job", "Scheduler"]

DEFAULT_LEASE_TTL: Final = timedelta(seconds=60)
# After a stop request, running ticks get SHUTDOWN_DRAIN_SECONDS to finish and are then cancelled and given SHUTDOWN_CANCEL_SECONDS to release their leases; compose.yaml's worker stop_grace_period covers both.
SHUTDOWN_DRAIN_SECONDS: Final = 10.0
SHUTDOWN_CANCEL_SECONDS: Final = 10.0

log = logging.getLogger("rapidwire.worker.scheduler")


@dataclass(frozen=True)
class Job:
    """A periodic job: tick runs every interval_seconds (measured from the end of the previous run) while this process holds the job lock name for lease_ttl, renewed while held; with lease_ttl None the tick runs on every instance and takes any lock it needs itself."""

    name: str
    interval_seconds: float
    tick: Callable[[], Awaitable[object]]
    lease_ttl: timedelta | None = DEFAULT_LEASE_TTL

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("a job interval must be positive")


class Scheduler:
    """Runs jobs until stop is set; holder identifies this process in the job_lock table."""

    def __init__(
        self,
        engine: AsyncEngine,
        jobs: Sequence[Job],
        *,
        holder: str | None = None,
        drain_seconds: float = SHUTDOWN_DRAIN_SECONDS,
        cancel_seconds: float = SHUTDOWN_CANCEL_SECONDS,
    ) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        self.engine = engine
        self.jobs = tuple(jobs)
        self.holder = holder if holder is not None else make_holder()
        self.drain_seconds = drain_seconds
        self.cancel_seconds = cancel_seconds

    async def run(self, stop: asyncio.Event) -> None:
        """Run every job until stop is set, then wait drain_seconds for running ticks, cancel what is left and wait up to cancel_seconds for it to release its leases."""
        tasks = [asyncio.create_task(self._run_job(job, stop), name=f"job:{job.name}") for job in self.jobs]
        try:
            await stop.wait()
        finally:
            await self._shutdown(tasks)

    async def _shutdown(self, tasks: list[asyncio.Task[None]]) -> None:
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self.drain_seconds)
        if pending:
            log.warning("cancelling jobs still running at shutdown", extra={"jobs": sorted(task.get_name() for task in pending)})
            for task in pending:
                task.cancel()
            _, pending = await asyncio.wait(pending, timeout=self.cancel_seconds)
            if pending:
                log.error("jobs did not stop after cancellation", extra={"jobs": sorted(task.get_name() for task in pending)})

    async def _tick(self, job: Job) -> None:
        try:
            await job.tick()
        except Exception:
            log.exception("job failed; it runs again at its next tick", extra={"job": job.name})

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def _run_job(self, job: Job, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if job.lease_ttl is None:
                await self._tick(job)
                await self._sleep(stop, job.interval_seconds)
                continue
            retry_seconds = min(job.interval_seconds, job.lease_ttl.total_seconds())
            try:
                async with hold_lease(self.engine, job.name, holder=self.holder, ttl=job.lease_ttl):
                    log.info("job lock acquired", extra={"job": job.name})
                    while not stop.is_set():
                        await self._tick(job)
                        await self._sleep(stop, job.interval_seconds)
            except LeaseUnavailable:
                log.debug("job lock is held by another worker", extra={"job": job.name})
                await self._sleep(stop, retry_seconds)
            except LeaseLost:
                log.warning("job lock lost; trying to take it again", extra={"job": job.name})
                await self._sleep(stop, retry_seconds)
            except Exception:
                log.exception("job lock could not be taken", extra={"job": job.name})
                await self._sleep(stop, retry_seconds)
