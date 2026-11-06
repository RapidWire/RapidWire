from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.autocomplete import AUTOCOMPLETE_TIMEOUT, MAX_CHOICES, symbol_autocomplete
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.context import BotContext
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.symbols import resolve_currency
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import create_currency
from tests.unit.bot.fakes import as_interaction

GUILD = 5_000_001
OTHER_GUILD = 5_000_002


async def _seed(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid")
    await create_currency(db, currency_id=5_000_003, symbol="XRP", name="Ripple")
    await create_currency(db, currency_id=5_000_004, symbol="ARP", name="Arp")
    await create_currency(db, currency_id=5_000_005, symbol="RPG", name="Game")
    await create_currency(db, currency_id=5_000_006, symbol="OLDRP", name="Old", deleted_at=datetime(2026, 1, 1, tzinfo=UTC))


async def test_symbol_is_uppercased_and_trimmed(db: AsyncEngine, bot_ctx: BotContext, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = as_interaction(make_interaction(guild_id=OTHER_GUILD))
    for typed in ("rapid", " Rapid ", "RAPID"):
        info = await resolve_currency(bot_ctx, interaction, typed)
        assert (info.currency_id, info.symbol) == (GUILD, "RAPID")


async def test_missing_symbol_means_the_guild_currency(db: AsyncEngine, bot_ctx: BotContext, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    for symbol in (None, "", "  "):
        info = await resolve_currency(bot_ctx, as_interaction(make_interaction(guild_id=GUILD)), symbol)
        assert info.currency_id == GUILD


@pytest.mark.parametrize(
    ("guild_id", "symbol", "message"),
    [
        (None, None, "サーバー内で実行するか、通貨のシンボルを指定してください。"),
        (OTHER_GUILD, None, "このサーバーには通貨がありません。通貨のシンボルを指定してください。"),
        (GUILD, "nope", "通貨 `NOPE` が見つかりませんでした。シンボルを正しく指定してください。"),
        (GUILD, "oldrp", "通貨 `OLDRP` が見つかりませんでした。シンボルを正しく指定してください。"),
        (GUILD, "`" * 3 + "X" * 40, "通貨 `" + "X" * 32 + "` が見つかりませんでした。シンボルを正しく指定してください。"),
    ],
)
async def test_resolution_errors(db: AsyncEngine, bot_ctx: BotContext, make_interaction: MakeInteraction, guild_id: int | None, symbol: str | None, message: str) -> None:
    await _seed(db)
    with pytest.raises(UserInputError) as caught:
        await resolve_currency(bot_ctx, as_interaction(make_interaction(guild_id=guild_id)), symbol)
    assert caught.value.message == message


async def test_deleted_guild_currency_is_absent(db: AsyncEngine, bot_ctx: BotContext, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    with pytest.raises(UserInputError):
        await resolve_currency(bot_ctx, as_interaction(make_interaction(guild_id=5_000_006)), None)


async def test_autocomplete_prefix_first_then_contains(db: AsyncEngine, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = as_interaction(make_interaction(guild_id=GUILD))
    choices = await symbol_autocomplete(interaction, "rp")
    assert [(choice.name, choice.value) for choice in choices] == [("RPG (Game)", "RPG"), ("ARP (Arp)", "ARP"), ("XRP (Ripple)", "XRP")]
    assert [choice.value for choice in await symbol_autocomplete(interaction, "")] == ["ARP", "RAPID", "RPG", "XRP"]
    assert await symbol_autocomplete(interaction, "zz") == []
    assert await symbol_autocomplete(interaction, "%") == []
    assert await symbol_autocomplete(interaction, "_") == []
    assert await symbol_autocomplete(interaction, "R" * 40) == []


async def test_autocomplete_is_capped(db: AsyncEngine, make_interaction: MakeInteraction) -> None:
    for i in range(MAX_CHOICES + 5):
        await create_currency(db, currency_id=6_000_000 + i, symbol=f"Q{chr(65 + i // 26)}{chr(65 + i % 26)}")
    choices = await symbol_autocomplete(as_interaction(make_interaction()), "q")
    assert len(choices) == MAX_CHOICES
    assert [choice.value for choice in choices] == sorted(choice.value for choice in choices)


async def test_autocomplete_failure_offers_nothing_and_is_logged(bot: RapidWireBot, make_interaction: MakeInteraction, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr("rapidwire.bot.autocomplete.search_currency_symbols", failing)
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    assert await symbol_autocomplete(as_interaction(make_interaction()), "R") == []
    assert any(record.getMessage() == "symbol autocomplete failed" and record.exc_info for record in caplog.records)


async def test_slow_autocomplete_offers_nothing_and_is_logged(bot: RapidWireBot, make_interaction: MakeInteraction, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr("rapidwire.bot.autocomplete.search_currency_symbols", slow)
    monkeypatch.setattr("rapidwire.bot.autocomplete.AUTOCOMPLETE_TIMEOUT", 0.05)
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    assert await asyncio.wait_for(symbol_autocomplete(as_interaction(make_interaction()), "R"), 5) == []
    [record] = [record for record in caplog.records if record.name == "rapidwire.bot"]
    assert (record.levelno, record.getMessage(), record.exc_info) == (logging.WARNING, "symbol autocomplete timed out", None)
    assert AUTOCOMPLETE_TIMEOUT == 2.5
