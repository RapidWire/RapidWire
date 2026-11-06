"""Property-based test of the ledger invariants: random sequences of ledger and DEX operations over three currencies and five users must keep the database equal to a Python model and every ledger invariant intact.

Each example empties the database, seeds the world again (optionally with a few pools) and checks the state after every operation.
Three currencies and three fee tiers make pools of the same pair collide, so swaps take multi-hop and split routes; the SwapConfig is drawn per example from small values to keep allocation fast.
RAPIDWIRE_TEST_PROPERTY_EXAMPLES sets the number of examples and RAPIDWIRE_TEST_PROPERTY_STEPS the operations per example.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, note, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import pools
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.swap import SwapConfig
from rapidwire.core.types import ClaimStatus
from rapidwire.db.tables import FEE_TIERS_BPS, RATE_PPM_MAX
from rapidwire.db.types import AMOUNT_MAX
from tests.conftest import truncate_all
from tests.integration.opmodel import Harness, LedgerModel, ModelCurrency, ordered

EXAMPLES = int(os.environ.get("RAPIDWIRE_TEST_PROPERTY_EXAMPLES", "25"))
STEPS = int(os.environ.get("RAPIDWIRE_TEST_PROPERTY_STEPS", "30"))

T0 = datetime(2026, 7, 1, tzinfo=UTC)
USERS = (11, 12, 13, 14, 15)
# guild ID -> (symbol, owner, administrators)
GUILDS = {7001: ("ALPHA", 11, frozenset({13})), 7002: ("BETA", 12, frozenset({14})), 7003: ("GAMMA", 13, frozenset({11, 15}))}
CURRENCIES = tuple(GUILDS)
PAIRS = tuple((x, y) for x in CURRENCIES for y in CURRENCIES if x != y)
MISSING_CLAIM_ID = 10**9
ADVANCES = (0, 1, 1800, 3600, 86_400, 3 * 86_400, 8 * 86_400)
RATES = (0, 100, 10_000, RATE_PPM_MAX, RATE_PPM_MAX + 1)
STALE_AGES = (0, 3600, 86_400)
DELETE_WAITS = (timedelta(days=7), timedelta(days=8), timedelta(days=10), timedelta(days=1), timedelta(days=11))
EXPIRED_WAITS = (timedelta(days=10, seconds=1), timedelta(days=11), timedelta(days=30))
RATE_WAITS = (timedelta(days=7), timedelta(days=8), timedelta(days=1))
CAP_WAITS = (timedelta(hours=1), timedelta(days=1))
# The first kind is what shrinking turns irrelevant steps into, and advancing by the first delay (0) changes nothing.
KINDS = (
    "advance",
    "transfer",
    "transfer",
    "transfer_from",
    "transfer_from",
    "approve",
    "mint",
    "burn",
    "deposit",
    "deposit",
    "withdraw",
    "withdraw",
    "accrue_stale",
    "rate_request",
    "rate_apply",
    "rate_flow",
    "renounce",
    "delegation",
    "delete_request",
    "delete_finalize",
    "delete_flow",
    "delete_flow",
    "expire_flow",
    "cap_flow",
    "create",
    "claim_create",
    "claim_flow",
    "claim_flow",
    "claim_pay",
    "claim_pay",
    "claim_pay",
    "claim_cancel",
    "pool_create",
    "pool_create",
    "pool_add",
    "pool_add",
    "pool_remove",
    "pool_remove",
    "swap",
    "swap",
    "swap",
    "swap",
    "swap",
    "dissolve_flow",
)


def _amount(data: st.DataObject, available: int) -> int:
    """An amount that mostly fits within available, sometimes equals or exceeds it, and rarely is zero."""
    shape = data.draw(st.sampled_from(("within",) * 5 + ("exact", "over", "over", "zero")))
    if shape == "within":
        return data.draw(st.integers(1, max(1, available)))
    if shape == "exact":
        return max(1, available)
    if shape == "over":
        return available + data.draw(st.integers(1, 1000))
    return 0


def _near(data: st.DataObject, value: int) -> int:
    """A minimum for a predicted value: usually 0 or below it, sometimes exactly it or just above it."""
    shape = data.draw(st.sampled_from(("zero", "zero", "zero", "below", "exact", "above")))
    if shape == "zero":
        return 0
    if shape == "below":
        return data.draw(st.integers(0, max(0, value)))
    if shape == "exact":
        return max(0, value)
    return max(0, value) + data.draw(st.integers(1, 100))


def _manager(data: st.DataObject, currency_id: int) -> int:
    """A user for a management operation: usually the guild owner, sometimes an administrator or anyone."""
    _, owner, admins = GUILDS[currency_id]
    role = data.draw(st.sampled_from(("owner", "owner", "owner", "admin", "anyone")))
    if role == "owner":
        return owner
    if role == "admin":
        return data.draw(st.sampled_from(sorted(admins)))
    return data.draw(st.sampled_from(USERS))


async def _seed(harness: Harness, data: st.DataObject) -> None:
    for currency_id, supply, rate in ((7001, 1_000_000, 0), (7002, 1_000_000, 10_000), (7003, 500_000, RATE_PPM_MAX)):
        await harness.create_currency(GUILDS[currency_id][1], currency_id, supply, rate)
    await harness.set_delegation(11, 7001, enabled=True)
    for currency_id in CURRENCIES:
        owner = GUILDS[currency_id][1]
        for user_id in USERS:
            if user_id != owner:
                await harness.transfer(owner, user_id, currency_id, 100_000)
        for user_id, spender in zip(USERS, USERS[1:] + USERS[:1], strict=True):
            await harness.approve(user_id, spender, currency_id, 30_000)
    seeded = data.draw(st.lists(st.tuples(st.sampled_from(PAIRS), st.sampled_from(FEE_TIERS_BPS)), max_size=6, unique_by=lambda spec: (frozenset(spec[0]), spec[1])))
    for (currency_x, currency_y), fee_bps in seeded:
        user_id = data.draw(st.sampled_from(USERS))
        amount_x, amount_y = data.draw(st.integers(2_000, 15_000)), data.draw(st.integers(2_000, 15_000))
        note(f"seed pool {currency_x}/{currency_y} fee={fee_bps} user={user_id} amounts={amount_x}/{amount_y}")
        await harness.create_pool(user_id, currency_x, currency_y, fee_bps, amount_x, amount_y)
    assert all(key.endswith(":ok") for key in harness.outcomes)
    await harness.check()


async def _holder_step(harness: Harness, data: st.DataObject, kind: str, currency_id: int) -> None:
    model = harness.model
    user_id = data.draw(st.sampled_from(USERS))
    if kind == "withdraw" and model.stakes:
        user_id, currency_id = data.draw(st.sampled_from(sorted(model.stakes)))
    available = model.projected_stake(user_id, currency_id) if kind == "withdraw" else model.balance(user_id, currency_id)
    amount = _amount(data, available)
    note(f"{kind} user={user_id} currency={currency_id} amount={amount}")
    if kind == "transfer":
        await harness.transfer(user_id, data.draw(st.sampled_from(USERS)), currency_id, amount)
    elif kind == "burn":
        await harness.burn(user_id, currency_id, amount)
    elif kind == "deposit":
        await harness.deposit(user_id, currency_id, amount)
    elif kind == "withdraw":
        await harness.withdraw(user_id, currency_id, amount)
    else:
        spender = data.draw(st.sampled_from(USERS))
        note(f"approve spender={spender}")
        await harness.approve(user_id, spender, currency_id, amount)


async def _transfer_from_step(harness: Harness, data: st.DataObject, currency_id: int) -> None:
    model = harness.model
    spender, owner, recipient = (data.draw(st.sampled_from(USERS)) for _ in range(3))
    granted = sorted(key for key, amount in model.allowances.items() if amount > 0)
    if bool(granted) and data.draw(st.booleans()):
        owner, spender, currency_id = data.draw(st.sampled_from(granted))
    amount = _amount(data, min(model.allowances.get((owner, spender, currency_id), 0), model.balance(owner, currency_id)))
    note(f"transfer_from spender={spender} owner={owner} recipient={recipient} currency={currency_id} amount={amount}")
    await harness.transfer_from(spender, owner, recipient, currency_id, amount)


async def _cap_flow(harness: Harness, data: st.DataObject, user_id: int, currency_id: int) -> None:
    """Mint the supply up to just below the Amount limit, stake most of it and let interest run into the cap."""
    model = harness.model
    headroom = data.draw(st.integers(0, 1000))
    await harness.mint(user_id, currency_id, max(1, AMOUNT_MAX - model.supply(currency_id) - headroom))
    await harness.deposit(user_id, currency_id, max(1, model.balance(user_id, currency_id) // 2))
    model.now += data.draw(st.sampled_from(CAP_WAITS))
    if data.draw(st.booleans()):
        await harness.accrue_stale(timedelta(0))
    else:
        await harness.withdraw(user_id, currency_id, 1)


async def _delete(harness: Harness, data: st.DataObject, user_id: int, currency_id: int, waits: tuple[timedelta, ...], *, revive: bool = False) -> None:
    """Request deletion, wait and finalize, then revive the currency when revive is set and otherwise sometimes."""
    model = harness.model
    await harness.request_delete(user_id, currency_id)
    model.now += data.draw(st.sampled_from(waits))
    await harness.finalize_delete(user_id, currency_id)
    if not model.live(currency_id) and (revive or data.draw(st.booleans())):
        await harness.create_currency(GUILDS[currency_id][1], currency_id, data.draw(st.sampled_from((250_000, 0))), data.draw(st.sampled_from(RATES)))


async def _manager_step(harness: Harness, data: st.DataObject, kind: str, currency_id: int) -> None:
    model = harness.model
    user_id = _manager(data, currency_id)
    note(f"{kind} user={user_id} currency={currency_id}")
    if kind == "mint":
        await harness.mint(user_id, currency_id, _amount(data, 50_000))
    elif kind == "rate_request":
        await harness.request_rate_change(user_id, currency_id, data.draw(st.sampled_from(RATES)))
    elif kind == "rate_apply":
        await harness.apply_rate_change(user_id, currency_id)
    elif kind == "rate_flow":
        await harness.request_rate_change(user_id, currency_id, data.draw(st.sampled_from(RATES)))
        model.now += data.draw(st.sampled_from(RATE_WAITS))
        await harness.apply_rate_change(user_id, currency_id)
    elif kind == "renounce":
        await harness.renounce(user_id, currency_id)
    elif kind == "delegation":
        await harness.set_delegation(user_id, currency_id, data.draw(st.booleans()))
    elif kind == "delete_request":
        await harness.request_delete(user_id, currency_id)
    elif kind == "delete_finalize":
        await harness.finalize_delete(user_id, currency_id)
    elif kind == "delete_flow":
        await _delete(harness, data, user_id, currency_id, DELETE_WAITS)
    elif kind == "expire_flow":
        await harness.request_delete(user_id, currency_id)
        model.now += data.draw(st.sampled_from(EXPIRED_WAITS))
        await harness.finalize_delete(user_id, currency_id)
        await harness.request_delete(user_id, currency_id)
    elif kind == "cap_flow":
        await _cap_flow(harness, data, user_id, currency_id)
    elif kind == "create":
        await harness.create_currency(user_id, currency_id, data.draw(st.sampled_from((0, 1, 250_000))), data.draw(st.sampled_from(RATES)))
    else:
        raise AssertionError(f"unknown operation kind {kind}")


async def _claim_step(harness: Harness, data: st.DataObject, kind: str, currency_id: int) -> None:
    model = harness.model
    if kind == "claim_flow":
        claimant, payer = data.draw(st.sampled_from(USERS)), data.draw(st.sampled_from(USERS))
        note(f"claim_flow claimant={claimant} payer={payer} currency={currency_id}")
        await harness.create_claim(claimant, payer, currency_id, data.draw(st.integers(1, max(1, model.balance(payer, currency_id)))))
        newest = max(model.claims, default=None)
        if newest is not None and model.claims[newest].status is ClaimStatus.PENDING:
            if data.draw(st.integers(0, 3)) > 0:
                await harness.pay_claim(newest, model.claims[newest].payer_id)
            else:
                await harness.cancel_claim(newest, model.claims[newest].claimant_id)
        return
    if kind == "claim_create":
        claimant, payer = data.draw(st.sampled_from(USERS)), data.draw(st.sampled_from(USERS))
        amount = _amount(data, model.balance(payer, currency_id))
        note(f"claim_create claimant={claimant} payer={payer} currency={currency_id} amount={amount}")
        await harness.create_claim(claimant, payer, currency_id, amount)
        return
    pending = sorted(claim_id for claim_id, entry in model.claims.items() if entry.status is ClaimStatus.PENDING)
    prefer_pending = bool(pending) and data.draw(st.integers(0, 4)) > 0
    choices = pending if prefer_pending else [*sorted(model.claims), MISSING_CLAIM_ID]
    claim_id = data.draw(st.sampled_from(choices))
    entry = model.claims.get(claim_id)
    party = data.draw(st.sampled_from(("payer", "payer", "claimant", "anyone")))
    if entry is not None and party == "payer":
        user_id = entry.payer_id
    elif entry is not None and party == "claimant":
        user_id = entry.claimant_id
    else:
        user_id = data.draw(st.sampled_from(USERS))
    note(f"{kind} claim={claim_id} user={user_id}")
    if kind == "claim_pay":
        await harness.pay_claim(claim_id, user_id)
    else:
        await harness.cancel_claim(claim_id, user_id)


def _pool_target(harness: Harness, data: st.DataObject) -> tuple[int, int, int]:
    """A pair in caller order and a fee tier: usually an active pool's, sometimes any (which may name no pool)."""
    active = harness.model.active_pools()
    if active and data.draw(st.integers(0, 4)) > 0:
        entry = data.draw(st.sampled_from(active))
        pair = (entry.currency_a, entry.currency_b) if data.draw(st.booleans()) else (entry.currency_b, entry.currency_a)
        return pair[0], pair[1], entry.fee_bps
    currency_x, currency_y = data.draw(st.sampled_from(PAIRS))
    return currency_x, currency_y, data.draw(st.sampled_from(FEE_TIERS_BPS))


