from __future__ import annotations

import asyncio
import logging
import os
import pkgutil
import signal
from typing import Any

import discord
import pytest
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot import app as app_module
from rapidwire.bot import commands as command_package
from rapidwire.bot import guild_sync, token_mention
from rapidwire.bot.app import SETUPS, build_bot, describe_commands
from rapidwire.bot.client import RapidWireBot, bot_context, bot_intents
from rapidwire.bot.errors import COMMAND_OUTDATED_MESSAGE
from rapidwire.bot.guild_info import CacheGuildInfo
from rapidwire.config import Settings
from rapidwire.core.clock import FixedClock
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.db.engine import create_engine
from tests.unit.bot.fakes import FakeInteraction


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, db_password="unit-test-password", **values)


def _bot(**kwargs: Any) -> RapidWireBot:
    return build_bot(_settings(), **kwargs)


def _bare_bot(settings: Settings | None = None, engine: AsyncEngine | None = None) -> RapidWireBot:
    """A RapidWireBot without the command and event modules, for testing the client's own mechanisms."""
    settings = settings if settings is not None else _settings()
    return RapidWireBot(settings, engine if engine is not None else create_engine(settings.database_url), guild_info=InMemoryGuildInfo())


def test_every_command_and_event_module_is_set_up() -> None:
    modules = {f"{command_package.__name__}.{info.name}" for info in pkgutil.iter_modules(command_package.__path__)}
    modules |= {guild_sync.__name__, token_mention.__name__}
    assert {setup.__module__ for setup in SETUPS} == modules
    assert len(SETUPS) == len(modules)


def test_build_bot_builds_the_context_without_connecting() -> None:
    settings = _settings(swap_chunk_count=7, max_gas=1000, default_gas=10)
    clock = FixedClock(discord.utils.utcnow())
    bot = build_bot(settings, clock=clock)
    ctx = bot.context
    assert ctx.settings is settings
    assert ctx.swap_config.chunk_count == 7
    assert ctx.runtime_config.max_gas == 1000
    assert isinstance(ctx.guild_info, CacheGuildInfo)
    assert ctx.now() == clock.now()
    assert bot.now() == clock.now()
    assert not bot.is_ready()
    assert bot.ws is None
    assert ctx.engine.pool.checkedout() == 0


def test_given_guild_info_and_no_clock() -> None:
    guild_info = InMemoryGuildInfo()
    bot = _bot(guild_info=guild_info)
    assert bot.context.guild_info is guild_info
    assert bot.context.now() is None


def test_intents() -> None:
    intents = bot_intents()
    assert intents.guilds and intents.members and intents.guild_messages and intents.dm_messages
    assert not intents.message_content
    assert not intents.presences
    assert _bot().intents == intents


def test_bot_context_of_an_interaction() -> None:
    bot = _bare_bot()
    assert bot_context(FakeInteraction(bot)) is bot.context  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bot_context(FakeInteraction(object()))  # type: ignore[arg-type]


async def test_listeners_receive_dispatched_events() -> None:
    bot = _bare_bot()
    received: list[tuple[str, Any]] = []
    done = asyncio.Event()

    async def first(message: Any) -> None:
        received.append(("first", message))

    async def second(message: Any) -> None:
        received.append(("second", message))
        done.set()

    bot.add_listener("message", first)
    bot.add_listener("on_message", second)
    assert bot.listeners("message") == (first, second)
    async with bot:
        bot.dispatch("message", "hello")
        await asyncio.wait_for(done.wait(), 5)
    assert sorted(received) == [("first", "hello"), ("second", "hello")]


async def test_listener_errors_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    bot = _bare_bot()
    done = asyncio.Event()

    async def failing(*args: Any) -> None:
        done.set()
        raise RuntimeError("listener failed")

    bot.add_listener("guild_join", failing)
    async with bot:
        bot.dispatch("guild_join", object())
        await asyncio.wait_for(done.wait(), 5)
        for _ in range(10):
            await asyncio.sleep(0)
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert record.event == "on_guild_join"  # type: ignore[attr-defined]
    assert record.exc_info is not None


