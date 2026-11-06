"""The API token mention: a message that starts with the bot's mention followed by API [channel_id] issues an API token for its author and sends it by direct message or to the channel.

The parsing and the checks follow the old bot's API key mention; issuing revokes the author's earlier tokens, the plaintext is sent once and never logged, and a token whose delivery failed or was cancelled is revoked again.
The message content of a message that mentions the bot arrives without the privileged message content intent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import discord

from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.context import BotContext
from rapidwire.core.tokens import issue_token, revoke_tokens
from rapidwire.core.validation import BIGINT_MAX

__all__ = [
    "BOT_NEEDS_CHANNEL",
    "CHANNEL_FETCH_FAILED",
    "CHANNEL_FORBIDDEN",
    "CHANNEL_NOT_FOUND",
    "COMMAND",
    "DM_FAILED",
    "DM_FORBIDDEN",
    "DM_WITH_CHANNEL",
    "ISSUE_FAILED",
    "NON_NUMERIC_CHANNEL",
    "NOT_GUILD_CHANNEL",
    "NOT_MESSAGEABLE",
    "OTHER_GUILD_CHANNEL",
    "REVOKED_NOTE",
    "REVOKE_DRAIN_TIMEOUT_SECONDS",
    "REVOKE_FAILED_NOTE",
    "SEND_FAILED",
    "SENT_TO_DM",
    "drain_pending_revokes",
    "handle_message",
    "send_forbidden",
    "sent_to_channel",
    "setup",
    "token_message",
    "undelivered_revoke_key",
]

log = logging.getLogger("rapidwire.bot.token_mention")

# The first word after the mention, compared case-insensitively.
COMMAND: Final = "API"

NON_NUMERIC_CHANNEL: Final = "チャンネルIDは数値で指定してください。"
BOT_NEEDS_CHANNEL: Final = "BotがAPIトークンを発行する場合は、チャンネルIDを指定する必要があります。"
CHANNEL_NOT_FOUND: Final = "指定されたチャンネルが見つかりませんでした。"
CHANNEL_FORBIDDEN: Final = "指定されたチャンネルへのアクセス権がありません。"
CHANNEL_FETCH_FAILED: Final = "チャンネルの取得中にエラーが発生しました。"
DM_WITH_CHANNEL: Final = "チャンネルを指定してAPIトークンを発行する場合は、サーバー内でコマンドを実行してください。"
NOT_GUILD_CHANNEL: Final = "指定されたチャンネルはサーバーのチャンネルではありません。"
OTHER_GUILD_CHANNEL: Final = "指定されたチャンネルは、メッセージを送信したサーバーと同一のサーバーにある必要があります。"
NOT_MESSAGEABLE: Final = "指定されたチャンネルにはメッセージを送信できません。"
ISSUE_FAILED: Final = "APIトークンの処理中にエラーが発生しました。"
SENT_TO_DM: Final = "APIトークンをDMに送信しました。"
SEND_FAILED: Final = "メッセージの送信中にエラーが発生しました。"
DM_FORBIDDEN: Final = "APIトークンをDMに送信できませんでした。DMの受信設定を確認してください。"
DM_FAILED: Final = "DMの送信中にエラーが発生しました。"
REVOKED_NOTE: Final = "発行したAPIトークンは無効にしました。以前のAPIトークンも無効になっているため、再度発行してください。"
REVOKE_FAILED_NOTE: Final = "発行したAPIトークンを無効にできませんでした。以前のAPIトークンも無効になっているため、再度発行してください。再度発行すると、このAPIトークンも無効になります。"

# Seconds shutdown waits for revocations of undelivered tokens still running.
REVOKE_DRAIN_TIMEOUT_SECONDS: Final = 5.0

# Revocations of undelivered tokens that outlive a cancelled handler.
_pending_revokes: set[asyncio.Task[bool]] = set()


def token_message(mention: str, token: str) -> str:
    """The message that hands the token to its owner."""
    return f"{mention} のAPIトークンです。大切に保管してください。\n```{token}```"


def sent_to_channel(channel_id: int) -> str:
    return f"<#{channel_id}> にAPIトークンを送信しました。"


def send_forbidden(channel_id: int) -> str:
    return f"<#{channel_id}> にメッセージを送信できませんでした。権限を確認してください。"


def _delivery_failure(channel_id: int | None, *, forbidden: bool) -> str:
    """The reply when the token could not be sent by DM (channel_id None) or to the channel."""
    if channel_id is None:
        return DM_FORBIDDEN if forbidden else DM_FAILED
    return send_forbidden(channel_id) if forbidden else SEND_FAILED


def undelivered_revoke_key(token_id: int) -> str:
    """The idempotency key of the revocation of an undelivered token."""
    return f"undelivered-token-{token_id}"


async def _revoke(ctx: BotContext, account_id: int, token_id: int) -> bool:
    try:
        await revoke_tokens(ctx.engine, account_id=account_id, token_id=token_id, now=ctx.now(), idempotency_key=undelivered_revoke_key(token_id))
    except Exception:
        log.exception("undelivered api token could not be revoked", extra={"account_id": account_id, "token_id": token_id})
        return False
    return True


async def _revoke_undelivered(ctx: BotContext, account_id: int, token_id: int) -> bool:
    """Revoke a token that was not delivered, finishing the revocation even when the caller is cancelled; False when it failed, which is logged."""
    task = asyncio.create_task(_revoke(ctx, account_id, token_id), name=f"revoke undelivered token {token_id}")
    _pending_revokes.add(task)
    task.add_done_callback(_pending_revokes.discard)
    return await asyncio.shield(task)


async def _reply(message: discord.Message, text: str) -> None:
    try:
        await message.reply(text)
    except discord.HTTPException as exc:
        log.warning("token mention reply failed", extra={"status": exc.status, "channel_id": message.channel.id})


def _parse(client: discord.Client, message: discord.Message) -> list[str] | None:
    """The words after the leading mention of the bot when the first one is API, or None when the message is not a token request."""
    user = client.user
    if user is None or message.author.id == user.id or getattr(message, "webhook_id", None) is not None:
        return None
    prefix = f"<@{user.id}>"
    if not message.content.startswith(prefix):
        return None
    args = message.content[len(prefix) :].strip().split()
    if not args or args[0].upper() != COMMAND:
        return None
    return args[1:]


async def _resolve_channel(client: discord.Client, message: discord.Message, channel_id: int) -> Any:
    """The target channel, or a reply text explaining why it cannot be used."""
    if message.guild is None:
        return DM_WITH_CHANNEL
    if not 1 <= channel_id <= BIGINT_MAX:
        return CHANNEL_NOT_FOUND
    channel: Any = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.NotFound:
            return CHANNEL_NOT_FOUND
        except discord.Forbidden:
            return CHANNEL_FORBIDDEN
        except discord.DiscordException as exc:
            log.warning("token mention channel fetch failed", extra={"channel_id": channel_id, "error": type(exc).__name__})
            return CHANNEL_FETCH_FAILED
    guild = getattr(channel, "guild", None)
    if guild is None:
        return NOT_GUILD_CHANNEL
    if guild.id != message.guild.id:
        return OTHER_GUILD_CHANNEL
    if not callable(getattr(channel, "send", None)):
        return NOT_MESSAGEABLE
    return channel


async def handle_message(client: discord.Client, ctx: BotContext, message: discord.Message) -> None:
    """Issue and deliver a token when message is `<@bot> API [channel_id]`, and ignore every other message.

    Without a channel ID the token goes to the author's DMs, which a bot author cannot receive; a channel must be a server channel of the server the message was sent in.
    """
    args = _parse(client, message)
    if args is None:
        return
    channel_id: int | None = None
    if args:
        if not args[0].isdecimal():
            await _reply(message, NON_NUMERIC_CHANNEL)
            return
        channel_id = int(args[0])
    if message.author.bot and channel_id is None:
        await _reply(message, BOT_NEEDS_CHANNEL)
        return
    target: Any = None
    if channel_id is not None:
        target = await _resolve_channel(client, message, channel_id)
        if isinstance(target, str):
            await _reply(message, target)
            return

    account_id = message.author.id
    try:
        issued = await issue_token(ctx.engine, account_id=account_id, now=ctx.now())
    except Exception:
        log.exception("api token issue failed", extra={"account_id": account_id})
        await _reply(message, ISSUE_FAILED)
        return

    text = token_message(message.author.mention, issued.token)
    delivery = "dm" if channel_id is None else "channel"
    try:
        if target is None:
            dm = await message.author.create_dm()
            await dm.send(text)
        else:
            await target.send(text)
    except asyncio.CancelledError:
        log.warning("api token delivery was cancelled, revoking it", extra={"account_id": account_id, "token_id": issued.token_id, "delivery": delivery})
        await _revoke_undelivered(ctx, account_id, issued.token_id)
        raise
    except Exception as exc:
        log.warning("api token delivery failed, revoking it", extra={"account_id": account_id, "token_id": issued.token_id, "delivery": delivery, "error": type(exc).__name__})
        revoked = await _revoke_undelivered(ctx, account_id, issued.token_id)
        note = REVOKED_NOTE if revoked else REVOKE_FAILED_NOTE
        await _reply(message, f"{_delivery_failure(channel_id, forbidden=isinstance(exc, discord.Forbidden))}\n{note}")
        return
    log.info("api token issued", extra={"account_id": account_id, "token_id": issued.token_id, "delivery": delivery})
    await _reply(message, SENT_TO_DM if channel_id is None else sent_to_channel(channel_id))


async def drain_pending_revokes(bound: float = REVOKE_DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait up to bound seconds for the revocations of undelivered tokens still running, so they finish before the engine is disposed; revocations still running after that are logged and left to the shutdown."""
    tasks = set(_pending_revokes)
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=bound)
    if pending:
        log.warning("undelivered api token revocations did not finish before shutdown", extra={"pending": len(pending), "timeout_seconds": bound})


def setup(bot: RapidWireBot) -> None:
    """Register the message listener of the token mention and the shutdown hook that waits for pending revocations on bot."""

    async def on_message(message: discord.Message) -> None:
        await handle_message(bot, bot.context, message)

    bot.add_listener("message", on_message)
    bot.add_shutdown_hook(drain_pending_revokes)
