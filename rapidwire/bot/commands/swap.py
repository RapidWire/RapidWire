"""The /swap command: an ephemeral quote with the allocation breakdown, confirmed through SwapConfirmView."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.interaction import Reply, run_command
from rapidwire.bot.symbols import resolve_currency
from rapidwire.bot.views.swap import ZERO_OUTPUT_MESSAGE, SwapConfirmView, quote_embed
from rapidwire.core.amounts import parse_amount
from rapidwire.core.errors import NoRoute
from rapidwire.core.swap import quote_swap

__all__ = ["build_command", "setup"]


def build_command() -> app_commands.Command[Any, ..., None]:
    """A new /swap command."""

    @app_commands.command(name="swap", description="通貨をスワップします。")
    @app_commands.describe(from_symbol="スワップ元の通貨シンボル", to_symbol="スワップ先の通貨シンボル", amount="スワップする量")
    @app_commands.autocomplete(from_symbol=symbol_autocomplete, to_symbol=symbol_autocomplete)
    async def swap_command(interaction: discord.Interaction[Any], from_symbol: str, to_symbol: str, amount: str) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            source = await resolve_currency(ctx, interaction, from_symbol)
            target = await resolve_currency(ctx, interaction, to_symbol)
            if source.currency_id == target.currency_id:
                raise UserInputError("スワップ元とスワップ先には異なる通貨を指定してください。")
            amount_in = parse_amount(amount, allow_zero=False)
            try:
                quote = await quote_swap(ctx.engine, from_currency=source.currency_id, to_currency=target.currency_id, amount_in=amount_in, config=ctx.swap_config)
            except NoRoute as error:
                if error.zero_output:
                    raise UserInputError(ZERO_OUTPUT_MESSAGE) from None
                raise
            return Reply(embed=quote_embed(quote), view=SwapConfirmView(ctx, interaction, quote))

        await run_command(interaction, work, ephemeral=True)

    return swap_command


def setup(bot: RapidWireBot) -> None:
    """Add the /swap command to bot.tree."""
    bot.tree.add_command(build_command())
