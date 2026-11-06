"""Static types of the contract language, their binary tags, and the field layouts of the read-only records builtins return.

Value is the type of a dynamic value (the result of call, CallResult.value, a storage read); a program narrows it with CHECK_TYPE.
Type variables (Kind.VAR) appear only in builtin signatures and cannot be encoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

# Deepest nesting of list/dict type constructors accepted in a binary.
MAX_TYPE_DEPTH = 8


class Kind(IntEnum):
    """Type constructors; the value is the tag byte of the type encoding."""

    NONE = 0
    INT = 1
    STR = 2
    BOOL = 3
    ACCOUNT = 4
    CURRENCY = 5
    LIST = 6
    DICT = 7
    VALUE = 8
    CALL_RESULT = 9
    PAID = 10
    CURRENCY_INFO = 11
    CLAIM_INFO = 12
    QUOTE = 13
    VAR = 255


ARITY: dict[Kind, int] = {Kind.LIST: 1, Kind.DICT: 2}

KIND_NAMES: dict[Kind, str] = {
    Kind.NONE: "None",
    Kind.INT: "int",
    Kind.STR: "str",
    Kind.BOOL: "bool",
    Kind.ACCOUNT: "Account",
    Kind.CURRENCY: "Currency",
    Kind.LIST: "list",
    Kind.DICT: "dict",
    Kind.VALUE: "Value",
    Kind.CALL_RESULT: "CallResult",
    Kind.PAID: "Paid",
    Kind.CURRENCY_INFO: "CurrencyInfo",
    Kind.CLAIM_INFO: "ClaimInfo",
    Kind.QUOTE: "Quote",
}

ENCODABLE_KINDS = frozenset(kind for kind in Kind if kind is not Kind.VAR)
SCALAR_KINDS = frozenset({Kind.INT, Kind.STR, Kind.BOOL, Kind.ACCOUNT, Kind.CURRENCY})
# bool is not a key kind: Python would treat True and 1 (False and 0) as the same key; an int key must also be within signed 64 bits at run time (values.is_key).
KEY_KINDS = frozenset({Kind.INT, Kind.STR, Kind.ACCOUNT, Kind.CURRENCY})
PUBLIC_PARAM_KINDS = SCALAR_KINDS
PUBLIC_RETURN_KINDS = frozenset({Kind.INT, Kind.STR, Kind.BOOL})
RECORD_KINDS = frozenset({Kind.CALL_RESULT, Kind.PAID, Kind.CURRENCY_INFO, Kind.CLAIM_INFO, Kind.QUOTE})


@dataclass(frozen=True, slots=True)
class Type:
    """A type: kind plus its element types (one for list, key and value for dict); name is set only for a type variable."""

    kind: Kind
    args: tuple[Type, ...] = ()
    name: str = ""

    def __post_init__(self) -> None:
        if len(self.args) != ARITY.get(self.kind, 0):
            raise ValueError(f"{KIND_NAMES.get(self.kind, 'type')} takes {ARITY.get(self.kind, 0)} type arguments")
        if (self.kind is Kind.VAR) != bool(self.name):
            raise ValueError("exactly type variables have a name")

    def __str__(self) -> str:
        if self.kind is Kind.VAR:
            return self.name
        name = KIND_NAMES[self.kind]
        if self.args:
            return f"{name}[{','.join(str(arg) for arg in self.args)}]"
        return name

    def depth(self) -> int:
        """Nesting depth: 1 for a type without arguments."""
        return 1 + max((arg.depth() for arg in self.args), default=0)


NONE = Type(Kind.NONE)
INT = Type(Kind.INT)
STR = Type(Kind.STR)
BOOL = Type(Kind.BOOL)
ACCOUNT = Type(Kind.ACCOUNT)
CURRENCY = Type(Kind.CURRENCY)
VALUE = Type(Kind.VALUE)
CALL_RESULT = Type(Kind.CALL_RESULT)
PAID = Type(Kind.PAID)
CURRENCY_INFO = Type(Kind.CURRENCY_INFO)
CLAIM_INFO = Type(Kind.CLAIM_INFO)
QUOTE = Type(Kind.QUOTE)


def list_of(element: Type) -> Type:
    return Type(Kind.LIST, (element,))


def dict_of(key: Type, value: Type) -> Type:
    return Type(Kind.DICT, (key, value))


def type_var(name: str) -> Type:
    return Type(Kind.VAR, name=name)


@dataclass(frozen=True, slots=True)
class RecordField:
    name: str
    type: Type


# Field order is the field index operand of GET_FIELD.
RECORD_FIELDS: dict[Kind, tuple[RecordField, ...]] = {
    Kind.CALL_RESULT: (
        RecordField("ok", BOOL),
        RecordField("value", VALUE),
        RecordField("error", STR),
        RecordField("message", STR),
    ),
    Kind.PAID: (
        RecordField("currency", CURRENCY),
        RecordField("amount", INT),
    ),
    Kind.CURRENCY_INFO: (
        RecordField("currency", CURRENCY),
        RecordField("name", STR),
        RecordField("symbol", STR),
        RecordField("issuer", ACCOUNT),
        RecordField("supply", INT),
        RecordField("hourly_rate_ppm", INT),
        RecordField("minting_renounced", BOOL),
        RecordField("delegation_enabled", BOOL),
        RecordField("delete_requested", BOOL),
        RecordField("created_at", INT),
    ),
    Kind.CLAIM_INFO: (
        RecordField("claim_id", INT),
        RecordField("claimant", ACCOUNT),
        RecordField("payer", ACCOUNT),
        RecordField("currency", CURRENCY),
        RecordField("amount", INT),
        RecordField("description", STR),
        RecordField("status", STR),
        RecordField("created_at", INT),
    ),
    Kind.QUOTE: (
        RecordField("from_currency", CURRENCY),
        RecordField("to_currency", CURRENCY),
        RecordField("amount_in", INT),
        RecordField("amount_out", INT),
        RecordField("route_count", INT),
        RecordField("truncated", BOOL),
    ),
}


def record_field_index(kind: Kind, name: str) -> int:
    """Index of the field called name in records of kind; raises KeyError when there is none."""
    for index, record_field in enumerate(RECORD_FIELDS[kind]):
        if record_field.name == name:
            return index
    raise KeyError(f"{KIND_NAMES[kind]} has no field {name!r}")


def type_problem(t: Type, *, allow_none: bool = False) -> str | None:
    """Why t is not the type of a value (or, with allow_none, of a function result), or None when it is valid."""
    if t.kind is Kind.VAR:
        return "type variables cannot appear in a binary"
    if t.kind is Kind.NONE:
        return None if allow_none else "None is only a function return type"
    if t.depth() > MAX_TYPE_DEPTH:
        return f"type nesting deeper than {MAX_TYPE_DEPTH}"
    if t.kind is Kind.DICT and t.args[0].kind not in KEY_KINDS:
        return f"dict key type must be int, str, Account or Currency, not {t.args[0]}"
    for arg in t.args:
        problem = type_problem(arg)
        if problem is not None:
            return problem
    return None


_TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|\[|\]|,)")
_KINDS_BY_NAME = {name: kind for kind, name in KIND_NAMES.items()}


def parse_type(text: str) -> Type:
    """Parse a type written as str() formats it (spaces allowed); single uppercase letters other than the named types are type variables; raises ValueError."""
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError(f"cannot parse type {text!r}")
        tokens.append(match.group(1))
        pos = match.end()
    result, index = _parse_tokens(tokens, 0, text)
    if index != len(tokens):
        raise ValueError(f"cannot parse type {text!r}")
    return result


def _parse_tokens(tokens: list[str], index: int, text: str) -> tuple[Type, int]:
    if index >= len(tokens):
        raise ValueError(f"cannot parse type {text!r}")
    name = tokens[index]
    index += 1
    kind = _KINDS_BY_NAME.get(name)
    if kind is None:
        if len(name) == 1 and name.isupper():
            return type_var(name), index
        raise ValueError(f"unknown type {name!r}")
    arity = ARITY.get(kind, 0)
    if arity == 0:
        return Type(kind), index
    if index >= len(tokens) or tokens[index] != "[":
        raise ValueError(f"{name} needs type arguments")
    index += 1
    args: list[Type] = []
    for position in range(arity):
        if position:
            if index >= len(tokens) or tokens[index] != ",":
                raise ValueError(f"cannot parse type {text!r}")
            index += 1
        arg, index = _parse_tokens(tokens, index, text)
        args.append(arg)
    if index >= len(tokens) or tokens[index] != "]":
        raise ValueError(f"cannot parse type {text!r}")
    return Type(kind, tuple(args)), index + 1
