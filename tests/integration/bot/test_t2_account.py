from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.errors import NUMBER_FORMAT_MESSAGES
from rapidwire.core import allowances, transfers
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.types import ExecutionType, TransferKind
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, insert_transfer, mint
from tests.unit.bot.fakes import FakeInteraction, FakeUser, invoke

GUILD = 7_100_001
OTHER = 7_100_002
ALICE = 7_200_001
BOB = 7_200_002
CAROL = 7_200_003
T0 = datetime(2026, 1, 31, 12, tzinfo=UTC)
UNIT = 1000


def _fields(sent: discord.Embed) -> list[tuple[str, str]]:
    return [(field.name or "", field.value or "") for field in sent.fields]


def _error(interaction: FakeInteraction) -> str:
    embed = interaction.last.embed
    assert embed.title == "エラー", embed.description
    return embed.description or ""


async def _seed(db: AsyncEngine) -> int:
    seeded = await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid", now=T0)
    await create_currency(db, currency_id=OTHER, symbol="OTH", name="Other", now=T0)
    await mint(db, GUILD, ALICE, 100 * UNIT, now=T0)
    await mint(db, OTHER, ALICE, 5 * UNIT, now=T0)
    return seeded.issuance_account_id


async def test_balance_lists_and_pages(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await invoke(bot.tree, "balance", make_interaction(user_id=ALICE, guild_id=GUILD), user=None, page=1)
    assert interaction.response.deferred == {"ephemeral": False, "thinking": True}
    embed = interaction.last.embed
    assert embed.colour == discord.Colour.green()
    assert embed.title == f"user{ALICE}の保有資産 (ページ 1/1)"
    assert _fields(embed) == [("Rapid (RAPID)", "`100.000`"), ("Other (OTH)", "`5.000`")]

    interaction = await invoke(bot.tree, "balance", make_interaction(user_id=ALICE), user=None, page=2)
    assert _error(interaction) == "指定されたページ (2) は存在しません。全 1 ページです。"

    bob = FakeUser(BOB)
    interaction = await invoke(bot.tree, "balance", make_interaction(user_id=ALICE), user=bob, page=1)
    assert interaction.last.embed.title == "残高"
    assert interaction.last.embed.description == f"user{BOB}は資産を保有していません。"


async def test_balance_pages_hold_fifteen(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    for i in range(16):
        await create_currency(db, currency_id=8_000_000 + i, symbol=f"C{chr(65 + i)}", name=f"Coin{i}")
        await mint(db, 8_000_000 + i, ALICE, UNIT)
    first = (await invoke(bot.tree, "balance", make_interaction(user_id=ALICE), user=None, page=1)).last.embed
    second = (await invoke(bot.tree, "balance", make_interaction(user_id=ALICE), user=None, page=2)).last.embed
    assert (len(first.fields), first.title) == (15, f"user{ALICE}の保有資産 (ページ 1/2)")
    assert _fields(second) == [("Coin15 (CP)", "`1.000`")]


async def test_transfer(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    bob = FakeUser(BOB)
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, amount="1234.5", symbol=None)
    assert _error(interaction) == "残高が不足しています。"
    await mint(db, GUILD, ALICE, 2000 * UNIT)
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, amount="1234.5", symbol=None)
    assert interaction.last.embed.description == f"<@{BOB}> へ `1,234.500 RAPID` の送金が完了しました。"

    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, amount="12.5", symbol=None)
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("送金完了", f"<@{BOB}> へ `12.500 RAPID` の送金が完了しました。")
    (name, value) = _fields(embed)[0]
    assert name == "転送ID" and value.startswith("`")
    assert await balance_of(db, BOB, GUILD) == 1_247_000

    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE), user=bob, amount="1", symbol="oth")
    assert interaction.last.embed.description == f"<@{BOB}> へ `1.000 OTH` の送金が完了しました。"


NOT_POSITIVE = "送金額は0より大きい必要があります。"

BAD_TRANSFER_AMOUNTS = [
    ("0", NOT_POSITIVE),
    ("0.000", NOT_POSITIVE),
    ("-1", NOT_POSITIVE),
    ("1.2345", NUMBER_FORMAT_MESSAGES["amount_format"]),
    ("1e3", NUMBER_FORMAT_MESSAGES["amount_format"]),
    (" 1", NUMBER_FORMAT_MESSAGES["amount_format"]),
    ("1 ", NUMBER_FORMAT_MESSAGES["amount_format"]),
    ("9" * 40, NUMBER_FORMAT_MESSAGES["amount_range"]),
]


