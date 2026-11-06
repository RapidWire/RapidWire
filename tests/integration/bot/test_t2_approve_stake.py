from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.config import Settings
from rapidwire.core import allowances, staking
from rapidwire.core.clock import FixedClock
from rapidwire.core.permissions import InMemoryGuildInfo
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, mint
from tests.unit.bot.fakes import FakeGuild, FakeInteraction, FakeUser, invoke

GUILD = 7_500_001
ALICE = 7_600_001
BOB = 7_600_002
UNIT = 1000
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def _error(interaction: FakeInteraction) -> str:
    embed = interaction.last.embed
    assert embed.title == "エラー", embed.description
    return embed.description or ""


async def test_approve_set_and_info(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid")
    bob = FakeUser(BOB)
    embed = (await invoke(bot.tree, "approve info", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, symbol=None)).last.embed
    assert (embed.title, embed.description) == ("許可情報", f"<@{BOB}> への現在の許可額: `0.000 RAPID`")

    embed = (await invoke(bot.tree, "approve set", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, amount="1500", symbol=None)).last.embed
    assert (embed.title, embed.description) == ("承認完了", f"<@{BOB}> に `1,500.000 RAPID` の使用を許可しました。")
    assert await allowances.get_allowance(db, ALICE, BOB, GUILD) == 1500 * UNIT

    embed = (await invoke(bot.tree, "approve info", make_interaction(user_id=ALICE), user=bob, symbol="rapid")).last.embed
    assert embed.description == f"<@{BOB}> への現在の許可額: `1,500.000 RAPID`"

    embed = (await invoke(bot.tree, "approve set", make_interaction(user_id=ALICE, guild_id=GUILD), user=bob, amount="0", symbol=None)).last.embed
    assert embed.description == f"<@{BOB}> に `0.000 RAPID` の使用を許可しました。"
    assert await allowances.get_allowance(db, ALICE, BOB, GUILD) == 0


async def test_approve_errors(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid")
    interaction = await invoke(bot.tree, "approve set", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(ALICE), amount="1", symbol=None)
    assert _error(interaction) == "自分自身に許可を与えることはできません。"
    assert [field.name for field in interaction.last.embed.fields] == ["実行ID"]
    interaction = await invoke(bot.tree, "approve set", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(BOB), amount="-1", symbol=None)
    assert _error(interaction) == "許可額は0以上である必要があります。"
    interaction = await invoke(bot.tree, "approve set", make_interaction(user_id=ALICE, guild_id=GUILD), user=FakeUser(BOB), amount=" 1", symbol=None)
    assert _error(interaction).startswith("金額は小数点以下 3 桁までの数値で指定してください")
    assert await allowances.get_allowance(db, ALICE, BOB, GUILD) == 0
    interaction = await invoke(bot.tree, "approve info", make_interaction(user_id=ALICE), user=FakeUser(BOB), symbol=None)
    assert _error(interaction) == "サーバー内で実行するか、通貨のシンボルを指定してください。"


def _clocked(bot_settings: Settings, db: AsyncEngine, guild_info: InMemoryGuildInfo, clock: FixedClock) -> RapidWireBot:
    return build_bot(bot_settings, engine=db, guild_info=guild_info, clock=clock)


async def test_stake_deposit_withdraw_and_info(db: AsyncEngine, bot_settings: Settings, guild_info: InMemoryGuildInfo) -> None:
    await create_currency(db, currency_id=GUILD, symbol="RAPID", name="Rapid", hourly_rate_ppm=12_500, now=T0)
    await mint(db, GUILD, ALICE, 1000 * UNIT, now=T0)
    clock = FixedClock(T0)
    bot = _clocked(bot_settings, db, guild_info, clock)
    guild = FakeGuild(GUILD)

    def interaction() -> FakeInteraction:
        member = guild.get_member(ALICE) or guild.add_member(ALICE)
        return FakeInteraction(bot, user=member, guild=guild)

    empty = (await invoke(bot.tree, "stake info", interaction(), page=1)).last.embed
    assert (empty.title, empty.description) == ("ステーキング情報", "現在、アクティブなステーキングはありません。")

    embed = (await invoke(bot.tree, "stake deposit", interaction(), amount="800", symbol=None)).last.embed
    assert (embed.title, embed.description) == ("ステーキング完了", "`800.000 RAPID` のステーキングが完了しました。\n現在の合計ステーキング額は `800.000 RAPID` です。")
    assert await balance_of(db, ALICE, GUILD) == 200 * UNIT

    clock.advance(timedelta(hours=1))
    embed = (await invoke(bot.tree, "stake info", interaction(), page=1)).last.embed
    assert embed.title == f"user{ALICE}のステーキング情報 (ページ 1/1)"
    assert embed.colour == discord.Colour.purple()
    assert [(field.name, field.value) for field in embed.fields] == [
        ("通貨: **Rapid (RAPID)**", f"ステーキング額: `810.000`\n現在の利率: `1.2500%/h`\n最終更新日時: <t:{int(T0.timestamp())}:F>"),
    ]
    stored = await staking.get_stake(db, ALICE, GUILD)
    assert stored is not None and stored.amount == 800 * UNIT

    embed = (await invoke(bot.tree, "stake withdraw", interaction(), amount="10", symbol="RAPID")).last.embed
    assert (embed.title, embed.description) == ("引き出し完了", "`10.000 RAPID` を引き出しました。\n残りのステーキング額: `800.000 RAPID`")

    assert _error(await invoke(bot.tree, "stake withdraw", interaction(), amount="5000", symbol=None)) == "ステーキングしている額が不足しています。"
    assert _error(await invoke(bot.tree, "stake deposit", interaction(), amount="5000", symbol=None)) == "ステーキングするための残高が不足しています。"
    assert _error(await invoke(bot.tree, "stake deposit", interaction(), amount="0.0001", symbol=None)).startswith("金額は小数点以下 3 桁までの数値で指定してください")
    assert _error(await invoke(bot.tree, "stake info", interaction(), page=2)) == "指定されたページ (2) は存在しません。全 1 ページです。"


async def test_stake_info_pages_hold_fifteen(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    for i in range(16):
        currency_id = 9_000_000 + i
        await create_currency(db, currency_id=currency_id, symbol=f"S{chr(65 + i)}", name=f"Stake{i}")
        await mint(db, currency_id, ALICE, UNIT)
        await staking.deposit(db, account_id=ALICE, currency_id=currency_id, amount=UNIT)
    first = (await invoke(bot.tree, "stake info", make_interaction(user_id=ALICE), page=1)).last.embed
    second = (await invoke(bot.tree, "stake info", make_interaction(user_id=ALICE), page=2)).last.embed
    assert (len(first.fields), len(second.fields)) == (15, 1)
    assert second.title == f"user{ALICE}のステーキング情報 (ページ 2/2)"
