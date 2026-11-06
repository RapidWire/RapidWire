"""The guild directory writer: gateway listeners (guilds intent and the privileged members intent) mirror each guild's owner, administrator roles and members into rapidwire.core.guild_directory through one ordered write queue per guild, and record the heartbeat that tells readers the directory is current while the session is connected and no guild is stale."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import discord
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.client import EventHandler, RapidWireBot
from rapidwire.config import GUILD_HEARTBEAT_INTERVAL_SECONDS
from rapidwire.core import guild_directory as gd
from rapidwire.core.guild_directory import MemberSnapshot

__all__ = [
    "CHUNK_MEMBERS_PER_SECOND",
    "CHUNK_TIMEOUT_FACTOR",
    "CHUNK_TIMEOUT_MIN_SECONDS",
    "DRAIN_TIMEOUT_SECONDS",
    "RETRY_DELAY_MAX_SECONDS",
    "WITHDRAW_AFTER_FAILURES",
    "GuildDirectorySync",
    "GuildState",
    "administrator_role_ids",
    "chunk_timeout",
    "drop_chunk_request",
    "guild_snapshot",
    "member_role_ids",
    "retry_delay",
    "setup",
]

log = logging.getLogger("rapidwire.bot.guild_sync")

# Seconds shutdown waits for queued directory writes before cancelling them.
DRAIN_TIMEOUT_SECONDS: Final = 10.0
# The longest wait between resync attempts of a stale guild.
RETRY_DELAY_MAX_SECONDS: Final = 60.0
# Failed resync rounds after which a guild is removed from the directory, so it stops holding back the heartbeat of every other guild while its resync keeps being retried.
WITHDRAW_AFTER_FAILURES: Final = 5
# A member chunk request may take CHUNK_TIMEOUT_FACTOR times the larger of CHUNK_TIMEOUT_MIN_SECONDS and one second per CHUNK_MEMBERS_PER_SECOND members.
CHUNK_TIMEOUT_MIN_SECONDS: Final = 5.0
CHUNK_MEMBERS_PER_SECOND: Final = 10_000
CHUNK_TIMEOUT_FACTOR: Final = 2.0

type Write = Callable[[], Awaitable[None]]


def retry_delay(attempt: int) -> float:
    """Seconds before retry number attempt (1-based): 1, 2, 4 ... up to RETRY_DELAY_MAX_SECONDS."""
    return float(min(2 ** max(attempt - 1, 0), RETRY_DELAY_MAX_SECONDS))


def chunk_timeout(guild: discord.Guild) -> float:
    """Seconds the member chunk request of guild may take before its resync counts as failed."""
    return max(CHUNK_TIMEOUT_MIN_SECONDS, (guild.member_count or 0) / CHUNK_MEMBERS_PER_SECOND) * CHUNK_TIMEOUT_FACTOR


def drop_chunk_request(guild: discord.Guild) -> None:
    """Forget the guild's pending member chunk request in discord.py's connection state, so the next chunk() asks Discord again instead of waiting on the request that timed out (discord.py reuses it until the next READY); a state without that table is left alone."""
    requests = getattr(getattr(guild, "_state", None), "_chunk_requests", None)
    if isinstance(requests, dict):
        requests.pop(guild.id, None)


async def _chunk(guild: discord.Guild, bound: float) -> None:
    """Fetch the guild's members within bound seconds; on TimeoutError the stale request is dropped before the error propagates."""
    try:
        await asyncio.wait_for(guild.chunk(), timeout=bound)
    except TimeoutError:
        drop_chunk_request(guild)
        raise


@dataclass(frozen=True)
class GuildState:
    """The replace_guild() arguments for one guild."""

    owner_id: int
    administrator_role_ids: frozenset[int]
    members: tuple[MemberSnapshot, ...]


def member_role_ids(member: discord.Member) -> frozenset[int]:
    """The IDs of the member's roles, @everyone included."""
    return frozenset(role.id for role in member.roles)


def administrator_role_ids(guild: discord.Guild) -> frozenset[int]:
    """The IDs of the guild's roles whose permissions include Administrator."""
    return frozenset(role.id for role in guild.roles if role.permissions.administrator)


