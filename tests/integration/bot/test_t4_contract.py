"""The /contract commands on the test database: registration from an attachment and the binary round trip, calls with typed arguments, payment, revert and out-of-gas through the presenter, function and args autocomplete, and the management commands."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import discord
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.bot import contract_args
from rapidwire.bot.client import RapidWireBot
from rapidwire.bot.commands import contract as contract_command
from rapidwire.bot.contract_args import parse_call_args, symbol_resolver
from rapidwire.bot.embeds import timestamp_full, timestamp_relative
from rapidwire.bot.errors import UserInputError
from rapidwire.config import Settings
from rapidwire.contract.compiler import compile_source
from rapidwire.contract.types import CURRENCY, STR
from rapidwire.contract.values import Currency
from rapidwire.core import contracts
from rapidwire.core.types import ExecutionStatus
from rapidwire.db.tables import ContractStatus
from tests.integration.bot.conftest import BOT_TEST_DISCORD_TOKEN, BOT_TEST_PASSWORD, MakeInteraction
from tests.integration.contract_runtime_helpers import PRELUDE, RAPID, executions
from tests.integration.factories import balance_of, create_currency, mint
from tests.unit.bot.fakes import FakeInteraction, FakeUser, autocomplete, invoke

OWNER = 7_100_001
CALLER = 7_100_002
STRANGER = 7_100_003
GUILD = 7_200_001

SOURCE = """
@public
def mix(amount: int, to: Account, cur: Currency, note: str, flag: bool) -> str:
    return str(amount) + ":" + str(int(to)) + ":" + str(int(cur)) + ":" + note + ":" + str(int(flag))

@public
def ping(x: int) -> int:
    return x + 1

@public
def say(text: str) -> str:
    return text

@public
def check(ok: bool) -> bool:
    if not ok:
        revert("not ok")
    return True

@public
def spin() -> int:
    n: int = 0
    while n >= 0:
        n = n + 1
    return n

@public
def buy() -> str:
    return "paid " + str(paid.amount)
"""

BINARY = compile_source(PRELUDE + SOURCE)


class FakeAttachment:
    """A discord.Attachment whose declared size may differ from its data, counting downloads."""

    def __init__(self, data: bytes, *, size: int | None = None, filename: str = "contract.rwc") -> None:
        self.data = data
        self.size = len(data) if size is None else size
        self.filename = filename
        self.reads = 0

    async def read(self, *, use_cached: bool = False) -> bytes:
        self.reads += 1
        return self.data


@pytest.fixture
def bot_settings() -> Settings:
    return Settings(_env_file=None, db_password=BOT_TEST_PASSWORD, discord_token=BOT_TEST_DISCORD_TOKEN, gas_currency_id=RAPID, max_gas=100_000)


async def _seed(db: AsyncEngine, *, deploy: bool = True) -> None:
    await create_currency(db, currency_id=RAPID, symbol="RAPID", name="Rapid")
    await create_currency(db, currency_id=GUILD, symbol="GLD", name="Gold")
    await mint(db, RAPID, CALLER, 1_000_000)
    await mint(db, GUILD, CALLER, 1_000_000)
    if deploy:
        await contracts.register_contract(db, account_id=OWNER, binary=BINARY, max_size=8000)


def _user(user_id: int) -> FakeUser:
    """The user option value of a user that is not a member of the interaction's guild."""
    return FakeUser(user_id)


def _fields(interaction: FakeInteraction) -> dict[str, str]:
    return {str(field.name): str(field.value) for field in interaction.last.embed.fields}


def _error(interaction: FakeInteraction) -> str:
    embed = interaction.last.embed
    assert embed.title == "エラー"
    return str(embed.description)


async def _call(bot: RapidWireBot, make_interaction: MakeInteraction, **kwargs: Any) -> FakeInteraction:
    interaction = make_interaction(user_id=CALLER, guild_id=GUILD)
    arguments: dict[str, Any] = {"user": _user(OWNER), "args": None, "pay": None, "currency": None, "gas": None}
    arguments.update(kwargs)
    return await invoke(bot.tree, "contract call", interaction, **arguments)


