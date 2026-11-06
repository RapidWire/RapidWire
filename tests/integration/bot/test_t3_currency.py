from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot.app import build_bot
from rapidwire.bot.client import RapidWireBot
from rapidwire.config import Settings
from rapidwire.core.clock import FixedClock
from rapidwire.core.currency import get_currency
from rapidwire.core.errors import RequestExpired
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import execution
from tests.integration.bot.conftest import MakeInteraction
from tests.integration.factories import balance_of, create_currency, create_pool, mint, supply_of
from tests.unit.bot.fakes import FakeInteraction, find_command, invoke

GUILD = 7_100_001
OTHER = 7_100_002
OWNER = 7_200_001
ADMIN = 7_200_002
MEMBER = 7_200_003
START = datetime(2026, 3, 1, tzinfo=UTC)
NOT_IN_GUILD = "Botがこのサーバーに参加していないため、サーバーの所有者と管理者を確認できません。"
MANAGER_DENIED = "この操作を行えるのは、サーバーの所有者と、管理の委任が有効な場合の管理者だけです。"


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture
def bot(db: AsyncEngine, bot_settings: Settings, guild_info: InMemoryGuildInfo, clock: FixedClock) -> RapidWireBot:
    """The bot on a fixed clock, so timelocks can be passed by moving it."""
    return build_bot(bot_settings, engine=db, guild_info=guild_info, clock=clock)


@pytest.fixture(autouse=True)
def guild(guild_info: InMemoryGuildInfo) -> None:
    guild_info.add_guild(GUILD, OWNER)
    guild_info.add_admin(GUILD, ADMIN)
    guild_info.add_member(GUILD, MEMBER)


async def _run(bot: RapidWireBot, make_interaction: MakeInteraction, command: str, /, *, user_id: int, guild_id: int | None = GUILD, **arguments: Any) -> FakeInteraction:
    return await invoke(bot.tree, command, make_interaction(user_id=user_id, guild_id=guild_id), **arguments)


def _embed(interaction: FakeInteraction) -> discord.Embed:
    assert interaction.response.deferred == {"ephemeral": False, "thinking": True}
    return interaction.last.embed


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {field.name or "": field.value or "" for field in embed.fields}


async def _seed(db: AsyncEngine, **columns: Any) -> None:
    await create_currency(db, currency_id=GUILD, symbol="GLD", name="Gold", created_by=OWNER, now=START, **columns)


async def _count_executions(db: AsyncEngine) -> int:
    async with db.connect() as conn:
        return len((await conn.execute(select(execution.c.execution_id))).all())


async def test_owner_creates_with_an_exact_rate(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    interaction = await _run(bot, make_interaction, "currency create", user_id=OWNER, name="Gold_Coin", symbol="gld", supply="1234.5", hourly_interest_rate="0.1234")
    embed = _embed(interaction)
    assert embed.title == "通貨発行成功"
    assert embed.colour == discord.Colour.green()
    assert embed.description == "新しい通貨 **Gold_Coin (GLD)** が発行されました。"
    fields = _fields(embed)
    assert fields["総供給量"] == "`1,234.500`"
    assert fields["ステーキングの利率"] == "`0.1234%/h`"
    info = await get_currency(db, GUILD)
    assert info is not None and (info.hourly_rate_ppm, info.created_by, info.supply) == (1234, OWNER, 1_234_500)
    assert await balance_of(db, OWNER, GUILD) == 1_234_500
    async with db.connect() as conn:
        key = (await conn.execute(select(execution.c.idempotency_key))).scalar_one()
    assert key == f"discord:{interaction.id}"


async def test_create_with_zero_supply_and_default_rate(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    embed = _embed(await _run(bot, make_interaction, "currency create", user_id=OWNER, name="Gold", symbol="GLD", supply="0"))
    assert embed.title == "通貨発行成功"
    assert "初期供給の転送ID" not in _fields(embed)
    info = await get_currency(db, GUILD)
    assert info is not None and (info.supply, info.hourly_rate_ppm) == (0, 0)


@pytest.mark.parametrize(
    ("user_id", "guild_id", "message"),
    [
        (ADMIN, GUILD, "通貨を発行できるのはサーバーの所有者だけです。"),
        (MEMBER, GUILD, "通貨を発行できるのはサーバーの所有者だけです。"),
        (OWNER, OTHER, NOT_IN_GUILD),
        (OWNER, None, "このコマンドはサーバー内でのみ使用できます。"),
    ],
)
async def test_create_is_for_the_owner_of_a_known_guild(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, user_id: int, guild_id: int | None, message: str) -> None:
    embed = _embed(await _run(bot, make_interaction, "currency create", user_id=user_id, guild_id=guild_id, name="Gold", symbol="GLD", supply="1"))
    assert (embed.title, embed.description) == ("エラー", message)
    assert await get_currency(db, GUILD) is None


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"name": "1Gold", "symbol": "GLD", "supply": "1"}, "通貨の名前は24文字以内"),
        ({"name": "Gold", "symbol": "G1", "supply": "1"}, "シンボルは8文字以内"),
        ({"name": "Gold", "symbol": "GLD", "supply": "1.0001"}, "小数点以下 3 桁"),
        ({"name": "Gold", "symbol": "GLD", "supply": "1", "hourly_interest_rate": "100.5"}, "利率は 0 から 100"),
        ({"name": "Gold", "symbol": "GLD", "supply": "1", "hourly_interest_rate": "0.00001"}, "利率は 0 から 100"),
    ],
)
async def test_create_rejects_bad_arguments_before_recording(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, arguments: dict[str, Any], fragment: str) -> None:
    embed = _embed(await _run(bot, make_interaction, "currency create", user_id=OWNER, **arguments))
    assert embed.title == "エラー" and embed.description is not None and fragment in embed.description
    assert await _count_executions(db) == 0


