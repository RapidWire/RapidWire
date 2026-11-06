"""The contract interpreter: executes a VerifiedProgram against a Host inside an ExecutionContext.

Operand types are checked dynamically; a mismatch, like every other fault of the program, raises a ContractFailure subclass.
HostError from a Host method becomes a ContractRuntimeError; every other exception from the Host (ledger rerun requests, database errors, HostProtocolError) propagates unchanged.

Gas: each instruction's base cost is charged before it runs, followed by its size surcharge before the sized work; free instructions follow the free run rule of costs, with a counter local to each function invocation.
The GasMeter is shared by the whole top-level execution; a call with gas=N opens a sub-limit of min(N, remaining), and running out raises OutOfGas whose level names the outermost limit that ran out.

Memory: the accounted size of everything reachable from the frames of every execution in the top-level call (size model in MemoryMeter) never exceeds the limit.
Every operation that can raise the accounted size first reports an upper bound of the increase; when the last measurement plus the reported increases would pass the limit, the meter measures live memory exactly (charging costs.memory_walk_gas) and fails with LimitExceeded if the operation would pass the limit, so the failure happens at the operation that crosses it.

Wall clock: the shared Deadline is compared with a monotonic clock every DEADLINE_CHECK_INTERVAL instructions and before and after every host call; after each of those periodic checks the execution yields to the event loop with the clock stopped, so other tasks keep running and their time is not charged to it.
"""

from __future__ import annotations

import asyncio
import hashlib
import operator
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn, Protocol

from rapidwire.contract.builtins import Builtin, BuiltinKind
from rapidwire.contract.costs import (
    AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT,
    COPY_ELEMENT_WEIGHT,
    FREE_RUN_ALLOWANCE,
    NEEDLE_CHARS_PER_ELEMENT,
    elements_gas,
    memory_walk_gas,
    storage_read_gas,
    storage_write_gas,
    str_gas,
)
from rapidwire.contract.errors import (
    CallFailed,
    CallFailure,
    ContractFailure,
    ContractRevert,
    ContractRuntimeError,
    HostError,
    HostProtocolError,
    LimitExceeded,
    LimitResource,
    OutOfGas,
    bound_message,
    try_call_catches,
)
from rapidwire.contract.limits import DEADLINE_CHECK_INTERVAL, DEFAULT_LIMITS, SNOWFLAKE_MAX, SNOWFLAKE_MIN, Limits
from rapidwire.contract.opcodes import CALL_ARGS_LIST, CALL_ATTACH, CALL_GAS, LIST_POP_INDEX, SLICE_START, SLICE_STOP, Op
from rapidwire.contract.types import KIND_NAMES, RECORD_KINDS, SCALAR_KINDS, Kind, Type
from rapidwire.contract.values import (
    PUBLIC_RESULT_TYPES,
    PYTHON_TYPES,
    SCALAR_TYPES,
    Account,
    Attachment,
    Currency,
    Record,
    Scalar,
    StorageValueError,
    Value,
    call_result_failure,
    call_result_ok,
    contains_is_ambiguous,
    decode_value_counted,
    encode_value_counted,
    format_value,
    is_key,
    keys_are_valid,
    list_contains,
    matches,
    paid_record,
    record_problem,
    scalar_problem,
)
from rapidwire.contract.verifier import NAME_MAX_LENGTH, VerifiedFunction, VerifiedProgram

REF_BYTES = 8
SCALAR_BYTES = 16
STR_BASE_BYTES = 16
OBJECT_BYTES = 32
FRAME_BYTES = 64
SLOT_BYTES = REF_BYTES + SCALAR_BYTES
ELEMENT_BYTES = REF_BYTES + SCALAR_BYTES

ACCOUNT_ID_MIN = 1
ACCOUNT_ID_MAX = 2**63 - 1

_DECIMAL = re.compile(r"-?[0-9]+")
_FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ELLIPSIS = "..."
_KEY_TYPE_NAMES = "a 64-bit int, str, Account or Currency"
_KIND_OF = operator.attrgetter("kind")


class GasMeter:
    """Gas used by a top-level execution against its limit and a stack of call sub-limits; ceiling is the effective limit."""

    __slots__ = ("_ceilings", "ceiling", "limit", "used")

    def __init__(self, limit: int) -> None:
        if type(limit) is not int or limit < 0:
            raise ValueError("gas limit must be a non-negative int")
        self.limit = limit
        self.used = 0
        self.ceiling = limit
        self._ceilings = [limit]

    @property
    def remaining(self) -> int:
        return self.ceiling - self.used

    @property
    def level_count(self) -> int:
        """Number of limits on the stack, the top-level limit included."""
        return len(self._ceilings)

    def charge(self, amount: int) -> None:
        """Consume amount gas, or raise OutOfGas (consuming the rest of the effective limit) when it is not available."""
        used = self.used + amount
        if used > self.ceiling:
            raise self.exhausted()
        self.used = used

    def exhausted(self) -> OutOfGas:
        """Mark the effective limit as used up and return the OutOfGas naming the outermost limit at that ceiling."""
        level = self._ceilings.index(self.ceiling)
        self.used = self.ceiling
        if level == 0:
            return OutOfGas(f"out of gas: the gas limit of {self.limit} is used up", level=0)
        return OutOfGas("out of gas: the gas limit given to this call is used up", level=level)

    def push_limit(self, gas: int) -> int:
        """Open a sub-limit allowing at most gas more units and return its level."""
        ceiling = min(self.used + gas, self.ceiling)
        self._ceilings.append(ceiling)
        self.ceiling = ceiling
        return len(self._ceilings) - 1

    def pop_limit(self, level: int) -> None:
        """Close the sub-limit opened at level, which must be the innermost one."""
        if level != len(self._ceilings) - 1 or level == 0:
            raise RuntimeError("gas sub-limits must be closed innermost first")
        self._ceilings.pop()
        self.ceiling = self._ceilings[-1]


class _Roots(Protocol):
    frames: list[_Frame]


