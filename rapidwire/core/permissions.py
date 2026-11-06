"""Guild management rules: services snapshot GuildInfo (the bot's current view of Discord) once per call before run_operation and decide inside the transaction on that frozen GuildSnapshot, so every deadlock retry decides the same way; holder operations such as transfers never consult it."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from rapidwire.core.errors import GuildUnavailable, PermissionDenied
from rapidwire.core.ledger import LedgerUsageError
from rapidwire.core.validation import is_guild_id, is_user_account_id

__all__ = [
    "GuildInfo",
    "GuildResolver",
    "GuildSnapshot",
    "InMemoryGuildInfo",
    "require_currency_manager",
    "require_guild_administrator",
    "require_guild_owner",
    "snapshot_guild",
]


class GuildInfo(Protocol):
    async def guild_owner(self, guild_id: int) -> int | None:
        """The owner's user ID, or None when the bot is not in the guild."""
        ...

    async def is_administrator(self, guild_id: int, user_id: int) -> bool:
        """Whether the user is a member with the Administrator permission; False when the bot is not in the guild or the user is not a member."""
        ...

    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        """Whether the member has the role; False when anything is unknown."""
        ...


class InMemoryGuildInfo:
    """A GuildInfo backed by dictionaries, for tests, answering as the database guild directory does: only members hold roles, every member holds the @everyone role (the guild's own ID), a member is an administrator through a role that grants Administrator (@everyone included), and the owner is neither implicitly a member nor an administrator.

    add_admin() makes a user a member holding a role that grants Administrator, standing for any such role, and add_role() makes the user a member too.
    """

    def __init__(self) -> None:
        self.owners: dict[int, int] = {}
        self.members: defaultdict[int, set[int]] = defaultdict(set)
        self.roles: defaultdict[tuple[int, int], set[int]] = defaultdict(set)
        self.administrator_roles: defaultdict[int, set[int]] = defaultdict(set)
        self.admins: defaultdict[int, set[int]] = defaultdict(set)

    def add_guild(self, guild_id: int, owner_id: int) -> None:
        self.owners[guild_id] = owner_id

    def remove_guild(self, guild_id: int) -> None:
        """Model the bot leaving the guild: it forgets the owner, the members, their roles and the administrator roles."""
        self.owners.pop(guild_id, None)
        for user_id in self.members.pop(guild_id, set()):
            self.roles.pop((guild_id, user_id), None)
        self.administrator_roles.pop(guild_id, None)
        self.admins.pop(guild_id, None)

    def set_owner(self, guild_id: int, owner_id: int) -> None:
        self.owners[guild_id] = owner_id

    def add_member(self, guild_id: int, user_id: int, role_ids: Iterable[int] = ()) -> None:
        """Make the user a member holding exactly role_ids (besides @everyone), as an upsert of the member does."""
        self.members[guild_id].add(user_id)
        self.roles[(guild_id, user_id)] = set(role_ids) - {guild_id}
        self.admins[guild_id].discard(user_id)

    def remove_member(self, guild_id: int, user_id: int) -> None:
        self.members[guild_id].discard(user_id)
        self.roles.pop((guild_id, user_id), None)
        self.admins[guild_id].discard(user_id)

    def set_role_administrator(self, guild_id: int, role_id: int, is_administrator: bool) -> None:
        """Record whether the role grants Administrator to every member holding it; the guild's own ID is the @everyone role."""
        (self.administrator_roles[guild_id].add if is_administrator else self.administrator_roles[guild_id].discard)(role_id)

    def add_admin(self, guild_id: int, user_id: int) -> None:
        self.members[guild_id].add(user_id)
        self.admins[guild_id].add(user_id)

    def remove_admin(self, guild_id: int, user_id: int) -> None:
        self.admins[guild_id].discard(user_id)

    def add_role(self, guild_id: int, user_id: int, role_id: int) -> None:
        self.members[guild_id].add(user_id)
        if role_id != guild_id:
            self.roles[(guild_id, user_id)].add(role_id)

    def remove_role(self, guild_id: int, user_id: int, role_id: int) -> None:
        self.roles[(guild_id, user_id)].discard(role_id)

    def delete_role(self, guild_id: int, role_id: int) -> None:
        """Model a role deleted from the guild: no member holds it and it grants nothing."""
        self.administrator_roles[guild_id].discard(role_id)
        for user_id in self.members.get(guild_id, set()):
            self.roles[(guild_id, user_id)].discard(role_id)

    def _is_member(self, guild_id: int, user_id: int) -> bool:
        return guild_id in self.owners and user_id in self.members.get(guild_id, set())

    async def guild_owner(self, guild_id: int) -> int | None:
        return self.owners.get(guild_id)

    async def is_administrator(self, guild_id: int, user_id: int) -> bool:
        if not self._is_member(guild_id, user_id):
            return False
        granting = self.administrator_roles.get(guild_id, set())
        held = self.roles.get((guild_id, user_id), set())
        return user_id in self.admins.get(guild_id, set()) or guild_id in granting or not granting.isdisjoint(held)

    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        return self._is_member(guild_id, user_id) and (role_id == guild_id or role_id in self.roles.get((guild_id, user_id), set()))


