"""The bot's own GuildInfo, answering from the discord.py gateway cache without any network request, so permission checks in the bot use the bot's current view of each guild rather than the stored guild directory."""

from __future__ import annotations

import discord

__all__ = ["CacheGuildInfo"]


class CacheGuildInfo:
    """A GuildInfo over a client's gateway cache, with the semantics of the database guild directory: an unknown or unavailable guild has no owner, only cached members hold roles, every member holds @everyone (the guild's own ID), and a member is an administrator only through a role granting Administrator, so the owner is not implicitly one.

    The answers depend on the members intent and member chunking; a member not yet in the cache is answered as a non-member.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    def _guild(self, guild_id: int) -> discord.Guild | None:
        guild = self._client.get_guild(guild_id)
        if guild is None or guild.unavailable:
            return None
        return guild

    def _member(self, guild_id: int, user_id: int) -> tuple[discord.Guild, discord.Member] | None:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        member = guild.get_member(user_id)
        return None if member is None else (guild, member)

    async def guild_owner(self, guild_id: int) -> int | None:
        guild = self._guild(guild_id)
        return None if guild is None else guild.owner_id

    async def is_administrator(self, guild_id: int, user_id: int) -> bool:
        # Member.guild_permissions grants everything to the owner, so the roles are checked directly.
        found = self._member(guild_id, user_id)
        return found is not None and any(role.permissions.administrator for role in found[1].roles)

    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        found = self._member(guild_id, user_id)
        if found is None:
            return False
        guild, member = found
        return role_id == guild.id or any(role.id == role_id for role in member.roles)
