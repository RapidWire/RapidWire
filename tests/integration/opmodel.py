"""A pure Python model of the ledger and DEX services for the property test, and a Harness that runs each operation against both the model and the database and checks that they agree.

The model predicts the outcome of every operation (success or the exact DomainError class), the key fields of each successful result, and the resulting wallet balances, allowances, stakes (amount, carry, last accrual), claims, currency flags, pools (reserves, shares, provider shares, active flag) and execution counts per type.
It computes pool shares, deposits, withdrawals and dissolution payouts with its own inline formulas, takes interest from compute_accrual() and applies every state transition itself.
Swap prediction reuses probe_amount(), enumerate_paths() and allocate() because tests/unit/test_routing.py checks the routing against reference enumeration and brute-force allocation oracles.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from hypothesis import event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import allowances, claims, pools, staking, transfers
from rapidwire.core import currency as cur
from rapidwire.core import swap as swaps
from rapidwire.core.errors import (
    AlreadyExists,
    AmountOverflow,
    ClaimNotFound,
    CurrencyNotFound,
    CurrencyRenounced,
    DomainError,
    InsufficientAllowance,
    InsufficientBalance,
    InsufficientLiquidity,
    InsufficientShares,
    InsufficientStake,
    InvalidAmount,
    InvalidArgument,
    InvalidState,
    NoPendingRequest,
    NoRoute,
    PermissionDenied,
    PoolAlreadyExists,
    PoolNotFound,
    RequestExpired,
    SlippageExceeded,
    TimelockActive,
)
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.routing import Allocation, PoolState, Route, allocate, enumerate_paths
from rapidwire.core.swap import SwapConfig, SwapHop, SwapRoute
from rapidwire.core.types import ClaimStatus, ExecutionStatus, ExecutionType
from rapidwire.db.tables import MINIMUM_LIQUIDITY, RATE_PPM_MAX, allowance, balance, claim, currency, execution, pool, pool_share, stake, transfer
from rapidwire.db.types import AMOUNT_MAX

type Expected = type[DomainError] | None
type BalanceKey = tuple[int, int]
type AllowanceKey = tuple[int, int, int]
type ShareKey = tuple[int, int]


@dataclass
class ModelCurrency:
    currency_id: int
    symbol: str
    owner: int
    admins: frozenset[int]
    created: bool = False
    live: bool = False
    rate: int = 0
    pending_rate: int | None = None
    rate_requested_at: datetime | None = None
    renounced: bool = False
    delete_requested_at: datetime | None = None
    delegation: bool = False
    halted: bool = False

    def state(self) -> tuple[Any, ...]:
        return (self.live, self.rate, self.pending_rate, self.rate_requested_at, self.renounced, self.delete_requested_at, self.delegation, self.halted)


@dataclass(frozen=True)
class ModelStake:
    amount: int
    carry: int
    last_accrued_at: datetime


@dataclass
class ModelClaim:
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    status: ClaimStatus = ClaimStatus.PENDING


@dataclass
class ModelPool:
    """A pool keyed by its ordered pair and fee tier; a dissolved pool keeps its row with zero reserves and shares."""

    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    reserve_a: int
    reserve_b: int
    total_shares: int
    locked_shares: int = MINIMUM_LIQUIDITY
    active: bool = True

    def state(self) -> tuple[Any, ...]:
        return (self.currency_a, self.currency_b, self.fee_bps, self.reserve_a, self.reserve_b, self.total_shares, self.locked_shares, self.active)

    def reserve_of(self, currency_id: int) -> int:
        return self.reserve_a if currency_id == self.currency_a else self.reserve_b

    def routing_state(self) -> PoolState:
        return PoolState(pool_id=self.pool_id, currency_a=self.currency_a, currency_b=self.currency_b, fee_bps=self.fee_bps, reserve_a=self.reserve_a, reserve_b=self.reserve_b)


@dataclass(frozen=True)
class LedgerState:
    """The comparable state of the ledger: non-zero user balances and allowances, stakes, claim statuses, currency flags, pools, provider shares and execution counts per type."""

    balances: dict[BalanceKey, int]
    allowances: dict[AllowanceKey, int]
    stakes: dict[BalanceKey, ModelStake]
    claims: dict[int, ClaimStatus]
    currencies: dict[int, tuple[Any, ...]]
    pools: dict[int, tuple[Any, ...]]
    shares: dict[ShareKey, int]
    executions: dict[str, int]


def _amount_error(amount: int, *, allow_zero: bool = False) -> Expected:
    if amount < 0 or (amount == 0 and not allow_zero):
        return InvalidAmount
    if amount > AMOUNT_MAX:
        return AmountOverflow
    return None


def ordered(currency_x: int, currency_y: int, value_x: int, value_y: int) -> tuple[int, int, int, int]:
    """Map a caller's (x, y) pair and values onto the pool's (a, b) order."""
    if currency_x < currency_y:
        return currency_x, currency_y, value_x, value_y
    return currency_y, currency_x, value_y, value_x


def expected_routes(allocation: Allocation, fees: dict[int, int]) -> tuple[SwapRoute, ...]:
    """The SwapRoute tuple a swap or quote reports for the allocation."""

    def hops(route: Route) -> tuple[SwapHop, ...]:
        return tuple(SwapHop(hop.pool_id, hop.currency_in, hop.currency_out, fees[hop.pool_id], hop.amount_in, hop.amount_out) for hop in route.hops)

    return tuple(SwapRoute(amount_in=route.amount_in, amount_out=route.amount_out, hops=hops(route)) for route in allocation.routes)


@dataclass
class LedgerModel:
    """The expected ledger; each operation method returns the DomainError class the service must raise, or None after applying the success to the model and setting result to the expected result fields."""

    now: datetime
    currencies: dict[int, ModelCurrency]
    balances: dict[BalanceKey, int] = field(default_factory=dict)
    allowances: dict[AllowanceKey, int] = field(default_factory=dict)
    stakes: dict[BalanceKey, ModelStake] = field(default_factory=dict)
    claims: dict[int, ModelClaim] = field(default_factory=dict)
    pools: dict[int, ModelPool] = field(default_factory=dict)
    shares: dict[ShareKey, int] = field(default_factory=dict)
    created_pool: ModelPool | None = None
    interest_executions: int = 0
    executions: dict[str, int] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)

    def balance(self, user_id: int, currency_id: int) -> int:
        return self.balances.get((user_id, currency_id), 0)

    def _add(self, user_id: int, currency_id: int, delta: int) -> None:
        self.balances[(user_id, currency_id)] = self.balance(user_id, currency_id) + delta

    def _ok(self, **fields: Any) -> None:
        self.result = fields

    def supply(self, currency_id: int) -> int:
        wallets = sum(amount for (_, c), amount in self.balances.items() if c == currency_id)
        reserves = sum(entry.reserve_of(currency_id) for entry in self.active_pools() if currency_id in (entry.currency_a, entry.currency_b))
        return wallets + self.staked(currency_id) + reserves

    def staked(self, currency_id: int) -> int:
        return sum(entry.amount for (_, c), entry in self.stakes.items() if c == currency_id)

    def live(self, currency_id: int) -> bool:
        return self.currencies[currency_id].live

    def count_execution(self, type_: ExecutionType) -> None:
        self.executions[type_.value] = self.executions.get(type_.value, 0) + 1

    def _manage_error(self, currency_id: int, user_id: int) -> Expected:
        model = self.currencies[currency_id]
        if not model.live:
            return CurrencyNotFound
        if user_id == model.owner or (model.delegation and user_id in model.admins):
            return None
        return PermissionDenied

    def _accrual(self, entry: ModelStake, model: ModelCurrency, supply: int) -> tuple[ModelStake, int, bool]:
        elapsed = staking.elapsed_microseconds(entry.last_accrued_at, self.now)
        result = staking.compute_accrual(entry.amount, entry.carry, 0 if model.halted else model.rate, elapsed)
        interest, carry, halted = result.interest, result.carry, False
        if interest > 0 and interest > AMOUNT_MAX - supply:
            interest, carry, halted = max(0, AMOUNT_MAX - supply), 0, True
        return ModelStake(entry.amount + interest, carry, max(self.now, entry.last_accrued_at)), interest, halted

    def _accrue(self, key: BalanceKey) -> int:
        """Accrue one stake and return the interest minted."""
        model = self.currencies[key[1]]
        updated, interest, halted = self._accrual(self.stakes[key], model, self.supply(key[1]))
        self.stakes[key] = updated
        if interest > 0:
            self.interest_executions += 1
        if halted:
            model.halted = True
            event("interest halted at the supply cap")
        return interest

    def transfer(self, sender: int, recipient: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount):
            return error
        if sender == recipient:
            return InvalidArgument
        if not self.live(currency_id):
            return CurrencyNotFound
        if self.balance(sender, currency_id) < amount:
            return InsufficientBalance
        self._add(sender, currency_id, -amount)
        self._add(recipient, currency_id, amount)
        self._ok(sender_id=sender, recipient_id=recipient, currency_id=currency_id, amount=amount)
        return None

    def transfer_from(self, spender: int, owner: int, recipient: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount):
            return error
        if spender == owner or owner == recipient:
            return InvalidArgument
        if not self.live(currency_id):
            return CurrencyNotFound
        key = (owner, spender, currency_id)
        if self.allowances.get(key, 0) < amount:
            return InsufficientAllowance
        if self.balance(owner, currency_id) < amount:
            return InsufficientBalance
        self.allowances[key] -= amount
        self._add(owner, currency_id, -amount)
        self._add(recipient, currency_id, amount)
        self._ok(spender_id=spender, owner_id=owner, recipient_id=recipient, currency_id=currency_id, amount=amount, allowance_remaining=self.allowances[key])
        return None

    def approve(self, owner: int, spender: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount, allow_zero=True):
            return error
        if owner == spender:
            return InvalidArgument
        if not self.live(currency_id):
            return CurrencyNotFound
        key = (owner, spender, currency_id)
        self._ok(owner_id=owner, spender_id=spender, currency_id=currency_id, old_amount=self.allowances.get(key, 0), new_amount=amount)
        self.allowances[key] = amount
        return None

    def mint(self, user_id: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount) or self._manage_error(currency_id, user_id):
            return error
        if self.currencies[currency_id].renounced:
            return CurrencyRenounced
        if self.supply(currency_id) + amount > AMOUNT_MAX:
            return AmountOverflow
        self._add(user_id, currency_id, amount)
        self._ok(currency_id=currency_id, account_id=user_id, amount=amount)
        return None

    def burn(self, user_id: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount):
            return error
        if not self.live(currency_id):
            return CurrencyNotFound
        if self.balance(user_id, currency_id) < amount:
            return InsufficientBalance
        self._add(user_id, currency_id, -amount)
        self._ok(currency_id=currency_id, account_id=user_id, amount=amount)
        return None

    def deposit(self, user_id: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount):
            return error
        if not self.live(currency_id):
            return CurrencyNotFound
        if self.balance(user_id, currency_id) < amount:
            return InsufficientBalance
        key = (user_id, currency_id)
        interest = 0
        if key in self.stakes:
            interest = self._accrue(key)
            entry = self.stakes[key]
            self.stakes[key] = ModelStake(entry.amount + amount, entry.carry, entry.last_accrued_at)
        else:
            self.stakes[key] = ModelStake(amount, 0, self.now)
        self._add(user_id, currency_id, -amount)
        self._ok(account_id=user_id, currency_id=currency_id, amount=amount, stake_amount=self.stakes[key].amount, interest=interest)
        return None

    def withdraw(self, user_id: int, currency_id: int, amount: int) -> Expected:
        if error := _amount_error(amount):
            return error
        if not self.live(currency_id):
            return CurrencyNotFound
        key = (user_id, currency_id)
        if key not in self.stakes:
            return InsufficientStake
        projected, _, _ = self._accrual(self.stakes[key], self.currencies[currency_id], self.supply(currency_id))
        if projected.amount < amount:
            return InsufficientStake
        interest = self._accrue(key)
        entry = self.stakes[key]
        remaining = entry.amount - amount
        if remaining == 0:
            del self.stakes[key]
        else:
            self.stakes[key] = ModelStake(remaining, entry.carry, entry.last_accrued_at)
        self._add(user_id, currency_id, amount)
        self._ok(account_id=user_id, currency_id=currency_id, amount=amount, stake_amount=remaining, interest=interest)
        return None

    def projected_stake(self, user_id: int, currency_id: int) -> int:
        key = (user_id, currency_id)
        if key not in self.stakes:
            return 0
        return self._accrual(self.stakes[key], self.currencies[currency_id], self.supply(currency_id))[0].amount

    def accrue_stale(self, older_than: timedelta) -> tuple[int, int]:
        """Apply accrue_stale_stakes() and return how many stakes it credits with interest and how many it advances without interest."""
        cutoff = self.now - older_than
        credited = without_interest = 0
        for currency_id, user_id in sorted((c, u) for u, c in self.stakes):
            model = self.currencies[currency_id]
            if not model.live or model.rate == 0 or model.halted or self.stakes[(user_id, currency_id)].last_accrued_at > cutoff:
                continue
            if self._accrue((user_id, currency_id)) > 0:
                credited += 1
            else:
                without_interest += 1
        return credited, without_interest

    def request_rate_change(self, user_id: int, currency_id: int, new_rate: int) -> Expected:
        if not 0 <= new_rate <= RATE_PPM_MAX:
            return InvalidArgument
        if error := self._manage_error(currency_id, user_id):
            return error
        model = self.currencies[currency_id]
        if model.renounced:
            return CurrencyRenounced
        if model.rate_requested_at is not None:
            return InvalidState
        model.pending_rate, model.rate_requested_at = new_rate, self.now
        self._ok(currency_id=currency_id, new_rate_ppm=new_rate, requested_at=self.now, apply_after=self.now + cur.RATE_CHANGE_TIMELOCK)
        return None

    def apply_rate_change(self, user_id: int, currency_id: int) -> Expected:
        if error := self._manage_error(currency_id, user_id):
            return error
        model = self.currencies[currency_id]
        if model.renounced:
            return CurrencyRenounced
        if model.rate_requested_at is None or model.pending_rate is None:
            return NoPendingRequest
        if self.now - model.rate_requested_at < cur.RATE_CHANGE_TIMELOCK:
            return TimelockActive
        keys = sorted(key for key in self.stakes if key[1] == currency_id)
        minted = sum(1 for key in keys if self._accrue(key) > 0)
        self._ok(currency_id=currency_id, old_rate_ppm=model.rate, new_rate_ppm=model.pending_rate, stakes_accrued=len(keys), interest_executions=minted)
        model.rate, model.pending_rate, model.rate_requested_at = model.pending_rate, None, None
        return None

    def renounce(self, user_id: int, currency_id: int) -> Expected:
        if error := self._manage_error(currency_id, user_id):
            return error
        model = self.currencies[currency_id]
        if model.renounced:
            return CurrencyRenounced
        model.renounced, model.pending_rate, model.rate_requested_at = True, None, None
        self._ok(currency_id=currency_id)
        return None

    def set_delegation(self, user_id: int, currency_id: int, enabled: bool) -> Expected:
        model = self.currencies[currency_id]
        if not model.live:
            return CurrencyNotFound
        if user_id != model.owner:
            return PermissionDenied
        self._ok(currency_id=currency_id, enabled=enabled, changed=model.delegation != enabled)
        model.delegation = enabled
        return None

    def request_delete(self, user_id: int, currency_id: int) -> Expected:
        if error := self._manage_error(currency_id, user_id):
            return error
        model = self.currencies[currency_id]
        if cur.delete_request_active(model.delete_requested_at, self.now):
            return InvalidState
        model.delete_requested_at = self.now
        self._ok(currency_id=currency_id, requested_at=self.now, finalize_from=self.now + cur.DELETE_TIMELOCK, finalize_until=self.now + cur.DELETE_EXPIRY)
        return None

    def finalize_delete(self, user_id: int, currency_id: int) -> Expected:
        """Apply finalize_delete(); a lapsed request is cleared even though the operation fails with RequestExpired."""
        if error := self._manage_error(currency_id, user_id):
            return error
        model = self.currencies[currency_id]
        requested = model.delete_requested_at
        if requested is None:
            return NoPendingRequest
        if self.now - requested < cur.DELETE_TIMELOCK:
            return TimelockActive
        if self.now - requested > cur.DELETE_EXPIRY:
            model.delete_requested_at = None
            return RequestExpired
        canceled = 0
        for entry in self.claims.values():
            if entry.currency_id == currency_id and entry.status is ClaimStatus.PENDING:
                entry.status = ClaimStatus.CANCELED
                canceled += 1
        holders = sum(1 for (_, c), amount in self.balances.items() if c == currency_id and amount > 0) + (1 if self.staked(currency_id) > 0 else 0)
        dissolved = [entry for entry in self.active_pools() if currency_id in (entry.currency_a, entry.currency_b)]
        self._ok(
            currency_id=currency_id,
            burned_amount=self.supply(currency_id),
            balances_burned=holders + len(dissolved),
            stakes_burned=sum(1 for key in self.stakes if key[1] == currency_id),
            claims_canceled=canceled,
            allowances_deleted=sum(1 for key in self.allowances if key[2] == currency_id),
            pools_dissolved=len(dissolved),
        )
        for dissolving in dissolved:
            self._dissolve(dissolving, currency_id)
        self.allowances = {key: amount for key, amount in self.allowances.items() if key[2] != currency_id}
        self.balances = {key: amount for key, amount in self.balances.items() if key[1] != currency_id}
        self.stakes = {key: entry for key, entry in self.stakes.items() if key[1] != currency_id}
        model.live, model.delete_requested_at, model.pending_rate, model.rate_requested_at = False, None, None, None
        return None

    def _dissolve(self, entry: ModelPool, currency_id: int) -> None:
        """Dissolve a pool of a deleted currency: its own reserve is burned, the partner reserve goes to the providers pro rata or, with no provider shares left, back to the partner's issuance."""
        partner = entry.currency_b if entry.currency_a == currency_id else entry.currency_a
        holdings = {account_id: shares for (pool_id, account_id), shares in self.shares.items() if pool_id == entry.pool_id}
        if holdings:
            reserve, outstanding = entry.reserve_of(partner), sum(holdings.values())
            payouts = {account_id: reserve * shares // outstanding for account_id, shares in holdings.items()}
            largest = min(holdings, key=lambda account_id: (-holdings[account_id], account_id))
            payouts[largest] += reserve - sum(payouts.values())
            for account_id, amount in payouts.items():
                self._add(account_id, partner, amount)
            event(f"dissolution: pool with {'several providers' if len(holdings) > 1 else 'one provider'}")
        else:
            event("dissolution: pool without provider shares")
        self.shares = {key: shares for key, shares in self.shares.items() if key[0] != entry.pool_id}
        entry.reserve_a = entry.reserve_b = entry.total_shares = entry.locked_shares = 0
        entry.active = False

    def create_currency(self, user_id: int, currency_id: int, initial_supply: int, rate: int) -> Expected:
        if not 0 <= rate <= RATE_PPM_MAX:
            return InvalidArgument
        if error := _amount_error(initial_supply, allow_zero=True):
            return error
        model = self.currencies[currency_id]
        if user_id != model.owner:
            return PermissionDenied
        if model.live:
            return AlreadyExists
        self._ok(currency_id=currency_id, symbol=model.symbol, hourly_rate_ppm=rate, initial_supply=initial_supply, revived=model.created)
        model.created, model.live, model.rate, model.pending_rate, model.rate_requested_at = True, True, rate, None, None
        model.renounced, model.delete_requested_at, model.delegation, model.halted = False, None, False, False
        if initial_supply > 0:
            self._add(user_id, currency_id, initial_supply)
        return None

    def create_claim(self, claimant: int, payer: int, currency_id: int, amount: int) -> Expected:
        """Check a claim creation; the caller records the created claim with add_claim()."""
        if error := _amount_error(amount):
            return error
        if claimant == payer:
            return InvalidArgument
        if not self.live(currency_id):
            return CurrencyNotFound
        self._ok(claimant_id=claimant, payer_id=payer, currency_id=currency_id, amount=amount)
        return None

    def add_claim(self, claim_id: int, claimant: int, payer: int, currency_id: int, amount: int) -> None:
        if claim_id in self.claims:
            raise AssertionError(f"claim ID {claim_id} was handed out twice")
        self.claims[claim_id] = ModelClaim(claimant, payer, currency_id, amount)

    def pay_claim(self, claim_id: int, payer: int) -> Expected:
        entry = self.claims.get(claim_id)
        if entry is None:
            return ClaimNotFound
        if entry.payer_id != payer:
            return PermissionDenied
        if entry.status is not ClaimStatus.PENDING:
            return InvalidState
        if not self.live(entry.currency_id):
            return CurrencyNotFound
        if self.balance(payer, entry.currency_id) < entry.amount:
            return InsufficientBalance
        self._add(payer, entry.currency_id, -entry.amount)
        self._add(entry.claimant_id, entry.currency_id, entry.amount)
        entry.status = ClaimStatus.PAID
        self._ok(claim_id=claim_id, payer_id=payer, claimant_id=entry.claimant_id, currency_id=entry.currency_id, amount=entry.amount)
        return None

    def cancel_claim(self, claim_id: int, user_id: int) -> Expected:
        entry = self.claims.get(claim_id)
        if entry is None:
            return ClaimNotFound
        if user_id not in (entry.payer_id, entry.claimant_id):
            return PermissionDenied
        if entry.status is not ClaimStatus.PENDING:
            return InvalidState
        entry.status = ClaimStatus.CANCELED
        self._ok(claim_id=claim_id, canceled_by=user_id)
        return None

    def active_pools(self) -> list[ModelPool]:
        """The active pools in ascending pool_id."""
        return [self.pools[pool_id] for pool_id in sorted(self.pools) if self.pools[pool_id].active]

    def find_pool(self, currency_a: int, currency_b: int, fee_bps: int) -> ModelPool | None:
        """The active pool of the ordered pair and fee tier, if any."""
        found = [entry for entry in self.active_pools() if (entry.currency_a, entry.currency_b, entry.fee_bps) == (currency_a, currency_b, fee_bps)]
        assert len(found) <= 1, f"two active pools share pair {currency_a}/{currency_b} and tier {fee_bps}"
        return found[0] if found else None

    def _pool_fields(self, entry: ModelPool, currency_x: int, currency_y: int, amount_a: int, amount_b: int, shares: int, provider_shares: int) -> dict[str, Any]:
        return {
            "pool_id": entry.pool_id,
            "currency_a": entry.currency_a,
            "currency_b": entry.currency_b,
            "fee_bps": entry.fee_bps,
            "currency_x": currency_x,
            "currency_y": currency_y,
            "amount_a": amount_a,
            "amount_b": amount_b,
            "shares": shares,
            "provider_shares": provider_shares,
            "total_shares": entry.total_shares,
            "reserve_a": entry.reserve_a,
            "reserve_b": entry.reserve_b,
        }

    def create_pool(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, amount_x: int, amount_y: int) -> Expected:
        """Check and apply a pool creation; the pool's ID is learned from the service's result through add_created_pool()."""
        if error := _amount_error(amount_x) or _amount_error(amount_y):
            return error
        a, b, amount_a, amount_b = ordered(currency_x, currency_y, amount_x, amount_y)
        if not self.live(a) or not self.live(b):
            return CurrencyNotFound
        if self.find_pool(a, b, fee_bps) is not None:
            return PoolAlreadyExists
        total = math.isqrt(amount_a * amount_b)
        if total <= MINIMUM_LIQUIDITY:
            return InsufficientLiquidity
        if self.balance(user_id, a) < amount_a or self.balance(user_id, b) < amount_b:
            return InsufficientBalance
        self._add(user_id, a, -amount_a)
        self._add(user_id, b, -amount_b)
        created = ModelPool(0, a, b, fee_bps, amount_a, amount_b, total)
        self.created_pool = created
        provider = total - MINIMUM_LIQUIDITY
        fields = self._pool_fields(created, currency_x, currency_y, amount_a, amount_b, provider, provider)
        del fields["pool_id"]
        self._ok(**fields, locked_shares=MINIMUM_LIQUIDITY)
        self.shares[(0, user_id)] = provider
        return None

    def add_created_pool(self, pool_id: int) -> None:
        """Record the pool create_pool() predicted under the ID the service handed out."""
        created, self.created_pool = self.created_pool, None
        assert created is not None
        if pool_id in self.pools or pool_id <= 0:
            raise AssertionError(f"pool ID {pool_id} was handed out twice")
        created.pool_id = pool_id
        self.pools[pool_id] = created
        self.shares = {(pool_id if key[0] == 0 else key[0], key[1]): shares for key, shares in self.shares.items()}

    def add_liquidity(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, desired_x: int, desired_y: int, min_x: int, min_y: int, min_shares: int) -> Expected:
        if error := _amount_error(desired_x) or _amount_error(desired_y):
            return error
        a, b, desired_a, desired_b = ordered(currency_x, currency_y, desired_x, desired_y)
        min_a, min_b = ordered(currency_x, currency_y, min_x, min_y)[2:]
        if not self.live(a) or not self.live(b):
            return CurrencyNotFound
        entry = self.find_pool(a, b, fee_bps)
        if entry is None:
            return PoolNotFound
        amount_a, amount_b = desired_a, desired_a * entry.reserve_b // entry.reserve_a
        if amount_b > desired_b:
            amount_a, amount_b = desired_b * entry.reserve_a // entry.reserve_b, desired_b
        minted = min(amount_a * entry.total_shares // entry.reserve_a, amount_b * entry.total_shares // entry.reserve_b)
        if amount_a == 0 or amount_b == 0 or minted == 0:
            return InsufficientLiquidity
        if amount_a < min_a or amount_b < min_b or minted < min_shares:
            return SlippageExceeded
        if max(entry.reserve_a + amount_a, entry.reserve_b + amount_b, entry.total_shares + minted) > AMOUNT_MAX:
            return AmountOverflow
        if self.balance(user_id, a) < amount_a or self.balance(user_id, b) < amount_b:
            return InsufficientBalance
        self._add(user_id, a, -amount_a)
        self._add(user_id, b, -amount_b)
        entry.reserve_a += amount_a
        entry.reserve_b += amount_b
        entry.total_shares += minted
        key = (entry.pool_id, user_id)
        self.shares[key] = self.shares.get(key, 0) + minted
        self._ok(**self._pool_fields(entry, currency_x, currency_y, amount_a, amount_b, minted, self.shares[key]))
        return None

    def remove_liquidity(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, shares: int, min_x: int, min_y: int) -> Expected:
        if error := _amount_error(shares):
            return error
        a, b, min_a, min_b = ordered(currency_x, currency_y, min_x, min_y)
        if not self.live(a) or not self.live(b):
            return CurrencyNotFound
        entry = self.find_pool(a, b, fee_bps)
        if entry is None:
            return PoolNotFound
        key = (entry.pool_id, user_id)
        held = self.shares.get(key, 0)
        if held < shares:
            return InsufficientShares
        amount_a, amount_b = shares * entry.reserve_a // entry.total_shares, shares * entry.reserve_b // entry.total_shares
        if amount_a == 0 or amount_b == 0:
            return InsufficientLiquidity
        if amount_a < min_a or amount_b < min_b:
            return SlippageExceeded
        assert entry.total_shares - shares >= entry.locked_shares and amount_a < entry.reserve_a and amount_b < entry.reserve_b
        self._add(user_id, a, amount_a)
        self._add(user_id, b, amount_b)
        entry.reserve_a -= amount_a
        entry.reserve_b -= amount_b
        entry.total_shares -= shares
        if held == shares:
            del self.shares[key]
        else:
            self.shares[key] = held - shares
        self._ok(**self._pool_fields(entry, currency_x, currency_y, amount_a, amount_b, shares, held - shares))
        return None

    def plan_swap(self, from_currency: int, to_currency: int, amount_in: int, config: SwapConfig) -> tuple[Expected, Allocation | None]:
        """Predict a swap or quote up to the routing step without changing the model: the error so far, or the allocation on the current reserves."""
        if error := _amount_error(amount_in):
            return error, None
        if not self.live(from_currency) or not self.live(to_currency):
            return CurrencyNotFound, None
        states = [entry.routing_state() for entry in self.active_pools()]
        search = enumerate_paths(states, from_currency, to_currency, max_paths=config.max_routes, probe_amount=swaps.probe_amount(amount_in, config.chunk_count), max_expansions=config.max_expansions)
        if not search.paths:
            return NoRoute, None
        used = {pool_id for path in search.paths for pool_id in path.pool_ids}
        allocation = allocate(search.paths, [state for state in states if state.pool_id in used], amount_in, chunk_count=config.chunk_count)
        if allocation.is_empty:
            return NoRoute, None
        assert allocation.amount_in == amount_in
        return None, allocation

    def fees(self) -> dict[int, int]:
        return {pool_id: entry.fee_bps for pool_id, entry in self.pools.items()}

    def swap(self, user_id: int, from_currency: int, to_currency: int, amount_in: int, min_out: int, config: SwapConfig) -> Expected:
        """Check and apply a swap; the failures take precedence in the order CurrencyNotFound, InsufficientBalance, NoRoute, SlippageExceeded, AmountOverflow."""
        expected, allocation = self.plan_swap(from_currency, to_currency, amount_in, config)
        if expected in (InvalidAmount, AmountOverflow, CurrencyNotFound):
            return expected
        if self.balance(user_id, from_currency) < amount_in:
            return InsufficientBalance
        if expected is not None:
            return expected
        assert allocation is not None
        if allocation.amount_out < min_out:
            return SlippageExceeded
        if any(reserve > AMOUNT_MAX for pair in allocation.final_reserves.values() for reserve in pair):
            return AmountOverflow
        for pool_id, (reserve_a, reserve_b) in allocation.final_reserves.items():
            entry = self.pools[pool_id]
            assert entry.active and reserve_a > 0 and reserve_b > 0
            entry.reserve_a, entry.reserve_b = reserve_a, reserve_b
        self._add(user_id, from_currency, -amount_in)
        self._add(user_id, to_currency, allocation.amount_out)
        self._ok(from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, min_out=min_out, amount_out=allocation.amount_out, routes=expected_routes(allocation, self.fees()))
        return None

    def state(self) -> LedgerState:
        executions = {**self.executions, ExecutionType.INTEREST.value: self.interest_executions}
        return LedgerState(
            balances={key: amount for key, amount in self.balances.items() if amount != 0},
            allowances={key: amount for key, amount in self.allowances.items() if amount != 0},
            stakes=dict(self.stakes),
            claims={claim_id: entry.status for claim_id, entry in self.claims.items()},
            currencies={currency_id: model.state() for currency_id, model in self.currencies.items()},
            pools={pool_id: entry.state() for pool_id, entry in self.pools.items()},
            shares=dict(self.shares),
            executions={name: count for name, count in executions.items() if count},
        )


async def read_state(engine: AsyncEngine, currency_ids: list[int]) -> LedgerState:
    """Read the same comparable state from the database; a currency without a row reads as not live with default flags."""
    async with engine.connect() as conn:
        balances = {(row.account_id, row.currency_id): int(row.amount) for row in await conn.execute(select(balance).where(balance.c.account_id > 0, balance.c.amount != 0))}
        allowance_rows = await conn.execute(select(allowance).where(allowance.c.amount != 0))
        allowed = {(row.owner_id, row.spender_id, row.currency_id): int(row.amount) for row in allowance_rows}
        stakes = {(row.account_id, row.currency_id): ModelStake(int(row.amount), int(row.carry), row.last_accrued_at) for row in await conn.execute(select(stake))}
        claim_rows = {row.claim_id: ClaimStatus(row.status) for row in await conn.execute(select(claim.c.claim_id, claim.c.status))}
        currency_rows = {row["currency_id"]: row for row in (await conn.execute(select(currency))).mappings()}
        pool_rows = {
            row.pool_id: (row.currency_a, row.currency_b, row.fee_bps, int(row.reserve_a), int(row.reserve_b), int(row.total_shares), int(row.locked_shares), row.dissolved_at is None)
            for row in await conn.execute(select(pool))
        }
        share_rows = {(row.pool_id, row.account_id): int(row.shares) for row in await conn.execute(select(pool_share))}
        counts = {row.type: int(row.executions) for row in await conn.execute(select(execution.c.type, func.count().label("executions")).group_by(execution.c.type))}
    currencies: dict[int, tuple[Any, ...]] = {}
    for currency_id in currency_ids:
        row = currency_rows.get(currency_id)
        if row is None:
            currencies[currency_id] = (False, 0, None, None, False, None, False, False)
            continue
        currencies[currency_id] = (
            row["deleted_at"] is None,
            row["hourly_rate_ppm"],
            row["pending_rate_ppm"],
            row["rate_change_requested_at"],
            bool(row["minting_renounced"]),
            row["delete_requested_at"],
            bool(row["delegation_enabled"]),
            bool(row["interest_halted"]),
        )
    return LedgerState(balances, allowed, stakes, claim_rows, currencies, pool_rows, share_rows, counts)


async def assert_failed_execution(engine: AsyncEngine, error: DomainError) -> None:
    """Assert that the error names a failed execution whose stored error matches it and that the execution moved nothing."""
    assert error.execution_id is not None, f"{type(error).__name__} carries no execution ID"
    async with engine.connect() as conn:
        row = (await conn.execute(select(execution.c.status, execution.c.output).where(execution.c.execution_id == error.execution_id))).one()
        moved = (await conn.execute(select(func.count()).select_from(transfer).where(transfer.c.execution_id == error.execution_id))).scalar_one()
    assert row.status == ExecutionStatus.FAILED.value
    assert row.output["error"]["code"] == error.code
    assert moved == 0


@dataclass
class Harness:
    """Runs one operation against the model and the database, checks the outcome class, the result fields, the failed execution record and, on demand, the full state and the invariants."""

    engine: AsyncEngine
    model: LedgerModel
    guilds: InMemoryGuildInfo
    config: SwapConfig
    outcomes: dict[str, int] = field(default_factory=dict)

    async def run(self, name: str, expected: Expected, call: Callable[[], Awaitable[Any]], execution: ExecutionType | None) -> Any:
        """Await call() and check that it succeeds with the model's expected result fields when expected is None and raises exactly expected otherwise; execution is the type of the execution the call records, if any; returns the result or None."""
        if execution is not None:
            self.model.count_execution(execution)
        fields = dict(self.model.result) if expected is None else {}
        self.model.result = {}
        try:
            result = await call()
        except DomainError as error:
            assert type(error) is expected, f"{name}: expected {expected.__name__ if expected else 'success'}, got {type(error).__name__}: {error.message}"
            await assert_failed_execution(self.engine, error)
            self._count(f"{name}:{error.code}")
            return None
        assert expected is None, f"{name}: expected {expected.__name__}, but it succeeded"
        actual = {key: getattr(result, key) for key in fields}
        assert actual == fields, f"{name}: result fields differ from the model"
        self._count(f"{name}:ok")
        return result

    def _count(self, key: str) -> None:
        self.outcomes[key] = self.outcomes.get(key, 0) + 1
        event(key)

    async def check(self) -> None:
        """Compare the database with the model and run the invariant verifier."""
        report = await verify_invariants(self.engine)
        assert report.ok, report.violations
        actual = await read_state(self.engine, sorted(self.model.currencies))
        expected = self.model.state()
        assert actual.balances == expected.balances
        assert all(amount >= 0 for amount in actual.balances.values())
        assert actual.allowances == expected.allowances
        assert actual.stakes == expected.stakes
        assert all(entry.amount > 0 for entry in actual.stakes.values())
        assert actual.claims == expected.claims
        assert actual.currencies == expected.currencies
        assert actual.pools == expected.pools
        assert actual.shares == expected.shares
        assert all(shares > 0 for shares in actual.shares.values())
        assert actual.executions == expected.executions
        for report_row in report.currencies:
            assert report_row.supply == self.model.supply(report_row.currency_id)
        reported = [(item.pool_id, item.active, item.reserve_a, item.reserve_b) for item in report.pools]
        assert reported == [(pool_id, entry.active, entry.reserve_a, entry.reserve_b) for pool_id, entry in sorted(self.model.pools.items())]

    async def transfer(self, sender: int, recipient: int, currency_id: int, amount: int) -> None:
        expected = self.model.transfer(sender, recipient, currency_id, amount)
        await self.run(
            "transfer", expected, lambda: transfers.transfer(self.engine, sender_id=sender, recipient_id=recipient, currency_id=currency_id, amount=amount, now=self.model.now), ExecutionType.TRANSFER
        )

    async def transfer_from(self, spender: int, owner: int, recipient: int, currency_id: int, amount: int) -> None:
        expected = self.model.transfer_from(spender, owner, recipient, currency_id, amount)
        await self.run(
            "transfer_from",
            expected,
            lambda: transfers.transfer_from(self.engine, spender_id=spender, owner_id=owner, recipient_id=recipient, currency_id=currency_id, amount=amount, now=self.model.now),
            ExecutionType.TRANSFER_FROM,
        )

    async def approve(self, owner: int, spender: int, currency_id: int, amount: int) -> None:
        expected = self.model.approve(owner, spender, currency_id, amount)
        await self.run(
            "approve", expected, lambda: allowances.approve(self.engine, owner_id=owner, spender_id=spender, currency_id=currency_id, amount=amount, now=self.model.now), ExecutionType.APPROVE
        )

    async def mint(self, user_id: int, currency_id: int, amount: int) -> None:
        expected = self.model.mint(user_id, currency_id, amount)
        await self.run("mint", expected, lambda: cur.mint(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, amount=amount, now=self.model.now), ExecutionType.MINT)

    async def burn(self, user_id: int, currency_id: int, amount: int) -> None:
        expected = self.model.burn(user_id, currency_id, amount)
        await self.run("burn", expected, lambda: cur.burn(self.engine, account_id=user_id, currency_id=currency_id, amount=amount, now=self.model.now), ExecutionType.BURN)

    async def deposit(self, user_id: int, currency_id: int, amount: int) -> None:
        expected = self.model.deposit(user_id, currency_id, amount)
        result = await self.run("deposit", expected, lambda: staking.deposit(self.engine, account_id=user_id, currency_id=currency_id, amount=amount, now=self.model.now), ExecutionType.STAKE_DEPOSIT)
        if result is not None:
            assert (result.interest_execution_id is not None) == (result.interest > 0)

    async def withdraw(self, user_id: int, currency_id: int, amount: int) -> None:
        expected = self.model.withdraw(user_id, currency_id, amount)
        result = await self.run(
            "withdraw", expected, lambda: staking.withdraw(self.engine, account_id=user_id, currency_id=currency_id, amount=amount, now=self.model.now), ExecutionType.STAKE_WITHDRAW
        )
        if result is not None:
            assert (result.interest_execution_id is not None) == (result.interest > 0)

    async def accrue_stale(self, older_than: timedelta) -> None:
        expected = self.model.accrue_stale(older_than)
        result = await self.run("accrue_stale", None, lambda: staking.accrue_stale_stakes(self.engine, older_than=older_than, now=self.model.now), None)
        assert (result.credited, result.without_interest, result.failed) == (*expected, 0)

    async def request_rate_change(self, user_id: int, currency_id: int, new_rate: int) -> None:
        expected = self.model.request_rate_change(user_id, currency_id, new_rate)
        await self.run(
            "rate_request",
            expected,
            lambda: cur.request_rate_change(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, new_rate_ppm=new_rate, now=self.model.now),
            ExecutionType.RATE_CHANGE_REQUEST,
        )

    async def apply_rate_change(self, user_id: int, currency_id: int) -> None:
        expected = self.model.apply_rate_change(user_id, currency_id)
        minted = self.model.result.pop("interest_executions", None)
        result = await self.run(
            "rate_apply", expected, lambda: cur.apply_rate_change(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, now=self.model.now), ExecutionType.RATE_CHANGE_APPLY
        )
        if result is not None:
            assert len(result.interest_execution_ids) == minted

    async def renounce(self, user_id: int, currency_id: int) -> None:
        expected = self.model.renounce(user_id, currency_id)
        await self.run("renounce", expected, lambda: cur.renounce(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, now=self.model.now), ExecutionType.CURRENCY_RENOUNCE)

    async def set_delegation(self, user_id: int, currency_id: int, enabled: bool) -> None:
        expected = self.model.set_delegation(user_id, currency_id, enabled)
        toggle = cur.enable_delegation if enabled else cur.disable_delegation
        kind = ExecutionType.DELEGATION_ENABLE if enabled else ExecutionType.DELEGATION_DISABLE
        await self.run("delegation", expected, lambda: toggle(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, now=self.model.now), kind)

    async def request_delete(self, user_id: int, currency_id: int) -> None:
        expected = self.model.request_delete(user_id, currency_id)
        await self.run(
            "delete_request", expected, lambda: cur.request_delete(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, now=self.model.now), ExecutionType.CURRENCY_DELETE_REQUEST
        )

    async def finalize_delete(self, user_id: int, currency_id: int) -> None:
        expected = self.model.finalize_delete(user_id, currency_id)
        result = await self.run(
            "delete_finalize", expected, lambda: cur.finalize_delete(self.engine, self.guilds, currency_id=currency_id, user_id=user_id, now=self.model.now), ExecutionType.CURRENCY_DELETE_FINALIZE
        )
        if result is not None and result.pools_dissolved > 0:
            self._count("delete_finalize:dissolved_pools")

    async def create_currency(self, user_id: int, currency_id: int, initial_supply: int, rate: int) -> None:
        expected = self.model.create_currency(user_id, currency_id, initial_supply, rate)
        symbol = self.model.currencies[currency_id].symbol
        result = await self.run(
            "create",
            expected,
            lambda: cur.create_currency(
                self.engine, self.guilds, guild_id=currency_id, user_id=user_id, name=symbol.capitalize(), symbol=symbol, initial_supply=initial_supply, hourly_rate_ppm=rate, now=self.model.now
            ),
            ExecutionType.CURRENCY_CREATE,
        )
        if result is not None and result.revived:
            self._count("create:revived")

    async def create_claim(self, claimant: int, payer: int, currency_id: int, amount: int) -> None:
        expected = self.model.create_claim(claimant, payer, currency_id, amount)
        result = await self.run(
            "claim_create",
            expected,
            lambda: claims.create_claim(self.engine, claimant_id=claimant, payer_id=payer, currency_id=currency_id, amount=amount, now=self.model.now),
            ExecutionType.CLAIM_CREATE,
        )
        if result is not None:
            self.model.add_claim(result.claim_id, claimant, payer, currency_id, amount)

    async def pay_claim(self, claim_id: int, payer: int) -> None:
        expected = self.model.pay_claim(claim_id, payer)
        await self.run("claim_pay", expected, lambda: claims.pay_claim(self.engine, claim_id=claim_id, payer_id=payer, now=self.model.now), ExecutionType.CLAIM_PAY)

    async def cancel_claim(self, claim_id: int, user_id: int) -> None:
        expected = self.model.cancel_claim(claim_id, user_id)
        await self.run("claim_cancel", expected, lambda: claims.cancel_claim(self.engine, claim_id=claim_id, user_id=user_id, now=self.model.now), ExecutionType.CLAIM_CANCEL)

    async def create_pool(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, amount_x: int, amount_y: int) -> None:
        expected = self.model.create_pool(user_id, currency_x, currency_y, fee_bps, amount_x, amount_y)
        result = await self.run(
            "pool_create",
            expected,
            lambda: pools.create_pool(self.engine, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, amount_x=amount_x, amount_y=amount_y, now=self.model.now),
            ExecutionType.POOL_CREATE,
        )
        if result is not None:
            self.model.add_created_pool(result.pool_id)

    async def add_liquidity(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, desired_x: int, desired_y: int, min_x: int, min_y: int, min_shares: int) -> None:
        expected = self.model.add_liquidity(user_id, currency_x, currency_y, fee_bps, desired_x, desired_y, min_x, min_y, min_shares)
        await self.run(
            "pool_add",
            expected,
            lambda: pools.add_liquidity(
                self.engine,
                user_id=user_id,
                currency_x=currency_x,
                currency_y=currency_y,
                fee_bps=fee_bps,
                amount_x_desired=desired_x,
                amount_y_desired=desired_y,
                amount_x_min=min_x,
                amount_y_min=min_y,
                min_shares=min_shares,
                now=self.model.now,
            ),
            ExecutionType.LIQUIDITY_ADD,
        )

    async def remove_liquidity(self, user_id: int, currency_x: int, currency_y: int, fee_bps: int, shares: int, min_x: int, min_y: int) -> None:
        expected = self.model.remove_liquidity(user_id, currency_x, currency_y, fee_bps, shares, min_x, min_y)
        result = await self.run(
            "pool_remove",
            expected,
            lambda: pools.remove_liquidity(
                self.engine, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, shares=shares, amount_x_min=min_x, amount_y_min=min_y, now=self.model.now
            ),
            ExecutionType.LIQUIDITY_REMOVE,
        )
        if result is not None and result.provider_shares == 0:
            self._count("pool_remove:position_closed")

    async def quote(self, from_currency: int, to_currency: int, amount_in: int) -> None:
        """Check quote_swap() against the model's prediction; a quote records no execution and changes nothing."""
        expected, allocation = self.model.plan_swap(from_currency, to_currency, amount_in, self.config)
        try:
            quote = await swaps.quote_swap(self.engine, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, config=self.config)
        except DomainError as error:
            assert type(error) is expected, f"quote: expected {expected.__name__ if expected else 'success'}, got {type(error).__name__}: {error.message}"
            assert error.execution_id is None
            self._count(f"quote:{error.code}")
            return
        assert expected is None and allocation is not None, f"quote: expected {expected.__name__ if expected else 'an allocation'}, but it succeeded"
        assert (quote.amount_in, quote.amount_out, quote.routes, quote.truncated) == (amount_in, allocation.amount_out, expected_routes(allocation, self.model.fees()), False)
        self._count("quote:ok")

    async def swap(self, user_id: int, from_currency: int, to_currency: int, amount_in: int, min_out: int) -> None:
        expected = self.model.swap(user_id, from_currency, to_currency, amount_in, min_out, self.config)
        result = await self.run(
            "swap",
            expected,
            lambda: swaps.swap(self.engine, user_id=user_id, from_currency=from_currency, to_currency=to_currency, amount_in=amount_in, min_out=min_out, config=self.config, now=self.model.now),
            ExecutionType.SWAP,
        )
        if result is None:
            return
        if len(result.routes) > 1:
            self._count("swap:split over several routes")
        hops = max(len(route.hops) for route in result.routes)
        if hops > 1:
            self._count("swap:multi-hop route")
        event(f"swap: longest route has {hops} hops")
        if len({hop.pool_id for route in result.routes for hop in route.hops}) < sum(len(route.hops) for route in result.routes):
            self._count("swap:routes sharing a pool")
