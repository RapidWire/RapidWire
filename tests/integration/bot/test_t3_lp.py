from __future__ import annotations

from typing import Any

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.dex_format import FEE_TIER_CHOICES
from rapidwire.core import pools
from rapidwire.db.tables import MINIMUM_LIQUIDITY, execution
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, create_pool, mint
from tests.unit.bot.fakes import FakeInteraction, FakeUser, find_command, invoke

# SLV has the lower ID, so the pools' canonical order is SLV/GLD while most commands name GLD first.
SLV = 7_300_000
GOLD = 7_300_001
COPPER = 7_300_003
ALICE = 7_400_001
BOB = 7_400_002


async def _seed(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=GOLD, symbol="GLD", name="Gold")
    await create_currency(db, currency_id=SLV, symbol="SLV", name="Silver")
    await create_currency(db, currency_id=COPPER, symbol="CPR", name="Copper")


async def _run(bot: RapidWireBot, make_interaction: MakeInteraction, command: str, /, *, user_id: int, **arguments: Any) -> FakeInteraction:
    return await invoke(bot.tree, command, make_interaction(user_id=user_id, guild_id=GOLD), **arguments)


def _embed(interaction: FakeInteraction) -> discord.Embed:
    return interaction.last.embed


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {field.name or "": field.value or "" for field in embed.fields}