class MemoryMeter:
    """Accounted memory of a top-level execution, over the frames of every execution attached to it.

    Size model: a frame costs FRAME_BYTES plus SLOT_BYTES per local and per operand stack slot of its function; a str held by a slot costs STR_BASE_BYTES plus its length per reference; a list, dict or record costs OBJECT_BYTES once per object plus, per element, key or field, REF_BYTES and the content (STR_BASE_BYTES plus the length for a str, SCALAR_BYTES for another scalar, nothing more for a list, dict or record, which is counted once).
    live + pending is an upper bound of the accounted size; allocate() measures exactly whenever that bound would pass the limit, and each measurement charges memory_walk_gas(references visited) to gas.
    """

    __slots__ = ("_roots", "gas", "limit", "live", "pending", "visited", "walks")

    def __init__(self, limit: int, gas: GasMeter | None = None) -> None:
        self.limit = limit
        self.gas = gas
        self.live = 0
        self.pending = 0
        self.walks = 0
        self.visited = 0
        self._roots: list[_Roots] = []

    def attach(self, root: _Roots) -> None:
        self._roots.append(root)

    def detach(self, root: _Roots) -> None:
        for index in range(len(self._roots) - 1, -1, -1):
            if self._roots[index] is root:
                del self._roots[index]
                return

    def allocate(self, nbytes: int) -> None:
        """Account for an operation about to raise the accounted size by at most nbytes; raises LimitExceeded when it would pass the limit."""
        if self.live + self.pending + nbytes > self.limit:
            self.collect(nbytes)
        else:
            self.pending += nbytes

    def collect(self, incoming: int = 0) -> int:
        """Measure live memory exactly; raises LimitExceeded when it plus incoming bytes is above the limit."""
        self.walks += 1
        total, visited = self.measure(self.limit - incoming)
        self.visited += visited
        if self.gas is not None:
            self.gas.charge(memory_walk_gas(visited))
        if total + incoming > self.limit:
            raise LimitExceeded(LimitResource.MEMORY, f"memory limit of {self.limit} bytes exceeded")
        self.live = total
        self.pending = incoming
        return total

    def measure(self, stop_above: int | None = None) -> tuple[int, int]:
        """(accounted bytes, references visited) over all attached frames; counting stops once the total passes stop_above."""
        bound = self.limit if stop_above is None else stop_above
        total = 0
        visited = 0
        seen: set[int] = set()
        objects: list[Any] = []
        for root in self._roots:
            for frame in root.frames:
                fn = frame.function
                total += FRAME_BYTES + SLOT_BYTES * (fn.local_count + fn.max_stack)
                for slots in (frame.stack, frame.locals):
                    for value in slots:
                        visited += 1
                        t = type(value)
                        if t is str:
                            total += STR_BASE_BYTES + len(value)
                        elif (t is list or t is dict or t is Record) and id(value) not in seen:
                            seen.add(id(value))
                            objects.append(value)
                while objects:
                    obj = objects.pop()
                    t = type(obj)
                    if t is list:
                        children: Iterable[Any] = obj
                        count = len(obj)
                    elif t is dict:
                        children = (*obj.keys(), *obj.values())
                        count = 2 * len(obj)
                    else:
                        children = [v for v in obj.values if v is not None]
                        count = len(obj.values)
                    total += OBJECT_BYTES + REF_BYTES * count
                    for child in children:
                        visited += 1
                        tc = type(child)
                        if tc is str:
                            total += STR_BASE_BYTES + len(child)
                        elif tc is list or tc is dict or tc is Record:
                            if id(child) not in seen:
                                seen.add(id(child))
                                objects.append(child)
                        else:
                            total += SCALAR_BYTES
                    if total > bound:
                        return total, visited
                if total > bound:
                    return total, visited
        return total, visited


def _slot_bytes(value: object) -> int:
    """Accounted bytes a new reference to value adds when held by a stack slot or local."""
    if type(value) is str:
        return STR_BASE_BYTES + len(value)
    return 0


def _moved_bytes(value: object) -> int:
    """Accounted bytes added when value moves from a slot into a container element (apart from the reference)."""
    t = type(value)
    if t is str or t is list or t is dict or t is Record:
        return 0
    return SCALAR_BYTES


def _copy_bytes(items: list[Any]) -> int:
    """Upper bound of the accounted bytes of a new list holding the elements of items, computed with C-level scans."""
    strs = list(filter(str.__instancecheck__, items))
    return OBJECT_BYTES + ELEMENT_BYTES * len(items) + STR_BASE_BYTES * len(strs) + sum(map(len, strs))


def _fresh_bytes(value: object) -> int:
    """Accounted bytes of a newly built value tree held by one slot."""
    t = type(value)
    if t is str:
        return STR_BASE_BYTES + len(value)  # type: ignore[arg-type]
    if t is list or t is dict or t is Record:
        total = 0
        work: list[Any] = [value]
        while work:
            obj = work.pop()
            to = type(obj)
            children = list(obj) if to is list else ((*obj.keys(), *obj.values()) if to is dict else obj.values)
            total += OBJECT_BYTES
            for child in children:
                tc = type(child)
                total += REF_BYTES
                if tc is str:
                    total += STR_BASE_BYTES + len(child)
                elif tc is list or tc is dict or tc is Record:
                    work.append(child)
                elif child is not None:
                    total += SCALAR_BYTES
        return total
    return 0


class Deadline:
    """The wall-clock deadline of a top-level execution, shared by nested calls; time inside suspended() does not count."""

    __slots__ = ("_suspended_since", "at", "clock", "seconds")

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.seconds = seconds
        self.at = clock() + seconds
        self._suspended_since: float | None = None

    def check(self) -> None:
        if self.clock() > self.at:
            raise LimitExceeded(LimitResource.WALL_CLOCK, f"wall-clock limit of {self.seconds} seconds exceeded")

    async def pause(self) -> None:
        """Let the event loop run other tasks once, with the clock stopped, so a compute-bound execution shares the loop and the time other tasks take is not charged to it."""
        with self.suspended():
            await asyncio.sleep(0)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Stop the clock for the block by moving the deadline later by the time it took; a Host wraps its database waits in it, and a nested suspension counts once."""
        if self._suspended_since is not None:
            yield
            return
        start = self.clock()
        self._suspended_since = start
        try:
            yield
        finally:
            self._suspended_since = None
            self.at += self.clock() - start


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything one contract execution sees besides its program and arguments.

    gas, memory, deadline, limits and now are shared by the whole top-level execution; random_log collects the values random() returned in this execution only (at most limits.max_random_draws).
    call_stack lists the contract accounts on the current call stack (self_account last), depth is len(call_stack), and parent is the calling execution's context.
    host_data is opaque to the VM; a Host may replace it with dataclasses.replace() before running a nested execution.
    """

    self_account: Account
    sender: Account
    paid: Attachment | None
    now: int
    depth: int
    call_stack: tuple[Account, ...]
    gas: GasMeter
    memory: MemoryMeter
    deadline: Deadline
    limits: Limits
    random_log: list[int] = field(default_factory=list)
    parent: ExecutionContext | None = None
    host_data: object = None

    @classmethod
    def top_level(
        cls,
        *,
        self_account: Account,
        sender: Account,
        paid: Attachment | None,
        now: int,
        gas_limit: int,
        limits: Limits = DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
        host_data: object = None,
    ) -> ExecutionContext:
        """The context of a top-level call of self_account's contract by sender; the wall clock starts now."""
        gas = GasMeter(gas_limit)
        return cls(
            self_account=self_account,
            sender=sender,
            paid=paid,
            now=now,
            depth=1,
            call_stack=(self_account,),
            gas=gas,
            memory=MemoryMeter(limits.memory_max_bytes, gas),
            deadline=Deadline(limits.wall_clock_seconds, clock),
            limits=limits,
            host_data=host_data,
        )

    def child(self, target: Account, attach: Attachment | None) -> ExecutionContext:
        """The context of a call from this execution to target's contract."""
        return replace(
            self,
            self_account=target,
            sender=self.self_account,
            paid=attach,
            depth=self.depth + 1,
            call_stack=(*self.call_stack, target),
            random_log=[],
            parent=self,
        )


