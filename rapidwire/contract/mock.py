"""An in-memory ledger that implements the VM Host, for local runs (rwc run) and tests.

It keeps balances, currencies, allowances, claims, constant-product pools (routed with the server's swap planner), stakes, contract storage in the canonical storage encoding, Discord role data and a set of registered contracts, and applies the server's rules and error codes to each builtin.
Every state change a run makes is logged with its inverse, and a failing top-level run or nested call undoes the changes logged since it started, so a failed run leaves nothing behind and a failed try_call undoes only what the callee did, its attachment included; the cost of a call does not grow with the run's history.
Discord send/add_role/remove_role are queued in an outbox that a run returns only when it succeeds; has_role answers from the configured roles.
As on the server, a contract may perform Discord operations only in guilds where discord_permissions holds (guild, contract), the grant a guild administrator makes with /discord-permission allow.
Gas is metered by the VM itself, so the reported gas equals the server's; a top-level run checks the gas cap and the prepayment and burns the fee from the sender as the server does, and stakes accrue no interest.
"""

from __future__ import annotations

import random
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

from rapidwire.config import Settings
from rapidwire.contract import storage_codec
from rapidwire.contract.compiler import compile_source
from rapidwire.contract.costs import route_search_gas
from rapidwire.contract.errors import CallFailed, CallFailure, ContractFailure, HostError
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.runtime import DISCORD_MESSAGE_MAX_LENGTH, contract_swap_config, recorded_args, recorded_function_name
from rapidwire.contract.types import KIND_NAMES, RECORD_FIELDS, Kind
from rapidwire.contract.values import KEY_TYPES, Account, Attachment, Currency, Record, Scalar, Value, make_record
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.contract.vm import ExecutionContext, check_args, execute
from rapidwire.core.claims import normalize_description
from rapidwire.core.errors import (
    AmountOverflow,
    ClaimNotFound,
    ContractStorageLimit,
    CurrencyNotFound,
    DomainError,
    InsufficientAllowance,
    InsufficientBalance,
    InsufficientLiquidity,
    InsufficientShares,
    InsufficientStake,
    InvalidAmount,
    InvalidArgument,
    InvalidState,
    NoRoute,
    PermissionDenied,
    PoolNotFound,
    SlippageExceeded,
)
from rapidwire.core.pools import canonical_pair, deposit_shares, initial_shares, optimal_deposit, validate_fee_tier, withdrawal_amounts
from rapidwire.core.routing import Allocation, PathSearch, PoolState, allocate, enumerate_paths
from rapidwire.core.swap import SwapConfig, allocation_chunk_hops, probe_amount
from rapidwire.core.validation import (
    BIGINT_MAX,
    is_guild_id,
    is_user_account_id,
    require_int_in_range,
    validate_non_negative_amount,
    validate_positive_amount,
    validate_positive_id,
)
from rapidwire.db.tables import (
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    FEE_TIERS_BPS,
    MINIMUM_LIQUIDITY,
    STAKE_CUSTODY_ACCOUNT_ID,
)
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "DEFAULT_GAS",
    "DEFAULT_MAX_SIZE",
    "GAS_CURRENCY_ID",
    "GAS_PRICE",
    "ISSUANCE_ACCOUNT_BASE",
    "MAX_GAS",
    "POOL_ACCOUNT_BASE",
    "CallRecord",
    "MockClaim",
    "MockContract",
    "MockCurrency",
    "MockLedger",
    "MockPool",
    "OutboxEntry",
    "RunResult",
    "TransferRecord",
    "dump_state",
    "from_json",
    "load_state",
    "to_json",
]


def _setting_default(name: str) -> Any:
    return Settings.model_fields[name].default


DEFAULT_GAS: int = _setting_default("default_gas")
MAX_GAS: int = _setting_default("max_gas")
GAS_PRICE: int = _setting_default("gas_price")
GAS_CURRENCY_ID: int = _setting_default("gas_currency_id")
DEFAULT_MAX_SIZE: int = _setting_default("contract_max_size")
SWAP_CHUNK_COUNT: int = _setting_default("swap_chunk_count")
SWAP_MAX_ROUTES: int = _setting_default("swap_max_routes")
# Transfer records name a pool's custody as POOL_ACCOUNT_BASE - pool_id; the server allocates real custody accounts instead.
POOL_ACCOUNT_BASE = -1_000_000
# Gas fees are burned into a currency's issuance account, named in transfer records as ISSUANCE_ACCOUNT_BASE - currency_id; burned units leave the supply.
ISSUANCE_ACCOUNT_BASE = -2_000_000
# The message of a negative minimum in add_liquidity and remove_liquidity, as the server's pools module gives it.
MIN_AMOUNT_MESSAGE = "The minimum amount must be a non-negative integer number of minimal units."


@dataclass
class MockCurrency:
    currency_id: int
    symbol: str
    name: str
    issuer: int
    hourly_rate_ppm: int = 0
    minting_renounced: bool = False
    delegation_enabled: bool = False
    delete_requested: bool = False
    created_at: int = 0


@dataclass
class MockClaim:
    claim_id: int
    claimant: int
    payer: int
    currency_id: int
    amount: int
    description: str
    status: str
    created_at: int


@dataclass
class MockPool:
    """A pool of currency_a < currency_b; total_shares is locked_shares plus the sum of shares."""

    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    reserve_a: int
    reserve_b: int
    total_shares: int
    shares: dict[int, int] = field(default_factory=dict)
    locked_shares: int = MINIMUM_LIQUIDITY

    @property
    def custody(self) -> int:
        return POOL_ACCOUNT_BASE - self.pool_id

    def state(self) -> PoolState:
        return PoolState(self.pool_id, self.currency_a, self.currency_b, self.fee_bps, self.reserve_a, self.reserve_b)


@dataclass
class MockContract:
    program: VerifiedProgram
    active: bool = True


@dataclass(frozen=True)
class TransferRecord:
    """One ledger movement; kind names the operation that made it, and pool and stake custody appear as negative account IDs."""

    kind: str
    from_id: int
    to_id: int
    currency_id: int
    amount: int


@dataclass(frozen=True)
class OutboxEntry:
    """A Discord operation queued by a contract; channel and message are set for send, user and role for the role operations."""

    kind: str
    contract_id: int
    guild: int
    channel: int | None = None
    message: str | None = None
    user: int | None = None
    role: int | None = None


@dataclass(frozen=True)
class CallRecord:
    """One contract execution of a run: depth 1 is the top-level call; error is the failure code, empty on success."""

    depth: int
    contract_id: int
    sender_id: int
    function: str
    args: tuple[Scalar, ...]
    attach: tuple[int, int] | None
    ok: bool
    value: int | str | bool | None
    error: str
    message: str
    gas_used: int
    random_values: tuple[int, ...]


