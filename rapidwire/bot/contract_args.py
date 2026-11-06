"""The args text of /contract call parsed by the called function's scalar signature (comma-separated in parameter order: an int in decimal, a str as a JSON string or unquoted when it is the only parameter, true/false, an Account as a mention or ID, a Currency as a symbol or ID), and the autocomplete of the function and args options."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import discord
from discord import app_commands

from rapidwire.bot.autocomplete import MAX_CHOICES
from rapidwire.bot.client import bot_context
from rapidwire.bot.errors import UserInputError
from rapidwire.bot.symbols import normalize_symbol
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.types import Kind, Type
from rapidwire.contract.values import ID_MAX, ID_MIN, Account, Currency, Value
from rapidwire.core.contracts import PublicFunction, list_public_functions
from rapidwire.core.currency import get_currency_by_symbol
from rapidwire.core.queries import search_currency_symbols
from rapidwire.db.tables import CURRENCY_SYMBOL_MAX, CURRENCY_SYMBOL_PATTERN

__all__ = [
    "CHOICE_TEXT_MAX",
    "ArgsHint",
    "SymbolResolver",
    "args_autocomplete",
    "args_hint",
    "function_autocomplete",
    "function_choices",
    "namespace_account_id",
    "parse_call_args",
    "symbol_resolver",
]

log = logging.getLogger("rapidwire.bot")

# Discord's limit on the name and on a string value of an autocomplete choice.
CHOICE_TEXT_MAX: Final = 100

# Resolves an uppercased currency symbol to the live currency's ID, or None when no live currency has it.
type SymbolResolver = Callable[[str], Awaitable[int | None]]

_INT_RE: Final = re.compile(r"(0|-?[1-9][0-9]*)")
_ID_RE: Final = re.compile(r"-?[0-9]+")
_MENTION_RE: Final = re.compile(r"<@!?([0-9]+)>")
_SYMBOL_RE: Final = re.compile(CURRENCY_SYMBOL_PATTERN)


def _type_name(t: Type) -> str:
    return str(t)


@dataclass(frozen=True)
class _Scan:
    """The comma-separated elements of an args text and whether a double-quoted string is left open."""

    elements: tuple[str, ...]
    open_quote: bool


def _scan(text: str) -> _Scan:
    elements: list[str] = []
    start = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == ",":
            elements.append(text[start:index])
            start = index + 1
    elements.append(text[start:])
    return _Scan(tuple(elements), in_string)


def _single_str(function: PublicFunction) -> bool:
    return len(function.params) == 1 and function.params[0].kind is Kind.STR


def _fail(where: str, message: str) -> UserInputError:
    return UserInputError(f"{where}: {message}")


def _int_text(text: str, where: str, limits: Limits) -> int:
    if len(text) > len(str(limits.int_bound)) + 1 or _INT_RE.fullmatch(text) is None:
        raise _fail(where, "整数を指定してください (例: 100、-5)。+ の符号、空白、先頭の余分な 0 は使えません。")
    number = int(text)
    if not limits.int_ok(number):
        raise _fail(where, f"整数の絶対値は 2^{limits.int_bits} 未満にしてください。")
    return number


def _id_text(text: str, where: str, label: str) -> int | None:
    """The ID written as digits with an optional minus sign, or None when text is not such a number."""
    if _ID_RE.fullmatch(text) is None:
        return None
    if len(text) > 20 or not ID_MIN <= int(text) <= ID_MAX:
        raise _fail(where, f"{label}の ID が範囲外です。")
    return int(text)


def _account_text(text: str, where: str) -> Account:
    mention = _MENTION_RE.fullmatch(text)
    if mention is not None:
        digits = mention.group(1)
        if len(digits) > 19 or not 1 <= int(digits) <= ID_MAX:
            raise _fail(where, "アカウントの ID が範囲外です。")
        return Account(int(digits))
    found = _id_text(text, where, "アカウント")
    if found is None:
        raise _fail(where, "ユーザーのメンション (<@ID>) またはアカウントの ID を指定してください。")
    return Account(found)


async def _currency_text(text: str, where: str, resolve: SymbolResolver) -> Currency:
    found = _id_text(text, where, "通貨")
    if found is not None:
        return Currency(found)
    symbol = normalize_symbol(text)
    if len(symbol) > CURRENCY_SYMBOL_MAX or _SYMBOL_RE.fullmatch(symbol) is None:
        raise _fail(where, "通貨のシンボルまたは通貨の ID を指定してください。")
    currency_id = await resolve(symbol)
    if currency_id is None:
        raise _fail(where, f"通貨 `{symbol}` が見つかりませんでした。")
    return Currency(currency_id)


def _str_value(value: str, where: str, limits: Limits) -> str:
    if len(value) > limits.str_max_length:
        raise _fail(where, f"文字列は {limits.str_max_length} 文字以内にしてください。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _fail(where, "文字列に使えない文字が含まれています。") from None
    return value


_QUOTE_STR: Final = '文字列は "..." のように二重引用符で囲んでください。カンマを含む文字列も引用符の中に書けます。'


async def _element(text: str, t: Type, where: str, limits: Limits, resolve: SymbolResolver) -> Value:
    """The value of one argument written as text; public functions take scalar parameters only, so any other kind is refused."""
    if not text:
        raise _fail(where, "値がありません。")
    kind = t.kind
    if kind is Kind.INT:
        return _int_text(text, where, limits)
    if kind is Kind.STR:
        if not text.startswith('"'):
            raise _fail(where, _QUOTE_STR)
        try:
            loaded = json.loads(text)
        except ValueError:
            raise _fail(where, _QUOTE_STR) from None
        if not isinstance(loaded, str):
            raise _fail(where, _QUOTE_STR)
        return _str_value(loaded, where, limits)
    if kind is Kind.BOOL:
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise _fail(where, "true または false を指定してください。")
        return lowered == "true"
    if kind is Kind.ACCOUNT:
        return _account_text(text, where)
    if kind is Kind.CURRENCY:
        return await _currency_text(text, where, resolve)
    raise _fail(where, "この型の引数は指定できません。")


def _count_error(function: PublicFunction, given: int) -> UserInputError:
    return UserInputError(f"引数の数が合いません。`{function.signature}` には {len(function.params)} 個の引数が必要ですが、{given} 個指定されました。")


def _empty_template(function: PublicFunction) -> str:
    """The placeholder template the args autocomplete shows before anything is typed, such as `<int>, <Account>`."""
    return ", ".join(f"<{_type_name(param)}>" for param in function.params)


async def parse_call_args(text: str | None, function: PublicFunction, *, resolve_currency: SymbolResolver, limits: Limits = DEFAULT_LIMITS) -> list[Value]:
    """The typed values of the args text for function's parameters, in the syntax of this module's docstring; raises UserInputError naming the argument (number and type) for an open quote, a wrong argument count, a value of the wrong type or outside the contract limits, an unknown currency symbol, or the placeholder template sent unchanged."""
    raw = (text or "").strip()
    params = function.params
    if not raw:
        if params:
            raise _count_error(function, 0)
        return []
    if params and raw == _empty_template(function):
        raise UserInputError(f"`{_empty_template(function)}` は引数の書式の例です。それぞれを実際の値に置き換えてください。")
    if _single_str(function) and not raw.startswith('"'):
        return [_str_value(raw, _where(0, params[0]), limits)]
    scan = _scan(raw)
    if scan.open_quote:
        raise UserInputError('引数を読み取れません: 閉じていない二重引用符 (") があります。')
    if len(scan.elements) != len(params):
        raise _count_error(function, len(scan.elements))
    return [await _element(element.strip(), param, _where(index, param), limits, resolve_currency) for index, (element, param) in enumerate(zip(scan.elements, params, strict=True))]


def _where(index: int, param: Type) -> str:
    return f"引数 {index + 1} ({_type_name(param)})"


def symbol_resolver(engine: Any, *, now: datetime | None = None) -> SymbolResolver:
    """A SymbolResolver reading live currencies through engine at the time now (the bot clock)."""

    async def resolve(symbol: str) -> int | None:
        found = await get_currency_by_symbol(engine, symbol, now=now)
        return None if found is None else found.currency_id

    return resolve


@dataclass(frozen=True)
class ArgsHint:
    """Where the typing of an args text stands: index is the parameter of the argument being typed (len(params) or more when there are too many), prefix the text before that argument (through the last separating comma and a following space), partial the argument typed so far, and template the typed arguments followed by <type> placeholders for the rest."""

    index: int
    prefix: str
    partial: str
    template: str


def args_hint(function: PublicFunction, current: str) -> ArgsHint:
    """The ArgsHint of the args text current for function, read leniently so a half-typed text still gives one."""
    params = function.params
    if _single_str(function) and not current.lstrip().startswith('"'):
        partial = current.strip()
        return ArgsHint(index=0, prefix="", partial=partial, template=partial or f"<{_type_name(params[0])}>")
    scan = _scan(current)
    index = len(scan.elements) - 1
    partial = scan.elements[-1].lstrip()
    prefix = current[: len(current) - len(partial)]
    if prefix.endswith(","):
        prefix += " "
    partial = partial.rstrip()
    typed = [element.strip() for element in scan.elements[:-1]]
    rest = [f"<{_type_name(param)}>" for param in params[index + 1 :]]
    here = [partial or f"<{_type_name(params[index])}>"] if index < len(params) else []
    return ArgsHint(index=index, prefix=prefix, partial=partial, template=", ".join(typed + here + rest))


def _cut_left(text: str, limit: int = CHOICE_TEXT_MAX) -> str:
    return text if len(text) <= limit else "..." + text[len(text) - limit + 3 :]


def _cut_right(text: str, limit: int = CHOICE_TEXT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def namespace_account_id(namespace: Any, name: str) -> int | None:
    """The user ID of a user option typed so far, which autocomplete delivers as a User, Member or discord.Object (each with an int id) or as the ID text, or None when the option is missing or not a user account ID."""
    value = getattr(namespace, name, None)
    ident = getattr(value, "id", value)
    if isinstance(ident, str) and ident.isascii() and ident.isdigit() and len(ident) <= 19:
        ident = int(ident)
    if isinstance(ident, bool) or not isinstance(ident, int) or not 1 <= ident <= ID_MAX:
        return None
    return ident


def function_choices(functions: Sequence[PublicFunction], current: str) -> list[app_commands.Choice[str]]:
    """Choices for the functions whose name contains current (letter case ignored), names starting with it first and each group in name order, at most MAX_CHOICES; each choice shows the signature and has the plain name as value."""
    typed = current.strip().lower()
    matching = [function for function in functions if typed in function.name.lower() and len(function.name) <= CHOICE_TEXT_MAX]
    matching.sort(key=lambda function: (not function.name.lower().startswith(typed), function.name))
    return [app_commands.Choice(name=_cut_right(function.signature), value=function.name) for function in matching[:MAX_CHOICES]]


async def function_autocomplete(interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
    """The public functions of the contract chosen in the user option matching current; no choices when no user is chosen yet, the account has no live contract, or the lookup fails (logged)."""
    contract_id = namespace_account_id(interaction.namespace, "user")
    if contract_id is None:
        return []
    try:
        functions = await list_public_functions(bot_context(interaction).engine, contract_id)
    except Exception:
        log.exception("contract function autocomplete failed")
        return []
    return function_choices(functions, current)


async def _completions(engine: Any, function: PublicFunction, hint: ArgsHint) -> list[tuple[str, str]]:
    """(name, value) completions of the argument being typed: true and false for a bool, live currency symbols for a Currency."""
    if hint.index >= len(function.params):
        return []
    kind = function.params[hint.index].kind
    if kind is Kind.BOOL:
        return [(hint.prefix + word, hint.prefix + word) for word in ("true", "false") if word.startswith(hint.partial.lower())]
    if kind is Kind.CURRENCY:
        typed = normalize_symbol(hint.partial)
        if len(typed) > CURRENCY_SYMBOL_MAX or not all(char.isascii() and (char.isalpha() or char == "-") for char in typed):
            return []
        found = await search_currency_symbols(engine, typed, limit=MAX_CHOICES - 1)
        return [(f"{hint.prefix}{item.symbol} ({item.name})", hint.prefix + item.symbol) for item in found]
    return []


async def args_autocomplete(interaction: discord.Interaction[Any], current: str) -> list[app_commands.Choice[str]]:
    """Hints for the args option from the signature of the function chosen in the function option of the contract chosen in the user option.

    The first choice shows the arguments typed so far followed by a <type> placeholder for each parameter still to come, keeping the typed text as its value (the template itself when nothing is typed yet, except for a function whose only parameter is a str, which would take the template as its text); for a bool or Currency parameter the next choices complete the argument being typed with true/false or live currency symbols.
    There are no choices when the contract or function is unknown, the lookup fails (logged), or the typed text is longer than a choice value can hold.
    """
    contract_id = namespace_account_id(interaction.namespace, "user")
    name = getattr(interaction.namespace, "function", None)
    if contract_id is None or not isinstance(name, str) or len(current) > CHOICE_TEXT_MAX:
        return []
    try:
        engine = bot_context(interaction).engine
        function = next((item for item in await list_public_functions(engine, contract_id) if item.name == name.strip()), None)
        if function is None:
            return []
        hint = args_hint(function, current)
        completions = await _completions(engine, function, hint)
    except Exception:
        log.exception("contract args autocomplete failed")
        return []
    choices: list[app_commands.Choice[str]] = []
    if not function.params:
        if current.strip():
            choices.append(app_commands.Choice(name=_cut_right(f"この関数は引数を取りません: {function.signature}"), value=current))
    elif hint.index >= len(function.params):
        choices.append(app_commands.Choice(name=_cut_right(f"引数が多すぎます: {function.signature}"), value=current))
    elif current.strip():
        if len(current) <= CHOICE_TEXT_MAX:
            choices.append(app_commands.Choice(name=_cut_left(hint.template), value=current))
    elif not _single_str(function) and len(hint.template) <= CHOICE_TEXT_MAX:
        choices.append(app_commands.Choice(name=_cut_left(hint.template), value=hint.template))
    for choice_name, choice_value in completions:
        if len(choices) >= MAX_CHOICES:
            break
        if len(choice_value) <= CHOICE_TEXT_MAX:
            choices.append(app_commands.Choice(name=_cut_left(choice_name), value=choice_value))
    return choices
