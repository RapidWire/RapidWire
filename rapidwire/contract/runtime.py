"""The database-backed contract runtime: LedgerHost implements the VM's Host over one open LedgerTx by calling the public in-transaction operations of the ledger services as the executing contract's account, runs nested calls as child executions, and RuntimeConfig carries the settings a contract call needs."""

from __future__ import annotations

import asyncio
import functools
import itertools
import secrets
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Final, NoReturn

from sqlalchemy import select

from rapidwire.config import CONTRACT_SIZE_LIMIT, MAX_GAS_LIMIT, Settings
from rapidwire.contract.costs import route_search_gas
from rapidwire.contract.errors import CallFailed, CallFailure, ContractFailure, ContractRevert, ContractRuntimeError, HostError, LimitExceeded, LimitResource, OutOfGas, VerificationError
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.types import Kind
from rapidwire.contract.values import SCALAR_TYPES, Account, Attachment, Currency, Record, Scalar, make_record, scalar_problem
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.contract.vm import ExecutionContext, ExecutionOutcome, check_args, execute, function_label
from rapidwire.core import allowances, claims, contracts, pools, queries, staking, swap, transfers
from rapidwire.core.currency import get_currency, get_currency_by_symbol
from rapidwire.core.discord_permissions import is_discord_allowed
from rapidwire.core.errors import (
    ContractCallFailed,
    ContractExecutionError,
    ContractLimitExceeded,
    ContractOutOfGas,
    ContractReverted,
    ContractRuntimeFailure,
    CurrencyNotFound,
    DomainError,
    InvalidArgument,
    PermissionDenied,
)
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, run_child, set_failure_output
from rapidwire.core.outbox import enqueue
from rapidwire.core.permissions import GuildInfo
from rapidwire.core.types import ExecutionType, OutboxKind, TransferKind
from rapidwire.core.validation import BIGINT_MAX, is_user_account_id
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX, ContractStatus, contract_storage
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "CONTRACT_SWAP_CHUNK_COUNT",
    "CONTRACT_SWAP_MAX_EXPANSIONS",
    "DISCORD_MESSAGE_MAX_LENGTH",
    "HAS_ROLE_TIMEOUT_SECONDS",
    "HOST_TIME_SECONDS",
    "MAX_CONCURRENT_DRY_RUNS",
    "MAX_CONCURRENT_EXECUTIONS",
    "HostFrame",
    "LedgerHost",
    "RandomDraw",
    "RuntimeConfig",
    "call_input",
    "check_callable",
    "contract_error",
    "contract_swap_config",
    "dry_run_slot",
    "encode_args",
    "encode_result",
    "execution_slot",
    "failure_fields",
    "random_tree",
    "recorded_args",
    "recorded_function_name",
    "runnable_program",
    "validate_call_args",
    "verified_program",
    "vm_failure",
]

# Path search budget of a swap made by a contract: its paths are enumerated inside the open transaction, so the budget is far below the one of a user swap.
CONTRACT_SWAP_MAX_EXPANSIONS: Final = 500
# Largest chunk count of a swap or quote made by a contract; contracts pay route_search_gas() per chunk-hop, so fewer chunks keep one swap cheap at a small cost in allocation quality.
CONTRACT_SWAP_CHUNK_COUNT: Final = 10
DISCORD_MESSAGE_MAX_LENGTH: Final = 2000
HAS_ROLE_TIMEOUT_SECONDS: Final = 0.25
# Database time a whole top-level call may spend in host methods, which the wall-clock limit does not count.
HOST_TIME_SECONDS: Final = 10.0
# Top-level contract calls one event loop runs at a time; further calls wait for a slot before their transaction starts, so waiting holds no row lock or connection.
MAX_CONCURRENT_EXECUTIONS: Final = 4
# Dry runs one event loop runs at a time, in slots of their own, so free dry runs cannot keep paid calls waiting.
MAX_CONCURRENT_DRY_RUNS: Final = 2
PROGRAM_CACHE_SIZE: Final = 256

_ARG_TYPES: Mapping[type, str] = {int: "int", str: "str", bool: "bool", Account: "Account", Currency: "Currency"}


def _setting_default(name: str) -> Any:
    return Settings.model_fields[name].default


def contract_swap_config(chunk_count: int, max_routes: int) -> swap.SwapConfig:
    """The SwapConfig of in-contract swaps and quotes for the given settings: at most CONTRACT_SWAP_CHUNK_COUNT chunks and CONTRACT_SWAP_MAX_EXPANSIONS path expansions."""
    return swap.SwapConfig(chunk_count=min(chunk_count, CONTRACT_SWAP_CHUNK_COUNT), max_routes=max_routes, max_expansions=CONTRACT_SWAP_MAX_EXPANSIONS)


