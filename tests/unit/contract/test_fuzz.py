"""Fuzz tests: verify() raises only VerificationError, and every accepted binary runs to a result or a ContractFailure.

RAPIDWIRE_TEST_CONTRACT_FUZZ_SCALE multiplies the number of examples (default 1).
"""

from __future__ import annotations

import collections
import os
import random
import struct
from collections.abc import Coroutine
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rapidwire.contract.assembler import Assembler, FunctionBuilder
from rapidwire.contract.builtins import BUILTINS_V1
from rapidwire.contract.errors import ContractFailure, VerificationError
from rapidwire.contract.format import HEADER, encode
from rapidwire.contract.limits import Limits
from rapidwire.contract.types import (
    ACCOUNT,
    BOOL,
    CURRENCY,
    INT,
    NONE,
    PUBLIC_PARAM_KINDS,
    RECORD_FIELDS,
    STR,
    VALUE,
    Kind,
    Type,
    dict_of,
    list_of,
)
from rapidwire.contract.values import Account, Currency
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.contract.vm import ExecutionContext, execute
from tests.unit.contract.helpers import RAPID, SELF, SENDER, CheckingMeter, FakeHost, checking_context, seeded

SCALE = max(1, int(os.environ.get("RAPIDWIRE_TEST_CONTRACT_FUZZ_SCALE", "1")))
MAX_SIZE = 100_000
GAS = 600
PEERS = [Account(3003), Account(3004), Account(3005)]

PUBLIC_PARAMS = [INT, STR, BOOL, ACCOUNT, CURRENCY]
INTERNAL_TYPES = [INT, STR, BOOL, ACCOUNT, CURRENCY, VALUE, list_of(INT), list_of(STR), dict_of(STR, INT), Type(Kind.PAID), Type(Kind.CALL_RESULT)]
CHECK_TYPES = [INT, STR, BOOL, ACCOUNT, CURRENCY, VALUE, list_of(INT), dict_of(STR, list_of(VALUE)), list_of(list_of(INT))]
STRINGS = ["", "a", "abc", "f0", "f1", "k", chr(0x3042) + chr(0x3044), "x" * 300, "12", "-7", ",", "a,b,c", "RAPID", "MISSING"]
INTS = [0, 1, -1, 2, 7, 100, 2**63 - 1, -(2**63), 2**255, -(2**255) + 1, 2**256 - 1]
UNARY = ["NEG", "NOT", "LEN", "TO_INT", "TO_STR", "CHECK_TYPE", "GET_FIELD", "LIST_POP0"]
BINARY = ["ADD", "SUB", "MUL", "FLOORDIV", "MOD", "EQ", "NE", "LT", "LE", "GT", "GE", "CONCAT", "INDEX_GET", "CONTAINS", "LIST_POP1"]
VALUE_BUILTINS = [b for b in BUILTINS_V1 if b.pushes]
STATEMENT_BUILTINS = [b for b in BUILTINS_V1 if not b.pushes]


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine that never waits for anything (FakeHost methods complete immediately); the VM's periodic bare yields to the event loop are resumed at once, and waiting on a future fails the test."""
    while True:
        try:
            awaited = coro.send(None)
        except StopIteration as stop:
            return stop.value
        if awaited is not None:
            coro.close()
            raise AssertionError("coroutine suspended")


