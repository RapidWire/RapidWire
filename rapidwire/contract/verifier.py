"""Registration checks of a contract binary, producing the pre-decoded program the VM executes.

verify() applies every structural check and a stack-depth dataflow analysis; it does not infer operand types.
Operand types are checked dynamically by the VM, and a mismatch is a ContractRuntimeError (a defined, catchable contract failure), so a verified program never needs format error handling at run time.
Beyond the listed checks, the function code ranges must tile the code section exactly, so a binary carries no bytes outside its functions, and no reachable loop may consist of free instructions only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from rapidwire.contract.builtins import Builtin
from rapidwire.contract.costs import free_instruction_weight
from rapidwire.contract.errors import FormatError, VerificationError
from rapidwire.contract.format import Constant, FunctionEntry, Program, decode
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.opcodes import (
    CALL_ARGS_LIST,
    CALL_ATTACH,
    CALL_FLAGS_MASK,
    CALL_GAS,
    INSTRUCTION_SETS,
    LIST_POP_FLAGS_MASK,
    LIST_POP_INDEX,
    SLICE_FLAGS_MASK,
    Flow,
    InstructionSet,
    Op,
    OperandKind,
    OpInfo,
    decode_instruction,
)
from rapidwire.contract.types import NONE, PUBLIC_PARAM_KINDS, PUBLIC_RETURN_KINDS, RECORD_FIELDS, RECORD_KINDS, Kind, Type, type_problem
from rapidwire.contract.values import PYTHON_TYPES, Record

NAME_MAX_LENGTH = 64
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

Instr = tuple[int, int, Any, Any, Any, Any, Any]
"""A pre-decoded instruction: (opcode, gas, operand 1 .. operand 5), unused operands 0; gas is the base cost (the builtin's cost for CALL_BUILTIN) when positive and minus the free run weight (costs.free_instruction_weight) for a free instruction, jump targets are instruction indexes and constant operands are the constant values."""


@dataclass(frozen=True, slots=True)
class Listing:
    """One decoded instruction as it appears in the binary: byte offset within the function, opcode info and raw operands."""

    offset: int
    info: OpInfo
    operands: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VerifiedFunction:
    index: int
    name: str
    public: bool
    params: tuple[Type, ...]
    returns: Type
    local_count: int
    code: tuple[Instr, ...]
    listing: tuple[Listing, ...]
    max_stack: int
    entry: FunctionEntry
    param_types: tuple[type, ...] | None = None
    """The exact Python type of every parameter (Record for a record type), or None when a parameter is a Value."""
    record_positions: tuple[int, ...] = ()
    """Positions of the record parameters, which need a kind check beyond param_types."""
    record_kinds: tuple[Kind, ...] = ()
    """Kinds of the record parameters, in the order of record_positions."""

    @property
    def returns_value(self) -> bool:
        return self.returns != NONE

    def signature(self) -> str:
        params = ", ".join(str(param) for param in self.params)
        return f"{self.name}({params}) -> {self.returns}"


@dataclass(frozen=True, slots=True)
class VerifiedProgram:
    """A binary that passed verify(): the original bytes, the decoded program and the pre-decoded functions in table order."""

    data: bytes
    program: Program
    instruction_set: InstructionSet
    functions: tuple[VerifiedFunction, ...]
    by_name: Mapping[str, VerifiedFunction]
    max_constant_int: int
    max_constant_str: int
    sha256: str = field(default="")

    def function(self, name: str) -> VerifiedFunction | None:
        return self.by_name.get(name)

    def public_function(self, name: str) -> VerifiedFunction | None:
        """The public function called name, or None when there is none."""
        fn = self.by_name.get(name)
        return fn if fn is not None and fn.public else None

    @property
    def public_functions(self) -> tuple[VerifiedFunction, ...]:
        return tuple(fn for fn in self.functions if fn.public)


def verify(raw: bytes | bytearray | memoryview, *, max_size: int, limits: Limits = DEFAULT_LIMITS) -> VerifiedProgram:
    """Run every registration check on raw and return the verified program; raises VerificationError (FormatError for unparseable bytes) and nothing else."""
    if not isinstance(raw, bytes | bytearray | memoryview):
        raise FormatError("binary must be bytes")
    data = bytes(raw)
    if len(data) > max_size:
        raise VerificationError(f"binary is {len(data)} bytes, larger than the {max_size}-byte limit")
    program = decode(data)
    iset = INSTRUCTION_SETS.get(program.version)
    if iset is None:
        raise FormatError(f"unsupported format version {program.version}")
    max_int, max_str = _check_constants(program.constants, limits)
    names = _check_functions(program, limits)
    drafts = [_verify_function(program, iset, index, names[index], limits) for index in range(len(program.functions))]
    for draft in drafts:
        _reject_free_loops(draft)
    functions = tuple(_finish(draft, iset) for draft in drafts)
    return VerifiedProgram(
        data=data,
        program=program,
        instruction_set=iset,
        functions=functions,
        by_name=MappingProxyType({fn.name: fn for fn in functions}),
        max_constant_int=max_int,
        max_constant_str=max_str,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _check_constants(constants: tuple[Constant, ...], limits: Limits) -> tuple[int, int]:
    max_int = 0
    max_str = 0
    for index, constant in enumerate(constants):
        if type(constant) is str:
            if len(constant) > limits.str_max_length:
                raise VerificationError(f"constant {index} is a str longer than {limits.str_max_length} characters")
            max_str = max(max_str, len(constant))
        elif type(constant) is int:
            if not limits.int_ok(constant):
                raise VerificationError(f"constant {index} is an int outside the {limits.int_bits}-bit limit")
            max_int = max(max_int, abs(constant))
        elif type(constant) is Type:
            problem = type_problem(constant)
            if problem is not None:
                raise VerificationError(f"constant {index} is not a valid type: {problem}")
    return max_int, max_str


def _check_functions(program: Program, limits: Limits) -> list[str]:
    functions = program.functions
    if not functions:
        raise VerificationError("binary defines no functions")
    names: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(functions):
        where = f"function {index}"
        if entry.name_index >= len(program.constants) or type(program.constants[entry.name_index]) is not str:
            raise VerificationError(f"{where}: name must refer to a str constant")
        name = program.constants[entry.name_index]
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or len(name) > NAME_MAX_LENGTH:
            raise VerificationError(f"{where}: name is not an identifier of at most {NAME_MAX_LENGTH} ASCII characters")
        if name.startswith("__"):
            raise VerificationError(f"{where}: names beginning with __ are not allowed")
        if name in seen:
            raise VerificationError(f"{where}: duplicate function name {name}")
        seen.add(name)
        names.append(name)
        where = f"function {name}"
        if len(entry.params) > limits.max_params:
            raise VerificationError(f"{where}: more than {limits.max_params} parameters")
        for position, param in enumerate(entry.params):
            problem = type_problem(param)
            if problem is not None:
                raise VerificationError(f"{where}: parameter {position}: {problem}")
            if entry.public and param.kind not in PUBLIC_PARAM_KINDS:
                raise VerificationError(f"{where}: public parameter {position} must be int, str, bool, Account or Currency, not {param}")
        problem = type_problem(entry.returns, allow_none=True)
        if problem is not None:
            raise VerificationError(f"{where}: return type: {problem}")
        if entry.public and entry.returns.kind not in PUBLIC_RETURN_KINDS:
            raise VerificationError(f"{where}: public return type must be int, str or bool, not {entry.returns}")
        if not len(entry.params) <= entry.local_count <= limits.max_locals:
            raise VerificationError(f"{where}: local count must be between the parameter count and {limits.max_locals}")
        if entry.code_length < 1 or entry.code_offset + entry.code_length > len(program.code):
            raise VerificationError(f"{where}: code range is empty or outside the code section")
    position = 0
    for entry in sorted(functions, key=lambda e: e.code_offset):
        if entry.code_offset < position:
            raise VerificationError("function code ranges overlap")
        if entry.code_offset > position:
            raise VerificationError(f"code bytes {position}..{entry.code_offset - 1} belong to no function")
        position = entry.code_offset + entry.code_length
    if position != len(program.code):
        raise VerificationError(f"code bytes from {position} belong to no function")
    return names


@dataclass(frozen=True, slots=True)
class _Draft:
    function: VerifiedFunction
    depths: list[int | None]
    where: str


def _successors(item: Listing, ins: Instr, position: int) -> tuple[int, ...]:
    flow = item.info.flow
    if flow is Flow.RETURN or flow is Flow.REVERT:
        return ()
    if flow is Flow.JUMP:
        return (ins[2],)
    if flow is Flow.BRANCH:
        return (position + 1, ins[2])
    if flow is Flow.LOOP:
        return (position + 1, ins[6] if item.info.op is Op.FOR_RANGE else ins[5])
    return (position + 1,)


def _reject_free_loops(draft: _Draft) -> None:
    """Raise VerificationError when a reachable loop consists of free instructions only (a depth-first search over the free instructions)."""
    fn = draft.function
    code = fn.code
    count = len(code)
    free = [draft.depths[i] is not None and code[i][1] == 0 for i in range(count)]
    successors = [_successors(fn.listing[i], code[i], i) for i in range(count)]
    state = [0] * count
    for start in range(count):
        if not free[start] or state[start]:
            continue
        state[start] = 1
        stack = [(start, iter(successors[start]))]
        while stack:
            node, pending = stack[-1]
            for successor in pending:
                if successor < count and free[successor]:
                    if state[successor] == 1:
                        raise VerificationError(f"{draft.where} at offset {fn.listing[successor].offset}: a loop must contain an instruction with a gas cost")
                    if state[successor] == 0:
                        state[successor] = 1
                        stack.append((successor, iter(successors[successor])))
                        break
            else:
                stack.pop()
                state[node] = 2


def _finish(draft: _Draft, iset: InstructionSet) -> VerifiedFunction:
    fn = draft.function
    code: list[Instr] = []
    for ins in fn.code:
        gas = ins[1]
        if not gas:
            builtin = iset.builtins[ins[2]].name if ins[0] == Op.CALL_BUILTIN else None
            gas = -free_instruction_weight(Op(ins[0]).name, builtin)
        code.append((ins[0], gas, ins[2], ins[3], ins[4], ins[5], ins[6]))
    types = [Record if param.kind in RECORD_KINDS else PYTHON_TYPES.get(param.kind) for param in fn.params]
    param_types = None if None in types else tuple(t for t in types if t is not None)
    record_positions = tuple(position for position, param in enumerate(fn.params) if param.kind in RECORD_KINDS)
    record_kinds = tuple(fn.params[position].kind for position in record_positions)
    return dataclasses.replace(fn, code=tuple(code), param_types=param_types, record_positions=record_positions, record_kinds=record_kinds)


def _verify_function(program: Program, iset: InstructionSet, index: int, name: str, limits: Limits) -> _Draft:
    entry = program.functions[index]
    where = f"function {name}"
    start = entry.code_offset
    end = start + entry.code_length
    listing: list[Listing] = []
    pos = start
    while pos < end:
        try:
            info, operands, next_pos = decode_instruction(iset, program.code, pos, end)
        except FormatError as exc:
            raise VerificationError(f"{where} at offset {pos - start}: {exc}") from None
        listing.append(Listing(pos - start, info, operands))
        pos = next_pos
    index_of = {item.offset: position for position, item in enumerate(listing)}
    code = tuple(_predecode(program, iset, entry, where, item, index_of, limits) for item in listing)
    max_stack, depths = _check_stack(program, iset, entry, where, listing, code, limits)
    function = VerifiedFunction(
        index=index,
        name=name,
        public=entry.public,
        params=entry.params,
        returns=entry.returns,
        local_count=entry.local_count,
        code=code,
        listing=tuple(listing),
        max_stack=max_stack,
        entry=entry,
    )
    return _Draft(function, depths, where)


def _predecode(program: Program, iset: InstructionSet, entry: FunctionEntry, where: str, item: Listing, index_of: Mapping[int, int], limits: Limits) -> Instr:
    info = item.info
    op = info.op
    here = f"{where} at offset {item.offset} ({info.mnemonic})"
    values: list[Any] = []
    gas = info.gas
    for kind, raw in zip(info.operands, item.operands, strict=True):
        if kind is OperandKind.SINT:
            values.append(raw)
        elif kind is OperandKind.CONST:
            if raw >= len(program.constants):
                raise VerificationError(f"{here}: constant {raw} does not exist")
            constant = program.constants[raw]
            wanted: tuple[type, ...] = {Op.PUSH_CONST: (str, int), Op.ASSERT: (str,), Op.CHECK_TYPE: (Type,)}[op]
            if type(constant) not in wanted:
                raise VerificationError(f"{here}: constant {raw} has the wrong kind")
            values.append(constant)
        elif kind is OperandKind.LOCAL:
            if raw >= entry.local_count:
                raise VerificationError(f"{here}: local {raw} does not exist")
            values.append(raw)
        elif kind is OperandKind.TARGET:
            target = index_of.get(raw)
            if target is None:
                raise VerificationError(f"{here}: jump target {raw} is not an instruction start in this function")
            values.append(target)
        elif kind is OperandKind.FUNC:
            if raw >= len(program.functions):
                raise VerificationError(f"{here}: function {raw} does not exist")
            values.append(raw)
        elif kind is OperandKind.BUILTIN:
            if raw >= len(iset.builtins):
                raise VerificationError(f"{here}: builtin {raw} does not exist")
            gas = iset.builtins[raw].gas
            values.append(raw)
        elif kind is OperandKind.COUNT:
            values.append(raw)
        elif kind is OperandKind.FLAGS:
            mask = {Op.SLICE: SLICE_FLAGS_MASK, Op.LIST_POP: LIST_POP_FLAGS_MASK, Op.CALL_CONTRACT: CALL_FLAGS_MASK, Op.TRY_CALL: CALL_FLAGS_MASK}[op]
            if raw & ~mask:
                raise VerificationError(f"{here}: undefined flag bits {raw:#x}")
            values.append(raw)
        elif kind is OperandKind.RECORD:
            if raw not in {int(k) for k in RECORD_KINDS}:
                raise VerificationError(f"{here}: {raw} is not a record kind")
            values.append(Kind(raw))
        else:
            record_kind = values[-1]
            if not isinstance(record_kind, Kind) or raw >= len(RECORD_FIELDS[record_kind]):
                raise VerificationError(f"{here}: record {record_kind.name} has no field {raw}")
            values.append(raw)
    if op is Op.CALL_BUILTIN:
        builtin: Builtin = iset.builtins[values[0]]
        if not builtin.accepts(values[1]):
            expected = str(builtin.arity) if builtin.required == builtin.arity else f"{builtin.required} to {builtin.arity}"
            raise VerificationError(f"{here}: builtin {builtin.name} takes {expected} arguments, not {values[1]}")
    elif op in (Op.CALL_CONTRACT, Op.TRY_CALL):
        flags, argc = values
        if flags & CALL_ARGS_LIST and argc:
            raise VerificationError(f"{here}: argument count must be 0 when the arguments are passed as a list")
        if argc > limits.max_params:
            raise VerificationError(f"{here}: more than {limits.max_params} arguments")
    elif op in (Op.BUILD_LIST, Op.BUILD_DICT):
        width = values[0] * (2 if op is Op.BUILD_DICT else 1)
        if width > limits.max_stack_depth:
            raise VerificationError(f"{here}: element count exceeds the stack limit")
    values.extend([0] * (5 - len(values)))
    return (int(op), gas, values[0], values[1], values[2], values[3], values[4])


def stack_effect(program: Program, iset: InstructionSet, entry: FunctionEntry, info: OpInfo, operands: tuple[int, ...]) -> tuple[int, int]:
    """(pops, pushes) of one instruction with its raw operands inside the function described by entry."""
    op = info.op
    if info.pops is not None and info.pushes is not None:
        return info.pops, info.pushes
    if op is Op.CALL:
        callee = program.functions[operands[0]]
        return len(callee.params), 0 if callee.returns == NONE else 1
    if op is Op.RET:
        return (0 if entry.returns == NONE else 1), 0
    if op is Op.CALL_BUILTIN:
        builtin = iset.builtins[operands[0]]
        return operands[1], builtin.pushes
    if op in (Op.CALL_CONTRACT, Op.TRY_CALL):
        flags, argc = operands
        pops = 2 + (1 if flags & CALL_ARGS_LIST else argc) + (2 if flags & CALL_ATTACH else 0) + (1 if flags & CALL_GAS else 0)
        return pops, 1
    if op is Op.SLICE:
        return 1 + bin(operands[0]).count("1"), 1
    if op is Op.BUILD_LIST:
        return operands[0], 1
    if op is Op.BUILD_DICT:
        return 2 * operands[0], 1
    if op is Op.LIST_POP:
        return 1 + (1 if operands[0] & LIST_POP_INDEX else 0), 1
    raise AssertionError(f"no stack effect rule for {info.mnemonic}")


def _check_stack(program: Program, iset: InstructionSet, entry: FunctionEntry, where: str, listing: list[Listing], code: tuple[Instr, ...], limits: Limits) -> tuple[int, list[int | None]]:
    """(maximum stack depth, depth before each instruction or None when it is unreachable)."""
    count = len(listing)
    depths: list[int | None] = [None] * count
    depths[0] = 0
    work = [0]
    max_depth = 0
    while work:
        position = work.pop()
        item = listing[position]
        depth = depths[position] or 0
        here = f"{where} at offset {item.offset} ({item.info.mnemonic})"
        pops, pushes = stack_effect(program, iset, entry, item.info, item.operands)
        if depth < pops:
            raise VerificationError(f"{here}: stack underflow")
        after = depth - pops + pushes
        if after > limits.max_stack_depth:
            raise VerificationError(f"{here}: stack deeper than {limits.max_stack_depth}")
        max_depth = max(max_depth, after)
        if item.info.flow is Flow.RETURN and depth != pops:
            raise VerificationError(f"{here}: the stack must hold exactly the return value")
        for successor in _successors(item, code[position], position):
            if successor >= count:
                raise VerificationError(f"{here}: execution falls off the end of the function")
            known = depths[successor]
            if known is None:
                depths[successor] = after
                work.append(successor)
            elif known != after:
                raise VerificationError(f"{where} at offset {listing[successor].offset}: stack depth {known} on one path and {after} on another")
    return max_depth, depths
