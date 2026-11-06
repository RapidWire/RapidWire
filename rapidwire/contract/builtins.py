"""Builtin registry of instruction set version 1: id, source name, parameters, result type, base gas and how the VM implements each builtin.

The id is the CALL_BUILTIN operand and is fixed by its position in BUILTINS_V1, so entries are only ever appended.
A name with a dot is a method or namespace member in source form ("str.split" is text.split(sep), "storage.get" is storage.get(key, default)); its receiver is the first parameter.
Context builtins read the ExecutionContext, pure builtins run inside the VM, and host builtins await the Host method named host_method after the VM has checked every argument's type.
Single uppercase letters in signatures are type variables bound by the compiler; the VM checks only the outer kind of each argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from rapidwire.contract.costs import BUILTIN_GAS
from rapidwire.contract.types import (
    ACCOUNT,
    BOOL,
    CLAIM_INFO,
    CURRENCY,
    CURRENCY_INFO,
    INT,
    NONE,
    PAID,
    QUOTE,
    STR,
    VALUE,
    Type,
    dict_of,
    list_of,
    type_var,
)


class BuiltinKind(StrEnum):
    CONTEXT = "context"
    PURE = "pure"
    HOST = "host"


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    type: Type


@dataclass(frozen=True, slots=True)
class Builtin:
    """One builtin; returns is types.NONE for a builtin used as a statement.

    CALL_BUILTIN may pass any argument count from required to arity; the trailing parameters beyond required are optional (storage.get and read_storage without default fail with a runtime error on a missing key).
    """

    id: int
    name: str
    params: tuple[Param, ...]
    returns: Type
    kind: BuiltinKind
    host_method: str | None
    gas: int
    required: int

    @property
    def arity(self) -> int:
        return len(self.params)

    def accepts(self, argc: int) -> bool:
        """Whether CALL_BUILTIN may pass argc arguments."""
        return self.required <= argc <= len(self.params)

    @property
    def is_host(self) -> bool:
        return self.kind is BuiltinKind.HOST

    @property
    def pushes(self) -> int:
        """Values the builtin leaves on the stack: 0 for a None result, else 1."""
        return 0 if self.returns == NONE else 1

    def signature(self) -> str:
        """The signature with optional parameters in brackets, for example storage.get(key: str[, default: Value]) -> Value."""
        required = ", ".join(f"{p.name}: {p.type}" for p in self.params[: self.required])
        optional = "".join(f"{', ' if index or required else ''}{p.name}: {p.type}" for index, p in enumerate(self.params[self.required :]))
        return f"{self.name}({required}{'[' + optional + ']' if optional else ''}) -> {self.returns}"


_K = type_var("K")
_V = type_var("V")

_SPECS: tuple[tuple[str, tuple[tuple[str, Type], ...], Type, BuiltinKind, str | None], ...] = (
    ("sender", (), ACCOUNT, BuiltinKind.CONTEXT, None),
    ("self", (), ACCOUNT, BuiltinKind.CONTEXT, None),
    ("paid", (), PAID, BuiltinKind.CONTEXT, None),
    ("now", (), INT, BuiltinKind.CONTEXT, None),
    ("balance", (("account", ACCOUNT), ("currency", CURRENCY)), INT, BuiltinKind.HOST, "balance"),
    ("currency", (("symbol", STR),), CURRENCY, BuiltinKind.HOST, "currency"),
    ("currency_info", (("currency", CURRENCY),), CURRENCY_INFO, BuiltinKind.HOST, "currency_info"),
    ("transfer", (("to", ACCOUNT), ("currency", CURRENCY), ("amount", INT)), NONE, BuiltinKind.HOST, "transfer"),
    ("transfer_from", (("owner", ACCOUNT), ("to", ACCOUNT), ("currency", CURRENCY), ("amount", INT)), NONE, BuiltinKind.HOST, "transfer_from"),
    ("approve", (("spender", ACCOUNT), ("currency", CURRENCY), ("amount", INT)), NONE, BuiltinKind.HOST, "approve"),
    ("allowance", (("owner", ACCOUNT), ("spender", ACCOUNT), ("currency", CURRENCY)), INT, BuiltinKind.HOST, "allowance"),
    ("create_claim", (("payer", ACCOUNT), ("currency", CURRENCY), ("amount", INT), ("description", STR)), INT, BuiltinKind.HOST, "create_claim"),
    ("pay_claim", (("claim_id", INT),), NONE, BuiltinKind.HOST, "pay_claim"),
    ("cancel_claim", (("claim_id", INT),), NONE, BuiltinKind.HOST, "cancel_claim"),
    ("claim_info", (("claim_id", INT),), CLAIM_INFO, BuiltinKind.HOST, "claim_info"),
    ("swap", (("from_currency", CURRENCY), ("to_currency", CURRENCY), ("amount_in", INT), ("min_out", INT)), INT, BuiltinKind.HOST, "swap"),
    (
        "add_liquidity",
        (
            ("currency_x", CURRENCY),
            ("currency_y", CURRENCY),
            ("fee_bps", INT),
            ("amount_x", INT),
            ("amount_y", INT),
            ("amount_x_min", INT),
            ("amount_y_min", INT),
            ("min_shares", INT),
        ),
        INT,
        BuiltinKind.HOST,
        "add_liquidity",
    ),
    (
        "remove_liquidity",
        (("currency_x", CURRENCY), ("currency_y", CURRENCY), ("fee_bps", INT), ("shares", INT), ("amount_x_min", INT), ("amount_y_min", INT)),
        list_of(INT),
        BuiltinKind.HOST,
        "remove_liquidity",
    ),
    ("quote", (("from_currency", CURRENCY), ("to_currency", CURRENCY), ("amount_in", INT)), QUOTE, BuiltinKind.HOST, "quote"),
    ("stake", (("currency", CURRENCY), ("amount", INT)), NONE, BuiltinKind.HOST, "stake"),
    ("unstake", (("currency", CURRENCY), ("amount", INT)), NONE, BuiltinKind.HOST, "unstake"),
    ("staked", (("account", ACCOUNT), ("currency", CURRENCY)), INT, BuiltinKind.HOST, "staked"),
    ("storage.get", (("key", STR), ("default", VALUE)), VALUE, BuiltinKind.HOST, "storage_get"),
    ("storage.set", (("key", STR), ("value", VALUE)), NONE, BuiltinKind.HOST, "storage_set"),
    ("storage.delete", (("key", STR),), NONE, BuiltinKind.HOST, "storage_delete"),
    ("read_storage", (("account", ACCOUNT), ("key", STR), ("default", VALUE)), VALUE, BuiltinKind.HOST, "read_storage"),
    ("discord.send", (("guild", INT), ("channel", INT), ("message", STR)), NONE, BuiltinKind.HOST, "discord_send"),
    ("discord.add_role", (("guild", INT), ("user", ACCOUNT), ("role", INT)), NONE, BuiltinKind.HOST, "discord_add_role"),
    ("discord.remove_role", (("guild", INT), ("user", ACCOUNT), ("role", INT)), NONE, BuiltinKind.HOST, "discord_remove_role"),
    ("discord.has_role", (("guild", INT), ("user", ACCOUNT), ("role", INT)), BOOL, BuiltinKind.HOST, "discord_has_role"),
    ("sha256", (("text", STR),), STR, BuiltinKind.PURE, None),
    ("random", (("low", INT), ("high", INT)), INT, BuiltinKind.PURE, None),
    ("min", (("a", INT), ("b", INT)), INT, BuiltinKind.PURE, None),
    ("max", (("a", INT), ("b", INT)), INT, BuiltinKind.PURE, None),
    ("abs", (("a", INT),), INT, BuiltinKind.PURE, None),
    ("Account", (("id", INT),), ACCOUNT, BuiltinKind.PURE, None),
    ("Currency", (("id", INT),), CURRENCY, BuiltinKind.PURE, None),
    ("str.split", (("text", STR), ("sep", STR)), list_of(STR), BuiltinKind.PURE, None),
    ("str.find", (("text", STR), ("sub", STR)), INT, BuiltinKind.PURE, None),
    ("str.startswith", (("text", STR), ("prefix", STR)), BOOL, BuiltinKind.PURE, None),
    ("str.endswith", (("text", STR), ("suffix", STR)), BOOL, BuiltinKind.PURE, None),
    ("str.replace", (("text", STR), ("old", STR), ("new", STR)), STR, BuiltinKind.PURE, None),
    ("str.upper", (("text", STR),), STR, BuiltinKind.PURE, None),
    ("str.lower", (("text", STR),), STR, BuiltinKind.PURE, None),
    ("str.strip", (("text", STR),), STR, BuiltinKind.PURE, None),
    ("str.join", (("sep", STR), ("items", list_of(STR))), STR, BuiltinKind.PURE, None),
    ("dict.get", (("d", dict_of(_K, _V)), ("key", _K), ("default", _V)), _V, BuiltinKind.PURE, None),
    ("dict.keys", (("d", dict_of(_K, _V)),), list_of(_K), BuiltinKind.PURE, None),
    ("dict.values", (("d", dict_of(_K, _V)),), list_of(_V), BuiltinKind.PURE, None),
)

# Trailing parameters a call may leave out, per builtin.
_OPTIONAL = {"storage.get": 1, "read_storage": 1}

BUILTINS_V1: tuple[Builtin, ...] = tuple(
    Builtin(index, name, tuple(Param(pname, ptype) for pname, ptype in params), returns, kind, method, BUILTIN_GAS[name], len(params) - _OPTIONAL.get(name, 0))
    for index, (name, params, returns, kind, method) in enumerate(_SPECS)
)
BUILTINS_V1_BY_NAME = MappingProxyType({builtin.name: builtin for builtin in BUILTINS_V1})
