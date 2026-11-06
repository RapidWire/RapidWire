"""Stand-ins for discord.py objects in bot tests: an interaction that records everything sent through it, users, members, roles, guilds, channels and messages detailed enough for CacheGuildInfo and the guild directory sync, a client exposing a guild cache, and helpers that find a command in a tree and call its callback or an autocomplete callback, and press a view's button or a persistent DynamicItem."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord import app_commands

_ids = itertools.count(1_400_000_000_000_000_000)


def next_snowflake() -> int:
    """A fresh snowflake-sized ID, distinct from every other one handed out in the test session."""
    return next(_ids)


@dataclass
class Sent:
    """One thing sent through a FakeInteraction or a FakeChannel: via names the call (response.send_message, response.edit_message, followup.send, edit_original_response, channel.send, message.edit)."""

    via: str
    content: str | None = None
    embeds: list[discord.Embed] = field(default_factory=list)
    view: Any = None
    files: list[discord.File] = field(default_factory=list)
    ephemeral: bool = False

    @property
    def embed(self) -> discord.Embed:
        """The only embed, asserting that there is exactly one."""
        assert len(self.embeds) == 1, f"expected one embed, got {len(self.embeds)}"
        return self.embeds[0]


def _collect(embed: Any, embeds: Any) -> list[discord.Embed]:
    collected: list[discord.Embed] = []
    if embed is not None and embed is not discord.utils.MISSING:
        collected.append(embed)
    if embeds is not None and embeds is not discord.utils.MISSING:
        collected.extend(embeds)
    return collected


def _files(file: Any, files: Any) -> list[discord.File]:
    collected: list[discord.File] = []
    if file is not None and file is not discord.utils.MISSING:
        collected.append(file)
    if files is not None and files is not discord.utils.MISSING:
        collected.extend(files)
    return collected


def _given(value: Any) -> Any:
    return None if value is discord.utils.MISSING else value


# Discord's message, embed and autocomplete limits, which the fakes enforce with AssertionError where Discord answers 400 Bad Request.
CONTENT_MAX = 2000
EMBEDS_PER_MESSAGE_MAX = 10
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELDS_MAX = 25
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FOOTER_MAX = 2048
EMBED_AUTHOR_NAME_MAX = 256
EMBED_TOTAL_MAX = 6000
CHOICES_MAX = 25
CHOICE_NAME_MAX = 100
CHOICE_VALUE_MAX = 100


def _length(value: Any) -> int:
    return 0 if value is None else len(str(value))


def check_message(content: Any = None, embeds: Iterable[discord.Embed] = ()) -> None:
    """Assert that content and embeds fit Discord's limits on one message: content length, embed count, each embed's title, description, field count, field name and value, footer and author name, and the combined text of all embeds."""
    embeds = list(embeds)
    content = _given(content)
    assert _length(content) <= CONTENT_MAX, f"content has {_length(content)} characters, more than {CONTENT_MAX}"
    assert len(embeds) <= EMBEDS_PER_MESSAGE_MAX, f"{len(embeds)} embeds, more than {EMBEDS_PER_MESSAGE_MAX}"
    total = 0
    for embed in embeds:
        title, description = _length(embed.title), _length(embed.description)
        footer, author = _length(embed.footer.text), _length(embed.author.name)
        assert title <= EMBED_TITLE_MAX, f"embed title has {title} characters, more than {EMBED_TITLE_MAX}"
        assert description <= EMBED_DESCRIPTION_MAX, f"embed description has {description} characters, more than {EMBED_DESCRIPTION_MAX}"
        assert len(embed.fields) <= EMBED_FIELDS_MAX, f"embed has {len(embed.fields)} fields, more than {EMBED_FIELDS_MAX}"
        assert footer <= EMBED_FOOTER_MAX, f"embed footer has {footer} characters, more than {EMBED_FOOTER_MAX}"
        assert author <= EMBED_AUTHOR_NAME_MAX, f"embed author name has {author} characters, more than {EMBED_AUTHOR_NAME_MAX}"
        total += title + description + footer + author
        for item in embed.fields:
            name, value = _length(item.name), _length(item.value)
            assert name <= EMBED_FIELD_NAME_MAX, f"embed field name has {name} characters, more than {EMBED_FIELD_NAME_MAX}"
            assert value <= EMBED_FIELD_VALUE_MAX, f"embed field {item.name!r} value has {value} characters, more than {EMBED_FIELD_VALUE_MAX}"
            total += name + value
    assert total <= EMBED_TOTAL_MAX, f"the embeds hold {total} characters, more than {EMBED_TOTAL_MAX}"


def check_choices(choices: Iterable[app_commands.Choice[Any]]) -> list[app_commands.Choice[Any]]:
    """Assert that autocomplete choices fit Discord's limits (at most 25 choices, names of 1 to 100 characters, string values of at most 100 characters) and return them as a list."""
    collected = list(choices)
    assert len(collected) <= CHOICES_MAX, f"{len(collected)} autocomplete choices, more than {CHOICES_MAX}"
    for choice in collected:
        assert 1 <= len(choice.name) <= CHOICE_NAME_MAX, f"choice name {choice.name!r} has {len(choice.name)} characters, not 1 to {CHOICE_NAME_MAX}"
        if isinstance(choice.value, str):
            assert len(choice.value) <= CHOICE_VALUE_MAX, f"choice value {choice.value!r} has {len(choice.value)} characters, more than {CHOICE_VALUE_MAX}"
    return collected