def _default_swap() -> swap.SwapConfig:
    return contract_swap_config(_setting_default("swap_chunk_count"), _setting_default("swap_max_routes"))


@dataclass(frozen=True)
class RuntimeConfig:
    """Settings of contract calls, defaulting to the Settings defaults: the gas currency and price, the default and largest gas limit a caller may give, the registration size limit (at most CONTRACT_SIZE_LIMIT), the VM limits, the SwapConfig of in-contract swaps, the time has_role() waits for GuildInfo, and how many top-level calls (execution_slot()) and dry runs (dry_run_slot()) run at once."""

    gas_currency_id: int = field(default_factory=lambda: _setting_default("gas_currency_id"))
    gas_price: int = field(default_factory=lambda: _setting_default("gas_price"))
    default_gas: int = field(default_factory=lambda: _setting_default("default_gas"))
    max_gas: int = field(default_factory=lambda: _setting_default("max_gas"))
    contract_max_size: int = field(default_factory=lambda: _setting_default("contract_max_size"))
    limits: Limits = DEFAULT_LIMITS
    swap: swap.SwapConfig = field(default_factory=_default_swap)
    has_role_timeout_seconds: float = HAS_ROLE_TIMEOUT_SECONDS
    host_time_seconds: float = HOST_TIME_SECONDS
    max_concurrent_executions: int = MAX_CONCURRENT_EXECUTIONS
    max_concurrent_dry_runs: int = MAX_CONCURRENT_DRY_RUNS

    def __post_init__(self) -> None:
        for name in ("gas_currency_id", "gas_price", "default_gas", "max_gas", "contract_max_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.gas_currency_id > BIGINT_MAX:
            raise ValueError("gas_currency_id must be a currency ID")
        if self.default_gas > self.max_gas:
            raise ValueError("default_gas must not exceed max_gas")
        if self.max_gas > MAX_GAS_LIMIT:
            raise ValueError(f"max_gas must not exceed {MAX_GAS_LIMIT}")
        if self.gas_price * self.max_gas > AMOUNT_MAX:
            raise ValueError("gas_price * max_gas must not exceed the largest amount")
        if self.contract_max_size > CONTRACT_SIZE_LIMIT:
            raise ValueError(f"contract_max_size must not exceed {CONTRACT_SIZE_LIMIT}")
        if not self.has_role_timeout_seconds > 0:
            raise ValueError("has_role_timeout_seconds must be positive")
        if not self.host_time_seconds > 0:
            raise ValueError("host_time_seconds must be positive")
        for name in ("max_concurrent_executions", "max_concurrent_dry_runs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeConfig:
        return cls(
            gas_currency_id=settings.gas_currency_id,
            gas_price=settings.gas_price,
            default_gas=settings.default_gas,
            max_gas=settings.max_gas,
            contract_max_size=settings.contract_max_size,
            swap=contract_swap_config(settings.swap_chunk_count, settings.swap_max_routes),
        )


_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], asyncio.Semaphore]] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _slot(pool: str, size: int) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    semaphores = _SLOTS.setdefault(loop, {})
    semaphore = semaphores.get((pool, size))
    if semaphore is None:
        semaphore = semaphores[pool, size] = asyncio.Semaphore(size)
    async with semaphore:
        yield


def execution_slot(config: RuntimeConfig) -> AbstractAsyncContextManager[None]:
    """Hold one of the config.max_concurrent_executions slots of the running event loop for the block, waiting for a free one first; top-level calls take a slot before their transaction starts, and nested calls run in their caller's slot."""
    return _slot("call", config.max_concurrent_executions)


def dry_run_slot(config: RuntimeConfig) -> AbstractAsyncContextManager[None]:
    """Hold one of the config.max_concurrent_dry_runs slots of the running event loop for the block, a pool separate from execution_slot(), so dry runs never wait for calls and never keep a call waiting."""
    return _slot("dry_run", config.max_concurrent_dry_runs)


@dataclass(frozen=True)
class RandomDraw:
    """The values one execution of a top-level call drew with random(): order numbers the executions of the call in the order they started (0 is the top-level one), and depth is 1 for the top-level one."""

    order: int
    depth: int
    contract_id: int
    function: str
    values: tuple[int, ...]

    def to_output(self) -> dict[str, Any]:
        return {"order": self.order, "depth": self.depth, "contract_id": self.contract_id, "function": self.function, "values": list(self.values)}


@dataclass(eq=False)
class HostFrame:
    """The ExecutionContext.host_data of one contract execution: its execution row, what it runs, its place in the call (order and depth), how many gas limits the meter held when it started, and the draws of the executions it called that have ended."""

    execution_id: int
    contract_id: int
    function: str
    depth: int
    order: int
    gas_levels: int
    nested_draws: list[RandomDraw] = field(default_factory=list)


