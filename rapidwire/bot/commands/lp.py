"""The /lp group: creating pools, adding and removing liquidity, the pools of a pair, and a provider's positions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import symbol_autocomplete
from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.context import BotContext
from rapidwire.bot.dex_format import FEE_TIER_CHOICES, fee_tier_label
from rapidwire.bot.embeds import ITEMS_PER_PAGE, Field, amount_text, pool_embed, success_embed
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.interaction import Reply, idempotency_key, run_command
from rapidwire.bot.paging import check_page, page_title
from rapidwire.bot.symbols import resolve_currency
from rapidwire.core import pools
from rapidwire.core.amounts import parse_amount
from rapidwire.core.currency import CurrencyInfo
from rapidwire.core.errors import InsufficientLiquidity, PoolNotFound
from rapidwire.core.paging import MAX_WINDOW
from rapidwire.db.tables import MINIMUM_LIQUIDITY

__all__ = ["build_group", "pool_info_embed", "setup"]

_POOL_NOT_FOUND: Final = "指定された通貨ペアと手数料の段階の流動性プールは見つかりませんでした。"
_CREATE_OVERRIDES: Final[Mapping[str, str]] = {
    InsufficientLiquidity.code: f"初回の預け入れ額が小さすぎます。発行されるシェア (2つの量の積の平方根) が {amount_text(MINIMUM_LIQUIDITY)} を超えるように預け入れてください。",
}
_ADD_OVERRIDES: Final[Mapping[str, str]] = {
    PoolNotFound.code: _POOL_NOT_FOUND,
    InsufficientLiquidity.code: "預け入れ額が小さすぎるため、シェアを発行できません。",
}
_REMOVE_OVERRIDES: Final[Mapping[str, str]] = {
    PoolNotFound.code: _POOL_NOT_FOUND,
    InsufficientLiquidity.code: "削除するシェアが少なすぎるため、受け取れる額が0になります。",
}


def pool_info_embed(
    *, pool_id: int, symbol_x: str, symbol_y: str, fee_bps: int, reserve_x: int, reserve_y: int, total_shares: int, description: str | None = None, extra: Iterable[Field] = ()
) -> discord.Embed:
    """The purple pool embed of the earlier bot (プールID, a reserve per currency inline, 合計シェア) with the fee tier, the pair in the order the user named it."""
    fields = [
        Field("プールID", f"`{pool_id}`"),
        Field("手数料", fee_tier_label(fee_bps)),
        Field(f"{symbol_x} リザーブ", amount_text(reserve_x), inline=True),
        Field(f"{symbol_y} リザーブ", amount_text(reserve_y), inline=True),
        Field("合計シェア", amount_text(total_shares)),
        *extra,
    ]
    return pool_embed(f"流動性プール情報: {symbol_x}/{symbol_y} ({fee_tier_label(fee_bps)})", description, fields=fields)


async def _pair(ctx: BotContext, interaction: discord.Interaction[Any], symbol_a: str, symbol_b: str) -> tuple[CurrencyInfo, CurrencyInfo]:
    first = await resolve_currency(ctx, interaction, symbol_a)
    second = await resolve_currency(ctx, interaction, symbol_b)
    if first.currency_id == second.currency_id:
        raise UserInputError("異なる2つの通貨を指定してください。")
    return first, second


def _xy(change: pools.LiquidityChange, x: CurrencyInfo) -> tuple[int, int]:
    """The pool's reserves after a change, in the (x, y) order of the command."""
    return (change.reserve_a, change.reserve_b) if x.currency_id == change.currency_a else (change.reserve_b, change.reserve_a)


