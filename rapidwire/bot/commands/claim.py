"""The /claim group (create, list, pay, cancel) and the registration of the claim notification buttons."""

from __future__ import annotations

import discord
from discord import app_commands

from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.commands.account import load_page
from rapidwire.bot.embeds import ITEMS_PER_PAGE, Field, account_mention, info_embed, plain_text, success_embed
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.interaction import Reply, idempotency_key, require_guild_id, run_command
from rapidwire.bot.paging import page_title
from rapidwire.bot.views.claim import CANCEL_OVERRIDES, CLAIM_BUTTON_CLASSES, PAY_OVERRIDES, canceled_embed, pay_reply
from rapidwire.core import claims
from rapidwire.core import errors as core_errors
from rapidwire.core.amounts import format_amount_display, parse_amount
from rapidwire.core.currency import get_currency
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.paging import Page
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX

__all__ = ["claim_group", "setup"]

claim_group = app_commands.Group(name="claim", description="請求に関連するコマンド")


async def _create(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, description: str | None) -> Reply:
    ctx = bot_context(interaction)
    guild_id = require_guild_id(interaction)
    units = parse_amount(amount, allow_zero=False)
    if description is not None and len(description) > CLAIM_DESCRIPTION_MAX:
        raise UserInputError(f"請求の説明は {CLAIM_DESCRIPTION_MAX} 文字以内で指定してください。")
    info = await get_currency(ctx.engine, guild_id, now=ctx.now())
    if info is None:
        raise UserInputError("このサーバーには通貨が存在しません。")
    result = await claims.create_claim(
        ctx.engine,
        claimant_id=interaction.user.id,
        payer_id=user.id,
        currency_id=info.currency_id,
        amount=units,
        description=description,
        now=ctx.now(),
        idempotency_key=idempotency_key(interaction),
    )
    return Reply(embed=success_embed("請求作成完了", f"{user.mention} への請求を作成しました。", fields=[Field("請求ID", f"`{result.claim_id}`")]))


@claim_group.command(name="create", description="ユーザーに通貨を請求します。")
@app_commands.describe(user="請求先のユーザー", amount="請求額", description="請求の説明 (50 文字まで)")
async def claim_create(interaction: discord.Interaction[RapidWireBot], user: discord.User, amount: str, description: app_commands.Range[str, None, CLAIM_DESCRIPTION_MAX] | None = None) -> None:
    overrides = {core_errors.InvalidArgument.code: "自分自身に請求することはできません。"}
    await run_command(interaction, lambda: _create(interaction, user, amount, description), overrides=overrides)


ROLE_CHOICES = (app_commands.Choice(name="請求者として", value=claims.ClaimRole.CLAIMANT.value), app_commands.Choice(name="支払者として", value=claims.ClaimRole.PAYER.value))


async def _list(interaction: discord.Interaction[RapidWireBot], page: int, role: claims.ClaimRole | None) -> Reply:
    ctx = bot_context(interaction)
    symbols: dict[int, str] = {}

    async def fetch(number: int) -> Page[claims.Claim]:
        async with consistent_snapshot(ctx.engine) as conn:
            found = await claims.list_claims_for_user(conn, interaction.user.id, page=number, per_page=ITEMS_PER_PAGE, role=role)
            for currency_id in sorted({item.currency_id for item in found.items} - symbols.keys()):
                info = await get_currency(conn, currency_id, include_deleted=True)
                symbols[currency_id] = info.symbol if info is not None else "???"
        return found

    result, pages = await load_page(page, ITEMS_PER_PAGE, fetch)
    if not result.items:
        return Reply(embed=success_embed("請求一覧", "関連する請求はありません。"))
    fields = []
    for item in result.items:
        name = f"ID: {item.claim_id} | {item.status.value.upper()} | {format_amount_display(item.amount)} {symbols[item.currency_id]}"
        description = plain_text(item.description) if item.description is not None else "N/A"
        fields.append(Field(name, f"請求者: {account_mention(item.claimant_id)}\n支払者: {account_mention(item.payer_id)}\n説明: {description}"))
    return Reply(embed=info_embed(page_title("請求一覧", page, pages), fields=fields))


@claim_group.command(name="list", description="あなたが関与する請求を一覧表示します。")
@app_commands.describe(page="ページ番号", role="自分が請求者の請求だけ、または支払者の請求だけを表示します (省略時は両方)")
@app_commands.choices(role=list(ROLE_CHOICES))
async def claim_list(interaction: discord.Interaction[RapidWireBot], page: int = 1, role: app_commands.Choice[str] | None = None) -> None:
    side = None if role is None else claims.ClaimRole(role.value)
    await run_command(interaction, lambda: _list(interaction, page, side))


@claim_group.command(name="pay", description="あなた宛の請求を支払います。")
@app_commands.describe(claim_id="支払う請求のID")
async def claim_pay(interaction: discord.Interaction[RapidWireBot], claim_id: int) -> None:
    await run_command(interaction, lambda: pay_reply(interaction, claim_id, idempotency_key(interaction)), overrides=PAY_OVERRIDES)


async def _cancel(interaction: discord.Interaction[RapidWireBot], claim_id: int) -> Reply:
    ctx = bot_context(interaction)
    result = await claims.cancel_claim(ctx.engine, claim_id=claim_id, user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    return Reply(embed=canceled_embed(result.claim_id))


@claim_group.command(name="cancel", description="未払いの請求をキャンセルします。")
@app_commands.describe(claim_id="キャンセルする請求のID")
async def claim_cancel(interaction: discord.Interaction[RapidWireBot], claim_id: int) -> None:
    await run_command(interaction, lambda: _cancel(interaction, claim_id), overrides=CANCEL_OVERRIDES)


def setup(bot: RapidWireBot) -> None:
    """Add the /claim group to bot.tree and register the claim notification buttons as persistent dynamic items."""
    bot.tree.add_command(claim_group)
    bot.add_dynamic_items(*CLAIM_BUTTON_CLASSES)
