"""The guild directory writer against the real database: gateway events, the ready resync with prune, heartbeats, per-guild ordering, failure recovery and withdrawal, shutdown, and agreement of the directory reader with the cache reader."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.guild_info import CacheGuildInfo
from rapidwire.bot.guild_sync import GuildDirectorySync
from rapidwire.config import Settings
from rapidwire.core import guild_directory as gd
from rapidwire.core.guild_directory import DbGuildInfo, MemberSnapshot
from rapidwire.db.tables import guild_administrator_role, guild_member_role, guild_member_snapshot, guild_snapshot
from tests.unit.bot.test_t5_guild_sync import SyncClient, SyncGuild, d, raw_remove

INTERVAL = 0.05

type Directory = dict[int, tuple[int, frozenset[int], dict[int, frozenset[int]]]]


async def directory(db: AsyncEngine) -> Directory:
    """Every guild in the directory: owner, administrator roles and members with their roles (@everyone not stored)."""
    async with db.connect() as conn:
        guilds = (await conn.execute(select(guild_snapshot.c.guild_id, guild_snapshot.c.owner_id))).all()
        admins = (await conn.execute(select(guild_administrator_role.c.guild_id, guild_administrator_role.c.role_id))).all()
        members = (await conn.execute(select(guild_member_snapshot.c.guild_id, guild_member_snapshot.c.user_id))).all()
        roles = (await conn.execute(select(guild_member_role.c.guild_id, guild_member_role.c.user_id, guild_member_role.c.role_id))).all()
    result: Directory = {}
    for guild_id, owner_id in guilds:
        member_roles = {user_id: frozenset(role for g, u, role in roles if g == guild_id and u == user_id) for g, user_id in members if g == guild_id}
        result[guild_id] = (owner_id, frozenset(role for g, role in admins if g == guild_id), member_roles)
    return result


async def _member_listed(db: AsyncEngine, guild_id: int, user_id: int) -> bool:
    return user_id in (await directory(db)).get(guild_id, (0, frozenset(), {}))[2]


def expected(guild: SyncGuild) -> tuple[int, frozenset[int], dict[int, frozenset[int]]]:
    """The directory entry the cache of guild should produce."""
    assert guild.owner_id is not None
    admins = frozenset(role.id for role in guild.roles if role.permissions.administrator)
    return guild.owner_id, admins, {member.id: frozenset(member.role_ids) & (guild.role_map.keys() - {guild.id}) for member in guild.members}


async def eventually(condition: Callable[[], bool | Awaitable[bool]], *, within: float = 5.0) -> None:
    """Wait until condition holds, failing after within seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        value = condition()
        if not isinstance(value, bool):
            value = await value
        if value:
            return
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


@pytest.fixture
async def make_sync(db: AsyncEngine) -> AsyncIterator[Callable[..., GuildDirectorySync]]:
    """Build syncs over a fake client on the test database with a short heartbeat interval and retry delay; each is shut down after the test."""
    created: list[GuildDirectorySync] = []

    def make(client: SyncClient, **kwargs: Any) -> GuildDirectorySync:
        kwargs.setdefault("heartbeat_interval", INTERVAL)
        kwargs.setdefault("retry_delay", lambda attempt: 0.01)
        sync = GuildDirectorySync(d(client), db, **kwargs)
        created.append(sync)
        return sync

    yield make
    for sync in created:
        await sync.shutdown()


def populated(owner_id: int = 11, *, chunked: bool = True) -> SyncGuild:
    guild = SyncGuild(owner_id=owner_id, chunked=chunked)
    admin = guild.add_role(administrator=True)
    plain = guild.add_role()
    guild.add_member(owner_id, role_ids=[plain.id])
    guild.add_member(role_ids=[admin.id])
    guild.add_member()
    return guild