async def test_create_add_remove(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await mint(db, GOLD, ALICE, 10_000_000)
    await mint(db, SLV, ALICE, 10_000_000)
    created = await _run(bot, make_interaction, "lp create", user_id=ALICE, symbol_a="gld", amount_a="100", symbol_b="SLV", amount_b="400", fee_tier=50)
    assert created.response.deferred == {"ephemeral": False, "thinking": True}
    embed = _embed(created)
    assert embed.title == "流動性プール情報: GLD/SLV (0.5%)"
    assert embed.colour == discord.Colour.purple()
    assert embed.description == "流動性プールを作成しました。"
    pool_id = (await pools.get_pools_for_pair(db, GOLD, SLV))[0].pool_id
    total = 200_000
    assert [(field.name, field.value, field.inline) for field in embed.fields] == [
        ("プールID", f"`{pool_id}`", False),
        ("手数料", "0.5%", False),
        ("GLD リザーブ", "`100.000`", True),
        ("SLV リザーブ", "`400.000`", True),
        ("合計シェア", "`200.000`", False),
        ("受け取ったシェア", "`199.000`", False),
        ("実行ID", embed.fields[-1].value, False),
    ]

    added = _embed(await _run(bot, make_interaction, "lp add", user_id=ALICE, symbol_a="GLD", amount_a="10", symbol_b="SLV", amount_b="100", fee_tier=50))
    assert (added.title, added.description) == ("流動性追加完了", "`20.000` シェアを受け取りました。")
    fields = _fields(added)
    assert fields["預け入れた額"] == "`10.000 GLD` + `40.000 SLV`"
    assert fields["保有シェア"] == "`219.000`"
    assert total - MINIMUM_LIQUIDITY + 20_000 == 219_000

    # Shares are typed like amounts: "22" is 22,000 raw shares.
    removed = _embed(await _run(bot, make_interaction, "lp remove", user_id=ALICE, symbol_a="SLV", symbol_b="GLD", shares="22", fee_tier=50))
    assert removed.title == "流動性削除完了"
    assert removed.description == "`44.000 SLV` と `11.000 GLD` を受け取りました。"
    assert (_fields(removed)["削除したシェア"], _fields(removed)["残りのシェア"]) == ("`22.000`", "`197.000`")

    fractional = _embed(await _run(bot, make_interaction, "lp remove", user_id=ALICE, symbol_a="GLD", symbol_b="SLV", shares="1.5", fee_tier=50))
    assert (_fields(fractional)["削除したシェア"], _fields(fractional)["残りのシェア"]) == ("`1.500`", "`195.500`")
    position = (await pools.list_provider_positions(db, ALICE, page=1, per_page=15)).items[0]
    assert position.shares == total - MINIMUM_LIQUIDITY + 20_000 - 22_000 - 1_500
    assert await balance_of(db, ALICE, GOLD) == 10_000_000 - 110_000 + 11_000 + 750
    async with db.connect() as conn:
        keys = (await conn.execute(select(execution.c.idempotency_key).where(execution.c.initiator_id == ALICE, execution.c.idempotency_key.is_not(None)))).scalars().all()
    assert len(keys) == 4 and all(key.startswith("discord:") for key in keys)


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("lp create", {"symbol_a": "GLD", "amount_a": "1", "symbol_b": "gld", "amount_b": "1", "fee_tier": 100}, "異なる2つの通貨を指定してください。"),
        ("lp create", {"symbol_a": "GLD", "amount_a": "1", "symbol_b": "NONE", "amount_b": "1", "fee_tier": 100}, "通貨 `NONE` が見つかりませんでした。シンボルを正しく指定してください。"),
        ("lp create", {"symbol_a": "GLD", "amount_a": "0", "symbol_b": "SLV", "amount_b": "1", "fee_tier": 100}, "金額は 0.001 以上を指定してください。"),
        ("lp create", {"symbol_a": "GLD", "amount_a": "2", "symbol_b": "SLV", "amount_b": "2", "fee_tier": 100}, "残高が不足しています。"),
        (
            "lp create",
            {"symbol_a": "GLD", "amount_a": "0.001", "symbol_b": "SLV", "amount_b": "0.001", "fee_tier": 100},
            "初回の預け入れ額が小さすぎます。発行されるシェア (2つの量の積の平方根) が `1.000` を超えるように預け入れてください。",
        ),
        ("lp add", {"symbol_a": "GLD", "amount_a": "1", "symbol_b": "SLV", "amount_b": "1", "fee_tier": 10}, "指定された通貨ペアと手数料の段階の流動性プールは見つかりませんでした。"),
        (
            "lp remove",
            {"symbol_a": "GLD", "symbol_b": "SLV", "shares": "1.0001", "fee_tier": 100},
            "金額は小数点以下 3 桁までの数値で指定してください (例: 12.5)。符号、指数表記、空白、先頭の余分な 0 は使えません。",
        ),
        ("lp remove", {"symbol_a": "GLD", "symbol_b": "SLV", "shares": "0", "fee_tier": 100}, "金額は 0.001 以上を指定してください。"),
        ("lp remove", {"symbol_a": "GLD", "symbol_b": "SLV", "shares": "0.000", "fee_tier": 100}, "金額は 0.001 以上を指定してください。"),
        ("lp remove", {"symbol_a": "GLD", "symbol_b": "SLV", "shares": "5", "fee_tier": 10}, "指定された通貨ペアと手数料の段階の流動性プールは見つかりませんでした。"),
        ("lp info", {"symbol_a": "GLD", "symbol_b": "CPR"}, "指定された通貨ペアのプールは見つかりませんでした。"),
    ],
)
async def test_errors(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, name: str, arguments: dict[str, Any], message: str) -> None:
    await _seed(db)
    embed = _embed(await _run(bot, make_interaction, name, user_id=BOB, **arguments))
    assert (embed.title, embed.description) == ("エラー", message)


