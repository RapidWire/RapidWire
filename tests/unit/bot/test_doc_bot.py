"""document/bot.md pinned to the bot: a `### /<command>` section for every invocable command and no other, each with a parameter table listing exactly the command's parameters, the documented numbers and texts taken from the code, and ASCII outside Japanese text."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from discord import app_commands

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import bot_intents
from rapidwire.bot.commands.account import HISTORY_PER_PAGE
from rapidwire.bot.embeds import ERROR_TITLE, ITEMS_PER_PAGE
from rapidwire.bot.errors import COMMAND_OUTDATED_MESSAGE, GENERIC_MESSAGE
from rapidwire.bot.guild_sync import WITHDRAW_AFTER_FAILURES
from rapidwire.bot.token_mention import BOT_NEEDS_CHANNEL, ISSUE_FAILED, NON_NUMERIC_CHANNEL, REVOKE_FAILED_NOTE, REVOKED_NOTE, SENT_TO_DM, sent_to_channel, token_message
from rapidwire.bot.views.claim import CLAIM_CUSTOM_ID_PREFIX
from rapidwire.bot.views.swap import (
    CANCELED_MESSAGE,
    NOT_INVOKER_MESSAGE,
    PRICE_MOVED_MESSAGE,
    SWAP_TIMEOUT_SECONDS,
    TIMED_OUT_MESSAGE,
    TOLERANCE_DENOMINATOR,
    TOLERANCE_NUMERATOR,
    ZERO_OUTPUT_MESSAGE,
    quote_embed,
)
from rapidwire.config import GUILD_HEARTBEAT_INTERVAL_SECONDS, Settings
from rapidwire.core.amounts import format_amount_display
from rapidwire.core.contracts import LOCK_HOURS_MAX
from rapidwire.core.paging import MAX_WINDOW
from rapidwire.core.swap import SwapQuote
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX, CONTRACT_FUNCTION_MAX, MINIMUM_LIQUIDITY
from tests.unit.bot.test_command_tree import invocable
from tests.unit.doc_text import character_violations

BOT_DOC = Path(__file__).resolve().parents[3] / "document" / "bot.md"
COMMAND_HEADING = re.compile(r"### /(\S.*)")
TABLE_HEADER = "| パラメータ | 型 | 必須 | 説明 |"
TABLE_SEPARATOR = "| --- | --- | --- | --- |"
NO_PARAMETERS = "パラメータはない。"
PARAM_NAME = re.compile(r"`([a-z0-9_-]+)`")
REQUIRED = {"はい": True, "いいえ": False}

type DocParam = tuple[str, str, bool]


def _lines() -> list[str]:
    return BOT_DOC.read_text(encoding="utf-8").splitlines()


def _sections() -> dict[str, list[str]]:
    """The lines of each `### /<command>` section by qualified command name, up to the next heading."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _lines():
        if line.startswith("#"):
            match = COMMAND_HEADING.fullmatch(line)
            current = None
            if match is not None:
                name = match.group(1)
                assert name not in sections, f"duplicate heading for /{name}"
                current = sections[name] = []
            continue
        if current is not None:
            current.append(line)
    return sections


def _table(name: str, lines: list[str]) -> list[DocParam]:
    """The parameters of a command section's table, or [] for a section stating NO_PARAMETERS; anything else fails."""
    headers = [index for index, line in enumerate(lines) if line == TABLE_HEADER]
    if not headers:
        assert NO_PARAMETERS in lines, f"/{name} has neither a parameter table nor {NO_PARAMETERS!r}"
        return []
    assert len(headers) == 1, f"/{name} has more than one parameter table"
    assert NO_PARAMETERS not in lines, f"/{name} has a parameter table and {NO_PARAMETERS!r}"
    start = headers[0]
    assert lines[start + 1] == TABLE_SEPARATOR, f"/{name} table separator"
    rows: list[DocParam] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == 4, f"/{name} row {line!r}"
        match = PARAM_NAME.fullmatch(cells[0])
        assert match is not None, f"/{name} row {line!r}"
        assert cells[2] in REQUIRED, f"/{name} row {line!r}"
        assert cells[3], f"/{name} row {line!r} has no description"
        rows.append((match.group(1), cells[1], REQUIRED[cells[2]]))
    return rows


