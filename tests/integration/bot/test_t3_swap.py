from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.views import swap as swap_view
from rapidwire.bot.views.swap import CANCELED_MESSAGE, NOT_INVOKER_MESSAGE, PRICE_MOVED_MESSAGE, TIMED_OUT_MESSAGE, ZERO_OUTPUT_MESSAGE, SwapConfirmView, minimum_out
from rapidwire.config import Settings
from rapidwire.core import swap as swap_service
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.swap import SwapConfig, SwapHop, SwapQuote, SwapRoute
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import execution
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, create_pool, mint
from tests.unit.bot.fakes import FakeGuild, FakeInteraction, as_interaction, buttons, find_command, invoke, press

RAPID = 7_600_001
USD = 7_600_002
GOLD = 7_600_003
LONE = 7_600_004
ALICE = 7_700_001
BOB = 7_700_002
PROVIDER = 7_700_003
SWAP_IN = 50_000


async def _seed(db: AsyncEngine) -> None:
    for currency_id, symbol in ((RAPID, "RAPID"), (USD, "USD"), (GOLD, "GOLD"), (LONE, "LONE")):
        await create_currency(db, currency_id=currency_id, symbol=symbol)
    await create_pool(db, user_id=PROVIDER, currency_x=RAPID, currency_y=GOLD, amount_x=1_000_000, amount_y=2_000_000, fee_bps=100)
    await create_pool(db, user_id=PROVIDER, currency_x=RAPID, currency_y=USD, amount_x=1_000_000, amount_y=1_000_000, fee_bps=50)
    await create_pool(db, user_id=PROVIDER, currency_x=USD, currency_y=GOLD, amount_x=1_000_000, amount_y=2_000_000, fee_bps=10)
    await mint(db, RAPID, ALICE, 10 * SWAP_IN)
    await mint(db, RAPID, BOB, 100 * SWAP_IN)


async def _quote(
    bot: RapidWireBot, make_interaction: MakeInteraction, *, user_id: int = ALICE, from_symbol: str = "rapid", to_symbol: str = "GOLD", amount: str = "50"
) -> tuple[FakeInteraction, SwapConfirmView]:
    interaction = await invoke(bot.tree, "swap", make_interaction(user_id=user_id, guild_id=RAPID), from_symbol=from_symbol, to_symbol=to_symbol, amount=amount)
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    sent = interaction.last
    assert sent.via == "followup.send" and sent.ephemeral
    assert isinstance(sent.view, SwapConfirmView), sent.embeds[0].description
    return interaction, sent.view


async def _press(view: SwapConfirmView, origin: FakeInteraction, make_interaction: MakeInteraction, label: str, *, user_id: int = ALICE) -> FakeInteraction:
    return await press(view, make_interaction(user_id=user_id, guild_id=RAPID, message=origin.original), label=label)


async def _swaps(db: AsyncEngine) -> list[RowMapping]:
    async with db.connect() as conn:
        return list((await conn.execute(select(execution).where(execution.c.type == ExecutionType.SWAP).order_by(execution.c.execution_id))).mappings().all())


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {field.name or "": field.value or "" for field in embed.fields}