def random_tree(frame: HostFrame, ctx: ExecutionContext) -> list[RandomDraw]:
    """Every draw of the execution of frame and of the executions it called, in start order, leaving out executions that drew nothing."""
    own = [RandomDraw(frame.order, frame.depth, frame.contract_id, frame.function, tuple(ctx.random_log))] if ctx.random_log else []
    return sorted([*own, *frame.nested_draws], key=lambda draw: draw.order)


def encode_args(args: Sequence[Scalar]) -> list[dict[str, Any]]:
    """The stored form of call arguments: one {"type", "value"} object per argument, with ints and the IDs of Account and Currency as decimal strings."""
    encoded: list[dict[str, Any]] = []
    for arg in args:
        name = _ARG_TYPES[type(arg)]
        value: Any = arg.id if isinstance(arg, Account | Currency) else arg
        encoded.append({"type": name, "value": value})
    return encoded


def encode_result(value: int | str | bool) -> dict[str, Any]:
    """The stored form of a public function's result: value plus value_type ("int", "str" or "bool"), so a str "5" and an int 5 stay distinct."""
    return {"value": value, "value_type": _ARG_TYPES[type(value)]}


def recorded_function_name(function: str) -> str:
    """The function name a nested call execution stores in input and contract_function: function itself when it is a function name of at most CONTRACT_FUNCTION_MAX characters, else function_label()'s quoted, escaped and bounded form."""
    return function_label(function, CONTRACT_FUNCTION_MAX)


def recorded_args(args: Sequence[object], failure: CallFailed | None = None) -> tuple[Scalar, ...]:
    """The arguments a nested call execution stores: none for an argument list the VM refused (a BAD_ARGUMENTS rejection, too long or holding a value that is not a scalar), else args."""
    if failure is not None and failure.reason is CallFailure.BAD_ARGUMENTS:
        return ()
    return tuple(args)  # type: ignore[arg-type]


def call_input(*, contract_id: int, function: str, args: Sequence[Scalar], attach: Attachment | None, gas_limit: int | None) -> dict[str, Any]:
    """The execution input of a contract call; for a nested call gas_limit is the sub-limit its gas= opened (min(gas, remaining)), or None without one."""
    return {
        "contract_id": contract_id,
        "function": function,
        "args": encode_args(args),
        "attach": None if attach is None else {"currency_id": attach.currency.id, "amount": attach.amount},
        "gas_limit": gas_limit,
    }


def validate_call_args(args: Sequence[object], limits: Limits = DEFAULT_LIMITS) -> tuple[Scalar, ...]:
    """Return args when every item is a VM scalar within limits and there are at most limits.max_params of them; raises InvalidArgument otherwise, before anything is recorded."""
    if isinstance(args, str | bytes) or not isinstance(args, Sequence):
        raise InvalidArgument("The arguments must be a list.")
    if len(args) > limits.max_params:
        raise InvalidArgument(f"A contract function takes at most {limits.max_params} arguments.")
    checked: list[Scalar] = []
    for position, arg in enumerate(args):
        if type(arg) not in SCALAR_TYPES or scalar_problem(arg, limits) is not None:
            raise InvalidArgument(f"Argument {position} must be an int, str, bool, Account or Currency within the contract limits.")
        checked.append(arg)  # type: ignore[arg-type]
    return tuple(checked)