async def test_remove_more_shares_than_held(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await create_pool(db, user_id=ALICE, currency_x=GOLD, currency_y=SLV, amount_x=100_000, amount_y=100_000)
    embed = _embed(await _run(bot, make_interaction, "lp remove", user_id=BOB, symbol_a="GLD", symbol_b="SLV", shares="1", fee_tier=100))
    assert embed.description == "保有しているシェアが不足しています。"
    assert "実行ID" in _fields(embed)


async def test_info_shows_every_tier(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    for tier, amount in ((10, 300_000), (100, 100_000)):
        await create_pool(db, user_id=ALICE, currency_x=GOLD, currency_y=SLV, amount_x=amount, amount_y=2 * amount, fee_bps=tier)
    interaction = await _run(bot, make_interaction, "lp info", user_id=BOB, symbol_a="GLD", symbol_b="SLV")
    embeds = interaction.last.embeds
    assert [embed.title for embed in embeds] == ["流動性プール情報: GLD/SLV (0.1%)", "流動性プール情報: GLD/SLV (1%)"]
    assert _fields(embeds[0])["GLD リザーブ"] == "`300.000`"
    assert _fields(embeds[0])["SLV リザーブ"] == "`600.000`"
    reversed_order = (await _run(bot, make_interaction, "lp info", user_id=BOB, symbol_a="SLV", symbol_b="GLD")).last.embeds
    assert _fields(reversed_order[1])["SLV リザーブ"] == "`200.000`"
    assert reversed_order[1].title == "流動性プール情報: SLV/GLD (1%)"


async def test_list_positions(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    first = await create_pool(db, user_id=ALICE, currency_x=GOLD, currency_y=SLV, amount_x=300_000, amount_y=300_000)
    await create_pool(db, user_id=BOB, currency_x=GOLD, currency_y=COPPER, amount_x=100_000, amount_y=100_000)
    await create_pool(db, user_id=BOB, currency_x=GOLD, currency_y=SLV, amount_x=10_000, amount_y=10_000, fee_bps=10)
    interaction = make_interaction(user_id=BOB, guild_id=GOLD)
    target = FakeUser(ALICE, name="alice")
    embed = (await invoke(bot.tree, "lp list", interaction, user=target, page=1)).last.embed
    assert embed.title == "alice の流動性プール一覧 (ページ 1/1)"
    assert embed.colour == discord.Colour.purple()
    assert [(field.name, field.value) for field in embed.fields] == [
        (f"ID: {first.pool_id} | SLV/GLD (1%)", "保有シェア: `299.000` (99.67%)\n推定価値: `299.000 SLV` + `299.000 GLD`"),
    ]
    own = (await invoke(bot.tree, "lp list", make_interaction(user_id=BOB, guild_id=GOLD), user=None, page=1)).last.embed
    assert len(own.fields) == 2
    beyond = (await invoke(bot.tree, "lp list", make_interaction(user_id=BOB, guild_id=GOLD), user=None, page=2)).last.embed
    assert beyond.description == "指定されたページ (2) は存在しません。全 1 ページです。"
    assert (await invoke(bot.tree, "lp list", make_interaction(user_id=BOB, guild_id=GOLD), user=None, page=0)).last.embed.description == "指定されたページ (0) は存在しません。全 1 ページです。"
    nobody = (await invoke(bot.tree, "lp list", make_interaction(user_id=BOB, guild_id=GOLD), user=FakeUser(name="carol"), page=1)).last.embed
    assert (nobody.title, nobody.description) == ("流動性プール一覧", "carol は流動性を提供していません。")


async def test_list_pages_by_fifteen(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    for index in range(17):
        await create_currency(db, currency_id=7_500_000 + index, symbol=f"P{chr(65 + index)}")
    for index in range(1, 17):
        await create_pool(db, user_id=ALICE, currency_x=7_500_000, currency_y=7_500_000 + index, amount_x=10_000, amount_y=10_000)
    first = (await invoke(bot.tree, "lp list", make_interaction(user_id=ALICE), user=None, page=1)).last.embed
    second = (await invoke(bot.tree, "lp list", make_interaction(user_id=ALICE), user=None, page=2)).last.embed
    assert (len(first.fields), len(second.fields)) == (15, 1)
    assert second.title is not None and second.title.endswith("(ページ 2/2)")


def test_parameters(bot: RapidWireBot) -> None:
    for name in ("lp create", "lp add", "lp remove"):
        fee = next(param for param in find_command(bot.tree, name).parameters if param.name == "fee_tier")
        assert fee.required
        assert [(choice.name, choice.value) for choice in fee.choices] == [(choice.name, choice.value) for choice in FEE_TIER_CHOICES] == [("1%", 100), ("0.5%", 50), ("0.1%", 10)]
    for name in ("lp create", "lp add", "lp remove", "lp info"):
        params = {param.name: param for param in find_command(bot.tree, name).parameters}
        assert params["symbol_a"].autocomplete and params["symbol_b"].autocomplete
    assert [param.name for param in find_command(bot.tree, "lp remove").parameters] == ["symbol_a", "symbol_b", "shares", "fee_tier"]