@pytest.mark.parametrize(("amount", "message"), BAD_TRANSFER_AMOUNTS)
async def test_transfer_rejects_bad_amounts(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, amount: str, message: str) -> None:
    await _seed(db)
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(BOB), amount=amount, symbol=None)
    assert _error(interaction) == message
    assert await balance_of(db, BOB, GUILD) == 0


@pytest.mark.parametrize(("amount", "message"), BAD_TRANSFER_AMOUNTS)
async def test_transfer_from_rejects_bad_amounts(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, amount: str, message: str) -> None:
    await _seed(db)
    await allowances.approve(db, owner_id=ALICE, spender_id=BOB, currency_id=GUILD, amount=5 * UNIT)
    interaction = await invoke(bot.tree, "transfer_from", make_interaction(user_id=BOB, guild_id=GUILD), source=FakeUser(ALICE), destination=FakeUser(CAROL), amount=amount, symbol=None)
    assert _error(interaction) == message
    assert await balance_of(db, CAROL, GUILD) == 0


async def test_transfer_errors(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(ALICE), amount="1", symbol=None)
    assert _error(interaction) == "自分自身には送金できません。"
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE), user=FakeUser(BOB), amount="1", symbol=None)
    assert _error(interaction) == "サーバー内で実行するか、通貨のシンボルを指定してください。"
    interaction = await invoke(bot.tree, "transfer", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(BOB), amount="1", symbol="NOPE")
    assert _error(interaction) == "通貨 `NOPE` が見つかりませんでした。シンボルを正しく指定してください。"