async def _list(ctx: BotContext, interaction: discord.Interaction[Any], user: discord.User | discord.Member | None, page: int) -> Reply:
    target = user if user is not None else interaction.user
    # A page outside the pageable window reads page 1 only to learn the total for check_page's message.
    fetched = page if 1 <= page <= MAX_WINDOW // ITEMS_PER_PAGE else 1
    found = await pools.list_provider_positions(ctx.engine, target.id, page=fetched, per_page=ITEMS_PER_PAGE)
    pages = check_page(page, found.total)
    if found.total == 0:
        return Reply(embed=success_embed("流動性プール一覧", f"{target.display_name} は流動性を提供していません。"))
    fields = []
    for position in found.items:
        info = position.pool
        name = f"ID: {info.pool_id} | {info.symbol_a}/{info.symbol_b} ({fee_tier_label(info.fee_bps)})"
        value = (
            f"保有シェア: {amount_text(position.shares)} ({position.share_percent}%)\n"
            f"推定価値: {amount_text(position.withdrawable_a, info.symbol_a)} + {amount_text(position.withdrawable_b, info.symbol_b)}"
        )
        fields.append(Field(name, value))
    return Reply(embed=pool_embed(page_title(f"{target.display_name} の流動性プール一覧", page, pages), fields=fields))