async def _pool_create_step(harness: Harness, data: st.DataObject) -> None:
    model = harness.model
    user_id = data.draw(st.sampled_from(USERS))
    currency_x, currency_y = data.draw(st.sampled_from(PAIRS))
    fee_bps = data.draw(st.sampled_from(FEE_TIERS_BPS))
    shape = data.draw(st.sampled_from(("normal", "normal", "normal", "tiny", "funds")))
    if shape == "normal":
        amount_x = data.draw(st.integers(1_000, max(1_000, min(model.balance(user_id, currency_x), 60_000))))
        amount_y = data.draw(st.integers(1_000, max(1_000, min(model.balance(user_id, currency_y), 60_000))))
    elif shape == "tiny":
        amount_x, amount_y = data.draw(st.integers(0, 1_100)), data.draw(st.integers(1, 1_100))
    else:
        amount_x, amount_y = _amount(data, model.balance(user_id, currency_x)), _amount(data, model.balance(user_id, currency_y))
    note(f"pool_create user={user_id} {currency_x}/{currency_y} fee={fee_bps} amounts={amount_x}/{amount_y}")
    await harness.create_pool(user_id, currency_x, currency_y, fee_bps, amount_x, amount_y)


async def _pool_add_step(harness: Harness, data: st.DataObject) -> None:
    model = harness.model
    user_id = data.draw(st.sampled_from(USERS))
    currency_x, currency_y, fee_bps = _pool_target(harness, data)
    desired_x = data.draw(st.integers(1, 3)) if data.draw(st.integers(0, 9)) == 0 else _amount(data, min(model.balance(user_id, currency_x), 50_000))
    desired_y = _amount(data, min(model.balance(user_id, currency_y), 50_000))
    a, b, desired_a, desired_b = ordered(currency_x, currency_y, desired_x, desired_y)
    entry = model.find_pool(a, b, fee_bps)
    min_x = min_y = 0
    min_shares = 1
    if entry is not None and desired_a > 0 and desired_b > 0:
        amount_a, amount_b = pools.optimal_deposit(desired_a, desired_b, entry.reserve_a, entry.reserve_b)
        minted = pools.deposit_shares(amount_a, amount_b, entry.reserve_a, entry.reserve_b, entry.total_shares)
        amount_x, amount_y = (amount_a, amount_b) if currency_x == a else (amount_b, amount_a)
        min_x, min_y = _near(data, amount_x), _near(data, amount_y)
        min_shares = max(1, _near(data, minted))
    note(f"pool_add user={user_id} {currency_x}/{currency_y} fee={fee_bps} desired={desired_x}/{desired_y} min={min_x}/{min_y} min_shares={min_shares}")
    await harness.add_liquidity(user_id, currency_x, currency_y, fee_bps, desired_x, desired_y, min_x, min_y, min_shares)


