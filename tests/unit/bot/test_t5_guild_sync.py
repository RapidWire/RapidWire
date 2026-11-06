"""Unit tests of the guild directory writer's pure mapping and of its registration, with fakes shaped like discord.py's guild cache (roles and members as lists), which the integration tests reuse."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from rapidwire.bot import guild_sync
from rapidwire.bot.app import build_bot
from rapidwire.bot.client import bot_intents
from rapidwire.bot.guild_sync import GuildDirectorySync, administrator_role_ids, chunk_timeout, guild_snapshot, member_role_ids, retry_delay
from rapidwire.config import Settings
from rapidwire.core.guild_directory import MemberSnapshot
from tests.unit.bot.fakes import FakeUser, next_snowflake


class SyncRole:
    """A discord.Role: id, guild and permissions."""

    def __init__(self, role_id: int, guild: SyncGuild, *, administrator: bool = False) -> None:
        self.id = role_id
        self.guild = guild
        self.permissions = discord.Permissions(administrator=administrator)


class SyncMember(FakeUser):
    """A discord.Member: roles lists @everyone and then the member's roles that exist in the guild."""

    def __init__(self, guild: SyncGuild, user_id: int | None = None, *, role_ids: Iterable[int] = ()) -> None:
        super().__init__(user_id)
        self.guild = guild
        self.role_ids = set(role_ids)

    @property
    def roles(self) -> list[SyncRole]:
        return [self.guild.default_role, *(self.guild.role_map[role_id] for role_id in sorted(self.role_ids) if role_id in self.guild.role_map)]

    def copy(self, *, role_ids: Iterable[int] | None = None) -> SyncMember:
        """A detached copy, like the before and after members of a member_update event."""
        return SyncMember(self.guild, self.id, role_ids=self.role_ids if role_ids is None else role_ids)


class SyncGuild:
    """A discord.Guild in the cache: owner_id, unavailable, chunked, roles and members as lists; chunk() can be held back by gate or made to fail."""

    def __init__(self, guild_id: int | None = None, *, owner_id: int | None = None, chunked: bool = True) -> None:
        self.id = next_snowflake() if guild_id is None else guild_id
        self.owner_id = owner_id
        self.unavailable = False
        self.chunked = chunked
        self.default_role = SyncRole(self.id, self)
        self.role_map: dict[int, SyncRole] = {self.id: self.default_role}
        self.member_map: dict[int, SyncMember] = {}
        self.gate: asyncio.Event | None = None
        self.chunk_calls = 0

    @property
    def roles(self) -> list[SyncRole]:
        return list(self.role_map.values())

    @property
    def members(self) -> list[SyncMember]:
        return list(self.member_map.values())

    @property
    def member_count(self) -> int:
        return len(self.member_map)

    def get_member(self, user_id: int) -> SyncMember | None:
        return self.member_map.get(user_id)

    def add_role(self, role_id: int | None = None, *, administrator: bool = False) -> SyncRole:
        role = SyncRole(next_snowflake() if role_id is None else role_id, self, administrator=administrator)
        self.role_map[role.id] = role
        return role

    def add_member(self, user_id: int | None = None, *, role_ids: Iterable[int] = ()) -> SyncMember:
        member = SyncMember(self, user_id, role_ids=role_ids)
        self.member_map[member.id] = member
        return member

    def copy(self) -> SyncGuild:
        """A detached copy with the same owner, like the before guild of a guild_update event."""
        other = SyncGuild(self.id, owner_id=self.owner_id)
        other.role_map = dict(self.role_map)
        return other

    async def chunk(self) -> None:
        self.chunk_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        self.chunked = True


class SyncClient:
    """A discord.Client: guilds, get_guild, user and ws.session_id."""

    def __init__(self, *guilds: SyncGuild, session_id: str = "session-1") -> None:
        self.guild_map = {guild.id: guild for guild in guilds}
        self.user = FakeUser(name="RapidWire", bot=True)
        self.ws = SimpleNamespace(session_id=session_id)

    @property
    def guilds(self) -> list[SyncGuild]:
        return list(self.guild_map.values())

    def add_guild(self, guild: SyncGuild) -> SyncGuild:
        self.guild_map[guild.id] = guild
        return guild

    def get_guild(self, guild_id: int) -> SyncGuild | None:
        return self.guild_map.get(guild_id)


def d(value: Any) -> Any:
    """A fake typed as the discord.py object it stands in for."""
    return cast("Any", value)