def build_group() -> app_commands.Group:
    """A new /lp group."""
    group = app_commands.Group(name="lp", description="流動性プールに関連するコマンド")

    @group.command(name="create", description="新しい流動性プールを作成します。")
    @app_commands.describe(symbol_a="通貨Aのシンボル", amount_a="通貨Aの量", symbol_b="通貨Bのシンボル", amount_b="通貨Bの量", fee_tier="手数料の段階")
    @app_commands.choices(fee_tier=FEE_TIER_CHOICES)
    @app_commands.autocomplete(symbol_a=symbol_autocomplete, symbol_b=symbol_autocomplete)
    async def create(interaction: discord.Interaction[Any], symbol_a: str, amount_a: str, symbol_b: str, amount_b: str, fee_tier: int) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            x, y = await _pair(ctx, interaction, symbol_a, symbol_b)
            units_x = parse_amount(amount_a, allow_zero=False)
            units_y = parse_amount(amount_b, allow_zero=False)
            created = await pools.create_pool(
                ctx.engine,
                user_id=interaction.user.id,
                currency_x=x.currency_id,
                currency_y=y.currency_id,
                fee_bps=fee_tier,
                amount_x=units_x,
                amount_y=units_y,
                now=ctx.now(),
                idempotency_key=idempotency_key(interaction),
            )
            reserve_x, reserve_y = _xy(created, x)
            extra = [Field("受け取ったシェア", amount_text(created.shares)), Field("実行ID", f"`{created.execution_id}`")]
            embed = pool_info_embed(
                pool_id=created.pool_id,
                symbol_x=x.symbol,
                symbol_y=y.symbol,
                fee_bps=created.fee_bps,
                reserve_x=reserve_x,
                reserve_y=reserve_y,
                total_shares=created.total_shares,
                description="流動性プールを作成しました。",
                extra=extra,
            )
            return Reply(embed=embed)

        await run_command(interaction, work, overrides=_CREATE_OVERRIDES)

    @group.command(name="add", description="流動性プールに流動性を追加します。")
    @app_commands.describe(symbol_a="通貨Aのシンボル", amount_a="通貨Aの量 (上限)", symbol_b="通貨Bのシンボル", amount_b="通貨Bの量 (上限)", fee_tier="手数料の段階")
    @app_commands.choices(fee_tier=FEE_TIER_CHOICES)
    @app_commands.autocomplete(symbol_a=symbol_autocomplete, symbol_b=symbol_autocomplete)
    async def add(interaction: discord.Interaction[Any], symbol_a: str, amount_a: str, symbol_b: str, amount_b: str, fee_tier: int) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            x, y = await _pair(ctx, interaction, symbol_a, symbol_b)
            units_x = parse_amount(amount_a, allow_zero=False)
            units_y = parse_amount(amount_b, allow_zero=False)
            added = await pools.add_liquidity(
                ctx.engine,
                user_id=interaction.user.id,
                currency_x=x.currency_id,
                currency_y=y.currency_id,
                fee_bps=fee_tier,
                amount_x_desired=units_x,
                amount_y_desired=units_y,
                amount_x_min=0,
                amount_y_min=0,
                min_shares=1,
                now=ctx.now(),
                idempotency_key=idempotency_key(interaction),
            )
            fields = [
                Field("預け入れた額", f"{amount_text(added.amount_x, x.symbol)} + {amount_text(added.amount_y, y.symbol)}"),
                Field("保有シェア", amount_text(added.provider_shares), inline=True),
                Field("プールID", f"`{added.pool_id}`", inline=True),
                Field("実行ID", f"`{added.execution_id}`"),
            ]
            return Reply(embed=success_embed("流動性追加完了", f"{amount_text(added.shares)} シェアを受け取りました。", fields=fields))

        await run_command(interaction, work, overrides=_ADD_OVERRIDES)

    @group.command(name="remove", description="流動性プールから流動性を削除します。")
    @app_commands.describe(symbol_a="通貨Aのシンボル", symbol_b="通貨Bのシンボル", shares="削除するシェアの量", fee_tier="手数料の段階")
    @app_commands.choices(fee_tier=FEE_TIER_CHOICES)
    @app_commands.autocomplete(symbol_a=symbol_autocomplete, symbol_b=symbol_autocomplete)
    async def remove(interaction: discord.Interaction[Any], symbol_a: str, symbol_b: str, shares: str, fee_tier: int) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            x, y = await _pair(ctx, interaction, symbol_a, symbol_b)
            burned = parse_amount(shares, allow_zero=False)
            removed = await pools.remove_liquidity(
                ctx.engine,
                user_id=interaction.user.id,
                currency_x=x.currency_id,
                currency_y=y.currency_id,
                fee_bps=fee_tier,
                shares=burned,
                amount_x_min=0,
                amount_y_min=0,
                now=ctx.now(),
                idempotency_key=idempotency_key(interaction),
            )
            description = f"{amount_text(removed.amount_x, x.symbol)} と {amount_text(removed.amount_y, y.symbol)} を受け取りました。"
            fields = [
                Field("削除したシェア", amount_text(removed.shares), inline=True),
                Field("残りのシェア", amount_text(removed.provider_shares), inline=True),
                Field("プールID", f"`{removed.pool_id}`", inline=True),
                Field("実行ID", f"`{removed.execution_id}`"),
            ]
            return Reply(embed=success_embed("流動性削除完了", description, fields=fields))

        await run_command(interaction, work, overrides=_REMOVE_OVERRIDES)

    @group.command(name="info", description="通貨ペアの流動性プールの情報を手数料の段階ごとに表示します。")
    @app_commands.describe(symbol_a="通貨Aのシンボル", symbol_b="通貨Bのシンボル")
    @app_commands.autocomplete(symbol_a=symbol_autocomplete, symbol_b=symbol_autocomplete)
    async def info(interaction: discord.Interaction[Any], symbol_a: str, symbol_b: str) -> None:
        async def work() -> Reply:
            ctx = bot_context(interaction)
            x, y = await _pair(ctx, interaction, symbol_a, symbol_b)
            found = await pools.get_pools_for_pair(ctx.engine, x.currency_id, y.currency_id)
            if not found:
                raise UserInputError("指定された通貨ペアのプールは見つかりませんでした。")
            embeds = []
            for pool_info in found:
                x_is_a = pool_info.currency_a == x.currency_id
                embeds.append(
                    pool_info_embed(
                        pool_id=pool_info.pool_id,
                        symbol_x=x.symbol,
                        symbol_y=y.symbol,
                        fee_bps=pool_info.fee_bps,
                        reserve_x=pool_info.reserve_a if x_is_a else pool_info.reserve_b,
                        reserve_y=pool_info.reserve_b if x_is_a else pool_info.reserve_a,
                        total_shares=pool_info.total_shares,
                    )
                )
            return Reply(embeds=embeds)

        await run_command(interaction, work)

    @group.command(name="list", description="保有している流動性プールの一覧を表示します。")
    @app_commands.describe(user="対象のユーザー", page="ページ番号")
    async def list_(interaction: discord.Interaction[Any], user: discord.User | None = None, page: int = 1) -> None:
        await run_command(interaction, lambda: _list(bot_context(interaction), interaction, user, page))

    return group


def setup(bot: RapidWireBot) -> None:
    """Add the /lp group to bot.tree."""
    bot.tree.add_command(build_group())