async def test_create_twice_is_refused(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    embed = _embed(await _run(bot, make_interaction, "currency create", user_id=OWNER, name="Other", symbol="OTH", supply="1"))
    assert embed.description == "このサーバーには既に通貨が存在するか、そのシンボルは使用済みです。"


async def test_info_fields(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db, hourly_rate_ppm=5000, pending_rate_ppm=1_000_000, rate_change_requested_at=START, delete_requested_at=START, delegation_enabled=True)
    await mint(db, GUILD, MEMBER, 2_500_000, now=START)
    embed = _embed(await _run(bot, make_interaction, "currency info", user_id=MEMBER, guild_id=None, symbol="gld"))
    assert embed.title == "通貨情報: Gold (GLD)"
    assert embed.colour == discord.Colour.blue()
    epoch = int(START.timestamp())
    day = 86400
    assert _fields(embed) == {
        "発行サーバーID (通貨ID)": f"`{GUILD}`",
        "作成者": f"<@{OWNER}>",
        "総供給量": "`2,500.000`",
        "ステーキング利率": "`0.5000%/h`",
        "次期ステーキング利率": "`100.0000%/h`",
        "利率変更要求日時": f"<t:{epoch}:F>",
        "利率変更の適用可能日時": f"<t:{epoch + 7 * day}:F>",
        "Mint/利率変更 放棄状態": "いいえ",
        "管理の委任": "有効",
        "削除要求日時": f"<t:{epoch}:F>",
        "削除の確定可能期間": f"<t:{epoch + 7 * day}:F> から <t:{epoch + 10 * day}:F> まで",
    }


async def test_info_defaults_to_the_guild_currency(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db, minting_renounced=True)
    fields = _fields(_embed(await _run(bot, make_interaction, "currency info", user_id=MEMBER, symbol=None)))
    assert fields["Mint/利率変更 放棄状態"] == "はい"
    assert fields["管理の委任"] == "無効"
    assert "次期ステーキング利率" not in fields and "削除要求日時" not in fields


async def test_info_without_currency(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    embed = _embed(await _run(bot, make_interaction, "currency info", user_id=MEMBER, guild_id=None, symbol=None))
    assert embed.description == "サーバー内で実行するか、通貨のシンボルを指定してください。"


async def test_owner_mints_into_own_wallet(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    embed = _embed(await _run(bot, make_interaction, "currency mint", user_id=OWNER, amount="10.5"))
    assert (embed.title, embed.description) == ("Mint成功", "`10.500 GLD` を追加発行しました。")
    assert await balance_of(db, OWNER, GUILD) == 10_500


async def test_delegated_administrator(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    denied = _embed(await _run(bot, make_interaction, "currency mint", user_id=ADMIN, amount="1"))
    assert (denied.title, denied.description) == ("エラー", MANAGER_DENIED)
    assert "実行ID" in _fields(denied)
    enabled = _embed(await _run(bot, make_interaction, "currency delegation enable", user_id=OWNER))
    assert (enabled.title, enabled.description) == ("管理の委任を有効化", "このサーバーの管理者権限を持つユーザーが、通貨 **GLD** を管理できるようになりました。")
    again = _embed(await _run(bot, make_interaction, "currency delegation enable", user_id=OWNER))
    assert again.description == "管理の委任は既に有効です。"
    assert _embed(await _run(bot, make_interaction, "currency mint", user_id=ADMIN, amount="2")).title == "Mint成功"
    assert await balance_of(db, ADMIN, GUILD) == 2000
    assert _embed(await _run(bot, make_interaction, "currency mint", user_id=MEMBER, amount="2")).description == MANAGER_DENIED
    disabled = _embed(await _run(bot, make_interaction, "currency delegation disable", user_id=OWNER))
    assert (disabled.title, disabled.description) == ("管理の委任を無効化", "通貨 **GLD** を管理できるのは、サーバーの所有者だけになりました。")
    assert _embed(await _run(bot, make_interaction, "currency mint", user_id=ADMIN, amount="2")).description == MANAGER_DENIED
    assert await balance_of(db, ADMIN, GUILD) == 2000


MANAGEMENT_COMMANDS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("currency renounce", {}),
    ("currency request-interest-change", {"rate": "1"}),
    ("currency apply-interest-change", {}),
    ("currency delete", {}),
)


@pytest.mark.parametrize(("name", "arguments"), MANAGEMENT_COMMANDS)
async def test_undelegated_administrator_is_refused(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, name: str, arguments: dict[str, Any]) -> None:
    await _seed(db, pending_rate_ppm=10_000, rate_change_requested_at=START - timedelta(days=8))
    for user_id in (ADMIN, MEMBER):
        embed = _embed(await _run(bot, make_interaction, name, user_id=user_id, **arguments))
        assert (embed.title, embed.description) == ("エラー", MANAGER_DENIED), (name, user_id)
    info = await get_currency(db, GUILD)
    assert info is not None and (info.hourly_rate_ppm, info.pending_rate_ppm, info.minting_renounced, info.delete_requested_at) == (0, 10_000, False, None)


async def test_delegated_administrator_manages_rates_renounce_and_delete(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock) -> None:
    await _seed(db, delegation_enabled=True)
    requested = _embed(await _run(bot, make_interaction, "currency request-interest-change", user_id=ADMIN, rate="1"))
    assert requested.title == "利率変更予約完了"
    clock.advance(timedelta(days=7))
    applied = _embed(await _run(bot, make_interaction, "currency apply-interest-change", user_id=ADMIN))
    assert (applied.title, applied.description) == ("利率変更適用完了", "ステーキングの利率が `1.0000%/hour` に正常に更新されました。")
    renounced = _embed(await _run(bot, make_interaction, "currency renounce", user_id=ADMIN))
    assert renounced.title == "機能放棄"
    delete_requested = _embed(await _run(bot, make_interaction, "currency delete", user_id=ADMIN))
    assert delete_requested.title == "削除要請完了"
    clock.advance(timedelta(days=7))
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=MEMBER)).description == MANAGER_DENIED
    deleted = _embed(await _run(bot, make_interaction, "currency delete", user_id=ADMIN))
    assert deleted.title == "通貨削除完了"
    assert await get_currency(db, GUILD) is None


async def test_delegation_is_for_the_owner_only(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db, delegation_enabled=True)
    for user_id in (ADMIN, MEMBER):
        embed = _embed(await _run(bot, make_interaction, "currency delegation disable", user_id=user_id))
        assert embed.description == "管理の委任を切り替えられるのはサーバーの所有者だけです。"
    info = await get_currency(db, GUILD)
    assert info is not None and info.delegation_enabled


async def test_bot_not_in_guild_and_direct_messages(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, guild_info: InMemoryGuildInfo) -> None:
    await _seed(db)
    assert _embed(await _run(bot, make_interaction, "currency mint", user_id=OWNER, guild_id=None, amount="1")).description == "このコマンドはサーバー内でのみ使用できます。"
    guild_info.remove_guild(GUILD)
    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("currency mint", {"amount": "1"}),
        ("currency renounce", {}),
        ("currency delete", {}),
        ("currency delegation enable", {}),
        ("currency request-interest-change", {"rate": "1"}),
        ("currency apply-interest-change", {}),
    )
    for name, arguments in cases:
        embed = _embed(await _run(bot, make_interaction, name, user_id=OWNER, **arguments))
        assert embed.description == NOT_IN_GUILD, name
    info = await get_currency(db, GUILD)
    assert info is not None and (info.pending_rate_ppm, info.minting_renounced, info.delete_requested_at) == (None, False, None)
    await mint(db, GUILD, MEMBER, 5000, now=START)
    burned = _embed(await _run(bot, make_interaction, "currency burn", user_id=MEMBER, amount="1"))
    assert (burned.title, burned.description) == ("Burn成功", "`1.000 GLD` を焼却しました。")
    assert await balance_of(db, MEMBER, GUILD) == 4000