@dataclass(frozen=True)
class GuildSnapshot:
    """GuildInfo's answers for one (guild_id, user_id) pair: the owner (None when the bot is not in the guild) and whether the user has Administrator."""

    guild_id: int
    user_id: int
    owner_id: int | None
    administrator: bool

    @property
    def bot_in_guild(self) -> bool:
        return self.owner_id is not None

    @property
    def is_owner(self) -> bool:
        return self.owner_id is not None and self.owner_id == self.user_id

    def check_subject(self, guild_id: int, user_id: int) -> None:
        """Raise LedgerUsageError unless the snapshot was taken for this guild and user."""
        if (guild_id, user_id) != (self.guild_id, self.user_id):
            raise LedgerUsageError(f"the guild snapshot is for guild {self.guild_id} and user {self.user_id}, not guild {guild_id} and user {user_id}")


async def snapshot_guild(guild_info: GuildInfo, guild_id: int, user_id: int) -> GuildSnapshot:
    """Ask guild_info once for the owner of guild_id and, while the bot is in it, whether user_id has Administrator there; an invalid guild or user ID is not asked about and yields a snapshot without the bot."""
    if not is_guild_id(guild_id) or not is_user_account_id(user_id):
        return GuildSnapshot(guild_id=guild_id, user_id=user_id, owner_id=None, administrator=False)
    owner = await guild_info.guild_owner(guild_id)
    administrator = owner is not None and await guild_info.is_administrator(guild_id, user_id)
    return GuildSnapshot(guild_id=guild_id, user_id=user_id, owner_id=owner, administrator=administrator)


class GuildResolver:
    """A GuildSnapshot taken lazily by resolve(), which services pass to run_operation() as its before hook so an idempotent replay never consults GuildInfo."""

    def __init__(self, guild_info: GuildInfo, guild_id: int, user_id: int) -> None:
        self._guild_info = guild_info
        self._guild_id = guild_id
        self._user_id = user_id
        self._snapshot: GuildSnapshot | None = None

    async def resolve(self) -> None:
        """Take the snapshot unless it was already taken."""
        if self._snapshot is None:
            self._snapshot = await snapshot_guild(self._guild_info, self._guild_id, self._user_id)

    @property
    def snapshot(self) -> GuildSnapshot:
        """The snapshot taken by resolve(); reading it earlier raises LedgerUsageError."""
        if self._snapshot is None:
            raise LedgerUsageError("the guild snapshot was read before resolve()")
        return self._snapshot


def require_guild_owner(guild: GuildSnapshot, guild_id: int, user_id: int) -> None:
    """Pass when user_id owns the guild; raise GuildUnavailable when the bot is not in it, PermissionDenied otherwise."""
    guild.check_subject(guild_id, user_id)
    if not guild.bot_in_guild:
        raise GuildUnavailable()
    if not guild.is_owner:
        raise PermissionDenied("Only the server owner can perform this operation.")


def require_guild_administrator(guild: GuildSnapshot, guild_id: int, user_id: int, *, message: str) -> None:
    """Pass when user_id owns the guild or has Administrator there; raise GuildUnavailable when the bot is not in it, PermissionDenied with message otherwise."""
    guild.check_subject(guild_id, user_id)
    if not guild.bot_in_guild:
        raise GuildUnavailable()
    if not (guild.is_owner or guild.administrator):
        raise PermissionDenied(message)


def require_currency_manager(guild: GuildSnapshot, guild_id: int, user_id: int, *, delegation_enabled: bool) -> None:
    """Pass when user_id owns the guild, or has Administrator there while delegation is enabled; raise GuildUnavailable when the bot is not in the guild, PermissionDenied otherwise.

    delegation_enabled must come from the currency row locked FOR UPDATE in the same transaction, so a concurrent disable cannot slip between the check and the operation.
    """
    guild.check_subject(guild_id, user_id)
    if not guild.bot_in_guild:
        raise GuildUnavailable()
    if guild.is_owner or (delegation_enabled and guild.administrator):
        return
    raise PermissionDenied("Only the server owner, or an administrator while management is delegated, can manage this currency.")