async def test_transfer_from(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    alice, carol = FakeUser(ALICE), FakeUser(CAROL)
    interaction = await invoke(bot.tree, "transfer_from", make_interaction(user_id=BOB, guild_id=GUILD), source=alice, destination=carol, amount="3", symbol=None)
    assert _error(interaction) == "承認額 (Allowance) が不足しています。"

    await allowances.approve(db, owner_id=ALICE, spender_id=BOB, currency_id=GUILD, amount=5 * UNIT)
    interaction = await invoke(bot.tree, "transfer_from", make_interaction(user_id=BOB, guild_id=GUILD), source=alice, destination=carol, amount="3", symbol=None)
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("代理送金完了", f"<@{ALICE}> から <@{CAROL}> へ `3.000 RAPID` の代理送金が完了しました。")
    assert [name for name, _ in _fields(embed)] == ["転送ID", "実行ID"]
    assert await balance_of(db, CAROL, GUILD) == 3 * UNIT
    assert await allowances.get_allowance(db, ALICE, BOB, GUILD) == 2 * UNIT

    interaction = await invoke(bot.tree, "transfer_from", make_interaction(user_id=ALICE, guild_id=GUILD), source=alice, destination=carol, amount="1", symbol=None)
    assert _error(interaction).startswith("送金元は実行者自身とも送金先とも異なるユーザーである必要があります。")


async def test_transfer_from_with_allowance_but_too_little_balance(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await allowances.approve(db, owner_id=ALICE, spender_id=BOB, currency_id=GUILD, amount=500 * UNIT)
    interaction = await invoke(bot.tree, "transfer_from", make_interaction(user_id=BOB, guild_id=GUILD), source=FakeUser(ALICE), destination=FakeUser(CAROL), amount="200", symbol=None)
    assert _error(interaction) == "残高が不足しています。"
    assert await balance_of(db, ALICE, GUILD) == 100 * UNIT
    assert await balance_of(db, CAROL, GUILD) == 0
    assert await allowances.get_allowance(db, ALICE, BOB, GUILD) == 500 * UNIT


async def _contract_move(db: AsyncEngine, *, source: int, destination: int, amount: int, function: str, at: datetime) -> int:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([GUILD])
        await tx.lock_balances([(source, GUILD), (destination, GUILD)])
        transfer_id = await tx.move(execution_id, GUILD, source, destination, amount, TransferKind.PRINCIPAL)
        return {"transfer_id": transfer_id}

    result = await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=source, input={"amount": amount}, body=body, now=at, contract_function=function)
    return result.int_field("transfer_id")


async def _seed_history(db: AsyncEngine) -> dict[str, int]:
    issuance = await _seed(db)
    await insert_transfer(
        db, execution_id=50, transfer_id=50, type=ExecutionType.TRANSFER, initiator=ALICE, currency_id=GUILD, source=ALICE, destination=BOB, amount=2 * UNIT, at=T0 + timedelta(hours=1)
    )
    # 2026-01-31 23:59:59 and 2026-02-01 00:00:00 in Japan Standard Time.
    late = await transfers.transfer(db, sender_id=ALICE, recipient_id=BOB, currency_id=GUILD, amount=3 * UNIT, now=datetime(2026, 1, 31, 14, 59, 59, tzinfo=UTC))
    early = await transfers.transfer(db, sender_id=BOB, recipient_id=ALICE, currency_id=GUILD, amount=1 * UNIT, now=datetime(2026, 1, 31, 15, tzinfo=UTC))
    called = await _contract_move(db, source=ALICE, destination=CAROL, amount=7 * UNIT, function="buy", at=datetime(2026, 2, 3, tzinfo=UTC))
    return {"issuance": issuance, "explicit": 50, "late": late.transfer_id, "early": early.transfer_id, "called": called}


async def _history(bot: RapidWireBot, interaction: FakeInteraction, **filters: Any) -> FakeInteraction:
    arguments: dict[str, Any] = {
        "transfer_id": None,
        "user": None,
        "source": None,
        "destination": None,
        "currency_symbol": None,
        "start_date": None,
        "end_date": None,
        "min_amount": None,
        "max_amount": None,
        "execution_type": None,
        "contract_function": None,
        "page": 1,
    }
    arguments.update(filters)
    return await invoke(bot.tree, "history", interaction, **arguments)


def _ids(embed: discord.Embed) -> list[int]:
    return [int((field.name or "").split(" | ")[1].removeprefix("ID: ")) for field in embed.fields]


async def test_history_list_directions(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    ids = await _seed_history(db)
    embed = (await _history(bot, make_interaction(user_id=ALICE, guild_id=GUILD))).last.embed
    assert embed.title == "転送履歴 (ページ 1/1)"
    assert embed.colour == discord.Colour.blue()
    assert _ids(embed) == sorted(_ids(embed), reverse=True)
    by_id = {int((field.name or "").split(" | ")[1].removeprefix("ID: ")): field for field in embed.fields}
    explicit = by_id[ids["explicit"]]
    assert explicit.name is not None and explicit.name.startswith("📤 | ID: 50 | <t:") and explicit.name.endswith(":R>")
    assert explicit.value == f"`2.000 RAPID` to <@{BOB}>"
    assert by_id[ids["early"]].value == f"`1.000 RAPID` from <@{BOB}>"
    assert (by_id[ids["early"]].name or "").startswith("📥")
    minted = [field for field in embed.fields if (field.value or "").endswith("from システム")]
    assert len(minted) == 2

    embed = (await _history(bot, make_interaction(user_id=CAROL), source=FakeUser(ALICE), destination=FakeUser(BOB))).last.embed
    assert sorted(_ids(embed)) == sorted([ids["explicit"], ids["late"]])
    assert all((field.name or "").startswith("📤") for field in embed.fields)

    embed = (await _history(bot, make_interaction(user_id=CAROL), user=FakeUser(BOB))).last.embed
    assert sorted(_ids(embed)) == sorted([ids["explicit"], ids["late"], ids["early"]])

    embed = (await _history(bot, make_interaction(user_id=CAROL))).last.embed
    assert _ids(embed) == [ids["called"]]
    assert (embed.fields[0].name or "").startswith("📥")


async def test_history_without_matches(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    embed = (await _history(bot, make_interaction(user_id=BOB))).last.embed
    assert (embed.title, embed.description) == ("転送履歴", "指定された条件の転送履歴はありません。")


async def test_history_date_bounds_are_japan_days(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    ids = await _seed_history(db)
    jan31 = (await _history(bot, make_interaction(user_id=BOB), start_date="2026-01-31", end_date="2026-01-31")).last.embed
    assert sorted(_ids(jan31)) == sorted([ids["explicit"], ids["late"]])
    feb1 = (await _history(bot, make_interaction(user_id=BOB), start_date="2026-02-01")).last.embed
    assert _ids(feb1) == [ids["early"]]
    until_jan30 = (await _history(bot, make_interaction(user_id=BOB), end_date="2026-01-30")).last.embed
    assert until_jan30.description == "指定された条件の転送履歴はありません。"
    interaction = await _history(bot, make_interaction(user_id=BOB), start_date="31-01-2026")
    assert _error(interaction) == "開始日は YYYY-MM-DD の形式で指定してください (例: 2026-01-31)。"


async def test_history_amount_type_function_and_currency_filters(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    ids = await _seed_history(db)
    alice = make_interaction
    embed = (await _history(bot, alice(user_id=ALICE), min_amount="2", max_amount="3")).last.embed
    assert sorted(_ids(embed)) == sorted([ids["explicit"], ids["late"]])
    embed = (await _history(bot, alice(user_id=ALICE), execution_type="contract_call")).last.embed
    assert _ids(embed) == [ids["called"]]
    embed = (await _history(bot, alice(user_id=ALICE), execution_type=" TRANSFER ")).last.embed
    assert sorted(_ids(embed)) == sorted([ids["explicit"], ids["late"], ids["early"]])
    embed = (await _history(bot, alice(user_id=ALICE), contract_function="buy")).last.embed
    assert _ids(embed) == [ids["called"]]
    embed = (await _history(bot, alice(user_id=ALICE), contract_function="sell")).last.embed
    assert embed.description == "指定された条件の転送履歴はありません。"
    embed = (await _history(bot, alice(user_id=ALICE), currency_symbol="oth")).last.embed
    assert len(embed.fields) == 1 and (embed.fields[0].value or "").startswith("`5.000 OTH`")

    assert _error(await _history(bot, alice(user_id=ALICE), execution_type="bogus")) == "実行の種別は候補の中から選んでください。"
    assert _error(await _history(bot, alice(user_id=ALICE), contract_function="f" * 65)) == "コントラクトの関数名は 64 文字以内で指定してください。"
    assert _error(await _history(bot, alice(user_id=ALICE), min_amount="5", max_amount="1")) == "最大金額には最小金額以上の値を指定してください。"
    assert _error(await _history(bot, alice(user_id=ALICE), min_amount="1.0001")) == NUMBER_FORMAT_MESSAGES["amount_format"]
    assert _error(await _history(bot, alice(user_id=ALICE), min_amount=" 2")) == NUMBER_FORMAT_MESSAGES["amount_format"]
    assert _error(await _history(bot, alice(user_id=ALICE), max_amount="3 ")) == NUMBER_FORMAT_MESSAGES["amount_format"]
    assert _error(await _history(bot, alice(user_id=ALICE), currency_symbol="NOPE")) == "通貨 `NOPE` が見つかりませんでした。シンボルを正しく指定してください。"
    assert _error(await _history(bot, alice(user_id=ALICE), page=0)) == "指定されたページ (0) は存在しません。全 1 ページです。"


async def test_history_blank_filters_are_not_given(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed_history(db)
    unfiltered = (await _history(bot, make_interaction(user_id=ALICE))).last.embed
    blanks = {"start_date": " ", "end_date": "\t", "min_amount": "  ", "max_amount": " ", "execution_type": " ", "contract_function": " ", "currency_symbol": " "}
    blank = (await _history(bot, make_interaction(user_id=ALICE), **blanks)).last.embed
    assert (blank.title, _ids(blank)) == (unfiltered.title, _ids(unfiltered))
    padded = (await _history(bot, make_interaction(user_id=BOB), start_date=" 2026-02-01 ")).last.embed
    unpadded = (await _history(bot, make_interaction(user_id=BOB), start_date="2026-02-01")).last.embed
    assert _ids(padded) == _ids(unpadded)


async def test_history_pages_hold_ten(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    for _ in range(10):
        await transfers.transfer(db, sender_id=ALICE, recipient_id=BOB, currency_id=GUILD, amount=1)
    first = (await _history(bot, make_interaction(user_id=ALICE))).last.embed
    second = (await _history(bot, make_interaction(user_id=ALICE), page=2)).last.embed
    assert (len(first.fields), first.title) == (10, "転送履歴 (ページ 1/2)")
    assert (len(second.fields), second.title) == (2, "転送履歴 (ページ 2/2)")


async def test_history_detail(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    ids = await _seed_history(db)
    embed = (await _history(bot, make_interaction(user_id=CAROL), transfer_id=ids["explicit"], user=FakeUser(CAROL))).last.embed
    assert embed.title == "転送詳細: ID 50"
    assert _fields(embed) == [
        ("日時", f"<t:{int((T0 + timedelta(hours=1)).timestamp())}:F>"),
        ("From", f"<@{ALICE}>"),
        ("To", f"<@{BOB}>"),
        ("金額", "`2.000 RAPID`"),
        ("実行ID", "`50`"),
        ("実行の種別", "送金 (`transfer`)"),
        ("実行の状態", "成功"),
    ]
    embed = (await _history(bot, make_interaction(user_id=CAROL), transfer_id=ids["called"])).last.embed
    assert embed.title == f"転送詳細: ID {ids['called']}"
    assert ("コントラクトの関数", "`buy`") in _fields(embed)
    assert ("実行の種別", "コントラクト実行 (`contract_call`)") in _fields(embed)

    first_mint = min(_ids((await _history(bot, make_interaction(user_id=ALICE))).last.embed))
    embed = (await _history(bot, make_interaction(user_id=CAROL), transfer_id=first_mint)).last.embed
    assert ("From", "システム") in _fields(embed)

    for missing in (999_999, 0, -5):
        assert _error(await _history(bot, make_interaction(user_id=CAROL), transfer_id=missing)) == "指定された転送IDは見つかりませんでした。"
