"""Top-level contract calls: call_contract() runs a public function of a contract as one contract_call execution whose gas is prepaid, settled and burned in the gas currency, and dry_run_contract() runs the same operation and rolls it back."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.contract.errors import CallFailure, ContractFailure
from rapidwire.contract.runtime import (
    HostFrame,
    LedgerHost,
    RandomDraw,
    RuntimeConfig,
    call_input,
    check_callable,
    contract_error,
    dry_run_slot,
    encode_result,
    execution_slot,
    failure_fields,
    random_tree,
    runnable_program,
    validate_call_args,
)
from rapidwire.contract.values import Account, Attachment, Currency, Scalar
from rapidwire.contract.verifier import VerifiedProgram
from rapidwire.contract.vm import ExecutionContext, execute
from rapidwire.core import contracts
from rapidwire.core.errors import ContractNotFound, ContractPaused, CurrencyNotFound, DomainError, InsufficientBalance, InvalidArgument, InvalidContract
from rapidwire.core.ledger import LedgerTx, OperationResult, failure_output, run_operation, set_failure_output
from rapidwire.core.permissions import GuildInfo
from rapidwire.core.types import ExecutionType, JsonObject, TransferKind
from rapidwire.core.validation import require_int_in_range, require_user_account, validate_positive_amount, validate_positive_id, validate_user_account_id
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX, ContractStatus, execution, outbox, transfer
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "ContractCallResult",
    "DryRunError",
    "DryRunOutbox",
    "DryRunResult",
    "DryRunTransfer",
    "call_contract",
    "dry_run_contract",
]


def _int(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError(f"stored contract call field {value!r} is not an integer string")
    return int(value)


def _decode_result(output: JsonObject) -> int | str | bool:
    value, value_type = output.get("value"), output.get("value_type")
    if value_type == "int":
        return _int(value)
    if value_type == "str" and isinstance(value, str):
        return value
    if value_type == "bool" and isinstance(value, bool):
        return value
    raise ValueError(f"stored contract result {value!r} of type {value_type!r} is malformed")


@dataclass(frozen=True)
class ContractCallResult:
    """A successful top-level call: the function's value, the gas used out of gas_limit, the gas currency and price it was paid in as recorded by the call (so a replay reports the original cost), and the random values the top-level execution itself drew (nested calls keep theirs on their own rows); replayed takes no part in equality."""

    execution_id: int
    contract_id: int
    function: str
    value: int | str | bool
    gas_used: int
    gas_limit: int
    gas_currency_id: int
    gas_price: int
    random_values: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    @property
    def gas_cost(self) -> int:
        """The amount of the gas currency burned: gas_used * gas_price."""
        return self.gas_used * self.gas_price

    @classmethod
    def from_operation(cls, op: OperationResult, *, contract_id: int, function: str) -> ContractCallResult:
        random = op.output.get("random")
        return cls(
            execution_id=op.execution_id,
            contract_id=contract_id,
            function=function,
            value=_decode_result(op.output),
            gas_used=op.int_field("gas_used"),
            gas_limit=op.int_field("gas_limit"),
            gas_currency_id=op.int_field("gas_currency_id"),
            gas_price=op.int_field("gas_price"),
            random_values=tuple(_int(item) for item in random) if isinstance(random, list) else (),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class DryRunError:
    """Why a dry run failed: the DomainError code and message the real call would record, and the failure kind (REVERTED, RUNTIME, OUT_OF_GAS, CALL_FAILED, MEMORY_LIMIT, WALL_CLOCK_LIMIT, HOST_TIME_LIMIT or SETTLEMENT) with the CALL_FAILED reason when there is one."""

    code: str
    message: str
    kind: str | None
    reason: str | None


@dataclass(frozen=True)
class DryRunTransfer:
    """A transfer the call would make; execution_id is the would-be execution (the call's own or one of its nested calls) and is never committed."""

    execution_id: int
    currency_id: int
    from_account: int
    to_account: int
    amount: int
    kind: str


@dataclass(frozen=True)
class DryRunOutbox:
    """A Discord operation the call would queue."""

    execution_id: int
    kind: str
    payload: JsonObject


@dataclass(frozen=True)
class DryRunResult:
    """The outcome of a dry run: the value on success or the error, the gas the real call would use and limit with the gas currency and price it ran under, the top-level execution's own random values and the draws of every execution of the call, and the transfers (the gas transfer included) and outbox rows of the call and its nested calls."""

    value: int | str | bool | None
    error: DryRunError | None
    gas_used: int
    gas_limit: int
    gas_currency_id: int
    gas_price: int
    random_values: tuple[int, ...]
    random_tree: tuple[RandomDraw, ...]
    transfers: tuple[DryRunTransfer, ...]
    outbox: tuple[DryRunOutbox, ...]

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def gas_cost(self) -> int:
        """The amount of the gas currency the real call would burn: gas_used * gas_price."""
        return self.gas_used * self.gas_price


def _with_fields(error: DomainError, fields: dict[str, Any]) -> DomainError:
    set_failure_output(error, fields)
    return error


class _TopLevelCall:
    """prepare, body and on_failure of one top-level call, passed to run_operation(); prepare resets the state each transaction attempt builds.

    Locking: prepare locks the gas currency and the attachment's currency FOR SHARE in one ascending call, then the contract row FOR UPDATE for the whole call (calls of one contract run one at a time, and storage writes never upgrade a shared lock), then the caller's gas balance and both attachment balances in one call.
    The gas currency's issuance balance is locked only to settle: by the body right before the gas transfer, or by on_failure, so no call holds that shared row while its contract runs.
    Settling thus locks the issuance balance after the caller's, the reverse of the ascending order in which operations that touch the gas currency's issuance account (burn, stake and unstake with their interest, mint) lock it before the caller's balance; one of those racing a call of the same account can deadlock with the settlement, and InnoDB aborts one transaction, which run_operation() runs again.
    Gas: prepare checks that the caller holds limit * gas_price of the gas currency, plus the attachment when it is in that currency; the body moves the attachment (kind principal), runs the VM and moves used * gas_price from the caller to the issuance account (kind gas, a burn), making no gas transfer when no gas was used.
    A failed body's own transfers roll back with its savepoint, and on_failure collects the gas used (the whole limit when the top-level limit ran out) as the failed execution's only transfer; settlement the caller can no longer pay (the contract took the caller's gas currency through transfer_from) fails the body and is collected from the restored balance.
    A call that cannot start (missing, paused or deleted contract, unknown function, bad arguments, unaffordable gas limit or attachment, missing attachment currency) is a recorded failure charging no gas.
    """

    def __init__(
        self,
        guild_info: GuildInfo | None,
        config: RuntimeConfig,
        *,
        caller_id: int,
        contract_id: int,
        function: str,
        args: tuple[Scalar, ...],
        attach: Attachment | None,
        gas_limit: int,
    ) -> None:
        self.guild_info = guild_info
        self.config = config
        self.caller_id = caller_id
        self.contract_id = contract_id
        self.function = function
        self.args = args
        self.attach = attach
        self.gas_limit = gas_limit
        self.input = call_input(contract_id=contract_id, function=function, args=args, attach=attach, gas_limit=gas_limit)
        self._reset()

    def _reset(self) -> None:
        self.pending: DomainError | None = None
        self.program: VerifiedProgram | None = None
        self.issuance_id: int | None = None
        self.gas_used = 0
        self.random_values: tuple[int, ...] = ()
        self.draws: tuple[RandomDraw, ...] = ()
        self.value: int | str | bool | None = None

    def _gas_terms(self) -> dict[str, Any]:
        """The gas currency and price this call is charged in, recorded on its output whether it succeeds or fails."""
        return {"gas_currency_id": self.config.gas_currency_id, "gas_price": self.config.gas_price}

    def _fields(self, kind: str, *, failure: ContractFailure | None = None, reason: str | None = None) -> dict[str, Any]:
        fields = failure_fields(kind=kind, gas_used=self.gas_used, gas_limit=self.gas_limit, random_values=self.random_values, draws=self.draws, failure=failure, reason=reason)
        return {**fields, **self._gas_terms()}

    def _not_called(self, error: DomainError, reason: CallFailure) -> DomainError:
        return _with_fields(error, self._fields("CALL_FAILED", reason=reason.value))

    async def prepare(self, tx: LedgerTx, execution_id: int) -> None:
        """Lock in ledger order and decide whether the call may start; a domain failure is kept for the body to raise, so it is recorded instead of rolling the execution back, and a call of an account without a live contract locks no balance at all."""
        self._reset()
        gas_currency = self.config.gas_currency_id
        currency_ids = {gas_currency} | ({self.attach.currency.id} if self.attach is not None else set())
        try:
            rows = await tx.lock_currencies(currency_ids)
        except CurrencyNotFound as error:
            self.pending = self._not_called(error, CallFailure.NO_CURRENCY)
            return
        locked = await contracts.lock_contract_in_tx(tx, self.contract_id, for_update=True)
        if locked is None or locked.status is ContractStatus.DELETED:
            self.pending = self._not_called(ContractNotFound(), CallFailure.NO_CONTRACT)
            return
        if locked.status is not ContractStatus.ACTIVE:
            self.pending = self._not_called(ContractPaused(), CallFailure.INACTIVE)
            return
        try:
            self.program = runnable_program(locked, self.contract_id, self.config.limits)
        except ContractFailure as failure:
            self.pending = self._not_called(InvalidContract(failure.message), CallFailure.INACTIVE)
            return
        self.issuance_id = rows[gas_currency]["issuance_account_id"]
        keys = {(self.caller_id, gas_currency)}
        if self.attach is not None:
            keys |= {(self.caller_id, self.attach.currency.id), (self.contract_id, self.attach.currency.id)}
        await tx.lock_balances(keys)
        needed = self.gas_limit * self.config.gas_price
        if self.attach is not None and self.attach.currency.id == gas_currency:
            needed += self.attach.amount
        held = tx.balance(self.caller_id, gas_currency)
        if held < needed:
            message = f"Calling with a gas limit of {self.gas_limit} needs {needed} of currency {gas_currency}, but you hold {held}."
            self.pending = self._not_called(InsufficientBalance(message), CallFailure.INSUFFICIENT_BALANCE)

    async def _collect(self, tx: LedgerTx, execution_id: int) -> None:
        cost = self.gas_used * self.config.gas_price
        if cost and self.issuance_id is not None:
            gas_currency = self.config.gas_currency_id
            await tx.lock_balances([(self.issuance_id, gas_currency)])
            await tx.move(execution_id, gas_currency, self.caller_id, self.issuance_id, cost, TransferKind.GAS)

    async def body(self, tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        if self.pending is not None:
            raise self.pending
        program = self.program
        if program is None or self.issuance_id is None:
            raise RuntimeError("prepare did not run before the body")
        limits = self.config.limits
        try:
            check_callable(program, self.function, self.args, limits)
        except ContractFailure as failure:
            raise _with_fields(contract_error(failure), self._fields(failure.code, failure=failure)) from None
        if self.attach is not None:
            try:
                await tx.move(execution_id, self.attach.currency.id, self.caller_id, self.contract_id, self.attach.amount, TransferKind.PRINCIPAL)
            except DomainError as error:
                raise self._not_called(error, CallFailure.INSUFFICIENT_BALANCE) from None
        frame = HostFrame(execution_id=execution_id, contract_id=self.contract_id, function=self.function, depth=1, order=0, gas_levels=1)
        ctx = ExecutionContext.top_level(
            self_account=Account(self.contract_id),
            sender=Account(self.caller_id),
            paid=self.attach,
            now=int(tx.now.timestamp()),
            gas_limit=self.gas_limit,
            limits=limits,
            host_data=frame,
        )
        host = LedgerHost(tx, self.config, self.guild_info)
        try:
            outcome = await execute(program, self.function, self.args, host=host, context=ctx)
        except ContractFailure as failure:
            self._record(ctx, frame)
            raise _with_fields(contract_error(failure), self._fields(failure.code, failure=failure)) from None
        except DomainError as error:
            self._record(ctx, frame)
            raise _with_fields(error, self._fields("RUNTIME")) from None
        self._record(ctx, frame)
        self.value = outcome.value
        try:
            await self._collect(tx, execution_id)
        except DomainError as error:
            raise _with_fields(error, self._fields("SETTLEMENT")) from None
        return {**encode_result(outcome.value), "gas_used": outcome.gas_used, "gas_limit": self.gas_limit, **self._gas_terms(), "random": list(outcome.random_values)}

    def _record(self, ctx: ExecutionContext, frame: HostFrame) -> None:
        self.gas_used, self.random_values, self.draws = ctx.gas.used, tuple(ctx.random_log), tuple(random_tree(frame, ctx))

    async def on_failure(self, tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        """Collect the gas a failed call used from the caller's balance, restored by the body's rollback and covered by the prepay check, locking the issuance balance now."""
        await self._collect(tx, execution_id)


def _prevalidate(
    guild_info: GuildInfo | None,
    config: RuntimeConfig,
    *,
    caller_id: int,
    contract_id: int,
    function: str,
    args: Sequence[object],
    attach: Attachment | None,
    gas_limit: int | None,
) -> _TopLevelCall:
    """Check what must be storable before anything is recorded: account IDs, a function name that fits the record, VM scalar arguments, a positive attachment in a currency ID, and a gas limit of 1 to max_gas; raises InvalidArgument, InvalidAmount or AmountOverflow."""
    validate_user_account_id(caller_id)
    require_user_account(contract_id, "contract")
    if not isinstance(function, str) or not 1 <= len(function) <= CONTRACT_FUNCTION_MAX:
        raise InvalidArgument(f"The function name must be 1 to {CONTRACT_FUNCTION_MAX} characters.")
    checked = validate_call_args(args, config.limits)
    if attach is not None:
        if not isinstance(attach, Attachment) or not isinstance(attach.currency, Currency):
            raise InvalidArgument("The attachment must name a currency and an amount.")
        validate_positive_id("currency_id", attach.currency.id)
        validate_positive_amount(attach.amount)
        if caller_id == contract_id:
            raise InvalidArgument("You cannot attach currency to a call of your own contract.")
    limit = config.default_gas if gas_limit is None else require_int_in_range("gas_limit", gas_limit, 1, config.max_gas)
    if limit * config.gas_price > AMOUNT_MAX:
        raise InvalidArgument("The gas limit costs more than the largest amount.")
    return _TopLevelCall(guild_info, config, caller_id=caller_id, contract_id=contract_id, function=function, args=checked, attach=attach, gas_limit=limit)


async def call_contract(
    engine: AsyncEngine,
    guild_info: GuildInfo | None,
    *,
    caller_id: int,
    contract_id: int,
    function: str,
    args: Sequence[object] = (),
    attach: Attachment | None = None,
    gas_limit: int | None = None,
    config: RuntimeConfig,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> ContractCallResult:
    """Call contract_id's public function with args as a contract_call execution initiated by caller_id and return its value; guild_info answers discord.has_role().

    Malformed arguments raise before anything is recorded (see _prevalidate()); every other failure is a recorded failed (or reverted) execution whose output holds the error, the failure kind, the gas used and limit, the gas currency and price, and the draws of every execution of the call, and is raised with execution_id set; gas, locking and settlement are described on _TopLevelCall.
    The call waits for one of the process's execution slots (execution_slot()) before its transaction starts.
    """
    call = _prevalidate(guild_info, config, caller_id=caller_id, contract_id=contract_id, function=function, args=args, attach=attach, gas_limit=gas_limit)
    async with execution_slot(config):
        op = await run_operation(
            engine,
            type=ExecutionType.CONTRACT_CALL,
            initiator_id=caller_id,
            input=call.input,
            body=call.body,
            prepare=call.prepare,
            on_failure=call.on_failure,
            contract_function=function,
            late_currency_locks=True,
            now=now,
            idempotency_key=idempotency_key,
        )
    return ContractCallResult.from_operation(op, contract_id=contract_id, function=function)


async def _descendants(conn: AsyncConnection, execution_id: int) -> list[int]:
    ids, frontier = [execution_id], [execution_id]
    while frontier:
        frontier = list((await conn.execute(select(execution.c.execution_id).where(execution.c.parent_execution_id.in_(frontier)))).scalars())
        ids.extend(frontier)
    return ids


async def _effects(conn: AsyncConnection, execution_id: int) -> tuple[tuple[DryRunTransfer, ...], tuple[DryRunOutbox, ...]]:
    ids = await _descendants(conn, execution_id)
    transfer_rows = (await conn.execute(select(transfer).where(transfer.c.execution_id.in_(ids)).order_by(transfer.c.transfer_id))).mappings().all()
    outbox_rows = (await conn.execute(select(outbox).where(outbox.c.execution_id.in_(ids)).order_by(outbox.c.outbox_id))).mappings().all()
    transfers = tuple(
        DryRunTransfer(execution_id=row["execution_id"], currency_id=row["currency_id"], from_account=row["from_account"], to_account=row["to_account"], amount=int(row["amount"]), kind=row["kind"])
        for row in transfer_rows
    )
    queued = tuple(DryRunOutbox(execution_id=row["execution_id"], kind=row["kind"], payload=row["payload"]) for row in outbox_rows)
    return transfers, queued


async def dry_run_contract(
    engine: AsyncEngine,
    guild_info: GuildInfo | None,
    *,
    caller_id: int,
    contract_id: int,
    function: str,
    args: Sequence[object] = (),
    attach: Attachment | None = None,
    gas_limit: int | None = None,
    config: RuntimeConfig,
    now: datetime | None = None,
) -> DryRunResult:
    """Run the call exactly as call_contract() does, through run_operation() with rollback_inspect, so nothing it writes (executions, transfers, storage, outbox rows, new accounts) is ever committed, and report its outcome.

    Malformed arguments raise as in call_contract(); retries and reruns behave as for a real call, and an error raised by prepare or on_failure propagates as it would from call_contract().
    The dry run waits for one of the process's dry-run slots (dry_run_slot()), which are separate from the slots of real calls, before its transaction starts.
    """
    call = _prevalidate(guild_info, config, caller_id=caller_id, contract_id=contract_id, function=function, args=args, attach=attach, gas_limit=gas_limit)
    effects: list[tuple[tuple[DryRunTransfer, ...], tuple[DryRunOutbox, ...]]] = []

    async def inspect(tx: LedgerTx, execution_id: int) -> None:
        effects.append(await _effects(tx.conn, execution_id))

    failed: DomainError | None = None
    try:
        async with dry_run_slot(config):
            await run_operation(
                engine,
                type=ExecutionType.CONTRACT_CALL,
                initiator_id=caller_id,
                input=call.input,
                body=call.body,
                prepare=call.prepare,
                on_failure=call.on_failure,
                contract_function=function,
                late_currency_locks=True,
                now=now,
                rollback_inspect=inspect,
            )
    except DomainError as error:
        if not effects:
            # The failure came from prepare or on_failure, so the operation never reached rollback_inspect and has no outcome to report.
            raise
        failed = error
    reported: DryRunError | None = None
    if failed is not None:
        fields = failure_output(failed)
        kind, reason = fields.get("kind"), fields.get("reason")
        reported = DryRunError(code=failed.code, message=failed.message, kind=kind if isinstance(kind, str) else None, reason=reason if isinstance(reason, str) else None)
    transfers, queued = effects[-1]
    return DryRunResult(
        value=call.value if failed is None else None,
        error=reported,
        gas_used=call.gas_used,
        gas_limit=call.gas_limit,
        gas_currency_id=config.gas_currency_id,
        gas_price=config.gas_price,
        random_values=call.random_values,
        random_tree=call.draws,
        transfers=transfers,
        outbox=queued,
    )