async def test_ready_resyncs_every_guild_prunes_and_then_beats(db: AsyncEngine, make_sync: Any) -> None:
    first, second = populated(), populated(owner_id=21, chunked=False)
    stale = SyncGuild(owner_id=31)
    await gd.replace_guild(db, guild_id=stale.id, owner_id=31, administrator_role_ids=[], members=[MemberSnapshot(31)])
    await gd.replace_guild(db, guild_id=first.id, owner_id=99, administrator_role_ids=[1], members=[MemberSnapshot(98)])
    client = SyncClient(first, second)
    sync = make_sync(client)
    await sync.on_ready()
    await sync.wait_resynced()
    assert sync.resynced
    assert second.chunk_calls == 1
    assert await directory(db) == {first.id: expected(first), second.id: expected(second)}
    await eventually(lambda: sync.beats >= 2)
    heartbeat = await gd.get_heartbeat(db)
    assert heartbeat is not None
    assert heartbeat.bot_user_id == client.user.id


async def test_heartbeat_starts_only_after_the_resync(db: AsyncEngine, make_sync: Any) -> None:
    slow = populated(chunked=False)
    slow.gate = asyncio.Event()
    await gd.record_heartbeat(db, bot_user_id=5)
    sync = make_sync(SyncClient(slow, populated()))
    await sync.on_ready()
    await eventually(lambda: slow.chunk_calls == 1)
    await asyncio.sleep(INTERVAL * 4)
    assert await gd.get_heartbeat(db) is None
    assert sync.beats == 0
    assert not sync.heartbeat_active
    slow.gate.set()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    assert await gd.get_heartbeat(db) is not None


async def test_disconnect_stops_and_clears_and_resume_restarts(db: AsyncEngine, make_sync: Any) -> None:
    sync = make_sync(SyncClient(populated()))
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    await sync.on_disconnect()
    assert await gd.get_heartbeat(db) is None
    beats = sync.beats
    await asyncio.sleep(INTERVAL * 4)
    assert sync.beats == beats
    assert await gd.get_heartbeat(db) is None
    await sync.on_resumed()
    await eventually(lambda: sync.beats > beats)
    assert await gd.get_heartbeat(db) is not None


async def test_new_session_resyncs_again(db: AsyncEngine, make_sync: Any) -> None:
    guild = populated()
    client = SyncClient(guild)
    sync = make_sync(client)
    await sync.on_ready()
    await sync.wait_resynced()
    await sync.on_disconnect()
    newcomer = guild.add_member(role_ids=[next(iter(guild.role_map))])
    client.ws.session_id = "session-2"
    await sync.on_ready()
    assert not sync.resynced
    await sync.wait_resynced()
    assert newcomer.id in (await directory(db))[guild.id][2]
    await eventually(lambda: sync.beats >= 2)


async def test_resync_skips_guilds_already_synced_in_the_session(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild))
    calls: list[int] = []
    original = gd.replace_guild

    async def counting(engine: AsyncEngine, **kwargs: Any) -> None:
        calls.append(kwargs["guild_id"])
        await original(engine, **kwargs)

    monkeypatch.setattr(gd, "replace_guild", counting)
    await sync.on_guild_available(d(guild))
    await sync.on_ready()
    await sync.wait_resynced()
    assert calls == [guild.id]
    assert await directory(db) == {guild.id: expected(guild)}


async def test_shutdown_drains_writes_and_clears_the_heartbeat(db: AsyncEngine, make_sync: Any) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild))
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    member = guild.add_member()
    await sync.on_member_join(d(member))
    await sync.shutdown()
    assert member.id in (await directory(db))[guild.id][2]
    assert await gd.get_heartbeat(db) is None
    late = guild.add_member()
    await sync.on_member_join(d(late))
    await sync.flush()
    assert late.id not in (await directory(db))[guild.id][2]
    assert not sync.heartbeat_active


