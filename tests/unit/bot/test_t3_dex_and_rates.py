from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from rapidwire.bot.commands.currency import RATE_PPM_PER_PERCENT, format_rate, parse_rate_percent
from rapidwire.bot.context import BotContext
from rapidwire.bot.dex_format import FEE_TIER_CHOICES, fee_tier_label
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.views.swap import (
    CANCELED_MESSAGE,
    MAX_ROUTE_FIELDS,
    NOT_INVOKER_MESSAGE,
    SWAP_TIMEOUT_SECONDS,
    TIMED_OUT_MESSAGE,
    SwapConfirmView,
    minimum_out,
    quote_embed,
    route_path,
)
from rapidwire.core.swap import SwapHop, SwapQuote, SwapRoute
from rapidwire.db.tables import FEE_TIERS_BPS, RATE_PPM_MAX
from tests.unit.bot.fakes import FakeClient, FakeInteraction, FakeMessage, FakeUser, buttons, press

SYMBOLS = {1: "RAPID", 2: "USD", 3: "GOLD", 4: "SILVER-X"}


@pytest.mark.parametrize(
    ("text", "ppm"),
    [("0", 0), ("0.0001", 1), ("1", RATE_PPM_PER_PERCENT), ("0.5", 5000), ("12.3456", 123456), ("100", RATE_PPM_MAX), ("100.0000", RATE_PPM_MAX), ("99.9999", RATE_PPM_MAX - 1)],
)
def test_rate_parsing_is_exact(text: str, ppm: int) -> None:
    assert parse_rate_percent(text) == ppm


@pytest.mark.parametrize("text", ["", "100.0001", "101", "0.00001", "-1", "+1", "1e2", " 1", "1 ", "01", ".5", "5.", "1,5", "nan", "inf", "0x10", chr(0xFF11), "1" * 20])
def test_rate_parsing_rejects(text: str) -> None:
    with pytest.raises(UserInputError) as caught:
        parse_rate_percent(text)
    assert "利率は 0 から 100 までの数値" in caught.value.message


def test_rate_formatting() -> None:
    assert [format_rate(ppm) for ppm in (0, 1, 5000, 123456, RATE_PPM_MAX)] == ["0.0000%/h", "0.0001%/h", "0.5000%/h", "12.3456%/h", "100.0000%/h"]
    assert format_rate(25000, unit="%/hour") == "2.5000%/hour"


def test_fee_tiers() -> None:
    assert [fee_tier_label(tier) for tier in (100, 50, 10, 30, 5)] == ["1%", "0.5%", "0.1%", "0.3%", "0.05%"]
    assert [(choice.name, choice.value) for choice in FEE_TIER_CHOICES] == [(fee_tier_label(tier), tier) for tier in FEE_TIERS_BPS]
    assert {choice.value for choice in FEE_TIER_CHOICES} == {100, 50, 10}


def test_minimum_out_boundary() -> None:
    assert minimum_out(1000) == 990
    assert minimum_out(101) == 99
    assert minimum_out(1) == 0
    assert minimum_out(10**37) == 99 * 10**35


def _route(amount_in: int, amount_out: int, path: tuple[int, ...], fees: tuple[int, ...], pool_base: int = 0) -> SwapRoute:
    hops = tuple(SwapHop(pool_base + index, path[index], path[index + 1], fees[index], amount_in, amount_out) for index in range(len(fees)))
    return SwapRoute(amount_in=amount_in, amount_out=amount_out, hops=hops)


def _quote(routes: tuple[SwapRoute, ...], *, truncated: bool = False) -> SwapQuote:
    return SwapQuote(
        from_currency=1,
        to_currency=3,
        amount_in=sum(route.amount_in for route in routes),
        amount_out=sum(route.amount_out for route in routes),
        routes=routes,
        truncated=truncated,
        symbols=SYMBOLS,
    )


def test_route_path_shows_symbols_and_tiers() -> None:
    assert route_path(_route(1, 1, (1, 2, 4, 3), (100, 50, 10)), SYMBOLS) == "RAPID -(1%)-> USD -(0.5%)-> SILVER-X -(0.1%)-> GOLD"


def test_single_route_embed() -> None:
    embed = quote_embed(_quote((_route(1_234_500, 1_000_000, (1, 3), (100,)),)))
    assert embed.title == "スワップ確認"
    assert embed.colour == discord.Colour.blue()
    assert embed.description is None
    assert [(field.name, field.value) for field in embed.fields] == [
        ("スワップ元", "`1,234.500 RAPID`"),
        ("スワップ先 (推定)", "`1,000.000 GOLD`"),
        ("ルート", "入力: `1,234.500 RAPID`\n受取: `1,000.000 GOLD`\n経路: `RAPID -(1%)-> GOLD`"),
    ]
    assert embed.footer.text == f"このレートは{SWAP_TIMEOUT_SECONDS}秒間有効です。" == "このレートは30秒間有効です。"


