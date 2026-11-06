from __future__ import annotations

import inspect

import pytest

from rapidwire.core.errors import GuildUnavailable, PermissionDenied
from rapidwire.core.ledger import LedgerUsageError
from rapidwire.core.permissions import (
    GuildInfo,
    GuildSnapshot,
    InMemoryGuildInfo,
    require_currency_manager,
    require_guild_administrator,
    require_guild_owner,
    snapshot_guild,
)

GUILD = 10
OWNER = 1
ADMIN = 2
MEMBER = 3


@pytest.fixture
def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    info.add_guild(GUILD, OWNER)
    info.add_admin(GUILD, ADMIN)
    return info


async def _owner_check(guilds: InMemoryGuildInfo, guild_id: int, user_id: int) -> None:
    require_guild_owner(await snapshot_guild(guilds, guild_id, user_id), guild_id, user_id)


async def _manager_check(guilds: InMemoryGuildInfo, guild_id: int, user_id: int, *, delegation_enabled: bool) -> None:
    require_currency_manager(await snapshot_guild(guilds, guild_id, user_id), guild_id, user_id, delegation_enabled=delegation_enabled)


def test_fake_satisfies_protocol(guilds: InMemoryGuildInfo) -> None:
    protocol: GuildInfo = guilds
    assert protocol is guilds


def test_delegation_flag_is_keyword_only() -> None:
    parameter = inspect.signature(require_currency_manager).parameters["delegation_enabled"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


async def test_snapshot_captures_owner_and_administrator(guilds: InMemoryGuildInfo) -> None:
    assert await snapshot_guild(guilds, GUILD, ADMIN) == GuildSnapshot(guild_id=GUILD, user_id=ADMIN, owner_id=OWNER, administrator=True)
    owner = await snapshot_guild(guilds, GUILD, OWNER)
    assert (owner.bot_in_guild, owner.is_owner, owner.administrator) == (True, True, False)
    absent = await snapshot_guild(guilds, GUILD + 1, OWNER)
    assert (absent.bot_in_guild, absent.is_owner, absent.administrator) == (False, False, False)


async def test_snapshot_is_frozen_against_later_changes(guilds: InMemoryGuildInfo) -> None:
    snapshot = await snapshot_guild(guilds, GUILD, ADMIN)
    guilds.remove_admin(GUILD, ADMIN)
    require_currency_manager(snapshot, GUILD, ADMIN, delegation_enabled=True)


async def test_snapshot_for_another_subject_is_a_usage_error(guilds: InMemoryGuildInfo) -> None:
    snapshot = await snapshot_guild(guilds, GUILD, OWNER)
    with pytest.raises(LedgerUsageError):
        require_guild_owner(snapshot, GUILD + 1, OWNER)
    with pytest.raises(LedgerUsageError):
        require_currency_manager(snapshot, GUILD, ADMIN, delegation_enabled=True)


async def test_owner_passes_owner_check(guilds: InMemoryGuildInfo) -> None:
    await _owner_check(guilds, GUILD, OWNER)


async def test_non_owner_fails_owner_check(guilds: InMemoryGuildInfo) -> None:
    for user in (ADMIN, MEMBER):
        with pytest.raises(PermissionDenied):
            await _owner_check(guilds, GUILD, user)


async def test_owner_check_without_bot_in_guild(guilds: InMemoryGuildInfo) -> None:
    with pytest.raises(GuildUnavailable):
        await _owner_check(guilds, GUILD + 1, OWNER)


@pytest.mark.parametrize("delegation", [False, True])
async def test_owner_is_manager(guilds: InMemoryGuildInfo, delegation: bool) -> None:
    await _manager_check(guilds, GUILD, OWNER, delegation_enabled=delegation)


async def test_admin_is_manager_only_with_delegation(guilds: InMemoryGuildInfo) -> None:
    await _manager_check(guilds, GUILD, ADMIN, delegation_enabled=True)
    with pytest.raises(PermissionDenied):
        await _manager_check(guilds, GUILD, ADMIN, delegation_enabled=False)


@pytest.mark.parametrize("delegation", [False, True])
async def test_member_is_never_manager(guilds: InMemoryGuildInfo, delegation: bool) -> None:
    with pytest.raises(PermissionDenied):
        await _manager_check(guilds, GUILD, MEMBER, delegation_enabled=delegation)


@pytest.mark.parametrize("user", [OWNER, ADMIN])
async def test_manager_check_without_bot_in_guild(guilds: InMemoryGuildInfo, user: int) -> None:
    guilds.remove_guild(GUILD)
    with pytest.raises(GuildUnavailable):
        await _manager_check(guilds, GUILD, user, delegation_enabled=True)
    assert not await guilds.is_administrator(GUILD, ADMIN)


@pytest.mark.parametrize(("user", "allowed"), [(OWNER, True), (ADMIN, True), (MEMBER, False)])
async def test_administrator_check(guilds: InMemoryGuildInfo, user: int, allowed: bool) -> None:
    snapshot = await snapshot_guild(guilds, GUILD, user)
    if allowed:
        require_guild_administrator(snapshot, GUILD, user, message="no")
        return
    with pytest.raises(PermissionDenied, match=r"^no$"):
        require_guild_administrator(snapshot, GUILD, user, message="no")


async def test_administrator_check_without_bot_in_guild(guilds: InMemoryGuildInfo) -> None:
    with pytest.raises(GuildUnavailable):
        require_guild_administrator(await snapshot_guild(guilds, GUILD + 1, OWNER), GUILD + 1, OWNER, message="no")


async def test_ownership_change_moves_rights(guilds: InMemoryGuildInfo) -> None:
    guilds.set_owner(GUILD, MEMBER)
    await _owner_check(guilds, GUILD, MEMBER)
    await _manager_check(guilds, GUILD, MEMBER, delegation_enabled=False)
    with pytest.raises(PermissionDenied):
        await _owner_check(guilds, GUILD, OWNER)
    with pytest.raises(PermissionDenied):
        await _manager_check(guilds, GUILD, OWNER, delegation_enabled=False)


async def test_admin_removal_revokes_delegated_rights(guilds: InMemoryGuildInfo) -> None:
    guilds.remove_admin(GUILD, ADMIN)
    with pytest.raises(PermissionDenied):
        await _manager_check(guilds, GUILD, ADMIN, delegation_enabled=True)


async def test_roles(guilds: InMemoryGuildInfo) -> None:
    assert not await guilds.has_role(GUILD, MEMBER, 77)
    guilds.add_role(GUILD, MEMBER, 77)
    assert await guilds.has_role(GUILD, MEMBER, 77)
    assert not await guilds.has_role(GUILD, ADMIN, 77)
    assert not await guilds.has_role(GUILD + 1, MEMBER, 77)
    guilds.remove_role(GUILD, MEMBER, 77)
    assert not await guilds.has_role(GUILD, MEMBER, 77)


async def test_guild_owner_lookup(guilds: InMemoryGuildInfo) -> None:
    assert await guilds.guild_owner(GUILD) == OWNER
    assert await guilds.guild_owner(GUILD + 1) is None