def test_empty_event_name_is_rejected() -> None:
    async def handler() -> None:
        pass

    with pytest.raises(ValueError, match="event name"):
        _bot().add_listener("on_", handler)


async def test_setup_hook_registers_views_runs_hooks_and_syncs(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bare_bot()
    calls: list[str] = []

    class Persistent(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=None)
            self.add_item(discord.ui.Button(label="x", custom_id="rapidwire:test"))

    async def startup() -> None:
        calls.append("startup")

    async def fake_sync(*, guild: Any = None) -> list[Any]:
        calls.append("sync")
        return []

    bot.add_persistent_view(Persistent)
    bot.add_startup_hook(startup)
    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    async with bot:
        await bot.setup_hook()
        assert [type(view) for view in bot.persistent_views] == [Persistent]
    assert calls == ["startup", "sync"]


async def test_sync_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    bot = _bare_bot()

    async def failing_sync(*, guild: Any = None) -> list[Any]:
        raise discord.HTTPException(type("R", (), {"status": 500, "reason": "boom"})(), "boom")

    monkeypatch.setattr(bot.tree, "sync", failing_sync)
    async with bot:
        await bot.setup_hook()
    assert any(record.getMessage() == "could not sync the application commands" for record in caplog.records)


async def test_shutdown_hooks_run_once_and_failures_do_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bare_bot()
    calls: list[str] = []

    async def failing() -> None:
        calls.append("failing")
        raise RuntimeError("hook failed")

    async def second() -> None:
        calls.append("second")

    bot.add_shutdown_hook(failing)
    bot.add_shutdown_hook(second)
    async with bot:
        await bot.close()
    await bot.close()
    assert calls == ["failing", "second"]


async def test_concurrent_closes_wait_for_the_shutdown_hooks() -> None:
    bot = _bare_bot()
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow() -> None:
        calls.append("hook")
        await gate.wait()

    bot.add_shutdown_hook(slow)
    async with bot:
        first = asyncio.create_task(bot.close())
        second = asyncio.create_task(bot.close())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert not bot.is_closed()
        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert bot.is_closed()
    assert calls == ["hook"]


async def test_close_cancels_running_listener_tasks_after_the_hooks() -> None:
    bot = _bare_bot()
    events: list[str] = []
    started = asyncio.Event()

    async def endless(*args: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("listener cancelled")
            raise

    async def quick(*args: Any) -> None:
        pass

    async def hook() -> None:
        events.append("hook")

    bot.add_listener("guild_join", endless)
    bot.add_listener("guild_join", quick)
    bot.add_shutdown_hook(hook)
    async with bot:
        bot.dispatch("guild_join", object())
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(bot.listener_tasks) == 1
        await asyncio.wait_for(bot.close(), 5)
    assert events == ["hook", "listener cancelled"]
    assert bot.listener_tasks == frozenset()


async def _run_tree(bot: RapidWireBot, interaction: FakeInteraction) -> None:
    """Deliver interaction to the tree as discord.py does for an application command, then wait for the invoker task."""
    # The options are resolved against the connection state, which an interaction without resolved data never reads.
    interaction._state = None  # type: ignore[attr-defined]
    async with bot:
        bot.tree._from_interaction(interaction)  # type: ignore[arg-type]
        invokers = [task for task in asyncio.all_tasks() if task.get_name() == "CommandTree-invoker"]
        await asyncio.wait_for(asyncio.gather(*invokers), 5)


async def test_tree_errors_show_the_presenter_embed() -> None:
    bot = _bare_bot()

    def refuse(interaction: discord.Interaction[Any]) -> bool:
        return False

    @app_commands.command(name="guarded", description="g")
    @app_commands.check(refuse)
    async def guarded(interaction: discord.Interaction[Any]) -> None:
        raise AssertionError("the check failed")

    bot.tree.add_command(guarded)
    interaction = FakeInteraction(bot, data={"type": 1, "name": "guarded", "options": []})
    await _run_tree(bot, interaction)
    sent = interaction.last
    assert (sent.via, sent.ephemeral, sent.embed.title, sent.embed.description) == ("response.send_message", True, "エラー", "このコマンドを実行できません。")


async def test_tree_answers_an_unknown_command_as_outdated(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    bot = _bare_bot()
    interaction = FakeInteraction(bot, data={"type": 1, "name": "removed-command", "options": []})
    await _run_tree(bot, interaction)
    assert interaction.last.embed.description == COMMAND_OUTDATED_MESSAGE
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert record.interaction_id == interaction.id  # type: ignore[attr-defined]


async def test_serve_cancels_a_client_that_does_not_stop_and_disposes_the_engine_last(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="rapidwire.bot")
    events: list[str] = []
    started = asyncio.Event()

    async def stubborn_start(self: RapidWireBot, token: str, *, reconnect: bool = True) -> None:
        # Like discord.py sleeping in its reconnect backoff, it never looks at is_closed().
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("client cancelled")
            raise

    def build(settings: Settings, *, engine: Any = None, **kwargs: Any) -> RapidWireBot:
        bot = _bare_bot(settings, engine)

        async def hook() -> None:
            events.append("hook")

        bot.add_shutdown_hook(hook)
        return bot

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        events.append("dispose")

    monkeypatch.setattr(RapidWireBot, "start", stubborn_start)
    monkeypatch.setattr(app_module, "build_bot", build)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    serve = asyncio.create_task(app_module._serve(_settings(), "unit-test-token", stop_timeout=0.2))
    await asyncio.wait_for(started.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(serve, 5)
    assert events == ["hook", "client cancelled", "dispose"]
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    messages = [record.getMessage() for record in caplog.records if record.name == "rapidwire.bot"]
    assert "stopping bot" in messages
    assert "the discord client did not stop in time and is cancelled" in messages


async def test_serve_waits_for_a_client_that_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def polite_start(self: RapidWireBot, token: str, *, reconnect: bool = True) -> None:
        started.set()
        await stopped.wait()
        events.append("client returned")

    async def hook() -> None:
        stopped.set()

    def build(settings: Settings, *, engine: Any = None, **kwargs: Any) -> RapidWireBot:
        bot = _bare_bot(settings, engine)
        bot.add_shutdown_hook(hook)
        return bot

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        events.append("dispose")

    monkeypatch.setattr(RapidWireBot, "start", polite_start)
    monkeypatch.setattr(app_module, "build_bot", build)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    serve = asyncio.create_task(app_module._serve(_settings(), "unit-test-token"))
    await asyncio.wait_for(started.wait(), 5)
    os.kill(os.getpid(), signal.SIGINT)
    await asyncio.wait_for(serve, 5)
    assert events == ["client returned", "dispose"]
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_dynamic_items_are_recorded() -> None:
    class Item(discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"rapidwire:test:(?P<n>[0-9]+)"):
        def __init__(self, n: int) -> None:
            super().__init__(discord.ui.Button(label="x", custom_id=f"rapidwire:test:{n}"))

        @classmethod
        async def from_custom_id(cls, interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: Any, /) -> Item:
            return cls(int(match["n"]))

    bot = _bare_bot()
    bot.add_dynamic_items(Item)
    assert bot.dynamic_item_classes == (Item,)


def test_describe_commands() -> None:
    bot = _bare_bot()
    group = app_commands.Group(name="demo", description="d")

    @group.command(name="run", description="r")
    async def run(interaction: discord.Interaction[Any], amount: str, user: discord.User, page: int = 1) -> None:
        pass

    @app_commands.command(name="alpha", description="a")
    async def alpha(interaction: discord.Interaction[Any]) -> None:
        pass

    bot.tree.add_command(group)
    bot.tree.add_command(alpha)
    assert describe_commands(bot.tree) == ["/alpha", "/demo run amount:string:required user:user:required page:integer:optional"]
