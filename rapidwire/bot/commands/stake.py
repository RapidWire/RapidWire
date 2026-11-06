"""The /stake group: deposit, withdraw and info."""

from __future__ import annotations

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.commands.account import load_page
from rapidwire.bot.embeds import ITEMS_PER_PAGE, Field, amount_text, stake_embed, success_embed, timestamp_full
from rapidwire.bot.interaction import Reply, idempotency_key, run_command
from rapidwire.bot.paging import page_title
from rapidwire.bot.symbols import resolve_currency
from rapidwire.core import errors as core_errors
from rapidwire.core import staking
from rapidwire.core.amounts import parse_amount
from rapidwire.core.clock import resolve_now
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.paging import Page
from rapidwire.core.queries import currency_supplies
from rapidwire.core.staking import StakeInfo

__all__ = ["rate_text", "setup", "stake_group"]

stake_group = app_commands.Group(name="stake", description="ステーキングに関連するコマンド")

# Rates are stored in parts per million per hour, so a percentage has at most 4 decimals.
_PPM_PER_PERCENT = 10_000


def rate_text(rate_ppm: int) -> str:
    """An hourly rate in parts per million as a percentage with 4 decimals ("1.2500%/h"), as the earlier bot showed it."""
    whole, fraction = divmod(rate_ppm, _PPM_PER_PERCENT)
    return f"{whole}.{fraction:04d}%/h"


async def _deposit(interaction: discord.Interaction[RapidWireBot], amount: str, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    units = parse_amount(amount, allow_zero=False)
    info = await resolve_currency(ctx, interaction, symbol)
    result = await staking.deposit(ctx.engine, account_id=interaction.user.id, currency_id=info.currency_id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    description = f"{amount_text(result.amount, info.symbol)} のステーキングが完了しました。\n現在の合計ステーキング額は {amount_text(result.stake_amount, info.symbol)} です。"
    return Reply(embed=success_embed("ステーキング完了", description))


@stake_group.command(name="deposit", description="通貨を預け入れ、ステーキングを開始します。")
@app_commands.describe(amount="預け入れる量", symbol="預け入れる通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def stake_deposit(interaction: discord.Interaction[RapidWireBot], amount: str, symbol: str | None = None) -> None:
    overrides = {core_errors.InsufficientBalance.code: "ステーキングするための残高が不足しています。"}
    await run_command(interaction, lambda: _deposit(interaction, amount, symbol), overrides=overrides)


async def _withdraw(interaction: discord.Interaction[RapidWireBot], amount: str, symbol: str | None) -> Reply:
    ctx = bot_context(interaction)
    units = parse_amount(amount, allow_zero=False)
    info = await resolve_currency(ctx, interaction, symbol)
    result = await staking.withdraw(ctx.engine, account_id=interaction.user.id, currency_id=info.currency_id, amount=units, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    description = f"{amount_text(result.amount, info.symbol)} を引き出しました。\n残りのステーキング額: {amount_text(result.stake_amount, info.symbol)}"
    return Reply(embed=success_embed("引き出し完了", description))


@stake_group.command(name="withdraw", description="ステーキングした通貨の一部または全部を引き出します。")
@app_commands.describe(amount="引き出す量", symbol="引き出す通貨のシンボル")
@app_commands.autocomplete(symbol=symbol_autocomplete)
async def stake_withdraw(interaction: discord.Interaction[RapidWireBot], amount: str, symbol: str | None = None) -> None:
    await run_command(interaction, lambda: _withdraw(interaction, amount, symbol))


def _stake_field(item: StakeInfo, amount: int) -> Field:
    value = f"ステーキング額: {amount_text(amount)}\n現在の利率: `{rate_text(item.hourly_rate_ppm)}`\n最終更新日時: {timestamp_full(item.last_accrued_at)}"
    return Field(f"通貨: **{item.name} ({item.symbol})**", value)


async def _info(interaction: discord.Interaction[RapidWireBot], page: int) -> Reply:
    ctx = bot_context(interaction)
    now = resolve_now(ctx.now())
    supplies: dict[int, int] = {}

    async def fetch(number: int) -> Page[StakeInfo]:
        async with consistent_snapshot(ctx.engine) as conn:
            found = await staking.list_stakes(conn, interaction.user.id, page=number, per_page=ITEMS_PER_PAGE)
            supplies.update(await currency_supplies(conn, (item.currency_id for item in found.items)))
        return found

    result, pages = await load_page(page, ITEMS_PER_PAGE, fetch)
    if not result.items:
        return Reply(embed=success_embed("ステーキング情報", "現在、アクティブなステーキングはありません。"))
    fields = [_stake_field(item, staking.projected_amount(item, now, supply=supplies.get(item.currency_id))) for item in result.items]
    return Reply(embed=stake_embed(page_title(f"{interaction.user.display_name}のステーキング情報", page, pages), fields=fields))


@stake_group.command(name="info", description="あなたのステーキング状況を表示します。")
@app_commands.describe(page="ページ番号")
async def stake_info(interaction: discord.Interaction[RapidWireBot], page: int = 1) -> None:
    await run_command(interaction, lambda: _info(interaction, page))


def setup(bot: RapidWireBot) -> None:
    """Add the /stake group to bot.tree."""
    bot.tree.add_command(stake_group)
