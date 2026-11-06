"""The /approve group: set and info of allowances."""

from __future__ import annotations

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.embeds import amount_text, success_embed
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.interaction import Reply, idempotency_key, run_command
from rapidwire.bot.symbols import resolve_currency
from rapidwire.core import allowances
from rapidwire.core import errors as core_errors
from rapidwire.core.amounts import NumberFormatError, parse_amount

__all__ = ["approve_group", "setup"]

approve_group = app_commands.Group(name="approve", description="他のユーザーにあなたの資産の使用を許可します。")


async def _set(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    try:
        units = parse_amount(amount, allow_zero=True)
    except NumberFormatError as error:
        if error.kind == "amount_negative":
            raise UserInputError("許可額は0以上である必要があります。") from None
        raise
    info = await resolve_currency(ctx, interaction, symbol)
    result = await allowances.approve(
        ctx.engine, owner_id=interaction.user.id, spender_id=user.id, currency_id=info.currency_id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction)
    )
    return Reply(embed=success_embed("承認完了", f"{user.mention} に {amount_text(result.new_amount, info.symbol)} の使用を許可しました。"))


@approve_group.command(name="set", description="指定したユーザーに資産の使用を許可します。")
@app_commands.describe(user="許可を与えるユーザー", amount="許可する量 (0 で取り消し)", symbol="通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def approve_set(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, symbol: str | None = None) -> None:
    overrides = {core_errors.InvalidArgument.code: "自分自身に許可を与えることはできません。"}
    await run_command(interaction, lambda: _set(interaction, user, amount, symbol), overrides=overrides)


async def _info(interaction: discord.Interaction[RapidWireBot], user: discord.User, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    info = await resolve_currency(ctx, interaction, symbol)
    current = await allowances.get_allowance(ctx.engine, interaction.user.id, user.id, info.currency_id)
    return Reply(embed=success_embed("許可情報", f"{user.mention} への現在の許可額: {amount_text(current, info.symbol)}"))


@approve_group.command(name="info", description="指定したユーザーへの許可状況を確認します。")
@app_commands.describe(user="確認するユーザー", symbol="通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def approve_info(interaction: discord.Interaction[RapidWireBot], user: discord.User, symbol: str | None = None) -> None:
    await run_command(interaction, lambda: _info(interaction, user, symbol))


def setup(bot: RapidWireBot) -> None:
    """Add the /approve group to bot.tree."""
    bot.tree.add_command(approve_group)
