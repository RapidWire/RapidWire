"""The /notification group: allow, deny and list of claim notification permissions."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.commands.account import load_page
from rapidwire.bot.embeds import ITEMS_PER_PAGE, info_embed, resolve_user_label, success_embed
from rapidwire.bot.interaction import Reply, idempotency_key, run_command
from rapidwire.bot.paging import page_title
from rapidwire.core import errors as core_errors
from rapidwire.core import notifications

__all__ = ["notification_group", "setup"]

notification_group = app_commands.Group(name="notification", description="請求通知に関わるコマンド")

_SELF_OVERRIDES = {core_errors.InvalidArgument.code: "自分自身を対象にすることはできません。"}


async def _allow(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> Reply:
    ctx = bot_context(interaction)
    await notifications.allow_notifications(ctx.engine, user_id=interaction.user.id, allowed_user_id=user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    return Reply(embed=success_embed(description=f"{user.mention} から請求が来た際にDMで通知を送ります。"))


@notification_group.command(name="allow", description="指定したユーザーからの請求通知を許可します。")
@app_commands.describe(user="許可するユーザー")
async def notification_allow(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> None:
    await run_command(interaction, lambda: _allow(interaction, user), overrides=_SELF_OVERRIDES)


async def _deny(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> Reply:
    ctx = bot_context(interaction)
    await notifications.deny_notifications(ctx.engine, user_id=interaction.user.id, allowed_user_id=user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction))
    return Reply(embed=success_embed(description=f"{user.mention} からの請求通知をオフにしました。"))


@notification_group.command(name="deny", description="指定したユーザーからの請求通知を拒否します。")
@app_commands.describe(user="拒否するユーザー")
async def notification_deny(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> None:
    await run_command(interaction, lambda: _deny(interaction, user), overrides=_SELF_OVERRIDES)


async def _list(interaction: discord.Interaction[RapidWireBot], page: int) -> Reply:
    ctx = bot_context(interaction)
    result, pages = await load_page(page, ITEMS_PER_PAGE, lambda number: notifications.list_allowed(ctx.engine, interaction.user.id, page=number, per_page=ITEMS_PER_PAGE))
    if not result.items:
        return Reply(embed=success_embed(description="現在、請求通知を許可しているユーザーはいません。"))
    labels = await asyncio.gather(*(resolve_user_label(interaction.client, item.allowed_user_id) for item in result.items))
    return Reply(embed=info_embed(page_title("請求通知許可ユーザー一覧", page, pages), "\n".join(labels)))


@notification_group.command(name="list", description="請求通知を許可しているユーザーの一覧を表示します。")
@app_commands.describe(page="ページ番号")
async def notification_list(interaction: discord.Interaction[RapidWireBot], page: int = 1) -> None:
    await run_command(interaction, lambda: _list(interaction, page))


def setup(bot: RapidWireBot) -> None:
    """Add the /notification group to bot.tree."""
    bot.tree.add_command(notification_group)