async def _pool_remove_step(harness: Harness, data: st.DataObject) -> None:
    model = harness.model
    held = sorted(model.shares)
    if held and data.draw(st.integers(0, 4)) > 0:
        pool_id, holder = data.draw(st.sampled_from(held))
        entry = model.pools[pool_id]
        currency_x, currency_y = (entry.currency_a, entry.currency_b) if data.draw(st.booleans()) else (entry.currency_b, entry.currency_a)
        fee_bps = entry.fee_bps
    else:
        holder = data.draw(st.sampled_from(USERS))
        currency_x, currency_y, fee_bps = _pool_target(harness, data)
    found = model.find_pool(min(currency_x, currency_y), max(currency_x, currency_y), fee_bps)
    position = 0 if found is None else model.shares.get((found.pool_id, holder), 0)
    if data.draw(st.integers(0, 3)) == 0:
        shares = max(1, position)
    elif data.draw(st.integers(0, 9)) == 0:
        shares = data.draw(st.integers(1, 5))
    else:
        shares = _amount(data, position)
    min_x = min_y = 0
    if found is not None and shares > 0:
        amount_a, amount_b = pools.withdrawal_amounts(shares, found.reserve_a, found.reserve_b, found.total_shares)
        amount_x, amount_y = (amount_a, amount_b) if currency_x == found.currency_a else (amount_b, amount_a)
        min_x, min_y = _near(data, amount_x), _near(data, amount_y)
    note(f"pool_remove user={holder} {currency_x}/{currency_y} fee={fee_bps} shares={shares} min={min_x}/{min_y}")
    await harness.remove_liquidity(holder, currency_x, currency_y, fee_bps, shares, min_x, min_y)


