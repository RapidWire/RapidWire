"""Compiler from contract source, a statically typed subset of Python, to RWC1 binaries that pass verify().

The source is parsed with the ast module and nothing outside the subset is accepted: module level holds constants (folded at compile time), function definitions and the optional `from rapidwire_contract import *` line.
Every expression has a static type; int and bool are distinct, Account and Currency are distinct from int and from each other, empty list and dict literals need an expected type, and a dynamic Value is narrowed with int(), str(), bool() or an annotated assignment (which emits CHECK_TYPE).
storage.get(key, default) and read_storage(account, key, default) have the type of their default and check the stored value against it.
Comprehensions, list `+=` and f-strings are lowered to loops, appends and concatenations; conditions jump directly on and/or/not.
Errors are CompileError with the line and 1-based column of the first offending node.
"""

from __future__ import annotations

import ast
import builtins as _python_builtins
import operator
import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rapidwire.contract.assembler import Assembler, FunctionBuilder, Operand
from rapidwire.contract.builtins import BUILTINS_V1_BY_NAME, Builtin
from rapidwire.contract.errors import VerificationError
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.opcodes import CALL_ARGS_LIST, CALL_ATTACH, CALL_GAS, INSTRUCTION_SET_V1, LIST_POP_INDEX, SLICE_START, SLICE_STOP, OpInfo
from rapidwire.contract.types import (
    ACCOUNT,
    BOOL,
    CALL_RESULT,
    CLAIM_INFO,
    CURRENCY,
    CURRENCY_INFO,
    INT,
    KEY_KINDS,
    NONE,
    PAID,
    PUBLIC_PARAM_KINDS,
    PUBLIC_RETURN_KINDS,
    QUOTE,
    RECORD_FIELDS,
    RECORD_KINDS,
    SCALAR_KINDS,
    STR,
    VALUE,
    Kind,
    Type,
    dict_of,
    list_of,
    type_problem,
)
from rapidwire.contract.verifier import NAME_MAX_LENGTH, verify

__all__ = ["ASSERT_MESSAGE", "STUB_MODULE", "CompileError", "compile_source"]

STUB_MODULE = "rapidwire_contract"
ASSERT_MESSAGE = "assertion failed"

SINT_MIN = -(2**63)
SINT_MAX = 2**63 - 1
# Literal lists and dicts are built in chunks so the operand stack stays far below its limit.
LIST_CHUNK = 64
DICT_CHUNK = 32

TYPE_NAMES: dict[str, Type] = {
    "int": INT,
    "str": STR,
    "bool": BOOL,
    "Account": ACCOUNT,
    "Currency": CURRENCY,
    "Value": VALUE,
    "CallResult": CALL_RESULT,
    "Paid": PAID,
    "CurrencyInfo": CURRENCY_INFO,
    "ClaimInfo": CLAIM_INFO,
    "Quote": QUOTE,
}

# Builtins read as values rather than called.
VALUE_BUILTINS = frozenset({"sender", "self", "paid"})
NAMESPACES = frozenset({"storage", "discord"})
# Builtins the compiler implements itself rather than through CALL_BUILTIN.
SPECIAL_FUNCTIONS = frozenset({"call", "try_call", "revert", "len", "int", "str", "bool", "range"})
# Registry builtins that are plain functions in source form.
FUNCTION_BUILTINS: dict[str, Builtin] = {name: builtin for name, builtin in BUILTINS_V1_BY_NAME.items() if "." not in name and name not in VALUE_BUILTINS}
# Names that can never be rebound: types, the language forms and the namespaces.
RESERVED = frozenset({*TYPE_NAMES, *SPECIAL_FUNCTIONS, *NAMESPACES, "list", "dict", "public", "True", "False", "None"})
# Every name a module-level constant or function definition may not take.
GLOBAL_NAMES = frozenset({*RESERVED, *BUILTINS_V1_BY_NAME, *VALUE_BUILTINS})

DICT_KEYS = "dict keys must be one of " + ", ".join(str(t) for t in TYPE_NAMES.values() if t.kind in KEY_KINDS) + ","

_LINE_BREAK = re.compile(r"\r\n|\r|\n")

_OPS: dict[str, OpInfo] = {info.mnemonic: info for info in INSTRUCTION_SET_V1.ops.values()}

_BINARY_OPS: dict[type[ast.operator], tuple[str, str]] = {
    ast.Add: ("+", "ADD"),
    ast.Sub: ("-", "SUB"),
    ast.Mult: ("*", "MUL"),
    ast.FloorDiv: ("//", "FLOORDIV"),
    ast.Mod: ("%", "MOD"),
}
_OTHER_OPERATORS: dict[type[ast.operator], str] = {
    ast.Div: "/ is not supported because there are no floats; use // for integer division",
    ast.Pow: "** is not supported; multiply in a loop",
    ast.MatMult: "@ is not supported",
    ast.LShift: "<< is not supported",
    ast.RShift: ">> is not supported",
    ast.BitOr: "| is not supported",
    ast.BitXor: "^ is not supported",
    ast.BitAnd: "& is not supported",
}
_COMPARE_OPS: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.Eq: ("==", "EQ"),
    ast.NotEq: ("!=", "NE"),
    ast.Lt: ("<", "LT"),
    ast.LtE: ("<=", "LE"),
    ast.Gt: (">", "GT"),
    ast.GtE: (">=", "GE"),
}

_REJECTED_STATEMENTS: dict[type[ast.stmt], str] = {
    ast.FunctionDef: "nested functions are not supported",
    ast.AsyncFunctionDef: "async functions are not supported",
    ast.ClassDef: "class definitions are not supported",
    ast.Import: "import is not allowed",
    ast.ImportFrom: "import is not allowed",
    ast.Global: "global is not supported",
    ast.Nonlocal: "nonlocal is not supported",
    ast.Try: "try is not supported; use try_call() to catch the failure of another contract",
    ast.TryStar: "try is not supported; use try_call() to catch the failure of another contract",
    ast.With: "with is not supported",
    ast.AsyncWith: "async with is not supported",
    ast.AsyncFor: "async for is not supported",
    ast.Raise: "raise is not supported; use revert(message)",
    ast.Match: "match is not supported",
    ast.TypeAlias: "type aliases are not supported",
}
_REJECTED_EXPRESSIONS: dict[type[ast.expr], str] = {
    ast.Lambda: "lambda is not supported",
    ast.NamedExpr: "assignment expressions (:=) are not supported",
    ast.GeneratorExp: "generator expressions are not supported; use a list comprehension",
    ast.SetComp: "sets are not supported",
    ast.Set: "sets are not supported",
    ast.Tuple: "tuples are not supported",
    ast.Await: "await is not supported",
    ast.Yield: "generators are not supported",
    ast.YieldFrom: "generators are not supported",
    ast.Starred: "star expressions are not supported",
    ast.Slice: "a slice can only appear inside []",
}


class CompileError(Exception):
    """A source error at line and 1-based column; str() is "filename:line:column: message"."""

    def __init__(self, line: int, column: int, message: str, *, filename: str = "<contract>") -> None:
        super().__init__(message)
        self.line = line
        self.column = column
        self.message = message
        self.filename = filename

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}:{self.column}: {self.message}"