class FakeUser:
    """A discord.User: id, name, display_name, global_name, mention and bot."""

    def __init__(self, user_id: int | None = None, *, name: str | None = None, bot: bool = False) -> None:
        self.id = next_snowflake() if user_id is None else user_id
        self.name = name if name is not None else f"user{self.id}"
        self.global_name: str | None = None
        self.bot = bot
        self.sent: list[Sent] = []

    @property
    def display_name(self) -> str:
        return self.global_name or self.name

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def send(self, content: str | None = None, *, embed: Any = None, embeds: Any = None, view: Any = None, file: Any = None, files: Any = None, **_: Any) -> FakeMessage:
        """A direct message to the user, recorded in sent."""
        entry = Sent("user.send", content, _collect(embed, embeds), view, _files(file, files))
        check_message(content, entry.embeds)
        self.sent.append(entry)
        return FakeMessage(content=content, embeds=entry.embeds, view=view, author=None)

    async def create_dm(self) -> FakeUser:
        return self

    def __str__(self) -> str:
        return self.name


class FakeRole:
    """A discord.Role: id, name, guild, permissions (a real discord.Permissions) and position; the @everyone role has the guild's ID."""

    def __init__(self, role_id: int, guild: FakeGuild, *, name: str | None = None, administrator: bool = False, position: int = 1) -> None:
        self.id = role_id
        self.guild = guild
        self.name = name if name is not None else f"role{role_id}"
        self.permissions = discord.Permissions(administrator=administrator)
        self.position = position

    def is_default(self) -> bool:
        return self.id == self.guild.id

    @property
    def mention(self) -> str:
        return "@everyone" if self.is_default() else f"<@&{self.id}>"


class FakeMember(FakeUser):
    """A discord.Member of a FakeGuild: roles lists @everyone first and then the member's own roles that still exist in the guild, as discord.py does."""

    def __init__(self, guild: FakeGuild, user_id: int | None = None, *, name: str | None = None, bot: bool = False, role_ids: Iterable[int] = ()) -> None:
        super().__init__(user_id, name=name, bot=bot)
        self.guild = guild
        self.role_ids: set[int] = set(role_ids) - {guild.id}
        self.nick: str | None = None

    @property
    def display_name(self) -> str:
        return self.nick or self.global_name or self.name

    @property
    def roles(self) -> list[FakeRole]:
        own = [self.guild.roles[role_id] for role_id in sorted(self.role_ids) if role_id in self.guild.roles]
        return [self.guild.default_role, *own]

    def get_role(self, role_id: int) -> FakeRole | None:
        if role_id == self.guild.id:
            return self.guild.default_role
        return self.guild.roles.get(role_id) if role_id in self.role_ids else None

    @property
    def guild_permissions(self) -> discord.Permissions:
        """Like discord.py: everything for the owner or an Administrator role, otherwise the union of the roles' permissions."""
        if self.guild.owner_id == self.id or any(role.permissions.administrator for role in self.roles):
            return discord.Permissions.all()
        value = 0
        for role in self.roles:
            value |= role.permissions.value
        return discord.Permissions(value)


