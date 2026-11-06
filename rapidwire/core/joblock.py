"""Named leases in the job_lock table that keep a periodic job from running in two workers at once; expiry follows the database clock unless now is given, and leases are coordination state, so no call writes an execution."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import delete, func, insert, literal, select, type_coerce, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.clock import ensure_utc
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, MAX_ATTEMPTS, is_retryable, mysql_error_code, retry_delay
from rapidwire.db.tables import JOB_LOCK_HOLDER_MAX, JOB_LOCK_NAME_MAX, job_lock
from rapidwire.db.types import UtcDateTime

__all__ = [
    "HOLDER_MAX",
    "NAME_MAX",
    "LeaseLost",
    "LeaseUnavailable",
    "database_now",
    "hold_lease",
    "lease_held_in_tx",
    "make_holder",
    "release",
    "renew",
    "try_acquire",
]

NAME_MAX = JOB_LOCK_NAME_MAX
HOLDER_MAX = JOB_LOCK_HOLDER_MAX

_log = logging.getLogger(__name__)


class LeaseUnavailable(Exception):
    """hold_lease could not take the lease because another holder has a live one."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job lock {name!r} is held by another holder")
        self.name = name


class LeaseLost(Exception):
    """The lease held by hold_lease could not be renewed, so the body was cancelled."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job lock {name!r} was lost")
        self.name = name


def make_holder() -> str:
    """A holder string unique to this process start: host name, process ID and a random suffix."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(8)}"[-HOLDER_MAX:]


def _validate(name: str, holder: str, ttl: timedelta | None = None) -> None:
    if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"job lock name must be 1 to {NAME_MAX} characters")
    if not isinstance(holder, str) or not 1 <= len(holder) <= HOLDER_MAX:
        raise ValueError(f"job lock holder must be 1 to {HOLDER_MAX} characters")
    if ttl is not None and ttl <= timedelta(0):
        raise ValueError("job lock ttl must be positive")


async def database_now(conn: AsyncConnection) -> datetime:
    """The database server's current UTC time with microseconds."""
    value = (await conn.execute(select(type_coerce(func.utc_timestamp(6), UtcDateTime())))).scalar_one()
    if not isinstance(value, datetime):
        raise TypeError("UTC_TIMESTAMP(6) did not return a datetime")
    return value


async def _resolve(conn: AsyncConnection, now: datetime | None) -> datetime:
    return ensure_utc(now) if now is not None else await database_now(conn)


async def _acquire_once(engine: AsyncEngine, name: str, holder: str, ttl: timedelta, now: datetime | None) -> bool:
    async with engine.begin() as conn:
        at = await _resolve(conn, now)
        expires_at = at + ttl
        row = (await conn.execute(select(job_lock.c.holder, job_lock.c.expires_at).where(job_lock.c.name == name).with_for_update())).one_or_none()
        if row is None:
            await conn.execute(insert(job_lock).values(name=name, holder=holder, acquired_at=at, expires_at=expires_at))
            return True
        if row.holder == holder:
            await conn.execute(update(job_lock).where(job_lock.c.name == name).values(expires_at=max(row.expires_at, expires_at)))
            return True
        if row.expires_at <= at:
            await conn.execute(update(job_lock).where(job_lock.c.name == name).values(holder=holder, acquired_at=at, expires_at=expires_at))
            return True
        return False


async def try_acquire(engine: AsyncEngine, name: str, holder: str, ttl: timedelta, *, now: datetime | None = None) -> bool:
    """Take the lease until now + ttl when it is free or expired, or extend it to the later of its current expiry and now + ttl when holder already has it; returns False while another holder's lease is live.

    Concurrent first acquirers both find no row and insert; the loser's insert fails on the primary key once the winner commits, and the retry then sees the winner's lease.
    """
    _validate(name, holder, ttl)
    attempt = 0
    while True:
        try:
            return await _acquire_once(engine, name, holder, ttl, now)
        except IntegrityError as exc:
            attempt += 1
            if mysql_error_code(exc) != DUPLICATE_KEY_ERROR or attempt >= MAX_ATTEMPTS:
                raise
        except DBAPIError as exc:
            attempt += 1
            if not is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                raise
            await asyncio.sleep(retry_delay(attempt))