async def test_set_refuses_too_large_attachment_before_download(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    attachment = FakeAttachment(BINARY, size=8001)
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=attachment, lock_hours=None)
    assert _error(interaction) == "コントラクトのバイナリは 8,000 バイト以下にしてください (添付ファイルは 8,001 バイトです)。"
    empty = FakeAttachment(b"", size=0)
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=empty, lock_hours=None)
    assert _error(interaction) == "添付ファイルが空です。"
    assert attachment.reads == 0 and empty.reads == 0
    assert await contracts.get_contract(db, OWNER) is None


async def test_set_invalid_binary_shows_verifier_description(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=FakeAttachment(b"not a contract"), lock_hours=None)
    assert _error(interaction) == "コントラクトのバイナリが正しくありません。"
    assert "詳細" in _fields(interaction)


async def test_set_and_get_round_trip(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db, deploy=False)
    attachment = FakeAttachment(BINARY)
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=attachment, lock_hours=24)
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("成功", "コントラクトを正常に設定しました。")
    fields = _fields(interaction)
    sha = hashlib.sha256(BINARY).hexdigest()
    assert fields["版"] == "`v1`"
    assert fields["SHA-256"] == f"`{sha}`"
    assert fields["サイズ"] == f"`{len(BINARY):,} バイト`"
    info = await contracts.get_contract(db, OWNER)
    assert info is not None and info.locked_until is not None
    lock_text = f"{timestamp_full(info.locked_until)} ({timestamp_relative(info.locked_until)})"
    assert fields["ロック期限"] == lock_text
    assert "`mix(int, Account, Currency, str, bool) -> str`" in fields["公開関数"]

    interaction = await invoke(bot.tree, "contract get", make_interaction(user_id=STRANGER), user=_user(OWNER))
    sent = interaction.last
    assert sent.embed.title == "コントラクト詳細"
    file = sent.files[0]
    assert file.filename == f"contract-{OWNER}-v1.rwc"
    data = file.fp.read()
    assert data == BINARY
    fields = _fields(interaction)
    assert fields["SHA-256"] == f"`{hashlib.sha256(data).hexdigest()}`" == f"`{sha}`"
    assert fields["版"] == "`v1`" and fields["状態"] == "有効" and fields["ロック期限"] == lock_text
    assert fields["サイズ"] == f"`{len(BINARY):,} バイト`"
    assert fields["公開関数"].splitlines()[0] == "`buy() -> str`"