class FakeChannel:
    """A text channel or DM channel: send() records into sent and returns a FakeMessage."""

    def __init__(self, channel_id: int | None = None, *, guild: FakeGuild | None = None, name: str = "general") -> None:
        self.id = next_snowflake() if channel_id is None else channel_id
        self.guild = guild
        self.name = name
        self.sent: list[Sent] = []

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, content: str | None = None, *, embed: Any = None, embeds: Any = None, view: Any = None, file: Any = None, files: Any = None, **_: Any) -> FakeMessage:
        entry = Sent("channel.send", content, _collect(embed, embeds), view, _files(file, files))
        check_message(content, entry.embeds)
        self.sent.append(entry)
        return FakeMessage(content=content, embeds=entry.embeds, view=view, channel=self)


class FakeGuild:
    """A discord.Guild in the client cache: owner_id, unavailable, chunked, the @everyone role (ID = guild ID), roles, members, channels and their lookups."""

    def __init__(self, guild_id: int | None = None, *, owner_id: int | None = None, name: str | None = None, unavailable: bool = False) -> None:
        self.id = next_snowflake() if guild_id is None else guild_id
        self.name = name if name is not None else f"guild{self.id}"
        self.owner_id = owner_id
        self.unavailable = unavailable
        self.chunked = True
        self.roles: dict[int, FakeRole] = {}
        self.members: dict[int, FakeMember] = {}
        self.channels: dict[int, FakeChannel] = {}
        self.default_role = FakeRole(self.id, self, name="@everyone", position=0)
        self.roles[self.id] = self.default_role

    @property
    def owner(self) -> FakeMember | None:
        return None if self.owner_id is None else self.members.get(self.owner_id)

    @property
    def member_count(self) -> int:
        return len(self.members)

    def add_role(self, role_id: int | None = None, *, administrator: bool = False, name: str | None = None) -> FakeRole:
        role = FakeRole(next_snowflake() if role_id is None else role_id, self, name=name, administrator=administrator, position=len(self.roles))
        self.roles[role.id] = role
        return role

    def remove_role(self, role_id: int) -> None:
        self.roles.pop(role_id, None)

    def add_member(self, user_id: int | None = None, *, role_ids: Iterable[int] = (), name: str | None = None, bot: bool = False) -> FakeMember:
        member = FakeMember(self, user_id, name=name, bot=bot, role_ids=role_ids)
        self.members[member.id] = member
        return member

    def remove_member(self, user_id: int) -> None:
        self.members.pop(user_id, None)

    def add_channel(self, channel_id: int | None = None, *, name: str = "general") -> FakeChannel:
        channel = FakeChannel(channel_id, guild=self, name=name)
        self.channels[channel.id] = channel
        return channel

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


class FakeMessage:
    """A sent message: content, embeds, view, author, channel; edit() updates it and records into edits."""

    def __init__(self, *, content: str | None = None, embeds: list[discord.Embed] | None = None, view: Any = None, author: Any = None, channel: Any = None, message_id: int | None = None) -> None:
        self.id = next_snowflake() if message_id is None else message_id
        self.content = content
        self.embeds = list(embeds or [])
        self.view = view
        self.author = author
        self.channel = channel
        self.guild = getattr(channel, "guild", None)
        self.mentions: list[Any] = []
        self.edits: list[Sent] = []
        self.replies: list[Sent] = []
        self.deleted = False

    async def edit(self, *, content: Any = discord.utils.MISSING, embed: Any = discord.utils.MISSING, embeds: Any = discord.utils.MISSING, view: Any = discord.utils.MISSING, **_: Any) -> FakeMessage:
        entry = Sent("message.edit", _given(content), _collect(embed, embeds), _given(view))
        check_message(content, entry.embeds)
        self.edits.append(entry)
        if content is not discord.utils.MISSING:
            self.content = content
        if embed is not discord.utils.MISSING or embeds is not discord.utils.MISSING:
            self.embeds = entry.embeds
        if view is not discord.utils.MISSING:
            self.view = view
        return self

    async def reply(self, content: str | None = None, *, embed: Any = None, embeds: Any = None, **_: Any) -> FakeMessage:
        entry = Sent("message.reply", content, _collect(embed, embeds))
        check_message(content, entry.embeds)
        self.replies.append(entry)
        return FakeMessage(content=content, embeds=entry.embeds, channel=self.channel)

    async def delete(self) -> None:
        self.deleted = True


