from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.discord_permissions import allow_discord, deny_discord, is_discord_allowed, list_discord_permissions, list_discord_permissions_as
from rapidwire.core.errors import GuildUnavailable, InvalidArgument, PermissionDenied
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import execution

NOW = datetime(2026, 5, 3, tzinfo=UTC)
GUILD = 900
OTHER_GUILD = 901
OWNER = 111
ADMIN = 222
MEMBER = 333
DEVELOPER = 444


@pytest.fixture
def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    info.add_guild(GUILD, OWNER)
    info.add_admin(GUILD, ADMIN)
    info.add_guild(OTHER_GUILD, MEMBER)
    return info


async def _records(engine: AsyncEngine) -> list[tuple[str, str, int]]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(execution.c.type, execution.c.status, execution.c.initiator_id).order_by(execution.c.execution_id))
        return [(row.type, row.status, row.initiator_id) for row in rows]


async def test_admin_allows_and_denies(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    result = await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    assert result.changed
    assert await is_discord_allowed(db, GUILD, DEVELOPER)
    assert not await is_discord_allowed(db, OTHER_GUILD, DEVELOPER)
    permissions = (await list_discord_permissions(db, GUILD)).items
    assert [(p.guild_id, p.account_id, p.granted_by, p.created_at) for p in permissions] == [(GUILD, DEVELOPER, ADMIN, NOW)]
    denied = await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    assert denied.changed
    async with db.connect() as conn:
        assert not await is_discord_allowed(conn, GUILD, DEVELOPER)
    assert (await list_discord_permissions(db, GUILD)).items == ()
    assert await _records(db) == [
        (ExecutionType.DISCORD_PERMISSION_ALLOW, ExecutionStatus.SUCCESS, ADMIN),
        (ExecutionType.DISCORD_PERMISSION_DENY, ExecutionStatus.SUCCESS, ADMIN),
    ]


async def test_owner_without_admin_flag_may_change(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    assert (await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW)).changed
    assert (await list_discord_permissions(db, GUILD)).items[0].granted_by == OWNER
    assert (await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW)).changed


async def test_non_admin_is_denied(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    with pytest.raises(PermissionDenied) as error:
        await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=MEMBER, now=NOW)
    assert error.value.execution_id is not None
    await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    with pytest.raises(PermissionDenied):
        await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=MEMBER, now=NOW)
    with pytest.raises(PermissionDenied):
        await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=DEVELOPER, now=NOW)
    assert await is_discord_allowed(db, GUILD, DEVELOPER)
    assert [status for _, status, _ in await _records(db)] == [ExecutionStatus.FAILED, ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.FAILED]


async def test_admin_rights_are_read_at_call_time(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    guilds.remove_admin(GUILD, ADMIN)
    with pytest.raises(PermissionDenied):
        await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    guilds.set_owner(GUILD, ADMIN)
    assert (await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)).changed


async def test_bot_not_in_guild(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    guilds.remove_guild(GUILD)
    with pytest.raises(GuildUnavailable):
        await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW)
    with pytest.raises(GuildUnavailable):
        await allow_discord(db, guilds, guild_id=GUILD, account_id=MEMBER, acting_user_id=ADMIN, now=NOW)
    assert await is_discord_allowed(db, GUILD, DEVELOPER)
    assert not await is_discord_allowed(db, GUILD, MEMBER)


async def test_repeated_allow_keeps_the_first_grant(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    again = await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW + timedelta(hours=1))
    assert not again.changed
    assert [(p.granted_by, p.created_at) for p in (await list_discord_permissions(db, GUILD)).items] == [(ADMIN, NOW)]
    missing = await deny_discord(db, guilds, guild_id=GUILD, account_id=MEMBER, acting_user_id=OWNER, now=NOW)
    assert not missing.changed


class CountingGuildInfo(InMemoryGuildInfo):
    def __init__(self) -> None:
        super().__init__()
        self.owner_calls = 0

    async def guild_owner(self, guild_id: int) -> int | None:
        self.owner_calls += 1
        return await super().guild_owner(guild_id)


async def test_guild_info_is_asked_once_per_change(db: AsyncEngine) -> None:
    guilds = CountingGuildInfo()
    guilds.add_guild(GUILD, OWNER)
    await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW)
    with pytest.raises(PermissionDenied):
        await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=MEMBER, now=NOW)
    assert guilds.owner_calls == 2


async def test_concurrent_allows_insert_one_row(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    results = await asyncio.gather(*(allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=acting, now=NOW) for acting in (OWNER, ADMIN, OWNER, ADMIN)))
    assert sum(result.changed for result in results) == 1
    permissions = (await list_discord_permissions(db, GUILD)).items
    assert len(permissions) == 1
    assert permissions[0].granted_by == [OWNER, ADMIN, OWNER, ADMIN][[result.changed for result in results].index(True)]


async def test_idempotency_key_replays(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    first = await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW, idempotency_key="d-1")
    again = await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW, idempotency_key="d-1")
    assert again.replayed
    assert again.changed
    assert again.execution_id == first.execution_id
    with pytest.raises(PermissionDenied) as denied:
        await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=MEMBER, now=NOW, idempotency_key="d-2")
    with pytest.raises(PermissionDenied) as replayed:
        await deny_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=MEMBER, now=NOW, idempotency_key="d-2")
    assert replayed.value.execution_id == denied.value.execution_id
    assert len(await _records(db)) == 2


async def test_list_as_requires_owner_or_admin(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await allow_discord(db, guilds, guild_id=GUILD, account_id=DEVELOPER, acting_user_id=ADMIN, now=NOW)
    assert [p.account_id for p in (await list_discord_permissions_as(db, guilds, guild_id=GUILD, user_id=OWNER)).items] == [DEVELOPER]
    assert [p.account_id for p in (await list_discord_permissions_as(db, guilds, guild_id=GUILD, user_id=ADMIN)).items] == [DEVELOPER]
    with pytest.raises(PermissionDenied):
        await list_discord_permissions_as(db, guilds, guild_id=GUILD, user_id=MEMBER)
    guilds.remove_guild(GUILD)
    with pytest.raises(GuildUnavailable):
        await list_discord_permissions_as(db, guilds, guild_id=GUILD, user_id=OWNER)
    assert len(await _records(db)) == 1


async def test_invalid_ids_are_rejected(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    with pytest.raises(InvalidArgument):
        await allow_discord(db, guilds, guild_id=0, account_id=DEVELOPER, acting_user_id=OWNER, now=NOW)
    with pytest.raises(InvalidArgument):
        await allow_discord(db, guilds, guild_id=GUILD, account_id=-5, acting_user_id=OWNER, now=NOW)
