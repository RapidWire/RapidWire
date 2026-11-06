"""Fixtures for bot tests: test Settings, the bot built by build_bot() on the test database with an in-memory GuildInfo (never connected to Discord), its BotContext, and a factory of FakeInteractions addressed to it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.context import BotContext
from rapidwire.config import Settings
from rapidwire.core.permissions import InMemoryGuildInfo
from tests.unit.bot.fakes import FakeChannel, FakeGuild, FakeInteraction, FakeMessage, FakeUser

BOT_TEST_PASSWORD = "bot-test-db-password"
BOT_TEST_DISCORD_TOKEN = "bot-test-discord-token"

type MakeInteraction = Callable[..., FakeInteraction]


@pytest.fixture
def guild_info() -> InMemoryGuildInfo:
    """The GuildInfo the bot's services consult; tests add guilds, owners, members and administrators to it."""
    return InMemoryGuildInfo()


@pytest.fixture
def bot_settings() -> Settings:
    """Settings for the test bot; the database fields are placeholders because the bot uses the db fixture's engine."""
    return Settings(_env_file=None, db_password=BOT_TEST_PASSWORD, discord_token=BOT_TEST_DISCORD_TOKEN)


@pytest.fixture
def bot(db: AsyncEngine, bot_settings: Settings, guild_info: InMemoryGuildInfo) -> RapidWireBot:
    """The bot with every command module set up, on the emptied test database."""
    return build_bot(bot_settings, engine=db, guild_info=guild_info)


@pytest.fixture
def bot_ctx(bot: RapidWireBot) -> BotContext:
    return bot.context


@pytest.fixture
def make_interaction(bot: RapidWireBot) -> MakeInteraction:
    """Build a FakeInteraction for the bot: user_id (or a fresh user) in guild_id (a FakeGuild with that ID, or a direct message when None), with optional namespace, message and data."""

    def make(
        *,
        user_id: int | None = None,
        guild_id: int | None = None,
        guild: FakeGuild | None = None,
        namespace: dict[str, Any] | None = None,
        message: FakeMessage | None = None,
        data: dict[str, Any] | None = None,
    ) -> FakeInteraction:
        if guild is None and guild_id is not None:
            guild = FakeGuild(guild_id)
        user: FakeUser
        if guild is not None:
            member = guild.get_member(user_id) if user_id is not None else None
            user = member if member is not None else guild.add_member(user_id)
            channel = guild.add_channel()
        else:
            user = FakeUser(user_id)
            channel = FakeChannel()
        return FakeInteraction(bot, user=user, guild=guild, channel=channel, namespace=namespace, message=message, data=data)

    return make