class FakeResponse:
    """interaction.response: defer, send_message, edit_message, send_modal and autocomplete, each answering the interaction once like Discord does."""

    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self._done = False
        self.deferred: dict[str, bool] | None = None
        self.choices: list[app_commands.Choice[Any]] | None = None
        self.modal: Any = None

    def is_done(self) -> bool:
        return self._done

    def _answer(self) -> None:
        if self._done:
            raise discord.InteractionResponded(as_interaction(self._interaction))
        self._done = True

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._answer()
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(
        self, content: str | None = None, *, embed: Any = None, embeds: Any = None, view: Any = None, file: Any = None, files: Any = None, ephemeral: bool = False, **_: Any
    ) -> None:
        entry = Sent("response.send_message", content, _collect(embed, embeds), view, _files(file, files), ephemeral)
        check_message(content, entry.embeds)
        self._answer()
        self._interaction.sent.append(entry)
        self._interaction.original = FakeMessage(content=content, embeds=entry.embeds, view=view, channel=self._interaction.channel)

    async def edit_message(
        self,
        *,
        content: Any = discord.utils.MISSING,
        embed: Any = discord.utils.MISSING,
        embeds: Any = discord.utils.MISSING,
        view: Any = discord.utils.MISSING,
        attachments: Any = discord.utils.MISSING,
        **_: Any,
    ) -> None:
        entry = Sent("response.edit_message", _given(content), _collect(embed, embeds), _given(view), list(_given(attachments) or []))
        check_message(content, entry.embeds)
        self._answer()
        self._interaction.sent.append(entry)
        if self._interaction.message is not None:
            await self._interaction.message.edit(content=content, embed=embed, embeds=embeds, view=view)

    async def send_modal(self, modal: Any) -> None:
        self._answer()
        self.modal = modal

    async def autocomplete(self, choices: list[app_commands.Choice[Any]]) -> None:
        checked = check_choices(choices)
        self._answer()
        self.choices = checked


class FakeFollowup:
    """interaction.followup: send() records the message and returns a FakeMessage; the first followup after a thinking defer replaces the thinking message, so like on Discord it takes the defer's ephemeral flag and ignores its own, and becomes the original response."""

    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self._replaced_thinking = False

    async def send(
        self, content: str | None = None, *, embed: Any = None, embeds: Any = None, view: Any = None, file: Any = None, files: Any = None, ephemeral: bool = False, wait: bool = False, **_: Any
    ) -> FakeMessage:
        if not self._interaction.response.is_done():
            raise AssertionError("followup.send before the interaction was answered")
        entry = Sent("followup.send", content, _collect(embed, embeds), view, _files(file, files), ephemeral)
        check_message(content, entry.embeds)
        deferred = self._interaction.response.deferred
        if deferred is not None and deferred["thinking"] and not self._replaced_thinking:
            self._replaced_thinking = True
            entry.ephemeral = deferred["ephemeral"]
        self._interaction.sent.append(entry)
        message = FakeMessage(content=content, embeds=entry.embeds, view=view, channel=self._interaction.channel)
        if self._interaction.original is None:
            self._interaction.original = message
        return message