async def test_every_event_maps_to_the_directory(db: AsyncEngine, make_sync: Any) -> None:
    guild = populated(chunked=False)
    client = SyncClient()
    sync = make_sync(client)

    async def check() -> None:
        await sync.flush()
        assert (await directory(db))[guild.id] == expected(guild)

    client.add_guild(guild)
    await sync.on_guild_join(d(guild))
    await check()
    assert guild.chunk_calls == 1

    before = guild.copy()
    guild.owner_id = guild.members[1].id
    await sync.on_guild_update(d(before), d(guild))
    await check()

    role = guild.add_role(administrator=True)
    await sync.on_guild_role_create(d(role))
    await check()

    joined = guild.add_member(role_ids=[role.id])
    await sync.on_member_join(d(joined))
    await check()

    old = joined.copy()
    other = guild.add_role()
    joined.role_ids = {other.id}
    await sync.on_member_update(d(old), d(joined))
    await check()

    demoted = SyncGuild(guild.id).add_role(role.id, administrator=True)
    role.permissions = discord.Permissions(administrator=False)
    await sync.on_guild_role_update(d(demoted), d(role))
    await check()

    holder = guild.add_member(role_ids=[other.id])
    await sync.on_member_join(d(holder))
    del guild.role_map[other.id]
    await sync.on_guild_role_delete(d(other))
    await check()
    assert (await directory(db))[guild.id][2][holder.id] == frozenset()

    del guild.member_map[joined.id]
    await sync.on_raw_member_remove(raw_remove(guild.id, joined.id))
    await check()

    guild.unavailable = True
    await sync.on_guild_unavailable(d(guild))
    await sync.flush()
    assert guild.id not in await directory(db)

    guild.unavailable = False
    await sync.on_guild_available(d(guild))
    await check()

    del client.guild_map[guild.id]
    await sync.on_guild_remove(d(guild))
    await sync.flush()
    assert await directory(db) == {}


async def test_owner_unchanged_update_and_non_admin_role_change_write_nothing(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild))
    await sync.on_guild_join(d(guild))
    await sync.flush()
    writes: list[str] = []
    for name in ("upsert_guild", "set_role_administrator", "upsert_member"):

        async def spy(*args: Any, _name: str = name, **kwargs: Any) -> None:
            writes.append(_name)

        monkeypatch.setattr(gd, name, spy)
    await sync.on_guild_update(d(guild.copy()), d(guild))
    plain = guild.add_role()
    await sync.on_guild_role_create(d(plain))
    await sync.on_guild_role_update(d(plain), d(plain))
    member = guild.members[0]
    await sync.on_member_update(d(member.copy()), d(member))
    await sync.flush()
    assert writes == []


async def test_dispatched_events_keep_gateway_order_per_guild(db: AsyncEngine, bot_settings: Settings, make_sync: Any) -> None:
    first, second = populated(), populated()
    sync = make_sync(SyncClient(first, second))
    bot = RapidWireBot(bot_settings, db)
    sync.register(bot)
    await sync.on_guild_join(d(first))
    await sync.on_guild_join(d(second))
    await sync.flush()
    roles = {guild.id: [guild.add_role() for _ in range(5)] for guild in (first, second)}
    members = {guild.id: guild.members[2] for guild in (first, second)}
    async with bot:
        for step in range(60):
            for guild in (first, second):
                member = members[guild.id]
                before = member.copy()
                member.role_ids = {roles[guild.id][step % 5].id, roles[guild.id][(step * 3) % 5].id}
                bot.dispatch("member_update", d(before), d(member.copy()))
        for guild in (first, second):
            member = members[guild.id]
            before = member.copy()
            member.role_ids = {role.id for role in roles[guild.id][:3]}
            bot.dispatch("member_update", d(before), d(member.copy()))
        removed = first.members[1]
        bot.dispatch("raw_member_remove", raw_remove(first.id, removed.id))
        del first.member_map[removed.id]
        # One pass of the event loop starts every listener task, and each enqueues before its first await.
        await asyncio.sleep(0)
        assert sum(len(queue) for queue in sync._queues.values()) >= 121
        await sync.flush()
        state = await directory(db)
        assert state[first.id] == expected(first)
        assert state[second.id] == expected(second)


