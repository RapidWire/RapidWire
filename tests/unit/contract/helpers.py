"""A fake Host and helpers for running hand-assembled programs."""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from rapidwire.contract import storage_codec
from rapidwire.contract.assembler import assemble
from rapidwire.contract.costs import FREE_RUN_ALLOWANCE
from rapidwire.contract.errors import CallFailed, CallFailure, HostError
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.types import Kind
from rapidwire.contract.values import Account, Attachment, Currency, Record, Scalar, make_record
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.contract.vm import ExecutionContext, ExecutionOutcome, MemoryMeter, execute

SELF = Account(1001)
SENDER = Account(2002)
RAPID = Currency(77)
NOW = 1_700_000_000


def straight_line_gas(verified: VerifiedProgram, function: str = "main") -> int:
    """Gas of running a function without jumps or calls once: its charged instructions plus the free run rule over its free instructions."""
    gas = 0
    counter = 0
    for ins in verified.function(function).code:  # type: ignore[union-attr]
        if ins[1] > 0:
            gas += ins[1]
            counter = 0
        else:
            counter -= ins[1]
            if counter >= FREE_RUN_ALLOWANCE:
                counter -= FREE_RUN_ALLOWANCE
                gas += 1
    return gas


def seeded(seed: float) -> random.Random:
    """A deterministic pseudo-random generator for reproducible tests."""
    return random.Random(seed)  # noqa: S311 - test inputs, not secrets


def program(text: str, *, limits: Limits = DEFAULT_LIMITS) -> VerifiedProgram:
    return verify(assemble(text), max_size=1_000_000, limits=limits)