def failure_fields(
    *,
    kind: str,
    gas_used: int,
    gas_limit: int | None,
    random_values: Sequence[int],
    draws: Sequence[RandomDraw] = (),
    failure: ContractFailure | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The fields a failed contract execution stores beside "error": the failure kind, the gas used and the limit, its own random values, random_tree with the draws of every execution in the failed subtree (whose own rows are rolled back), and where known the CALL_FAILED reason, the failed builtin's error code and the failing instruction."""
    fields: dict[str, Any] = {"kind": kind, "gas_used": gas_used, "gas_limit": gas_limit, "random": list(random_values), "random_tree": [draw.to_output() for draw in draws]}
    if isinstance(failure, CallFailed):
        reason = failure.reason.value
    if reason is not None:
        fields["reason"] = reason
    if isinstance(failure, ContractRuntimeError) and failure.host_code is not None:
        fields["host_code"] = failure.host_code
    if failure is not None and failure.function is not None:
        fields["location"] = {"function": failure.function, "offset": failure.offset}
    return fields


_ERROR_CLASSES: tuple[tuple[type[ContractFailure], type[ContractExecutionError]], ...] = (
    (ContractRevert, ContractReverted),
    (OutOfGas, ContractOutOfGas),
    (CallFailed, ContractCallFailed),
    (LimitExceeded, ContractLimitExceeded),
)


def contract_error(failure: ContractFailure) -> ContractExecutionError:
    """The DomainError recording failure on an execution, keeping failure for vm_failure(): ContractRevert -> ContractReverted (a RevertError, so status reverted), OutOfGas -> ContractOutOfGas, CallFailed -> ContractCallFailed, LimitExceeded -> ContractLimitExceeded, any other ContractFailure -> ContractRuntimeFailure.

    When failure came out of a failed child execution (vm_failure() raised it from that child's error), the child's description is carried over, so the parent's record names the child that failed it.
    """
    cls: type[ContractExecutionError] = ContractRuntimeFailure
    for failure_type, error_type in _ERROR_CLASSES:
        if isinstance(failure, failure_type):
            cls = error_type
            break
    error = cls(failure.message, failure=failure)
    cause = failure.__cause__
    if isinstance(cause, DomainError) and cause.child is not None:
        error.child = dict(cause.child)
    return error


def vm_failure(error: DomainError) -> ContractFailure:
    """The VM failure for the DomainError of a failed child execution, the inverse of contract_error(): the original ContractFailure (so the VM's try_call sees its kind and gas level), or a ContractRuntimeError for an error that carries none."""
    if isinstance(error, ContractExecutionError) and isinstance(error.failure, ContractFailure):
        return error.failure
    return ContractRuntimeError(error.message)


@functools.lru_cache(maxsize=PROGRAM_CACHE_SIZE)
def verified_program(binary: bytes, limits: Limits) -> VerifiedProgram:
    """verify() of a stored binary, cached per process; the size limit applied is the binary's own size, because a registered binary keeps running when the registration limit changes."""
    return verify(binary, max_size=max(len(binary), 1), limits=limits)


def runnable_program(locked: contracts.LockedContract | None, account_id: int, limits: Limits) -> VerifiedProgram:
    """The program of a locked contract that may run; raises CallFailed(NO_CONTRACT) for no or a deleted contract and CallFailed(INACTIVE) for a paused one or a binary the current limits reject."""
    if locked is None or locked.status is ContractStatus.DELETED or locked.binary is None:
        raise CallFailed(CallFailure.NO_CONTRACT, f"account {account_id} has no contract")
    if locked.status is not ContractStatus.ACTIVE:
        raise CallFailed(CallFailure.INACTIVE, f"the contract of account {account_id} is paused")
    try:
        return verified_program(locked.binary, limits)
    except VerificationError:
        raise CallFailed(CallFailure.INACTIVE, f"the contract of account {account_id} no longer passes verification") from None


def check_callable(program: VerifiedProgram, function: str, args: Sequence[object], limits: Limits) -> None:
    """Raise CallFailed(NO_FUNCTION) unless function is a public function of program, and CallFailed(BAD_ARGUMENTS) unless args fit its parameters; run before an attachment moves."""
    fn = program.public_function(function)
    if fn is None:
        raise CallFailed(CallFailure.NO_FUNCTION, f"no public function {function!r}")
    problem = check_args(fn.params, args, limits)
    if problem is not None:
        raise CallFailed(CallFailure.BAD_ARGUMENTS, f"{fn.name}: {problem}")


def _seconds(value: Any) -> int:
    return int(value.timestamp())


class LedgerHost:
    """The VM Host of one transaction: tx is the open LedgerTx, config the runtime settings, and guild_info the bot's view of Discord used only by has_role(); every ExecutionContext it serves carries a HostFrame as host_data.

    Builtins act as the executing contract (self) through the services' *_in_tx operations, so their transfers, storage writes and outbox rows belong to that contract's execution; Mint, burning others' funds, currency settings and permission changes have no method here.
    A domain failure of a builtin becomes HostError, a RUNTIME failure of the executing contract; RerunTransaction, CurrencyLockEscalation, database errors and every other exception propagate unchanged.
    Database work inside a host method runs with the VM's wall-clock deadline suspended, so waiting for a row lock is not a WALL_CLOCK failure; instead the whole top-level call may spend at most config.host_time_seconds in such work, checked as each piece ends, and passing it is an uncatchable LimitExceeded(HOST_TIME) like the wall-clock limit.
    The executing contract and every callee are locked FOR UPDATE when first locked, because storage writes update the counters on that row; read_storage() of another contract takes no lock, so that contract can still be called and locked later.
    """

    def __init__(self, tx: LedgerTx, config: RuntimeConfig, guild_info: GuildInfo | None, *, random_below: Callable[[int], int] = secrets.randbelow) -> None:
        self.tx = tx
        self.config = config
        self.guild_info = guild_info
        self._random_below = random_below
        self._orders = itertools.count(1)
        self.host_seconds = 0.0

    @contextmanager
    def _host_time(self, ctx: ExecutionContext) -> Iterator[None]:
        """Run the block with the deadline suspended and add its duration to host_seconds; past config.host_time_seconds raise LimitExceeded(HOST_TIME), also in place of a builtin or call failure the block raised, while server-side exceptions (reruns, database errors) propagate unchanged."""
        start = time.monotonic()
        try:
            with ctx.deadline.suspended():
                yield
        except (HostError, DomainError, ContractFailure):
            self._add_host_time(start)
            raise
        except BaseException:
            self.host_seconds += time.monotonic() - start
            raise
        self._add_host_time(start)

    def _add_host_time(self, start: float) -> None:
        self.host_seconds += time.monotonic() - start
        if self.host_seconds > self.config.host_time_seconds:
            raise LimitExceeded(LimitResource.HOST_TIME, f"host time limit of {self.config.host_time_seconds} seconds exceeded")

    @staticmethod
    def frame(ctx: ExecutionContext) -> HostFrame:
        frame = ctx.host_data
        if not isinstance(frame, HostFrame):
            raise LedgerUsageError("the execution context carries no HostFrame")
        return frame

    @classmethod
    def execution_id(cls, ctx: ExecutionContext) -> int:
        return cls.frame(ctx).execution_id

    async def _db[T](self, ctx: ExecutionContext, operation: Awaitable[T]) -> T:
        """Await database work under _host_time(), turning a DomainError into HostError."""
        with self._host_time(ctx):
            try:
                return await operation
            except DomainError as error:
                raise HostError(error.code, error.message) from None

    def random_below(self, n: int) -> int:
        return self._random_below(n)

    async def balance(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        return await self._db(ctx, queries.get_balance(self.tx.conn, account.id, currency.id))

    async def currency(self, ctx: ExecutionContext, symbol: str) -> Currency:
        info = await self._db(ctx, get_currency_by_symbol(self.tx.conn, symbol, now=self.tx.now))
        if info is None:
            raise HostError(CurrencyNotFound.code, f"No currency has the symbol {symbol[:40]!r}.")
        return Currency(info.currency_id)

    async def currency_info(self, ctx: ExecutionContext, currency: Currency) -> Record:
        info = await self._db(ctx, get_currency(self.tx.conn, currency.id, now=self.tx.now))
        if info is None:
            raise HostError(CurrencyNotFound.code, f"Currency {currency.id} was not found.")
        return make_record(
            Kind.CURRENCY_INFO,
            currency=Currency(info.currency_id),
            name=info.name,
            symbol=info.symbol,
            issuer=Account(info.created_by),
            supply=info.supply,
            hourly_rate_ppm=info.hourly_rate_ppm,
            minting_renounced=info.minting_renounced,
            delegation_enabled=info.delegation_enabled,
            delete_requested=info.delete_requested_at is not None,
            created_at=_seconds(info.created_at),
        )

    async def transfer(self, ctx: ExecutionContext, to: Account, currency: Currency, amount: int) -> None:
        operation = transfers.transfer_in_tx(self.tx, self.execution_id(ctx), sender_id=ctx.self_account.id, recipient_id=to.id, currency_id=currency.id, amount=amount)
        await self._db(ctx, operation)

    async def transfer_from(self, ctx: ExecutionContext, owner: Account, to: Account, currency: Currency, amount: int) -> None:
        operation = transfers.transfer_from_in_tx(self.tx, self.execution_id(ctx), spender_id=ctx.self_account.id, owner_id=owner.id, recipient_id=to.id, currency_id=currency.id, amount=amount)
        await self._db(ctx, operation)

    async def approve(self, ctx: ExecutionContext, spender: Account, currency: Currency, amount: int) -> None:
        await self._db(ctx, allowances.approve_in_tx(self.tx, self.execution_id(ctx), owner_id=ctx.self_account.id, spender_id=spender.id, currency_id=currency.id, amount=amount))

    async def allowance(self, ctx: ExecutionContext, owner: Account, spender: Account, currency: Currency) -> int:
        return await self._db(ctx, allowances.get_allowance(self.tx.conn, owner.id, spender.id, currency.id))

    async def create_claim(self, ctx: ExecutionContext, payer: Account, currency: Currency, amount: int, description: str) -> int:
        operation = claims.create_claim_in_tx(self.tx, self.execution_id(ctx), claimant_id=ctx.self_account.id, payer_id=payer.id, currency_id=currency.id, amount=amount, description=description)
        return (await self._db(ctx, operation)).claim_id

    async def pay_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        await self._db(ctx, claims.pay_claim_in_tx(self.tx, self.execution_id(ctx), claim_id=claim_id, payer_id=ctx.self_account.id))

    async def cancel_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        await self._db(ctx, claims.cancel_claim_in_tx(self.tx, self.execution_id(ctx), claim_id=claim_id, user_id=ctx.self_account.id))

    async def claim_info(self, ctx: ExecutionContext, claim_id: int) -> Record:
        found = await self._db(ctx, claims.get_claim(self.tx.conn, claim_id))
        return make_record(
            Kind.CLAIM_INFO,
            claim_id=found.claim_id,
            claimant=Account(found.claimant_id),
            payer=Account(found.payer_id),
            currency=Currency(found.currency_id),
            amount=found.amount,
            description=found.description or "",
            status=found.status.value,
            created_at=_seconds(found.created_at),
        )

    async def swap(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int, min_out: int) -> int:
        """Plan the paths, charge route_search_gas() for the search and the allocation over the planned paths, then swap along that plan; running out of gas fails before anything moves, and a swap that then fails (no route, slippage, balance) has still paid the route gas."""
        plan = await self._db(ctx, swap.plan_swap(self.tx.conn, from_currency=from_currency.id, to_currency=to_currency.id, amount_in=amount_in, config=self.config.swap))
        ctx.gas.charge(route_search_gas(plan.expansions, swap.allocation_chunk_hops(plan.paths, amount_in, self.config.swap.chunk_count)))
        operation = swap.swap_in_tx(
            self.tx,
            self.execution_id(ctx),
            user_id=ctx.self_account.id,
            from_currency=from_currency.id,
            to_currency=to_currency.id,
            amount_in=amount_in,
            min_out=min_out,
            config=self.config.swap,
            plan=plan,
        )
        return (await self._db(ctx, operation)).amount_out

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
    ) -> int:
        operation = pools.add_liquidity_in_tx(
            self.tx,
            self.execution_id(ctx),
            user_id=ctx.self_account.id,
            currency_x=currency_x.id,
            currency_y=currency_y.id,
            fee_bps=fee_bps,
            amount_x_desired=amount_x,
            amount_y_desired=amount_y,
            amount_x_min=amount_x_min,
            amount_y_min=amount_y_min,
            min_shares=min_shares,
        )
        return (await self._db(ctx, operation)).shares

    async def remove_liquidity(self, ctx: ExecutionContext, currency_x: Currency, currency_y: Currency, fee_bps: int, shares: int, amount_x_min: int, amount_y_min: int) -> list[int]:
        operation = pools.remove_liquidity_in_tx(
            self.tx,
            self.execution_id(ctx),
            user_id=ctx.self_account.id,
            currency_x=currency_x.id,
            currency_y=currency_y.id,
            fee_bps=fee_bps,
            shares=shares,
            amount_x_min=amount_x_min,
            amount_y_min=amount_y_min,
        )
        removed = await self._db(ctx, operation)
        return [removed.amount_x, removed.amount_y]

    async def quote(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int) -> Record:
        """Quote on the current reserves and charge route_search_gas() for the search and allocation it simulated, also when they found no route (the NoRoute failure then surfaces after the charge)."""
        searched: list[tuple[int, int]] = []

        def record(expansions: int, chunk_hops: int) -> None:
            searched.append((expansions, chunk_hops))

        try:
            found = await self._db(ctx, swap.quote_swap(self.tx.conn, from_currency=from_currency.id, to_currency=to_currency.id, amount_in=amount_in, config=self.config.swap, on_search=record))
        finally:
            if searched:
                ctx.gas.charge(route_search_gas(*searched[-1]))
        return make_record(
            Kind.QUOTE,
            from_currency=Currency(found.from_currency),
            to_currency=Currency(found.to_currency),
            amount_in=found.amount_in,
            amount_out=found.amount_out,
            route_count=len(found.routes),
            truncated=found.truncated,
        )

    async def stake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        await self._db(ctx, staking.deposit_in_tx(self.tx, self.execution_id(ctx), account_id=ctx.self_account.id, currency_id=currency.id, amount=amount))

    async def unstake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        await self._db(ctx, staking.withdraw_in_tx(self.tx, self.execution_id(ctx), account_id=ctx.self_account.id, currency_id=currency.id, amount=amount))

    async def staked(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        """The stake of account in currency as an accrual at the execution time would make it, 0 when there is none."""
        info = await self._db(ctx, staking.get_stake(self.tx.conn, account.id, currency.id))
        if info is None:
            return 0
        supply = await self._db(ctx, queries.currency_supply(self.tx.conn, currency.id))
        return staking.projected_amount(info, self.tx.now, supply=supply)

    async def _stored(self, ctx: ExecutionContext, account_id: int, key: str) -> bytes | None:
        stmt = select(contract_storage.c.encoded_value).where(contract_storage.c.account_id == account_id, contract_storage.c.storage_key == key)
        with self._host_time(ctx):
            data = (await self.tx.conn.execute(stmt)).scalar_one_or_none()
        return None if data is None else bytes(data)

    async def storage_get(self, ctx: ExecutionContext, key: str) -> bytes | None:
        """The encoded entry of self's storage; the VM decodes it and charges its read gas."""
        return await self._stored(ctx, ctx.self_account.id, key)

    async def storage_set(self, ctx: ExecutionContext, key: str, data: bytes) -> None:
        """Store bytes the VM encoded and already charged write gas for (by encoded size and value count)."""
        await self._db(ctx, contracts.put_storage_in_tx(self.tx, ctx.self_account.id, key, data))

    async def storage_delete(self, ctx: ExecutionContext, key: str) -> None:
        await self._db(ctx, contracts.delete_storage_in_tx(self.tx, ctx.self_account.id, key))

    async def read_storage(self, ctx: ExecutionContext, account: Account, key: str) -> bytes | None:
        """Any contract's entry (storage is public), read without locking that contract's row."""
        return await self._stored(ctx, account.id, key)

    @staticmethod
    def _require_id(name: str, value: int) -> None:
        if not 1 <= value <= BIGINT_MAX:
            raise HostError(InvalidArgument.code, f"The {name} ID must be an integer between 1 and {BIGINT_MAX}.")

    async def _require_discord(self, ctx: ExecutionContext, guild: int) -> None:
        """Raise HostError unless guild is a Discord ID and its administrators allowed the executing contract there."""
        self._require_id("server", guild)
        if not await self._db(ctx, is_discord_allowed(self.tx.conn, guild, ctx.self_account.id)):
            raise HostError(PermissionDenied.code, f"This contract is not allowed to perform Discord operations in server {guild}.")

    def _require_user(self, user: Account) -> None:
        if not is_user_account_id(user.id):
            raise HostError(InvalidArgument.code, "The user must be a user account.")

    async def _enqueue(self, ctx: ExecutionContext, kind: OutboxKind, payload: Mapping[str, Any]) -> None:
        await self._db(ctx, enqueue(self.tx, kind=kind, payload=payload, execution_id=self.execution_id(ctx)))

    async def discord_send(self, ctx: ExecutionContext, guild: int, channel: int, message: str) -> None:
        """Queue a message for the worker to send after commit; IDs outside the Discord range, a message of 0 or more than DISCORD_MESSAGE_MAX_LENGTH characters and a missing discord_permission are RUNTIME failures."""
        self._require_id("channel", channel)
        if not 1 <= len(message) <= DISCORD_MESSAGE_MAX_LENGTH:
            raise HostError(InvalidArgument.code, f"A Discord message must have 1 to {DISCORD_MESSAGE_MAX_LENGTH} characters.")
        await self._require_discord(ctx, guild)
        await self._enqueue(ctx, OutboxKind.DISCORD_SEND, {"contract_id": ctx.self_account.id, "guild_id": guild, "channel_id": channel, "message": message})

    async def _role_change(self, ctx: ExecutionContext, kind: OutboxKind, guild: int, user: Account, role: int) -> None:
        self._require_user(user)
        self._require_id("role", role)
        await self._require_discord(ctx, guild)
        await self._enqueue(ctx, kind, {"contract_id": ctx.self_account.id, "guild_id": guild, "user_id": user.id, "role_id": role})

    async def discord_add_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        await self._role_change(ctx, OutboxKind.DISCORD_ADD_ROLE, guild, user, role)

    async def discord_remove_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        await self._role_change(ctx, OutboxKind.DISCORD_REMOVE_ROLE, guild, user, role)

    async def discord_has_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> bool:
        """Ask GuildInfo whether the member has the role, waiting at most config.has_role_timeout_seconds.

        This is the one GuildInfo call made inside a transaction, so the provider must answer from the bot's member cache without network requests.
        IDs outside the Discord range and a missing discord_permission for the executing contract are RUNTIME failures; for an allowed contract and valid IDs, an unknown guild, member or role, a missing provider, a timeout and a provider error all answer False.
        """
        self._require_user(user)
        self._require_id("role", role)
        await self._require_discord(ctx, guild)
        if self.guild_info is None:
            return False
        try:
            answer = await asyncio.wait_for(self.guild_info.has_role(guild, user.id, role), timeout=self.config.has_role_timeout_seconds)
        except Exception:
            return False
        return answer is True

    async def call_contract(self, ctx: ExecutionContext, target: Account, function: str, args: list[Scalar], attach: Attachment | None) -> int | str | bool:
        """Run target's function as a child execution of the calling contract's execution (run_child(), so a failed callee rolls back everything it and its own calls did and keeps only its failed or reverted row), initiated by the calling contract's account.

        Every failure is recorded on the child execution with the gas it used and the draws of its subtree, and raised as the VM failure (CallFailed for a missing or paused contract, a missing function, bad arguments, an attachment in a missing currency or one the caller cannot pay).
        The draws of the child's subtree are handed to the calling execution as well, so they stay recorded if that execution fails too.
        """
        return await self._child_call(ctx, target, function, args, attach, None)

    async def reject_call(self, ctx: ExecutionContext, target: Account, function: str, args: list[Any], attach: Attachment | None, failure: CallFailed) -> NoReturn:
        """Record a call the VM refused as a failed CALL_FAILED child execution that locked nothing, used no gas and drew nothing, storing recorded_args() and recorded_function_name(), and raise failure chained from the child's error."""
        await self._child_call(ctx, target, function, recorded_args(args, failure), attach, failure)
        raise AssertionError("a rejected call cannot succeed")

    async def _child_call(self, ctx: ExecutionContext, target: Account, function: str, args: Sequence[Scalar], attach: Attachment | None, rejected: CallFailed | None) -> int | str | bool:
        parent = self.frame(ctx)
        limits = self.config.limits
        start = ctx.gas.used
        sub_limit = ctx.gas.remaining if ctx.gas.level_count > parent.gas_levels else None
        order = next(self._orders)
        outcomes: list[ExecutionOutcome] = []
        frames: list[HostFrame] = []
        name = recorded_function_name(function)

        async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
            frame = HostFrame(execution_id=execution_id, contract_id=target.id, function=name, depth=ctx.depth, order=order, gas_levels=ctx.gas.level_count)
            frames.append(frame)
            child = replace(ctx, host_data=frame)

            def fields(kind: str, failure: ContractFailure | None = None) -> dict[str, Any]:
                return failure_fields(kind=kind, gas_used=ctx.gas.used - start, gas_limit=sub_limit, random_values=child.random_log, draws=random_tree(frame, child), failure=failure)

            try:
                if rejected is not None:
                    raise rejected
                with self._host_time(ctx):
                    locked = await contracts.lock_contract_in_tx(tx, target.id, for_update=True)
                program = runnable_program(locked, target.id, limits)
                check_callable(program, function, args, limits)
                if attach is not None:
                    with self._host_time(ctx):
                        await self._move_attachment(tx, execution_id, ctx.sender.id, target.id, attach)
                outcome = await execute(program, function, args, host=self, context=child)
            except ContractFailure as failure:
                error = contract_error(failure)
                set_failure_output(error, fields(failure.code, failure))
                raise error from None
            except DomainError as error:
                set_failure_output(error, fields("RUNTIME"))
                raise
            outcomes.append(outcome)
            return {**encode_result(outcome.value), "gas_used": outcome.gas_used, "gas_limit": sub_limit, "random": list(outcome.random_values)}

        try:
            await run_child(
                self.tx,
                parent_execution_id=parent.execution_id,
                type=ExecutionType.CONTRACT_CALL,
                initiator_id=ctx.sender.id,
                input=call_input(contract_id=target.id, function=name, args=args, attach=attach, gas_limit=sub_limit),
                body=body,
                contract_function=name,
            )
        except DomainError as error:
            raise vm_failure(error) from error
        finally:
            if frames:
                parent.nested_draws.extend(random_tree(frames[-1], ctx))
        return outcomes[-1].value

    @staticmethod
    async def _move_attachment(tx: LedgerTx, execution_id: int, payer_id: int, contract_id: int, attach: Attachment) -> None:
        """Move a nested call's attachment from the calling contract to the callee as a principal transfer of the child execution; a missing currency is CallFailed(NO_CURRENCY) and any other failure CallFailed(INSUFFICIENT_BALANCE)."""
        currency_id = attach.currency.id
        try:
            await tx.lock_currencies([currency_id])
        except CurrencyNotFound:
            raise CallFailed(CallFailure.NO_CURRENCY, f"the attachment's currency {currency_id} was not found") from None
        try:
            await tx.lock_balances([(payer_id, currency_id), (contract_id, currency_id)])
            await tx.move(execution_id, currency_id, payer_id, contract_id, attach.amount, TransferKind.PRINCIPAL)
        except DomainError as error:
            raise CallFailed(CallFailure.INSUFFICIENT_BALANCE, f"the attachment cannot be paid: {error.message}") from None
