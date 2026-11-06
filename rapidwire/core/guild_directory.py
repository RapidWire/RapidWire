"""The guild directory: the bot writes its gateway view of Discord (guilds, owners, administrator roles, members and their roles) and a heartbeat to the database, and DbGuildInfo answers GuildInfo from it in processes without a gateway.

Writers run one short transaction each, lock the guild_snapshot row FOR UPDATE before any other directory row of that guild and retry deadlocks, so concurrent writers serialize per guild without surfacing deadlock errors.
The heartbeat (unless record_heartbeat() is given now), the staleness check (unless DbGuildInfo is given a clock) and every updated_at use the database clock, so skew between the bot's and the readers' host clocks cannot make information look fresh or stale.
Directory rows are coordination state, so no call writes an execution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import URL, ColumnElement, and_, delete, exists, func, literal, or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.clock import Clock, ensure_utc
from rapidwire.core.ledger import MAX_ATTEMPTS, is_retryable, retry_delay
from rapidwire.core.validation import BIGINT_MAX, is_guild_id
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import GUILD_DIRECTORY_STATE_ID, guild_administrator_role, guild_directory_state, guild_member_role, guild_member_snapshot, guild_snapshot
from rapidwire.db.types import UtcDateTime

if TYPE_CHECKING:
    from rapidwire.config import Settings

__all__ = [
    "LOOKUP_WAIT_SECONDS",
    "READER_POOL_SIZE",
    "READER_POOL_TIMEOUT_SECONDS",
    "WRITE_BATCH_SIZE",
    "DbGuildInfo",
    "Heartbeat",
    "MemberSnapshot",
    "clear_heartbeat",
    "get_heartbeat",
    "prune_guilds",
    "record_heartbeat",
    "remove_guild",
    "remove_member",
    "remove_role",
    "replace_guild",
    "set_role_administrator",
    "upsert_guild",
    "upsert_member",
]

# Rows per multi-row INSERT when a guild is replaced.
WRITE_BATCH_SIZE = 1000

# The dedicated reader engine of DbGuildInfo.open(): connections it keeps, and seconds a call waits for a free one, below the time has_role() waits for an answer, so a busy reader pool gives a quick unknown answer instead of a timeout.
READER_POOL_SIZE = 8
READER_POOL_TIMEOUT_SECONDS = 0.1
# How long guild_owner() and is_administrator(), which run before a transaction starts, keep asking a busy pool for a connection before answering unknown.
LOOKUP_WAIT_SECONDS = 2.0

_DB_NOW = func.utc_timestamp(6)
_UNKNOWN: Final = object()

log = logging.getLogger("rapidwire.core.guild_directory")


def _is_id(value: object) -> bool:
    """Whether value is a Discord snowflake, a positive BIGINT."""
    return is_guild_id(value)


def _check_id(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not _is_id(value):
        raise ValueError(f"{name} must be an integer from 1 to {BIGINT_MAX}")
    return value


def _role_set(guild_id: int, role_ids: Iterable[int]) -> frozenset[int]:
    """The validated role IDs without the @everyone role (the guild's own ID), which every member has implicitly."""
    return frozenset(_check_id("role_id", role_id) for role_id in role_ids) - {guild_id}


@dataclass(frozen=True)
class MemberSnapshot:
    """A guild member and the IDs of the roles it holds; the @everyone role may be included or left out."""

    user_id: int
    role_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        _check_id("user_id", self.user_id)
        object.__setattr__(self, "role_ids", frozenset(_check_id("role_id", role_id) for role_id in self.role_ids))


@dataclass(frozen=True)
class Heartbeat:
    """The bot's last heartbeat and the bot user it named, if any."""

    at: datetime
    bot_user_id: int | None


async def _write[T](engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run work in one transaction, running it again after a deadlock or lock wait timeout up to MAX_ATTEMPTS times."""
    attempt = 0
    while True:
        try:
            async with engine.begin() as conn:
                return await work(conn)
        except DBAPIError as exc:
            attempt += 1
            if not is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                raise
            await asyncio.sleep(retry_delay(attempt))


async def _lock_guild(conn: AsyncConnection, guild_id: int) -> bool:
    """Lock the guild row FOR UPDATE; False when the bot is not in the guild."""
    stmt = select(guild_snapshot.c.guild_id).where(guild_snapshot.c.guild_id == guild_id).with_for_update()
    return (await conn.execute(stmt)).first() is not None


async def _put_guild(conn: AsyncConnection, guild_id: int, owner_id: int) -> None:
    """Insert or update the guild row, which takes its exclusive lock."""
    stmt = mysql_insert(guild_snapshot).values(guild_id=guild_id, owner_id=owner_id, updated_at=_DB_NOW)
    await conn.execute(stmt.on_duplicate_key_update(owner_id=stmt.inserted.owner_id, updated_at=_DB_NOW))


async def _insert_batched(conn: AsyncConnection, table: Any, rows: Sequence[dict[str, Any]]) -> None:
    for start in range(0, len(rows), WRITE_BATCH_SIZE):
        await conn.execute(table.insert().values(list(rows[start : start + WRITE_BATCH_SIZE])))


async def _delete_member_rows(conn: AsyncConnection, guild_id: int) -> None:
    await conn.execute(delete(guild_member_role).where(guild_member_role.c.guild_id == guild_id))
    await conn.execute(delete(guild_member_snapshot).where(guild_member_snapshot.c.guild_id == guild_id))


async def record_heartbeat(engine: AsyncEngine, *, bot_user_id: int | None = None, now: datetime | None = None) -> None:
    """Record that the bot's view is current as of now (the database time when None), and the bot's user ID when given; the bot calls it every GUILD_HEARTBEAT_INTERVAL_SECONDS (rapidwire.config) only while its gateway session is ready and its guild data written."""
    if bot_user_id is not None:
        _check_id("bot_user_id", bot_user_id)
    at: Any = _DB_NOW if now is None else literal(ensure_utc(now), UtcDateTime())
    stmt = mysql_insert(guild_directory_state).values(state_id=GUILD_DIRECTORY_STATE_ID, bot_user_id=bot_user_id, bot_heartbeat_at=at)
    updates: dict[str, Any] = {"bot_heartbeat_at": at}
    if bot_user_id is not None:
        updates["bot_user_id"] = bot_user_id

    async def work(conn: AsyncConnection) -> None:
        await conn.execute(stmt.on_duplicate_key_update(**updates))

    await _write(engine, work)


async def clear_heartbeat(engine: AsyncEngine) -> None:
    """Forget the heartbeat, so readers treat guild information as unavailable at once; the bot calls it when its gateway session ends or it shuts down."""

    async def work(conn: AsyncConnection) -> None:
        await conn.execute(guild_directory_state.update().where(guild_directory_state.c.state_id == GUILD_DIRECTORY_STATE_ID).values(bot_heartbeat_at=None))

    await _write(engine, work)


async def get_heartbeat(engine: AsyncEngine) -> Heartbeat | None:
    """The last recorded heartbeat, or None when there is none."""
    stmt = select(guild_directory_state.c.bot_heartbeat_at, guild_directory_state.c.bot_user_id).where(guild_directory_state.c.state_id == GUILD_DIRECTORY_STATE_ID)
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
    if row is None or row.bot_heartbeat_at is None:
        return None
    return Heartbeat(at=row.bot_heartbeat_at, bot_user_id=row.bot_user_id)


async def replace_guild(engine: AsyncEngine, *, guild_id: int, owner_id: int, administrator_role_ids: Iterable[int], members: Iterable[MemberSnapshot]) -> None:
    """Replace everything known about the guild (owner, administrator roles, members and their roles) in one transaction, inserting in batches of WRITE_BATCH_SIZE rows; the bot calls it when it joins a guild or a guild becomes available and its member list is complete.

    Readers see either the previous or the new state, never a mix; a member listed twice raises ValueError.
    """
    _check_id("guild_id", guild_id)
    _check_id("owner_id", owner_id)
    admin_roles = sorted(frozenset(_check_id("role_id", role_id) for role_id in administrator_role_ids))
    by_user: dict[int, frozenset[int]] = {}
    for member in members:
        if not isinstance(member, MemberSnapshot):
            raise TypeError("members must be MemberSnapshot values")
        if member.user_id in by_user:
            raise ValueError(f"member {member.user_id} is listed more than once")
        by_user[member.user_id] = _role_set(guild_id, member.role_ids)
    user_ids = sorted(by_user)

    async def work(conn: AsyncConnection) -> None:
        await _put_guild(conn, guild_id, owner_id)
        await conn.execute(delete(guild_administrator_role).where(guild_administrator_role.c.guild_id == guild_id))
        await _delete_member_rows(conn, guild_id)
        await _insert_batched(conn, guild_administrator_role, [{"guild_id": guild_id, "role_id": role_id, "updated_at": _DB_NOW} for role_id in admin_roles])
        await _insert_batched(conn, guild_member_snapshot, [{"guild_id": guild_id, "user_id": user_id, "updated_at": _DB_NOW} for user_id in user_ids])
        role_rows = [{"guild_id": guild_id, "user_id": user_id, "role_id": role_id} for user_id in user_ids for role_id in sorted(by_user[user_id])]
        await _insert_batched(conn, guild_member_role, role_rows)

    await _write(engine, work)


async def upsert_guild(engine: AsyncEngine, *, guild_id: int, owner_id: int) -> None:
    """Record the guild's owner, adding the guild without members when it is not yet known; the bot calls it when ownership is transferred."""
    _check_id("guild_id", guild_id)
    _check_id("owner_id", owner_id)

    async def work(conn: AsyncConnection) -> None:
        await _put_guild(conn, guild_id, owner_id)

    await _write(engine, work)


async def remove_guild(engine: AsyncEngine, *, guild_id: int) -> bool:
    """Forget the guild with its roles and members, so readers answer that the bot is not in it; returns False when it was not known."""
    _check_id("guild_id", guild_id)

    async def work(conn: AsyncConnection) -> bool:
        if not await _lock_guild(conn, guild_id):
            return False
        await conn.execute(delete(guild_administrator_role).where(guild_administrator_role.c.guild_id == guild_id))
        await _delete_member_rows(conn, guild_id)
        await conn.execute(delete(guild_snapshot).where(guild_snapshot.c.guild_id == guild_id))
        return True

    return await _write(engine, work)


async def prune_guilds(engine: AsyncEngine, *, keep_guild_ids: Iterable[int]) -> list[int]:
    """Forget every known guild not in keep_guild_ids, one guild per transaction in ascending order, and return the removed IDs; the bot calls it after its startup resync with the guilds it is in."""
    keep = frozenset(_check_id("guild_id", guild_id) for guild_id in keep_guild_ids)
    async with engine.connect() as conn:
        known = (await conn.execute(select(guild_snapshot.c.guild_id).order_by(guild_snapshot.c.guild_id))).scalars().all()
    return [guild_id for guild_id in known if guild_id not in keep and await remove_guild(engine, guild_id=guild_id)]


async def set_role_administrator(engine: AsyncEngine, *, guild_id: int, role_id: int, is_administrator: bool) -> bool:
    """Record whether the role grants the Administrator permission, which changes the answer for every member holding it; returns False and writes nothing when the guild is not known.

    The bot calls it when a role is created or its permissions change, with the guild's own ID for the @everyone role.
    """
    _check_id("guild_id", guild_id)
    _check_id("role_id", role_id)

    async def work(conn: AsyncConnection) -> bool:
        if not await _lock_guild(conn, guild_id):
            return False
        if is_administrator:
            stmt = mysql_insert(guild_administrator_role).values(guild_id=guild_id, role_id=role_id, updated_at=_DB_NOW)
            await conn.execute(stmt.on_duplicate_key_update(updated_at=_DB_NOW))
        else:
            await conn.execute(delete(guild_administrator_role).where(guild_administrator_role.c.guild_id == guild_id, guild_administrator_role.c.role_id == role_id))
        return True

    return await _write(engine, work)


async def remove_role(engine: AsyncEngine, *, guild_id: int, role_id: int) -> bool:
    """Forget a deleted role, both as an administrator role and from every member holding it; returns False when the guild is not known, and the @everyone role (the guild's own ID) raises ValueError."""
    _check_id("guild_id", guild_id)
    _check_id("role_id", role_id)
    if role_id == guild_id:
        raise ValueError("the @everyone role cannot be removed")

    async def work(conn: AsyncConnection) -> bool:
        if not await _lock_guild(conn, guild_id):
            return False
        await conn.execute(delete(guild_administrator_role).where(guild_administrator_role.c.guild_id == guild_id, guild_administrator_role.c.role_id == role_id))
        await conn.execute(delete(guild_member_role).where(guild_member_role.c.guild_id == guild_id, guild_member_role.c.role_id == role_id))
        return True

    return await _write(engine, work)


async def upsert_member(engine: AsyncEngine, *, guild_id: int, user_id: int, role_ids: Iterable[int]) -> bool:
    """Record the member with exactly role_ids as its roles; returns False and writes nothing when the guild is not known, because the next replace_guild() of it brings the member along."""
    _check_id("guild_id", guild_id)
    _check_id("user_id", user_id)
    roles = sorted(_role_set(guild_id, role_ids))

    async def work(conn: AsyncConnection) -> bool:
        if not await _lock_guild(conn, guild_id):
            return False
        stmt = mysql_insert(guild_member_snapshot).values(guild_id=guild_id, user_id=user_id, updated_at=_DB_NOW)
        await conn.execute(stmt.on_duplicate_key_update(updated_at=_DB_NOW))
        await conn.execute(delete(guild_member_role).where(guild_member_role.c.guild_id == guild_id, guild_member_role.c.user_id == user_id))
        await _insert_batched(conn, guild_member_role, [{"guild_id": guild_id, "user_id": user_id, "role_id": role_id} for role_id in roles])
        return True

    return await _write(engine, work)


async def remove_member(engine: AsyncEngine, *, guild_id: int, user_id: int) -> bool:
    """Forget the member and its roles; returns False when the guild or the member was not known."""
    _check_id("guild_id", guild_id)
    _check_id("user_id", user_id)

    async def work(conn: AsyncConnection) -> bool:
        if not await _lock_guild(conn, guild_id):
            return False
        await conn.execute(delete(guild_member_role).where(guild_member_role.c.guild_id == guild_id, guild_member_role.c.user_id == user_id))
        result = await conn.execute(delete(guild_member_snapshot).where(guild_member_snapshot.c.guild_id == guild_id, guild_member_snapshot.c.user_id == user_id))
        return result.rowcount == 1

    return await _write(engine, work)


class DbGuildInfo:
    """A GuildInfo answering from the guild directory, with the semantics of InMemoryGuildInfo: an owner is not implicitly an administrator, and the @everyone role (the guild's own ID) is held by every member.

    Every answer is unknown (None or False) while the heartbeat is missing or older than staleness, measured on the database clock, or on clock when one is given.
    Each call is one plain SELECT on its own connection, a non-locking read under READ COMMITTED, so it never waits for a writer and is safe to await while another connection holds a transaction.
    A database or connection error, and a reader pool with no free connection (has_role() asks once, guild_owner() and is_administrator() keep asking for LOOKUP_WAIT_SECONDS), is logged and answered as unknown, so callers see an unavailable guild or a missing permission rather than an exception.
    A process opens it with open(), which gives it a small engine of its own, so a busy main pool cannot delay the answers has_role() needs inside contract transactions; whoever opens it calls close().
    """

    def __init__(self, engine: AsyncEngine, *, staleness: timedelta, clock: Clock | None = None, owns_engine: bool = False) -> None:
        if staleness <= timedelta(0):
            raise ValueError("staleness must be positive")
        self._engine = engine
        self._staleness = staleness
        self._clock = clock
        self._owns_engine = owns_engine

    @classmethod
    def open(cls, url: URL, settings: Settings, *, clock: Clock | None = None) -> DbGuildInfo:
        """A DbGuildInfo with the configured staleness on a dedicated engine for url of READER_POOL_SIZE connections that waits at most READER_POOL_TIMEOUT_SECONDS for one; close() disposes the engine."""
        engine = create_engine(url, pool_size=READER_POOL_SIZE, max_overflow=0, pool_timeout=READER_POOL_TIMEOUT_SECONDS)
        return cls(engine, staleness=timedelta(seconds=settings.guild_directory_staleness_seconds), clock=clock, owns_engine=True)

    async def close(self) -> None:
        """Dispose the engine when open() created it; an engine passed in stays with its owner."""
        if self._owns_engine:
            await self._engine.dispose()

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def staleness(self) -> timedelta:
        return self._staleness

    def _fresh(self) -> ColumnElement[bool]:
        state = guild_directory_state.c
        if self._clock is None:
            microseconds = self._staleness // timedelta(microseconds=1)
            cutoff: Any = func.timestampadd(text("MICROSECOND"), -microseconds, _DB_NOW)
        else:
            cutoff = literal(ensure_utc(self._clock.now()) - self._staleness, UtcDateTime())
        return and_(state.state_id == GUILD_DIRECTORY_STATE_ID, state.bot_heartbeat_at > cutoff)

    async def _first(self, stmt: Any, *, wait: float) -> Any:
        """The first row of stmt (None when it has none), or _UNKNOWN after a database or connection error, or when no connection came free within wait seconds (one attempt when wait is 0); the failure is logged."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            try:
                async with self._engine.connect() as conn:
                    return (await conn.execute(stmt)).first()
            except PoolTimeoutError:
                if loop.time() >= deadline:
                    log.warning("guild directory read answered unknown: no reader connection was free")
                    return _UNKNOWN
            except (DBAPIError, OSError) as exc:
                log.warning("guild directory read answered unknown after %s", type(exc).__name__)
                return _UNKNOWN

    async def guild_owner(self, guild_id: int) -> int | None:
        """The owner's user ID, or None when the bot is not in the guild, its information is stale, or the directory could not be read within LOOKUP_WAIT_SECONDS."""
        if not _is_id(guild_id):
            return None
        stmt = select(guild_snapshot.c.owner_id).select_from(guild_snapshot.join(guild_directory_state, self._fresh())).where(guild_snapshot.c.guild_id == guild_id)
        row = await self._first(stmt, wait=LOOKUP_WAIT_SECONDS)
        return None if row is None or row is _UNKNOWN else int(row.owner_id)

    async def is_administrator(self, guild_id: int, user_id: int) -> bool:
        """Whether the user is a member holding a role (or @everyone) that grants Administrator; False means not an administrator, not a member, the bot not in the guild, stale information, or a directory that could not be read within LOOKUP_WAIT_SECONDS."""
        if not _is_id(guild_id) or not _is_id(user_id):
            return False
        member, admin, role = guild_member_snapshot.c, guild_administrator_role.c, guild_member_role.c
        held = select(role.role_id).where(role.guild_id == guild_id, role.user_id == user_id)
        admin_role = exists().where(admin.guild_id == guild_id, or_(admin.role_id == guild_id, admin.role_id.in_(held)))
        stmt = select(literal(1)).select_from(guild_member_snapshot.join(guild_directory_state, self._fresh())).where(member.guild_id == guild_id, member.user_id == user_id, admin_role)
        row = await self._first(stmt, wait=LOOKUP_WAIT_SECONDS)
        return row is not None and row is not _UNKNOWN

    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        """Whether the member holds the role; False means it does not, or the member, the guild or fresh information is unknown, including when no reader connection is free at once or the read fails."""
        if not _is_id(guild_id) or not _is_id(user_id) or not _is_id(role_id):
            return False
        if role_id == guild_id:
            member = guild_member_snapshot.c
            stmt = select(literal(1)).select_from(guild_member_snapshot.join(guild_directory_state, self._fresh())).where(member.guild_id == guild_id, member.user_id == user_id)
        else:
            role = guild_member_role.c
            stmt = select(literal(1)).select_from(guild_member_role.join(guild_directory_state, self._fresh())).where(role.guild_id == guild_id, role.user_id == user_id, role.role_id == role_id)
        row = await self._first(stmt, wait=0.0)
        return row is not None and row is not _UNKNOWN