def test_many_routes_are_summarised_within_discord_limits() -> None:
    big = 10**37 - 1
    routes = tuple(_route(big // 40, big // 41, (1, 2, 4, 2, 4, 3), (100, 50, 10, 100, 50), pool_base=10 * index) for index in range(40))
    embed = quote_embed(_quote(routes, truncated=True))
    names = [field.name for field in embed.fields]
    assert names[:2] == ["スワップ元", "スワップ先 (推定)"]
    assert names[2:-1] == [f"ルート {number}" for number in range(1, MAX_ROUTE_FIELDS + 1)]
    assert names[-1] == f"その他の {40 - MAX_ROUTE_FIELDS} ルート"
    rest = routes[MAX_ROUTE_FIELDS:]
    assert embed.fields[-1].value is not None and f"{sum(route.amount_in for route in rest) // 1000:,}" in embed.fields[-1].value
    assert embed.description is not None and "40 個のルート" in embed.description and "より良いルート" in embed.description
    assert len(embed.fields) <= 25
    assert all(field.value is not None and len(field.value) <= 1024 and len(field.name or "") <= 256 for field in embed.fields)
    assert len(embed) <= 6000


async def _view_parts(*, view_timeout: float = SWAP_TIMEOUT_SECONDS) -> tuple[FakeClient, FakeInteraction, SwapConfirmView]:
    """A confirmation view sent as the followup of a deferred /swap interaction, as run_command sends it."""
    client = FakeClient()
    origin = FakeInteraction(client, user=FakeUser())
    ctx = cast("BotContext", SimpleNamespace(engine=None, swap_config=None, now=lambda: None))
    view = SwapConfirmView(ctx, cast("discord.Interaction[Any]", origin), _quote((_route(1000, 500, (1, 3), (100,)),)), timeout=view_timeout)
    await origin.response.defer(thinking=True, ephemeral=True)
    await origin.followup.send(embed=discord.Embed(title="スワップ確認"), view=view, ephemeral=True)
    return client, origin, view


async def test_view_shape_and_constants() -> None:
    _, origin, view = await _view_parts()
    assert view.timeout == SWAP_TIMEOUT_SECONDS
    assert [(button.label, button.style) for button in buttons(view)] == [("確定", discord.ButtonStyle.green), ("キャンセル", discord.ButtonStyle.grey)]
    assert view.min_out == 495
    assert view.idempotency_key == f"discord:{origin.id}"


async def test_only_the_invoker_may_press() -> None:
    client, _, view = await _view_parts()
    stranger = FakeInteraction(client, user=FakeUser())
    await press(view, stranger, label="キャンセル")
    assert stranger.last.via == "response.send_message"
    assert stranger.last.content == NOT_INVOKER_MESSAGE
    assert stranger.last.ephemeral
    assert not view.finished
    assert not any(button.disabled for button in buttons(view))


async def test_cancel_disables_the_buttons() -> None:
    client, origin, view = await _view_parts()
    pressing = FakeInteraction(client, user=origin.user, message=FakeMessage())
    await press(view, pressing, label="キャンセル")
    assert pressing.last.via == "edit_original_response"
    assert pressing.last.embed.title == "エラー"
    assert pressing.last.embed.description == CANCELED_MESSAGE
    assert pressing.last.view is view
    assert view.finished and view.is_finished()
    assert all(button.disabled for button in buttons(view))


async def test_timeout_edits_the_original_message() -> None:
    _, origin, view = await _view_parts()
    await view.on_timeout()
    assert origin.last.via == "edit_original_response"
    assert origin.last.embed.description == TIMED_OUT_MESSAGE
    assert origin.last.view is view
    assert all(button.disabled for button in buttons(view))
    assert origin.original is not None and origin.original.embeds[0].description == TIMED_OUT_MESSAGE


async def test_timeout_after_an_outcome_changes_nothing() -> None:
    client, origin, view = await _view_parts()
    await press(view, FakeInteraction(client, user=origin.user, message=FakeMessage()), label="キャンセル")
    sent = len(origin.sent)
    await view.on_timeout()
    assert len(origin.sent) == sent


async def test_short_timeout_fires_on_its_own() -> None:
    _, origin, view = await _view_parts(view_timeout=0.05)
    # The timeout starts when discord.py stores the view for a sent message.
    view._start_listening_from_store(cast("Any", SimpleNamespace(remove_view=lambda view: None)))
    assert await view.wait() is True
    await asyncio.gather(*(task for task in asyncio.all_tasks() if task.get_name().startswith("discord-ui-view-timeout")))
    assert origin.last.embed.description == TIMED_OUT_MESSAGE
    assert all(button.disabled for button in buttons(view))


async def test_failed_timeout_edit_is_logged(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _, origin, view = await _view_parts()

    async def failing(**_: Any) -> None:
        raise discord.HTTPException(cast("Any", SimpleNamespace(status=404, reason="Not Found")), "Unknown interaction")

    monkeypatch.setattr(origin, "edit_original_response", failing)
    caplog.set_level(logging.WARNING, logger="rapidwire.bot")
    await view.on_timeout()
    assert any(record.getMessage() == "could not show the swap timeout" for record in caplog.records)
