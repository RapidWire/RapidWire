from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.client import RapidWireBot
from rapidwire.core import discord_permissions, notifications
from rapidwire.core.permissions import InMemoryGuildInfo
from tests.integration.bot.conftest import MakeInteraction
from tests.unit.bot.fakes import FakeInteraction, FakeUser, invoke

GUILD = 7_700_001
UNKNOWN_GUILD = 7_700_002
OWNER = 7_800_001
ADMIN = 7_800_002
MEMBER = 7_800_003
DEV = 7_800_004
GHOST = 7_800_005


def _error(interaction: FakeInteraction) -> str:
    embed = interaction.last.embed
    assert embed.title == "エラー", embed.description
    return embed.description or ""


@pytest.fixture
def known_users(bot: RapidWireBot, monkeypatch: pytest.MonkeyPatch) -> dict[int, FakeUser]:
    """Users Discord knows; fetch_user answers NotFound for every other ID, as Discord does for deleted accounts."""
    users = {user_id: FakeUser(user_id) for user_id in (OWNER, ADMIN, MEMBER, DEV)}

    async def fetch_user(user_id: int) -> FakeUser:
        if user_id in users:
            return users[user_id]
        response: Any = SimpleNamespace(status=404, reason="Not Found")
        raise discord.NotFound(response, {"message": "Unknown User", "code": 10013})

    monkeypatch.setattr(bot, "get_user", lambda user_id: None)
    monkeypatch.setattr(bot, "fetch_user", fetch_user)
    return users


async def test_notification_allow_deny_list(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, known_users: dict[int, FakeUser]) -> None:
    empty = (await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=1)).last.embed
    assert (empty.title, empty.description) == ("成功", "現在、請求通知を許可しているユーザーはいません。")

    embed = (await invoke(bot.tree, "notification allow", make_interaction(user_id=MEMBER, guild_id=GUILD), user=FakeUser(DEV))).last.embed
    assert (embed.title, embed.description) == ("成功", f"<@{DEV}> から請求が来た際にDMで通知を送ります。")
    assert await notifications.is_notification_allowed(db, MEMBER, DEV)
    await invoke(bot.tree, "notification allow", make_interaction(user_id=MEMBER), user=FakeUser(GHOST))

    embed = (await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=1)).last.embed
    assert embed.title == "請求通知許可ユーザー一覧 (ページ 1/1)"
    assert embed.colour == discord.Colour.blue()
    assert embed.description == f"<@{DEV}>\n`{GHOST}` (不明なユーザー)"

    embed = (await invoke(bot.tree, "notification deny", make_interaction(user_id=MEMBER), user=FakeUser(DEV))).last.embed
    assert (embed.title, embed.description) == ("成功", f"<@{DEV}> からの請求通知をオフにしました。")
    assert not await notifications.is_notification_allowed(db, MEMBER, DEV)

    assert _error(await invoke(bot.tree, "notification allow", make_interaction(user_id=MEMBER), user=FakeUser(MEMBER))) == "自分自身を対象にすることはできません。"
    assert _error(await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=3)) == "指定されたページ (3) は存在しません。全 1 ページです。"


async def test_notification_list_pages_hold_fifteen(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, known_users: dict[int, FakeUser]) -> None:
    allowed = [7_900_000 + i for i in range(16)]
    for user_id in allowed:
        await notifications.allow_notifications(db, user_id=MEMBER, allowed_user_id=user_id)
    first = (await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=1)).last.embed
    second = (await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=2)).last.embed
    assert first.title == "請求通知許可ユーザー一覧 (ページ 1/2)"
    assert second.title == "請求通知許可ユーザー一覧 (ページ 2/2)"
    lines = (first.description or "").split("\n") + (second.description or "").split("\n")
    assert len((first.description or "").split("\n")) == 15
    assert lines == [f"`{user_id}` (不明なユーザー)" for user_id in allowed]
    assert _error(await invoke(bot.tree, "notification list", make_interaction(user_id=MEMBER), page=3)) == "指定されたページ (3) は存在しません。全 2 ページです。"


@pytest.fixture
def guild(guild_info: InMemoryGuildInfo) -> InMemoryGuildInfo:
    guild_info.add_guild(GUILD, OWNER)
    guild_info.add_admin(GUILD, ADMIN)
    guild_info.add_member(GUILD, MEMBER)
    return guild_info


@pytest.mark.parametrize("actor", [OWNER, ADMIN])
async def test_discord_permission_by_owner_and_administrator(
    db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, guild: InMemoryGuildInfo, known_users: dict[int, FakeUser], actor: int
) -> None:
    interaction = await invoke(bot.tree, "discord-permission list", make_interaction(user_id=actor, guild_id=GUILD), page=1)
    assert (interaction.last.embed.title, interaction.last.embed.description) == ("権限一覧", "現在、Discord操作を許可されているユーザーはいません。")

    embed = (await invoke(bot.tree, "discord-permission allow", make_interaction(user_id=actor, guild_id=GUILD), user=FakeUser(DEV))).last.embed
    assert (embed.title, embed.description) == ("権限付与成功", f"<@{DEV}> のコントラクトによるDiscord操作を許可しました。")
    assert await discord_permissions.is_discord_allowed(db, GUILD, DEV)
    await invoke(bot.tree, "discord-permission allow", make_interaction(user_id=actor, guild_id=GUILD), user=FakeUser(GHOST))

    embed = (await invoke(bot.tree, "discord-permission list", make_interaction(user_id=actor, guild_id=GUILD), page=1)).last.embed
    assert embed.title == "Discord操作許可ユーザー一覧 (ページ 1/1)"
    assert embed.description == f"<@{DEV}>\n`{GHOST}` (不明なユーザー)"

    embed = (await invoke(bot.tree, "discord-permission deny", make_interaction(user_id=actor, guild_id=GUILD), user=FakeUser(DEV))).last.embed
    assert (embed.title, embed.description) == ("権限剥奪成功", f"<@{DEV}> のコントラクトによるDiscord操作を禁止しました。")
    assert not await discord_permissions.is_discord_allowed(db, GUILD, DEV)


async def test_discord_permission_refused(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, guild: InMemoryGuildInfo, known_users: dict[int, FakeUser]) -> None:
    denied = "この操作を行う権限がありません。"
    for name, arguments in (("discord-permission allow", {"user": FakeUser(DEV)}), ("discord-permission deny", {"user": FakeUser(DEV)}), ("discord-permission list", {"page": 1})):
        assert _error(await invoke(bot.tree, name, make_interaction(user_id=MEMBER, guild_id=GUILD), **arguments)) == denied
        assert _error(await invoke(bot.tree, name, make_interaction(user_id=DEV, guild_id=GUILD), **arguments)) == denied
        unavailable = await invoke(bot.tree, name, make_interaction(user_id=OWNER, guild_id=UNKNOWN_GUILD), **arguments)
        assert _error(unavailable) == "Botがこのサーバーに参加していないため、サーバーの所有者と管理者を確認できません。"
        direct = await invoke(bot.tree, name, make_interaction(user_id=OWNER), **arguments)
        assert _error(direct) == "このコマンドはサーバー内でのみ使用できます。"
    assert not await discord_permissions.is_discord_allowed(db, GUILD, DEV)

    guild.remove_admin(GUILD, ADMIN)
    assert _error(await invoke(bot.tree, "discord-permission allow", make_interaction(user_id=ADMIN, guild_id=GUILD), user=FakeUser(DEV))) == denied