def guild_snapshot(guild: discord.Guild) -> GuildState:
    """The owner, administrator roles and every cached member with its roles; a guild without a known owner raises ValueError."""
    if guild.owner_id is None:
        raise ValueError(f"guild {guild.id} has no known owner")
    members = tuple(MemberSnapshot(member.id, member_role_ids(member)) for member in guild.members)
    return GuildState(owner_id=guild.owner_id, administrator_role_ids=administrator_role_ids(guild), members=members)


@dataclass
class _Item:
    label: str
    write: Write
    # A full write (resync, removal, withdrawal) sets the guild's whole entry; the others change part of it and are skipped while the guild is withdrawn.
    full: bool = False
    future: asyncio.Future[bool] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class GuildDirectorySync:
    """Mirrors a client's gateway view into the guild directory: register() adds its listeners and its startup and shutdown hooks to a RapidWireBot.

    Every listener enqueues before its first await, and the bot starts listener tasks in dispatch order, so each guild's queue holds its events in gateway order.
    A failed write marks its guild stale and resyncs it from the cache with growing delays; after WITHDRAW_AFTER_FAILURES failed rounds the guild is removed from the directory (readers answer unknown for it alone) and no longer holds back the heartbeat, while its resync keeps being retried.
    """

    def __init__(
        self,
        client: discord.Client,
        engine: AsyncEngine,
        *,
        heartbeat_interval: float = GUILD_HEARTBEAT_INTERVAL_SECONDS,
        retry_delay: Callable[[int], float] = retry_delay,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
        chunk_timeout: Callable[[discord.Guild], float] = chunk_timeout,
        withdraw_after: int = WITHDRAW_AFTER_FAILURES,
    ) -> None:
        self._client = client
        self._engine = engine
        self._interval = heartbeat_interval
        self._retry_delay = retry_delay
        self._drain_timeout = drain_timeout
        self._chunk_timeout = chunk_timeout
        self._withdraw_after = withdraw_after
        self._queues: dict[int, deque[_Item]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._dirty: set[int] = set()
        self._withdrawn: set[int] = set()
        self._clean = asyncio.Event()
        self._clean.set()
        self._failures: dict[int, int] = {}
        self._retries: dict[int, asyncio.Task[None]] = {}
        self._synced_session: dict[int, str | None] = {}
        self._generation = 0
        self._resync_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._clear_task: asyncio.Task[None] | None = None
        self._heartbeat_lock = asyncio.Lock()
        self._kick = asyncio.Event()
        self._connected = False
        self._resynced = False
        self._closed = False
        # A previous process may have left a heartbeat behind, so the startup hook clears it.
        self._maybe_recorded = True
        self._clear_failures = 0
        self.beats = 0

    def register(self, bot: RapidWireBot) -> None:
        """Add the gateway listeners, the startup hook and the shutdown hook to bot."""
        listeners: dict[str, EventHandler] = {
            "ready": self.on_ready,
            "resumed": self.on_resumed,
            "disconnect": self.on_disconnect,
            "guild_join": self.on_guild_join,
            "guild_available": self.on_guild_available,
            "guild_remove": self.on_guild_remove,
            "guild_unavailable": self.on_guild_unavailable,
            "guild_update": self.on_guild_update,
            "guild_role_create": self.on_guild_role_create,
            "guild_role_update": self.on_guild_role_update,
            "guild_role_delete": self.on_guild_role_delete,
            "member_join": self.on_member_join,
            "member_update": self.on_member_update,
            "raw_member_remove": self.on_raw_member_remove,
        }
        for event, handler in listeners.items():
            bot.add_listener(event, handler)
        bot.add_startup_hook(self.startup)
        bot.add_shutdown_hook(self.shutdown)

    @property
    def resynced(self) -> bool:
        """Whether the current session's full resync has finished."""
        return self._resynced

    @property
    def stale_guild_ids(self) -> frozenset[int]:
        """Guilds whose last write failed and whose resync has neither succeeded nor been replaced by a withdrawal yet."""
        return frozenset(self._dirty)

    @property
    def withdrawn_guild_ids(self) -> frozenset[int]:
        """Guilds removed from the directory after repeated failures, whose resync is still being retried."""
        return frozenset(self._withdrawn)

    @property
    def heartbeat_active(self) -> bool:
        """Whether heartbeats are recorded now: connected, resynced, no stale guild and not shut down."""
        return self._connected and self._resynced and not self._dirty and not self._closed

    def _session(self) -> str | None:
        session = getattr(getattr(self._client, "ws", None), "session_id", None)
        return session if isinstance(session, str) else None

    def _available(self, guild_id: int) -> discord.Guild | None:
        guild = self._client.get_guild(guild_id)
        return None if guild is None or guild.unavailable else guild

    def _bot_user_id(self) -> int | None:
        user = self._client.user
        return None if user is None else user.id

    def _enqueue(self, guild_id: int, label: str, write: Write, *, full: bool = False) -> asyncio.Future[bool]:
        """Append a write to the guild's queue, starting its worker when idle; the future answers whether the write succeeded or was skipped."""
        item = _Item(label, write, full)
        if self._closed:
            item.future.set_result(False)
            return item.future
        queue = self._queues.get(guild_id)
        if queue is None:
            queue = self._queues[guild_id] = deque()
            self._workers[guild_id] = asyncio.create_task(self._work(guild_id, queue), name=f"guild directory {guild_id}")
        queue.append(item)
        return item.future

    async def _work(self, guild_id: int, queue: deque[_Item]) -> None:
        item: _Item | None = None
        try:
            while queue:
                item = queue.popleft()
                ok = await self._run(guild_id, item)
                if not item.future.done():
                    item.future.set_result(ok)
                item = None
        finally:
            for pending in (item, *queue) if item is not None else queue:
                pending.future.cancel()
            queue.clear()
            del self._queues[guild_id]
            del self._workers[guild_id]

    async def _run(self, guild_id: int, item: _Item) -> bool:
        if not item.full and guild_id in self._withdrawn:
            return True
        try:
            await item.write()
        except Exception as exc:
            self._failed(guild_id, item.label, exc)
            return False
        return True

    def _failed(self, guild_id: int, label: str, exc: Exception) -> None:
        """Log the failure (with its traceback only when the guild was healthy), mark the guild stale unless it is withdrawn, and schedule its resync."""
        failures = self._failures.get(guild_id, 0)
        if failures == 0:
            log.error("guild directory write failed", exc_info=exc, extra={"guild_id": guild_id, "write": label})
        else:
            log.warning("guild directory write failed again", extra={"guild_id": guild_id, "write": label, "failures": failures, "error": type(exc).__name__})
        if guild_id not in self._withdrawn:
            self._dirty.add(guild_id)
            self._clean.clear()
        if self._closed or guild_id in self._retries:
            return
        failures = self._failures[guild_id] = failures + 1
        if failures >= self._withdraw_after and guild_id not in self._withdrawn:
            self._enqueue_withdraw(guild_id, failures)
        self._retries[guild_id] = asyncio.create_task(self._retry(guild_id, self._retry_delay(failures)), name=f"guild directory retry {guild_id}")

    def _settled(self, guild_id: int) -> None:
        self._dirty.discard(guild_id)
        if not self._dirty:
            self._clean.set()
            self._kick.set()

    def _mark_clean(self, guild_id: int) -> None:
        if guild_id in self._withdrawn:
            self._withdrawn.discard(guild_id)
            log.info("guild directory guild is no longer withdrawn", extra={"guild_id": guild_id})
        self._failures.pop(guild_id, None)
        self._settled(guild_id)

    async def _retry(self, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._retries.pop(guild_id, None)
        self._enqueue_ensure(guild_id, force=True)

    def _enqueue_withdraw(self, guild_id: int, failures: int) -> None:
        """Queue the removal of a guild that keeps failing; a resync that succeeded before it runs keeps the guild."""

        async def write() -> None:
            if guild_id not in self._dirty:
                return
            await gd.remove_guild(self._engine, guild_id=guild_id)
            self._synced_session.pop(guild_id, None)
            self._withdrawn.add(guild_id)
            self._settled(guild_id)
            log.error("guild directory withdrew a guild after repeated failures and keeps retrying it", extra={"guild_id": guild_id, "failures": failures})

        self._enqueue(guild_id, "withdraw", write, full=True)

    def _enqueue_ensure(self, guild_id: int, *, force: bool) -> asyncio.Future[bool]:
        """Queue a resync of the guild from the cache: replace_guild() when the bot is in it and it is available, remove_guild() otherwise; without force a guild already resynced in this session and not stale or withdrawn is left alone."""

        async def write() -> None:
            session = self._session()
            healthy = guild_id not in self._dirty and guild_id not in self._withdrawn
            if not force and healthy and session is not None and self._synced_session.get(guild_id) == session:
                return
            guild = self._available(guild_id)
            if guild is None:
                await gd.remove_guild(self._engine, guild_id=guild_id)
                self._synced_session.pop(guild_id, None)
            else:
                if not guild.chunked:
                    await _chunk(guild, self._chunk_timeout(guild))
                state = guild_snapshot(guild)
                await gd.replace_guild(self._engine, guild_id=guild_id, owner_id=state.owner_id, administrator_role_ids=state.administrator_role_ids, members=state.members)
                self._synced_session[guild_id] = session
            self._mark_clean(guild_id)

        return self._enqueue(guild_id, "resync", write, full=True)

    def _enqueue_remove(self, guild_id: int) -> None:
        async def write() -> None:
            await gd.remove_guild(self._engine, guild_id=guild_id)
            self._synced_session.pop(guild_id, None)
            self._mark_clean(guild_id)

        self._enqueue(guild_id, "remove guild", write, full=True)

    async def _settle(self, futures: Iterable[asyncio.Future[bool]], generation: int) -> bool:
        """Wait for the writes and then until no guild is stale; False when a newer session or shutdown superseded this resync."""
        await asyncio.gather(*futures, return_exceptions=True)
        await self._clean.wait()
        return generation == self._generation and not self._closed

    async def _prune(self, generation: int) -> list[int] | None:
        attempt = 0
        while generation == self._generation and not self._closed:
            keep = [guild.id for guild in self._client.guilds if not guild.unavailable]
            try:
                return await gd.prune_guilds(self._engine, keep_guild_ids=keep)
            except Exception:
                attempt += 1
                log.exception("guild directory prune failed")
                await asyncio.sleep(self._retry_delay(attempt))
        return None

    async def _resync_all(self, generation: int) -> None:
        """The full resync of a new session: clear the heartbeat, resync every guild, forget guilds the bot is no longer in, then start heartbeats."""
        await self._clear_heartbeat()
        guild_ids = [guild.id for guild in self._client.guilds]
        if not await self._settle([self._enqueue_ensure(guild_id, force=False) for guild_id in guild_ids], generation):
            return
        removed = await self._prune(generation)
        if removed is None:
            return
        # A guild joined while the prune ran may have been removed by it; resyncing it after the removal restores it.
        repairs = [self._enqueue_ensure(guild_id, force=True) for guild_id in removed if self._available(guild_id) is not None]
        if not await self._settle(repairs, generation):
            return
        self._resynced = True
        log.info("guild directory resynced", extra={"guilds": len(guild_ids), "pruned": len(removed), "withdrawn": len(self._withdrawn)})
        self._kick.set()

    async def _clear_heartbeat(self) -> bool:
        """Clear the heartbeat when one may be recorded; False when the clear failed, which is logged."""
        async with self._heartbeat_lock:
            if not self._maybe_recorded:
                return True
            try:
                await gd.clear_heartbeat(self._engine)
            except Exception as exc:
                if self._clear_failures == 0:
                    log.error("guild directory heartbeat could not be cleared", exc_info=exc)
                else:
                    log.warning("guild directory heartbeat could not be cleared again", extra={"failures": self._clear_failures, "error": type(exc).__name__})
                self._clear_failures += 1
                return False
            self._maybe_recorded = False
            self._clear_failures = 0
            return True

    async def _retry_clear(self) -> None:
        """Retry the heartbeat clear with growing delays until it succeeds, the session is back or the sync shuts down."""
        attempt = 1
        while True:
            await asyncio.sleep(self._retry_delay(attempt))
            if self._connected or self._closed or await self._clear_heartbeat():
                return
            attempt += 1

    async def _beat(self) -> None:
        async with self._heartbeat_lock:
            if not self.heartbeat_active:
                return
            self._maybe_recorded = True
            try:
                await gd.record_heartbeat(self._engine, bot_user_id=self._bot_user_id())
            except Exception:
                log.exception("guild directory heartbeat failed")
            else:
                self.beats += 1

    async def _heartbeat_loop(self) -> None:
        while True:
            self._kick.clear()
            if self._closed:
                return
            await self._beat()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=self._interval)

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="guild directory heartbeat")

    async def startup(self) -> None:
        """Clear a heartbeat a previous process may have left behind, so readers do not trust the directory before this session's resync; a failure is logged and the next ready clears it again."""
        await self._clear_heartbeat()

    async def on_ready(self) -> None:
        """A new gateway session: stop heartbeats and resync every guild."""
        if self._closed:
            return
        self._generation += 1
        self._connected = True
        self._resynced = False
        self._start_heartbeat()
        self._resync_task = asyncio.create_task(self._resync_all(self._generation), name="guild directory resync")

    async def on_resumed(self) -> None:
        """A resumed session keeps the cache, so heartbeats restart at once when its resync had finished."""
        if self._closed:
            return
        self._connected = True
        self._kick.set()

    async def on_disconnect(self) -> None:
        """Stop heartbeats and clear the heartbeat, retrying a failed clear while disconnected, so readers treat guild information as unavailable until the session is back."""
        if self._closed:
            return
        self._connected = False
        if not await self._clear_heartbeat() and (self._clear_task is None or self._clear_task.done()):
            self._clear_task = asyncio.create_task(self._retry_clear(), name="guild directory heartbeat clear")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self._closed:
            self._enqueue_ensure(guild.id, force=True)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        if not self._closed:
            self._enqueue_ensure(guild.id, force=True)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if not self._closed:
            self._enqueue_remove(guild.id)

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        if not self._closed:
            self._enqueue_remove(guild.id)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        owner_id = after.owner_id
        if self._closed or owner_id is None or owner_id == before.owner_id:
            return
        guild_id = after.id

        async def write() -> None:
            if self._available(guild_id) is not None:
                await gd.upsert_guild(self._engine, guild_id=guild_id, owner_id=owner_id)

        self._enqueue(guild_id, "owner", write)

    def _enqueue_role(self, role: discord.Role, is_administrator: bool) -> None:
        guild_id, role_id = role.guild.id, role.id

        async def write() -> None:
            await gd.set_role_administrator(self._engine, guild_id=guild_id, role_id=role_id, is_administrator=is_administrator)

        self._enqueue(guild_id, "role", write)

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if not self._closed and role.permissions.administrator:
            self._enqueue_role(role, True)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if not self._closed and before.permissions.administrator != after.permissions.administrator:
            self._enqueue_role(after, after.permissions.administrator)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild_id, role_id = role.guild.id, role.id
        if self._closed or role_id == guild_id:
            return

        async def write() -> None:
            await gd.remove_role(self._engine, guild_id=guild_id, role_id=role_id)

        self._enqueue(guild_id, "remove role", write)

    def _enqueue_member(self, member: discord.Member) -> None:
        guild_id, user_id, role_ids = member.guild.id, member.id, member_role_ids(member)

        async def write() -> None:
            await gd.upsert_member(self._engine, guild_id=guild_id, user_id=user_id, role_ids=role_ids)

        self._enqueue(guild_id, "member", write)

    async def on_member_join(self, member: discord.Member) -> None:
        if not self._closed:
            self._enqueue_member(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._closed and member_role_ids(before) != member_role_ids(after):
            self._enqueue_member(after)

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        """Every member removal, cached member or not."""
        guild_id, user_id = payload.guild_id, payload.user.id
        if self._closed:
            return

        async def write() -> None:
            await gd.remove_member(self._engine, guild_id=guild_id, user_id=user_id)

        self._enqueue(guild_id, "remove member", write)

    async def flush(self) -> None:
        """Wait until every queued write has run."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)

    async def wait_resynced(self) -> None:
        """Wait for the current session's full resync task to end."""
        if self._resync_task is not None:
            await self._resync_task

    async def shutdown(self) -> None:
        """Stop resyncs, retries and heartbeats, clear the heartbeat when one may be recorded, then give queued writes DRAIN_TIMEOUT_SECONDS to finish and cancel and log the rest."""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._clean.set()
        self._kick.set()
        # The clear waits for a beat in progress, and once closed no beat records again, so readers stop trusting the directory before the drain.
        await self._clear_heartbeat()
        background: list[asyncio.Task[Any]] = [task for task in (self._resync_task, self._clear_task, *self._retries.values()) if task is not None]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        tasks = [task for task in (self._heartbeat_task, *self._workers.values()) if task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._drain_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if pending:
                log.warning("guild directory writes were cancelled at shutdown", extra={"cancelled": len(pending)})


def setup(bot: RapidWireBot) -> None:
    """Register the guild directory listeners and hooks on bot."""
    GuildDirectorySync(bot, bot.context.engine).register(bot)