@dataclass(frozen=True)
class RunResult:
    """The outcome of a top-level run; outbox and transfers are empty when it failed, because the run's effects were undone, and calls holds the executions the server would record, so a failed run holds only its top-level one."""

    ok: bool
    value: int | str | bool | None
    error: str
    message: str
    failed_function: str | None
    failed_offset: int | None
    gas_used: int
    gas_limit: int
    gas_fee: int
    random_values: tuple[int, ...]
    outbox: tuple[OutboxEntry, ...]
    transfers: tuple[TransferRecord, ...]
    calls: tuple[CallRecord, ...]


@dataclass
class _State:
    balances: dict[tuple[int, int], int] = field(default_factory=dict)
    allowances: dict[tuple[int, int, int], int] = field(default_factory=dict)
    claims: dict[int, MockClaim] = field(default_factory=dict)
    next_claim_id: int = 1
    pools: dict[int, MockPool] = field(default_factory=dict)
    next_pool_id: int = 1
    stakes: dict[tuple[int, int], int] = field(default_factory=dict)
    storage: dict[int, dict[str, bytes]] = field(default_factory=dict)
    outbox: list[OutboxEntry] = field(default_factory=list)
    transfers: list[TransferRecord] = field(default_factory=list)


def _host(error: DomainError) -> HostError:
    return HostError(error.code, error.message)


def _id(value: Account | Currency | int) -> int:
    return value if isinstance(value, int) else value.id


