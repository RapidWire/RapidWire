"""Building and running the bot: build_bot() assembles the client, its context and the full command tree without connecting to Discord or the database, and run_bot() runs it until SIGINT or SIGTERM."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any, Final

from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot import guild_sync, token_mention
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.commands import account, approve, claim, contract, currency, discord_permission, lp, notification, stake, swap
from rapidwire.config import Settings
from rapidwire.core.clock import Clock
from rapidwire.core.permissions import GuildInfo
from rapidwire.db.engine import create_engine
from rapidwire.signals import stop_signals

__all__ = ["RUNNER_STOP_TIMEOUT", "SETUPS", "build_bot", "describe_commands", "run_bot"]

log = logging.getLogger("rapidwire.bot")

# Seconds the closed client's start() may take to return after a stop signal before it is cancelled, even while discord.py sleeps in its reconnect backoff.
# With the shutdown hooks (the guild directory drain and the wait for token revocations) and the listener stop before it, the engine is disposed within the bot service's stop_grace_period of 30 seconds in compose.yaml.
RUNNER_STOP_TIMEOUT: Final = 5.0

# The setup(bot) of every command and event module, in the order they extend the bot.
SETUPS: Final[tuple[Callable[[RapidWireBot], None], ...]] = (
    account.setup,
    currency.setup,
    stake.setup,
    approve.setup,
    contract.setup,
    claim.setup,
    lp.setup,
    swap.setup,
    notification.setup,
    discord_permission.setup,
    guild_sync.setup,
    token_mention.setup,
)


def build_bot(settings: Settings, *, engine: AsyncEngine | None = None, guild_info: GuildInfo | None = None, clock: Clock | None = None) -> RapidWireBot:
    """The bot with every module set up; engine defaults to a new engine from settings (it connects on first use, and the caller disposes it), guild_info to the bot's CacheGuildInfo, and clock to none."""
    db = create_engine(settings.database_url) if engine is None else engine
    bot = RapidWireBot(settings, db, guild_info=guild_info, clock=clock)
    for setup in SETUPS:
        setup(bot)
    return bot


def _walk(commands: Iterable[Any]) -> Iterable[app_commands.Command[Any, ..., Any] | app_commands.ContextMenu]:
    for command in sorted(commands, key=lambda item: item.name):
        if isinstance(command, app_commands.Group):
            yield from _walk(command.commands)
        else:
            yield command


def describe_commands(tree: app_commands.CommandTree[Any]) -> list[str]:
    """One line per invocable global command: /qualified name, then each parameter as name:type:required or name:type:optional; context menus as [menu type] name."""
    lines: list[str] = []
    for command in _walk(tree.get_commands()):
        if isinstance(command, app_commands.ContextMenu):
            lines.append(f"[{command.type.name}] {command.name}")
            continue
        params = "".join(f" {param.name}:{param.type.name}:{'required' if param.required else 'optional'}" for param in command.parameters)
        lines.append(f"/{command.qualified_name}{params}")
    return lines


async def _stop_runner(runner: asyncio.Task[None], grace: float) -> None:
    """Wait up to grace seconds for the closed client's start() to return, then cancel it; an exception it raised still propagates."""
    done, _ = await asyncio.wait({runner}, timeout=grace)
    if not done:
        log.warning("the discord client did not stop in time and is cancelled", extra={"timeout_seconds": grace})
        runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if not runner.cancelled() or (current is not None and current.cancelling()):
            raise


async def _serve(settings: Settings, token: str, *, stop_timeout: float = RUNNER_STOP_TIMEOUT) -> None:
    engine = create_engine(settings.database_url)
    stop = asyncio.Event()
    try:
        with stop_signals(stop.set):
            bot = build_bot(settings, engine=engine)
            async with bot:
                runner = asyncio.create_task(bot.start(token), name="discord client")
                waiter = asyncio.create_task(stop.wait(), name="stop signal")
                done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                if runner in done:
                    await runner
                else:
                    log.info("stopping bot")
                    await bot.close()
                    await _stop_runner(runner, stop_timeout)
    finally:
        await engine.dispose()


def run_bot(settings: Settings) -> None:
    """Connect to Discord with the configured token and run until SIGINT or SIGTERM closes the client gracefully; discord.py installs no log handler of its own, so its records go through the application's handler, and the engine is disposed on exit."""
    token = settings.require_discord_token()
    log.info("starting bot")
    asyncio.run(_serve(settings, token))
    log.info("bot stopped")