def compile_source(source: str, *, filename: str = "<contract>", limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Compile contract source to an RWC1 binary that passes verify() under limits; raises CompileError for the first error in the source."""
    return _Compiler(source, filename, limits).compile()


Folded = tuple[Type, int | str | bool]


@dataclass(frozen=True, slots=True)
class _Signature:
    name: str
    params: tuple[tuple[str, Type], ...]
    returns: Type
    public: bool
    node: ast.FunctionDef


@dataclass(slots=True)
class _Local:
    name: str
    slot: int
    type: Type


@dataclass(slots=True)
class _Loop:
    continue_label: str
    break_label: str
    broke: bool = False


@dataclass(slots=True)
class _Function:
    signature: _Signature
    builder: FunctionBuilder
    bound: frozenset[str]
    scopes: list[dict[str, _Local]]
    next_slot: int
    free_slots: list[int] = field(default_factory=list)
    loops: list[_Loop] = field(default_factory=list)
    depth: int = 0
    labels: int = 0
    # Local types found by earlier passes over the function, and whether this is a typing pass whose code is discarded.
    known: dict[str, Type] = field(default_factory=dict)
    typing: bool = False
    skipped: bool = False


class _Untyped(Exception):
    """A typing pass read a local whose type no earlier statement has determined yet."""


class _Compiler:
    def __init__(self, source: str, filename: str, limits: Limits) -> None:
        self.source = source
        self.filename = filename
        self.limits = limits
        self.asm = Assembler()
        self.constants: dict[str, Folded] = {}
        self.signatures: dict[str, _Signature] = {}
        self._fn: _Function | None = None
        self.node: ast.AST | None = None

    # Errors and entry point.

    def fail(self, node: ast.AST | None, message: str) -> CompileError:
        line = getattr(node, "lineno", 1) if node is not None else 1
        offset = getattr(node, "col_offset", 0) if node is not None else 0
        # ast column offsets count UTF-8 bytes; errors report characters.
        lines = _LINE_BREAK.split(self.source)
        text = lines[line - 1] if 1 <= line <= len(lines) else ""
        column = len(text.encode("utf-8", errors="surrogatepass")[:offset].decode("utf-8", errors="ignore")) + 1
        return CompileError(line, column, message, filename=self.filename)

    def compile(self) -> bytes:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(self.source, filename=self.filename)
        except SyntaxError as exc:
            raise CompileError(exc.lineno or 1, exc.offset or 1, f"syntax error: {exc.msg}", filename=self.filename) from None
        except ValueError as exc:
            raise CompileError(1, 1, f"cannot parse the source: {exc}", filename=self.filename) from None
        except (MemoryError, RecursionError):
            raise CompileError(1, 1, "the program is too deeply nested for the parser", filename=self.filename) from None
        try:
            self.module(tree)
        except (MemoryError, RecursionError):
            raise self.fail(self.node, "the program is nested too deeply") from None
        data = self.asm.assemble()
        try:
            verify(data, max_size=max(len(data), 1), limits=self.limits)
        except VerificationError as exc:
            raise CompileError(1, 1, f"internal compiler error, the binary does not verify: {exc}", filename=self.filename) from None
        return data

    @property
    def fn(self) -> _Function:
        if self._fn is None:
            raise AssertionError("no function is being compiled")
        return self._fn

    # Module level.

    def module(self, tree: ast.Module) -> None:
        functions: list[ast.FunctionDef] = []
        imported = False
        for stmt in tree.body:
            self.node = stmt
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            if isinstance(stmt, ast.ImportFrom) and stmt.module == STUB_MODULE and stmt.level == 0 and [alias.name for alias in stmt.names] == ["*"]:
                if imported:
                    raise self.fail(stmt, f"{STUB_MODULE} is imported twice")
                imported = True
                continue
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                raise self.fail(stmt, f"import is not allowed; the only accepted import is 'from {STUB_MODULE} import *'")
            if isinstance(stmt, ast.FunctionDef):
                functions.append(stmt)
                self.signature(stmt)
                continue
            if isinstance(stmt, ast.Assign | ast.AnnAssign):
                self.module_constant(stmt)
                continue
            if type(stmt) in _REJECTED_STATEMENTS:
                raise self.fail(stmt, _REJECTED_STATEMENTS[type(stmt)])
            raise self.fail(stmt, "only constants and function definitions are allowed at module level")
        if not functions:
            raise self.fail(None, "the contract defines no functions")
        if not any(sig.public for sig in self.signatures.values()):
            raise self.fail(functions[0], "the contract has no @public function")
        for node in functions:
            self.function(self.signatures[node.name])

    def global_name(self, node: ast.AST, name: str, what: str) -> None:
        self.check_identifier(node, name)
        if name in GLOBAL_NAMES:
            raise self.fail(node, f"{what} {name} shadows a builtin")
        if name in self.constants:
            raise self.fail(node, f"{name} is already defined as a constant")
        if name in self.signatures:
            raise self.fail(node, f"{name} is already defined as a function")

    def module_constant(self, stmt: ast.Assign | ast.AnnAssign) -> None:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1:
                raise self.fail(stmt, "assign one constant per statement")
            target = stmt.targets[0]
            value = stmt.value
        else:
            target = stmt.target
            if stmt.value is None:
                raise self.fail(stmt, "a module-level constant needs a value")
            value = stmt.value
        if not isinstance(target, ast.Name):
            raise self.fail(target, "a module-level constant must be assigned to a single name")
        self.global_name(target, target.id, "constant")
        folded = self.fold(value)
        if folded is None:
            raise self.fail(value, "a module-level value must be a constant int, str or bool expression")
        if isinstance(stmt, ast.AnnAssign):
            declared = self.parse_type(stmt.annotation)
            if declared != folded[0]:
                raise self.fail(stmt.annotation, f"constant {target.id} is declared {declared} but its value is {folded[0]}")
        self.constants[target.id] = folded

    def signature(self, node: ast.FunctionDef) -> None:
        self.global_name(node, node.name, "function")
        if not node.name.isascii() or len(node.name) > NAME_MAX_LENGTH:
            raise self.fail(node, f"function names must be ASCII identifiers of at most {NAME_MAX_LENGTH} characters")
        if node.type_params:
            raise self.fail(node, "type parameters are not supported")
        public = False
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "public" and not public:
                public = True
                continue
            raise self.fail(decorator, "the only allowed decorator is @public")
        args = node.args
        if args.posonlyargs:
            raise self.fail(args.posonlyargs[0], "positional-only parameters are not supported")
        if args.vararg is not None:
            raise self.fail(args.vararg, "*args is not supported")
        if args.kwonlyargs:
            raise self.fail(args.kwonlyargs[0], "keyword-only parameters are not supported")
        if args.kwarg is not None:
            raise self.fail(args.kwarg, "**kwargs is not supported")
        if args.defaults:
            raise self.fail(args.defaults[0], "default parameter values are not supported")
        if len(args.args) > self.limits.max_params:
            raise self.fail(args.args[self.limits.max_params], f"a function takes at most {self.limits.max_params} parameters")
        params: list[tuple[str, Type]] = []
        for arg in args.args:
            self.node = arg
            if any(name == arg.arg for name, _ in params):
                raise self.fail(arg, f"duplicate parameter {arg.arg}")
            self.check_binding(arg, arg.arg)
            if arg.annotation is None:
                raise self.fail(arg, f"parameter {arg.arg} needs a type annotation")
            param_type = self.parse_type(arg.annotation)
            if public and param_type.kind not in PUBLIC_PARAM_KINDS:
                raise self.fail(arg.annotation, f"a public function parameter must be int, str, bool, Account or Currency, not {param_type}")
            params.append((arg.arg, param_type))
        if node.returns is None:
            raise self.fail(node, f"function {node.name} needs a return type annotation")
        returns = self.parse_type(node.returns, allow_none=True)
        if public and returns.kind not in PUBLIC_RETURN_KINDS:
            raise self.fail(node.returns, f"a public function must return int, str or bool, not {returns}")
        self.signatures[node.name] = _Signature(node.name, tuple(params), returns, public, node)

    def parse_type(self, node: ast.expr, *, allow_none: bool = False) -> Type:
        result = self._parse_type(node, allow_none)
        problem = type_problem(result, allow_none=allow_none)
        if problem is not None:
            raise self.fail(node, problem)
        return result

    def _parse_type(self, node: ast.expr, allow_none: bool) -> Type:
        if isinstance(node, ast.Constant) and node.value is None:
            if allow_none:
                return NONE
            raise self.fail(node, "None is only allowed as a return type")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            raise self.fail(node, "string annotations are not supported")
        if isinstance(node, ast.Name):
            if node.id in TYPE_NAMES:
                return TYPE_NAMES[node.id]
            if node.id in ("list", "dict"):
                raise self.fail(node, f"{node.id} needs element types, for example {'list[int]' if node.id == 'list' else 'dict[str, int]'}")
            raise self.fail(node, f"unknown type {node.id}")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in ("list", "dict"):
            if node.value.id == "list":
                if isinstance(node.slice, ast.Tuple):
                    raise self.fail(node, "list takes one element type")
                return list_of(self._parse_type(node.slice, False))
            if not isinstance(node.slice, ast.Tuple) or len(node.slice.elts) != 2:
                raise self.fail(node, "dict takes a key type and a value type")
            key = self._parse_type(node.slice.elts[0], False)
            if key.kind not in KEY_KINDS:
                raise self.fail(node.slice.elts[0], f"{DICT_KEYS} not {key}")
            return dict_of(key, self._parse_type(node.slice.elts[1], False))
        raise self.fail(node, "unsupported type annotation")

    # Names and bindings.

    def check_identifier(self, node: ast.AST, name: str) -> None:
        if name.startswith("__"):
            raise self.fail(node, f"names beginning with __ are not allowed: {name}")

    def check_binding(self, node: ast.AST, name: str) -> None:
        self.check_identifier(node, name)
        if name in RESERVED:
            raise self.fail(node, f"{name} cannot be assigned")
        if name in self.constants:
            raise self.fail(node, f"cannot assign to constant {name}")
        if name in self.signatures:
            raise self.fail(node, f"cannot assign to function {name}")

    def lookup(self, node: ast.AST, name: str) -> _Local | None:
        """The local called name, or None when name is not a local.

        A local of the function that is read before the statement assigning it is declared here with the type a typing pass found, and the VM fails at run time if it still holds no value, as Python raises UnboundLocalError.
        """
        fn = self.fn
        for scope in reversed(fn.scopes):
            local = scope.get(name)
            if local is not None:
                return local
        if name not in fn.bound:
            return None
        known = fn.known.get(name)
        if known is not None:
            return self.declare(node, name, known)
        if fn.typing:
            raise _Untyped(name)
        raise self.fail(node, f"the type of {name} cannot be inferred where it is used; assign or annotate it first")

    def is_local_name(self, name: str) -> bool:
        return name in self.fn.bound or any(name in scope for scope in self.fn.scopes)

    def declare(self, node: ast.AST, name: str, t: Type) -> _Local:
        self.check_binding(node, name)
        if t.kind is Kind.NONE:
            raise self.fail(node, f"{name} cannot hold None")
        local = _Local(name, self.new_slot(), t)
        self.fn.scopes[0][name] = local
        return local

    def bind_function_local(self, node: ast.AST, name: str, t: Type) -> _Local:
        """The function local name, declared with type t when it is new, else checked to have type t."""
        self.check_binding(node, name)
        local = self.fn.scopes[0].get(name)
        if local is None:
            return self.declare(node, name, t)
        if local.type != t:
            raise self.fail(node, f"{name} has type {local.type}, not {t}")
        return local

    def new_slot(self) -> int:
        fn = self.fn
        slot = fn.next_slot
        fn.next_slot += 1
        if fn.next_slot > self.limits.max_locals:
            raise self.fail(self.node, f"function {fn.signature.name} needs more than {self.limits.max_locals} local variables")
        return slot

    def temp(self) -> int:
        if self.fn.free_slots:
            return self.fn.free_slots.pop()
        return self.new_slot()

    def free(self, *slots: int) -> None:
        self.fn.free_slots.extend(slots)

    def label(self) -> str:
        self.fn.labels += 1
        return f"L{self.fn.labels}"

    # Emission with stack depth tracking.

    def emit(self, mnemonic: str, *operands: Operand, pops: int | None = None, pushes: int | None = None) -> None:
        info = _OPS[mnemonic]
        popped = info.pops if pops is None else pops
        pushed = info.pushes if pushes is None else pushes
        if popped is None or pushed is None:
            raise AssertionError(f"{mnemonic} needs an explicit stack effect")
        fn = self.fn
        fn.depth -= popped
        if fn.depth < 0:
            raise AssertionError(f"stack underflow emitting {mnemonic}")
        fn.depth += pushed
        if fn.depth > self.limits.max_stack_depth:
            raise self.fail(self.node, "the expression is too deeply nested")
        fn.builder.emit(mnemonic, *operands)

    def place(self, name: str) -> None:
        self.fn.builder.label(name)

    def push_constant(self, t: Type, value: int | str | bool) -> None:
        if t.kind is Kind.BOOL:
            self.emit("PUSH_TRUE" if value else "PUSH_FALSE")
        elif t.kind is Kind.INT and isinstance(value, int) and SINT_MIN <= value <= SINT_MAX:
            self.emit("PUSH_INT", value)
        else:
            self.emit("PUSH_CONST", value)

    def call_builtin_op(self, builtin: Builtin, argc: int | None = None) -> None:
        count = builtin.arity if argc is None else argc
        self.emit("CALL_BUILTIN", builtin.name, count, pops=count, pushes=builtin.pushes)

    # Functions.

    def function(self, sig: _Signature) -> None:
        """Compile one function: typing passes find the type of every local, including locals read before the statement that assigns them (as in loops), then the final pass emits the code."""
        known: dict[str, Type] = {}
        while True:
            fn = self.function_pass(sig, known, typing=True)
            found = {name: local.type for name, local in fn.scopes[0].items()}
            if not fn.skipped or found == known:
                break
            known = found
        builder = self.asm.function(sig.name, params=[t for _, t in sig.params], returns=sig.returns, public=sig.public, local_count=0)
        self.function_pass(sig, known, typing=False, builder=builder)

    def function_pass(self, sig: _Signature, known: dict[str, Type], *, typing: bool, builder: FunctionBuilder | None = None) -> _Function:
        node = sig.node
        self.node = node
        if builder is None:
            builder = FunctionBuilder(self.asm, sig.name, tuple(t for _, t in sig.params), sig.returns, sig.public, 0, None)
        bound = frozenset(_bound_names(node.body)) | {name for name, _ in sig.params}
        fn = _Function(sig, builder, bound, [{}], 0, known=dict(known), typing=typing)
        self._fn = fn
        for name, t in sig.params:
            self.declare(node, name, t)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        terminated = self.block(body)
        if not terminated and not typing:
            if sig.returns.kind is not Kind.NONE:
                raise self.fail(node, f"function {sig.name} can reach its end without returning a {sig.returns}")
            self.emit("RET", pops=0, pushes=0)
        builder.local_count = fn.next_slot
        self._fn = None
        return fn

    def block(self, stmts: Sequence[ast.stmt]) -> bool:
        """Compile statements and return whether control can never fall through past them; statements after a terminating one are checked and discarded."""
        terminated = False
        for stmt in stmts:
            if not terminated:
                terminated = self.checked_stmt(stmt)
                continue
            builder = self.fn.builder
            mark = len(builder.items)
            flags = [loop.broke for loop in self.fn.loops]
            self.checked_stmt(stmt)
            del builder.items[mark:]
            self.fn.depth = 0
            for loop, broke in zip(self.fn.loops, flags, strict=True):
                loop.broke = broke
        return terminated

    def checked_stmt(self, stmt: ast.stmt) -> bool:
        """Compile a statement; in a typing pass a statement that reads a local of unknown type is dropped and retried by the next pass."""
        fn = self.fn
        if not fn.typing:
            return self.stmt(stmt)
        mark, loops, scopes = len(fn.builder.items), len(fn.loops), len(fn.scopes)
        try:
            return self.stmt(stmt)
        except _Untyped:
            del fn.builder.items[mark:]
            del fn.loops[loops:]
            del fn.scopes[scopes:]
            fn.depth = 0
            fn.skipped = True
            return False

    def stmt(self, stmt: ast.stmt) -> bool:
        self.node = stmt
        method: Callable[[ast.stmt], bool] | None = getattr(self, f"s_{type(stmt).__name__}", None)
        if method is None:
            raise self.fail(stmt, _REJECTED_STATEMENTS.get(type(stmt), f"{type(stmt).__name__} statements are not supported"))
        result = method(stmt)
        if self.fn.depth != 0:
            raise AssertionError(f"stack depth {self.fn.depth} after a statement")
        return result

    def s_Pass(self, stmt: ast.Pass) -> bool:
        return False

    def s_Expr(self, stmt: ast.Expr) -> bool:
        value = stmt.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return False
        if not isinstance(value, ast.Call):
            if type(value) in _REJECTED_EXPRESSIONS:
                raise self.fail(value, _REJECTED_EXPRESSIONS[type(value)])
            raise self.fail(value, "this expression has no effect; only calls can be used as statements")
        result = self.call(value, None, statement=True)
        if result.kind is not Kind.NONE:
            self.emit("POP")
        return isinstance(value.func, ast.Name) and value.func.id == "revert" and not self.is_local_name("revert")

    def s_Return(self, stmt: ast.Return) -> bool:
        returns = self.fn.signature.returns
        value = stmt.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None and returns.kind is Kind.NONE):
            if returns.kind is not Kind.NONE:
                raise self.fail(stmt, f"return needs a {returns} value")
            self.emit("RET", pops=0, pushes=0)
            return True
        if returns.kind is Kind.NONE:
            raise self.fail(value, f"function {self.fn.signature.name} returns None; remove the return value")
        actual = self.expr(value, returns)
        self.coerce(value, actual, returns, "return value")
        self.emit("RET", pops=1, pushes=0)
        return True

    def s_Assign(self, stmt: ast.Assign) -> bool:
        if len(stmt.targets) > 1:
            names: list[ast.Name] = []
            for item in stmt.targets:
                if not isinstance(item, ast.Name):
                    raise self.fail(item, "chained assignment is only supported to names")
                self.check_binding(item, item.id)
                names.append(item)
            existing = self.fn.scopes[0].get(names[0].id)
            t = self.expr(stmt.value, existing.type if existing is not None else None)
            for index, name in enumerate(names):
                if index + 1 < len(names):
                    self.emit("DUP")
                self.store_name(name, t, stmt.value)
            return False
        target = stmt.targets[0]
        if isinstance(target, ast.Name):
            self.check_binding(target, target.id)
            existing = self.fn.scopes[0].get(target.id)
            t = self.expr(stmt.value, existing.type if existing is not None else None)
            self.store_name(target, t, stmt.value)
            return False
        if isinstance(target, ast.Subscript):
            self.assign_subscript(target, stmt.value)
            return False
        raise self.fail(target, self.bad_target(target))

    def bad_target(self, target: ast.expr) -> str:
        if isinstance(target, ast.Attribute):
            return "attributes cannot be assigned"
        if isinstance(target, ast.Tuple | ast.List):
            return "unpacking assignment is not supported"
        return "this cannot be assigned"

    def store_name(self, target: ast.Name, t: Type, value: ast.expr) -> None:
        existing = self.fn.scopes[0].get(target.id)
        if existing is None:
            local = self.declare(target, target.id, t)
        else:
            self.coerce(value, t, existing.type, f"assignment to {target.id}")
            local = existing
        self.emit("STORE", local.slot)

    def assign_subscript(self, target: ast.Subscript, value: ast.expr) -> None:
        if isinstance(target.slice, ast.Slice):
            raise self.fail(target, "slice assignment is not supported")
        # Python evaluates the value, then the container, then the key; when none of them contains a call the order cannot be observed and the value is emitted last.
        reorder = any(_has_call(part) for part in (value, target.value, target.slice))
        if self.is_storage(target.value):
            if reorder:
                stored = self.temp()
                self.storable(value, self.expr(value))
                self.emit("STORE", stored)
                self.expect(target.slice, STR, "storage key")
                self.emit("LOAD", stored)
                self.free(stored)
            else:
                self.expect(target.slice, STR, "storage key")
                self.storable(value, self.expr(value))
            self.call_builtin_op(BUILTINS_V1_BY_NAME["storage.set"])
            return
        if not reorder:
            key_type, element, what = self.item_types(target, self.expr(target.value))
            self.expect(target.slice, key_type, what)
            self.coerce(value, self.expr(value, element), element, "item assignment")
            self.emit("INDEX_SET")
            return
        key_type, element, what = self.item_types(target, self.probe(target.value))
        held = self.temp()
        self.coerce(value, self.expr(value, element), element, "item assignment")
        self.emit("STORE", held)
        self.expr(target.value)
        self.expect(target.slice, key_type, what)
        self.emit("LOAD", held)
        self.free(held)
        self.emit("INDEX_SET")

    def item_types(self, target: ast.Subscript, container: Type) -> tuple[Type, Type, str]:
        """The key type, element type and key description of an assignment to an item of a container of type container."""
        if container.kind is Kind.LIST:
            return INT, container.args[0], "list index"
        if container.kind is Kind.DICT:
            return container.args[0], container.args[1], "dict key"
        if container.kind is Kind.STR:
            raise self.fail(target, "str does not support item assignment")
        raise self.fail(target.value, f"{container} does not support item assignment")

    def probe(self, node: ast.expr, expected: Type | None = None) -> Type:
        """The type of node, found by compiling it into code that is then discarded."""
        fn = self.fn
        mark, depth, scopes, free = len(fn.builder.items), fn.depth, len(fn.scopes), list(fn.free_slots)
        try:
            return self.expr(node, expected)
        finally:
            del fn.builder.items[mark:]
            del fn.scopes[scopes:]
            fn.depth = depth
            fn.free_slots[:] = free

    def s_AnnAssign(self, stmt: ast.AnnAssign) -> bool:
        target = stmt.target
        if not isinstance(target, ast.Name):
            raise self.fail(target, "annotations are only allowed on names")
        declared = self.parse_type(stmt.annotation)
        existing = self.fn.scopes[0].get(target.id)
        if existing is not None and existing.type != declared:
            raise self.fail(stmt, f"{target.id} is already declared as {existing.type}")
        if stmt.value is None:
            if existing is None:
                self.declare(target, target.id, declared)
            return False
        actual = self.expr(stmt.value, declared)
        self.coerce(stmt.value, actual, declared, f"assignment to {target.id}", narrow=True)
        local = existing if existing is not None else self.declare(target, target.id, declared)
        self.emit("STORE", local.slot)
        return False

    def s_AugAssign(self, stmt: ast.AugAssign) -> bool:
        symbol, _ = self.binary_symbol(stmt, stmt.op)
        target = stmt.target
        if isinstance(target, ast.Name):
            self.check_binding(target, target.id)
            local = self.lookup(target, target.id)
            if local is None or local is not self.fn.scopes[0].get(target.id):
                raise self.fail(target, f"{target.id} is used before it is assigned")
            if local.type.kind is Kind.LIST and isinstance(stmt.op, ast.Add):
                self.extend_list(local, stmt.value)
                return False
            self.emit("LOAD", local.slot)
            right = self.expr(stmt.value, local.type)
            result = self.binary_result(stmt, stmt.op, symbol, local.type, right)
            self.coerce(stmt, result, local.type, f"assignment to {target.id}")
            self.emit("STORE", local.slot)
            return False
        if isinstance(target, ast.Subscript):
            if self.is_storage(target.value):
                raise self.fail(target, "storage values cannot be updated in place; write storage[key] = storage.get(key, default) + ...")
            if isinstance(target.slice, ast.Slice):
                raise self.fail(target, "slice assignment is not supported")
            container_slot = self.temp()
            key_slot = self.temp()
            container = self.expr(target.value)
            if container.kind is Kind.LIST:
                key_type, element = INT, container.args[0]
            elif container.kind is Kind.DICT:
                key_type, element = container.args
            else:
                raise self.fail(target.value, f"{container} does not support item assignment")
            if element.kind is Kind.LIST and isinstance(stmt.op, ast.Add):
                raise self.fail(stmt, "+= on a list element is not supported; use .append() or x[k] = x[k] + y")
            self.emit("STORE", container_slot)
            self.expect(target.slice, key_type, "index")
            self.emit("STORE", key_slot)
            for slot in (container_slot, key_slot, container_slot, key_slot):
                self.emit("LOAD", slot)
            self.emit("INDEX_GET")
            right = self.expr(stmt.value, element)
            result = self.binary_result(stmt, stmt.op, symbol, element, right)
            self.coerce(stmt, result, element, "item assignment")
            self.emit("INDEX_SET")
            self.free(container_slot, key_slot)
            return False
        raise self.fail(target, self.bad_target(target))

    def extend_list(self, local: _Local, value: ast.expr) -> None:
        """Lower list += value to appending each element of a copy of value, so aliases of the list see the change as in Python; the list is read before value is evaluated, so an unassigned list fails first, as in Python."""
        target, source, index, item = self.temp(), self.temp(), self.temp(), self.temp()
        self.emit("LOAD", local.slot)
        self.emit("STORE", target)
        actual = self.expr(value, local.type)
        self.coerce(value, actual, local.type, f"+= on {local.name}")
        head, done = self.label(), self.label()
        self.emit("SLICE", 0, pops=1, pushes=1)
        self.emit("STORE", source)
        self.emit("PUSH_INT", 0)
        self.emit("STORE", index)
        self.place(head)
        self.emit("FOR_EACH", source, index, item, done)
        self.emit("LOAD", target)
        self.emit("LOAD", item)
        self.emit("APPEND")
        self.emit("JUMP", head)
        self.place(done)
        self.free(target, source, index, item)

    def s_If(self, stmt: ast.If) -> bool:
        end = self.label()
        orelse = self.label() if stmt.orelse else end
        self.branch(stmt.test, orelse, jump_if=False)
        body_terminated = self.block(stmt.body)
        if not stmt.orelse:
            self.place(end)
            return False
        if not body_terminated:
            self.emit("JUMP", end)
        self.place(orelse)
        else_terminated = self.block(stmt.orelse)
        self.place(end)
        return body_terminated and else_terminated

    def s_While(self, stmt: ast.While) -> bool:
        if stmt.orelse:
            raise self.fail(stmt, "while ... else is not supported")
        head, back, done = self.label(), self.label(), self.label()
        self.place(head)
        folded = self.fold(stmt.test)
        forever = folded is not None and folded == (BOOL, True)
        if not forever:
            self.branch(stmt.test, done, jump_if=False)
        # Every cycle must contain a charged instruction, so an endless loop closes with a conditional jump on True instead of a free JUMP.
        loop = _Loop(back if forever else head, done)
        self.fn.loops.append(loop)
        self.block(stmt.body)
        self.fn.loops.pop()
        if not forever:
            self.emit("JUMP", head)
            self.place(done)
            return False
        self.place(back)
        self.emit("PUSH_TRUE")
        self.emit("JUMP_IF_TRUE", head)
        if not loop.broke:
            # The fall-through of the closing jump is never taken; it needs an instruction for the verifier's control flow.
            self.emit("PUSH_CONST", "unreachable")
            self.emit("REVERT")
        self.place(done)
        return not loop.broke

    def s_For(self, stmt: ast.For) -> bool:
        if stmt.orelse:
            raise self.fail(stmt, "for ... else is not supported")
        if stmt.type_comment:
            raise self.fail(stmt, "type comments are not supported")

        def bind(target: ast.Name, t: Type) -> _Local:
            return self.bind_function_local(target, target.id, t)

        def body(head: str, done: str) -> None:
            self.fn.loops.append(_Loop(head, done))
            self.block(stmt.body)
            self.fn.loops.pop()

        self.loop(stmt.target, stmt.iter, bind, body)
        return False

    def loop(self, target: ast.expr, iterable: ast.expr, bind: Callable[[ast.Name, Type], _Local], body: Callable[[str, str], None]) -> None:
        """Emit a range or element loop over iterable binding target; body(head, done) emits the loop body, where head continues and done breaks."""
        if not isinstance(target, ast.Name):
            raise self.fail(target, "a loop target must be a single name")
        head, done = self.label(), self.label()
        if isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Name) and iterable.func.id == "range" and not self.is_local_name("range"):
            counter, stop, step = self.temp(), self.temp(), self.temp()
            args = iterable.args
            if iterable.keywords or not 1 <= len(args) <= 3 or any(isinstance(arg, ast.Starred) for arg in args):
                raise self.fail(iterable, "range() takes one to three int arguments")
            if len(args) == 1:
                self.emit("PUSH_INT", 0)
                self.emit("STORE", counter)
                self.expect(args[0], INT, "range() argument")
                self.emit("STORE", stop)
            else:
                self.expect(args[0], INT, "range() argument")
                self.emit("STORE", counter)
                self.expect(args[1], INT, "range() argument")
                self.emit("STORE", stop)
            if len(args) == 3:
                if self.fold(args[2]) == (INT, 0):
                    raise self.fail(args[2], "range() step must not be zero")
                self.expect(args[2], INT, "range() argument")
            else:
                self.emit("PUSH_INT", 1)
            self.emit("STORE", step)
            variable = bind(target, INT)
            self.place(head)
            self.emit("FOR_RANGE", counter, stop, step, variable.slot, done)
            temps = [counter, stop, step]
        else:
            t = self.expr(iterable)
            if t.kind is Kind.STR:
                element = STR
            elif t.kind is Kind.LIST:
                element = t.args[0]
            elif t.kind is Kind.DICT:
                self.call_builtin_op(BUILTINS_V1_BY_NAME["dict.keys"])
                element = t.args[0]
            else:
                raise self.fail(iterable, f"cannot iterate over {t}")
            sequence, index = self.temp(), self.temp()
            self.emit("STORE", sequence)
            self.emit("PUSH_INT", 0)
            self.emit("STORE", index)
            variable = bind(target, element)
            self.place(head)
            self.emit("FOR_EACH", sequence, index, variable.slot, done)
            temps = [sequence, index]
        body(head, done)
        self.emit("JUMP", head)
        self.place(done)
        self.free(*temps)

    def s_Break(self, stmt: ast.Break) -> bool:
        if not self.fn.loops:
            raise self.fail(stmt, "break outside a loop")
        loop = self.fn.loops[-1]
        loop.broke = True
        self.emit("JUMP", loop.break_label)
        return True

    def s_Continue(self, stmt: ast.Continue) -> bool:
        if not self.fn.loops:
            raise self.fail(stmt, "continue outside a loop")
        self.emit("JUMP", self.fn.loops[-1].continue_label)
        return True

    def s_Assert(self, stmt: ast.Assert) -> bool:
        folded = self.fold(stmt.test)
        if folded == (BOOL, True):
            return False
        if folded is not None and folded[0] != BOOL:
            raise self.fail(stmt.test, f"assert condition: expected bool, got {folded[0]}")
        if folded == (BOOL, False):
            if stmt.msg is None:
                self.emit("PUSH_CONST", ASSERT_MESSAGE)
            else:
                self.expect(stmt.msg, STR, "assert message")
            self.emit("REVERT")
            return True
        folded_message = self.fold(stmt.msg) if stmt.msg is not None else (STR, ASSERT_MESSAGE)
        if folded_message is not None and folded_message[0] == STR:
            self.expect(stmt.test, BOOL, "assert condition")
            self.emit("ASSERT", str(folded_message[1]))
        elif stmt.msg is not None:
            ok = self.label()
            self.branch(stmt.test, ok, jump_if=True)
            self.expect(stmt.msg, STR, "assert message")
            self.emit("REVERT")
            self.place(ok)
        return False

    def s_Delete(self, stmt: ast.Delete) -> bool:
        for target in stmt.targets:
            self.node = target
            if not isinstance(target, ast.Subscript) or isinstance(target.slice, ast.Slice):
                raise self.fail(target, "del only removes storage[key], d[key] or a list item")
            if self.is_storage(target.value):
                self.expect(target.slice, STR, "storage key")
                self.call_builtin_op(BUILTINS_V1_BY_NAME["storage.delete"])
                continue
            container = self.expr(target.value)
            if container.kind is Kind.LIST:
                self.expect(target.slice, INT, "list index")
            elif container.kind is Kind.DICT:
                self.expect(target.slice, container.args[0], "dict key")
            else:
                raise self.fail(target.value, f"cannot delete items of {container}")
            self.emit("DELETE_ITEM")
        return False

    # Types.

    def coerce(self, node: ast.AST, actual: Type, wanted: Type, what: str, *, narrow: bool = False) -> None:
        """Check that a value of type actual can be used as wanted; with narrow, a Value is checked at run time with CHECK_TYPE."""
        if actual == wanted or (wanted.kind is Kind.VALUE and actual.kind is not Kind.NONE):
            return
        if actual.kind is Kind.VALUE:
            if narrow and wanted.kind in (Kind.LIST, Kind.DICT) and not self.fresh_read(node):
                raise self.fail(node, f"{what}: a list or dict can only be narrowed from a Value read directly from storage.get() or read_storage(), which no other reference shares")
            if narrow:
                self.emit("CHECK_TYPE", wanted)
                return
            raise self.fail(node, f"{what}: a Value must be narrowed with int(), str(), bool() or an annotated assignment before it can be used as {wanted}")
        raise self.fail(node, f"{what}: expected {wanted}, got {actual}")

    def fresh_read(self, node: ast.AST) -> bool:
        """Whether node is a storage read without a default (or a conditional choosing between such reads), whose value is newly decoded and shared with nothing else."""
        if isinstance(node, ast.IfExp):
            return self.fresh_read(node.body) and self.fresh_read(node.orelse)
        if not isinstance(node, ast.Call) or len(node.args) != (1 if isinstance(node.func, ast.Attribute) else 2):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr == "get" and self.is_storage(func.value)
        return isinstance(func, ast.Name) and func.id == "read_storage" and not self.is_local_name("read_storage")

    def expect(self, node: ast.expr, wanted: Type, what: str) -> Type:
        actual = self.expr(node, wanted)
        self.coerce(node, actual, wanted, what)
        return wanted

    def storable(self, node: ast.AST, t: Type) -> None:
        if _contains_record(t):
            raise self.fail(node, f"a {t} cannot be stored")

    def is_storage(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == "storage"

    # Constant folding.

    def fold(self, node: ast.expr) -> Folded | None:
        """The type and value of node when it is a constant expression of int, str and bool, or None; overflow and division by zero are CompileErrors."""
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                return BOOL, value
            if isinstance(value, int):
                return INT, self.check_int(node, value)
            if isinstance(value, str):
                if len(value) > self.limits.str_max_length:
                    raise self.fail(node, f"string literal longer than {self.limits.str_max_length} characters")
                if not value.isascii():
                    try:
                        value.encode("utf-8")
                    except UnicodeEncodeError:
                        raise self.fail(node, "string literal contains a lone surrogate, which is not valid Unicode text") from None
                return STR, value
            return None
        if isinstance(node, ast.Name):
            if self._fn is not None and self.is_local_name(node.id):
                return None
            return self.constants.get(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = self.fold(node.operand)
            if operand is None:
                return None
            t, v = operand
            if isinstance(node.op, ast.USub | ast.UAdd) and t == INT and isinstance(v, int):
                return INT, self.check_int(node, -v if isinstance(node.op, ast.USub) else v)
            if isinstance(node.op, ast.Not) and t == BOOL:
                return BOOL, not v
            return None
        if isinstance(node, ast.BinOp):
            left = self.fold(node.left)
            right = self.fold(node.right) if left is not None else None
            if left is None or right is None:
                return None
            return self.fold_binary(node, left, right)
        if isinstance(node, ast.BoolOp):
            deciding = isinstance(node.op, ast.Or)
            for index, term in enumerate(node.values):
                item = self.fold(term)
                if item is None or item[0] != BOOL:
                    return None
                rest = node.values[index + 1 :]
                if item[1] == deciding or not rest:
                    return (BOOL, bool(item[1])) if all(self.is_bool(skipped) for skipped in rest) else None
            return None
        if isinstance(node, ast.Compare):
            folded_items = [self.fold(node.left), *(self.fold(item) for item in node.comparators)]
            items = [item for item in folded_items if item is not None]
            if len(items) != len(folded_items):
                return None
            result = True
            for op, left_item, right_item in zip(node.ops, items, items[1:], strict=False):
                outcome = _fold_compare(op, left_item, right_item)
                if outcome is None:
                    return None
                result = result and outcome
            return BOOL, result
        return None

    def is_bool(self, node: ast.expr) -> bool:
        """Whether an operand that short-circuiting skips is a bool: inside a function it is type-checked by compiling it, at module level it must be a constant bool expression."""
        if self._fn is not None:
            return self.probe(node) == BOOL
        return _constant_type(node, self.constants) == BOOL

    def check_int(self, node: ast.AST, value: int) -> int:
        if not self.limits.int_ok(value):
            raise self.fail(node, f"integer constant outside the {self.limits.int_bits}-bit limit")
        return value

    def unfoldable(self, node: ast.AST, message: str) -> None:
        """Reject a constant operation that would fail: a module-level constant cannot be computed, while inside a function the operation is left to fail at run time as Python would."""
        if self._fn is None:
            raise self.fail(node, message)

    def fold_binary(self, node: ast.BinOp, left: Folded, right: Folded) -> Folded | None:
        (lt, lv), (rt, rv) = left, right
        if lt == STR and rt == STR and isinstance(node.op, ast.Add):
            text = str(lv) + str(rv)
            if len(text) > self.limits.str_max_length:
                self.unfoldable(node, f"string constant longer than {self.limits.str_max_length} characters")
                return None
            return STR, text
        if lt != INT or rt != INT or not isinstance(lv, int) or not isinstance(rv, int) or type(node.op) not in _BINARY_OPS:
            return None
        if isinstance(node.op, ast.FloorDiv | ast.Mod) and rv == 0:
            self.unfoldable(node, "division by zero in a constant expression")
            return None
        if isinstance(node.op, ast.Add):
            value = lv + rv
        elif isinstance(node.op, ast.Sub):
            value = lv - rv
        elif isinstance(node.op, ast.Mult):
            value = lv * rv
        elif isinstance(node.op, ast.FloorDiv):
            value = lv // rv
        else:
            value = lv % rv
        if not self.limits.int_ok(value):
            self.unfoldable(node, f"integer constant outside the {self.limits.int_bits}-bit limit")
            return None
        return INT, value

    # Expressions.

    def expr(self, node: ast.expr, expected: Type | None = None) -> Type:
        """Emit node, which must produce a value, and return its type; expected is the type the context wants, used to type empty literals."""
        result = self.expr_or_none(node, expected)
        if result.kind is Kind.NONE:
            raise self.fail(node, "this call returns None and has no value")
        return result

    def expr_or_none(self, node: ast.expr, expected: Type | None) -> Type:
        saved = self.node
        self.node = node
        try:
            folded = self.fold(node)
            if folded is not None:
                self.push_constant(*folded)
                return folded[0]
            method: Callable[[ast.expr, Type | None], Type] | None = getattr(self, f"e_{type(node).__name__}", None)
            if method is None:
                raise self.fail(node, _REJECTED_EXPRESSIONS.get(type(node), f"{type(node).__name__} expressions are not supported"))
            return method(node, expected)
        finally:
            self.node = saved

    def e_Constant(self, node: ast.Constant, expected: Type | None) -> Type:
        value = node.value
        if value is None:
            raise self.fail(node, "None is not a value")
        if isinstance(value, float):
            raise self.fail(node, "floats are not supported")
        if isinstance(value, complex):
            raise self.fail(node, "complex numbers are not supported")
        if isinstance(value, bytes):
            raise self.fail(node, "bytes are not supported")
        raise self.fail(node, f"{value!r} is not a supported literal")

    def e_Name(self, node: ast.Name, expected: Type | None) -> Type:
        name = node.id
        self.check_identifier(node, name)
        local = self.lookup(node, name)
        if local is not None:
            self.emit("LOAD", local.slot)
            return local.type
        if name in VALUE_BUILTINS:
            builtin = BUILTINS_V1_BY_NAME[name]
            self.call_builtin_op(builtin)
            return builtin.returns
        raise self.fail(node, self.not_a_value(name))

    def not_a_value(self, name: str) -> str:
        if name in self.signatures:
            return f"function {name} is not a value; call it"
        if name in NAMESPACES:
            return f"{name} can only be used through its methods"
        if name in FUNCTION_BUILTINS or name in SPECIAL_FUNCTIONS or name in ("list", "dict"):
            return f"{name} is a function; call it"
        if name in TYPE_NAMES:
            return f"type {name} is not a value"
        if name == "public":
            return "public is only a decorator"
        if hasattr(_python_builtins, name):
            return f"{name} is not an allowed builtin"
        return f"name {name} is not defined"

    def e_BinOp(self, node: ast.BinOp, expected: Type | None) -> Type:
        symbol, _ = self.binary_symbol(node, node.op)
        left_expected = expected if expected is not None and expected.kind is Kind.LIST and isinstance(node.op, ast.Add) else None
        left = self.expr(node.left, left_expected)
        right = self.expr(node.right, left if left.kind is Kind.LIST else None)
        return self.binary_result(node, node.op, symbol, left, right)

    def binary_symbol(self, node: ast.AST, op: ast.operator) -> tuple[str, str]:
        if type(op) in _OTHER_OPERATORS:
            raise self.fail(node, _OTHER_OPERATORS[type(op)])
        return _BINARY_OPS[type(op)]

    def binary_result(self, node: ast.AST, op: ast.operator, symbol: str, left: Type, right: Type) -> Type:
        """Emit the operator on two operands already on the stack and return the result type."""
        if isinstance(op, ast.Add) and left == right and left.kind in (Kind.STR, Kind.LIST):
            self.emit("CONCAT")
            return left
        if left == INT and right == INT:
            self.emit(_BINARY_OPS[type(op)][1])
            return INT
        raise self.fail(node, f"unsupported operand types for {symbol}: {left} and {right}")

    def e_UnaryOp(self, node: ast.UnaryOp, expected: Type | None) -> Type:
        if isinstance(node.op, ast.Invert):
            raise self.fail(node, "~ is not supported")
        operand = self.expr(node.operand)
        if isinstance(node.op, ast.Not):
            if operand != BOOL:
                raise self.fail(node, f"not needs a bool, not {operand}")
            self.emit("NOT")
            return BOOL
        if operand != INT:
            raise self.fail(node, f"unary {'-' if isinstance(node.op, ast.USub) else '+'} needs an int, not {operand}")
        if isinstance(node.op, ast.USub):
            self.emit("NEG")
        return INT

    def e_BoolOp(self, node: ast.BoolOp, expected: Type | None) -> Type:
        end = self.label()
        jump = "JUMP_IF_FALSE" if isinstance(node.op, ast.And) else "JUMP_IF_TRUE"
        for index, value in enumerate(node.values):
            t = self.expr(value)
            if t != BOOL:
                raise self.fail(value, f"{'and' if isinstance(node.op, ast.And) else 'or'} needs bool operands, not {t}")
            if index + 1 < len(node.values):
                self.emit("DUP")
                self.emit(jump, end)
                self.emit("POP")
        self.place(end)
        return BOOL

    def e_Compare(self, node: ast.Compare, expected: Type | None) -> Type:
        left = self.expr(node.left)
        if len(node.ops) == 1:
            right = self.expr(node.comparators[0], left)
            self.compare(node, node.ops[0], left, right)
            return BOOL
        end = self.label()
        held = self.temp()
        for index, (op, comparator) in enumerate(zip(node.ops, node.comparators, strict=True)):
            last = index + 1 == len(node.ops)
            right = self.expr(comparator, left)
            if not last:
                self.emit("STORE", held)
                self.emit("LOAD", held)
            self.compare(comparator, op, left, right)
            if not last:
                self.emit("DUP")
                self.emit("JUMP_IF_FALSE", end)
                self.emit("POP")
                self.emit("LOAD", held)
            left = right
        self.place(end)
        self.free(held)
        return BOOL

    def compare(self, node: ast.AST, op: ast.cmpop, left: Type, right: Type) -> None:
        if isinstance(op, ast.Is | ast.IsNot):
            raise self.fail(node, "is is not supported; use ==")
        if isinstance(op, ast.In | ast.NotIn):
            word = "in" if isinstance(op, ast.In) else "not in"
            substring = right == STR and left == STR
            element = right.kind is Kind.LIST and left == right.args[0] and left.kind in SCALAR_KINDS
            key = right.kind is Kind.DICT and left == right.args[0]
            if not (substring or element or key):
                raise self.fail(node, f"unsupported operand types for {word}: {left} and {right}")
            self.emit("CONTAINS")
            if isinstance(op, ast.NotIn):
                self.emit("NOT")
            return
        symbol, mnemonic = _COMPARE_OPS[type(op)]
        if isinstance(op, ast.Eq | ast.NotEq):
            if left != right or left.kind not in SCALAR_KINDS:
                if left == right:
                    raise self.fail(node, f"{symbol} compares int, str, bool, Account or Currency values, not {left}")
                raise self.fail(node, f"cannot compare {left} and {right} with {symbol}")
        elif left != right or left.kind not in (Kind.INT, Kind.STR):
            raise self.fail(node, f"{symbol} compares two ints or two strs, not {left} and {right}")
        self.emit(mnemonic)

    def branch(self, node: ast.expr, target: str, *, jump_if: bool) -> None:
        """Emit a jump to target taken when the bool condition node equals jump_if, short-circuiting and, or and not."""
        saved = self.node
        self.node = node
        try:
            folded = self.fold(node)
            if folded is not None:
                if folded[0] != BOOL:
                    raise self.fail(node, f"a condition must be a bool, not {folded[0]}")
                if folded[1] == jump_if:
                    self.emit("JUMP", target)
                return
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                self.branch(node.operand, target, jump_if=not jump_if)
                return
            if isinstance(node, ast.BoolOp):
                conjunction = isinstance(node.op, ast.And)
                if conjunction != jump_if:
                    for value in node.values:
                        self.branch(value, target, jump_if=jump_if)
                    return
                skip = self.label()
                for value in node.values[:-1]:
                    self.branch(value, skip, jump_if=not jump_if)
                self.branch(node.values[-1], target, jump_if=jump_if)
                self.place(skip)
                return
            t = self.expr(node)
            if t != BOOL:
                raise self.fail(node, f"a condition must be a bool, not {t}")
            self.emit("JUMP_IF_TRUE" if jump_if else "JUMP_IF_FALSE", target)
        finally:
            self.node = saved

    def e_IfExp(self, node: ast.IfExp, expected: Type | None) -> Type:
        orelse, end = self.label(), self.label()
        self.branch(node.test, orelse, jump_if=False)
        depth = self.fn.depth
        first = self.expr(node.body, expected)
        self.emit("JUMP", end)
        self.fn.depth = depth
        self.place(orelse)
        second = self.expr(node.orelse, expected if expected is not None else first)
        self.place(end)
        if second == first:
            return first
        if VALUE in (first, second):
            return VALUE
        raise self.fail(node, f"both branches of a conditional expression must have the same type, not {first} and {second}")

    def e_Subscript(self, node: ast.Subscript, expected: Type | None) -> Type:
        if self.is_storage(node.value) and not self.is_local_name("storage"):
            raise self.fail(node, "read storage with storage.get(key, default)")
        if isinstance(node.slice, ast.Slice):
            container = self.expr(node.value, expected)
            if container.kind not in (Kind.STR, Kind.LIST):
                raise self.fail(node.value, f"{container} cannot be sliced")
            if node.slice.step is not None:
                raise self.fail(node.slice.step, "slice steps are not supported")
            flags = 0
            if node.slice.lower is not None:
                self.expect(node.slice.lower, INT, "slice start")
                flags |= SLICE_START
            if node.slice.upper is not None:
                self.expect(node.slice.upper, INT, "slice stop")
                flags |= SLICE_STOP
            self.emit("SLICE", flags, pops=1 + bin(flags).count("1"), pushes=1)
            return container
        container = self.expr(node.value)
        if container.kind is Kind.STR:
            self.expect(node.slice, INT, "str index")
            result = STR
        elif container.kind is Kind.LIST:
            self.expect(node.slice, INT, "list index")
            result = container.args[0]
        elif container.kind is Kind.DICT:
            self.expect(node.slice, container.args[0], "dict key")
            result = container.args[1]
        else:
            raise self.fail(node.value, f"{container} is not subscriptable")
        self.emit("INDEX_GET")
        return result

    def e_Attribute(self, node: ast.Attribute, expected: Type | None) -> Type:
        self.check_identifier(node, node.attr)
        if isinstance(node.value, ast.Name) and node.value.id in NAMESPACES and not self.is_local_name(node.value.id):
            raise self.fail(node, f"{node.value.id}.{node.attr} must be called")
        record = self.expr(node.value)
        if record.kind in RECORD_KINDS:
            for field_def in RECORD_FIELDS[record.kind]:
                if field_def.name == node.attr:
                    self.emit("GET_FIELD", record.kind, node.attr)
                    return field_def.type
            raise self.fail(node, f"{record} has no field {node.attr}")
        if self.method_exists(record, node.attr):
            raise self.fail(node, f"method {record}.{node.attr} must be called")
        raise self.fail(node, f"{record} has no attribute {node.attr}")

    def e_List(self, node: ast.List, expected: Type | None) -> Type:
        element = expected.args[0] if expected is not None and expected.kind is Kind.LIST else None
        if not node.elts:
            if element is None:
                raise self.fail(node, "cannot infer the element type of an empty list; annotate it, for example x: list[int] = []")
            self.emit("BUILD_LIST", 0, pops=0, pushes=1)
            return list_of(element)
        for index, item in enumerate(node.elts):
            if index == LIST_CHUNK:
                self.emit("BUILD_LIST", LIST_CHUNK, pops=LIST_CHUNK, pushes=1)
            if index >= LIST_CHUNK:
                self.emit("DUP")
            actual = self.expr(item, element)
            if element is None:
                element = actual
            else:
                self.coerce(item, actual, element, "list element")
            if index >= LIST_CHUNK:
                self.emit("APPEND")
        if len(node.elts) <= LIST_CHUNK:
            self.emit("BUILD_LIST", len(node.elts), pops=len(node.elts), pushes=1)
        return list_of(_known(element))

    def e_Dict(self, node: ast.Dict, expected: Type | None) -> Type:
        key_type, value_type = _dict_types(expected)
        if not node.keys:
            if key_type is None or value_type is None:
                raise self.fail(node, "cannot infer the types of an empty dict; annotate it, for example x: dict[str, int] = {}")
            self.emit("BUILD_DICT", 0, pops=0, pushes=1)
            return dict_of(key_type, value_type)
        count = len(node.keys)
        for index, (key, item) in enumerate(zip(node.keys, node.values, strict=True)):
            if key is None:
                raise self.fail(item, "** unpacking is not supported")
            if index == DICT_CHUNK:
                self.emit("BUILD_DICT", DICT_CHUNK, pops=2 * DICT_CHUNK, pushes=1)
            if index >= DICT_CHUNK:
                self.emit("DUP")
            actual_key = self.expr(key, key_type)
            if key_type is None:
                if actual_key.kind not in KEY_KINDS:
                    raise self.fail(key, f"{DICT_KEYS} not {actual_key}")
                key_type = actual_key
            else:
                self.coerce(key, actual_key, key_type, "dict key")
            actual_value = self.expr(item, value_type)
            if value_type is None:
                value_type = actual_value
            else:
                self.coerce(item, actual_value, value_type, "dict value")
            if index >= DICT_CHUNK:
                self.emit("INDEX_SET")
        if count <= DICT_CHUNK:
            self.emit("BUILD_DICT", count, pops=2 * count, pushes=1)
        return dict_of(_known(key_type), _known(value_type))

    def e_JoinedStr(self, node: ast.JoinedStr, expected: Type | None) -> Type:
        if not node.values:
            self.emit("PUSH_CONST", "")
            return STR
        for index, part in enumerate(node.values):
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1:
                    raise self.fail(part, "f-string conversions such as !r are not supported")
                if part.format_spec is not None:
                    raise self.fail(part, "f-string format specifications are not supported")
                t = self.expr(part.value)
                if t.kind not in SCALAR_KINDS:
                    raise self.fail(part.value, f"an f-string can only format int, str, bool, Account or Currency values, not {t}")
                if t != STR:
                    self.emit("TO_STR")
            else:
                self.expect(part, STR, "f-string part")
            if index:
                self.emit("CONCAT")
        return STR

    def e_ListComp(self, node: ast.ListComp, expected: Type | None) -> Type:
        element = expected.args[0] if expected is not None and expected.kind is Kind.LIST else None
        result_slot = self.temp()
        self.emit("BUILD_LIST", 0, pops=0, pushes=1)
        self.emit("STORE", result_slot)
        produced: list[Type] = []

        def produce() -> None:
            self.emit("LOAD", result_slot)
            actual = self.expr(node.elt, element if element is not None else (produced[0] if produced else None))
            wanted = element if element is not None else (produced[0] if produced else actual)
            self.coerce(node.elt, actual, wanted, "list element")
            produced.append(wanted)
            self.emit("APPEND")

        self.comprehension(node.generators, produce)
        self.emit("LOAD", result_slot)
        self.free(result_slot)
        return list_of(produced[0])

    def e_DictComp(self, node: ast.DictComp, expected: Type | None) -> Type:
        key_type, value_type = _dict_types(expected)
        result_slot = self.temp()
        self.emit("BUILD_DICT", 0, pops=0, pushes=1)
        self.emit("STORE", result_slot)
        produced: list[tuple[Type, Type]] = []

        def produce() -> None:
            self.emit("LOAD", result_slot)
            actual_key = self.expr(node.key, key_type)
            wanted_key = key_type if key_type is not None else actual_key
            if wanted_key.kind not in KEY_KINDS:
                raise self.fail(node.key, f"{DICT_KEYS} not {wanted_key}")
            self.coerce(node.key, actual_key, wanted_key, "dict key")
            actual_value = self.expr(node.value, value_type)
            wanted_value = value_type if value_type is not None else actual_value
            self.coerce(node.value, actual_value, wanted_value, "dict value")
            produced.append((wanted_key, wanted_value))
            self.emit("INDEX_SET")

        self.comprehension(node.generators, produce)
        self.emit("LOAD", result_slot)
        self.free(result_slot)
        return dict_of(*produced[0])

    def comprehension(self, generators: list[ast.comprehension], produce: Callable[[], None]) -> None:
        """Lower the generators of a comprehension to nested loops in a new scope, calling produce() in the innermost body."""
        scope: dict[str, _Local] = {}
        pushed = False

        def level(index: int) -> None:
            nonlocal pushed
            generator = generators[index]
            if generator.is_async:
                raise self.fail(generator.target, "async comprehensions are not supported")

            def bind(target: ast.Name, t: Type) -> _Local:
                nonlocal pushed
                self.check_binding(target, target.id)
                if not pushed:
                    self.fn.scopes.append(scope)
                    pushed = True
                if target.id in scope:
                    raise self.fail(target, f"{target.id} is bound twice in one comprehension")
                local = _Local(target.id, self.temp(), t)
                scope[target.id] = local
                return local

            def body(head: str, done: str) -> None:
                for condition in generator.ifs:
                    self.branch(condition, head, jump_if=False)
                if index + 1 < len(generators):
                    level(index + 1)
                else:
                    produce()

            self.loop(generator.target, generator.iter, bind, body)

        level(0)
        if pushed:
            self.fn.scopes.pop()
        self.free(*(local.slot for local in scope.values()))

    # Calls.

    def e_Call(self, node: ast.Call, expected: Type | None) -> Type:
        return self.call(node, expected, statement=False)

    def call(self, node: ast.Call, expected: Type | None, *, statement: bool) -> Type:
        self.node = node
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise self.fail(arg, "*args is not supported")
        for keyword in node.keywords:
            if keyword.arg is None:
                raise self.fail(keyword.value, "**kwargs is not supported")
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
            self.check_identifier(func, name)
            if self.is_local_name(name):
                local = self.lookup(func, name)
                raise self.fail(func, f"{name} is a {local.type if local is not None else 'variable'}, not a function")
            if name in ("call", "try_call"):
                return self.contract_call(node, name == "try_call")
            if node.keywords:
                raise self.fail(node.keywords[0].value, "keyword arguments are not supported")
            if name in self.signatures:
                return self.call_function(node, self.signatures[name])
            if name == "revert":
                if not statement:
                    raise self.fail(node, "revert() can only be used as a statement")
                self.arity(node, "revert", 1)
                self.expect(node.args[0], STR, "revert() message")
                self.emit("REVERT")
                return NONE
            if name in ("len", "int", "str", "bool"):
                self.arity(node, name, 1)
                return self.conversion(node, name)
            if name == "range":
                raise self.fail(node, "range() can only be used in a for loop or comprehension")
            if name in FUNCTION_BUILTINS:
                return self.call_builtin(node, FUNCTION_BUILTINS[name], node.args, None, expected)
            if name in VALUE_BUILTINS:
                raise self.fail(node, f"{name} is not callable; write {name} without parentheses")
            if name in self.constants:
                raise self.fail(func, f"constant {name} is not callable")
            if name in ("list", "dict"):
                raise self.fail(node, f"{name}() is not supported; write a literal")
            raise self.fail(func, self.not_a_value(name) if name not in TYPE_NAMES else f"{name}() is not supported")
        if isinstance(func, ast.Attribute):
            self.check_identifier(func, func.attr)
            if node.keywords:
                raise self.fail(node.keywords[0].value, "keyword arguments are not supported")
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in NAMESPACES and not self.is_local_name(owner.id):
                builtin = BUILTINS_V1_BY_NAME.get(f"{owner.id}.{func.attr}")
                if builtin is None:
                    raise self.fail(func, f"{owner.id} has no function {func.attr}")
                return self.call_builtin(node, builtin, node.args, None, expected)
            receiver = self.expr(owner)
            return self.method(node, func.attr, receiver, expected)
        raise self.fail(func, "only functions, builtins and methods can be called")

    def arity(self, node: ast.Call, name: str, count: int) -> None:
        if len(node.args) != count:
            raise self.fail(node, f"{name}() takes {count} argument{'s' if count != 1 else ''}, got {len(node.args)}")

    def call_function(self, node: ast.Call, sig: _Signature) -> Type:
        self.arity(node, sig.name, len(sig.params))
        for arg, (pname, ptype) in zip(node.args, sig.params, strict=True):
            actual = self.expr(arg, ptype)
            self.coerce(arg, actual, ptype, f"argument {pname} of {sig.name}()")
        self.emit("CALL", sig.name, pops=len(sig.params), pushes=0 if sig.returns.kind is Kind.NONE else 1)
        return sig.returns

    def conversion(self, node: ast.Call, name: str) -> Type:
        arg = node.args[0]
        t = self.expr(arg)
        if name == "len":
            if t.kind not in (Kind.STR, Kind.LIST, Kind.DICT):
                raise self.fail(arg, f"len() needs a str, list or dict, not {t}")
            self.emit("LEN")
            return INT
        if name == "int":
            if t.kind not in (Kind.INT, Kind.BOOL, Kind.ACCOUNT, Kind.CURRENCY, Kind.STR, Kind.VALUE):
                raise self.fail(arg, f"int() cannot convert {t}")
            if t != INT:
                self.emit("TO_INT")
            return INT
        if name == "str":
            if t.kind not in SCALAR_KINDS and t.kind is not Kind.VALUE:
                raise self.fail(arg, f"str() cannot convert {t}")
            if t != STR:
                self.emit("TO_STR")
            return STR
        if t == BOOL:
            return BOOL
        if t.kind is Kind.VALUE:
            self.emit("CHECK_TYPE", BOOL)
        elif t == INT:
            self.emit("PUSH_INT", 0)
            self.emit("NE")
        elif t.kind in (Kind.STR, Kind.LIST, Kind.DICT):
            self.emit("LEN")
            self.emit("PUSH_INT", 0)
            self.emit("NE")
        else:
            raise self.fail(arg, f"bool() cannot convert {t}")
        return BOOL

    def method_exists(self, receiver: Type, name: str) -> bool:
        if receiver.kind is Kind.LIST:
            return name in ("append", "pop")
        if receiver.kind is Kind.STR:
            return f"str.{name}" in BUILTINS_V1_BY_NAME
        if receiver.kind is Kind.DICT:
            return f"dict.{name}" in BUILTINS_V1_BY_NAME
        return False

    def method(self, node: ast.Call, name: str, receiver: Type, expected: Type | None) -> Type:
        if receiver.kind is Kind.LIST:
            element = receiver.args[0]
            if name == "append":
                self.arity(node, "append", 1)
                actual = self.expr(node.args[0], element)
                self.coerce(node.args[0], actual, element, "append() argument")
                self.emit("APPEND")
                return NONE
            if name == "pop":
                if len(node.args) > 1:
                    raise self.fail(node, "pop() takes at most 1 argument")
                flags = 0
                if node.args:
                    self.expect(node.args[0], INT, "pop() index")
                    flags = LIST_POP_INDEX
                self.emit("LIST_POP", flags, pops=1 + (1 if flags else 0), pushes=1)
                return element
            raise self.fail(node.func, f"list has no method {name}; lists support append() and pop()")
        if receiver.kind in (Kind.STR, Kind.DICT):
            prefix = "str" if receiver.kind is Kind.STR else "dict"
            builtin = BUILTINS_V1_BY_NAME.get(f"{prefix}.{name}")
            if builtin is None:
                methods = ", ".join(sorted(b.split(".", 1)[1] + "()" for b in BUILTINS_V1_BY_NAME if b.startswith(prefix + ".")))
                raise self.fail(node.func, f"{prefix} has no method {name}; {prefix} supports {methods}")
            return self.call_builtin(node, builtin, node.args, receiver, expected)
        raise self.fail(node.func, f"{receiver} has no method {name}")

    def call_builtin(self, node: ast.Call, builtin: Builtin, args: Sequence[ast.expr], receiver: Type | None, expected: Type | None) -> Type:
        """Emit a registry builtin; with receiver, the first parameter is the receiver already on the stack."""
        params = builtin.params
        display = builtin.name
        given = len(args) + (1 if receiver is not None else 0)
        if not builtin.accepts(given):
            offset = 1 if receiver is not None else 0
            low, high = builtin.required - offset, len(params) - offset
            shown = str(high) if low == high else f"{low} or {high}"
            raise self.fail(node, f"{display}() takes {shown} argument{'s' if high != 1 else ''}, got {len(args)}")
        bindings: dict[str, Type] = {}
        dynamic: Type | None = None
        remaining = params
        if receiver is not None:
            if not _unify(params[0].type, receiver, bindings):
                raise self.fail(node.func, f"{display}() cannot be called on {receiver}")
            remaining = params[1:]
        for arg, param in zip(args, remaining, strict=False):
            pattern = param.type
            if pattern.kind is Kind.VALUE:
                hint = expected if param.name == "default" else None
                actual = self.expr(arg, hint)
                self.storable(arg, actual)
                if param.name == "default":
                    dynamic = actual
                continue
            actual = self.expr(arg, _substitute(pattern, bindings))
            if actual.kind is Kind.VALUE:
                raise self.fail(arg, f"argument {param.name} of {display}(): a Value must be narrowed with int(), str(), bool() or an annotated assignment first")
            if not _unify(pattern, actual, bindings):
                raise self.fail(arg, f"argument {param.name} of {display}() must be {_substitute(pattern, bindings) or pattern}, not {actual}")
        self.call_builtin_op(builtin, given)
        if builtin.returns.kind is Kind.VALUE and dynamic is not None:
            if dynamic.kind is not Kind.VALUE:
                self.emit("CHECK_TYPE", dynamic)
            return dynamic
        result = _substitute(builtin.returns, bindings)
        if result is None:
            raise AssertionError(f"unbound type variable in the result of {display}")
        return result

    def contract_call(self, node: ast.Call, catch: bool) -> Type:
        name = "try_call" if catch else "call"
        if not 2 <= len(node.args) <= 3:
            raise self.fail(node, f"{name}() takes a target account, a function name and an optional argument list")
        self.expect(node.args[0], ACCOUNT, f"{name}() target")
        self.expect(node.args[1], STR, f"{name}() function name")
        flags = 0
        argc = 0
        pops = 2
        if len(node.args) == 3:
            arg_list = node.args[2]
            if isinstance(arg_list, ast.List):
                if len(arg_list.elts) > self.limits.max_params:
                    raise self.fail(arg_list, f"a contract call takes at most {self.limits.max_params} arguments")
                for item in arg_list.elts:
                    t = self.expr(item)
                    if t.kind not in SCALAR_KINDS and t.kind is not Kind.VALUE:
                        raise self.fail(item, f"contract call arguments must be int, str, bool, Account or Currency, not {t}")
                argc = len(arg_list.elts)
                pops += argc
            else:
                t = self.expr(arg_list)
                if t.kind is not Kind.LIST or (t.args[0].kind not in SCALAR_KINDS and t.args[0].kind is not Kind.VALUE):
                    raise self.fail(arg_list, f"contract call arguments must be a list of int, str, bool, Account or Currency values, not {t}")
                flags |= CALL_ARGS_LIST
                pops += 1
        keywords = {keyword.arg: keyword for keyword in node.keywords}
        for keyword in node.keywords:
            if keyword.arg not in ("attach", "gas"):
                raise self.fail(keyword.value, f"{name}() accepts only the keywords attach and gas")
        if len(keywords) != len(node.keywords):
            raise self.fail(node, "a keyword is given twice")
        order = [keyword.arg for keyword in node.keywords]
        # The VM takes the gas limit above the attachment; a gas keyword written first is evaluated first and held in a local.
        gas_first = "gas" in keywords and "attach" in keywords and order.index("gas") < order.index("attach")
        held_gas: int | None = None
        if gas_first:
            held_gas = self.temp()
            self.expect(keywords["gas"].value, INT, f"{name}() gas")
            self.emit("STORE", held_gas)
        if "attach" in keywords:
            attach = keywords["attach"].value
            if not isinstance(attach, ast.Tuple) or len(attach.elts) != 2:
                raise self.fail(attach, "attach must be a pair (currency, amount)")
            self.expect(attach.elts[0], CURRENCY, "attached currency")
            self.expect(attach.elts[1], INT, "attached amount")
            flags |= CALL_ATTACH
            pops += 2
        if "gas" in keywords:
            if held_gas is None:
                self.expect(keywords["gas"].value, INT, f"{name}() gas")
            else:
                self.emit("LOAD", held_gas)
                self.free(held_gas)
            flags |= CALL_GAS
            pops += 1
        self.emit("TRY_CALL" if catch else "CALL_CONTRACT", flags, argc, pops=pops, pushes=1)
        return CALL_RESULT if catch else VALUE


def _dict_types(expected: Type | None) -> tuple[Type | None, Type | None]:
    if expected is not None and expected.kind is Kind.DICT:
        return expected.args[0], expected.args[1]
    return None, None


def _known(t: Type | None) -> Type:
    if t is None:
        raise AssertionError("a literal with elements has an element type")
    return t


_ORDERINGS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _fold_compare(op: ast.cmpop, left: Folded, right: Folded) -> bool | None:
    (lt, lv), (rt, rv) = left, right
    if lt != rt:
        return None
    if isinstance(op, ast.Eq):
        return lv == rv
    if isinstance(op, ast.NotEq):
        return lv != rv
    ordering = _ORDERINGS.get(type(op))
    if ordering is None or lt not in (INT, STR):
        return None
    return bool(ordering(lv, rv))


def _unify(pattern: Type, actual: Type, bindings: dict[str, Type]) -> bool:
    if pattern.kind is Kind.VAR:
        bound = bindings.get(pattern.name)
        if bound is None:
            bindings[pattern.name] = actual
            return True
        return bound == actual
    if pattern.kind is not actual.kind or len(pattern.args) != len(actual.args):
        return False
    return all(_unify(p, a, bindings) for p, a in zip(pattern.args, actual.args, strict=True))


def _substitute(pattern: Type, bindings: dict[str, Type]) -> Type | None:
    """pattern with its type variables replaced, or None when one is unbound."""
    if pattern.kind is Kind.VAR:
        return bindings.get(pattern.name)
    if not pattern.args:
        return pattern
    args = [_substitute(arg, bindings) for arg in pattern.args]
    if any(arg is None for arg in args):
        return None
    return Type(pattern.kind, tuple(arg for arg in args if arg is not None))


def _constant_type(node: ast.expr, constants: dict[str, Folded]) -> Type | None:
    """The type of a constant expression without computing its value, or None when node is not one."""
    if isinstance(node, ast.Constant):
        return {bool: BOOL, int: INT, str: STR}.get(type(node.value))
    if isinstance(node, ast.Name):
        found = constants.get(node.id)
        return None if found is None else found[0]
    if isinstance(node, ast.UnaryOp):
        operand = _constant_type(node.operand, constants)
        if isinstance(node.op, ast.Not):
            return BOOL if operand == BOOL else None
        return INT if operand == INT and isinstance(node.op, ast.USub | ast.UAdd) else None
    if isinstance(node, ast.BinOp):
        left, right = _constant_type(node.left, constants), _constant_type(node.right, constants)
        if left == right == STR and isinstance(node.op, ast.Add):
            return STR
        return INT if left == right == INT and type(node.op) in _BINARY_OPS else None
    if isinstance(node, ast.BoolOp):
        return BOOL if all(_constant_type(value, constants) == BOOL for value in node.values) else None
    if isinstance(node, ast.Compare):
        operands = [_constant_type(item, constants) for item in (node.left, *node.comparators)]
        return BOOL if None not in operands and len(set(operands)) == 1 else None
    return None


def _has_call(node: ast.AST) -> bool:
    return any(isinstance(item, ast.Call) for item in ast.walk(node))


def _contains_record(t: Type) -> bool:
    return t.kind in RECORD_KINDS or any(_contains_record(arg) for arg in t.args)


def _bound_names(stmts: Sequence[ast.stmt]) -> set[str]:
    """Names a function body assigns, annotates or loops over, outside comprehensions; they are the function's locals as in Python."""
    names: set[str] = set()

    def target(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            names.add(node.id)

    def visit(body: Sequence[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for item in stmt.targets:
                    target(item)
            elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
                target(stmt.target)
            elif isinstance(stmt, ast.For):
                target(stmt.target)
                visit(stmt.body)
                visit(stmt.orelse)
            elif isinstance(stmt, ast.If | ast.While):
                visit(stmt.body)
                visit(stmt.orelse)

    visit(stmts)
    return names
