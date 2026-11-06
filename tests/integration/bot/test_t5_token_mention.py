"""The API token mention against the real database: parsing, channel checks, delivery by DM or channel, revocation of undelivered and cancelled tokens, and that the plaintext is stored only as a hash and never logged."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot import token_mention as tm
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.context import BotContext
from rapidwire.core.tokens import authenticate_token, revoke_tokens
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import api_token, execution
from tests.unit.bot.fakes import FakeChannel, FakeClient, FakeGuild, FakeMessage, FakeUser

TOKEN_PATTERN = re.compile(r"^<@(\d+)> のAPIトークンです。大切に保管してください。\n```(rw_[A-Za-z0-9_-]{43})```$")


def _response(status: int, reason: str) -> Any:
    return SimpleNamespace(status=status, reason=reason)


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(_response(403, "Forbidden"), {"message": "Missing Access", "code": 50001})


def not_found() -> discord.NotFound:
    return discord.NotFound(_response(404, "Not Found"), {"message": "Unknown Channel", "code": 10003})


def server_error() -> discord.HTTPException:
    return discord.HTTPException(_response(500, "Internal Server Error"), {"message": "boom", "code": 0})


class TokenClient(FakeClient):
    """A FakeClient whose fetch_channel answers from fetchable, raising the exception stored there or NotFound."""

    def __init__(self, *guilds: FakeGuild) -> None:
        super().__init__(*guilds)
        self.fetchable: dict[int, Any] = {}
        self.fetched: list[int] = []

    async def fetch_channel(self, channel_id: int) -> Any:
        self.fetched.append(channel_id)
        found = self.fetchable.get(channel_id)
        if found is None:
            raise not_found()
        if isinstance(found, Exception):
            raise found
        return found


class FailingChannel(FakeChannel):
    """A channel whose send records the attempted content and then raises error."""

    def __init__(self, error: Exception, *, guild: FakeGuild | None = None) -> None:
        super().__init__(guild=guild)
        self.error = error
        self.attempted: list[str] = []

    async def send(self, content: str | None = None, *args: Any, **kwargs: Any) -> FakeMessage:
        self.attempted.append(content or "")
        raise self.error


class HangingChannel(FakeChannel):
    """A channel whose send records the attempted content and never returns."""

    def __init__(self, *, guild: FakeGuild | None = None) -> None:
        super().__init__(guild=guild)
        self.attempted: list[str] = []
        self.entered = asyncio.Event()

    async def send(self, content: str | None = None, *args: Any, **kwargs: Any) -> FakeMessage:
        self.attempted.append(content or "")
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ClosedDmUser(FakeUser):
    """A user whose direct messages record the attempted content and then raise error."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.attempted: list[str] = []

    async def send(self, content: str | None = None, *args: Any, **kwargs: Any) -> FakeMessage:
        self.attempted.append(content or "")
        raise self.error


def attempted_token(attempted: list[str], user_id: int) -> str:
    """The token in the single delivery attempt recorded by a failing fake."""
    assert len(attempted) == 1
    match = TOKEN_PATTERN.fullmatch(attempted[0])
    assert match is not None, attempted[0]
    assert int(match.group(1)) == user_id
    return match.group(2)


class World:
    """A client with two guilds, a channel in each, and helpers that build messages to the bot."""

    def __init__(self) -> None:
        self.guild = FakeGuild(owner_id=1)
        self.other = FakeGuild(owner_id=2)
        self.client = TokenClient(self.guild, self.other)
        self.here = self.guild.add_channel(name="here")
        self.there = self.other.add_channel(name="there")
        self.author = self.guild.add_member()

    def message(self, text: str, *, author: Any = None, channel: FakeChannel | None = None, mention: bool = True) -> FakeMessage:
        content = f"<@{self.client.user.id}>{text}" if mention else text
        return FakeMessage(content=content, author=author if author is not None else self.author, channel=channel if channel is not None else self.here)


@pytest.fixture
def world() -> World:
    return World()


async def handle(world: World, ctx: BotContext, message: FakeMessage) -> list[str | None]:
    await tm.handle_message(cast("discord.Client", world.client), ctx, cast("discord.Message", message))
    return [reply.content for reply in message.replies]


async def token_rows(db: AsyncEngine) -> list[Any]:
    async with db.connect() as conn:
        return list((await conn.execute(select(api_token).order_by(api_token.c.api_token_id))).all())