class Host(Protocol):
    """Ledger, storage, Discord and nested-call services of the VM.

    Every async method receives the context of the execution making the request; arguments are already type-checked (ints within the int limit, Account and Currency wrappers, str of valid Unicode).
    Discord guild, channel and role ids and the user's account id are always within SNOWFLAKE_MIN .. SNOWFLAKE_MAX (1 .. 2**63 - 1).
    A method raises HostError for a failure under a ledger or Discord rule; any other exception aborts the whole execution unchanged.
    Storage methods move encoded bytes (values.encode_value); the VM encodes, decodes, limits and charges for them, and reports a missing key itself.
    """

    async def balance(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int: ...
    async def currency(self, ctx: ExecutionContext, symbol: str) -> Currency: ...
    async def currency_info(self, ctx: ExecutionContext, currency: Currency) -> Record: ...
    async def transfer(self, ctx: ExecutionContext, to: Account, currency: Currency, amount: int) -> None: ...
    async def transfer_from(self, ctx: ExecutionContext, owner: Account, to: Account, currency: Currency, amount: int) -> None: ...
    async def approve(self, ctx: ExecutionContext, spender: Account, currency: Currency, amount: int) -> None: ...
    async def allowance(self, ctx: ExecutionContext, owner: Account, spender: Account, currency: Currency) -> int: ...
    async def create_claim(self, ctx: ExecutionContext, payer: Account, currency: Currency, amount: int, description: str) -> int: ...
    async def pay_claim(self, ctx: ExecutionContext, claim_id: int) -> None: ...
    async def cancel_claim(self, ctx: ExecutionContext, claim_id: int) -> None: ...
    async def claim_info(self, ctx: ExecutionContext, claim_id: int) -> Record: ...
    async def swap(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int, min_out: int) -> int: ...
    async def add_liquidity(
        self,
        ctx: ExecutionContext,
        currency_x: Currency,
        currency_y: Currency,
        fee_bps: int,
        amount_x: int,
        amount_y: int,
        amount_x_min: int,
        amount_y_min: int,
        min_shares: int,
    ) -> int: ...
    async def remove_liquidity(self, ctx: ExecutionContext, currency_x: Currency, currency_y: Currency, fee_bps: int, shares: int, amount_x_min: int, amount_y_min: int) -> list[int]: ...
    async def quote(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int) -> Record: ...
    async def stake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None: ...
    async def unstake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None: ...
    async def staked(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int: ...
    async def storage_get(self, ctx: ExecutionContext, key: str) -> bytes | None: ...
    async def storage_set(self, ctx: ExecutionContext, key: str, data: bytes) -> None: ...
    async def storage_delete(self, ctx: ExecutionContext, key: str) -> None: ...
    async def read_storage(self, ctx: ExecutionContext, account: Account, key: str) -> bytes | None: ...
    async def discord_send(self, ctx: ExecutionContext, guild: int, channel: int, message: str) -> None: ...
    async def discord_add_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None: ...
    async def discord_remove_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None: ...
    async def discord_has_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> bool: ...

    async def call_contract(self, ctx: ExecutionContext, target: Account, function: str, args: list[Scalar], attach: Attachment | None) -> int | str | bool:
        """Run target's contract function with args in the child context ctx (already built by the VM, with any gas sub-limit open), normally by calling execute() with that context.

        Calls the VM refuses (call_rejection()) go to reject_call() instead, so function is a valid function name and args are at most max_params scalars.
        Raise CallFailed when the call cannot be made (no contract, inactive, no such public function, wrong arguments, attachment not payable); let the callee's ContractFailure propagate after undoing its effects.
        """
        ...

    async def reject_call(self, ctx: ExecutionContext, target: Account, function: str, args: list[Any], attach: Attachment | None, failure: CallFailed) -> NoReturn:
        """Record a call the VM refused before it started (failure from call_rejection()) as a failed call of target that used no gas, in the child context ctx with any gas sub-limit open, then raise failure."""
        ...

    def random_below(self, n: int) -> int:
        """A uniformly random int in 0 .. n - 1 from a cryptographically secure source (secrets.randbelow in production)."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """A successful execution: the public function's result, the gas this execution and its nested calls used, and the values random() returned in this execution."""

    value: int | str | bool
    gas_used: int
    random_values: tuple[int, ...]


async def execute(program: VerifiedProgram, function: str, args: Sequence[object], *, host: Host, context: ExecutionContext) -> ExecutionOutcome:
    """Run the public function of program with args; raises a ContractFailure on any defined failure.

    An unknown or non-public function raises CallFailed(NO_FUNCTION), arguments that do not match the parameter types or limits CallFailed(BAD_ARGUMENTS).
    """
    limits = context.limits
    fn = program.public_function(function)
    if fn is None:
        raise CallFailed(CallFailure.NO_FUNCTION, f"no public function {format_value(function[:80])}")
    problem = check_args(fn.params, args, limits)
    if problem is not None:
        raise CallFailed(CallFailure.BAD_ARGUMENTS, f"{fn.name}: {problem}")
    if context.depth > limits.max_contract_depth:
        raise CallFailed(CallFailure.DEPTH, f"contract call depth limit of {limits.max_contract_depth} reached")
    if program.max_constant_str > limits.str_max_length or not limits.int_ok(program.max_constant_int):
        raise ContractRuntimeError("program constants exceed the value limits")
    context.deadline.check()
    run = _Execution(program, host, context)
    start = context.gas.used
    context.memory.attach(run)
    try:
        value = await run.run(fn, list(args))
    finally:
        context.memory.detach(run)
    return ExecutionOutcome(value=value, gas_used=context.gas.used - start, random_values=tuple(context.random_log))


class _Frame:
    __slots__ = ("code", "function", "locals", "pc", "stack")

    def __init__(self, function: VerifiedFunction, code: tuple[Any, ...], stack: list[Any], local_values: list[Any]) -> None:
        self.function = function
        self.code = code
        self.pc = 0
        self.stack = stack
        self.locals = local_values


def _frame_bytes(fn: VerifiedFunction) -> int:
    return FRAME_BYTES + SLOT_BYTES * (fn.local_count + fn.max_stack)


def _rt(message: str) -> ContractRuntimeError:
    return ContractRuntimeError(message)


def _type_name(value: object) -> str:
    if value is None:
        return "no value"
    if isinstance(value, Record):
        return KIND_NAMES[value.kind]
    return type(value).__name__


def _operand_error(operation: str, *values: object) -> ContractRuntimeError:
    return _rt(f"unsupported operand types for {operation}: {', '.join(_type_name(v) for v in values)}")


def _key_error(operation: str, key: object) -> ContractRuntimeError:
    return _rt(f"{operation}: dict keys must be {_KEY_TYPE_NAMES}, not {_type_name(key)}")


_PUSH_INT = int(Op.PUSH_INT)
_PUSH_CONST = int(Op.PUSH_CONST)
_PUSH_TRUE = int(Op.PUSH_TRUE)
_PUSH_FALSE = int(Op.PUSH_FALSE)
_POP = int(Op.POP)
_DUP = int(Op.DUP)
_SWAP = int(Op.SWAP)
_LOAD = int(Op.LOAD)
_STORE = int(Op.STORE)
_ADD = int(Op.ADD)
_SUB = int(Op.SUB)
_MUL = int(Op.MUL)
_EQ = int(Op.EQ)
_NE = int(Op.NE)
_LT = int(Op.LT)
_LE = int(Op.LE)
_GT = int(Op.GT)
_GE = int(Op.GE)
_JUMP = int(Op.JUMP)
_JUMP_IF_FALSE = int(Op.JUMP_IF_FALSE)
_JUMP_IF_TRUE = int(Op.JUMP_IF_TRUE)
_FOR_RANGE = int(Op.FOR_RANGE)
_FOR_EACH = int(Op.FOR_EACH)
_CALL = int(Op.CALL)
_RET = int(Op.RET)
_CALL_BUILTIN = int(Op.CALL_BUILTIN)
_CALL_CONTRACT = int(Op.CALL_CONTRACT)
_TRY_CALL = int(Op.TRY_CALL)
_INDEX_GET = int(Op.INDEX_GET)
_GET_FIELD = int(Op.GET_FIELD)

_SlowOp = Callable[["_Execution", tuple[Any, ...], list[Any]], None]
_PureBuiltin = Callable[["_Execution", list[Any]], Any]


class _Execution:
    """One contract execution: its frames are memory roots while it runs."""

    __slots__ = ("bound", "context_values", "ctx", "frames", "host", "limits", "max_digits", "memory", "meter", "program")

    def __init__(self, program: VerifiedProgram, host: Host, ctx: ExecutionContext) -> None:
        self.program = program
        self.host = host
        self.ctx = ctx
        self.limits = ctx.limits
        self.meter = ctx.gas
        self.memory = ctx.memory
        self.bound = ctx.limits.int_bound
        self.max_digits = len(str(self.bound)) + 1
        self.frames: list[_Frame] = []
        paid = paid_record(ctx.paid)
        by_name: dict[str, tuple[Any, int]] = {"sender": (ctx.sender, 0), "self": (ctx.self_account, 0), "paid": (paid, _fresh_bytes(paid)), "now": (ctx.now, 0)}
        # builtin id -> (value, accounted bytes of one more reference to it) for the context builtins, whose values are fixed for the execution.
        self.context_values: dict[int, tuple[Any, int]] = {b.id: by_name[b.name] for b in program.instruction_set.builtins if b.kind is BuiltinKind.CONTEXT}

    async def run(self, fn: VerifiedFunction, args: list[Any]) -> Any:
        frames = self.frames
        meter = self.meter
        allocate = self.memory.allocate
        bound = self.bound
        functions = self.program.functions
        max_frames = self.limits.max_frames
        deadline = self.ctx.deadline
        slow_ops = _SLOW_OPS
        builtins = self.program.instruction_set.builtins
        host_kind = BuiltinKind.HOST
        context_values = self.context_values
        memory = self.memory
        memory_limit = memory.limit
        code = fn.code
        pc = 0
        free_run = 0
        try:
            allocate(_frame_bytes(fn) + sum(map(_slot_bytes, args)))
            args.extend([None] * (fn.local_count - len(args)))
            frame = _Frame(fn, code, [], args)
            frames.append(frame)
            stack = frame.stack
            locs = frame.locals
            ticks = DEADLINE_CHECK_INTERVAL
            while True:
                ins = code[pc]
                pc += 1
                gas = ins[1]
                if gas > 0:
                    free_run = 0
                    used = meter.used + gas
                    if used > meter.ceiling:
                        raise meter.exhausted()
                    meter.used = used
                else:
                    free_run -= gas
                    if free_run >= FREE_RUN_ALLOWANCE:
                        free_run -= FREE_RUN_ALLOWANCE
                        used = meter.used + 1
                        if used > meter.ceiling:
                            raise meter.exhausted()
                        meter.used = used
                ticks -= 1
                if not ticks:
                    ticks = DEADLINE_CHECK_INTERVAL
                    deadline.check()
                    await deadline.pause()
                op = ins[0]
                # Opcodes are grouped by range: constants, stack and locals below 0x10, arithmetic and comparison below 0x20, control flow below 0x30, collections above.
                if op < 0x10:
                    if op == _LOAD:
                        value = locs[ins[2]]
                        if value is None:
                            raise _rt(f"local {ins[2]} is read before it is assigned")
                        if type(value) is str:
                            # MemoryMeter.allocate() inlined for the most frequent allocation.
                            size = STR_BASE_BYTES + len(value)
                            if memory.live + memory.pending + size > memory_limit:
                                memory.collect(size)
                            else:
                                memory.pending += size
                        stack.append(value)
                    elif op == _STORE:
                        locs[ins[2]] = stack.pop()
                    elif op == _PUSH_INT:
                        stack.append(ins[2])
                    elif op == _PUSH_CONST:
                        value = ins[2]
                        if type(value) is str:
                            size = STR_BASE_BYTES + len(value)
                            if memory.live + memory.pending + size > memory_limit:
                                memory.collect(size)
                            else:
                                memory.pending += size
                        stack.append(value)
                    elif op == _POP:
                        stack.pop()
                    elif op == _DUP:
                        value = stack[-1]
                        if type(value) is str:
                            size = STR_BASE_BYTES + len(value)
                            if memory.live + memory.pending + size > memory_limit:
                                memory.collect(size)
                            else:
                                memory.pending += size
                        stack.append(value)
                    elif op == _PUSH_TRUE:
                        stack.append(True)
                    elif op == _PUSH_FALSE:
                        stack.append(False)
                    else:
                        stack[-1], stack[-2] = stack[-2], stack[-1]
                elif op < 0x20:
                    if op <= _MUL:
                        b = stack.pop()
                        a = stack[-1]
                        if type(a) is not int or type(b) is not int:
                            raise _operand_error(Op(op).name, a, b)
                        result = a + b if op == _ADD else (a - b if op == _SUB else a * b)
                        if not -bound < result < bound:
                            raise _rt("int result exceeds the int limit")
                        stack[-1] = result
                    elif _LT <= op <= _GE:
                        b = stack.pop()
                        a = stack[-1]
                        ta = type(a)
                        if ta is not type(b) or (ta is not int and ta is not str):
                            raise _operand_error(Op(op).name, a, b)
                        stack[-1] = a < b if op == _LT else (a <= b if op == _LE else (a > b if op == _GT else a >= b))
                    elif op == _EQ or op == _NE:  # noqa: SIM109 - chained == avoids building a tuple per instruction
                        b = stack.pop()
                        a = stack[-1]
                        ta = type(a)
                        if ta is not type(b) or ta not in SCALAR_TYPES:
                            raise _operand_error(Op(op).name, a, b)
                        stack[-1] = (a == b) if op == _EQ else (a != b)
                    else:
                        slow_ops[op](self, ins, stack)
                elif op < 0x30:
                    if op == _JUMP_IF_FALSE or op == _JUMP_IF_TRUE:  # noqa: SIM109
                        cond = stack.pop()
                        if cond is True:
                            if op == _JUMP_IF_TRUE:
                                pc = ins[2]
                        elif cond is False:
                            if op == _JUMP_IF_FALSE:
                                pc = ins[2]
                        else:
                            raise _rt(f"condition must be bool, not {_type_name(cond)}")
                    elif op == _JUMP:
                        pc = ins[2]
                    elif op == _FOR_RANGE:
                        counter = locs[ins[2]]
                        stop = locs[ins[3]]
                        step = locs[ins[4]]
                        if type(counter) is not int or type(stop) is not int or type(step) is not int:
                            raise _rt("range loop state must be ints")
                        if step > 0:
                            go = counter < stop
                        elif step < 0:
                            go = counter > stop
                        else:
                            raise _rt("range() step must not be zero")
                        if go:
                            locs[ins[5]] = counter
                            following = counter + step
                            locs[ins[2]] = following if -bound < following < bound else stop
                        else:
                            pc = ins[6]
                    elif op == _FOR_EACH:
                        seq = locs[ins[2]]
                        index = locs[ins[3]]
                        ts = type(seq)
                        if (ts is not list and ts is not str) or type(index) is not int:
                            raise _rt("for loop state must be a list or str and an int index")
                        if 0 <= index < len(seq):
                            element = seq[index]
                            if type(element) is str:
                                allocate(STR_BASE_BYTES + len(element))
                            locs[ins[4]] = element
                            locs[ins[3]] = index + 1
                        else:
                            pc = ins[5]
                    elif op == _CALL:
                        callee = functions[ins[2]]
                        if len(frames) >= max_frames:
                            raise _rt(f"internal call depth limit of {max_frames} reached")
                        count = len(callee.params)
                        if count:
                            call_args = stack[-count:]
                            if tuple(map(type, call_args)) != callee.param_types or (
                                callee.record_positions and tuple(map(_KIND_OF, map(call_args.__getitem__, callee.record_positions))) != callee.record_kinds
                            ):
                                _check_call_args(callee, call_args)
                            allocate(_frame_bytes(callee))
                            del stack[-count:]
                        else:
                            call_args = []
                            allocate(_frame_bytes(callee))
                        call_args.extend([None] * (callee.local_count - count))
                        frame.pc = pc
                        frame = _Frame(callee, callee.code, [], call_args)
                        frames.append(frame)
                        fn = callee
                        code = frame.code
                        stack = frame.stack
                        locs = frame.locals
                        pc = 0
                    elif op == _RET:
                        result_value = None
                        if fn.returns_value:
                            result_value = stack.pop()
                            if not matches(result_value, fn.returns):
                                raise _rt(f"{fn.name}() must return {fn.returns}, not {_type_name(result_value)}")
                        frames.pop()
                        if not frames:
                            return result_value
                        free_run = 0
                        frame = frames[-1]
                        fn = frame.function
                        code = frame.code
                        stack = frame.stack
                        locs = frame.locals
                        pc = frame.pc
                        if result_value is not None:
                            stack.append(result_value)
                    elif op == _CALL_BUILTIN:
                        context_value = context_values.get(ins[2])
                        if context_value is not None:
                            if context_value[1]:
                                allocate(context_value[1])
                            stack.append(context_value[0])
                        else:
                            spec = builtins[ins[2]]
                            if spec.kind is host_kind:
                                await self.builtin(ins, stack)
                            else:
                                self.pure_builtin(spec, ins[3], stack)
                    elif op == _CALL_CONTRACT or op == _TRY_CALL:  # noqa: SIM109
                        await self.contract_call(ins, stack, op == _TRY_CALL)
                    else:
                        slow_ops[op](self, ins, stack)
                elif op == _INDEX_GET:
                    key = stack.pop()
                    container = stack[-1]
                    tc = type(container)
                    if tc is list or tc is str:
                        if type(key) is not int:
                            raise _operand_error("index", container, key)
                        if not -len(container) <= key < len(container):
                            raise _rt("index out of range")
                        found = container[key]
                    elif tc is dict:
                        if not is_key(key):
                            raise _key_error("index", key)
                        found = container.get(key, None)
                        if found is None:
                            raise _rt(f"key {format_value(key)} not found")
                    else:
                        raise _operand_error("index", container, key)
                    if type(found) is str:
                        allocate(STR_BASE_BYTES + len(found))
                    stack[-1] = found
                elif op == _GET_FIELD:
                    record = stack[-1]
                    if type(record) is not Record or record.kind is not ins[2]:
                        raise _rt(f"expected a {KIND_NAMES[ins[2]]} record, got {_type_name(record)}")
                    field_value = record.values[ins[3]]
                    if field_value is None:
                        raise _rt("CallResult.value is not available because the call failed")
                    if type(field_value) is str:
                        allocate(STR_BASE_BYTES + len(field_value))
                    stack[-1] = field_value
                else:
                    slow_ops[op](self, ins, stack)
        except ContractFailure as exc:
            if exc.function is None:
                exc.function = fn.name
                exc.offset = fn.listing[max(pc - 1, 0)].offset
            exc.bound_to(self.limits.message_max_length)
            raise

    def charge(self, amount: int) -> None:
        if amount:
            self.meter.charge(amount)

    def builtin_args(self, spec: Builtin, count: int, stack: list[Any]) -> list[Any]:
        """A copy of the top count stack values, type-checked; they stay on the stack, and so stay counted as live memory, until the builtin is done."""
        if not count:
            return []
        args = stack[-count:]
        for arg, param in zip(args, spec.params, strict=False):
            if not matches(arg, param.type):
                raise _rt(f"{spec.name}() argument {param.name} must be {param.type}, not {_type_name(arg)}")
        return args

    async def builtin(self, ins: tuple[Any, ...], stack: list[Any]) -> None:
        """Run a host builtin."""
        spec = self.program.instruction_set.builtins[ins[2]]
        count = ins[3]
        args = self.builtin_args(spec, count, stack)
        result = await self.host_builtin(spec, args)
        if count:
            del stack[-count:]
        if spec.pushes:
            stack.append(result)

    def pure_builtin(self, spec: Builtin, count: int, stack: list[Any]) -> None:
        """Run a context or pure builtin without a coroutine."""
        args = self.builtin_args(spec, count, stack)
        result = _PURE_BUILTINS[spec.name](self, args)
        if count:
            del stack[-count:]
        if spec.pushes:
            stack.append(result)

    async def call_host(self, spec: Builtin, method_name: str, *args: Any) -> Any:
        deadline = self.ctx.deadline
        deadline.check()
        method = getattr(self.host, method_name)
        try:
            result = await method(self.ctx, *args)
        except HostError as exc:
            raise ContractRuntimeError(f"{spec.name} failed: {exc.code}: {exc.message}", host_code=exc.code) from exc
        deadline.check()
        return result

    def storage_key(self, spec: Builtin, key: str) -> None:
        if not 1 <= len(key) <= self.limits.storage_key_max_length:
            raise _rt(f"{spec.name}(): storage keys must have 1 to {self.limits.storage_key_max_length} characters")

    def stored_value(self, spec: Builtin, key: str, data: object, default: list[Any]) -> Any:
        if data is None:
            if not default:
                raise _rt(f"{spec.name}(): storage key {format_value(key)} not found")
            return default[0]
        if type(data) is not bytes:
            raise HostProtocolError(f"{spec.host_method} must return bytes or None")
        if len(data) > self.limits.storage_value_max_bytes:
            raise _rt(f"{spec.name}(): the stored value is larger than {self.limits.storage_value_max_bytes} bytes")
        value, count = decode_value_counted(data, limits=self.limits)
        self.charge(storage_read_gas(len(data), count) - spec.gas)
        self.memory.allocate(_fresh_bytes(value))
        return value

    async def host_builtin(self, spec: Builtin, args: list[Any]) -> Any:
        name = spec.name
        if name == "storage.get":
            self.storage_key(spec, args[0])
            return self.stored_value(spec, args[0], await self.call_host(spec, "storage_get", args[0]), args[1:])
        if name == "read_storage":
            self.storage_key(spec, args[1])
            return self.stored_value(spec, args[1], await self.call_host(spec, "read_storage", args[0], args[1]), args[2:])
        if name == "storage.set":
            self.storage_key(spec, args[0])
            key_bytes = len(args[0].encode("utf-8"))
            try:
                encoded, count = encode_value_counted(args[1], limits=self.limits)
            except StorageValueError as exc:
                self.charge(storage_write_gas(key_bytes + exc.encoded_bytes, exc.values) - spec.gas)
                raise
            self.charge(storage_write_gas(key_bytes + len(encoded), count) - spec.gas)
            await self.call_host(spec, "storage_set", args[0], encoded)
            return None
        if name == "storage.delete":
            self.storage_key(spec, args[0])
            await self.call_host(spec, "storage_delete", args[0])
            return None
        if name.startswith("discord."):
            for arg, param in zip(args, spec.params, strict=True):
                if param.type.kind is Kind.STR:
                    continue
                ident = arg.id if type(arg) is Account else arg
                if not SNOWFLAKE_MIN <= ident <= SNOWFLAKE_MAX:
                    raise _rt(f"{name}(): {param.name} must be a Discord id between {SNOWFLAKE_MIN} and {SNOWFLAKE_MAX}")
        result = await self.call_host(spec, spec.host_method or spec.name, *args)
        return self.checked_host_result(spec, result)

    def checked_host_result(self, spec: Builtin, result: object) -> Any:
        returns = spec.returns
        kind = returns.kind
        if kind is Kind.NONE:
            problem = None if result is None else "must return None"
        elif kind in RECORD_KINDS:
            problem = record_problem(result, kind, self.limits)
            if problem is None:
                self.memory.allocate(_fresh_bytes(result))
        elif kind is Kind.LIST:
            if type(result) is not list:
                problem = "must return a list"
            else:
                result = list(result)
                problem = None
                if len(result) > self.limits.collection_max_length:
                    problem = "returned too many elements"
                for item in result:
                    if not matches(item, returns.args[0]) or scalar_problem(item, self.limits) is not None:
                        problem = f"must return {returns}"
                        break
                if problem is None:
                    self.memory.allocate(_fresh_bytes(result))
        elif not matches(result, returns):
            problem = f"must return {returns}"
        else:
            problem = scalar_problem(result, self.limits)
            if problem is None:
                self.memory.allocate(_slot_bytes(result))
        if problem is not None:
            raise HostProtocolError(f"Host.{spec.host_method} {problem}")
        return result

    async def contract_call(self, ins: tuple[Any, ...], stack: list[Any], catch: bool) -> None:
        flags = ins[2]
        argc = ins[3]
        limits = self.limits
        gas: Any = stack.pop() if flags & CALL_GAS else None
        attach: Attachment | None = None
        if flags & CALL_ATTACH:
            amount = stack.pop()
            currency = stack.pop()
            if type(currency) is not Currency or type(amount) is not int:
                raise _operand_error("attach", currency, amount)
            if amount <= 0:
                raise _rt("attached amount must be positive")
            attach = Attachment(currency, amount)
        if flags & CALL_ARGS_LIST:
            arg_list = stack.pop()
            if type(arg_list) is not list:
                raise _rt(f"call arguments must be a list, not {_type_name(arg_list)}")
            args = list(arg_list)
        elif argc:
            args = stack[-argc:]
            del stack[-argc:]
        else:
            args = []
        function = stack.pop()
        target = stack.pop()
        if type(target) is not Account:
            raise _rt(f"call target must be an Account, not {_type_name(target)}")
        if type(function) is not str:
            raise _rt(f"function name must be a str, not {_type_name(function)}")
        if gas is not None and (type(gas) is not int or gas <= 0):
            raise _rt("call gas must be a positive int")
        ctx = self.ctx
        meter = self.meter
        level = meter.level_count
        try:
            rejection = call_rejection(ctx, target, function, args)
            ctx.deadline.check()
            child = ctx.child(target, attach)
            sub = meter.push_limit(gas) if gas is not None else None
            try:
                if rejection is not None:
                    await self.host.reject_call(child, target, function, args, attach, rejection)
                    raise HostProtocolError("Host.reject_call must raise the call's failure")
                try:
                    result = await self.host.call_contract(child, target, function, args, attach)
                except HostError as exc:
                    raise ContractRuntimeError(f"call failed: {exc.code}: {exc.message}", host_code=exc.code) from exc
            finally:
                if sub is not None:
                    meter.pop_limit(sub)
            ctx.deadline.check()
            if type(result) not in PUBLIC_RESULT_TYPES or scalar_problem(result, limits) is not None:
                raise HostProtocolError("Host.call_contract must return an int, str or bool within the limits")
        except ContractFailure as exc:
            if catch and try_call_catches(exc, level):
                record = call_result_failure(exc.kind, bound_message(exc.message, limits.message_max_length))
                self.memory.allocate(_fresh_bytes(record))
                stack.append(record)
                return
            raise
        if catch:
            record = call_result_ok(result)
            self.memory.allocate(_fresh_bytes(record))
            stack.append(record)
        else:
            self.memory.allocate(_slot_bytes(result))
            stack.append(result)


def _check_call_args(callee: VerifiedFunction, call_args: list[Any]) -> None:
    for arg, param in zip(call_args, callee.params, strict=True):
        if not matches(arg, param):
            raise _rt(f"{callee.name}() argument must be {param}, not {_type_name(arg)}")


def _op_floordiv_mod(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    b = stack.pop()
    a = stack[-1]
    if type(a) is not int or type(b) is not int:
        raise _operand_error(Op(ins[0]).name, a, b)
    if b == 0:
        raise _rt("division by zero")
    stack[-1] = a // b if ins[0] == Op.FLOORDIV else a % b


def _op_neg(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    a = stack[-1]
    if type(a) is not int:
        raise _operand_error("NEG", a)
    stack[-1] = -a


def _op_not(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    a = stack[-1]
    if type(a) is not bool:
        raise _operand_error("NOT", a)
    stack[-1] = not a


def _op_revert(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    message = stack.pop()
    if type(message) is not str:
        raise _rt(f"revert message must be a str, not {_type_name(message)}")
    raise ContractRevert(message)


def _op_assert(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    cond = stack.pop()
    if cond is True:
        return
    if cond is False:
        raise ContractRevert(ins[2])
    raise _rt(f"assert condition must be bool, not {_type_name(cond)}")


def _op_concat(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    a, b = stack[-2:]
    ta = type(a)
    if ta is not type(b) or (ta is not str and ta is not list):
        raise _operand_error("CONCAT", a, b)
    size = len(a) + len(b)
    if ta is str:
        if size > run.limits.str_max_length:
            raise _rt(f"str longer than {run.limits.str_max_length} characters")
        run.charge(str_gas(size))
        run.memory.allocate(STR_BASE_BYTES + size)
    else:
        if size > run.limits.collection_max_length:
            raise _rt(f"list longer than {run.limits.collection_max_length} elements")
        run.charge(elements_gas(size * COPY_ELEMENT_WEIGHT))
        run.memory.allocate(_copy_bytes(a) + _copy_bytes(b) - OBJECT_BYTES)
    stack.pop()
    stack[-1] = a + b


def _op_slice(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    flags = ins[2]
    stop = stack.pop() if flags & SLICE_STOP else None
    start = stack.pop() if flags & SLICE_START else None
    container = stack[-1]
    tc = type(container)
    if (tc is not str and tc is not list) or (start is not None and type(start) is not int) or (stop is not None and type(stop) is not int):
        raise _operand_error("SLICE", container, *(v for v in (start, stop) if v is not None))
    size = len(range(len(container))[start:stop])
    if tc is str:
        run.charge(str_gas(size))
        run.memory.allocate(STR_BASE_BYTES + size)
        stack[-1] = container[start:stop]
    else:
        run.charge(elements_gas(size * COPY_ELEMENT_WEIGHT))
        result = container[start:stop]
        run.memory.allocate(_copy_bytes(result))
        stack[-1] = result


def _op_index_set(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    container, key, value = stack[-3:]
    tc = type(container)
    if tc is list:
        if type(key) is not int:
            raise _operand_error("index", container, key)
        if not -len(container) <= key < len(container):
            raise _rt("index out of range")
        run.memory.allocate(_moved_bytes(value))
        container[key] = value
    elif tc is dict:
        if not is_key(key):
            raise _key_error("index assignment", key)
        if key not in container:
            if len(container) >= run.limits.collection_max_length:
                raise _rt(f"dict larger than {run.limits.collection_max_length} entries")
            run.memory.allocate(2 * REF_BYTES + _moved_bytes(key) + _moved_bytes(value))
        else:
            run.memory.allocate(_moved_bytes(value))
        container[key] = value
    else:
        raise _operand_error("index assignment", container, key)
    del stack[-3:]


def _op_len(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    value = stack[-1]
    tv = type(value)
    if tv is not str and tv is not list and tv is not dict:
        raise _operand_error("len", value)
    stack[-1] = len(value)


def _op_contains(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    container = stack.pop()
    needle = stack[-1]
    tc = type(container)
    if tc is str:
        if type(needle) is not str:
            raise _operand_error("in", needle, container)
        run.charge(str_gas(len(container) + len(needle)))
        found = needle in container
    elif tc is list:
        tn = type(needle)
        if tn not in SCALAR_TYPES:
            raise _operand_error("in", needle, container)
        per_element = 1 + len(needle) // NEEDLE_CHARS_PER_ELEMENT if tn is str else 1
        run.charge(elements_gas(len(container) * per_element))
        if contains_is_ambiguous(container, needle):
            run.charge(elements_gas(len(container) * AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT))
            found = list_contains(container, needle)
        else:
            found = needle in container
    elif tc is dict:
        if not is_key(needle):
            raise _key_error("in", needle)
        found = needle in container
    else:
        raise _operand_error("in", needle, container)
    stack[-1] = found


def _op_build_list(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    count = ins[2]
    if count > run.limits.collection_max_length:
        raise _rt(f"list longer than {run.limits.collection_max_length} elements")
    run.charge(elements_gas(count))
    run.memory.allocate(OBJECT_BYTES + ELEMENT_BYTES * count)
    items = stack[-count:] if count else []
    if count:
        del stack[-count:]
    stack.append(items)


def _op_build_dict(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    count = ins[2]
    if count > run.limits.collection_max_length:
        raise _rt(f"dict larger than {run.limits.collection_max_length} entries")
    run.charge(elements_gas(count))
    flat = stack[-2 * count :] if count else []
    keys = flat[0::2]
    if not keys_are_valid(keys):
        raise _key_error("dict", next(key for key in keys if not is_key(key)))
    run.memory.allocate(OBJECT_BYTES + 2 * ELEMENT_BYTES * count)
    if count:
        del stack[-2 * count :]
    stack.append(dict(zip(keys, flat[1::2], strict=True)))


def _op_append(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    target, value = stack[-2:]
    if type(target) is not list:
        raise _operand_error("append", target)
    if len(target) >= run.limits.collection_max_length:
        raise _rt(f"list longer than {run.limits.collection_max_length} elements")
    run.memory.allocate(REF_BYTES + _moved_bytes(value))
    target.append(value)
    del stack[-2:]


def _op_list_pop(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    index = stack.pop() if ins[2] & LIST_POP_INDEX else None
    target = stack[-1]
    if type(target) is not list or (index is not None and type(index) is not int):
        raise _operand_error("pop", target, *(() if index is None else (index,)))
    if not target:
        raise _rt("pop from an empty list")
    if index is None:
        stack[-1] = target.pop()
        return
    if not -len(target) <= index < len(target):
        raise _rt("pop index out of range")
    run.charge(elements_gas(len(target)))
    stack[-1] = target.pop(index)


def _op_delete_item(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    key = stack.pop()
    container = stack.pop()
    tc = type(container)
    if tc is list:
        if type(key) is not int:
            raise _operand_error("del", container, key)
        if not -len(container) <= key < len(container):
            raise _rt("index out of range")
        run.charge(elements_gas(len(container)))
        del container[key]
    elif tc is dict:
        if not is_key(key):
            raise _key_error("del", key)
        if key not in container:
            raise _rt(f"key {format_value(key)} not found")
        del container[key]
    else:
        raise _operand_error("del", container, key)


def _op_check_type(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    value = stack[-1]
    wanted: Type = ins[2]
    if not _conforms(run, value, wanted):
        raise _rt(f"expected {wanted}, got {_type_name(value)}")


def _elements_conform(run: _Execution, items: Iterable[Any], count: int, t: Type, work: list[tuple[Any, Type]]) -> bool:
    """Check or queue the elements of one container against t; scalar element types are checked with one C-level pass."""
    kind = t.kind
    run.charge(elements_gas(count))
    if kind is Kind.VALUE:
        return True
    if kind in SCALAR_KINDS:
        return {PYTHON_TYPES[kind]}.issuperset(map(type, items))
    work.extend((item, t) for item in items)
    return True


def _conforms(run: _Execution, value: Any, wanted: Type) -> bool:
    """Deep type check; each nested list, dict or record visited after the first costs 1 gas, containers are visited once per (object, type), and scalar elements are charged as elements_gas()."""
    work: list[tuple[Any, Type]] = [(value, wanted)]
    seen: set[tuple[int, Type]] = set()
    first = True
    while work:
        item, t = work.pop()
        kind = t.kind
        if kind is Kind.VALUE:
            continue
        if not first:
            run.charge(1)
        first = False
        if not matches(item, t):
            return False
        if kind is Kind.LIST or kind is Kind.DICT:
            marker = (id(item), t)
            if marker in seen:
                continue
            seen.add(marker)
            if kind is Kind.LIST:
                if not _elements_conform(run, item, len(item), t.args[0], work):
                    return False
            elif not _elements_conform(run, item.keys(), len(item), t.args[0], work) or not _elements_conform(run, item.values(), len(item), t.args[1], work):
                return False
    return True


def _op_to_int(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    value = stack[-1]
    tv = type(value)
    if tv is int:
        return
    if tv is bool:
        stack[-1] = int(value)
    elif tv is Account or tv is Currency:
        stack[-1] = value.id
    elif tv is str:
        if len(value) > run.max_digits or not _DECIMAL.fullmatch(value):
            raise _rt(f"int() cannot parse {format_value(value[:40])}")
        result = int(value)
        if not -run.bound < result < run.bound:
            raise _rt("int() result exceeds the int limit")
        stack[-1] = result
    else:
        raise _operand_error("int()", value)


def _op_to_str(run: _Execution, ins: tuple[Any, ...], stack: list[Any]) -> None:
    value = stack[-1]
    tv = type(value)
    if tv is str:
        return
    if tv is int or tv is bool:
        text = str(value)
    elif tv is Account or tv is Currency:
        text = str(value.id)
    else:
        raise _operand_error("str()", value)
    run.memory.allocate(STR_BASE_BYTES + len(text))
    stack[-1] = text


_SLOW_OPS: dict[int, _SlowOp] = {
    int(Op.FLOORDIV): _op_floordiv_mod,
    int(Op.MOD): _op_floordiv_mod,
    int(Op.NEG): _op_neg,
    int(Op.NOT): _op_not,
    int(Op.REVERT): _op_revert,
    int(Op.ASSERT): _op_assert,
    int(Op.CONCAT): _op_concat,
    int(Op.SLICE): _op_slice,
    int(Op.INDEX_SET): _op_index_set,
    int(Op.LEN): _op_len,
    int(Op.CONTAINS): _op_contains,
    int(Op.BUILD_LIST): _op_build_list,
    int(Op.BUILD_DICT): _op_build_dict,
    int(Op.APPEND): _op_append,
    int(Op.LIST_POP): _op_list_pop,
    int(Op.DELETE_ITEM): _op_delete_item,
    int(Op.CHECK_TYPE): _op_check_type,
    int(Op.TO_INT): _op_to_int,
    int(Op.TO_STR): _op_to_str,
}


def _b_now(run: _Execution, args: list[Any]) -> Any:
    return run.ctx.now


def _b_paid(run: _Execution, args: list[Any]) -> Any:
    record = paid_record(run.ctx.paid)
    run.memory.allocate(_fresh_bytes(record))
    return record


def _b_sha256(run: _Execution, args: list[Any]) -> Any:
    text: str = args[0]
    run.charge(str_gas(len(text)))
    run.memory.allocate(STR_BASE_BYTES + 64)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _b_random(run: _Execution, args: list[Any]) -> Any:
    low, high = args
    if low > high:
        raise _rt("random(): low must not be greater than high")
    log = run.ctx.random_log
    if len(log) >= run.limits.max_random_draws:
        raise _rt(f"random(): at most {run.limits.max_random_draws} draws per execution")
    span = high - low + 1
    drawn = run.host.random_below(span)
    if type(drawn) is not int or not 0 <= drawn < span:
        raise HostProtocolError("Host.random_below returned a value outside 0 .. n - 1")
    value: int = low + drawn
    log.append(value)
    return value


def _b_abs(run: _Execution, args: list[Any]) -> Any:
    return abs(args[0])


def _b_account(run: _Execution, args: list[Any]) -> Any:
    ident = args[0]
    if not ACCOUNT_ID_MIN <= ident <= ACCOUNT_ID_MAX:
        raise _rt(f"Account() id must be between {ACCOUNT_ID_MIN} and {ACCOUNT_ID_MAX}")
    return Account(ident)


def _b_currency(run: _Execution, args: list[Any]) -> Any:
    ident = args[0]
    if not ACCOUNT_ID_MIN <= ident <= ACCOUNT_ID_MAX:
        raise _rt(f"Currency() id must be between {ACCOUNT_ID_MIN} and {ACCOUNT_ID_MAX}")
    return Currency(ident)


def _new_str(run: _Execution, text: str) -> str:
    if len(text) > run.limits.str_max_length:
        raise _rt(f"str longer than {run.limits.str_max_length} characters")
    run.memory.allocate(STR_BASE_BYTES + len(text))
    return text


def _b_split(run: _Execution, args: list[Any]) -> Any:
    text, sep = args
    if not sep:
        raise _rt("split(): empty separator")
    run.charge(str_gas(len(text)))
    count = text.count(sep) + 1
    if count > run.limits.collection_max_length:
        raise _rt(f"split() result longer than {run.limits.collection_max_length} elements")
    run.charge(elements_gas(count))
    run.memory.allocate(OBJECT_BYTES + (REF_BYTES + STR_BASE_BYTES) * count + len(text))
    return text.split(sep)


def _b_find(run: _Execution, args: list[Any]) -> Any:
    text, sub = args
    run.charge(str_gas(len(text) + len(sub)))
    return text.find(sub)


def _b_startswith(run: _Execution, args: list[Any]) -> Any:
    text, prefix = args
    run.charge(str_gas(len(prefix)))
    return text.startswith(prefix)


def _b_endswith(run: _Execution, args: list[Any]) -> Any:
    text, suffix = args
    run.charge(str_gas(len(suffix)))
    return text.endswith(suffix)


def _b_replace(run: _Execution, args: list[Any]) -> Any:
    text, old, new = args
    run.charge(str_gas(len(text)))
    count = text.count(old)
    size = len(text) + count * (len(new) - len(old))
    if size > run.limits.str_max_length:
        raise _rt(f"str longer than {run.limits.str_max_length} characters")
    run.charge(str_gas(size))
    return _new_str(run, text.replace(old, new))


def _case_function(method: Callable[[str], str]) -> _PureBuiltin:
    def apply(run: _Execution, args: list[Any]) -> Any:
        text = args[0]
        run.charge(str_gas(len(text)))
        return _new_str(run, method(text))

    return apply


def _b_join(run: _Execution, args: list[Any]) -> Any:
    sep, items = args
    run.charge(elements_gas(len(items)))
    if not {str}.issuperset(map(type, items)):
        bad = next(item for item in items if type(item) is not str)
        raise _rt(f"join() items must be str, not {_type_name(bad)}")
    size = len(sep) * max(len(items) - 1, 0) + sum(map(len, items))
    if size > run.limits.str_max_length:
        raise _rt(f"str longer than {run.limits.str_max_length} characters")
    run.charge(str_gas(size))
    return _new_str(run, sep.join(items))


def _b_dict_get(run: _Execution, args: list[Any]) -> Any:
    mapping, key, default = args
    if not is_key(key):
        raise _key_error("dict.get", key)
    result = mapping.get(key, default)
    run.memory.allocate(_slot_bytes(result))
    return result


def _b_dict_keys(run: _Execution, args: list[Any]) -> Any:
    mapping = args[0]
    run.charge(elements_gas(len(mapping) * COPY_ELEMENT_WEIGHT))
    result = list(mapping.keys())
    run.memory.allocate(_copy_bytes(result))
    return result


def _b_dict_values(run: _Execution, args: list[Any]) -> Any:
    mapping = args[0]
    run.charge(elements_gas(len(mapping) * COPY_ELEMENT_WEIGHT))
    result = list(mapping.values())
    run.memory.allocate(_copy_bytes(result))
    return result


_PURE_BUILTINS: dict[str, _PureBuiltin] = {
    "sender": lambda run, args: run.ctx.sender,
    "self": lambda run, args: run.ctx.self_account,
    "paid": _b_paid,
    "now": _b_now,
    "sha256": _b_sha256,
    "random": _b_random,
    "min": lambda run, args: min(args[0], args[1]),
    "max": lambda run, args: max(args[0], args[1]),
    "abs": _b_abs,
    "Account": _b_account,
    "Currency": _b_currency,
    "str.split": _b_split,
    "str.find": _b_find,
    "str.startswith": _b_startswith,
    "str.endswith": _b_endswith,
    "str.replace": _b_replace,
    "str.upper": _case_function(str.upper),
    "str.lower": _case_function(str.lower),
    "str.strip": _case_function(str.strip),
    "str.join": _b_join,
    "dict.get": _b_dict_get,
    "dict.keys": _b_dict_keys,
    "dict.values": _b_dict_values,
}


def check_args(params: Iterable[Type], args: Sequence[object], limits: Limits = DEFAULT_LIMITS) -> str | None:
    """Why args do not fit params as public-call arguments, or None; hosts use it to report CallFailed(BAD_ARGUMENTS) before moving an attachment."""
    params = tuple(params)
    if len(args) != len(params):
        return f"expected {len(params)} arguments, got {len(args)}"
    for position, (arg, param) in enumerate(zip(args, params, strict=True)):
        if not matches(arg, param) or scalar_problem(arg, limits) is not None:
            return f"argument {position} must be {param}"
    return None


def is_function_name(name: str, max_length: int = NAME_MAX_LENGTH) -> bool:
    """Whether name is an identifier of at most max_length ASCII characters, the only names a binary's functions can have."""
    return len(name) <= max_length and _FUNCTION_NAME.fullmatch(name) is not None


def function_label(name: str, max_length: int = NAME_MAX_LENGTH) -> str:
    """name itself when it is a function name of at most max_length characters, else its quoted and escaped ASCII form cut to fit max_length (ending in "..." when cut), which is never an identifier."""
    if is_function_name(name, max_length):
        return name
    label = ascii(name)
    if len(label) <= max_length:
        return label
    keep = min(len(name), max_length)
    while keep > 0 and len(ascii(name[:keep])) + len(_ELLIPSIS) > max_length:
        keep -= 1
    return ascii(name[:keep]) + _ELLIPSIS


def call_rejection(ctx: ExecutionContext, target: Account, function: str, args: Sequence[object]) -> CallFailed | None:
    """The CallFailed for a nested call the VM refuses before any host is asked, or None: BAD_ARGUMENTS for more than max_params arguments or one that is not a scalar, DEPTH past the contract depth limit, REENTRANCY for a contract already on the call stack, and NO_FUNCTION for a name no binary can contain, checked in that order."""
    limits = ctx.limits
    if len(args) > limits.max_params:
        return CallFailed(CallFailure.BAD_ARGUMENTS, f"{len(args)} call arguments, more than {limits.max_params}")
    for position, arg in enumerate(args):
        if type(arg) not in SCALAR_TYPES:
            return CallFailed(CallFailure.BAD_ARGUMENTS, f"call argument {position} must be int, str, bool, Account or Currency, not {_type_name(arg)}")
    if ctx.depth >= limits.max_contract_depth:
        return CallFailed(CallFailure.DEPTH, f"contract call depth limit of {limits.max_contract_depth} reached")
    if target in ctx.call_stack:
        return CallFailed(CallFailure.REENTRANCY, f"contract {target.id} is already executing")
    if not is_function_name(function):
        return CallFailed(CallFailure.NO_FUNCTION, f"no public function {function_label(function)}")
    return None


__all__ = [
    "Deadline",
    "ExecutionContext",
    "ExecutionOutcome",
    "GasMeter",
    "Host",
    "MemoryMeter",
    "Value",
    "call_rejection",
    "check_args",
    "execute",
    "function_label",
    "is_function_name",
]
