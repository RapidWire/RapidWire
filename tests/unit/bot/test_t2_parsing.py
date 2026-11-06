from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rapidwire.bot.app import build_bot
from rapidwire.bot.commands.account import EXECUTION_STATUS_LABELS, EXECUTION_TYPE_LABELS, HISTORY_TIMEZONE, date_bounds, execution_type_autocomplete, load_page
from rapidwire.bot.commands.stake import rate_text
from rapidwire.bot.errors import UserInputError
from rapidwire.config import Settings
from rapidwire.core.paging import MAX_WINDOW, Page
from rapidwire.core.types import ExecutionStatus, ExecutionType
from tests.unit.bot.fakes import FakeInteraction, as_interaction, find_command


def _page(page: int, total: int, per_page: int = 15) -> Page[int]:
    start = (page - 1) * per_page
    return Page(items=tuple(range(start, min(start + per_page, total))), total=total, page=page, per_page=per_page)


def test_date_bounds_are_jst_midnights_with_an_exclusive_end() -> None:
    start, end = date_bounds("2026-01-31", "2026-02-01")
    assert start == datetime(2026, 1, 30, 15, tzinfo=UTC)
    assert end == datetime(2026, 2, 1, 15, tzinfo=UTC)
    assert start is not None and start.utcoffset() == HISTORY_TIMEZONE.utcoffset(None)
    assert date_bounds(None, None) == (None, None)
    assert date_bounds("", "") == (None, None)
    assert date_bounds("  ", "\t") == (None, None)
    assert date_bounds(" 2026-01-31 ", None)[0] == datetime(2026, 1, 30, 15, tzinfo=UTC)
    one_day = date_bounds("2026-03-01", "2026-03-01")
    assert one_day == (datetime(2026, 2, 28, 15, tzinfo=UTC), datetime(2026, 3, 1, 15, tzinfo=UTC))
    assert date_bounds(None, "2024-02-29")[1] == datetime(2024, 2, 29, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026/01/01", None, "開始日は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。"),
        ("20260101", None, "開始日は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。"),
        (None, "2026-1-1", "終了日は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。"),
        (None, "2026-02-30", "終了日に存在しない日付が指定されました。"),
        (None, "9999-12-31", "終了日に存在しない日付が指定されました。"),
        ("2026-01-02", "2026-01-01", "終了日には開始日以降の日付を指定してください。"),
        ("".join(chr(0xFF10 + digit) for digit in (2, 0, 2, 6)) + "-01-01", None, "開始日は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。"),
    ],
)
def test_bad_dates(start: str | None, end: str | None, message: str) -> None:
    with pytest.raises(UserInputError) as caught:
        date_bounds(start, end)
    assert caught.value.message == message


def test_rate_text() -> None:
    assert rate_text(0) == "0.0000%/h"
    assert rate_text(12_500) == "1.2500%/h"
    assert rate_text(1) == "0.0001%/h"
    assert rate_text(1_000_000) == "100.0000%/h"


def test_labels_cover_every_enum_member() -> None:
    assert set(EXECUTION_TYPE_LABELS) == set(ExecutionType)
    assert set(EXECUTION_STATUS_LABELS) == set(ExecutionStatus)


async def test_execution_type_autocomplete() -> None:
    interaction = as_interaction(FakeInteraction(None))
    everything = await execution_type_autocomplete(interaction, "")
    assert len(everything) == 25
    assert everything[0].value == "transfer"
    transfers = await execution_type_autocomplete(interaction, "TRANSFER")
    assert [choice.value for choice in transfers] == ["transfer", "transfer_from"]
    assert transfers[0].name == "transfer (送金)"
    assert [choice.value for choice in await execution_type_autocomplete(interaction, "スワップ")] == ["swap"]
    assert await execution_type_autocomplete(interaction, "nothing") == []


async def test_load_page_in_range_and_empty() -> None:
    calls: list[int] = []

    async def fetch(page: int) -> Page[int]:
        calls.append(page)
        return _page(page, 31)

    result, pages = await load_page(3, 15, fetch)
    assert (result.items, pages, calls) == ((30,), 3, [3])

    async def empty(page: int) -> Page[int]:
        return _page(page, 0)

    assert (await load_page(1, 15, empty))[1] == 0
    assert (await load_page(5, 15, empty))[1] == 0
    assert (await load_page(10**9, 15, empty))[1] == 0


@pytest.mark.parametrize(
    ("page", "message"),
    [
        (0, "指定されたページ (0) は存在しません。全 3 ページです。"),
        (-2, "指定されたページ (-2) は存在しません。全 3 ページです。"),
        (4, "指定されたページ (4) は存在しません。全 3 ページです。"),
        (10**12, "指定されたページ (1000000000000) は存在しません。全 3 ページです。"),
    ],
)
async def test_load_page_out_of_range(page: int, message: str) -> None:
    async def fetch(number: int) -> Page[int]:
        return _page(number, 31)

    with pytest.raises(UserInputError) as caught:
        await load_page(page, 15, fetch)
    assert caught.value.message == message


async def test_load_page_beyond_the_window() -> None:
    async def fetch(number: int) -> Page[int]:
        assert number * 15 <= MAX_WINDOW
        return Page(items=(), total=MAX_WINDOW, page=number, per_page=15, total_is_lower_bound=True)

    with pytest.raises(UserInputError) as caught:
        await load_page(667, 15, fetch)
    assert caught.value.message == "先頭の 10,000 件より後のページは表示できません。条件を絞り込んでください。"


def test_command_parameters() -> None:
    bot = build_bot(Settings(_env_file=None, db_password="unit-test-password"))
    group = bot.tree.get_command("discord-permission")
    assert group is not None and group.default_permissions is not None and group.default_permissions.administrator
    assert getattr(group, "guild_only", False)
    for name in ("transfer", "transfer_from", "approve set", "approve info", "stake deposit", "stake withdraw"):
        params = find_command(bot.tree, name)._params
        assert params["symbol"].autocomplete is not None, name
        if "amount" in params:
            assert params["amount"].type.name == "string", name
    history = find_command(bot.tree, "history")._params
    assert history["currency_symbol"].autocomplete is not None
    assert history["execution_type"].autocomplete is not None
    assert history["min_amount"].type.name == history["max_amount"].type.name == "string"
    description = find_command(bot.tree, "claim create")._params["description"].to_dict()
    assert description["max_length"] == 50
    assert not description["required"]