class FakeInteraction:
    """A discord.Interaction recording every response, followup and edit in sent, with user, guild, guild_id, channel, id, type, client, message (the message of a pressed component), data, namespace (the options typed so far, for autocomplete) and command.

    type is interaction_type, by default application_command; invoke(), autocomplete(), press() and press_dynamic() set it to the type of what they simulate.
    """

    def __init__(
        self,
        client: Any,
        *,
        user: FakeUser | None = None,
        guild: FakeGuild | None = None,
        channel: FakeChannel | None = None,
        interaction_id: int | None = None,
        message: FakeMessage | None = None,
        data: dict[str, Any] | None = None,
        namespace: dict[str, Any] | None = None,
        interaction_type: discord.InteractionType = discord.InteractionType.application_command,
    ) -> None:
        self.client = client
        self.id = next_snowflake() if interaction_id is None else interaction_id
        self.type = interaction_type
        self.guild = guild
        if user is None:
            user = guild.add_member() if guild is not None else FakeUser()
        self.user = user
        self.channel = channel if channel is not None else (guild.add_channel() if guild is not None else FakeChannel())
        self.message = message
        self.data = data if data is not None else {}
        self.namespace = SimpleNamespace(**(namespace or {}))
        self.command: Any = None
        self.locale = discord.Locale.japanese
        self.sent: list[Sent] = []
        self.original: FakeMessage | None = None
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    @property
    def guild_id(self) -> int | None:
        return None if self.guild is None else self.guild.id

    @property
    def channel_id(self) -> int | None:
        return None if self.channel is None else self.channel.id

    async def original_response(self) -> FakeMessage:
        if self.original is None:
            raise discord.ClientException("no original response")
        return self.original

    async def edit_original_response(
        self,
        *,
        content: Any = discord.utils.MISSING,
        embed: Any = discord.utils.MISSING,
        embeds: Any = discord.utils.MISSING,
        view: Any = discord.utils.MISSING,
        attachments: Any = discord.utils.MISSING,
        **_: Any,
    ) -> FakeMessage:
        if not self.response.is_done():
            raise AssertionError("edit_original_response before the interaction was answered")
        entry = Sent("edit_original_response", _given(content), _collect(embed, embeds), _given(view), list(_given(attachments) or []))
        check_message(content, entry.embeds)
        self.sent.append(entry)
        target = self.original if self.original is not None else self.message
        if target is None:
            target = self.original = FakeMessage(channel=self.channel)
        return await target.edit(content=content, embed=embed, embeds=embeds, view=view)

    async def delete_original_response(self) -> None:
        if self.original is not None:
            await self.original.delete()

    @property
    def last(self) -> Sent:
        """The last thing sent, asserting that something was."""
        assert self.sent, "nothing was sent"
        return self.sent[-1]

    @property
    def embeds(self) -> list[discord.Embed]:
        """Every embed sent, in order."""
        return [embed for entry in self.sent for embed in entry.embeds]


class FakeClient:
    """A discord.Client exposing a guild and user cache: get_guild, get_user, fetch_user (NotFound for unknown users), get_channel and user (the bot's own user)."""

    def __init__(self, *guilds: FakeGuild) -> None:
        self.guild_map: dict[int, FakeGuild] = {guild.id: guild for guild in guilds}
        self.users: dict[int, FakeUser] = {}
        self.user = FakeUser(name="RapidWire", bot=True)

    @property
    def guilds(self) -> list[FakeGuild]:
        return list(self.guild_map.values())

    def add_guild(self, guild: FakeGuild) -> FakeGuild:
        self.guild_map[guild.id] = guild
        return guild

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild_map.get(guild_id)

    def get_user(self, user_id: int) -> FakeUser | None:
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeUser:
        user = self.users.get(user_id)
        if user is None:
            response: Any = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(response, {"message": "Unknown User", "code": 10013})
        return user

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        for guild in self.guild_map.values():
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None


def as_interaction(fake: FakeInteraction) -> discord.Interaction[Any]:
    """The fake typed as a discord.Interaction, for calling bot functions directly in tests."""
    return cast("discord.Interaction[Any]", fake)


def find_command(tree: app_commands.CommandTree[Any], qualified_name: str) -> app_commands.Command[Any, ..., Any]:
    """The command at a qualified name such as "balance" or "currency mint", descending through groups; AssertionError when there is none."""
    parts = qualified_name.split()
    found: Any = tree.get_command(parts[0])
    for part in parts[1:]:
        assert isinstance(found, app_commands.Group), f"{found!r} is not a group"
        found = found.get_command(part)
    assert isinstance(found, app_commands.Command), f"no command {qualified_name!r}"
    return found


