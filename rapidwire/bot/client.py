"""The bot client: a discord.Client holding the slash command tree and the BotContext, with registration points through which command, view and event modules extend it without editing this file."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Final

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.context import BotContext
from rapidwire.bot.guild_info import CacheGuildInfo
from rapidwire.bot.interaction import send_error
from rapidwire.config import Settings
from rapidwire.core.clock import Clock
from rapidwire.core.permissions import GuildInfo

__all__ = ["LISTENER_STOP_TIMEOUT", "EventHandler", "Hook", "RapidWireBot", "RapidWireTree", "ViewFactory", "bot_context", "bot_intents"]

log = logging.getLogger("rapidwire.bot")

# Seconds close() waits for cancelled listener tasks to finish.
LISTENER_STOP_TIMEOUT: Final = 5.0

# A gateway event listener, called with the event's arguments.
type EventHandler = Callable[..., Coroutine[Any, Any, None]]
# A coroutine run once at startup or at shutdown.
type Hook = Callable[[], Coroutine[Any, Any, None]]
# Builds a persistent view; it is called in setup_hook, because a view can only be created inside the running event loop.
type ViewFactory = Callable[[], discord.ui.View]

DynamicItemClass = type[discord.ui.DynamicItem[Any]]

# The bot never joins voice, so discord.py's warnings about missing voice libraries are switched off.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False


def bot_intents() -> discord.Intents:
    """The guilds, guild messages, direct messages and privileged members intents, the last because the guild directory and CacheGuildInfo need every member and its roles.
    The message content intent is not needed, because Discord delivers the content of messages that mention the bot, which is all the token mention reads.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.guild_messages = True
    intents.dm_messages = True
    return intents


class RapidWireTree(app_commands.CommandTree["RapidWireBot"]):
    """The command tree, whose error handler shows the presenter's embed for failures raised outside run_command(), such as failed checks and argument conversions."""

    async def on_error(self, interaction: discord.Interaction[RapidWireBot], error: app_commands.AppCommandError, /) -> None:
        await send_error(interaction, error)


class RapidWireBot(discord.Client):
    """The RapidWire Discord client: a discord.Client with an app_commands.CommandTree, which modules extend through tree.add_command(), add_listener(), add_dynamic_items(), add_persistent_view(), add_startup_hook() and add_shutdown_hook().
    setup_hook() registers the persistent views, runs the startup hooks and syncs the tree globally; close() runs the shutdown hooks and stops the listener tasks before disconnecting.
    """

    def __init__(self, settings: Settings, engine: AsyncEngine, *, guild_info: GuildInfo | None = None, clock: Clock | None = None) -> None:
        super().__init__(
            intents=bot_intents(),
            chunk_guilds_at_startup=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True),
        )
        self.context = BotContext.build(settings, engine, guild_info if guild_info is not None else CacheGuildInfo(self), clock=clock)
        self.tree = RapidWireTree(self)
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._dynamic_item_classes: list[DynamicItemClass] = []
        self._view_factories: list[ViewFactory] = []
        self._startup_hooks: list[Hook] = []
        self._shutdown_hooks: list[Hook] = []
        self._listener_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_task: asyncio.Task[None] | None = None

    def now(self) -> datetime | None:
        """The context's clock time, or None in production."""
        return self.context.now()

    @staticmethod
    def _event_name(event: str) -> str:
        name = event.removeprefix("on_")
        if not name:
            raise ValueError("an event name is required")
        return name

    def add_listener(self, event: str, handler: EventHandler) -> None:
        """Call handler with the arguments of every gateway event named event ("message", "guild_join" or "on_message"), as its own task beside every other listener; an exception it raises is logged."""
        self._event_handlers.setdefault(self._event_name(event), []).append(handler)

    def listeners(self, event: str) -> tuple[EventHandler, ...]:
        """The handlers added for event, in the order they were added."""
        return tuple(self._event_handlers.get(self._event_name(event), ()))

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        super().dispatch(event, *args, **kwargs)
        for handler in self._event_handlers.get(event, ()):
            task = self._schedule_event(handler, f"on_{event}", *args, **kwargs)
            self._listener_tasks.add(task)
            task.add_done_callback(self._listener_tasks.discard)

    @property
    def listener_tasks(self) -> frozenset[asyncio.Task[Any]]:
        """The listener tasks still running."""
        return frozenset(self._listener_tasks)

    def add_dynamic_items(self, *items: DynamicItemClass) -> None:
        """Register DynamicItem classes so their buttons keep working on messages sent before a restart."""
        super().add_dynamic_items(*items)
        self._dynamic_item_classes.extend(items)

    @property
    def dynamic_item_classes(self) -> tuple[DynamicItemClass, ...]:
        """The DynamicItem classes registered through add_dynamic_items()."""
        return tuple(self._dynamic_item_classes)

    def add_persistent_view(self, factory: ViewFactory) -> None:
        """Register a persistent view (no timeout, every item with a custom_id) that setup_hook() builds with factory and adds to the client."""
        self._view_factories.append(factory)

    def add_startup_hook(self, hook: Hook) -> None:
        """Run hook in setup_hook(), after login and before the gateway connects; an exception it raises stops the bot."""
        self._startup_hooks.append(hook)

    def add_shutdown_hook(self, hook: Hook) -> None:
        """Run hook once when the client closes, before the gateway disconnects; an exception it raises is logged and the other hooks still run."""
        self._shutdown_hooks.append(hook)

    async def setup_hook(self) -> None:
        for factory in self._view_factories:
            self.add_view(factory())
        for hook in self._startup_hooks:
            await hook()
        try:
            synced = await self.tree.sync()
        except discord.HTTPException:
            log.exception("could not sync the application commands")
        else:
            log.info("application commands synced", extra={"commands": len(synced)})

    async def _shut_down(self) -> None:
        for hook in self._shutdown_hooks:
            try:
                await hook()
            except Exception:
                log.exception("a bot shutdown hook failed", extra={"hook": getattr(hook, "__qualname__", repr(hook))})
        tasks = set(self._listener_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=LISTENER_STOP_TIMEOUT)
            if pending:
                log.warning("listener tasks did not stop after cancellation", extra={"pending": len(pending), "timeout_seconds": LISTENER_STOP_TIMEOUT})

    async def close(self) -> None:
        """Run the shutdown hooks once, then cancel the running listener tasks and wait up to LISTENER_STOP_TIMEOUT seconds for them, then disconnect; every concurrent call waits for the same shutdown before disconnecting."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shut_down(), name="bot shutdown")
        await asyncio.shield(self._shutdown_task)
        await super().close()

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        log.exception("unhandled error in a bot event handler", extra={"event": event_method})


def bot_context(interaction: discord.Interaction[Any]) -> BotContext:
    """The BotContext of the bot that received the interaction."""
    client = interaction.client
    if not isinstance(client, RapidWireBot):
        raise TypeError(f"the interaction belongs to {type(client).__name__}, not RapidWireBot")
    return client.context
