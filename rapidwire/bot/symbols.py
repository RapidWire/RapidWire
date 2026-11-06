"""Currency arguments of commands: a symbol is matched case-insensitively, and a missing symbol means the currency of the guild the command runs in, as in the earlier bot."""

from __future__ import annotations

from typing import Any

import discord

from rapidwire.bot.context import BotContext
from rapidwire.bot.errors import UserInputError
from rapidwire.core.currency import CurrencyInfo, get_currency, get_currency_by_symbol
from rapidwire.db.tables import CURRENCY_SYMBOL_MAX

__all__ = ["normalize_symbol", "resolve_currency"]

# How much of an unknown symbol the error message repeats.
_SHOWN_SYMBOL_MAX = 32


def normalize_symbol(text: str) -> str:
    """The symbol a user typed, trimmed and uppercased."""
    return text.strip().upper()


async def resolve_currency(ctx: BotContext, interaction: discord.Interaction[Any], symbol: str | None) -> CurrencyInfo:
    """The live currency named by symbol, or with no symbol the currency of the interaction's guild; raise UserInputError when the symbol matches no currency, when no symbol is given in direct messages, or when the guild has no currency."""
    if symbol is not None and symbol.strip():
        wanted = normalize_symbol(symbol)
        info = await get_currency_by_symbol(ctx.engine, wanted, now=ctx.now()) if len(wanted) <= CURRENCY_SYMBOL_MAX else None
        if info is None:
            shown = wanted.replace("`", "")[:_SHOWN_SYMBOL_MAX]
            raise UserInputError(f"通貨 `{shown}` が見つかりませんでした。シンボルを正しく指定してください。")
        return info
    if interaction.guild_id is None:
        raise UserInputError("サーバー内で実行するか、通貨のシンボルを指定してください。")
    info = await get_currency(ctx.engine, interaction.guild_id, now=ctx.now())
    if info is None:
        raise UserInputError("このサーバーには通貨がありません。通貨のシンボルを指定してください。")
    return info
