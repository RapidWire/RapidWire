"""Embed builders in the styles of the earlier bot (error red with the title ERROR_TITLE, success green, information blue, pools and stakes purple) and the display forms of amounts, users and times."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import discord

from rapidwire.core.amounts import format_amount_display

__all__ = [
    "DESCRIPTION_MAX",
    "ERROR_COLOUR",
    "ERROR_TITLE",
    "FIELD_VALUE_MAX",
    "INFO_COLOUR",
    "ITEMS_PER_PAGE",
    "POOL_COLOUR",
    "STAKE_COLOUR",
    "SUCCESS_COLOUR",
    "SUCCESS_TITLE",
    "SYSTEM_ACCOUNT_LABEL",
    "UNKNOWN_USER_SUFFIX",
    "Field",
    "account_mention",
    "amount_text",
    "error_embed",
    "info_embed",
    "plain_text",
    "pool_embed",
    "resolve_user_label",
    "stake_embed",
    "success_embed",
    "timestamp_full",
    "timestamp_relative",
    "unknown_user_label",
]

ERROR_COLOUR: Final = discord.Colour.red()
SUCCESS_COLOUR: Final = discord.Colour.green()
INFO_COLOUR: Final = discord.Colour.blue()
POOL_COLOUR: Final = discord.Colour.purple()
STAKE_COLOUR: Final = discord.Colour.purple()

# Page size of every paged list the bot shows.
ITEMS_PER_PAGE: Final = 15

SYSTEM_ACCOUNT_LABEL: Final = "システム"
UNKNOWN_USER_SUFFIX: Final = "(不明なユーザー)"
ERROR_TITLE: Final = "エラー"
SUCCESS_TITLE: Final = "成功"

# Discord's limits on embed field values and descriptions.
FIELD_VALUE_MAX: Final = 1024
DESCRIPTION_MAX: Final = 4096


@dataclass(frozen=True)
class Field:
    """One embed field; fields are full width unless inline is set."""

    name: str
    value: str
    inline: bool = False


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _embed(title: str, description: str | None, colour: discord.Colour, fields: Iterable[Field]) -> discord.Embed:
    """An embed whose description is cut to DESCRIPTION_MAX characters with ... when longer."""
    embed = discord.Embed(title=title, description=None if description is None else _cut(description, DESCRIPTION_MAX), colour=colour)
    for field in fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    return embed


def error_embed(message: str, *, fields: Iterable[Field] = ()) -> discord.Embed:
    """A red embed titled ERROR_TITLE with message as its description."""
    return _embed(ERROR_TITLE, message, ERROR_COLOUR, fields)


def success_embed(title: str = SUCCESS_TITLE, description: str | None = None, *, fields: Iterable[Field] = ()) -> discord.Embed:
    """A green embed for a completed operation or a list such as balances."""
    return _embed(title, description, SUCCESS_COLOUR, fields)


def info_embed(title: str, description: str | None = None, *, fields: Iterable[Field] = ()) -> discord.Embed:
    """A blue embed for information such as currency details, history and confirmations."""
    return _embed(title, description, INFO_COLOUR, fields)


def pool_embed(title: str, description: str | None = None, *, fields: Iterable[Field] = ()) -> discord.Embed:
    """A purple embed for liquidity pools and positions."""
    return _embed(title, description, POOL_COLOUR, fields)


def stake_embed(title: str, description: str | None = None, *, fields: Iterable[Field] = ()) -> discord.Embed:
    """A purple embed for stakes."""
    return _embed(title, description, STAKE_COLOUR, fields)


def amount_text(units: int, symbol: str | None = None) -> str:
    """An amount in minimal units as inline code with thousands separators and 3 decimals, followed by the symbol when given ("`1,000.000 RAPID`")."""
    shown = format_amount_display(units)
    return f"`{shown}`" if symbol is None else f"`{shown} {symbol}`"


def account_mention(account_id: int) -> str:
    """A user mention, or SYSTEM_ACCOUNT_LABEL for account 0 and the negative system accounts (issuance, custody)."""
    return SYSTEM_ACCOUNT_LABEL if account_id <= 0 else f"<@{account_id}>"


def unknown_user_label(user_id: int) -> str:
    """The label of a user Discord does not know: the ID as inline code followed by UNKNOWN_USER_SUFFIX."""
    return f"`{user_id}` {UNKNOWN_USER_SUFFIX}"


async def resolve_user_label(client: discord.Client, user_id: int) -> str:
    """The mention of a user known to Discord (cache first, then one fetch), unknown_user_label() when Discord answers that the user does not exist, and a plain mention when the lookup fails otherwise."""
    if user_id <= 0:
        return SYSTEM_ACCOUNT_LABEL
    if client.get_user(user_id) is not None:
        return f"<@{user_id}>"
    try:
        await client.fetch_user(user_id)
    except discord.NotFound:
        return unknown_user_label(user_id)
    except discord.HTTPException:
        return f"<@{user_id}>"
    return f"<@{user_id}>"


def _epoch(moment: datetime | int) -> int:
    return moment if isinstance(moment, int) else int(moment.timestamp())


def timestamp_full(moment: datetime | int) -> str:
    """A Discord timestamp shown as a full date and time (<t:N:F>); moment is an aware datetime or Unix seconds."""
    return f"<t:{_epoch(moment)}:F>"


def timestamp_relative(moment: datetime | int) -> str:
    """A Discord timestamp shown relative to now (<t:N:R>)."""
    return f"<t:{_epoch(moment)}:R>"


def plain_text(text: str, *, limit: int = FIELD_VALUE_MAX) -> str:
    """User or contract supplied text made inert for an embed: markdown and mentions escaped, cut to limit characters with ... when longer."""
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return escaped if len(escaped) <= limit else escaped[: max(limit - 3, 0)] + "..."
