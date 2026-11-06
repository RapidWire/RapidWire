"""A program builder, a text assembler and a disassembler for RWC1 binaries.

Text syntax, one item per line, ";" starting a comment outside string literals:

- `.const "text"`, `.const 123` or `.const type list[int]` appends a constant to the pool (always a new entry).
- `.function NAME [public] [params=T,T] [returns=T] [locals=N] [name=#N]` starts a function; returns defaults to None, locals to the smallest count covering the parameters and every local operand, name=#N picks the name constant instead of the first equal str constant.
- `LABEL:` marks the next instruction; `.end` closes the function.
- An instruction is its mnemonic and operands: CONST operands are #N, a JSON string literal or an int literal (interned) or, for CHECK_TYPE, a type; TARGET operands a label or a byte offset; FUNC operands a function name or index; BUILTIN operands a builtin name or id; RECORD a record type name; FIELD a field name; everything else an int.

disassemble() prints this syntax, and assemble(disassemble(p)) reproduces the binary whenever its functions are laid out in table order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from rapidwire.contract.format import FORMAT_VERSION, Constant, FunctionEntry, Program, encode
from rapidwire.contract.opcodes import INSTRUCTION_SETS, InstructionSet, Op, OperandKind, encode_instruction
from rapidwire.contract.types import KIND_NAMES, NONE, RECORD_FIELDS, RECORD_KINDS, Kind, Type, parse_type, record_field_index
from rapidwire.contract.values import format_value
from rapidwire.contract.verifier import VerifiedProgram


@dataclass(frozen=True, slots=True)
class ConstRef:
    """An explicit constant pool index used as a CONST operand."""

    index: int


Operand = int | str | Type | Kind | ConstRef


@dataclass
class _Item:
    mnemonic: str | None
    operands: tuple[Operand, ...] = ()
    label: str | None = None
    raw: bytes = b""


@dataclass
class FunctionBuilder:
    """Instructions of one function under construction; emit() and label() return the builder for chaining."""

    assembler: Assembler
    name: str
    params: tuple[Type, ...]
    returns: Type
    public: bool
    local_count: int | None
    name_index: int | None
    items: list[_Item] = field(default_factory=list)

    def label(self, name: str) -> FunctionBuilder:
        self.items.append(_Item(None, label=name))
        return self

    def emit(self, mnemonic: str, *operands: Operand) -> FunctionBuilder:
        self.assembler.instruction_set.by_mnemonic(mnemonic)
        self.items.append(_Item(mnemonic, tuple(operands)))
        return self

    def raw(self, data: bytes) -> FunctionBuilder:
        """Append bytes verbatim, for building malformed code."""
        self.items.append(_Item(None, raw=bytes(data)))
        return self


class Assembler:
    """Collects constants and functions and lays them out into a Program; functions are placed in the code section in declaration order."""

    def __init__(self, version: int = FORMAT_VERSION) -> None:
        self.version = version
        self.instruction_set: InstructionSet = INSTRUCTION_SETS[version]
        self.constants: list[Constant] = []
        self.functions: list[FunctionBuilder] = []

    def add_const(self, value: Constant) -> int:
        """Append a constant and return its index."""
        self.constants.append(value)
        return len(self.constants) - 1

    def const(self, value: Constant) -> int:
        """Index of the first constant of the same type equal to value, appending it when there is none."""
        for index, existing in enumerate(self.constants):
            if type(existing) is type(value) and existing == value:
                return index
        return self.add_const(value)

    def function(
        self,
        name: str,
        *,
        params: tuple[Type, ...] | list[Type] = (),
        returns: Type = NONE,
        public: bool = False,
        local_count: int | None = None,
        name_index: int | None = None,
    ) -> FunctionBuilder:
        builder = FunctionBuilder(self, name, tuple(params), returns, public, local_count, name_index)
        self.functions.append(builder)
        return builder

    def program(self) -> Program:
        """Resolve names and labels and lay out the code; raises ValueError for an unknown name, label or operand."""
        names = {builder.name: index for index, builder in enumerate(self.functions)}
        name_indexes = [builder.name_index if builder.name_index is not None else self.const(builder.name) for builder in self.functions]
        resolved = [self._resolve_operands(builder, names) for builder in self.functions]
        bodies = [self._layout(builder, items) for builder, items in zip(self.functions, resolved, strict=True)]
        entries: list[FunctionEntry] = []
        offset = 0
        for builder, name_index, (body, local_count) in zip(self.functions, name_indexes, bodies, strict=True):
            entries.append(FunctionEntry(name_index, builder.public, builder.params, builder.returns, local_count, offset, len(body)))
            offset += len(body)
        return Program(version=self.version, constants=tuple(self.constants), functions=tuple(entries), code=b"".join(body for body, _ in bodies))

    def assemble(self) -> bytes:
        return encode(self.program())

    def _resolve_operands(self, builder: FunctionBuilder, names: dict[str, int]) -> list[_Item]:
        iset = self.instruction_set
        items: list[_Item] = []
        for item in builder.items:
            if item.mnemonic is None:
                items.append(item)
                continue
            info = iset.by_mnemonic(item.mnemonic)
            if len(item.operands) != len(info.operands):
                raise ValueError(f"{builder.name}: {item.mnemonic} takes {len(info.operands)} operands, got {len(item.operands)}")
            out: list[Operand] = []
            record_kind: Kind | None = None
            for kind, operand in zip(info.operands, item.operands, strict=True):
                if kind is OperandKind.CONST:
                    if isinstance(operand, ConstRef):
                        out.append(operand.index)
                    elif type(operand) in (str, int, Type):
                        out.append(self.const(operand))
                    else:
                        raise ValueError(f"{builder.name}: bad constant operand {operand!r}")
                elif kind is OperandKind.FUNC and isinstance(operand, str):
                    if operand not in names:
                        raise ValueError(f"{builder.name}: unknown function {operand}")
                    out.append(names[operand])
                elif kind is OperandKind.BUILTIN and isinstance(operand, str):
                    builtin = iset.builtins_by_name.get(operand)
                    if builtin is None:
                        raise ValueError(f"{builder.name}: unknown builtin {operand}")
                    out.append(builtin.id)
                elif kind is OperandKind.RECORD:
                    if isinstance(operand, str):
                        matches = [k for k in RECORD_KINDS if KIND_NAMES[k] == operand]
                        if not matches:
                            raise ValueError(f"{builder.name}: unknown record type {operand}")
                        operand = matches[0]
                    if isinstance(operand, Kind):
                        record_kind = operand
                        out.append(int(operand))
                    elif type(operand) is int:
                        record_kind = Kind(operand) if operand in {int(k) for k in RECORD_KINDS} else None
                        out.append(operand)
                    else:
                        raise ValueError(f"{builder.name}: bad record operand {operand!r}")
                elif kind is OperandKind.FIELD and isinstance(operand, str):
                    if record_kind is None:
                        raise ValueError(f"{builder.name}: field {operand} of an unknown record type")
                    out.append(record_field_index(record_kind, operand))
                elif (kind is OperandKind.TARGET and isinstance(operand, str)) or type(operand) is int:
                    out.append(operand)
                else:
                    raise ValueError(f"{builder.name}: bad {kind.value} operand {operand!r} for {item.mnemonic}")
            items.append(_Item(item.mnemonic, tuple(out)))
        return items

    def _layout(self, builder: FunctionBuilder, items: list[_Item]) -> tuple[bytes, int]:
        iset = self.instruction_set
        labels: dict[str, int] = {}
        for _ in range(16):
            offsets: dict[str, int] = {}
            body = bytearray()
            for item in items:
                if item.label is not None:
                    offsets[item.label] = len(body)
                    continue
                if item.mnemonic is None:
                    body += item.raw
                    continue
                info = iset.by_mnemonic(item.mnemonic)
                operands: list[int] = []
                for kind, operand in zip(info.operands, item.operands, strict=True):
                    if kind is OperandKind.TARGET and isinstance(operand, str):
                        operands.append(labels.get(operand, 0))
                    elif type(operand) is int:
                        operands.append(operand)
                    else:
                        raise ValueError(f"{builder.name}: unresolved operand {operand!r}")
                body += encode_instruction(info, tuple(operands))
            if offsets == labels:
                break
            labels = offsets
        else:
            raise ValueError(f"{builder.name}: label layout does not converge")
        missing = {operand for item in items for operand in item.operands if isinstance(operand, str)} - set(labels)
        if missing:
            raise ValueError(f"{builder.name}: undefined labels {sorted(missing)}")
        local_count = builder.local_count
        if local_count is None:
            used = [
                operand
                for item in items
                if item.mnemonic is not None
                for kind, operand in zip(iset.by_mnemonic(item.mnemonic).operands, item.operands, strict=True)
                if kind is OperandKind.LOCAL and type(operand) is int
            ]
            local_count = max([len(builder.params), *(u + 1 for u in used)])
        return bytes(body), local_count


_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s"]+')
_INT = re.compile(r"-?[0-9]+")


def _tokens(line: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN.finditer(line):
        token = match.group(0)
        if token.startswith(";"):
            break
        tokens.append(token)
    return tokens


def _split_types(text: str) -> tuple[Type, ...]:
    if not text:
        return ()
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth += {"[": 1, "]": -1}.get(char, 0)
        current += char
    parts.append(current)
    return tuple(parse_type(part) for part in parts)


def parse(text: str, *, version: int = FORMAT_VERSION) -> Assembler:
    """Parse assembly text into an Assembler; raises ValueError with the line number on a syntax error."""
    asm = Assembler(version)
    current: FunctionBuilder | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            tokens = _tokens(line)
            if not tokens:
                continue
            head = tokens[0]
            if head == ".const":
                if current is not None:
                    raise ValueError(".const inside a function")
                if len(tokens) == 3 and tokens[1] == "type":
                    asm.add_const(parse_type(tokens[2]))
                elif len(tokens) == 2:
                    asm.add_const(_literal(tokens[1]))
                else:
                    raise ValueError("bad .const")
            elif head == ".function":
                if current is not None or len(tokens) < 2:
                    raise ValueError("bad .function")
                public = False
                params: tuple[Type, ...] = ()
                returns = NONE
                local_count: int | None = None
                name_index: int | None = None
                for option in tokens[2:]:
                    if option == "public":
                        public = True
                    elif option.startswith("params="):
                        params = _split_types(option[7:])
                    elif option.startswith("returns="):
                        returns = parse_type(option[8:])
                    elif option.startswith("locals="):
                        local_count = int(option[7:])
                    elif option.startswith("name=#"):
                        name_index = int(option[6:])
                    else:
                        raise ValueError(f"unknown .function option {option}")
                current = asm.function(tokens[1], params=params, returns=returns, public=public, local_count=local_count, name_index=name_index)
            elif head == ".end":
                if current is None or len(tokens) != 1:
                    raise ValueError("bad .end")
                current = None
            elif current is None:
                raise ValueError("instruction outside a function")
            elif head.endswith(":") and len(tokens) == 1:
                current.label(head[:-1])
            else:
                info = asm.instruction_set.by_mnemonic(head)
                if len(tokens) - 1 != len(info.operands):
                    raise ValueError(f"{head} takes {len(info.operands)} operands")
                current.emit(head, *(_operand(info.op, kind, token) for kind, token in zip(info.operands, tokens[1:], strict=True)))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"line {number}: {exc}") from None
    if current is not None:
        raise ValueError("missing .end")
    return asm


def assemble(text: str, *, version: int = FORMAT_VERSION) -> bytes:
    """Assemble text into binary bytes."""
    return parse(text, version=version).assemble()


def _literal(token: str) -> str | int:
    if token.startswith('"'):
        value = json.loads(token)
        if type(value) is not str:
            raise ValueError(f"bad string literal {token}")
        return value
    if _INT.fullmatch(token):
        return int(token)
    raise ValueError(f"bad literal {token}")


def _operand(op: Op, kind: OperandKind, token: str) -> Operand:
    if kind is OperandKind.CONST:
        if token.startswith("#"):
            return ConstRef(int(token[1:]))
        if op is Op.CHECK_TYPE:
            return parse_type(token)
        return _literal(token)
    if _INT.fullmatch(token):
        return int(token)
    if kind in (OperandKind.TARGET, OperandKind.FUNC, OperandKind.BUILTIN, OperandKind.RECORD, OperandKind.FIELD):
        return token
    raise ValueError(f"bad {kind.value} operand {token}")


def disassemble(program: VerifiedProgram) -> str:
    """The function table and instruction listing of a verified program in assembler syntax, with offsets and constant values as comments."""
    source = program.program
    lines = [
        f"; RWC1 format version {source.version}, {len(program.data)} bytes, sha256 {program.sha256}",
        f"; {len(source.constants)} constants, {len(program.functions)} functions, {len(source.code)} code bytes",
    ]
    for index, constant in enumerate(source.constants):
        if type(constant) is Type:
            lines.append(f".const type {constant}  ; #{index}")
        else:
            lines.append(f".const {format_value(constant)}  ; #{index}")
    for fn in program.functions:
        header = [".function", fn.name]
        if fn.public:
            header.append("public")
        if fn.params:
            header.append("params=" + ",".join(str(param) for param in fn.params))
        if fn.returns != NONE:
            header.append(f"returns={fn.returns}")
        header.append(f"locals={fn.local_count}")
        first_equal = next(i for i, c in enumerate(source.constants) if type(c) is str and c == fn.name)
        if first_equal != fn.entry.name_index:
            header.append(f"name=#{fn.entry.name_index}")
        lines.append("")
        lines.append(" ".join(header) + f"  ; {fn.signature()}, code {fn.entry.code_offset}..{fn.entry.code_offset + fn.entry.code_length - 1}")
        targets = {item.operands[position] for item in fn.listing for position, kind in enumerate(item.info.operands) if kind is OperandKind.TARGET}
        for item in fn.listing:
            if item.offset in targets:
                lines.append(f"L{item.offset}:")
            text, note = _format_instruction(program, item.info.op, item.info.operands, item.operands)
            comment = f"; @{item.offset}" + (f" {note}" if note else "")
            lines.append(f"    {text:<40} {comment}")
        lines.append(".end")
    return "\n".join(lines) + "\n"


def _format_instruction(program: VerifiedProgram, op: Op, kinds: tuple[OperandKind, ...], operands: tuple[int, ...]) -> tuple[str, str]:
    source = program.program
    iset = program.instruction_set
    parts = [op.name]
    notes: list[str] = []
    record_kind: Kind | None = None
    for kind, value in zip(kinds, operands, strict=True):
        if kind is OperandKind.CONST:
            parts.append(f"#{value}")
            constant = source.constants[value]
            notes.append(str(constant) if type(constant) is Type else format_value(constant))
        elif kind is OperandKind.TARGET:
            parts.append(f"L{value}")
        elif kind is OperandKind.FUNC:
            parts.append(program.functions[value].name)
        elif kind is OperandKind.BUILTIN:
            builtin = iset.builtins[value]
            parts.append(builtin.name)
            notes.append(builtin.signature())
        elif kind is OperandKind.RECORD:
            record_kind = Kind(value)
            parts.append(KIND_NAMES[record_kind])
        elif kind is OperandKind.FIELD and record_kind is not None:
            parts.append(RECORD_FIELDS[record_kind][value].name)
        else:
            parts.append(str(value))
    return " ".join(parts), "; ".join(notes)
