from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import discord
import pytest

from rapidwire.bot.autocomplete import MAX_CHOICES, symbol_choices
from rapidwire.bot.embeds import (
    DESCRIPTION_MAX,
    ERROR_COLOUR,
    ERROR_TITLE,
    INFO_COLOUR,
    ITEMS_PER_PAGE,
    POOL_COLOUR,
    STAKE_COLOUR,
    SUCCESS_COLOUR,
    SUCCESS_TITLE,
    Field,
    account_mention,
    amount_text,
    error_embed,
    info_embed,
    plain_text,
    pool_embed,
    resolve_user_label,
    stake_embed,
    success_embed,
    timestamp_full,
    timestamp_relative,
    unknown_user_label,
)
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.paging import check_page, page_count, page_title
from rapidwire.core.queries import CurrencySymbol
from tests.unit.bot.fakes import FakeClient, FakeUser


def test_embed_styles_match_the_earlier_bot() -> None:
    error = error_embed("残高が不足しています。")
    assert (error.title, error.description, error.colour) == ("エラー", "残高が不足しています。", discord.Colour.red())
    success = success_embed()
    assert (success.title, success.description, success.colour) == ("成功", None, discord.Colour.green())
    sent = success_embed("送金完了", "done", fields=[Field("転送ID", "`1`")])
    assert [(field.name, field.value, field.inline) for field in sent.fields] == [("転送ID", "`1`", False)]
    assert info_embed("通貨情報").colour == INFO_COLOUR == discord.Colour.blue()
    assert pool_embed("p").colour == POOL_COLOUR == discord.Colour.purple()
    assert stake_embed("s").colour == STAKE_COLOUR == discord.Colour.purple()
    assert ERROR_COLOUR != SUCCESS_COLOUR
    assert ITEMS_PER_PAGE == 15


def test_long_descriptions_are_cut_to_the_discord_limit() -> None:
    exact = info_embed("t", "x" * DESCRIPTION_MAX)
    assert exact.description == "x" * DESCRIPTION_MAX
    cut = error_embed("y" * (DESCRIPTION_MAX + 50))
    assert cut.description is not None and len(cut.description) == DESCRIPTION_MAX
    assert cut.description.endswith("...")
    assert (ERROR_TITLE, SUCCESS_TITLE) == ("エラー", "成功")


def test_amount_and_account_display() -> None:
    assert amount_text(1_234_567, "RAPID") == "`1,234.567 RAPID`"
    assert amount_text(5) == "`0.005`"
    assert account_mention(0) == "システム"
    assert account_mention(-1) == "システム"
    assert account_mention(42) == "<@42>"
    assert unknown_user_label(42) == "`42` (不明なユーザー)"


async def test_resolve_user_label() -> None:
    client = FakeClient()
    client.users[7] = FakeUser(7)
    assert await resolve_user_label(cast(Any, client), 7) == "<@7>"
    assert await resolve_user_label(cast(Any, client), 8) == "`8` (不明なユーザー)"
    assert await resolve_user_label(cast(Any, client), 0) == "システム"


def test_timestamps() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    epoch = int(moment.timestamp())
    assert timestamp_full(moment) == f"<t:{epoch}:F>"
    assert timestamp_relative(moment) == f"<t:{epoch}:R>"
    assert timestamp_full(123) == "<t:123:F>"


def test_plain_text_escapes_and_truncates() -> None:
    assert plain_text("@everyone **x**") == "@​everyone \\*\\*x\\*\\*"
    assert plain_text("abcdef", limit=5) == "ab..."


def test_page_count_and_title() -> None:
    assert [page_count(n) for n in (0, 1, 15, 16, 30, 31)] == [0, 1, 1, 2, 2, 3]
    assert page_count(11, per_page=10) == 2
    assert page_title("Aの保有資産", 1, 3) == "Aの保有資産 (ページ 1/3)"


def test_check_page() -> None:
    assert check_page(1, 0) == 0
    assert check_page(2, 0) == 0
    assert check_page(2, 16) == 2
    for page, total in ((3, 16), (0, 16), (-1, 0)):
        with pytest.raises(UserInputError) as caught:
            check_page(page, total)
        assert caught.value.message == f"指定されたページ ({page}) は存在しません。全 {page_count(total)} ページです。"


def test_symbol_choices() -> None:
    choices = symbol_choices([CurrencySymbol(1, "RAPID", "Rapid"), CurrencySymbol(2, "X", "n" * 200)])
    assert [(choice.name, choice.value) for choice in choices] == [("RAPID (Rapid)", "RAPID"), (f"X ({'n' * 200}"[:100], "X")]
    many = [CurrencySymbol(i, f"A{chr(65 + i // 26)}{chr(65 + i % 26)}", "n") for i in range(60)]
    assert len(symbol_choices(many)) == MAX_CHOICES