async def test_a_failed_write_keeps_the_worker_and_resyncs_the_guild(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild))
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    original_upsert, original_replace = gd.upsert_member, gd.replace_guild
    failures = [1]
    resync_entered, resync_gate = asyncio.Event(), asyncio.Event()

    async def flaky(engine: AsyncEngine, **kwargs: Any) -> bool:
        if failures:
            failures.pop()
            raise RuntimeError("database went away")
        return await original_upsert(engine, **kwargs)

    async def gated(engine: AsyncEngine, **kwargs: Any) -> None:
        # The retried resync waits here, so the guild stays stale for as long as the test needs.
        resync_entered.set()
        await resync_gate.wait()
        await original_replace(engine, **kwargs)

    monkeypatch.setattr(gd, "upsert_member", flaky)
    monkeypatch.setattr(gd, "replace_guild", gated)
    lost = guild.add_member()
    kept = guild.add_member()
    with caplog.at_level(logging.ERROR, logger="rapidwire.bot.guild_sync"):
        await sync.on_member_join(d(lost))
        await sync.on_member_join(d(kept))
        await asyncio.wait_for(resync_entered.wait(), timeout=5)
    assert any(record.getMessage() == "guild directory write failed" for record in caplog.records)
    await eventually(lambda: _member_listed(db, guild.id, kept.id))
    assert lost.id not in (await directory(db))[guild.id][2]
    assert sync.stale_guild_ids == {guild.id}
    assert not sync.heartbeat_active
    beats = sync.beats
    await asyncio.sleep(INTERVAL * 3)
    assert sync.beats == beats
    assert sync.stale_guild_ids == {guild.id}
    resync_gate.set()
    await eventually(lambda: not sync.stale_guild_ids)
    await sync.flush()
    assert (await directory(db))[guild.id] == expected(guild)
    await eventually(lambda: sync.beats > beats)