async def renew(engine: AsyncEngine, name: str, holder: str, ttl: timedelta, *, now: datetime | None = None) -> bool:
    """Extend holder's live lease to the later of its current expiry and now + ttl; returns False when holder does not hold it or it has already expired (the caller then calls try_acquire)."""
    _validate(name, holder, ttl)
    async with engine.begin() as conn:
        at = await _resolve(conn, now)
        stmt = (
            update(job_lock)
            .where(job_lock.c.name == name, job_lock.c.holder == holder, job_lock.c.expires_at > at)
            .values(expires_at=func.greatest(job_lock.c.expires_at, literal(at + ttl, UtcDateTime())))
        )
        result = await conn.execute(stmt)
    return result.rowcount == 1


async def lease_held_in_tx(conn: AsyncConnection, name: str, holder: str, *, now: datetime | None = None) -> bool:
    """Whether holder has a live lease, read with FOR SHARE in the caller's transaction so a takeover waits until that transaction ends; a job checks it in each transaction before committing, so a worker whose lease was taken over commits no more work."""
    _validate(name, holder)
    at = ensure_utc(now) if now is not None else func.utc_timestamp(6)
    stmt = select(job_lock.c.name).where(job_lock.c.name == name, job_lock.c.holder == holder, job_lock.c.expires_at > at).with_for_update(read=True)
    return (await conn.execute(stmt)).first() is not None


async def release(engine: AsyncEngine, name: str, holder: str) -> bool:
    """Remove the lease when holder holds it (live or expired); returns False and changes nothing when another holder has it or it does not exist."""
    _validate(name, holder)
    async with engine.begin() as conn:
        result = await conn.execute(delete(job_lock).where(job_lock.c.name == name, job_lock.c.holder == holder))
    return result.rowcount == 1


async def _keep_renewed(engine: AsyncEngine, name: str, holder: str, ttl: timedelta, interval: float, on_lost: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    expires = loop.time() + ttl.total_seconds()
    while True:
        await asyncio.sleep(interval)
        started = loop.time()
        try:
            renewed = await renew(engine, name, holder, ttl)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("renewing a job lock failed", exc_info=True, extra={"job": name})
            if loop.time() + interval < expires:
                continue
            renewed = False
        if not renewed:
            _log.error("job lock lost", extra={"job": name})
            on_lost.set()
            return
        expires = started + ttl.total_seconds()


@asynccontextmanager
async def hold_lease(engine: AsyncEngine, name: str, *, holder: str, ttl: timedelta, renew_interval: timedelta | None = None) -> AsyncIterator[None]:
    """Hold the lease for the body, renewing it every renew_interval (default ttl / 3) and releasing it on exit, or raise LeaseUnavailable when another holder has a live one.

    When a renewal is refused, or renewals keep failing until the lease would expire, the body is cancelled and LeaseLost is raised.
    """
    _validate(name, holder, ttl)
    interval = renew_interval if renew_interval is not None else ttl / 3
    if not timedelta(0) < interval < ttl:
        raise ValueError("job lock renew interval must be positive and shorter than ttl")
    if not await try_acquire(engine, name, holder, ttl):
        raise LeaseUnavailable(name)
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("hold_lease must run inside a task")
    lost = asyncio.Event()
    body_running = True
    cancelled_by_us = False

    def cancel_body(_: asyncio.Task[None]) -> None:
        nonlocal cancelled_by_us
        if lost.is_set() and body_running:
            cancelled_by_us = True
            task.cancel()

    keeper = asyncio.create_task(_keep_renewed(engine, name, holder, ttl, interval.total_seconds(), lost), name=f"lease:{name}")
    keeper.add_done_callback(cancel_body)
    try:
        try:
            yield
        except BaseException as exc:
            body_running = False
            if cancelled_by_us and task.uncancel() == 0:
                raise LeaseLost(name) from (None if isinstance(exc, asyncio.CancelledError) else exc)
            raise
        body_running = False
        if cancelled_by_us and task.uncancel() == 0:
            raise LeaseLost(name)
    finally:
        body_running = False
        keeper.cancel()
        await asyncio.gather(keeper, return_exceptions=True)
        try:
            await release(engine, name, holder)
        except Exception:
            _log.warning("releasing a job lock failed; it expires on its own", exc_info=True, extra={"job": name})