def raw_remove(guild_id: int, user_id: int) -> discord.RawMemberRemoveEvent:
    return cast("discord.RawMemberRemoveEvent", SimpleNamespace(guild_id=guild_id, user=FakeUser(user_id)))


def test_guild_snapshot_maps_owner_admin_roles_and_members() -> None:
    guild = SyncGuild(owner_id=11)
    admin = guild.add_role(administrator=True)
    plain = guild.add_role()
    guild.add_role(administrator=True)
    first = guild.add_member(11, role_ids=[plain.id])
    second = guild.add_member(12, role_ids=[admin.id, plain.id, 999])
    state = guild_snapshot(d(guild))
    assert state.owner_id == 11
    assert state.administrator_role_ids == administrator_role_ids(d(guild))
    assert admin.id in state.administrator_role_ids
    assert plain.id not in state.administrator_role_ids
    assert set(state.members) == {
        MemberSnapshot(first.id, frozenset({guild.id, plain.id})),
        MemberSnapshot(second.id, frozenset({guild.id, admin.id, plain.id})),
    }
    assert member_role_ids(d(second)) == frozenset({guild.id, admin.id, plain.id})


def test_everyone_with_administrator_is_an_administrator_role() -> None:
    guild = SyncGuild(owner_id=11)
    guild.default_role.permissions = discord.Permissions(administrator=True)
    assert administrator_role_ids(d(guild)) == {guild.id}


def test_guild_without_owner_cannot_be_snapshotted() -> None:
    with pytest.raises(ValueError, match="no known owner"):
        guild_snapshot(d(SyncGuild()))


def test_retry_delay_grows_and_is_capped() -> None:
    assert [retry_delay(attempt) for attempt in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert retry_delay(50) == guild_sync.RETRY_DELAY_MAX_SECONDS


def test_chunk_timeout_scales_with_the_member_count() -> None:
    small = SimpleNamespace(member_count=None)
    large = SimpleNamespace(member_count=250_000)
    assert chunk_timeout(d(small)) == guild_sync.CHUNK_TIMEOUT_MIN_SECONDS * guild_sync.CHUNK_TIMEOUT_FACTOR
    assert chunk_timeout(d(large)) == 250_000 / guild_sync.CHUNK_MEMBERS_PER_SECOND * guild_sync.CHUNK_TIMEOUT_FACTOR


async def test_a_timed_out_chunk_asks_discord_again_on_the_next_try() -> None:
    client = discord.Client(intents=bot_intents())
    state: Any = client._connection
    state.loop = asyncio.get_running_loop()
    nonces: list[str | None] = []

    async def request_chunks(guild_id: int, **kwargs: Any) -> None:
        nonces.append(kwargs["nonce"])

    state._get_websocket = lambda *args, **kwargs: SimpleNamespace(request_chunks=request_chunks)
    guild = discord.Guild(data=cast(Any, {"id": "123", "name": "g", "member_count": 50, "owner_id": "11"}), state=state)
    state._add_guild(guild)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(guild.chunk(), timeout=0.01)
    assert guild.id in state._chunk_requests
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(guild.chunk(), timeout=0.01)
    assert len(nonces) == 1
    with pytest.raises(TimeoutError):
        await guild_sync._chunk(guild, 0.01)
    assert guild.id not in state._chunk_requests
    with pytest.raises(TimeoutError):
        await guild_sync._chunk(guild, 0.01)
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]
    guild_sync.drop_chunk_request(d(SimpleNamespace(id=1)))


def test_setup_registers_every_listener_and_the_hooks() -> None:
    bot = build_bot(Settings(_env_file=None, db_password="unit-test-password"))
    events = (
        "ready",
        "resumed",
        "disconnect",
        "guild_join",
        "guild_available",
        "guild_remove",
        "guild_unavailable",
        "guild_update",
        "guild_role_create",
        "guild_role_update",
        "guild_role_delete",
        "member_join",
        "member_update",
        "raw_member_remove",
    )
    for event in events:
        owners = [getattr(handler, "__self__", None) for handler in bot.listeners(event)]
        assert any(isinstance(owner, GuildDirectorySync) for owner in owners), event
    assert not any(isinstance(getattr(handler, "__self__", None), GuildDirectorySync) for handler in bot.listeners("member_remove"))
    assert any(isinstance(getattr(hook, "__self__", None), GuildDirectorySync) for hook in bot._shutdown_hooks)
    assert any(isinstance(getattr(hook, "__self__", None), GuildDirectorySync) for hook in bot._startup_hooks)