@dataclass
class FakeHost:
    """In-memory Host: balances per (account, currency), storage per account, contracts per account, and a log of every host call."""

    balances: dict[tuple[Account, Currency], int] = field(default_factory=dict)
    storage: dict[Account, dict[str, bytes]] = field(default_factory=dict)
    contracts: dict[Account, VerifiedProgram] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    rng: random.Random = field(default_factory=lambda: seeded(1234))
    fail: dict[str, BaseException] = field(default_factory=dict)
    on_call: Callable[[str], None] | None = None
    canonical: bool = False
    """Store storage values in canonical form (decoded and re-encoded), as the database host does."""

    def _log(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.on_call is not None:
            self.on_call(name)
        if name in self.fail:
            raise self.fail[name]

    async def balance(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        self._log("balance", account, currency)
        return self.balances.get((account, currency), 0)

    async def currency(self, ctx: ExecutionContext, symbol: str) -> Currency:
        self._log("currency", symbol)
        if symbol == "MISSING":
            raise HostError("CurrencyNotFound", f"no currency {symbol}")
        return RAPID

    async def currency_info(self, ctx: ExecutionContext, currency: Currency) -> Record:
        self._log("currency_info", currency)
        return make_record(
            Kind.CURRENCY_INFO,
            currency=currency,
            name="Rapid",
            symbol="RAPID",
            issuer=Account(1),
            supply=10**9,
            hourly_rate_ppm=0,
            minting_renounced=False,
            delegation_enabled=False,
            delete_requested=False,
            created_at=NOW - 100,
        )

    async def transfer(self, ctx: ExecutionContext, to: Account, currency: Currency, amount: int) -> None:
        self._log("transfer", to, currency, amount)
        source = (ctx.self_account, currency)
        if amount <= 0 or self.balances.get(source, 0) < amount:
            raise HostError("InsufficientBalance", "not enough")
        self.balances[source] -= amount
        self.balances[(to, currency)] = self.balances.get((to, currency), 0) + amount

    async def transfer_from(self, ctx: ExecutionContext, owner: Account, to: Account, currency: Currency, amount: int) -> None:
        self._log("transfer_from", owner, to, currency, amount)

    async def approve(self, ctx: ExecutionContext, spender: Account, currency: Currency, amount: int) -> None:
        self._log("approve", spender, currency, amount)

    async def allowance(self, ctx: ExecutionContext, owner: Account, spender: Account, currency: Currency) -> int:
        self._log("allowance", owner, spender, currency)
        return 5

    async def create_claim(self, ctx: ExecutionContext, payer: Account, currency: Currency, amount: int, description: str) -> int:
        self._log("create_claim", payer, currency, amount, description)
        return 42

    async def pay_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        self._log("pay_claim", claim_id)

    async def cancel_claim(self, ctx: ExecutionContext, claim_id: int) -> None:
        self._log("cancel_claim", claim_id)

    async def claim_info(self, ctx: ExecutionContext, claim_id: int) -> Record:
        self._log("claim_info", claim_id)
        return make_record(
            Kind.CLAIM_INFO,
            claim_id=claim_id,
            claimant=ctx.self_account,
            payer=SENDER,
            currency=RAPID,
            amount=10,
            description="",
            status="pending",
            created_at=NOW,
        )

    async def swap(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int, min_out: int) -> int:
        self._log("swap", from_currency, to_currency, amount_in, min_out)
        return amount_in // 2

    async def add_liquidity(
        self, ctx: ExecutionContext, currency_x: Currency, currency_y: Currency, fee_bps: int, amount_x: int, amount_y: int, amount_x_min: int, amount_y_min: int, min_shares: int
    ) -> int:
        self._log("add_liquidity", currency_x, currency_y, fee_bps, amount_x, amount_y)
        return 1000

    async def remove_liquidity(self, ctx: ExecutionContext, currency_x: Currency, currency_y: Currency, fee_bps: int, shares: int, amount_x_min: int, amount_y_min: int) -> list[int]:
        self._log("remove_liquidity", currency_x, currency_y, fee_bps, shares)
        return [shares, shares * 2]

    async def quote(self, ctx: ExecutionContext, from_currency: Currency, to_currency: Currency, amount_in: int) -> Record:
        self._log("quote", from_currency, to_currency, amount_in)
        return make_record(Kind.QUOTE, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, amount_out=amount_in // 2, route_count=1, truncated=False)

    async def stake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        self._log("stake", currency, amount)

    async def unstake(self, ctx: ExecutionContext, currency: Currency, amount: int) -> None:
        self._log("unstake", currency, amount)

    async def staked(self, ctx: ExecutionContext, account: Account, currency: Currency) -> int:
        self._log("staked", account, currency)
        return 7

    async def storage_get(self, ctx: ExecutionContext, key: str) -> bytes | None:
        self._log("storage_get", key)
        return self.storage.get(ctx.self_account, {}).get(key)

    async def storage_set(self, ctx: ExecutionContext, key: str, data: bytes) -> None:
        self._log("storage_set", key, data)
        if self.canonical:
            data = storage_codec.canonicalize(data, limits=ctx.limits)
        self.storage.setdefault(ctx.self_account, {})[key] = data

    async def storage_delete(self, ctx: ExecutionContext, key: str) -> None:
        self._log("storage_delete", key)
        self.storage.get(ctx.self_account, {}).pop(key, None)

    async def read_storage(self, ctx: ExecutionContext, account: Account, key: str) -> bytes | None:
        self._log("read_storage", account, key)
        return self.storage.get(account, {}).get(key)

    async def discord_send(self, ctx: ExecutionContext, guild: int, channel: int, message: str) -> None:
        self._log("discord_send", guild, channel, message)

    async def discord_add_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        self._log("discord_add_role", guild, user, role)

    async def discord_remove_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> None:
        self._log("discord_remove_role", guild, user, role)

    async def discord_has_role(self, ctx: ExecutionContext, guild: int, user: Account, role: int) -> bool:
        self._log("discord_has_role", guild, user, role)
        return role == 1

    async def call_contract(self, ctx: ExecutionContext, target: Account, function: str, args: list[Scalar], attach: Attachment | None) -> int | str | bool:
        self._log("call_contract", target, function, tuple(args), attach)
        target_program = self.contracts.get(target)
        if target_program is None:
            raise CallFailed(CallFailure.NO_CONTRACT, f"account {target.id} has no contract")
        snapshot = (dict(self.balances), {k: dict(v) for k, v in self.storage.items()})
        try:
            outcome = await execute(target_program, function, args, host=self, context=ctx)
        except BaseException:
            self.balances, self.storage = snapshot
            raise
        return outcome.value

    async def reject_call(self, ctx: ExecutionContext, target: Account, function: str, args: list[Any], attach: Attachment | None, failure: CallFailed) -> NoReturn:
        self._log("reject_call", target, function, len(args), attach, failure.reason)
        raise failure

    def random_below(self, n: int) -> int:
        return self.rng.randrange(n)


class CheckingMeter(MemoryMeter):
    """A MemoryMeter that asserts, before every allocation, that live + pending bounds the exact accounted size."""

    checks = 0

    def allocate(self, nbytes: int) -> None:
        exact, _ = self.measure(10**12)
        assert exact <= self.live + self.pending, (exact, self.live, self.pending)
        CheckingMeter.checks += 1
        super().allocate(nbytes)


def checking_context(*, gas: int = 100_000, limits: Limits = DEFAULT_LIMITS, **fields: Any) -> ExecutionContext:
    """A top-level context whose memory meter is a CheckingMeter."""
    ctx = context(gas=gas, limits=limits)
    return dataclasses.replace(ctx, memory=CheckingMeter(limits.memory_max_bytes, ctx.gas), **fields)


def context(*, gas: int = 10_000, limits: Limits = DEFAULT_LIMITS, paid: Attachment | None = None, clock: Callable[[], float] | None = None) -> ExecutionContext:
    if clock is None:
        return ExecutionContext.top_level(self_account=SELF, sender=SENDER, paid=paid, now=NOW, gas_limit=gas, limits=limits)
    return ExecutionContext.top_level(self_account=SELF, sender=SENDER, paid=paid, now=NOW, gas_limit=gas, limits=limits, clock=clock)


async def run(
    text: str | VerifiedProgram,
    function: str = "main",
    args: list[Any] | None = None,
    *,
    gas: int = 10_000,
    limits: Limits = DEFAULT_LIMITS,
    host: FakeHost | None = None,
    ctx: ExecutionContext | None = None,
) -> ExecutionOutcome:
    verified = program(text, limits=limits) if isinstance(text, str) else text
    return await execute(verified, function, args or [], host=host or FakeHost(), context=ctx or context(gas=gas, limits=limits))


async def value(text: str, function: str = "main", args: list[Any] | None = None, **kwargs: Any) -> Any:
    return (await run(text, function, args, **kwargs)).value