async def test_guild_without_currency(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    cases: tuple[tuple[str, dict[str, Any]], ...] = (("currency mint", {"amount": "1"}), ("currency burn", {"amount": "1"}), ("currency delete", {}), ("currency apply-interest-change", {}))
    for name, arguments in cases:
        assert _embed(await _run(bot, make_interaction, name, user_id=OWNER, **arguments)).description == "このサーバーには通貨が存在しません。"


async def test_burn_needs_balance(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    assert _embed(await _run(bot, make_interaction, "currency burn", user_id=MEMBER, amount="1")).description == "焼却するための残高が不足しています。"


async def test_renounce_then_mint_and_rate_change_are_refused(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    embed = _embed(await _run(bot, make_interaction, "currency renounce", user_id=OWNER))
    assert (embed.title, embed.description) == ("機能放棄", "**GLD** のMint機能と利率変更機能を永久に放棄しました。この操作は取り消せません。")
    assert _embed(await _run(bot, make_interaction, "currency renounce", user_id=OWNER)).description == "この通貨の機能は既に放棄されています。"
    assert _embed(await _run(bot, make_interaction, "currency mint", user_id=OWNER, amount="1")).description == "この通貨のMint機能は放棄されています。"
    assert _embed(await _run(bot, make_interaction, "currency request-interest-change", user_id=OWNER, rate="1")).description == "この通貨の利率変更は放棄されています。"


async def test_rate_change_request_and_apply(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock) -> None:
    await _seed(db, hourly_rate_ppm=100)
    bad = _embed(await _run(bot, make_interaction, "currency request-interest-change", user_id=OWNER, rate="100.0001"))
    assert bad.description is not None and bad.description.startswith("利率は 0 から 100")
    applied_early = _embed(await _run(bot, make_interaction, "currency apply-interest-change", user_id=OWNER))
    assert applied_early.description == "予約されている利率変更はありません。"
    requested = _embed(await _run(bot, make_interaction, "currency request-interest-change", user_id=OWNER, rate="2.5"))
    apply_at = int((START + timedelta(days=7)).timestamp())
    assert requested.title == "利率変更予約完了"
    assert requested.description == f"ステーキングの利率を`2.5000%/hour`に変更するリクエストを受け付けました。\nこの変更は <t:{apply_at}:F> (<t:{apply_at}:R>) 以降に適用可能になります。"
    assert (
        _embed(await _run(bot, make_interaction, "currency request-interest-change", user_id=OWNER, rate="3")).description == "既に予約されている利率変更があります。適用してから再度予約してください。"
    )
    clock.advance(timedelta(days=6))
    locked = _embed(await _run(bot, make_interaction, "currency apply-interest-change", user_id=OWNER))
    assert locked.description == f"利率変更の予約から7日間が経過していないため、まだ適用できません。\n適用可能になる日時: <t:{apply_at}:F> (<t:{apply_at}:R>)"
    assert "実行ID" in _fields(locked)
    clock.advance(timedelta(days=1))
    applied = _embed(await _run(bot, make_interaction, "currency apply-interest-change", user_id=OWNER))
    assert (applied.title, applied.description) == ("利率変更適用完了", "ステーキングの利率が `2.5000%/hour` に正常に更新されました。")
    info = await get_currency(db, GUILD)
    assert info is not None and (info.hourly_rate_ppm, info.pending_rate_ppm) == (25_000, None)


async def test_delete_toggles_request_and_finalize(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock) -> None:
    await _seed(db)
    await create_currency(db, currency_id=OTHER, symbol="SLV", created_by=OWNER, now=START)
    await mint(db, GUILD, MEMBER, 7000, now=START)
    await create_pool(db, user_id=MEMBER, currency_x=GUILD, currency_y=OTHER, amount_x=50_000, amount_y=50_000, now=START)
    epoch = int(START.timestamp())
    day = 86400
    requested = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    assert requested.title == "削除要請完了"
    assert requested.description == "通貨 **GLD** の削除要請を受け付けました。\n**7日後から10日後まで**の間に再度このコマンドを実行すると、削除が確定します。"
    assert _fields(requested)["確定可能期間"] == f"<t:{epoch + 7 * day}:F> から <t:{epoch + 10 * day}:F> まで"
    clock.advance(timedelta(days=3))
    early = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    assert early.title == "エラー"
    assert early.description == f"削除の確定には、削除要請から7日間が経過している必要があります。\n確定可能になる日時: <t:{epoch + 7 * day}:F> (<t:{epoch + 7 * day}:R>)"
    clock.advance(timedelta(days=4))
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=MEMBER)).description == MANAGER_DENIED
    deleted = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    assert deleted.title == "通貨削除完了"
    assert deleted.description == "通貨 **GLD** を完全に削除しました。\n2件の残高と0件のステークを焼却しました。"
    fields = _fields(deleted)
    assert fields["焼却した総額"] == "`57.000 GLD`"
    assert fields["解散したプール"] == "`1`"
    assert await get_currency(db, GUILD) is None
    assert await supply_of(db, GUILD) == 0


LAPSED = "削除要請から10日以上が経過したため、この削除要請は無効になりました。\n再度削除を要請してください。"


async def _finalize_executions(db: AsyncEngine) -> list[tuple[str, Any]]:
    async with db.connect() as conn:
        rows = (await conn.execute(select(execution.c.status, execution.c.output).where(execution.c.type == ExecutionType.CURRENCY_DELETE_FINALIZE).order_by(execution.c.execution_id))).all()
    return [(row.status, row.output) for row in rows]


async def test_delete_one_second_before_seven_days_is_too_early(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock) -> None:
    await _seed(db)
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER)).title == "削除要請完了"
    clock.advance(timedelta(days=7) - timedelta(seconds=1))
    early = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    epoch = int(START.timestamp()) + 7 * 86400
    assert (early.title, early.description) == ("エラー", f"削除の確定には、削除要請から7日間が経過している必要があります。\n確定可能になる日時: <t:{epoch}:F> (<t:{epoch}:R>)")
    info = await get_currency(db, GUILD, now=clock.now())
    assert info is not None and info.delete_requested_at == START