async def _swap_step(harness: Harness, data: st.DataObject) -> None:
    model = harness.model
    user_id = data.draw(st.sampled_from(USERS))
    pooled = sorted({currency_id for entry in model.active_pools() for currency_id in (entry.currency_a, entry.currency_b)})
    candidates = [pair for pair in PAIRS if set(pair) <= set(pooled)]
    from_currency, to_currency = data.draw(st.sampled_from(candidates if candidates and data.draw(st.integers(0, 4)) > 0 else PAIRS))
    depth = max((entry.reserve_of(from_currency) for entry in model.active_pools() if from_currency in (entry.currency_a, entry.currency_b)), default=1000)
    shape = data.draw(st.sampled_from(("sized", "sized", "sized", "large", "tiny", "funds")))
    if shape == "sized":
        amount_in = data.draw(st.integers(1, max(1, min(model.balance(user_id, from_currency), depth // 2))))
    elif shape == "large":
        amount_in = data.draw(st.integers(max(1, depth // 2), max(1, 3 * depth)))
    elif shape == "tiny":
        amount_in = data.draw(st.integers(0, 20))
    else:
        amount_in = _amount(data, model.balance(user_id, from_currency))
    expected, allocation = model.plan_swap(from_currency, to_currency, amount_in, harness.config)
    predicted = 0 if allocation is None else allocation.amount_out
    min_out = _near(data, predicted) if expected is None else data.draw(st.integers(0, 100))
    note(f"swap user={user_id} {from_currency}->{to_currency} amount_in={amount_in} min_out={min_out} predicted={predicted} ({expected.__name__ if expected else 'routable'})")
    if data.draw(st.integers(0, 3)) == 0:
        await harness.quote(from_currency, to_currency, amount_in)
    await harness.swap(user_id, from_currency, to_currency, amount_in, min_out)


async def _dissolve_flow(harness: Harness, data: st.DataObject) -> None:
    """Pick an active pool, sometimes have every provider withdraw all its shares first, then delete one of its currencies as its owner and revive it."""
    model = harness.model
    active = model.active_pools()
    if not active:
        await _pool_create_step(harness, data)
        return
    entry = data.draw(st.sampled_from(active))
    if data.draw(st.booleans()):
        for _, holder in sorted(key for key in model.shares if key[0] == entry.pool_id):
            await harness.remove_liquidity(holder, entry.currency_a, entry.currency_b, entry.fee_bps, model.shares[(entry.pool_id, holder)], 0, 0)
    currency_id = data.draw(st.sampled_from((entry.currency_a, entry.currency_b)))
    note(f"dissolve_flow pool={entry.pool_id} currency={currency_id}")
    await _delete(harness, data, GUILDS[currency_id][1], currency_id, (timedelta(days=8),), revive=True)


async def _pool_step(harness: Harness, data: st.DataObject, kind: str) -> None:
    if kind == "pool_create":
        await _pool_create_step(harness, data)
    elif kind == "pool_add":
        await _pool_add_step(harness, data)
    elif kind == "pool_remove":
        await _pool_remove_step(harness, data)
    elif kind == "swap":
        await _swap_step(harness, data)
    else:
        await _dissolve_flow(harness, data)


async def _step(harness: Harness, data: st.DataObject) -> None:
    kind = data.draw(st.sampled_from(KINDS))
    if kind.startswith(("pool_", "swap", "dissolve_")):
        await _pool_step(harness, data, kind)
        return
    currency_id = data.draw(st.sampled_from(CURRENCIES))
    if kind == "advance":
        harness.model.now += timedelta(seconds=data.draw(st.sampled_from(ADVANCES)))
        note(f"advance to {harness.model.now.isoformat()}")
    elif kind in ("transfer", "burn", "deposit", "withdraw", "approve"):
        await _holder_step(harness, data, kind, currency_id)
    elif kind == "transfer_from":
        await _transfer_from_step(harness, data, currency_id)
    elif kind == "accrue_stale":
        age = timedelta(seconds=data.draw(st.sampled_from(STALE_AGES)))
        note(f"accrue_stale older_than={age}")
        await harness.accrue_stale(age)
    elif kind.startswith("claim_"):
        await _claim_step(harness, data, kind, currency_id)
    else:
        await _manager_step(harness, data, kind, currency_id)


# Long operation sequences can exceed Hypothesis's per-example data budget; such examples are discarded, which is harmless here.
@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow])
@given(data=st.data())
async def test_random_operation_sequences_keep_the_ledger_consistent(engine: AsyncEngine, data: st.DataObject) -> None:
    await truncate_all(engine)
    model = LedgerModel(now=T0, currencies={currency_id: ModelCurrency(currency_id, symbol, owner, admins) for currency_id, (symbol, owner, admins) in GUILDS.items()})
    config = SwapConfig(chunk_count=data.draw(st.integers(4, 10)), max_routes=data.draw(st.integers(4, 8)))
    note(f"swap config {config}")
    harness = Harness(engine=engine, model=model, guilds=InMemoryGuildInfo(), config=config)
    for currency_id, (_, owner, admins) in GUILDS.items():
        harness.guilds.add_guild(currency_id, owner)
        for admin in admins:
            harness.guilds.add_admin(currency_id, admin)
    await _seed(harness, data)
    for _ in range(STEPS):
        await _step(harness, data)
        await harness.check()