def delivered_token(sent: list[Any], user_id: int) -> str:
    assert len(sent) == 1
    match = TOKEN_PATTERN.fullmatch(sent[0].content)
    assert match is not None, sent[0].content
    assert int(match.group(1)) == user_id
    return match.group(2)


def assert_not_logged(caplog: pytest.LogCaptureFixture, token: str) -> None:
    assert caplog.records
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in repr(record.__dict__)


async def test_dm_delivery_issues_a_hashed_token_that_authenticates(db: AsyncEngine, bot_ctx: BotContext, world: World, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        replies = await handle(world, bot_ctx, world.message(" API"))
    assert replies == [tm.SENT_TO_DM]
    token = delivered_token(world.author.sent, world.author.id)
    assert await authenticate_token(db, token) == world.author.id
    rows = await token_rows(db)
    assert len(rows) == 1
    assert rows[0].token_hash == hashlib.sha256(token.encode()).digest()
    assert token.encode() not in bytes(rows[0].token_hash)
    assert_not_logged(caplog, token)


async def test_a_new_token_revokes_the_previous_one(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    await handle(world, bot_ctx, world.message(" api"))
    first = delivered_token(world.author.sent, world.author.id)
    world.author.sent.clear()
    await handle(world, bot_ctx, world.message("   Api   "))
    second = delivered_token(world.author.sent, world.author.id)
    assert first != second
    assert await authenticate_token(db, first) is None
    assert await authenticate_token(db, second) == world.author.id


async def test_dm_message_without_channel_uses_dm(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    user = FakeUser()
    replies = await handle(world, bot_ctx, world.message(" API", author=user, channel=FakeChannel()))
    assert replies == [tm.SENT_TO_DM]
    assert await authenticate_token(db, delivered_token(user.sent, user.id)) == user.id


async def test_cached_channel_in_the_same_guild(db: AsyncEngine, bot_ctx: BotContext, world: World, caplog: pytest.LogCaptureFixture) -> None:
    target = world.guild.add_channel(name="tokens")
    with caplog.at_level(logging.DEBUG):
        replies = await handle(world, bot_ctx, world.message(f" API {target.id} extra words"))
    assert replies == [tm.sent_to_channel(target.id)]
    assert world.author.sent == []
    token = delivered_token(target.sent, world.author.id)
    assert await authenticate_token(db, token) == world.author.id
    assert world.client.fetched == []
    assert_not_logged(caplog, token)


async def test_fetched_channel_in_the_same_guild(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    uncached = FakeChannel(guild=world.guild)
    world.client.fetchable[uncached.id] = uncached
    replies = await handle(world, bot_ctx, world.message(f" API {uncached.id}"))
    assert replies == [tm.sent_to_channel(uncached.id)]
    assert world.client.fetched == [uncached.id]
    assert await authenticate_token(db, delivered_token(uncached.sent, world.author.id)) == world.author.id


async def test_bot_author_needs_a_channel(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    bot_author = world.guild.add_member(bot=True)
    assert await handle(world, bot_ctx, world.message(" API", author=bot_author)) == [tm.BOT_NEEDS_CHANNEL]
    assert await token_rows(db) == []
    replies = await handle(world, bot_ctx, world.message(f" API {world.here.id}", author=bot_author))
    assert replies == [tm.sent_to_channel(world.here.id)]
    assert await authenticate_token(db, delivered_token(world.here.sent, bot_author.id)) == bot_author.id


@pytest.mark.parametrize(
    ("argument", "fetch_result", "reply"),
    [
        ("12ab", None, tm.NON_NUMERIC_CHANNEL),
        ("-5", None, tm.NON_NUMERIC_CHANNEL),
        (chr(0xFF11) + chr(0xFF12) + chr(0xFF13) + chr(0xFF14), None, tm.CHANNEL_NOT_FOUND),
        (chr(0xB2), None, tm.NON_NUMERIC_CHANNEL),
        ("0", None, tm.CHANNEL_NOT_FOUND),
        ("99999999999999999999", None, tm.CHANNEL_NOT_FOUND),
        ("1234", None, tm.CHANNEL_NOT_FOUND),
        ("1234", "forbidden", tm.CHANNEL_FORBIDDEN),
        ("1234", "error", tm.CHANNEL_FETCH_FAILED),
        ("1234", "other guild", tm.OTHER_GUILD_CHANNEL),
        ("1234", "dm channel", tm.NOT_GUILD_CHANNEL),
        ("1234", "category", tm.NOT_MESSAGEABLE),
    ],
)
async def test_rejected_channel_arguments_issue_nothing(db: AsyncEngine, bot_ctx: BotContext, world: World, argument: str, fetch_result: str | None, reply: str) -> None:
    results: dict[str, Any] = {
        "forbidden": forbidden(),
        "error": server_error(),
        "other guild": FakeChannel(guild=world.other),
        "dm channel": FakeChannel(),
        "category": SimpleNamespace(id=1234, guild=world.guild),
    }
    if fetch_result is not None:
        world.client.fetchable[1234] = results[fetch_result]
    assert await handle(world, bot_ctx, world.message(f" API {argument}")) == [reply]
    assert await token_rows(db) == []
    assert world.author.sent == []


async def test_cached_channel_of_another_guild_is_rejected(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    assert await handle(world, bot_ctx, world.message(f" API {world.there.id}")) == [tm.OTHER_GUILD_CHANNEL]
    assert world.there.sent == []
    assert await token_rows(db) == []


async def test_channel_from_a_dm_is_rejected(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    user = FakeUser()
    replies = await handle(world, bot_ctx, world.message(f" API {world.here.id}", author=user, channel=FakeChannel()))
    assert replies == [tm.DM_WITH_CHANNEL]
    assert world.client.fetched == []
    assert await token_rows(db) == []


@pytest.mark.parametrize(
    ("error", "reply"),
    [(forbidden(), tm.DM_FORBIDDEN), (server_error(), tm.DM_FAILED)],
)
async def test_undeliverable_dm_revokes_the_token(db: AsyncEngine, bot_ctx: BotContext, world: World, error: Exception, reply: str, caplog: pytest.LogCaptureFixture) -> None:
    user = ClosedDmUser(error)
    with caplog.at_level(logging.DEBUG):
        replies = await handle(world, bot_ctx, world.message(" API", author=user))
    assert replies == [f"{reply}\n{tm.REVOKED_NOTE}"]
    rows = await token_rows(db)
    assert len(rows) == 1
    assert rows[0].account_id == user.id
    assert rows[0].revoked_at is not None
    token = attempted_token(user.attempted, user.id)
    assert await authenticate_token(db, token) is None
    assert_not_logged(caplog, token)


async def test_undeliverable_channel_message_revokes_the_token(db: AsyncEngine, bot_ctx: BotContext, world: World, caplog: pytest.LogCaptureFixture) -> None:
    await handle(world, bot_ctx, world.message(" API"))
    earlier = delivered_token(world.author.sent, world.author.id)
    for error, reply in ((forbidden(), tm.send_forbidden), (server_error(), None)):
        channel = FailingChannel(error, guild=world.guild)
        world.guild.channels[channel.id] = channel
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            replies = await handle(world, bot_ctx, world.message(f" API {channel.id}"))
        failure = reply(channel.id) if reply is not None else tm.SEND_FAILED
        assert replies == [f"{failure}\n{tm.REVOKED_NOTE}"]
        assert_not_logged(caplog, attempted_token(channel.attempted, world.author.id))
    rows = await token_rows(db)
    assert len(rows) == 3
    assert all(row.revoked_at is not None for row in rows)
    assert await authenticate_token(db, earlier) is None


async def test_undelivered_revocation_uses_a_key_per_token(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    user = ClosedDmUser(server_error())
    await handle(world, bot_ctx, world.message(" API", author=user))
    (row,) = await token_rows(db)
    async with db.connect() as conn:
        keys = (await conn.execute(select(execution.c.idempotency_key).where(execution.c.type == ExecutionType.TOKEN_REVOKE))).scalars().all()
    assert keys == [tm.undelivered_revoke_key(row.api_token_id)]
    assert tm.undelivered_revoke_key(row.api_token_id) == f"undelivered-token-{row.api_token_id}"


async def test_a_failed_revocation_of_an_undelivered_token_still_replies(db: AsyncEngine, bot_ctx: BotContext, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tm, "revoke_tokens", broken)
    channel = FailingChannel(server_error(), guild=world.guild)
    world.guild.channels[channel.id] = channel
    with caplog.at_level(logging.DEBUG):
        replies = await handle(world, bot_ctx, world.message(f" API {channel.id}"))
    assert replies == [f"{tm.SEND_FAILED}\n{tm.REVOKE_FAILED_NOTE}"]
    assert any(record.getMessage() == "undelivered api token could not be revoked" and record.exc_info for record in caplog.records)
    assert_not_logged(caplog, attempted_token(channel.attempted, world.author.id))


async def test_a_cancelled_delivery_revokes_the_token_and_propagates(db: AsyncEngine, bot_ctx: BotContext, world: World, caplog: pytest.LogCaptureFixture) -> None:
    channel = HangingChannel(guild=world.guild)
    world.guild.channels[channel.id] = channel
    message = world.message(f" API {channel.id}")
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(handle(world, bot_ctx, message))
        await asyncio.wait_for(channel.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    token = attempted_token(channel.attempted, world.author.id)
    assert await authenticate_token(db, token) is None
    rows = await token_rows(db)
    assert len(rows) == 1
    assert rows[0].revoked_at is not None
    assert message.replies == []
    assert_not_logged(caplog, token)


@pytest.mark.parametrize(
    "content",
    [" hello", "", "  ", " APIs", " トークン API"],
)
async def test_other_mentions_are_ignored(db: AsyncEngine, bot_ctx: BotContext, world: World, content: str) -> None:
    message = world.message(content)
    assert await handle(world, bot_ctx, message) == []
    assert await token_rows(db) == []


async def test_messages_not_starting_with_the_mention_or_from_the_bot_are_ignored(db: AsyncEngine, bot_ctx: BotContext, world: World) -> None:
    bot_user_id = world.client.user.id
    messages = [
        world.message("API", mention=False),
        world.message(f"hi <@{bot_user_id}> API", mention=False),
        world.message(f"<@!{bot_user_id}> API", mention=False),
        world.message(f" API {world.here.id}", author=world.client.user),
    ]
    webhook = world.message(f" API {world.here.id}", author=FakeUser(bot=True))
    webhook.webhook_id = 77  # type: ignore[attr-defined]
    messages.append(webhook)
    for message in messages:
        assert await handle(world, bot_ctx, message) == []
    assert await token_rows(db) == []
    assert world.here.sent == []


async def test_issue_failure_replies_with_an_error(db: AsyncEngine, bot_ctx: BotContext, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tm, "issue_token", broken)
    assert await handle(world, bot_ctx, world.message(" API")) == [tm.ISSUE_FAILED]
    assert world.author.sent == []


async def _orphaned_revoke(world: World, ctx: BotContext, monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, ClosedDmUser]:
    """Fail a DM delivery whose revocation waits for the returned gate, and cancel the handler while it waits, leaving the revocation pending."""
    gate, entered = asyncio.Event(), asyncio.Event()
    original = revoke_tokens

    async def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(tm, "revoke_tokens", held)
    user = ClosedDmUser(server_error())
    task = asyncio.create_task(handle(world, ctx, world.message(" API", author=user)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(tm._pending_revokes) == 1
    return gate, user


async def test_shutdown_waits_for_a_pending_revocation(db: AsyncEngine, bot_ctx: BotContext, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    gate, user = await _orphaned_revoke(world, bot_ctx, monkeypatch)
    drain = asyncio.create_task(tm.drain_pending_revokes(bound=5))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not drain.done()
    gate.set()
    await asyncio.wait_for(drain, timeout=5)
    assert tm._pending_revokes == set()
    assert await authenticate_token(db, attempted_token(user.attempted, user.id)) is None


async def test_shutdown_logs_a_revocation_that_outlasts_the_bound(db: AsyncEngine, bot_ctx: BotContext, world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    gate, _ = await _orphaned_revoke(world, bot_ctx, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="rapidwire.bot.token_mention"):
        await asyncio.wait_for(tm.drain_pending_revokes(bound=0.05), timeout=5)
    (record,) = [record for record in caplog.records if record.getMessage() == "undelivered api token revocations did not finish before shutdown"]
    assert record.__dict__["pending"] == 1
    gate.set()
    await asyncio.wait_for(tm.drain_pending_revokes(bound=5), timeout=5)
    assert tm._pending_revokes == set()


async def test_setup_listens_to_messages(bot: RapidWireBot) -> None:
    handlers = [handler for handler in bot.listeners("message") if handler.__module__ == tm.__name__]
    assert len(handlers) == 1
    assert tm.drain_pending_revokes in bot._shutdown_hooks