class Generator:
    """Untyped random programs: statements start and end at stack depth 0, so jumps between statement starts always merge, but operand types are arbitrary."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def program(self) -> Assembler:
        rng = self.rng
        asm = Assembler()
        count = rng.randint(1, 3)
        signatures: list[tuple[str, list[Type], Type, bool, int]] = []
        for index in range(count):
            public = index == 0 or rng.random() < 0.5
            if public:
                params = [rng.choice(PUBLIC_PARAMS) for _ in range(rng.randint(0, 3))]
                returns = rng.choice([INT, STR, BOOL])
            else:
                params = [rng.choice(INTERNAL_TYPES) for _ in range(rng.randint(0, 3))]
                returns = rng.choice([INT, STR, NONE, list_of(INT), VALUE])
            signatures.append((f"f{index}", params, returns, public, max(1, len(params) + rng.randint(0, 4))))
        self.signatures = signatures
        for name, params, returns, public, local_count in signatures:
            builder = asm.function(name, params=params, returns=returns, public=public, local_count=local_count)
            self.local_count = local_count
            self.returns = returns
            statements = rng.randint(1, 10)
            self.labels = [f"s{k}" for k in range(statements + 1)]
            for k in range(statements):
                builder.label(self.labels[k])
                self.statement(builder)
            builder.label(self.labels[statements])
            self.ret(builder)
        return asm

    def ret(self, builder: FunctionBuilder) -> None:
        if self.returns != NONE:
            self.expr(builder, 0)
        builder.emit("RET")

    def local(self) -> int:
        return self.rng.randrange(self.local_count)

    def statement(self, builder: FunctionBuilder) -> None:
        rng = self.rng
        choice = rng.randrange(12)
        if choice <= 2:
            self.expr(builder, 0)
            builder.emit("STORE", self.local())
        elif choice == 3:
            self.expr(builder, 0)
            builder.emit(rng.choice(["JUMP_IF_FALSE", "JUMP_IF_TRUE"]), rng.choice(self.labels))
        elif choice == 4:
            builder.emit("JUMP", rng.choice(self.labels))
        elif choice == 5:
            builder.emit("FOR_RANGE", self.local(), self.local(), self.local(), self.local(), rng.choice(self.labels))
        elif choice == 6:
            builder.emit("FOR_EACH", self.local(), self.local(), self.local(), rng.choice(self.labels))
        elif choice == 7:
            self.expr(builder, 0)
            if rng.random() < 0.5:
                builder.emit("REVERT")
            else:
                builder.emit("ASSERT", rng.choice(STRINGS))
        elif choice == 8:
            builtin = rng.choice(STATEMENT_BUILTINS)
            for _ in builtin.params:
                self.expr(builder, 1)
            builder.emit("CALL_BUILTIN", builtin.name, builtin.arity)
        elif choice == 9:
            mnemonic, width = rng.choice([("INDEX_SET", 3), ("APPEND", 2), ("DELETE_ITEM", 2)])
            for _ in range(width):
                self.expr(builder, 1)
            builder.emit(mnemonic)
        elif choice == 10:
            self.expr(builder, 0)
            builder.emit("POP")
        else:
            self.ret(builder)

    def expr(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        if depth >= 3 or rng.random() < 0.35:
            self.leaf(builder)
            return
        choice = rng.randrange(8)
        if choice == 0:
            self.expr(builder, depth + 1)
            op = rng.choice(UNARY)
            if op == "CHECK_TYPE":
                builder.emit("CHECK_TYPE", rng.choice(CHECK_TYPES))
            elif op == "GET_FIELD":
                kind = rng.choice(list(RECORD_FIELDS))
                builder.emit("GET_FIELD", kind, rng.randrange(len(RECORD_FIELDS[kind])))
            elif op == "LIST_POP0":
                builder.emit("LIST_POP", 0)
            else:
                builder.emit(op)
        elif choice == 1:
            self.expr(builder, depth + 1)
            self.expr(builder, depth + 1)
            op = rng.choice(BINARY)
            if op == "LIST_POP1":
                builder.emit("LIST_POP", 1)
            else:
                builder.emit(op)
        elif choice == 2:
            count = rng.randint(0, 3)
            mnemonic, width = ("BUILD_LIST", 1) if rng.random() < 0.5 else ("BUILD_DICT", 2)
            for _ in range(count * width):
                self.expr(builder, depth + 1)
            builder.emit(mnemonic, count)
        elif choice == 3:
            flags = rng.randrange(4)
            self.expr(builder, depth + 1)
            for bit in (1, 2):
                if flags & bit:
                    self.expr(builder, depth + 1)
            builder.emit("SLICE", flags)
        elif choice == 4:
            candidates = [s for s in self.signatures if s[2] != NONE]
            if not candidates:
                self.leaf(builder)
                return
            name, params, _, _, _ = rng.choice(candidates)
            for _ in params:
                self.expr(builder, depth + 1)
            builder.emit("CALL", name)
        elif choice == 5:
            builtin = rng.choice(VALUE_BUILTINS)
            for _ in builtin.params:
                self.expr(builder, depth + 1)
            builder.emit("CALL_BUILTIN", builtin.name, builtin.arity)
        elif choice == 6:
            self.contract_call(builder, depth)
        else:
            self.leaf(builder)

    def contract_call(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        if rng.random() < 0.8:
            builder.emit("PUSH_INT", rng.choice([PEERS[0].id, SELF.id, 99]))
            builder.emit("CALL_BUILTIN", "Account", 1)
        else:
            self.expr(builder, depth + 1)
        builder.emit("PUSH_CONST", rng.choice(["f0", "f1", "f2", "missing"]))
        flags = rng.randrange(8)
        argc = 0
        if flags & 4:
            self.expr(builder, depth + 1)
        else:
            argc = rng.randint(0, 3)
            for _ in range(argc):
                self.expr(builder, depth + 1)
        if flags & 1:
            builder.emit("PUSH_INT", RAPID.id)
            builder.emit("CALL_BUILTIN", "Currency", 1)
            builder.emit("PUSH_INT", rng.choice([1, 5, 0, -1]))
        if flags & 2:
            builder.emit("PUSH_INT", rng.choice([1, 10, 100, 0]))
        builder.emit(rng.choice(["CALL_CONTRACT", "TRY_CALL"]), flags, argc)

    def leaf(self, builder: FunctionBuilder) -> None:
        rng = self.rng
        choice = rng.randrange(7)
        if choice == 0:
            builder.emit("PUSH_INT", rng.choice([0, 1, -1, 3, 1000, 2**62]))
        elif choice == 1:
            builder.emit("PUSH_CONST", rng.choice(STRINGS))
        elif choice == 2:
            builder.emit("PUSH_CONST", rng.choice(INTS))
        elif choice == 3:
            builder.emit(rng.choice(["PUSH_TRUE", "PUSH_FALSE"]))
        elif choice == 4:
            builder.emit("LOAD", self.local())
        elif choice == 5:
            builder.emit("CALL_BUILTIN", rng.choice(["sender", "self", "paid", "now"]), 0)
        else:
            count = rng.randint(0, 2)
            for _ in range(count):
                builder.emit("PUSH_INT", rng.randint(-3, 3))
            builder.emit("BUILD_LIST", count)


TAGS = ("int", "str", "bool", "list", "dict", "acc", "cur")
TAG_OF = {INT: "int", STR: "str", BOOL: "bool", ACCOUNT: "acc", CURRENCY: "cur", list_of(INT): "list", dict_of(STR, INT): "dict"}
TYPED_INTERNAL = [INT, STR, BOOL, ACCOUNT, CURRENCY, list_of(INT), dict_of(STR, INT)]


class TypedGenerator:
    """Mostly well-typed structured programs (if, range and list loops, calls, storage, nested contract calls) with a small rate of deliberate type noise, so executions run deep."""

    def __init__(self, rng: random.Random, noise: float = 0.03) -> None:
        self.rng = rng
        self.noise = noise
        self.untyped = Generator(rng)
        self.untyped.signatures = []
        self.labels = 0

    def program(self) -> Assembler:
        rng = self.rng
        asm = Assembler()
        self.signatures: list[tuple[str, list[Type], Type, bool]] = []
        for index in range(rng.randint(1, 4)):
            public = index == 0 or rng.random() < 0.5
            if public:
                params = [rng.choice(PUBLIC_PARAMS) for _ in range(rng.randint(0, 3))]
                returns = rng.choice([INT, STR, BOOL])
            else:
                params = [rng.choice(TYPED_INTERNAL) for _ in range(rng.randint(0, 3))]
                returns = rng.choice([INT, STR, BOOL, list_of(INT), NONE])
            self.signatures.append((f"f{index}", params, returns, public))
        for name, params, returns, public in self.signatures:
            builder = asm.function(name, params=params, returns=returns, public=public)
            self.locals = [TAG_OF[param] for param in params]
            self.returns = returns
            self.untyped.local_count = max(1, len(params))
            for _ in range(rng.randint(1, 4)):
                tag = rng.choice(TAGS)
                self.expr(builder, tag, 1)
                builder.emit("STORE", self.new_local(tag))
            self.block(builder, 0, rng.randint(1, 6))
            self.ret(builder)
        return asm

    def new_local(self, tag: str) -> int:
        self.locals.append(tag)
        return len(self.locals) - 1

    def local_of(self, tag: str) -> int | None:
        candidates = [index for index, t in enumerate(self.locals) if t == tag]
        return self.rng.choice(candidates) if candidates else None

    def label(self) -> str:
        self.labels += 1
        return f"L{self.labels}"

    def ret(self, builder: FunctionBuilder) -> None:
        if self.returns != NONE:
            self.expr(builder, TAG_OF[self.returns], 0)
        builder.emit("RET")

    def block(self, builder: FunctionBuilder, depth: int, count: int) -> None:
        for _ in range(count):
            self.statement(builder, depth)

    def statement(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        choice = rng.randrange(14)
        nested = depth < 2
        if choice <= 2:
            tag = rng.choice(TAGS)
            target = self.local_of(tag)
            self.expr(builder, tag, 0)
            builder.emit("STORE", target if target is not None else self.new_local(tag))
        elif choice == 3 and nested:
            other, end = self.label(), self.label()
            self.expr(builder, "bool", 0)
            builder.emit("JUMP_IF_FALSE", other)
            self.block(builder, depth + 1, rng.randint(1, 3))
            builder.emit("JUMP", end).label(other)
            self.block(builder, depth + 1, rng.randint(0, 2))
            builder.label(end)
        elif choice == 4 and nested:
            counter, stop, step, var = (self.new_local("int") for _ in range(4))
            head, done = self.label(), self.label()
            builder.emit("PUSH_INT", rng.randint(-3, 3)).emit("STORE", counter)
            self.expr(builder, "int", 2)
            builder.emit("STORE", stop)
            builder.emit("PUSH_INT", rng.choice([1, 1, 2, -1, 0])).emit("STORE", step)
            builder.label(head).emit("FOR_RANGE", counter, stop, step, var, done)
            self.block(builder, depth + 1, rng.randint(1, 3))
            builder.emit("JUMP", head).label(done)
        elif choice == 5 and nested:
            seq, index, var = self.new_local("list"), self.new_local("int"), self.new_local("int")
            head, done = self.label(), self.label()
            self.expr(builder, "list", 1)
            builder.emit("STORE", seq).emit("PUSH_INT", 0).emit("STORE", index)
            builder.label(head).emit("FOR_EACH", seq, index, var, done)
            self.block(builder, depth + 1, rng.randint(1, 3))
            builder.emit("JUMP", head).label(done)
        elif choice == 6:
            name, params = rng.choice(
                [
                    ("transfer", ["acc", "cur", "int"]),
                    ("storage.set", ["str", rng.choice(TAGS)]),
                    ("storage.delete", ["str"]),
                    ("discord.send", ["int", "int", "str"]),
                    ("approve", ["acc", "cur", "int"]),
                    ("stake", ["cur", "int"]),
                    ("pay_claim", ["int"]),
                ]
            )
            for tag in params:
                self.expr(builder, tag, 1)
            builder.emit("CALL_BUILTIN", name, len(params))
        elif choice == 7:
            target = self.local_of("list")
            if target is None:
                return
            builder.emit("LOAD", target)
            self.expr(builder, "int", 1)
            builder.emit("APPEND")
        elif choice == 8:
            target = self.local_of("dict")
            if target is None:
                return
            builder.emit("LOAD", target)
            self.expr(builder, "str", 2)
            if rng.random() < 0.7:
                self.expr(builder, "int", 1)
                builder.emit("INDEX_SET")
            else:
                builder.emit("DELETE_ITEM")
        elif choice == 9:
            self.expr(builder, "bool", 0)
            builder.emit("ASSERT", "assertion failed")
        elif choice == 10 and rng.random() < 0.2:
            self.expr(builder, "str", 1)
            builder.emit("REVERT")
        elif choice == 11 and rng.random() < 0.3:
            self.ret(builder)
        elif choice == 12:
            procedures = [s for s in self.signatures if s[2] == NONE]
            if procedures:
                procedure, procedure_params, _, _ = rng.choice(procedures)
                for param in procedure_params:
                    self.expr(builder, TAG_OF[param], 1)
                builder.emit("CALL", procedure)
        else:
            tag = rng.choice(TAGS)
            self.expr(builder, tag, 0)
            builder.emit("POP")

    def expr(self, builder: FunctionBuilder, tag: str, depth: int) -> None:
        rng = self.rng
        if rng.random() < self.noise:
            if rng.random() < 0.7:
                self.untyped.leaf(builder)
            else:
                self.untyped.expr(builder, 2)
            return
        if depth >= 3 or rng.random() < 0.3:
            self.leaf(builder, tag)
            return
        getattr(self, f"expr_{tag}")(builder, depth + 1)

    def leaf(self, builder: FunctionBuilder, tag: str) -> None:
        rng = self.rng
        local = self.local_of(tag)
        if local is not None and rng.random() < 0.5:
            builder.emit("LOAD", local)
        elif tag == "int":
            builder.emit("PUSH_INT", rng.choice([0, 1, 2, 3, -1, 10, 1000])) if rng.random() < 0.9 else builder.emit("PUSH_CONST", rng.choice(INTS))
        elif tag == "str":
            builder.emit("PUSH_CONST", rng.choice(STRINGS))
        elif tag == "bool":
            builder.emit(rng.choice(["PUSH_TRUE", "PUSH_FALSE"]))
        elif tag == "list":
            count = rng.randint(0, 3)
            for _ in range(count):
                builder.emit("PUSH_INT", rng.randint(-2, 5))
            builder.emit("BUILD_LIST", count)
        elif tag == "dict":
            count = rng.randint(0, 2)
            for key in rng.sample(["a", "b", "k", "abc"], count):
                builder.emit("PUSH_CONST", key).emit("PUSH_INT", rng.randint(0, 9))
            builder.emit("BUILD_DICT", count)
        elif tag == "acc":
            if rng.random() < 0.5:
                builder.emit("CALL_BUILTIN", rng.choice(["sender", "self"]), 0)
            else:
                builder.emit("PUSH_INT", rng.choice([p.id for p in PEERS] + [SELF.id, 7])).emit("CALL_BUILTIN", "Account", 1)
        else:
            builder.emit("PUSH_INT", RAPID.id).emit("CALL_BUILTIN", "Currency", 1)

    def call_value(self, builder: FunctionBuilder, tag: str, depth: int) -> bool:
        rng = self.rng
        internal = [s for s in self.signatures if s[2] != NONE and TAG_OF[s[2]] == tag]
        public = [s for s in self.signatures if s[3] and TAG_OF[s[2]] == tag]
        if internal and rng.random() < 0.5:
            name, params, _, _ = rng.choice(internal)
            for param in params:
                self.expr(builder, TAG_OF[param], depth)
            builder.emit("CALL", name)
            return True
        if public:
            name, params, returns, _ = rng.choice(public)
            builder.emit("PUSH_INT", rng.choice(PEERS).id).emit("CALL_BUILTIN", "Account", 1).emit("PUSH_CONST", name)
            for param in params:
                self.expr(builder, TAG_OF[param], depth + 1)
            flags = 0
            if rng.random() < 0.3:
                flags |= 1
                builder.emit("PUSH_INT", RAPID.id).emit("CALL_BUILTIN", "Currency", 1).emit("PUSH_INT", rng.randint(1, 5))
            if rng.random() < 0.5:
                flags |= 2
                builder.emit("PUSH_INT", rng.choice([20, 80, 300, 10_000]))
            builder.emit("CALL_CONTRACT", flags, len(params)).emit("CHECK_TYPE", returns)
            return True
        return False

    def try_call(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        public = [s for s in self.signatures if s[3]]
        name, params, _, _ = rng.choice(public)
        builder.emit("PUSH_INT", rng.choice([p.id for p in PEERS] + [SELF.id, 99])).emit("CALL_BUILTIN", "Account", 1).emit("PUSH_CONST", name)
        for param in params:
            self.expr(builder, TAG_OF[param], depth + 1)
        flags = 2 if rng.random() < 0.6 else 0
        if flags:
            builder.emit("PUSH_INT", rng.choice([5, 40, 200]))
        builder.emit("TRY_CALL", flags, len(params))

    def expr_int(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        choice = rng.randrange(14)
        if choice <= 2:
            self.expr(builder, "int", depth)
            self.expr(builder, "int", depth)
            builder.emit(rng.choice(["ADD", "SUB", "MUL", "FLOORDIV", "MOD", "ADD", "SUB"]))
        elif choice == 3:
            self.expr(builder, rng.choice(["str", "list", "dict"]), depth)
            builder.emit("LEN")
        elif choice == 4:
            tag = rng.choice(["str", "acc", "bool", "cur"])
            self.expr(builder, tag, depth)
            builder.emit("TO_INT")
        elif choice == 5:
            name, params = rng.choice(
                [
                    ("now", []),
                    ("balance", ["acc", "cur"]),
                    ("allowance", ["acc", "acc", "cur"]),
                    ("staked", ["acc", "cur"]),
                    ("min", ["int", "int"]),
                    ("max", ["int", "int"]),
                    ("abs", ["int"]),
                    ("random", ["int", "int"]),
                    ("str.find", ["str", "str"]),
                    ("create_claim", ["acc", "cur", "int", "str"]),
                    ("swap", ["cur", "cur", "int", "int"]),
                ]
            )
            for tag in params:
                self.expr(builder, tag, depth)
            builder.emit("CALL_BUILTIN", name, len(params))
        elif choice == 6:
            builder.emit("CALL_BUILTIN", "paid", 0).emit("GET_FIELD", "Paid", "amount")
        elif choice == 7:
            self.expr(builder, "list", depth)
            self.expr(builder, "int", depth)
            builder.emit("INDEX_GET")
        elif choice == 8:
            self.expr(builder, "dict", depth)
            self.expr(builder, "str", depth)
            self.expr(builder, "int", depth)
            builder.emit("CALL_BUILTIN", "dict.get", 3)
        elif choice == 9:
            self.expr(builder, "str", depth)
            self.expr(builder, "int", depth)
            builder.emit("CALL_BUILTIN", "storage.get", 2).emit("CHECK_TYPE", INT)
        elif choice == 10:
            self.expr(builder, "list", depth)
            builder.emit("LIST_POP", 0)
        elif choice == 11 and self.call_value(builder, "int", depth):
            return
        elif choice == 12:
            self.expr(builder, "int", depth)
            builder.emit("NEG")
        else:
            self.leaf(builder, "int")

    def expr_str(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        choice = rng.randrange(11)
        if choice <= 1:
            self.expr(builder, "str", depth)
            self.expr(builder, "str", depth)
            builder.emit("CONCAT")
        elif choice == 2:
            flags = rng.randrange(4)
            self.expr(builder, "str", depth)
            for bit in (1, 2):
                if flags & bit:
                    self.expr(builder, "int", depth)
            builder.emit("SLICE", flags)
        elif choice == 3:
            self.expr(builder, rng.choice(["int", "acc", "bool", "cur", "str"]), depth)
            builder.emit("TO_STR")
        elif choice == 4:
            self.expr(builder, "str", depth)
            builder.emit("CALL_BUILTIN", rng.choice(["sha256", "str.upper", "str.lower", "str.strip"]), 1)
        elif choice == 5:
            self.expr(builder, "str", depth)
            self.expr(builder, "str", depth)
            self.expr(builder, "str", depth)
            builder.emit("CALL_BUILTIN", "str.replace", 3)
        elif choice == 6:
            self.expr(builder, "str", depth)
            self.expr(builder, "str", depth)
            self.expr(builder, "str", depth)
            builder.emit("CALL_BUILTIN", "str.split", 2).emit("CALL_BUILTIN", "str.join", 2)
        elif choice == 7:
            self.expr(builder, "str", depth)
            self.expr(builder, "int", depth)
            builder.emit("INDEX_GET")
        elif choice == 8:
            self.try_call(builder, depth)
            builder.emit("GET_FIELD", "CallResult", rng.choice(["message", "error"]))
        elif choice == 9 and self.call_value(builder, "str", depth):
            return
        else:
            self.leaf(builder, "str")

    def expr_bool(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        choice = rng.randrange(9)
        if choice <= 2:
            tag = rng.choice(["int", "int", "str"])
            self.expr(builder, tag, depth)
            self.expr(builder, tag, depth)
            builder.emit(rng.choice(["LT", "LE", "GT", "GE", "EQ", "NE"]))
        elif choice == 3:
            tag = rng.choice(["acc", "cur", "bool"])
            self.expr(builder, tag, depth)
            self.expr(builder, tag, depth)
            builder.emit(rng.choice(["EQ", "NE"]))
        elif choice == 4:
            self.expr(builder, "bool", depth)
            builder.emit("NOT")
        elif choice == 5:
            needle, container = rng.choice([("int", "list"), ("str", "str"), ("str", "dict")])
            self.expr(builder, needle, depth)
            self.expr(builder, container, depth)
            builder.emit("CONTAINS")
        elif choice == 6:
            self.try_call(builder, depth)
            builder.emit("GET_FIELD", "CallResult", "ok")
        elif choice == 7:
            self.expr(builder, "int", depth)
            self.expr(builder, "acc", depth)
            self.expr(builder, "int", depth)
            builder.emit("CALL_BUILTIN", "discord.has_role", 3)
        elif self.call_value(builder, "bool", depth):
            return
        else:
            self.leaf(builder, "bool")

    def expr_list(self, builder: FunctionBuilder, depth: int) -> None:
        rng = self.rng
        choice = rng.randrange(6)
        if choice == 0:
            count = rng.randint(0, 3)
            for _ in range(count):
                self.expr(builder, "int", depth)
            builder.emit("BUILD_LIST", count)
        elif choice == 1:
            self.expr(builder, "list", depth)
            self.expr(builder, "list", depth)
            builder.emit("CONCAT")
        elif choice == 2:
            self.expr(builder, "list", depth)
            self.expr(builder, "int", depth)
            builder.emit("SLICE", 1)
        elif choice == 3:
            self.expr(builder, "dict", depth)
            builder.emit("CALL_BUILTIN", "dict.values", 1)
        elif choice == 4:
            self.expr(builder, "str", depth)
            self.expr(builder, "list", depth)
            builder.emit("CALL_BUILTIN", "storage.get", 2).emit("CHECK_TYPE", list_of(INT))
        elif not self.call_value(builder, "list", depth):
            self.leaf(builder, "list")

    def expr_dict(self, builder: FunctionBuilder, depth: int) -> None:
        count = self.rng.randint(0, 2)
        for _ in range(count):
            self.expr(builder, "str", depth)
            self.expr(builder, "int", depth)
        builder.emit("BUILD_DICT", count)

    def expr_acc(self, builder: FunctionBuilder, depth: int) -> None:
        if self.rng.random() < 0.3:
            self.expr(builder, "int", depth)
            builder.emit("CALL_BUILTIN", "claim_info", 1).emit("GET_FIELD", "ClaimInfo", "payer")
        else:
            self.leaf(builder, "acc")

    def expr_cur(self, builder: FunctionBuilder, depth: int) -> None:
        choice = self.rng.randrange(3)
        if choice == 0:
            builder.emit("CALL_BUILTIN", "paid", 0).emit("GET_FIELD", "Paid", "currency")
        elif choice == 1:
            self.expr(builder, "str", depth)
            builder.emit("CALL_BUILTIN", "currency", 1)
        else:
            self.leaf(builder, "cur")


def random_arg(rng: random.Random, t: Type) -> object:
    kind = t.kind
    if kind is Kind.INT:
        return rng.choice([0, 1, 3, -5, 2**200, rng.randint(-1000, 1000)])
    if kind is Kind.STR:
        return rng.choice(STRINGS)
    if kind is Kind.BOOL:
        return rng.random() < 0.5
    if kind is Kind.ACCOUNT:
        return rng.choice([SELF, SENDER, *PEERS, Account(rng.randint(1, 10**6))])
    return rng.choice([RAPID, Currency(rng.randint(1, 10**6))])


def check_binary(data: bytes, rng: random.Random, stats: collections.Counter[str] | None = None) -> bool:
    """Verify data and, when accepted, run each public function; return whether it was accepted."""
    try:
        verified = verify(data, max_size=MAX_SIZE)
    except VerificationError:
        return False
    run_program(verified, rng, stats)
    return True


def run_program(verified: VerifiedProgram, rng: random.Random, stats: collections.Counter[str] | None = None, *, checking: bool = False, limits: Limits | None = None) -> None:
    """Run each public function with random args; with checking, the memory meter asserts its upper-bound invariant at every allocation."""
    limits = limits or Limits(wall_clock_seconds=30.0)
    for fn in verified.public_functions:
        assert all(param.kind in PUBLIC_PARAM_KINDS for param in fn.params)
        contracts = dict.fromkeys([*PEERS, SELF], verified)
        host = FakeHost(contracts=contracts, balances={(SELF, RAPID): 50, (PEERS[0], RAPID): 50}, rng=seeded(rng.random()))
        ctx = checking_context(gas=GAS, limits=limits) if checking else ExecutionContext.top_level(self_account=SELF, sender=SENDER, paid=None, now=1, gas_limit=GAS, limits=limits)
        args = [random_arg(rng, param) for param in fn.params]
        try:
            outcome = run_sync(execute(verified, fn.name, args, host=host, context=ctx))
        except ContractFailure as exc:
            if stats is not None:
                stats[type(exc).__name__] += 1
                stats["gas"] += ctx.gas.used
                stats["host_calls"] += len(host.calls)
            continue
        assert type(outcome.value) in (int, str, bool)
        assert outcome.gas_used <= GAS
        if stats is not None:
            stats["ok"] += 1
            stats["gas"] += outcome.gas_used
            stats["host_calls"] += len(host.calls)


def mutate(data: bytes, rng: random.Random) -> bytes:
    buf = bytearray(data)
    choice = rng.randrange(7)
    if choice == 0 and buf:
        position = rng.randrange(len(buf))
        buf[position] ^= 1 << rng.randrange(8)
    elif choice == 1 and buf:
        del buf[rng.randrange(len(buf)) :]
    elif choice == 2:
        position = rng.randrange(len(buf) + 1)
        buf[position:position] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 4)))
    elif choice == 3 and len(buf) > 20:
        position = rng.randrange(20, len(buf))
        del buf[position : position + rng.randint(1, 4)]
    elif choice == 4 and len(buf) >= HEADER.size:
        field = rng.choice([8, 12, 16])
        current = struct.unpack_from("<I", buf, field)[0]
        struct.pack_into("<I", buf, field, max(0, min(2**32 - 1, current + rng.randint(-3, 3))))
    elif choice == 5 and len(buf) > 20:
        position = rng.randrange(20, len(buf))
        buf[position] = rng.randrange(256)
    elif choice == 6 and len(buf) > 21:
        start = rng.randrange(20, len(buf) - 1)
        end = min(len(buf), start + rng.randint(1, 8))
        buf[start:end] = bytes(reversed(buf[start:end]))
    return bytes(buf)


def test_typed_programs_verify_and_run_deep() -> None:
    rng = seeded(20261031)
    stats: collections.Counter[str] = collections.Counter()
    total = 400 * SCALE
    for _ in range(total):
        assert check_binary(TypedGenerator(rng).program().assemble(), rng, stats)
    runs = sum(stats[key] for key in ("ok", "ContractRevert", "ContractRuntimeError", "OutOfGas", "CallFailed", "LimitExceeded"))
    assert stats["ok"] >= runs * 0.1
    assert stats["host_calls"] >= runs


def test_memory_accounting_bounds_the_exact_size_in_random_programs() -> None:
    rng = seeded(4242)
    before = CheckingMeter.checks
    for _ in range(60 * SCALE):
        for limit in (2048, 16 * 1024):
            data = TypedGenerator(rng).program().assemble()
            try:
                verified = verify(data, max_size=MAX_SIZE)
            except VerificationError:
                continue
            run_program(verified, rng, checking=True, limits=Limits(wall_clock_seconds=30.0, memory_max_bytes=limit))
    assert CheckingMeter.checks > before


def test_untyped_programs_mostly_verify_and_run() -> None:
    rng = seeded(1031)
    accepted = 0
    total = 300 * SCALE
    for _ in range(total):
        accepted += check_binary(Generator(rng).program().assemble(), rng)
    assert accepted >= total * 0.9


def test_mutated_binaries() -> None:
    rng = seeded(42)
    seeds = [TypedGenerator(rng).program().assemble() for _ in range(20 * SCALE)] + [Generator(rng).program().assemble() for _ in range(10 * SCALE)]
    accepted = 0
    for _ in range(4000 * SCALE):
        data = rng.choice(seeds)
        for _ in range(rng.randint(1, 3)):
            data = mutate(data, rng)
        accepted += check_binary(data, rng)
    assert accepted > 0


def test_random_bytes() -> None:
    rng = seeded(7)
    header = encode(Generator(seeded(1)).program().program())[:20]
    for _ in range(3000 * SCALE):
        body = bytes(rng.randrange(256) for _ in range(rng.randint(0, 60)))
        check_binary(body, rng)
        lengths = [rng.randint(0, 20) for _ in range(3)]
        check_binary(HEADER.pack(b"RWC1", 1, 0, *lengths) + bytes(rng.randrange(256) for _ in range(sum(lengths))), rng)
        check_binary(header + body, rng)


@settings(max_examples=150 * SCALE, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.binary(max_size=200))
def test_hypothesis_random_bytes(data: bytes) -> None:
    check_binary(data, seeded(0))
    check_binary(b"RWC1\x01\x00\x00\x00" + data, seeded(0))


@settings(max_examples=60 * SCALE, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.randoms(use_true_random=False), st.booleans(), st.lists(st.integers(min_value=0, max_value=2**32 - 1), max_size=4))
def test_hypothesis_generated_and_mutated(rng: random.Random, typed: bool, noise: list[int]) -> None:
    data = (TypedGenerator(rng) if typed else Generator(rng)).program().assemble()
    check_binary(data, rng)
    for value in noise:
        mutated = bytearray(data)
        position = value % (len(mutated) + 1)
        mutated[position:position] = value.to_bytes(4, "little")
        check_binary(bytes(mutated), rng)
