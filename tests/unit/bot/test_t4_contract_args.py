"""The /contract call argument syntax, the args hint, the function choices and the user option read from an autocomplete namespace."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from rapidwire.bot.contract_args import CHOICE_TEXT_MAX, args_hint, function_choices, namespace_account_id, parse_call_args
from rapidwire.bot.errors import UserInputError
from rapidwire.contract.types import ACCOUNT, BOOL, CURRENCY, INT, STR, Type, dict_of, list_of
from rapidwire.contract.values import Account, Currency, Value
from rapidwire.core.contracts import PublicFunction

SYMBOLS = {"RAPID": 11, "GOLD-X": 12}
MIXED = PublicFunction("mix", (INT, STR, BOOL, ACCOUNT, CURRENCY), STR)


async def _resolve(symbol: str) -> int | None:
    return SYMBOLS.get(symbol)


def _fn(*params: Type) -> PublicFunction:
    return PublicFunction("f", params, STR)


async def parse(text: str | None, function: PublicFunction) -> list[Value]:
    return await parse_call_args(text, function, resolve_currency=_resolve)


async def error_of(text: str | None, function: PublicFunction) -> str:
    with pytest.raises(UserInputError) as caught:
        await parse(text, function)
    return caught.value.message


async def test_every_scalar_type() -> None:
    assert await parse('-5, "a, b", TRUE, <@!42>, rapid', MIXED) == [-5, "a, b", True, Account(42), Currency(11)]
    assert await parse('0,"",false,<@42>,12', MIXED) == [0, "", False, Account(42), Currency(12)]
    values = await parse(' 7 , "x" , false , 42 , gold-x ', MIXED)
    assert values == [7, "x", False, Account(42), Currency(12)]
    assert type(values[0]) is int and type(values[2]) is bool


async def test_quotes_keep_commas_brackets_and_escapes() -> None:
    fn = _fn(STR, STR)
    assert await parse(r'"he said \"hi\", ok", "[1, 2]"', fn) == ['he said "hi", ok', "[1, 2]"]
    assert await parse(r'"line\nnext", "あ"', fn) == ["line\nnext", "あ"]


async def test_single_str_parameter_may_be_unquoted() -> None:
    fn = _fn(STR)
    assert await parse("hello, world", fn) == ["hello, world"]
    assert await parse('"quoted, text"', fn) == ["quoted, text"]
    assert await parse("  spaced  ", fn) == ["spaced"]
    assert "引数 1 (str)" in await error_of('"a" b', fn)
    with pytest.raises(UserInputError):
        await parse("x" * 4097, fn)


@pytest.mark.parametrize("text", ["-0", "007", "+1", "1.5", "1e3", chr(0xFF11), str(2**256), str(-(2**256)), "0x10", ""])
async def test_bad_ints(text: str) -> None:
    assert (await error_of(f"{text}, 1", _fn(INT, INT))).startswith("引数 1 (int): ")


async def test_int_limits() -> None:
    assert await parse(f"{2**256 - 1}, {-(2**256) + 1}", _fn(INT, INT)) == [2**256 - 1, -(2**256) + 1]


async def test_strings_must_be_quoted_with_several_parameters() -> None:
    assert (await error_of("abc, 1", _fn(STR, INT))).startswith("引数 1 (str): 文字列は")
    assert (await error_of('"a" "b", 1', _fn(STR, INT))).startswith("引数 1 (str): ")
    assert (await error_of('"' + "x" * 4097 + '", 1', _fn(STR, INT))) == "引数 1 (str): 文字列は 4096 文字以内にしてください。"


async def test_bools() -> None:
    assert await parse("False, tRUE", _fn(BOOL, BOOL)) == [False, True]
    assert await error_of("yes, true", _fn(BOOL, BOOL)) == "引数 1 (bool): true または false を指定してください。"
    assert await error_of("1, true", _fn(BOOL, BOOL)) == "引数 1 (bool): true または false を指定してください。"


async def test_account_forms() -> None:
    fn = _fn(ACCOUNT, ACCOUNT, ACCOUNT)
    assert await parse("<@123>, <@!456>, 789", fn) == [Account(123), Account(456), Account(789)]
    assert await parse(f"-3, {2**63 - 1}, <@{2**63 - 1}>", fn) == [Account(-3), Account(2**63 - 1), Account(2**63 - 1)]
    for bad in ("<@0>", "<@&5>", "<#5>", "@user", f"{2**63}", f"<@{2**63}>"):
        assert (await error_of(f"{bad}, 1, 2", fn)).startswith("引数 1 (Account): "), bad


async def test_currency_by_symbol_or_id() -> None:
    fn = _fn(CURRENCY)
    assert await parse("rapid", fn) == [Currency(11)]
    assert await parse("Gold-X", fn) == [Currency(12)]
    assert await parse("99", fn) == [Currency(99)]
    assert await error_of("nope", fn) == "引数 1 (Currency): 通貨 `NOPE` が見つかりませんでした。"
    assert await error_of("R`X", fn) == "引数 1 (Currency): 通貨のシンボルまたは通貨の ID を指定してください。"
    assert await error_of("TOOLONGSYM", fn) == "引数 1 (Currency): 通貨のシンボルまたは通貨の ID を指定してください。"


async def test_container_parameters_are_refused() -> None:
    assert await error_of("[1]", _fn(list_of(INT))) == "引数 1 (list[int]): この型の引数は指定できません。"
    assert await error_of('{"a": 1}', _fn(dict_of(STR, INT))) == f"引数 1 ({dict_of(STR, INT)}): この型の引数は指定できません。"


async def test_open_quote() -> None:
    assert await error_of('"open, 1', _fn(STR, INT)) == '引数を読み取れません: 閉じていない二重引用符 (") があります。'


async def test_placeholder_template_is_refused() -> None:
    assert await error_of("<str>", _fn(STR)) == "`<str>` は引数の書式の例です。それぞれを実際の値に置き換えてください。"
    assert await error_of(" <int>, <Account> ", _fn(INT, ACCOUNT)) == "`<int>, <Account>` は引数の書式の例です。それぞれを実際の値に置き換えてください。"
    assert await parse('"<str>"', _fn(STR)) == ["<str>"]


async def test_argument_count() -> None:
    fn = PublicFunction("pay", (INT, ACCOUNT), STR)
    assert await error_of("1", fn) == "引数の数が合いません。`pay(int, Account) -> str` には 2 個の引数が必要ですが、1 個指定されました。"
    assert await error_of("1, 2, 3", fn) == "引数の数が合いません。`pay(int, Account) -> str` には 2 個の引数が必要ですが、3 個指定されました。"
    assert await error_of(None, fn) == "引数の数が合いません。`pay(int, Account) -> str` には 2 個の引数が必要ですが、0 個指定されました。"
    assert await error_of("1,", fn) == "引数 2 (Account): 値がありません。"
    none = PublicFunction("ping", (), INT)
    assert await parse(None, none) == []
    assert await parse("   ", none) == []
    assert await error_of("1", none) == "引数の数が合いません。`ping() -> int` には 0 個の引数が必要ですが、1 個指定されました。"


def test_args_hint_follows_the_typing() -> None:
    fn = PublicFunction("pay", (INT, ACCOUNT, BOOL), STR)
    hint = args_hint(fn, "")
    assert (hint.index, hint.prefix, hint.partial, hint.template) == (0, "", "", "<int>, <Account>, <bool>")
    hint = args_hint(fn, "100")
    assert (hint.index, hint.prefix, hint.partial, hint.template) == (0, "", "100", "100, <Account>, <bool>")
    hint = args_hint(fn, "100,")
    assert (hint.index, hint.prefix, hint.partial, hint.template) == (1, "100, ", "", "100, <Account>, <bool>")
    hint = args_hint(fn, "100, <@5>,  tr")
    assert (hint.index, hint.prefix, hint.partial, hint.template) == (2, "100, <@5>,  ", "tr", "100, <@5>, tr")
    assert args_hint(fn, "1, 2, 3, 4").index == 3
    quoted = PublicFunction("q", (STR, INT), STR)
    hint = args_hint(quoted, '"a, b",')
    assert (hint.index, hint.template) == (1, '"a, b", <int>')


def test_args_hint_single_str() -> None:
    fn = PublicFunction("say", (STR,), STR)
    assert args_hint(fn, "").template == "<str>"
    hint = args_hint(fn, "hello, world")
    assert (hint.index, hint.template) == (0, "hello, world")


def test_function_choices() -> None:
    functions = [PublicFunction("ping", (INT,), INT), PublicFunction("stop", (), STR), PublicFunction("pay", (INT, ACCOUNT), STR), PublicFunction("map", (), INT)]
    choices = function_choices(sorted(functions, key=lambda fn: fn.name), "P")
    assert [(choice.name, choice.value) for choice in choices] == [
        ("pay(int, Account) -> str", "pay"),
        ("ping(int) -> int", "ping"),
        ("map() -> int", "map"),
        ("stop() -> str", "stop"),
    ]
    assert function_choices(functions, "zz") == []
    many = [PublicFunction(f"f{index:02d}" + "x" * 90, (CURRENCY, ACCOUNT), INT) for index in range(40)]
    choices = function_choices(many, "")
    assert len(choices) == 25
    assert all(len(choice.name) <= CHOICE_TEXT_MAX and choice.name.endswith("...") for choice in choices)
    assert choices[0].value == "f00" + "x" * 90
    assert function_choices([PublicFunction("y" * 101, (), INT)], "") == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (discord.Object(id=5), 5),
        (SimpleNamespace(id=6), 6),
        ("123", 123),
        (77, 77),
        (None, None),
        ("abc", None),
        ("0", None),
        (True, None),
        (str(2**63), None),
        (SimpleNamespace(id="x"), None),
    ],
)
def test_namespace_account_id(value: Any, expected: int | None) -> None:
    assert namespace_account_id(SimpleNamespace(user=value), "user") == expected
    assert namespace_account_id(SimpleNamespace(), "user") is None