async def test_get_and_info_without_contract(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    interaction = await invoke(bot.tree, "contract get", make_interaction(user_id=OWNER), user=None)
    assert (interaction.last.embed.title, interaction.last.embed.description) == ("コントラクト情報", "現在、コントラクトは設定されていません。")
    interaction = await invoke(bot.tree, "contract info", make_interaction(user_id=OWNER), user=_user(STRANGER))
    assert interaction.last.embed.description == f"<@{STRANGER}> はコントラクトを設定していません。"


async def test_info_shows_state(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await contracts.register_contract(db, account_id=OWNER, binary=BINARY, max_size=8000)
    interaction = await invoke(bot.tree, "contract info", make_interaction(user_id=OWNER), user=None)
    fields = _fields(interaction)
    assert interaction.last.embed.title == "コントラクト情報"
    assert fields["状態"] == "有効"
    assert fields["現在の版"] == "`v2` (全 2 版)"
    assert fields["ロック期限"] == "ロックされていません"
    assert fields["ストレージ"] == "`0 / 2,000` キー、`0 / 262,144` バイト"
    assert len(fields["公開関数"].splitlines()) == 6
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert fields["最終更新"] == f"{timestamp_full(info.updated_at)} (実行ID `{info.updated_execution_id}`)"


async def test_call_success_with_typed_args(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, function="mix", args=f'5, <@{STRANGER}>, gld, "a, b", true')
    embed = interaction.last.embed
    assert (embed.title, embed.description) == ("コントラクト実行完了", f"<@{OWNER}> のコントラクトを実行しました。")
    fields = _fields(interaction)
    assert fields["戻り値 (`str`)"] == f"```5:{STRANGER}:{GUILD}:a, b:1```"
    used, limit = fields["使用ガス"].strip("`").split(" / ")
    assert limit == "128" and 0 < int(used) <= 128
    assert fields["ガス代"] == f"`0.{int(used):03d} RAPID`"
    assert await balance_of(db, CALLER, RAPID) == 1_000_000 - int(used)
    rows = await executions(db)
    assert fields["実行ID"] == f"`{rows[-1]['execution_id']}`"
    assert rows[-1]["input"]["args"] == [
        {"type": "int", "value": "5"},
        {"type": "Account", "value": str(STRANGER)},
        {"type": "Currency", "value": str(GUILD)},
        {"type": "str", "value": "a, b"},
        {"type": "bool", "value": True},
    ]

    interaction = await _call(bot, make_interaction, function=" ping ", args="41", gas=1000)
    fields = _fields(interaction)
    assert fields["戻り値 (`int`)"] == "`42`"
    assert fields["使用ガス"].endswith(" / 1,000`")
    interaction = await _call(bot, make_interaction, function="say", args="hello, world")
    assert _fields(interaction)["戻り値 (`str`)"] == "```hello, world```"
    interaction = await _call(bot, make_interaction, function="check", args="true")
    assert _fields(interaction)["戻り値 (`bool`)"] == "`true`"


async def test_call_pay_attaches_guild_currency_by_default(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, function="buy", pay="1.5")
    fields = _fields(interaction)
    assert fields["戻り値 (`str`)"] == "```paid 1500```"
    assert fields["支払い"] == "`1.500 GLD`"
    assert await balance_of(db, OWNER, GUILD) == 1500
    interaction = await _call(bot, make_interaction, function="buy", pay="2", currency="rapid")
    assert _fields(interaction)["支払い"] == "`2.000 RAPID`"
    assert await balance_of(db, OWNER, RAPID) == 2000


async def test_call_revert_and_out_of_gas_through_presenter(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, function="check", args="false")
    assert _error(interaction) == "取引はコントラクトによってキャンセルされました。"
    fields = _fields(interaction)
    assert fields["種別"] == "取り消し (`REVERTED`)"
    assert fields["詳細"] == "```not ok```"
    rows = await executions(db)
    assert rows[-1]["status"] == ExecutionStatus.REVERTED.value
    assert fields["実行ID"] == f"`{rows[-1]['execution_id']}`"

    before = await balance_of(db, CALLER, RAPID)
    interaction = await _call(bot, make_interaction, function="spin", gas=50)
    assert _error(interaction) == "コントラクトの実行中にガスが不足しました。"
    fields = _fields(interaction)
    assert fields["種別"] == "ガス不足 (`OUT_OF_GAS`)"
    assert fields["使用ガス"] == "`50 / 50`"
    assert fields["ガス代"] == "`0.050 RAPID`"
    assert await balance_of(db, CALLER, RAPID) == before - 50


async def test_call_gas_prepay_shortfall_without_payment(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await mint(db, RAPID, STRANGER, 999)
    interaction = make_interaction(user_id=STRANGER, guild_id=GUILD)
    interaction = await invoke(bot.tree, "contract call", interaction, user=_user(OWNER), function="ping", args="1", pay=None, currency=None, gas=1000)
    assert _error(interaction) == "残高が不足しています。"
    fields = _fields(interaction)
    assert fields["種別"] == "呼び出しの失敗 (`CALL_FAILED`)"
    assert fields["理由"] == "ガス代の前払いまたは添付する額の残高が不足しています"
    assert (fields["使用ガス"], fields["ガス代"]) == ("`0 / 1,000`", "`0.000 RAPID`")
    rows = await executions(db)
    assert fields["実行ID"] == f"`{rows[-1]['execution_id']}`"
    assert await balance_of(db, STRANGER, RAPID) == 999


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"function": "nope"}, "関数 `nope` はこのコントラクトの公開関数ではありません。"),
        ({"function": "ping", "args": "x"}, "引数 1 (int): 整数を指定してください (例: 100、-5)。+ の符号、空白、先頭の余分な 0 は使えません。"),
        ({"function": "ping"}, "引数の数が合いません。`ping(int) -> int` には 1 個の引数が必要ですが、0 個指定されました。"),
        ({"function": "mix", "args": '1, <@5>, NOPE, "x", true'}, "引数 3 (Currency): 通貨 `NOPE` が見つかりませんでした。"),
        ({"function": "buy", "currency": "GLD"}, "currency は pay と一緒に指定してください。"),
        ({"function": "buy", "pay": "1", "currency": "NOPE"}, "通貨 `NOPE` が見つかりませんでした。シンボルを正しく指定してください。"),
        ({"function": "buy", "pay": "0"}, "金額は 0.001 以上を指定してください。"),
        ({"function": "ping", "args": "1", "gas": 100_001}, "ガスの上限は 1 から 100,000 の範囲で指定してください。"),
    ],
)
async def test_call_argument_errors(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, kwargs: dict[str, Any], message: str) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, **kwargs)
    assert _error(interaction) == message
    assert await executions(db) == []