@pytest.mark.parametrize("elapsed", [timedelta(days=7), timedelta(days=8, hours=12), timedelta(days=10)])
async def test_delete_finalizes_from_seven_to_ten_days(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock, elapsed: timedelta) -> None:
    await _seed(db)
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER)).title == "削除要請完了"
    clock.advance(elapsed)
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER)).title == "通貨削除完了"
    assert await get_currency(db, GUILD) is None


async def test_lapsed_delete_request_is_cancelled_then_requested_again(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, clock: FixedClock) -> None:
    await _seed(db)
    assert _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER)).title == "削除要請完了"
    clock.advance(timedelta(days=10, seconds=1))
    info = await get_currency(db, GUILD, now=clock.now())
    assert info is not None and (info.delete_requested_at, info.delete_request_lapsed) == (None, True)
    lapsed = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    assert (lapsed.title, lapsed.description) == ("エラー", LAPSED)
    [(status, output)] = await _finalize_executions(db)
    assert status == ExecutionStatus.FAILED
    assert output["error"]["code"] == RequestExpired.code
    info = await get_currency(db, GUILD, now=clock.now())
    assert info is not None and (info.delete_requested_at, info.delete_request_lapsed, info.deleted_at) == (None, False, None)
    clock.advance(timedelta(minutes=1))
    again = _embed(await _run(bot, make_interaction, "currency delete", user_id=OWNER))
    assert again.title == "削除要請完了"
    epoch = int(clock.now().timestamp())
    assert _fields(again)["確定可能期間"] == f"<t:{epoch + 7 * 86400}:F> から <t:{epoch + 10 * 86400}:F> まで"
    info = await get_currency(db, GUILD, now=clock.now())
    assert info is not None and (info.delete_requested_at, info.delete_request_lapsed) == (clock.now(), False)
    assert len(await _finalize_executions(db)) == 1


def test_command_parameters(bot: RapidWireBot) -> None:
    create = find_command(bot.tree, "currency create")
    assert [(param.name, param.required) for param in create.parameters] == [("name", True), ("symbol", True), ("supply", True), ("hourly_interest_rate", False)]
    assert find_command(bot.tree, "currency info").parameters[0].autocomplete
    assert [param.name for param in find_command(bot.tree, "currency request-interest-change").parameters] == ["rate"]
