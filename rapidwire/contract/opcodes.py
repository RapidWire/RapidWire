"""Instruction sets by format version: opcode byte, mnemonic, operand kinds, stack effect, control flow and base gas.

An instruction is one opcode byte followed by its operands, each a shortest-form uvarint (SINT operands are zigzag-encoded).
Jump targets are byte offsets from the start of the enclosing function and must be instruction starts in it.
pops or pushes is None when it depends on the operands or the program; verifier.stack_effect() applies the rules below.
Locals hold no value until stored (parameters are stored on entry); reading an empty local is a runtime error.

Version 1 operand stack conventions (top of stack last):

- FOR_RANGE counter stop step var exit: if step is 0 runtime error; if (step > 0 and counter < stop) or (step < 0 and counter > stop), store counter in var and counter + step in counter, else jump to exit.
- FOR_EACH seq index var exit: seq is a list or str local, index an int local; if index < len(seq), store seq[index] in var and index + 1 in index, else jump to exit.
- CALL fn: pops the callee's parameters (first parameter deepest) and pushes its result unless it returns None.
- CALL_BUILTIN id argc: argc is between the builtin's required count and its arity (see Builtin.accepts); pops the arguments and pushes the result unless the builtin returns None.
- CALL_CONTRACT and TRY_CALL flags argc, in push order: target (Account), function (str), then argc arguments or, with CALL_ARGS_LIST, one list of arguments (argc 0), then attach currency and amount with CALL_ATTACH, then the gas limit with CALL_GAS; push the result (CALL_CONTRACT, a Value) or a CallResult (TRY_CALL).
- SLICE flags, in push order: the container, then start with SLICE_START and stop with SLICE_STOP.
- Operands below are listed in push order (the last one on top): INDEX_GET container, key; INDEX_SET container, key, value; CONTAINS needle, container; APPEND list, value; DELETE_ITEM container, key; LIST_POP list and, with LIST_POP_INDEX, index; arithmetic and comparisons a, b.
- BUILD_LIST n pops n items; BUILD_DICT n pops n key, value pairs.
- ASSERT msg pops a bool and reverts with the str constant msg when it is False; REVERT pops the message str.
- GET_FIELD record field reads a field of a record of that kind; CHECK_TYPE type checks the top value against a type constant and leaves it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType

from rapidwire.contract.builtins import BUILTINS_V1, BUILTINS_V1_BY_NAME, Builtin
from rapidwire.contract.costs import OPCODE_GAS
from rapidwire.contract.errors import FormatError
from rapidwire.contract.format import FORMAT_VERSION, encode_uvarint, read_uvarint, zigzag_decode, zigzag_encode


class OperandKind(Enum):
    SINT = "sint"
    CONST = "const"
    LOCAL = "local"
    TARGET = "target"
    FUNC = "func"
    BUILTIN = "builtin"
    COUNT = "count"
    FLAGS = "flags"
    RECORD = "record"
    FIELD = "field"


class Flow(Enum):
    NEXT = "next"
    JUMP = "jump"
    BRANCH = "branch"
    LOOP = "loop"
    RETURN = "return"
    REVERT = "revert"


class Op(IntEnum):
    PUSH_INT = 0x01
    PUSH_CONST = 0x02
    PUSH_TRUE = 0x03
    PUSH_FALSE = 0x04
    POP = 0x05
    DUP = 0x06
    SWAP = 0x07
    LOAD = 0x08
    STORE = 0x09
    ADD = 0x10
    SUB = 0x11
    MUL = 0x12
    FLOORDIV = 0x13
    MOD = 0x14
    NEG = 0x15
    EQ = 0x18
    NE = 0x19
    LT = 0x1A
    LE = 0x1B
    GT = 0x1C
    GE = 0x1D
    NOT = 0x1E
    JUMP = 0x20
    JUMP_IF_FALSE = 0x21
    JUMP_IF_TRUE = 0x22
    FOR_RANGE = 0x23
    FOR_EACH = 0x24
    CALL = 0x25
    RET = 0x26
    CALL_BUILTIN = 0x27
    CALL_CONTRACT = 0x28
    TRY_CALL = 0x29
    REVERT = 0x2A
    ASSERT = 0x2B
    CONCAT = 0x30
    SLICE = 0x31
    INDEX_GET = 0x32
    INDEX_SET = 0x33
    LEN = 0x34
    CONTAINS = 0x35
    BUILD_LIST = 0x36
    BUILD_DICT = 0x37
    APPEND = 0x38
    LIST_POP = 0x39
    DELETE_ITEM = 0x3A
    GET_FIELD = 0x3B
    CHECK_TYPE = 0x3C
    TO_INT = 0x3D
    TO_STR = 0x3E


CALL_ATTACH = 0x01
CALL_GAS = 0x02
CALL_ARGS_LIST = 0x04
CALL_FLAGS_MASK = CALL_ATTACH | CALL_GAS | CALL_ARGS_LIST
SLICE_START = 0x01
SLICE_STOP = 0x02
SLICE_FLAGS_MASK = SLICE_START | SLICE_STOP
LIST_POP_INDEX = 0x01
LIST_POP_FLAGS_MASK = LIST_POP_INDEX


@dataclass(frozen=True, slots=True)
class OpInfo:
    """Static description of one opcode."""

    op: Op
    operands: tuple[OperandKind, ...]
    pops: int | None
    pushes: int | None
    flow: Flow
    gas: int
    summary: str

    @property
    def mnemonic(self) -> str:
        return self.op.name

    @property
    def code(self) -> int:
        return int(self.op)


_K = OperandKind


def _info(op: Op, operands: tuple[OperandKind, ...], pops: int | None, pushes: int | None, flow: Flow, summary: str) -> OpInfo:
    return OpInfo(op, operands, pops, pushes, flow, OPCODE_GAS[op.name], summary)


_N = Flow.NEXT
_OPS_V1: tuple[OpInfo, ...] = (
    _info(Op.PUSH_INT, (_K.SINT,), 0, 1, _N, "push an inline int"),
    _info(Op.PUSH_CONST, (_K.CONST,), 0, 1, _N, "push a str or int constant"),
    _info(Op.PUSH_TRUE, (), 0, 1, _N, "push True"),
    _info(Op.PUSH_FALSE, (), 0, 1, _N, "push False"),
    _info(Op.POP, (), 1, 0, _N, "discard the top value"),
    _info(Op.DUP, (), 1, 2, _N, "duplicate the top value"),
    _info(Op.SWAP, (), 2, 2, _N, "swap the two top values"),
    _info(Op.LOAD, (_K.LOCAL,), 0, 1, _N, "push a local"),
    _info(Op.STORE, (_K.LOCAL,), 1, 0, _N, "pop into a local"),
    _info(Op.ADD, (), 2, 1, _N, "int a + b"),
    _info(Op.SUB, (), 2, 1, _N, "int a - b"),
    _info(Op.MUL, (), 2, 1, _N, "int a * b"),
    _info(Op.FLOORDIV, (), 2, 1, _N, "int a // b (floor)"),
    _info(Op.MOD, (), 2, 1, _N, "int a % b (sign of b)"),
    _info(Op.NEG, (), 1, 1, _N, "int -a"),
    _info(Op.EQ, (), 2, 1, _N, "a == b for two scalars of the same type"),
    _info(Op.NE, (), 2, 1, _N, "a != b for two scalars of the same type"),
    _info(Op.LT, (), 2, 1, _N, "a < b for two ints or two strs"),
    _info(Op.LE, (), 2, 1, _N, "a <= b for two ints or two strs"),
    _info(Op.GT, (), 2, 1, _N, "a > b for two ints or two strs"),
    _info(Op.GE, (), 2, 1, _N, "a >= b for two ints or two strs"),
    _info(Op.NOT, (), 1, 1, _N, "bool not a"),
    _info(Op.JUMP, (_K.TARGET,), 0, 0, Flow.JUMP, "jump"),
    _info(Op.JUMP_IF_FALSE, (_K.TARGET,), 1, 0, Flow.BRANCH, "pop a bool and jump when it is False"),
    _info(Op.JUMP_IF_TRUE, (_K.TARGET,), 1, 0, Flow.BRANCH, "pop a bool and jump when it is True"),
    _info(Op.FOR_RANGE, (_K.LOCAL, _K.LOCAL, _K.LOCAL, _K.LOCAL, _K.TARGET), 0, 0, Flow.LOOP, "next step of a range loop"),
    _info(Op.FOR_EACH, (_K.LOCAL, _K.LOCAL, _K.LOCAL, _K.TARGET), 0, 0, Flow.LOOP, "next element of a list or str loop"),
    _info(Op.CALL, (_K.FUNC,), None, None, _N, "call an internal function"),
    _info(Op.RET, (), None, 0, Flow.RETURN, "return; the stack must hold exactly the result (or nothing for None)"),
    _info(Op.CALL_BUILTIN, (_K.BUILTIN, _K.COUNT), None, None, _N, "call a builtin"),
    _info(Op.CALL_CONTRACT, (_K.FLAGS, _K.COUNT), None, 1, _N, "call another contract; its failure fails the caller"),
    _info(Op.TRY_CALL, (_K.FLAGS, _K.COUNT), None, 1, _N, "call another contract and push a CallResult"),
    _info(Op.REVERT, (), 1, 0, Flow.REVERT, "pop a str and revert with it"),
    _info(Op.ASSERT, (_K.CONST,), 1, 0, _N, "pop a bool and revert with the str constant when it is False"),
    _info(Op.CONCAT, (), 2, 1, _N, "str + str or list + list"),
    _info(Op.SLICE, (_K.FLAGS,), None, 1, _N, "str or list slice with optional start and stop"),
    _info(Op.INDEX_GET, (), 2, 1, _N, "str[i], list[i] or dict[k]"),
    _info(Op.INDEX_SET, (), 3, 0, _N, "list[i] = v or dict[k] = v"),
    _info(Op.LEN, (), 1, 1, _N, "length of a str, list or dict"),
    _info(Op.CONTAINS, (), 2, 1, _N, "needle in str, list or dict"),
    _info(Op.BUILD_LIST, (_K.COUNT,), None, 1, _N, "build a list from n values"),
    _info(Op.BUILD_DICT, (_K.COUNT,), None, 1, _N, "build a dict from n key, value pairs"),
    _info(Op.APPEND, (), 2, 0, _N, "list.append(v)"),
    _info(Op.LIST_POP, (_K.FLAGS,), None, 1, _N, "list.pop() or list.pop(i)"),
    _info(Op.DELETE_ITEM, (), 2, 0, _N, "del list[i] or del dict[k]"),
    _info(Op.GET_FIELD, (_K.RECORD, _K.FIELD), 1, 1, _N, "read a record field"),
    _info(Op.CHECK_TYPE, (_K.CONST,), 1, 1, _N, "narrow a value to a type constant or fail"),
    _info(Op.TO_INT, (), 1, 1, _N, "int(x) of an int, bool, Account, Currency or decimal str"),
    _info(Op.TO_STR, (), 1, 1, _N, "str(x) of a scalar"),
)


@dataclass(frozen=True, slots=True)
class InstructionSet:
    """The opcodes and builtins of one format version."""

    version: int
    ops: Mapping[int, OpInfo]
    builtins: tuple[Builtin, ...]
    builtins_by_name: Mapping[str, Builtin]

    def by_mnemonic(self, mnemonic: str) -> OpInfo:
        for info in self.ops.values():
            if info.mnemonic == mnemonic:
                return info
        raise KeyError(mnemonic)


INSTRUCTION_SET_V1 = InstructionSet(
    version=FORMAT_VERSION,
    ops=MappingProxyType({info.code: info for info in _OPS_V1}),
    builtins=BUILTINS_V1,
    builtins_by_name=BUILTINS_V1_BY_NAME,
)

INSTRUCTION_SETS: Mapping[int, InstructionSet] = MappingProxyType({1: INSTRUCTION_SET_V1})


def encode_instruction(info: OpInfo, operands: tuple[int, ...]) -> bytes:
    """Encode one instruction with its raw operands (SINT operands as signed values); raises ValueError on a wrong operand count or range."""
    if len(operands) != len(info.operands):
        raise ValueError(f"{info.mnemonic} takes {len(info.operands)} operands, got {len(operands)}")
    out = bytearray([info.code])
    for kind, value in zip(info.operands, operands, strict=True):
        out += encode_uvarint(zigzag_encode(value) if kind is OperandKind.SINT else value)
    return bytes(out)


def decode_instruction(iset: InstructionSet, data: bytes, pos: int, end: int) -> tuple[OpInfo, tuple[int, ...], int]:
    """Decode the instruction at data[pos:end] into (info, raw operands, next position); raises FormatError for an undefined opcode or an instruction that does not end before end."""
    if pos >= end:
        raise FormatError(f"truncated instruction at code byte {pos}")
    info = iset.ops.get(data[pos])
    if info is None:
        raise FormatError(f"undefined opcode {data[pos]:#04x} at code byte {pos}")
    pos += 1
    operands: list[int] = []
    for kind in info.operands:
        value, pos = read_uvarint(data, pos, end)
        operands.append(zigzag_decode(value) if kind is OperandKind.SINT else value)
    return info, tuple(operands), pos
