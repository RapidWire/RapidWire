from __future__ import annotations

from typing import Any, cast

import pytest

from rapidwire.bot.guild_info import CacheGuildInfo
from rapidwire.core.permissions import GuildInfo, InMemoryGuildInfo
from tests.unit.bot.fakes import FakeClient, FakeGuild

OWNER = 501
ADMIN = 502
MEMBER = 503
OUTSIDER = 504


def _setup() -> tuple[FakeGuild, CacheGuildInfo]:
    guild = FakeGuild(owner_id=OWNER)
    admin_role = guild.add_role(administrator=True)
    plain_role = guild.add_role()
    guild.add_member(OWNER)
    guild.add_member(ADMIN, role_ids=[admin_role.id, plain_role.id])
    guild.add_member(MEMBER, role_ids=[plain_role.id])
    return guild, CacheGuildInfo(cast(Any, FakeClient(guild)))


async def test_owner_comes_from_the_cached_guild() -> None:
    guild, info = _setup()
    assert await info.guild_owner(guild.id) == OWNER
    assert await info.guild_owner(guild.id + 1) is None


async def test_unavailable_guild_is_unknown() -> None:
    guild, info = _setup()
    guild.unavailable = True
    assert await info.guild_owner(guild.id) is None
    assert not await info.is_administrator(guild.id, ADMIN)
    assert not await info.has_role(guild.id, ADMIN, guild.id)


async def test_administrator_comes_from_roles_and_the_owner_is_not_implicitly_one() -> None:
    guild, info = _setup()
    assert await info.is_administrator(guild.id, ADMIN)
    assert not await info.is_administrator(guild.id, MEMBER)
    assert not await info.is_administrator(guild.id, OWNER)
    assert not await info.is_administrator(guild.id, OUTSIDER)


async def test_everyone_role_with_administrator_makes_every_member_one() -> None:
    guild, info = _setup()
    guild.default_role.permissions.administrator = True
    assert await info.is_administrator(guild.id, MEMBER)
    assert await info.is_administrator(guild.id, OWNER)
    assert not await info.is_administrator(guild.id, OUTSIDER)


async def test_roles_are_held_by_cached_members_only() -> None:
    guild, info = _setup()
    plain_role = next(role for role in guild.roles.values() if not role.is_default() and not role.permissions.administrator)
    assert await info.has_role(guild.id, MEMBER, plain_role.id)
    assert await info.has_role(guild.id, MEMBER, guild.id)
    assert not await info.has_role(guild.id, OWNER, plain_role.id)
    assert not await info.has_role(guild.id, OUTSIDER, guild.id)
    assert not await info.has_role(guild.id + 1, MEMBER, plain_role.id)
    guild.remove_role(plain_role.id)
    assert not await info.has_role(guild.id, MEMBER, plain_role.id)
    guild.remove_member(MEMBER)
    assert not await info.has_role(guild.id, MEMBER, guild.id)


@pytest.mark.parametrize("user_id", [OWNER, ADMIN, MEMBER, OUTSIDER])
async def test_answers_match_the_in_memory_model(user_id: int) -> None:
    guild, cache = _setup()
    model = InMemoryGuildInfo()
    model.add_guild(guild.id, OWNER)
    for member in guild.members.values():
        model.add_member(guild.id, member.id, member.role_ids)
    for role in guild.roles.values():
        model.set_role_administrator(guild.id, role.id, role.permissions.administrator)
    infos: list[GuildInfo] = [cache, model]
    answers = []
    for info in infos:
        answers.append(
            (
                await info.guild_owner(guild.id),
                await info.is_administrator(guild.id, user_id),
                [await info.has_role(guild.id, user_id, role_id) for role_id in sorted(guild.roles)],
            )
        )
    assert answers[0] == answers[1]
