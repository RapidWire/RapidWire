"""The /discord-permission group: allow, deny and list of contract Discord operations in a guild, shown only to Administrators in Discord's command list and checked again by the core services against the bot's view of the guild (owner or Administrator)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import discord
from discord import app_commands

from rapidwire.bot.client import RapidWireBot, bot_context
from rapidwire.bot.commands.account import load_page
from rapidwire.bot.embeds import ITEMS_PER_PAGE, info_embed, resolve_user_label, success_embed
from rapidwire.bot.interaction import Reply, idempotency_key, require_guild_id, run_command
from rapidwire.bot.paging import page_title
from rapidwire.core import discord_permissions
from rapidwire.core.paging import Page

__all__ = ["discord_permission_group", "setup"]

discord_permission_group = app_commands.Group(
    name="discord-permission",
    description="[管理者] コントラクトによるDiscord操作権限を管理します。",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


async def _allow(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> Reply:
    ctx = bot_context(interaction)
    guild_id = require_guild_id(interaction)
    await discord_permissions.allow_discord(
        ctx.engine, ctx.guild_info, guild_id=guild_id, account_id=user.id, acting_user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction)
    )
    return Reply(embed=success_embed("権限付与成功", f"{user.mention} のコントラクトによるDiscord操作を許可しました。"))


@discord_permission_group.command(name="allow", description="指定したユーザーのコントラクトにDiscord操作を許可します。")
@app_commands.describe(user="許可するユーザー")
async def discord_permission_allow(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> None:
    await run_command(interaction, lambda: _allow(interaction, user))


async def _deny(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> Reply:
    ctx = bot_context(interaction)
    guild_id = require_guild_id(interaction)
    await discord_permissions.deny_discord(
        ctx.engine, ctx.guild_info, guild_id=guild_id, account_id=user.id, acting_user_id=interaction.user.id, now=ctx.now(), idempotency_key=idempotency_key(interaction)
    )
    return Reply(embed=success_embed("権限剥奪成功", f"{user.mention} のコントラクトによるDiscord操作を禁止しました。"))


@discord_permission_group.command(name="deny", description="指定したユーザーのコントラクトによるDiscord操作を禁止します。")
@app_commands.describe(user="禁止するユーザー")
async def discord_permission_deny(interaction: discord.Interaction[RapidWireBot], user: discord.User) -> None:
    await run_command(interaction, lambda: _deny(interaction, user))


async def _list(interaction: discord.Interaction[RapidWireBot], page: int) -> Reply:
    ctx = bot_context(interaction)
    guild_id = require_guild_id(interaction)

    def fetch(number: int) -> Awaitable[Page[discord_permissions.DiscordPermission]]:
        return discord_permissions.list_discord_permissions_as(ctx.engine, ctx.guild_info, guild_id=guild_id, user_id=interaction.user.id, page=number, per_page=ITEMS_PER_PAGE)

    result, pages = await load_page(page, ITEMS_PER_PAGE, fetch)
    if not result.items:
        return Reply(embed=success_embed("権限一覧", "現在、Discord操作を許可されているユーザーはいません。"))
    labels = await asyncio.gather(*(resolve_user_label(interaction.client, item.account_id) for item in result.items))
    return Reply(embed=info_embed(page_title("Discord操作許可ユーザー一覧", page, pages), "\n".join(labels)))


@discord_permission_group.command(name="list", description="Discord操作を許可されているユーザーの一覧を表示します。")
@app_commands.describe(page="ページ番号")
async def discord_permission_list(interaction: discord.Interaction[RapidWireBot], page: int = 1) -> None:
    await run_command(interaction, lambda: _list(interaction, page))


def setup(bot: RapidWireBot) -> None:
    """Add the /discord-permission group to bot.tree."""
    bot.tree.add_command(discord_permission_group)