async def invoke(tree: app_commands.CommandTree[Any], qualified_name: str, interaction: FakeInteraction, **arguments: Any) -> FakeInteraction:
    """Call the callback of the command at qualified_name with interaction and keyword arguments (Python parameter names) as discord.py would (a Group subclass's method gets its group bound), skipping Discord's argument conversion; returns interaction for chaining."""
    command = find_command(tree, qualified_name)
    interaction.command = command
    interaction.type = discord.InteractionType.application_command
    callback: Any = command.callback
    if command.binding is not None:
        await callback(command.binding, interaction, **arguments)
    else:
        await callback(interaction, **arguments)
    return interaction


async def autocomplete(tree: app_commands.CommandTree[Any], qualified_name: str, parameter: str, interaction: FakeInteraction, current: str) -> list[app_commands.Choice[Any]]:
    """The choices the autocomplete callback of a command's parameter (Python name) returns for the typed text current, asserting that they fit Discord's limits; interaction.namespace holds the other options typed so far."""
    command = find_command(tree, qualified_name)
    interaction.command = command
    interaction.type = discord.InteractionType.autocomplete
    # The public Parameter.autocomplete is only a flag, so the callback is read from the command's internal parameter table.
    params = command._params
    assert parameter in params, f"{qualified_name} has no parameter {parameter!r}"
    callback: Any = params[parameter].autocomplete
    assert callback is not None, f"{qualified_name} {parameter} has no autocomplete"
    if getattr(callback, "pass_command_binding", False):
        choices = await callback(command.binding, interaction, current)
    else:
        choices = await callback(interaction, current)
    return check_choices(choices)


def buttons(view: discord.ui.View) -> list[discord.ui.Button[Any]]:
    """The buttons of a view in display order, including buttons wrapped by DynamicItems."""
    found: list[discord.ui.Button[Any]] = []
    for child in view.children:
        item = child.item if isinstance(child, discord.ui.DynamicItem) else child
        if isinstance(item, discord.ui.Button):
            found.append(item)
    return found


def _component_data(custom_id: str) -> dict[str, Any]:
    return {"custom_id": custom_id, "component_type": discord.ComponentType.button.value}


async def press(view: discord.ui.View, interaction: FakeInteraction, *, label: str | None = None, custom_id: str | None = None) -> FakeInteraction:
    """Press the one button of view matching label and/or custom_id as discord.py would: interaction.data gets the component's custom_id, the view's interaction_check runs, then the item's callback; exceptions propagate to the test instead of reaching View.on_error."""
    children: list[discord.ui.Item[Any]] = [child for child in view.children if isinstance(child, discord.ui.DynamicItem | discord.ui.Button)]
    matches = []
    for child in children:
        button = child.item if isinstance(child, discord.ui.DynamicItem) else child
        assert isinstance(button, discord.ui.Button)
        if (label is None or button.label == label) and (custom_id is None or button.custom_id == custom_id):
            matches.append((child, button))
    assert len(matches) == 1, f"expected one button matching label={label!r} custom_id={custom_id!r}, found {len(matches)}"
    item, button = matches[0]
    assert not button.disabled, "the button is disabled"
    interaction.type = discord.InteractionType.component
    if button.custom_id is not None:
        interaction.data = _component_data(button.custom_id)
    if not await view.interaction_check(as_interaction(interaction)):
        return interaction
    if isinstance(item, discord.ui.DynamicItem) and not await item.interaction_check(as_interaction(interaction)):
        return interaction
    await item.callback(as_interaction(interaction))
    return interaction


async def press_dynamic(item_classes: Iterable[type[discord.ui.DynamicItem[Any]]], custom_id: str, interaction: FakeInteraction) -> FakeInteraction:
    """Press a persistent button by custom_id as discord.py does after a restart: the first DynamicItem class (for example bot.dynamic_item_classes) whose template matches builds the item with from_custom_id, then interaction_check and callback run; AssertionError when no template matches."""
    interaction.data = _component_data(custom_id)
    interaction.type = discord.InteractionType.component
    for cls in item_classes:
        match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
        if match is None:
            continue
        base = discord.ui.Button[Any](custom_id=custom_id)
        item = await cls.from_custom_id(as_interaction(interaction), base, match)
        if await item.interaction_check(as_interaction(interaction)):
            await item.callback(as_interaction(interaction))
        return interaction
    raise AssertionError(f"no dynamic item matches {custom_id!r}")
