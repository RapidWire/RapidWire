"""Autocomplete callbacks shared by the commands; contract function and argument completion lives with the contract commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.client import bot_context
from rapidwire.bot.symbols import normalize_symbol
from rapidwire.core.queries import CurrencySymbol, search_currency_symbols
from rapidwire.db.tables import CURRENCY_SYMBOL_MAX

__all__ = ["AUTOCOMPLETE_TIMEOUT", "MAX_CHOICES", "symbol_autocomplete", "symbol_choices"]

log = logging.getLogger("rapidwire.bot")

# Discord shows at most 25 autocomplete choices.
MAX_CHOICES: Final = 25
# Seconds a lookup may take; Discord drops an autocomplete answer sent after 3 seconds.
AUTOCOMPLETE_TIMEOUT: Final = 2.5
# Discord's limit on a choice name.
_CHOICE_NAME_MAX: Final = 100


def symbol_choices(currencies: Iterable[CurrencySymbol]) -> list[app_commands.Choice[str]]:
    """Choices named "SYMBOL (name)" with the symbol as value, at most MAX_CHOICES, in the given order."""
    return [app_commands.Choice(name=f"{item.symbol} ({item.name})"[:_CHOICE_NAME_MAX], value=item.symbol) for item in list(currencies)[:MAX_CHOICES]]


async def symbol_autocomplete(interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
    """Live currencies whose symbol contains the typed text uppercased, those starting with it first, at most MAX_CHOICES; text longer than a symbol offers nothing, and a lookup that fails or outlasts AUTOCOMPLETE_TIMEOUT is logged and offers no choices."""
    typed = normalize_symbol(current)
    if len(typed) > CURRENCY_SYMBOL_MAX:
        return []
    try:
        async with asyncio.timeout(AUTOCOMPLETE_TIMEOUT):
            found = await search_currency_symbols(bot_context(interaction).engine, typed, limit=MAX_CHOICES)
    except TimeoutError:
        log.warning("symbol autocomplete timed out", extra={"timeout_seconds": AUTOCOMPLETE_TIMEOUT})
        return []
    except Exception:
        log.exception("symbol autocomplete failed")
        return []
    return symbol_choices(found)