async def test_call_without_contract_or_own_payment(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, user=_user(STRANGER), function="ping", args="1")
    assert _error(interaction) == "このアカウントにはコントラクトが登録されていません。"
    own = make_interaction(user_id=OWNER, guild_id=GUILD)
    interaction = await invoke(bot.tree, "contract call", own, user=_user(OWNER), function="buy", args=None, pay="1", currency=None, gas=None)
    assert _error(interaction) == "自分のコントラクトを呼び出すときは支払いを添付できません。"
    assert await executions(db) == []


async def test_call_paused_contract_is_a_recorded_failure(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await contracts.pause_contract(db, account_id=OWNER)
    interaction = await _call(bot, make_interaction, function="ping", args="1")
    assert _error(interaction) == "コントラクトは一時停止中です。"
    assert _fields(interaction)["理由"] == "呼び出し先のコントラクトが停止中です"


async def test_currency_symbols_resolve_against_the_database(db: AsyncEngine) -> None:
    await _seed(db, deploy=False)
    function = contracts.PublicFunction("f", (CURRENCY, CURRENCY), STR)
    values = await parse_call_args("gld, Rapid", function, resolve_currency=symbol_resolver(db))
    assert values == [Currency(GUILD), Currency(RAPID)]
    assert all(type(value) is Currency for value in values)
    with pytest.raises(UserInputError):
        await parse_call_args("gld, zzz", function, resolve_currency=symbol_resolver(db))


async def test_function_autocomplete(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = make_interaction(user_id=CALLER, namespace={"user": discord.Object(id=OWNER)})
    choices = await autocomplete(bot.tree, "contract call", "function", interaction, "P")
    assert [(choice.name, choice.value) for choice in choices] == [("ping(int) -> int", "ping"), ("spin() -> int", "spin")]
    choices = await autocomplete(bot.tree, "contract call", "function", interaction, "i")
    assert [(choice.name, choice.value) for choice in choices] == [("mix(int, Account, Currency, str, bool) -> str", "mix"), ("ping(int) -> int", "ping"), ("spin() -> int", "spin")]
    assert len(await autocomplete(bot.tree, "contract call", "function", interaction, "")) == 6
    for namespace in ({}, {"user": discord.Object(id=STRANGER)}, {"user": "abc"}):
        assert await autocomplete(bot.tree, "contract call", "function", make_interaction(namespace=namespace), "") == []


async def test_args_autocomplete(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)

    async def choices(function: str, current: str, user: int = OWNER) -> list[tuple[str, str]]:
        interaction = make_interaction(user_id=CALLER, namespace={"user": discord.Object(id=user), "function": function})
        found = await autocomplete(bot.tree, "contract call", "args", interaction, current)
        assert len(found) <= 25 and all(len(choice.name) <= 100 and len(str(choice.value)) <= 100 for choice in found)
        return [(choice.name, str(choice.value)) for choice in found]

    template = "<int>, <Account>, <Currency>, <str>, <bool>"
    assert await choices("mix", "") == [(template, template)]
    assert await choices("mix", "5") == [("5, <Account>, <Currency>, <str>, <bool>", "5")]
    assert await choices("mix", "5, <@1>, g") == [("5, <@1>, g, <str>, <bool>", "5, <@1>, g"), ("5, <@1>, GLD (Gold)", "5, <@1>, GLD")]
    assert await choices("mix", "5, <@1>,") == [
        ("5, <@1>, <Currency>, <str>, <bool>", "5, <@1>,"),
        ("5, <@1>, GLD (Gold)", "5, <@1>, GLD"),
        ("5, <@1>, RAPID (Rapid)", "5, <@1>, RAPID"),
    ]
    assert await choices("mix", '5, <@1>, GLD, "x, y", T') == [('5, <@1>, GLD, "x, y", T', '5, <@1>, GLD, "x, y", T'), ('5, <@1>, GLD, "x, y", true', '5, <@1>, GLD, "x, y", true')]
    assert await choices("check", "") == [("<bool>", "<bool>"), ("true", "true"), ("false", "false")]
    assert await choices("ping", "1, 2") == [("引数が多すぎます: ping(int) -> int", "1, 2")]
    assert await choices("buy", "") == []
    assert await choices("buy", "x") == [("この関数は引数を取りません: buy() -> str", "x")]
    assert await choices("say", "hi, there") == [("hi, there", "hi, there")]
    assert await choices("say", "") == []
    assert await choices("nope", "") == []
    assert await choices("ping", "", user=STRANGER) == []
    assert await choices("ping", "1" * 101) == []
    long = await choices("mix", "1" * 90 + ", <@1>")
    assert long[0][0].startswith("...") and long[0][0].endswith("<Currency>, <str>, <bool>")


async def test_autocomplete_is_empty_when_the_lookup_fails(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(contract_args, "list_public_functions", broken)
    interaction = make_interaction(user_id=CALLER, namespace={"user": discord.Object(id=OWNER), "function": "ping"})
    assert await autocomplete(bot.tree, "contract call", "function", interaction, "") == []
    assert await autocomplete(bot.tree, "contract call", "args", interaction, "1") == []


async def test_call_with_the_placeholder_template_is_refused(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await _call(bot, make_interaction, function="say", args="<str>")
    assert _error(interaction) == "`<str>` は引数の書式の例です。それぞれを実際の値に置き換えてください。"
    assert await executions(db) == []


async def test_management_commands(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    async def run(name: str, user_id: int = OWNER, **kwargs: Any) -> FakeInteraction:
        return await invoke(bot.tree, f"contract {name}", make_interaction(user_id=user_id), **kwargs)

    for name, kwargs in (("pause", {}), ("resume", {}), ("delete", {}), ("lock", {"hours": 1})):
        assert _error(await run(name, **kwargs)) == "このアカウントにはコントラクトが登録されていません。"
    await _seed(db)

    interaction = await run("pause")
    assert interaction.last.embed.description == "コントラクトを一時停止しました。"
    assert _error(await run("pause")) == "コントラクトは既に一時停止中です。"
    assert _error(await run("lock", hours=1)) == "一時停止中のコントラクトはロックできません。先に再開してください。"
    assert (await run("resume")).last.embed.description == "コントラクトを再開しました。"
    assert _error(await run("resume")) == "コントラクトは一時停止中ではありません。"

    interaction = await run("lock", hours=48)
    assert interaction.last.embed.description == "コントラクトをロックしました。"
    first = await contracts.get_contract(db, OWNER)
    assert first is not None and first.locked_until is not None
    assert _fields(interaction)["ロック期限"] == f"{timestamp_full(first.locked_until)} ({timestamp_relative(first.locked_until)})"
    assert "以前のロック期限" not in _fields(interaction)
    interaction = await run("lock", hours=72)
    second = await contracts.get_contract(db, OWNER)
    assert second is not None and second.locked_until is not None
    assert _fields(interaction)["ロック期限"] == f"{timestamp_full(second.locked_until)} ({timestamp_relative(second.locked_until)})"
    assert _fields(interaction)["以前のロック期限"] == timestamp_full(first.locked_until)
    failed = await run("lock", hours=1)
    assert _error(failed) == f"ロック期限は延長のみできます。現在のロック期限 ({timestamp_full(second.locked_until)}) より後になる期間を指定してください。"
    locked = "コントラクトはロック中のため、ロック期限まで更新・一時停止・削除できません。"
    assert _error(await run("pause")) == locked
    assert _error(await run("delete")) == locked
    assert _error(await run("resume")) == "コントラクトはロック中のため、ロック期限まで状態を変更できません。"
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=FakeAttachment(BINARY), lock_hours=None)
    assert _error(interaction) == locked

    # Commands of another user act on that user's own account only.
    assert _error(await run("pause", user_id=STRANGER)) == "このアカウントにはコントラクトが登録されていません。"
    info = await contracts.get_contract(db, OWNER)
    assert info is not None and info.status is ContractStatus.ACTIVE


async def test_delete_and_revive_through_set(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    interaction = await invoke(bot.tree, "contract delete", make_interaction(user_id=OWNER))
    assert interaction.last.embed.description == "コントラクトを削除しました。"
    assert _fields(interaction)["削除した版"] == "`v1`"
    assert _error(await invoke(bot.tree, "contract delete", make_interaction(user_id=OWNER))) == "このアカウントにはコントラクトが登録されていません。"
    interaction = await invoke(bot.tree, "contract get", make_interaction(user_id=OWNER), user=None)
    assert interaction.last.embed.description == "現在、コントラクトは設定されていません。"
    interaction = await invoke(bot.tree, "contract info", make_interaction(user_id=OWNER), user=None)
    assert _fields(interaction)["状態"] == "削除済み" and _fields(interaction)["公開関数"] == "なし"
    interaction = await _call(bot, make_interaction, function="ping", args="1")
    assert _error(interaction) == "このアカウントにはコントラクトが登録されていません。"

    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=FakeAttachment(BINARY), lock_hours=None)
    assert _fields(interaction)["版"] == "`v2` (削除済みのコントラクトを復元しました)"


async def test_set_while_paused_reports_the_failed_lock(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction) -> None:
    await _seed(db)
    await contracts.pause_contract(db, account_id=OWNER)
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=FakeAttachment(BINARY), lock_hours=5)
    fields = _fields(interaction)
    assert fields["版"] == "`v2`" and fields["状態"] == "一時停止中"
    assert fields["ロック期限"] == "ロックできませんでした: 一時停止中のコントラクトはロックできません。先に再開してください。"


async def test_set_reports_an_unexpected_lock_failure(db: AsyncEngine, bot: RapidWireBot, make_interaction: MakeInteraction, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(contract_command, "lock_contract", broken)
    caplog.set_level(logging.ERROR, logger="rapidwire.bot")
    interaction = await invoke(bot.tree, "contract set", make_interaction(user_id=OWNER), binary=FakeAttachment(BINARY), lock_hours=5)
    assert interaction.last.embed.description == "コントラクトを正常に設定しました。"
    fields = _fields(interaction)
    assert fields["版"] == "`v1`"
    assert fields["ロック期限"] == "ロックできませんでした: 予期せぬエラーが発生しました。`/contract lock` で再度お試しください。"
    assert "connection lost" not in str(interaction.last.embed.to_dict())
    assert [record.getMessage() for record in caplog.records if record.name == "rapidwire.bot"] == ["the lock after a contract registration failed"]
    info = await contracts.get_contract(db, OWNER)
    assert info is not None and info.locked_until is None