async def test_failed_guild_resync_is_retried_before_heartbeats_start(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    good, bad = populated(), populated()
    sync = make_sync(SyncClient(good, bad))
    original = gd.replace_guild
    failures = [1, 2]

    async def flaky(engine: AsyncEngine, **kwargs: Any) -> None:
        if kwargs["guild_id"] == bad.id and failures:
            failures.pop()
            raise RuntimeError("lock wait timeout")
        await original(engine, **kwargs)

    monkeypatch.setattr(gd, "replace_guild", flaky)
    await sync.on_ready()
    await sync.wait_resynced()
    assert failures == []
    assert sync.resynced
    assert await directory(db) == {good.id: expected(good), bad.id: expected(bad)}
    await eventually(lambda: sync.beats >= 1)


async def test_a_guild_joined_during_the_prune_is_restored(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = populated()
    client = SyncClient(guild)
    sync = make_sync(client)
    joined = populated()
    original = gd.prune_guilds

    async def racing(engine: AsyncEngine, *, keep_guild_ids: Any) -> list[int]:
        client.add_guild(joined)
        await gd.replace_guild(engine, guild_id=joined.id, owner_id=11, administrator_role_ids=[], members=[])
        return await original(engine, keep_guild_ids=keep_guild_ids)

    monkeypatch.setattr(gd, "prune_guilds", racing)
    await sync.on_ready()
    await sync.wait_resynced()
    assert await directory(db) == {guild.id: expected(guild), joined.id: expected(joined)}


def messages(caplog: pytest.LogCaptureFixture, text: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == text]


async def heartbeat_cleared(db: AsyncEngine) -> bool:
    return await gd.get_heartbeat(db) is None


async def test_a_chunk_that_times_out_marks_the_guild_stale_and_is_retried(db: AsyncEngine, make_sync: Any, caplog: pytest.LogCaptureFixture) -> None:
    slow = populated(chunked=False)
    slow.gate = asyncio.Event()
    sync = make_sync(SyncClient(slow), chunk_timeout=lambda guild: 0.05, withdraw_after=1000)
    with caplog.at_level(logging.WARNING, logger="rapidwire.bot.guild_sync"):
        await sync.on_ready()
        await eventually(lambda: slow.chunk_calls >= 3)
    assert sync.stale_guild_ids == {slow.id}
    assert not sync.resynced
    assert not sync.heartbeat_active
    (first,) = messages(caplog, "guild directory write failed")
    assert first.exc_info is not None
    assert first.exc_info[0] is TimeoutError
    assert all(record.exc_info is None for record in messages(caplog, "guild directory write failed again"))
    slow.gate.set()
    await sync.wait_resynced()
    assert await directory(db) == {slow.id: expected(slow)}
    await eventually(lambda: sync.beats >= 1)


async def test_a_guild_that_keeps_failing_is_withdrawn_and_the_others_keep_beating(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    good, ownerless = populated(), populated()
    ownerless.owner_id = None
    await gd.replace_guild(db, guild_id=ownerless.id, owner_id=11, administrator_role_ids=[], members=[MemberSnapshot(11)])
    sync = make_sync(SyncClient(good, ownerless), withdraw_after=3)
    reader = DbGuildInfo(db, staleness=timedelta(seconds=60))
    with caplog.at_level(logging.INFO, logger="rapidwire.bot.guild_sync"):
        await sync.on_ready()
        await sync.wait_resynced()
        await eventually(lambda: sync.beats >= 1)
        assert sync.withdrawn_guild_ids == {ownerless.id}
        assert sync.stale_guild_ids == set()
        assert sync.heartbeat_active
        assert await directory(db) == {good.id: expected(good)}
        assert await reader.guild_owner(good.id) == 11
        assert await reader.guild_owner(ownerless.id) is None
        assert len(messages(caplog, "guild directory withdrew a guild after repeated failures and keeps retrying it")) == 1

        upserts: list[int] = []
        original = gd.upsert_member

        async def spy(engine: AsyncEngine, **kwargs: Any) -> bool:
            upserts.append(kwargs["guild_id"])
            return await original(engine, **kwargs)

        monkeypatch.setattr(gd, "upsert_member", spy)
        retried = len(messages(caplog, "guild directory write failed again"))
        await sync.on_member_join(d(ownerless.add_member()))
        await eventually(lambda: len(messages(caplog, "guild directory write failed again")) >= retried + 2)
        assert upserts == []
        assert ownerless.id not in await directory(db)

        ownerless.owner_id = 11
        await eventually(lambda: not sync.withdrawn_guild_ids)
    tracebacks = [record for record in caplog.records if record.exc_info is not None and getattr(record, "guild_id", None) == ownerless.id]
    assert len(tracebacks) == 1
    await eventually(lambda: ownerless.id in sync._synced_session)
    assert (await directory(db))[ownerless.id] == expected(ownerless)
    assert await reader.guild_owner(ownerless.id) == 11


async def test_startup_clears_a_heartbeat_left_by_a_previous_process(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await gd.record_heartbeat(db, bot_user_id=5)

    async def broken(engine: AsyncEngine) -> None:
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(gd, "clear_heartbeat", broken)
        with caplog.at_level(logging.ERROR, logger="rapidwire.bot.guild_sync"):
            await make_sync(SyncClient()).startup()
    assert messages(caplog, "guild directory heartbeat could not be cleared")
    assert await gd.get_heartbeat(db) is not None
    await make_sync(SyncClient()).startup()
    assert await gd.get_heartbeat(db) is None


async def test_a_failed_clear_on_disconnect_is_retried_while_disconnected(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sync = make_sync(SyncClient(populated()))
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    original = gd.clear_heartbeat
    calls: list[int] = []

    async def flaky(engine: AsyncEngine) -> None:
        calls.append(len(calls))
        if len(calls) <= 2:
            raise RuntimeError("lock wait timeout")
        await original(engine)

    monkeypatch.setattr(gd, "clear_heartbeat", flaky)
    await sync.on_disconnect()
    assert await gd.get_heartbeat(db) is not None
    await eventually(lambda: heartbeat_cleared(db))
    assert len(calls) == 3
    await asyncio.sleep(INTERVAL * 3)
    assert len(calls) == 3


async def test_directory_and_cache_readers_agree(db: AsyncEngine, make_sync: Any) -> None:
    open_guild = SyncGuild(owner_id=11)
    open_guild.default_role.permissions = discord.Permissions(administrator=True)
    open_plain = open_guild.add_role()
    open_guild.add_member(11)
    open_guild.add_member(role_ids=[open_plain.id])
    ranked = SyncGuild(owner_id=21)
    admin = ranked.add_role(administrator=True)
    plain = ranked.add_role()
    owner = ranked.add_member(21, role_ids=[plain.id])
    admin_member = ranked.add_member(role_ids=[admin.id, 999])
    member = ranked.add_member()
    client = SyncClient(open_guild, ranked)
    sync = make_sync(client)
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    readers = (DbGuildInfo(db, staleness=timedelta(seconds=60)), CacheGuildInfo(d(client)))
    users = [*open_guild.member_map, *ranked.member_map, 404]
    roles = [open_guild.id, open_plain.id, ranked.id, admin.id, plain.id, 999, 405]
    for guild_id in (open_guild.id, ranked.id, 406):
        owners = [await reader.guild_owner(guild_id) for reader in readers]
        assert owners[0] == owners[1], guild_id
        for user_id in users:
            admins = [await reader.is_administrator(guild_id, user_id) for reader in readers]
            assert admins[0] == admins[1], (guild_id, user_id)
            for role_id in roles:
                held = [await reader.has_role(guild_id, user_id, role_id) for reader in readers]
                assert held[0] == held[1], (guild_id, user_id, role_id)
    for reader in readers:
        assert await reader.guild_owner(ranked.id) == owner.id
        assert all([await reader.is_administrator(open_guild.id, user_id) for user_id in open_guild.member_map])
        assert not await reader.is_administrator(ranked.id, owner.id)
        assert await reader.is_administrator(ranked.id, admin_member.id)
        assert not await reader.is_administrator(ranked.id, member.id)
        assert await reader.has_role(ranked.id, member.id, ranked.id)
        assert not await reader.has_role(ranked.id, 404, ranked.id)
        assert not await reader.has_role(ranked.id, admin_member.id, 999)


async def test_a_new_session_supersedes_an_in_progress_resync(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = populated(chunked=False)
    guild.gate = asyncio.Event()
    client = SyncClient(guild)
    sync = make_sync(client)
    prunes: list[int] = []
    original = gd.prune_guilds

    async def counting(engine: AsyncEngine, *, keep_guild_ids: Any) -> list[int]:
        prunes.append(len(prunes))
        return await original(engine, keep_guild_ids=keep_guild_ids)

    monkeypatch.setattr(gd, "prune_guilds", counting)
    await sync.on_ready()
    await eventually(lambda: guild.chunk_calls == 1)
    first = sync._resync_task
    await sync.on_disconnect()
    newcomer = guild.add_member()
    client.ws.session_id = "session-2"
    await sync.on_ready()
    assert sync._resync_task is not first
    guild.gate.set()
    await sync.wait_resynced()
    await first
    assert len(prunes) == 1
    assert sync.resynced
    assert (await directory(db))[guild.id] == expected(guild)
    assert newcomer.id in (await directory(db))[guild.id][2]
    await eventually(lambda: sync.beats >= 1)


async def test_shutdown_cancels_a_stuck_write_after_the_drain_timeout(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild), drain_timeout=0.1)
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    entered = asyncio.Event()

    async def hanging(engine: AsyncEngine, **kwargs: Any) -> bool:
        entered.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(gd, "upsert_member", hanging)
    await sync.on_member_join(d(guild.add_member()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    with caplog.at_level(logging.WARNING, logger="rapidwire.bot.guild_sync"):
        await asyncio.wait_for(sync.shutdown(), timeout=5)
    (record,) = messages(caplog, "guild directory writes were cancelled at shutdown")
    assert record.__dict__["cancelled"] == 1
    assert sync._workers == {}
    assert await gd.get_heartbeat(db) is None


async def test_shutdown_clears_the_heartbeat_before_draining_writes(db: AsyncEngine, make_sync: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    guild = populated()
    sync = make_sync(SyncClient(guild), drain_timeout=30)
    await sync.on_ready()
    await sync.wait_resynced()
    await eventually(lambda: sync.beats >= 1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = gd.upsert_member

    async def held(engine: AsyncEngine, **kwargs: Any) -> bool:
        entered.set()
        await release.wait()
        return await original(engine, **kwargs)

    monkeypatch.setattr(gd, "upsert_member", held)
    member = guild.add_member()
    await sync.on_member_join(d(member))
    await asyncio.wait_for(entered.wait(), timeout=5)
    shutdown = asyncio.create_task(sync.shutdown())
    await eventually(lambda: heartbeat_cleared(db))
    assert not shutdown.done()
    beats = sync.beats
    await asyncio.sleep(INTERVAL * 3)
    assert sync.beats == beats
    assert await gd.get_heartbeat(db) is None
    release.set()
    await asyncio.wait_for(shutdown, timeout=5)
    assert member.id in (await directory(db))[guild.id][2]
    assert await gd.get_heartbeat(db) is None