async def test_quote_then_confirm(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    embed = origin.last.embed
    assert embed.title == "スワップ確認"
    assert embed.colour == discord.Colour.blue()
    assert embed.footer.text == "このレートは30秒間有効です。"
    fields = _fields(embed)
    assert fields["スワップ元"] == "`50.000 RAPID`"
    assert fields["スワップ先 (推定)"] == f"`{view.estimate // 1000:,}.{view.estimate % 1000:03d} GOLD`"
    routes = [field for field in embed.fields if (field.name or "").startswith("ルート")]
    assert len(routes) == 2
    paths = sorted((field.value or "").split("経路: ")[1] for field in routes)
    assert paths == ["`RAPID -(0.5%)-> USD -(0.1%)-> GOLD`", "`RAPID -(1%)-> GOLD`"]
    assert [button.label for button in buttons(view)] == ["確定", "キャンセル"]
    assert await _swaps(db) == []

    pressed = await _press(view, origin, make_interaction, "確定")
    assert pressed.response.deferred == {"ephemeral": False, "thinking": False}
    result = pressed.last
    assert result.via == "edit_original_response"
    assert result.embed.title == "スワップ完了"
    assert result.view is view and all(button.disabled for button in buttons(view)) and view.is_finished()
    [row] = await _swaps(db)
    assert row["status"] == "success"
    assert (row["initiator_id"], row["idempotency_key"]) == (ALICE, f"discord:{origin.id}")
    assert row["input"]["min_out"] == str(minimum_out(view.estimate))
    received = int(row["output"]["amount_out"])
    assert received >= view.min_out
    assert result.embed.description == f"`50.000 RAPID` を `{received // 1000:,}.{received % 1000:03d} GOLD` にスワップしました。"
    assert _fields(result.embed)["実行ID"] == f"`{row['execution_id']}`"
    assert await balance_of(db, ALICE, GOLD) == received
    assert origin.original is not None and origin.original.embeds[0].title == "スワップ完了"


async def test_double_press_swaps_once(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    first, second = await asyncio.gather(_press(view, origin, make_interaction, "確定"), _press(view, origin, make_interaction, "確定"))
    rows = await _swaps(db)
    assert len(rows) == 1
    shown = {_fields(pressed.last.embed)["実行ID"] for pressed in (first, second)}
    assert shown == {f"`{rows[0]['execution_id']}`"}
    assert first.last.embed.title == second.last.embed.title == "スワップ完了"
    # A confirmation delivered again after the outcome replays the same swap.
    again = make_interaction(user_id=ALICE, guild_id=RAPID, message=origin.original)
    reply = await view._confirm(as_interaction(again))
    assert reply is not None and reply.embed is not None and _fields(reply.embed)["実行ID"] == f"`{rows[0]['execution_id']}`"
    assert len(await _swaps(db)) == 1
    assert await balance_of(db, ALICE, RAPID) == 9 * SWAP_IN


def _requote_returning(monkeypatch: pytest.MonkeyPatch, amount_out: Any) -> None:
    real = swap_service.quote_swap

    async def requote(*args: Any, **kwargs: Any) -> SwapQuote:
        quote = await real(*args, **kwargs)
        return dataclasses.replace(quote, amount_out=amount_out(quote))

    monkeypatch.setattr(swap_view, "quote_swap", requote)


async def test_requote_exactly_at_the_tolerance_proceeds(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    _requote_returning(monkeypatch, lambda quote: minimum_out(view.estimate))
    assert (await _press(view, origin, make_interaction, "確定")).last.embed.title == "スワップ完了"
    [row] = await _swaps(db)
    assert row["input"]["min_out"] == str(view.estimate * 99 // 100)


async def test_requote_one_unit_below_the_tolerance_aborts(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    _requote_returning(monkeypatch, lambda quote: minimum_out(view.estimate) - 1)
    result = (await _press(view, origin, make_interaction, "確定")).last
    assert (result.embed.title, result.embed.description) == ("エラー", PRICE_MOVED_MESSAGE)
    assert all(button.disabled for button in buttons(view)) and view.is_finished()
    assert await _swaps(db) == []


async def test_reserves_moved_before_confirm_abort(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    await swap_service.swap(db, user_id=BOB, from_currency=RAPID, to_currency=GOLD, amount_in=50 * SWAP_IN, min_out=0, config=bot.context.swap_config)
    result = (await _press(view, origin, make_interaction, "確定")).last
    assert result.embed.description == PRICE_MOVED_MESSAGE
    assert [row["initiator_id"] for row in await _swaps(db)] == [BOB]
    assert await balance_of(db, ALICE, RAPID) == 10 * SWAP_IN


async def test_reserves_moved_after_requote_hit_min_out(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    real = swap_service.quote_swap

    async def requote_then_front_run(*args: Any, **kwargs: Any) -> SwapQuote:
        quote = await real(*args, **kwargs)
        await swap_service.swap(db, user_id=BOB, from_currency=RAPID, to_currency=GOLD, amount_in=50 * SWAP_IN, min_out=0, config=bot.context.swap_config)
        return quote

    monkeypatch.setattr(swap_view, "quote_swap", requote_then_front_run)
    result = (await _press(view, origin, make_interaction, "確定")).last
    assert (result.embed.title, result.embed.description) == ("エラー", PRICE_MOVED_MESSAGE)
    rows = await _swaps(db)
    assert [(row["initiator_id"], row["status"]) for row in rows] == [(BOB, "success"), (ALICE, "failed")]
    assert rows[1]["input"]["min_out"] == str(view.min_out)
    assert _fields(result.embed)["実行ID"] == f"`{rows[1]['execution_id']}`"
    assert all(button.disabled for button in buttons(view))


async def test_insufficient_balance_is_shown_on_confirm(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction, amount="5000")
    result = (await _press(view, origin, make_interaction, "確定")).last
    assert (result.embed.title, result.embed.description) == ("エラー", "残高が不足しています。")
    assert "実行ID" in _fields(result.embed)
    assert all(button.disabled for button in buttons(view))


async def test_cancel_and_strangers(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    stranger = await _press(view, origin, make_interaction, "確定", user_id=BOB)
    assert (stranger.last.via, stranger.last.content, stranger.last.ephemeral) == ("response.send_message", NOT_INVOKER_MESSAGE, True)
    assert not view.is_finished()
    result = (await _press(view, origin, make_interaction, "キャンセル")).last
    assert (result.embed.title, result.embed.description) == ("エラー", CANCELED_MESSAGE)
    assert all(button.disabled for button in buttons(view))
    await view.on_timeout()
    assert origin.original is not None and origin.original.embeds[0].description == CANCELED_MESSAGE
    assert await _swaps(db) == []


async def test_timeout(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    await view.on_timeout()
    assert origin.last.via == "edit_original_response"
    assert origin.last.embed.description == TIMED_OUT_MESSAGE
    assert all(button.disabled for button in buttons(view))
    assert origin.original is not None and origin.original.embeds[0].description == TIMED_OUT_MESSAGE
    assert await _swaps(db) == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"from_symbol": "RAPID", "to_symbol": "LONE", "amount": "1"}, "スワップルートが見つかりませんでした。"),
        ({"from_symbol": "GOLD", "to_symbol": "RAPID", "amount": "0.001"}, ZERO_OUTPUT_MESSAGE),
        ({"from_symbol": "RAPID", "to_symbol": "rapid", "amount": "1"}, "スワップ元とスワップ先には異なる通貨を指定してください。"),
        ({"from_symbol": "RAPID", "to_symbol": "GOLD", "amount": "1.5e3"}, "金額は小数点以下 3 桁までの数値で指定してください (例: 12.5)。符号、指数表記、空白、先頭の余分な 0 は使えません。"),
        ({"from_symbol": "NOPE", "to_symbol": "GOLD", "amount": "1"}, "通貨 `NOPE` が見つかりませんでした。シンボルを正しく指定してください。"),
    ],
)
async def test_quote_errors_are_ephemeral(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, arguments: dict[str, str], message: str) -> None:
    await _seed(db)
    interaction = await invoke(bot.tree, "swap", make_interaction(user_id=ALICE, guild_id=RAPID), **arguments)
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.last.ephemeral
    assert (interaction.last.embed.title, interaction.last.embed.description) == ("エラー", message)


def _tiny_quote() -> SwapQuote:
    """A quote of 0.001 GOLD to RAPID claiming an output of one minimal unit, which the pools actually round down to zero."""
    route = SwapRoute(amount_in=1, amount_out=1, hops=(SwapHop(0, GOLD, RAPID, 100, 1, 1),))
    return SwapQuote(from_currency=GOLD, to_currency=RAPID, amount_in=1, amount_out=1, routes=(route,), truncated=False, symbols={GOLD: "GOLD", RAPID: "RAPID"})


async def test_zero_output_on_the_requote(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    view = SwapConfirmView(bot.context, as_interaction(make_interaction(user_id=ALICE, guild_id=RAPID)), _tiny_quote())
    reply = await view._confirm(as_interaction(make_interaction(user_id=ALICE, guild_id=RAPID)))
    assert reply is not None and reply.embed is not None
    assert (reply.embed.title, reply.embed.description) == ("エラー", ZERO_OUTPUT_MESSAGE)
    assert view.finished and await _swaps(db) == []


async def test_zero_output_on_the_swap_is_kept_on_replay(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    await mint(db, GOLD, ALICE, 1)
    quote = _tiny_quote()

    async def stale_quote(*args: Any, **kwargs: Any) -> SwapQuote:
        return quote

    monkeypatch.setattr(swap_view, "quote_swap", stale_quote)
    view = SwapConfirmView(bot.context, as_interaction(make_interaction(user_id=ALICE, guild_id=RAPID)), quote)
    reply = await view._confirm(as_interaction(make_interaction(user_id=ALICE, guild_id=RAPID)))
    assert reply is not None and reply.embed is not None
    assert (reply.embed.title, reply.embed.description) == ("エラー", ZERO_OUTPUT_MESSAGE)
    [row] = await _swaps(db)
    assert (row["status"], row["output"]["error"]["code"], row["output"]["zero_output"]) == ("failed", "no_route", True)
    assert _fields(reply.embed)["実行ID"] == f"`{row['execution_id']}`"
    # A confirmation delivered again replays the recorded failure with the same message.
    again = await view._confirm(as_interaction(make_interaction(user_id=ALICE, guild_id=RAPID)))
    assert again is not None and again.embed is not None and again.embed.description == ZERO_OUTPUT_MESSAGE
    assert len(await _swaps(db)) == 1


async def test_presses_are_serialised(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(db)
    origin, view = await _quote(bot, make_interaction)
    real = swap_service.swap
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def held_swap(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(swap_view, "swap", held_swap)
    first = asyncio.create_task(_press(view, origin, make_interaction, "確定"))
    await entered.wait()
    second = asyncio.create_task(_press(view, origin, make_interaction, "確定"))
    for _ in range(50):
        await asyncio.sleep(0)
    assert view._lock.locked() and not second.done() and calls == 1
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == 2
    [row] = await _swaps(db)
    assert {_fields(pressed.last.embed)["実行ID"] for pressed in results} == {f"`{row['execution_id']}`"}


async def test_swap_config_is_built_only_with_the_bot(db: AsyncEngine, bot_settings: Settings, guild_info: InMemoryGuildInfo, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = SwapConfig.from_settings.__func__  # type: ignore[attr-defined]

    def counting(cls: type[SwapConfig], settings: Settings) -> SwapConfig:
        nonlocal calls
        calls += 1
        return real(cls, settings)  # type: ignore[no-any-return]

    monkeypatch.setattr(SwapConfig, "from_settings", classmethod(counting))
    built = build_bot(bot_settings, engine=db, guild_info=guild_info)
    assert calls == 1
    await _seed(db)
    guild = FakeGuild(RAPID)

    def interaction(message: Any = None) -> FakeInteraction:
        return FakeInteraction(built, user=guild.get_member(ALICE) or guild.add_member(ALICE), guild=guild, channel=guild.add_channel(), message=message)

    origin = await invoke(built.tree, "swap", interaction(), from_symbol="RAPID", to_symbol="GOLD", amount="50")
    view = origin.last.view
    assert isinstance(view, SwapConfirmView)
    pressed = await press(view, interaction(origin.original), label="確定")
    assert pressed.last.embed.title == "スワップ完了"
    assert calls == 1


def test_parameters(bot: RapidWireBot) -> None:
    command = find_command(bot.tree, "swap")
    assert [(param.name, param.required, bool(param.autocomplete)) for param in command.parameters] == [("from_symbol", True, True), ("to_symbol", True, True), ("amount", True, False)]