class MockLedger:
    """The in-memory Host; set it up with the add_/set_/deploy methods and run public functions with run()."""

    def __init__(
        self,
        *,
        limits: Limits = DEFAULT_LIMITS,
        seed: int | None = None,
        max_size: int = DEFAULT_MAX_SIZE,
        gas_price: int = GAS_PRICE,
        swap_config: SwapConfig | None = None,
        gas_currency_id: int = GAS_CURRENCY_ID,
        max_gas: int = MAX_GAS,
    ) -> None:
        self.limits = limits
        self.max_size = max_size
        self.gas_price = gas_price
        self.gas_currency_id = gas_currency_id
        self.max_gas = max_gas
        # In-contract swaps use the server's contract swap settings (fewer chunks and a small path search budget) unless a test passes its own.
        self.swap_config = swap_config if swap_config is not None else contract_swap_config(SWAP_CHUNK_COUNT, SWAP_MAX_ROUTES)
        self.currencies: dict[int, MockCurrency] = {}
        self.contracts: dict[int, MockContract] = {}
        self.roles: set[tuple[int, int, int]] = set()
        self.discord_permissions: set[tuple[int, int]] = set()
        self.state = _State()
        self.calls: list[CallRecord] = []
        # Inverse operations of the state changes made by the current run, newest last; a mark is a length of this list.
        self._undo: list[Callable[[], object]] = []
        self._rng: random.Random = random.Random(seed) if seed is not None else secrets.SystemRandom()  # noqa: S311 - a seed is for reproducible local runs only

    # Setup.

    def add_currency(self, currency_id: int, symbol: str, *, name: str | None = None, issuer: int = 1, hourly_rate_ppm: int = 0, created_at: int = 0) -> Currency:
        if currency_id < 1 or any(c.symbol == symbol for c in self.currencies.values() if c.currency_id != currency_id):
            raise ValueError(f"currency id {currency_id} must be positive and symbol {symbol} unused")
        self.currencies[currency_id] = MockCurrency(currency_id, symbol, name or symbol.capitalize(), issuer, hourly_rate_ppm=hourly_rate_ppm, created_at=created_at)
        return Currency(currency_id)

    def resolve_currency(self, ref: Currency | int | str) -> int:
        """The ID of a currency given as a Currency, an ID or a symbol; raises KeyError for an unknown one."""
        if isinstance(ref, str):
            for item in self.currencies.values():
                if item.symbol == ref:
                    return item.currency_id
            raise KeyError(f"unknown currency symbol {ref}")
        ident = _id(ref)
        if ident not in self.currencies:
            raise KeyError(f"unknown currency {ident}")
        return ident

    def set_balance(self, account: Account | int, currency: Currency | int | str, amount: int) -> None:
        self.state.balances[(_id(account), self.resolve_currency(currency))] = amount

    def balance_of(self, account: Account | int, currency: Currency | int | str) -> int:
        return self.state.balances.get((_id(account), self.resolve_currency(currency)), 0)

    def set_allowance(self, owner: Account | int, spender: Account | int, currency: Currency | int | str, amount: int) -> None:
        self.state.allowances[(_id(owner), _id(spender), self.resolve_currency(currency))] = amount

    def set_stake(self, account: Account | int, currency: Currency | int | str, amount: int) -> None:
        """Set a stake; the stake custody balance changes with it, as the stake is held there."""
        currency_id = self.resolve_currency(currency)
        key = (_id(account), currency_id)
        custody = (STAKE_CUSTODY_ACCOUNT_ID, currency_id)
        balances = self.state.balances
        balances[custody] = balances.get(custody, 0) + amount - self.state.stakes.get(key, 0)
        self.state.stakes[key] = amount

    def add_pool(
        self,
        currency_x: Currency | int | str,
        currency_y: Currency | int | str,
        fee_bps: int,
        reserve_x: int,
        reserve_y: int,
        shares: Mapping[int, int] | None = None,
        *,
        provider: int = 1,
    ) -> MockPool:
        """Add an active pool; shares maps provider accounts to their shares and the locked minimum is added on top, and without shares provider holds what a first deposit of the reserves mints."""
        x, y = self.resolve_currency(currency_x), self.resolve_currency(currency_y)
        if x == y or fee_bps not in FEE_TIERS_BPS or reserve_x <= 0 or reserve_y <= 0:
            raise ValueError("a pool needs two currencies, a valid fee tier and positive reserves")
        a, b, reserve_a, reserve_b = (x, y, reserve_x, reserve_y) if x < y else (y, x, reserve_y, reserve_x)
        if any(p.currency_a == a and p.currency_b == b and p.fee_bps == fee_bps for p in self.state.pools.values()):
            raise ValueError("an active pool of this pair and tier exists")
        if shares is None:
            minted = initial_shares(reserve_a, reserve_b)
            if minted <= MINIMUM_LIQUIDITY:
                raise ValueError(f"the reserves must mint more than {MINIMUM_LIQUIDITY} shares")
            shares = {provider: minted - MINIMUM_LIQUIDITY}
        holdings = {account: amount for account, amount in shares.items() if amount > 0}
        pool = MockPool(self.state.next_pool_id, a, b, fee_bps, reserve_a, reserve_b, MINIMUM_LIQUIDITY + sum(holdings.values()), holdings)
        self.state.pools[pool.pool_id] = pool
        self.state.next_pool_id += 1
        self.state.balances[(pool.custody, a)] = reserve_a
        self.state.balances[(pool.custody, b)] = reserve_b
        return pool

    def add_claim(self, claimant: Account | int, payer: Account | int, currency: Currency | int | str, amount: int, description: str = "", *, status: str = "pending", created_at: int = 0) -> int:
        claim_id = self.state.next_claim_id
        self.state.claims[claim_id] = MockClaim(claim_id, _id(claimant), _id(payer), self.resolve_currency(currency), amount, description, status, created_at)
        self.state.next_claim_id += 1
        return claim_id

    def grant_role(self, guild: int, user: Account | int, role: int) -> None:
        self.roles.add((guild, _id(user), role))

    def allow_discord(self, guild: int, contract: Account | int) -> None:
        self.discord_permissions.add((guild, _id(contract)))

    def deploy(self, account: Account | int, binary: bytes | VerifiedProgram, *, active: bool = True) -> VerifiedProgram:
        """Register a contract for account after the server's registration checks; raises VerificationError."""
        program = binary if isinstance(binary, VerifiedProgram) else verify(binary, max_size=self.max_size, limits=self.limits)
        self.contracts[_id(account)] = MockContract(program, active)
        return program

    def set_storage(self, account: Account | int, key: str, value: Value) -> None:
        storage_codec.validate_key(key, limits=self.limits)
        self.state.storage.setdefault(_id(account), {})[key] = storage_codec.encode(value, limits=self.limits)

    def storage_value(self, account: Account | int, key: str) -> Value | None:
        data = self.state.storage.get(_id(account), {}).get(key)
        return None if data is None else storage_codec.decode(data, limits=self.limits)

    def storage_items(self, account: Account | int) -> dict[str, Value]:
        return {key: storage_codec.decode(data, limits=self.limits) for key, data in sorted(self.state.storage.get(_id(account), {}).items())}

    # Undo log of the state changes made while a run executes.

    def _put(self, mapping: dict[Any, Any], key: Any, value: Any) -> None:
        if key in mapping:
            old = mapping[key]
            self._undo.append(lambda: mapping.__setitem__(key, old))
        else:
            self._undo.append(lambda: mapping.pop(key, None))
        mapping[key] = value

    def _remove(self, mapping: dict[Any, Any], key: Any) -> None:
        if key in mapping:
            old = mapping.pop(key)
            self._undo.append(lambda: mapping.__setitem__(key, old))

    def _assign(self, obj: object, name: str, value: Any) -> None:
        old = getattr(obj, name)
        self._undo.append(lambda: setattr(obj, name, old))
        setattr(obj, name, value)

    def _add(self, items: list[Any], item: Any) -> None:
        items.append(item)
        self._undo.append(items.pop)

    def _rollback(self, mark: int) -> None:
        """Undo every state change logged after mark, newest first."""
        undo = self._undo
        while len(undo) > mark:
            undo.pop()()

    # Running.

    async def run(
        self,
        contract: Account | int,
        function: str,
        args: Sequence[Scalar] = (),
        *,
        sender: Account | int,
        attach: Attachment | None = None,
        gas: int = DEFAULT_GAS,
        now: int | None = None,
    ) -> RunResult:
        """Call a public function of contract as sender, attaching attach, with a gas limit, in the server's order of checks.

        Arguments the server rejects before recording anything (a gas limit outside 1 to max_gas or costing more than the largest amount, an attachment that is not positive or goes to one's own contract) raise ValueError.
        A run fails without executing when the gas currency is missing, the contract is missing or paused, or the sender cannot prepay gas_limit * gas_price plus an attachment in the gas currency; every effect of a failed run is undone except the gas fee, which is burned from the sender on success and on failure.
        """
        contract_id, sender_id = _id(contract), _id(sender)
        if not 1 <= gas <= self.max_gas:
            raise ValueError(f"gas_limit must be an integer between 1 and {self.max_gas}.")
        if gas * self.gas_price > AMOUNT_MAX:
            raise ValueError("The gas limit costs more than the largest amount.")
        if attach is not None:
            if attach.amount <= 0 or attach.amount > AMOUNT_MAX:
                raise ValueError(InvalidAmount.default_message)
            if contract_id == sender_id:
                raise ValueError("You cannot attach currency to a call of your own contract.")
        context = ExecutionContext.top_level(
            self_account=Account(contract_id),
            sender=Account(sender_id),
            paid=attach,
            now=int(time.time()) if now is None else now,
            gas_limit=gas,
            limits=self.limits,
        )
        self.state.outbox = []
        self.state.transfers = []
        first_call = len(self.calls)
        self._undo.clear()
        try:
            return await self._run_logged(context, function, list(args), attach, gas, first_call)
        except BaseException:
            self._rollback(0)
            raise
        finally:
            self._undo.clear()

    async def _run_logged(self, context: ExecutionContext, function: str, args: list[Scalar], attach: Attachment | None, gas: int, first_call: int) -> RunResult:
        sender_id = context.sender.id
        try:
            self._prepay(context, function, args, attach, gas)
            value = await self._invoke(context, context.self_account, function, args, attach)
        except ContractFailure as exc:
            return self._failed(context, gas, first_call, exc.code, exc.message, exc.function, exc.offset)
        try:
            self._burn_gas(sender_id, context.gas.used)
        except HostError as error:
            # The contract spent the sender's funds (for example with transfer_from): settlement fails, everything is undone and the fee is taken from the restored balance, which the prepay check covers.
            # As on the server, only the top-level execution stays recorded, now failed.
            top = self.calls[-1]
            del self.calls[first_call:]
            self.calls.append(replace(top, ok=False, value=None, error="SETTLEMENT", message=error.message))
            return self._failed(context, gas, first_call, "SETTLEMENT", error.message, None, None)
        return RunResult(
            ok=True,
            value=value,
            error="",
            message="",
            failed_function=None,
            failed_offset=None,
            gas_used=context.gas.used,
            gas_limit=gas,
            gas_fee=context.gas.used * self.gas_price,
            random_values=tuple(context.random_log),
            outbox=tuple(self.state.outbox),
            transfers=tuple(self.state.transfers),
            calls=tuple(self.calls[first_call:]),
        )

    def _failed(self, context: ExecutionContext, gas: int, first_call: int, error: str, message: str, function: str | None, offset: int | None) -> RunResult:
        """Undo the run, burn the fee of the gas it used from the restored balance and describe the failure."""
        self._rollback(0)
        self._burn_gas(context.sender.id, context.gas.used)
        return RunResult(
            ok=False,
            value=None,
            error=error,
            message=message,
            failed_function=function,
            failed_offset=offset,
            gas_used=context.gas.used,
            gas_limit=gas,
            gas_fee=context.gas.used * self.gas_price,
            random_values=tuple(context.random_log),
            outbox=(),
            transfers=tuple(self.state.transfers),
            calls=tuple(self.calls[first_call:]),
        )

    def _prepay(self, ctx: ExecutionContext, function: str, args: list[Scalar], attach: Attachment | None, gas: int) -> None:
        """The checks a top-level call passes before its function is looked up, in the server's order: the gas currency and the attachment's currency exist (lowest ID first), the contract can run, and the sender holds the gas limit's cost and an attachment in the gas currency."""
        try:
            for currency_id in sorted({self.gas_currency_id} | ({attach.currency.id} if attach is not None else set())):
                if currency_id not in self.currencies:
                    raise CallFailed(CallFailure.NO_CURRENCY, f"Currency {currency_id} was not found.")
            self._runnable(ctx.self_account)
            needed = gas * self.gas_price + (attach.amount if attach is not None and attach.currency.id == self.gas_currency_id else 0)
            held = self.state.balances.get((ctx.sender.id, self.gas_currency_id), 0)
            if held < needed:
                message = f"Calling with a gas limit of {gas} needs {needed} of currency {self.gas_currency_id}, but you hold {held}."
                raise CallFailed(CallFailure.INSUFFICIENT_BALANCE, message)
        except CallFailed as exc:
            self._record(ctx, function, args, attach, False, None, exc.code, exc.message, 0)
            raise

    def _burn_gas(self, sender_id: int, gas_used: int) -> None:
        """Burn the fee of gas_used from the sender into the gas currency's issuance account, which the server records as a gas transfer."""
        cost = gas_used * self.gas_price
        if not cost:
            return
        key = (sender_id, self.gas_currency_id)
        held = self.state.balances.get(key, 0)
        if held < cost:
            raise _host(InsufficientBalance())
        self._put(self.state.balances, key, held - cost)
        self._add(self.state.transfers, TransferRecord("gas", sender_id, ISSUANCE_ACCOUNT_BASE - self.gas_currency_id, self.gas_currency_id, cost))

    def _runnable(self, target: Account) -> MockContract:
        contract = self.contracts.get(target.id)
        if contract is None:
            raise CallFailed(CallFailure.NO_CONTRACT, f"account {target.id} has no contract")
        if not contract.active:
            raise CallFailed(CallFailure.INACTIVE, f"the contract of account {target.id} is paused")
        return contract

    async def _invoke(self, ctx: ExecutionContext, target: Account, function: str, args: list[Scalar], attach: Attachment | None) -> int | str | bool:
        try:
            contract = self._runnable(target)
            fn = contract.program.public_function(function)
            if fn is None:
                raise CallFailed(CallFailure.NO_FUNCTION, f"no public function {function!r}")
            problem = check_args(fn.params, args, self.limits)
            if problem is not None:
                raise CallFailed(CallFailure.BAD_ARGUMENTS, f"{function}: {problem}")
        except CallFailed as exc:
            self._record(ctx, function, args, attach, False, None, exc.code, exc.message, 0)
            raise
        mark = len(self._undo)
        first_call = len(self.calls)
        start = ctx.gas.used
        try:
            if attach is not None:
                self._pay_attachment(ctx, attach)
            outcome = await execute(contract.program, function, args, host=self, context=ctx)
        except ContractFailure as exc:
            # The server rolls back the rows of a failed execution's nested calls with it and keeps only the failed execution's own row.
            self._rollback(mark)
            del self.calls[first_call:]
            used = ctx.gas.used - start
            self._record(ctx, function, args, attach, False, None, exc.code, exc.message, used)
            raise
        except BaseException:
            self._rollback(mark)
            del self.calls[first_call:]
            raise
        self._record(ctx, function, args, attach, True, outcome.value, "", "", outcome.gas_used)
        return outcome.value

    def _pay_attachment(self, ctx: ExecutionContext, attach: Attachment) -> None:
        """Move the attachment from the caller to the callee with the server's failure reasons: a top-level call has already checked its currency, a nested call fails NO_CURRENCY for a missing currency and INSUFFICIENT_BALANCE otherwise."""
        nested = ctx.depth > 1
        if nested and attach.currency.id not in self.currencies:
            raise CallFailed(CallFailure.NO_CURRENCY, f"the attachment's currency {attach.currency.id} was not found")
        try:
            self._move("attachment", ctx.sender.id, ctx.self_account.id, attach.currency.id, attach.amount)
        except HostError as error:
            raise CallFailed(CallFailure.INSUFFICIENT_BALANCE, f"the attachment cannot be paid: {error.message}" if nested else error.message) from None

    def _record(self, ctx: ExecutionContext, function: str, args: list[Scalar], attach: Attachment | None, ok: bool, value: int | str | bool | None, error: str, message: str, gas: int) -> None:
        self.calls.append(
            CallRecord(
                depth=ctx.depth,
                contract_id=ctx.self_account.id,
                sender_id=ctx.sender.id,
                function=function if ctx.depth == 1 else recorded_function_name(function),
                args=tuple(args),
                attach=None if attach is None else (attach.currency.id, attach.amount),
                ok=ok,
                value=value,
                error=error,
                message=message,
                gas_used=gas,
                random_values=tuple(ctx.random_log),
            )
        )

    # Ledger helpers.

    def _move(self, kind: str, from_id: int, to_id: int, currency_id: int, amount: int) -> None:
        balances = self.state.balances
        held = balances.get((from_id, currency_id), 0)
        if held < amount:
            raise _host(InsufficientBalance())
        received = balances.get((to_id, currency_id), 0) + amount
        if received > AMOUNT_MAX:
            raise _host(AmountOverflow())
        self._put(balances, (from_id, currency_id), held - amount)
        self._put(balances, (to_id, currency_id), received)
        self._add(self.state.transfers, TransferRecord(kind, from_id, to_id, currency_id, amount))

    def _live_currency(self, currency: Currency) -> MockCurrency:
        found = self.currencies.get(currency.id)
        if found is None:
            raise _host(CurrencyNotFound(f"Currency {currency.id} was not found."))
        return found

    @staticmethod
    def _checked(check: Callable[[], object]) -> None:
        """Run one of the server's validation helpers, reporting its DomainError as the HostError the server's host raises."""
        try:
            check()
        except DomainError as error:
            raise _host(error) from None

    def _positive(self, amount: int) -> None:
        self._checked(lambda: validate_positive_amount(amount))

    def _non_negative(self, amount: int, message: str) -> None:
        self._checked(lambda: validate_non_negative_amount(amount, message))

    def _supply(self, currency_id: int) -> int:
        """Every unit of the currency held anywhere, pool and stake custody included."""
        return sum(amount for (_, cid), amount in self.state.balances.items() if cid == currency_id)

    # Host: balances and currencies.

    async def balance(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        self._live_currency(currency)
        return self.state.balances.get((account.id, currency.id), 0)

    async def currency(self, ctx: ExecutionContext, symbol: str) -> Currency:
        for item in self.currencies.values():
            if item.symbol == symbol:
                return Currency(item.currency_id)
        raise _host(CurrencyNotFound(f"No currency has the symbol {symbol[:40]!r}."))

    async def currency_info(self, ctx: ExecutionContext, currency: Currency) -> Record:
        item = self._live_currency(currency)
        return make_record(
            Kind.CURRENCY_INFO,
            currency=currency,
            name=item.name,
            symbol=item.symbol,
            issuer=Account(item.issuer),
            supply=self._supply(item.currency_id),
            hourly_rate_ppm=item.hourly_rate_ppm,
            minting_renounced=item.minting_renounced,
            delegation_enabled=item.delegation_enabled,
            delete_requested=item.delete_requested,
            created_at=item.created_at,
        )

    # Host: transfers and allowances.

    async def transfer(self, ctx: ExecutionContext, to: Account, currency: Currency, amount: int) -> None:
        self._positive(amount)
        if to == ctx.self_account:
            raise _host(InvalidArgument("You cannot transfer to yourself."))
        self._live_currency(currency)
        self._move("transfer", ctx.self_account.id, to.id, currency.id, amount)

    async def transfer_from(self, ctx: ExecutionContext, owner: Account, to: Account, currency: Currency, amount: int) -> None:
        self._positive(amount)
        spender = ctx.self_account
        if spender == owner:
            raise _host(InvalidArgument("The owner cannot spend an allowance on their own funds; use a transfer instead."))
        if owner == to:
            raise _host(InvalidArgument("The owner and the recipient must differ."))
        self._live_currency(currency)
        key = (owner.id, spender.id, currency.id)
        available = self.state.allowances.get(key, 0)
        if available < amount:
            raise _host(InsufficientAllowance())
        self._move("transfer_from", owner.id, to.id, currency.id, amount)
        self._put(self.state.allowances, key, available - amount)

    async def approve(self, ctx: ExecutionContext, spender: Account, currency: Currency, amount: int) -> None:
        self._non_negative(amount, "The allowance must be zero or a positive integer number of minimal units.")
        if spender == ctx.self_account:
            raise _host(InvalidArgument("You cannot approve yourself as a spender."))
        self._live_currency(currency)
        self._put(self.state.allowances, (ctx.self_account.id, spender.id, currency.id), amount)

    async def allowance(self, ctx: ExecutionContext, owner: Account, spender: Account, currency: Currency) -> int:
        return self.state.allowances.get((owner.id, spender.id, currency.id), 0)

    # Host: claims.

    async def create_claim(self, ctx: ExecutionContext, payer: Account, currency: Currency, amount: int, description: str) -> int:
        self._positive(amount)
        try:
            stored = normalize_description(description)
        except DomainError as error:
            raise _host(error) from None
        if payer == ctx.self_account:
            raise _host(InvalidArgument("You cannot create a claim against yourself."))
        self._live_currency(currency)
        claim_id = self.state.next_claim_id
        self._put(self.state.claims, claim_id, MockClaim(claim_id, ctx.self_account.id, payer.id, currency.id, amount, stored or "", "pending", ctx.now))
        self._assign(self.state, "next_claim_id", claim_id + 1)
        return claim_id

    def _claim(self, claim_id: int) -> MockClaim:
        claim = self.state.claims.get(claim_id)
        if claim is None:
            raise _host(ClaimNotFound())
        return claim

    async def pay_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        claim = self._claim(claim_id)
        if claim.payer != ctx.self_account.id:
            raise _host(PermissionDenied("Only the payer of this claim can pay it."))
        if claim.status != "pending":
            raise _host(InvalidState(f"The claim is not pending (status: {claim.status})."))
        if claim.currency_id not in self.currencies:
            raise _host(CurrencyNotFound(f"Currency {claim.currency_id} was not found."))
        self._move("claim", claim.payer, claim.claimant, claim.currency_id, claim.amount)
        self._assign(claim, "status", "paid")

    async def cancel_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        claim = self._claim(claim_id)
        if ctx.self_account.id not in (claim.payer, claim.claimant):
            raise _host(PermissionDenied("Only the payer or the claimant of this claim can cancel it."))
        if claim.status != "pending":
            raise _host(InvalidState(f"Only pending claims can be canceled (status: {claim.status})."))
        self._assign(claim, "status", "canceled")

    async def claim_info(self, ctx: ExecutionContext, claim_id: int) -> Record:
        claim = self._claim(claim_id)
        return make_record(
            Kind.CLAIM_INFO,
            claim_id=claim.claim_id,
            claimant=Account(claim.claimant),
            payer=Account(claim.payer),
            currency=Currency(claim.currency_id),
            amount=claim.amount,
            description=claim.description,
            status=claim.status,
            created_at=claim.created_at,
        )

    # Host: DEX.

    def _search(self, from_currency: Currency, to_currency: Currency, amount_in: int) -> tuple[list[PoolState], PathSearch]:
        """Validate the pair and amount as the server's planner does, then enumerate the candidate paths over the active pools."""
        try:
            validate_positive_id("from_currency", from_currency.id)
            validate_positive_id("to_currency", to_currency.id)
            validate_positive_amount(amount_in)
        except DomainError as error:
            raise _host(error) from None
        if from_currency == to_currency:
            raise _host(InvalidArgument("A swap needs two different currencies."))
        config = self.swap_config
        pools = [pool.state() for pool in self.state.pools.values()]
        search = enumerate_paths(
            pools,
            from_currency.id,
            to_currency.id,
            max_paths=config.max_routes,
            probe_amount=probe_amount(amount_in, config.chunk_count),
            max_expansions=config.max_expansions,
        )
        return pools, search

    def _allocate(self, pools: list[PoolState], search: PathSearch, from_currency: Currency, to_currency: Currency, amount_in: int) -> Allocation:
        used = {pool_id for path in search.paths for pool_id in path.pool_ids}
        return allocate(search.paths, [state for state in pools if state.pool_id in used], amount_in, chunk_count=self.swap_config.chunk_count)

    @staticmethod
    def _require_route(allocation: Allocation, from_currency: Currency, to_currency: Currency) -> None:
        if allocation.is_empty or allocation.amount_out <= 0:
            raise _host(NoRoute(f"No route from currency {from_currency.id} to currency {to_currency.id} pays out anything."))

    def _charge_route_search(self, ctx: ExecutionContext, search: PathSearch, amount_in: int) -> None:
        ctx.gas.charge(route_search_gas(search.expansions, allocation_chunk_hops(search.paths, amount_in, self.swap_config.chunk_count)))

    async def swap(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int, min_out: int) -> int:
        """Plan, charge the route search as the server does before any ledger check, then swap along the plan."""
        pools, search = self._search(from_currency, to_currency, amount_in)
        self._charge_route_search(ctx, search, amount_in)
        self._non_negative(min_out, "The minimum amount out must be a non-negative integer number of minimal units.")
        self._live_currency(from_currency)
        self._live_currency(to_currency)
        user = ctx.self_account.id
        held = self.state.balances.get((user, from_currency.id), 0)
        if held < amount_in:
            raise _host(InsufficientBalance(f"You hold {held} of currency {from_currency.id}, less than {amount_in}."))
        allocation = self._allocate(pools, search, from_currency, to_currency, amount_in)
        self._require_route(allocation, from_currency, to_currency)
        if allocation.amount_out < min_out:
            raise _host(SlippageExceeded(f"The swap would pay out {allocation.amount_out}, less than the minimum of {min_out}."))
        balances = self.state.balances
        received = balances.get((user, to_currency.id), 0) + allocation.amount_out
        if received > AMOUNT_MAX or any(max(reserves) > AMOUNT_MAX for reserves in allocation.final_reserves.values()):
            raise _host(AmountOverflow())
        self._put(balances, (user, from_currency.id), held - amount_in)
        self._put(balances, (user, to_currency.id), received)
        for pool_id, (reserve_a, reserve_b) in allocation.final_reserves.items():
            pool = self.state.pools[pool_id]
            self._assign(pool, "reserve_a", reserve_a)
            self._assign(pool, "reserve_b", reserve_b)
            self._put(balances, (pool.custody, pool.currency_a), reserve_a)
            self._put(balances, (pool.custody, pool.currency_b), reserve_b)
        for route in allocation.routes:
            for index, hop in enumerate(route.hops):
                custody = self.state.pools[hop.pool_id].custody
                if index == 0:
                    self._add(self.state.transfers, TransferRecord("swap", user, custody, hop.currency_in, hop.amount_in))
                receiver = user if index + 1 == len(route.hops) else self.state.pools[route.hops[index + 1].pool_id].custody
                self._add(self.state.transfers, TransferRecord("swap", custody, receiver, hop.currency_out, hop.amount_out))
        return allocation.amount_out

    async def quote(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int) -> Record:
        """Quote on the current reserves; as on the server the route search is charged once the search and allocation ran, also when they found no route, before NoRoute is raised."""
        pools, search = self._search(from_currency, to_currency, amount_in)
        self._live_currency(from_currency)
        self._live_currency(to_currency)
        allocation = self._allocate(pools, search, from_currency, to_currency, amount_in)
        self._charge_route_search(ctx, search, amount_in)
        self._require_route(allocation, from_currency, to_currency)
        return make_record(
            Kind.QUOTE,
            from_currency=from_currency,
            to_currency=to_currency,
            amount_in=amount_in,
            amount_out=allocation.amount_out,
            route_count=len(allocation.routes),
            truncated=search.truncated,
        )

    def _pair_and_tier(self, currency_x: Currency, currency_y: Currency, fee_bps: int) -> None:
        """The server's first checks of a liquidity operation: the pair (canonical_pair) and the fee tier (validate_fee_tier)."""
        self._checked(lambda: canonical_pair(currency_x.id, currency_y.id))
        self._checked(lambda: validate_fee_tier(fee_bps))

    def _pool(self, currency_x: Currency, currency_y: Currency, fee_bps: int) -> tuple[MockPool, bool]:
        self._live_currency(currency_x)
        self._live_currency(currency_y)
        a, b = sorted((currency_x.id, currency_y.id))
        for pool in self.state.pools.values():
            if pool.currency_a == a and pool.currency_b == b and pool.fee_bps == fee_bps:
                return pool, currency_x.id > currency_y.id
        raise _host(PoolNotFound(f"There is no active pool of currencies {a} and {b} with a {fee_bps} bps fee."))

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
        self._pair_and_tier(currency_x, currency_y, fee_bps)
        self._positive(amount_x)
        self._positive(amount_y)
        self._non_negative(amount_x_min, MIN_AMOUNT_MESSAGE)
        self._non_negative(amount_y_min, MIN_AMOUNT_MESSAGE)
        self._checked(lambda: require_int_in_range("min_shares", min_shares, 1, AMOUNT_MAX))
        pool, swapped = self._pool(currency_x, currency_y, fee_bps)
        desired_a, desired_b = (amount_y, amount_x) if swapped else (amount_x, amount_y)
        min_a, min_b = (amount_y_min, amount_x_min) if swapped else (amount_x_min, amount_y_min)
        amount_a, amount_b = optimal_deposit(desired_a, desired_b, pool.reserve_a, pool.reserve_b)
        minted = deposit_shares(amount_a, amount_b, pool.reserve_a, pool.reserve_b, pool.total_shares)
        if amount_a == 0 or amount_b == 0 or minted == 0:
            raise _host(InsufficientLiquidity("The deposit is too small to mint any shares at the pool's current price."))
        if amount_a < min_a or amount_b < min_b:
            raise _host(SlippageExceeded("The deposit at the pool's current price would use less than the minimum amount."))
        if minted < min_shares:
            raise _host(SlippageExceeded(f"The deposit would mint {minted} shares, fewer than the minimum of {min_shares}."))
        if max(pool.reserve_a + amount_a, pool.reserve_b + amount_b, pool.total_shares + minted) > AMOUNT_MAX:
            raise _host(AmountOverflow("The pool's reserves or shares would exceed the supported range."))
        user = ctx.self_account.id
        self._move("liquidity", user, pool.custody, pool.currency_a, amount_a)
        self._move("liquidity", user, pool.custody, pool.currency_b, amount_b)
        self._assign(pool, "reserve_a", pool.reserve_a + amount_a)
        self._assign(pool, "reserve_b", pool.reserve_b + amount_b)
        self._assign(pool, "total_shares", pool.total_shares + minted)
        self._put(pool.shares, user, pool.shares.get(user, 0) + minted)
        return minted

    async def remove_liquidity(self, ctx: ExecutionContext, currency_x: Currency, currency_y: Currency, fee_bps: int, shares: int, amount_x_min: int, amount_y_min: int) -> list[int]:
        self._pair_and_tier(currency_x, currency_y, fee_bps)
        self._positive(shares)
        self._non_negative(amount_x_min, MIN_AMOUNT_MESSAGE)
        self._non_negative(amount_y_min, MIN_AMOUNT_MESSAGE)
        pool, swapped = self._pool(currency_x, currency_y, fee_bps)
        user = ctx.self_account.id
        held = pool.shares.get(user, 0)
        if held < shares:
            raise _host(InsufficientShares(f"You hold {held} shares of this pool, fewer than {shares}."))
        amount_a, amount_b = withdrawal_amounts(shares, pool.reserve_a, pool.reserve_b, pool.total_shares)
        min_a, min_b = (amount_y_min, amount_x_min) if swapped else (amount_x_min, amount_y_min)
        if amount_a == 0 or amount_b == 0:
            raise _host(InsufficientLiquidity("Burning this many shares would pay out nothing of one currency."))
        if amount_a < min_a or amount_b < min_b:
            raise _host(SlippageExceeded("The withdrawal would pay out less than the minimum amount."))
        self._move("liquidity", pool.custody, user, pool.currency_a, amount_a)
        self._move("liquidity", pool.custody, user, pool.currency_b, amount_b)
        self._assign(pool, "reserve_a", pool.reserve_a - amount_a)
        self._assign(pool, "reserve_b", pool.reserve_b - amount_b)
        self._assign(pool, "total_shares", pool.total_shares - shares)
        if held == shares:
            self._remove(pool.shares, user)
        else:
            self._put(pool.shares, user, held - shares)
        return [amount_b, amount_a] if swapped else [amount_a, amount_b]

    # Host: staking.

    async def stake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        self._positive(amount)
        self._live_currency(currency)
        user = ctx.self_account.id
        self._move("stake", user, STAKE_CUSTODY_ACCOUNT_ID, currency.id, amount)
        self._put(self.state.stakes, (user, currency.id), self.state.stakes.get((user, currency.id), 0) + amount)

    async def unstake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        self._positive(amount)
        self._live_currency(currency)
        user = ctx.self_account.id
        held = self.state.stakes.get((user, currency.id), 0)
        if held < amount:
            raise _host(InsufficientStake("Withdrawal amount exceeds staked balance."))
        self._move("unstake", STAKE_CUSTODY_ACCOUNT_ID, user, currency.id, amount)
        self._put(self.state.stakes, (user, currency.id), held - amount)

    async def staked(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        return self.state.stakes.get((account.id, currency.id), 0)

    # Host: storage.

    async def storage_get(self, ctx: ExecutionContext, key: str) -> bytes | None:
        return self.state.storage.get(ctx.self_account.id, {}).get(key)

    async def storage_set(self, ctx: ExecutionContext, key: str, data: bytes) -> None:
        storage_codec.validate_key(key, limits=self.limits)
        encoded = storage_codec.canonicalize(data, limits=self.limits)
        entries = self.state.storage.setdefault(ctx.self_account.id, {})
        keys = len(entries) + (0 if key in entries else 1)
        total = sum(storage_codec.entry_size(k, len(v)) for k, v in entries.items() if k != key) + storage_codec.entry_size(key, len(encoded))
        if keys > CONTRACT_STORAGE_MAX_KEYS:
            raise _host(ContractStorageLimit(f"A contract can store at most {CONTRACT_STORAGE_MAX_KEYS} keys."))
        if total > CONTRACT_STORAGE_MAX_BYTES:
            raise _host(ContractStorageLimit(f"A contract can store at most {CONTRACT_STORAGE_MAX_BYTES} bytes."))
        self._put(entries, key, encoded)

    async def storage_delete(self, ctx: ExecutionContext, key: str) -> None:
        self._remove(self.state.storage.get(ctx.self_account.id, {}), key)

    async def read_storage(self, ctx: ExecutionContext, account: Account, key: str) -> bytes | None:
        return self.state.storage.get(account.id, {}).get(key)

    # Host: Discord.

    def _permitted(self, ctx: ExecutionContext, guild: int) -> None:
        if not is_guild_id(guild):
            raise _host(InvalidArgument(f"The server ID must be an integer between 1 and {BIGINT_MAX}."))
        if (guild, ctx.self_account.id) not in self.discord_permissions:
            raise _host(PermissionDenied(f"This contract is not allowed to perform Discord operations in server {guild}."))

    def _queue(self, ctx: ExecutionContext, entry: OutboxEntry) -> None:
        self._permitted(ctx, entry.guild)
        self._add(self.state.outbox, entry)

    @staticmethod
    def _discord_id(name: str, value: int) -> None:
        if not 1 <= value <= BIGINT_MAX:
            raise _host(InvalidArgument(f"The {name} ID must be an integer between 1 and {BIGINT_MAX}."))

    async def discord_send(self, ctx: ExecutionContext, guild: int, channel: int, message: str) -> None:
        self._discord_id("channel", channel)
        if not 1 <= len(message) <= DISCORD_MESSAGE_MAX_LENGTH:
            raise _host(InvalidArgument(f"A Discord message must have 1 to {DISCORD_MESSAGE_MAX_LENGTH} characters."))
        self._queue(ctx, OutboxEntry("discord_send", ctx.self_account.id, guild, channel=channel, message=message))

    def _role_change(self, ctx: ExecutionContext, kind: str, guild: int, user: Account, role: int) -> None:
        if not is_user_account_id(user.id):
            raise _host(InvalidArgument("The user must be a user account."))
        self._discord_id("role", role)
        self._queue(ctx, OutboxEntry(kind, ctx.self_account.id, guild, user=user.id, role=role))

    async def discord_add_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        self._role_change(ctx, "discord_add_role", guild, user, role)

    async def discord_remove_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        self._role_change(ctx, "discord_remove_role", guild, user, role)

    async def discord_has_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> bool:
        """Whether the configured roles hold (guild, user, role); as on the server, a user that is not a user account, an invalid role ID and a guild that has not granted this contract its Discord permission are failures."""
        if not is_user_account_id(user.id):
            raise _host(InvalidArgument("The user must be a user account."))
        self._discord_id("role", role)
        self._permitted(ctx, guild)
        return (guild, user.id, role) in self.roles

    # Host: nested calls and randomness.

    async def call_contract(self, ctx: ExecutionContext, target: Account, function: str, args: list[Scalar], attach: Attachment | None) -> int | str | bool:
        return await self._invoke(ctx, target, function, args, attach)

    async def reject_call(self, ctx: ExecutionContext, target: Account, function: str, args: list[Any], attach: Attachment | None, failure: CallFailed) -> NoReturn:
        """Record a call the VM refused as the server does: a failed call with no gas used, the server's recorded arguments and function name."""
        self._record(ctx, function, list(recorded_args(args, failure)), attach, False, None, failure.code, failure.message, 0)
        raise failure

    def random_below(self, n: int) -> int:
        return self._rng.randrange(n)


def to_json(value: object) -> Any:
    """The JSON form of a VM value: scalars as themselves, Account and Currency as {"$account": id} and {"$currency": id}, lists as arrays, dicts with str keys as objects, other dicts as {"$dict": [[key, value], ...]} and records as {"$record": kind, field: value, ...}."""
    if type(value) is Account:
        return {"$account": value.id}
    if type(value) is Currency:
        return {"$currency": value.id}
    if type(value) is list:
        return [to_json(item) for item in value]
    if type(value) is dict:
        if all(type(key) is str for key in value):
            return {key: to_json(item) for key, item in value.items()}
        return {"$dict": [[to_json(key), to_json(item)] for key, item in value.items()]}
    if type(value) is Record:
        fields = {f.name: to_json(v) for f, v in zip(RECORD_FIELDS[value.kind], value.values, strict=True)}
        return {"$record": KIND_NAMES[value.kind], **fields}
    if value is None or type(value) in (int, str, bool):
        return value
    raise TypeError(f"{type(value).__name__} is not a VM value")


def from_json(data: Any) -> Value:
    """The VM value of a to_json() form (records excepted); raises ValueError for anything else, including floats and null."""
    if type(data) in (int, str, bool):
        return data  # type: ignore[no-any-return]
    if type(data) is list:
        return [from_json(item) for item in data]
    if type(data) is dict:
        if set(data) == {"$account"}:
            return Account(_json_int(data["$account"]))
        if set(data) == {"$currency"}:
            return Currency(_json_int(data["$currency"]))
        if set(data) == {"$dict"} and type(data["$dict"]) is list:
            entries: dict[Any, Value] = {}
            for pair in data["$dict"]:
                if type(pair) is not list or len(pair) != 2:
                    raise ValueError("$dict entries must be [key, value] pairs")
                key = from_json(pair[0])
                if type(key) not in KEY_TYPES:
                    raise ValueError("dict keys must be scalars")
                entries[key] = from_json(pair[1])
            return entries
        if any(type(key) is str and key.startswith("$") for key in data):
            raise ValueError(f"unknown tagged value {sorted(data)}")
        return {key: from_json(item) for key, item in data.items()}
    raise ValueError(f"{type(data).__name__} is not a contract value")


def _json_int(value: Any) -> int:
    if type(value) is not int:
        raise ValueError(f"expected an integer, got {value!r}")
    return value


_STATE_KEYS = frozenset({"now", "gas_currency", "currencies", "balances", "allowances", "stakes", "pools", "claims", "roles", "discord_permissions", "contracts", "storage"})


def load_state(ledger: MockLedger, data: Mapping[str, Any], *, base_dir: Path) -> int | None:
    """Seed ledger from a state document and return its "now" (or None); raises ValueError for a malformed document, CompileError or VerificationError for a contract.

    Keys (all optional): "now" int; "gas_currency" the ID of the currency gas is paid in (default: the ledger's, the server's default gas currency ID); "currencies" [{"id", "symbol", "name"?, "issuer"?, "hourly_rate_ppm"?, "created_at"?}]; "balances", "stakes" [{"account", "currency", "amount"}]; "allowances" [{"owner", "spender", "currency", "amount"}];
    "pools" [{"currency_x", "currency_y", "fee_bps", "reserve_x", "reserve_y", "shares"?: {account: shares} (absent: account 1 holds the shares a first deposit mints)}]; "claims" [{"claimant", "payer", "currency", "amount", "description"?, "status"?, "created_at"?}];
    "roles" [[guild, user, role]]; "discord_permissions" [[guild, contract]] (the only guilds where a contract may send messages or change roles); "contracts" [{"account", "source" or "binary" (paths relative to base_dir), "active"?}]; "storage" {account: {key: value in to_json() form}}.
    A currency is named by its ID or symbol.
    """
    unknown = set(data) - _STATE_KEYS
    if unknown:
        raise ValueError(f"unknown state keys: {', '.join(sorted(unknown))}")
    try:
        if "gas_currency" in data:
            ledger.gas_currency_id = _json_int(data["gas_currency"])
        for item in data.get("currencies", []):
            ledger.add_currency(
                _json_int(item["id"]),
                str(item["symbol"]),
                name=item.get("name"),
                issuer=_json_int(item.get("issuer", 1)),
                hourly_rate_ppm=_json_int(item.get("hourly_rate_ppm", 0)),
                created_at=_json_int(item.get("created_at", 0)),
            )
        for item in data.get("balances", []):
            ledger.set_balance(_json_int(item["account"]), item["currency"], _json_int(item["amount"]))
        for item in data.get("stakes", []):
            ledger.set_stake(_json_int(item["account"]), item["currency"], _json_int(item["amount"]))
        for item in data.get("allowances", []):
            ledger.set_allowance(_json_int(item["owner"]), _json_int(item["spender"]), item["currency"], _json_int(item["amount"]))
        for item in data.get("pools", []):
            shares = {int(account): _json_int(amount) for account, amount in item["shares"].items()} if "shares" in item else None
            ledger.add_pool(item["currency_x"], item["currency_y"], _json_int(item["fee_bps"]), _json_int(item["reserve_x"]), _json_int(item["reserve_y"]), shares)
        for item in data.get("claims", []):
            ledger.add_claim(
                _json_int(item["claimant"]),
                _json_int(item["payer"]),
                item["currency"],
                _json_int(item["amount"]),
                str(item.get("description", "")),
                status=str(item.get("status", "pending")),
                created_at=_json_int(item.get("created_at", 0)),
            )
        for guild, user, role in data.get("roles", []):
            ledger.grant_role(_json_int(guild), _json_int(user), _json_int(role))
        for guild, account in data.get("discord_permissions", []):
            ledger.allow_discord(_json_int(guild), _json_int(account))
        for item in data.get("contracts", []):
            account = _json_int(item["account"])
            if "source" in item:
                path = base_dir / str(item["source"])
                binary = compile_source(path.read_text(encoding="utf-8"), filename=str(path))
            else:
                binary = (base_dir / str(item["binary"])).read_bytes()
            ledger.deploy(account, binary, active=bool(item.get("active", True)))
        for account, entries in data.get("storage", {}).items():
            for key, value in entries.items():
                ledger.set_storage(int(account), key, from_json(value))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed state document: {exc!r}") from None
    now = data.get("now")
    return None if now is None else _json_int(now)


def dump_state(ledger: MockLedger) -> dict[str, Any]:
    """The ledger's balances, allowances, stakes, pools, claims and storage in the form load_state() reads; zero amounts and the custody balances that pools and stakes imply are left out."""
    state = ledger.state
    return {
        "balances": [{"account": account, "currency": currency, "amount": amount} for (account, currency), amount in sorted(state.balances.items()) if amount and account > 0],
        "allowances": [{"owner": o, "spender": s, "currency": c, "amount": amount} for (o, s, c), amount in sorted(state.allowances.items()) if amount],
        "stakes": [{"account": account, "currency": currency, "amount": amount} for (account, currency), amount in sorted(state.stakes.items()) if amount],
        "pools": [
            {
                "currency_x": pool.currency_a,
                "currency_y": pool.currency_b,
                "fee_bps": pool.fee_bps,
                "reserve_x": pool.reserve_a,
                "reserve_y": pool.reserve_b,
                "shares": {str(account): shares for account, shares in sorted(pool.shares.items())},
            }
            for _, pool in sorted(state.pools.items())
        ],
        "claims": [
            {
                "claimant": claim.claimant,
                "payer": claim.payer,
                "currency": claim.currency_id,
                "amount": claim.amount,
                "description": claim.description,
                "status": claim.status,
                "created_at": claim.created_at,
            }
            for _, claim in sorted(state.claims.items())
        ],
        "storage": {str(account): {key: to_json(value) for key, value in ledger.storage_items(account).items()} for account in sorted(state.storage) if state.storage[account]},
    }