def _swap_footer() -> str:
    quote = SwapQuote(from_currency=1, to_currency=2, amount_in=1, amount_out=1, routes=(), truncated=False, symbols={1: "A", 2: "B"})
    footer = quote_embed(quote).footer.text
    assert footer is not None
    assert str(SWAP_TIMEOUT_SECONDS) in footer
    return footer


@pytest.fixture(scope="module")
def commands() -> Iterator[dict[str, app_commands.Command[Any, ..., Any]]]:
    bot = build_bot(Settings(_env_file=None, db_password="unit-test-password"))
    yield invocable(bot.tree)
    asyncio.run(bot.context.engine.dispose())


def test_every_command_has_a_section(commands: dict[str, app_commands.Command[Any, ..., Any]]) -> None:
    assert sorted(set(commands) - set(_sections())) == []


def test_every_section_names_a_command(commands: dict[str, app_commands.Command[Any, ..., Any]]) -> None:
    assert sorted(set(_sections()) - set(commands)) == []


def test_parameter_tables_match_the_tree(commands: dict[str, app_commands.Command[Any, ..., Any]]) -> None:
    sections = _sections()
    for name, command in commands.items():
        expected = [(param.name, param.type.name, param.required) for param in command.parameters]
        assert _table(name, sections.get(name, [NO_PARAMETERS])) == expected, f"/{name}"


def test_documented_numbers_and_texts_come_from_the_code() -> None:
    text = BOT_DOC.read_text(encoding="utf-8")
    assert TOLERANCE_DENOMINATOR == 100
    expected = [
        f"1 ページ {ITEMS_PER_PAGE} 件を表示する",
        f"`/history` だけは 1 ページ {HISTORY_PER_PAGE} 件",
        f"先頭 {MAX_WINDOW:,} 件",
        f"表示した見積もりの {TOLERANCE_NUMERATOR}% を下回る",
        f"E * {TOLERANCE_NUMERATOR} / {TOLERANCE_DENOMINATOR}",
        f"`{_swap_footer()}`",
        f"{SWAP_TIMEOUT_SECONDS} 秒間結果が出ないと",
        f"`{NOT_INVOKER_MESSAGE}`",
        f"`{PRICE_MOVED_MESSAGE}`",
        f"`{CANCELED_MESSAGE}`",
        f"`{TIMED_OUT_MESSAGE}`",
        f"`{CLAIM_CUSTOM_ID_PREFIX}pay:<claim id>`",
        f"`{CLAIM_CUSTOM_ID_PREFIX}deny:<claim id>`",
        f"`{CLAIM_CUSTOM_ID_PREFIX}mute:<claim id>`",
        f"`{token_message('<mention>', '').splitlines()[0]}`",
        f"`{SENT_TO_DM}`",
        f"`{sent_to_channel(0).replace('<#0>', '<#channel>')}`",
        f"`{REVOKED_NOTE}`",
        f"`{BOT_NEEDS_CHANNEL}`",
        f"`{GENERIC_MESSAGE}`",
        f"`{COMMAND_OUTDATED_MESSAGE}`",
        f"`{ERROR_TITLE}`",
        f"{GUILD_HEARTBEAT_INTERVAL_SECONDS} 秒ごとに",
        f"1 から {LOCK_HOURS_MAX:,} まで",
        f"{CLAIM_DESCRIPTION_MAX} 文字以内",
        f"{CONTRACT_FUNCTION_MAX} 文字以内",
        f"{format_amount_display(MINIMUM_LIQUIDITY)} シェアを超える",
        f"`{ZERO_OUTPUT_MESSAGE}`",
        f"`{ISSUE_FAILED}`",
        f"`{NON_NUMERIC_CHANNEL}`",
        f"`{REVOKE_FAILED_NOTE}`",
        f"{WITHDRAW_AFTER_FAILURES} 回連続で失敗した",
    ]
    assert [item for item in expected if item not in text] == []


def test_every_intent_is_documented() -> None:
    lines = _lines()
    start = lines.index("### ゲートウェイ intent")
    section = "\n".join(lines[start : lines.index("### 設定", start)])
    enabled = [name for name, value in bot_intents() if value]
    assert enabled
    assert [name for name in enabled if f"`{name}`" not in section] == []
    assert "`message_content` は不要である" in section


def test_the_document_is_ascii_outside_japanese_text() -> None:
    assert character_violations(BOT_DOC) == []
