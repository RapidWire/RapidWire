"""Property-based test of contract calls: random sequences of calls to the sample contracts and to a router that reaches them through try_call, made by several users with random attachments and gas limits and interleaved with plain transfers, keep every ledger invariant, and the RAPID supply falls by exactly the gas the calls burned.

Each example empties the database and seeds the samples again; after every step the invariants are verified and the burned gas is compared both with the gas transfers in the database and with the gas the calls reported.
RAPIDWIRE_TEST_PROPERTY_EXAMPLES sets the number of examples and RAPIDWIRE_TEST_PROPERTY_STEPS the steps per example.
"""

from __future__ import annotations

import os
from datetime import timedelta

from hypothesis import HealthCheck, given, note, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.compiler import compile_source
from rapidwire.contract.values import Account
from rapidwire.core import contracts, transfers
from rapidwire.core.errors import DomainError
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.types import TransferKind
from rapidwire.db.tables import transfer
from tests.conftest import truncate_all
from tests.integration.contract_runtime_helpers import NOW, RAPID
from tests.integration.factories import create_pool, supply_of
from tests.integration.test_contract_samples import ALICE, BOB, CLUB, CLUB_GUILD, CLUB_ROLE, DESK, GOLD, LOTTERY, VENDING, VENDING_GUILD, VENDING_ROLE, VIP, World, seed

EXAMPLES = int(os.environ.get("RAPIDWIRE_TEST_PROPERTY_EXAMPLES", "10"))
STEPS = int(os.environ.get("RAPIDWIRE_TEST_PROPERTY_STEPS", "20"))

ROUTER = 8105
CAROL = 7003
USERS = (ALICE, BOB, CAROL)
CURRENCIES = (RAPID, GOLD, VIP)
GAS_LIMITS = (None, None, 1, 5, 20, 40, 128, 1_000)
SUB_GAS = (1, 10, 30, 100, 1_000)

# Reaches a contract through try_call with the caller's attachment, refunds the attachment when the callee fails, and forwards whatever RAPID the callee paid it.
ROUTER_SOURCE = """from rapidwire_contract import *

@public
def relay(target: Account, function: str, guess: int, gas: int) -> str:
    rapid = currency("RAPID")
    if paid.currency != rapid:
        revert("Relay RAPID only")
    kept = balance(self, rapid) - paid.amount
    args: list[int] = [guess] if guess > 0 else []
    r = try_call(target, function, args, attach=(rapid, paid.amount), gas=gas)
    if not r.ok:
        transfer(sender, rapid, paid.amount)
        return "refunded " + r.error
    gained = balance(self, rapid) - kept
    if gained > 0:
        transfer(sender, rapid, gained)
    return "relayed " + str(r.value)
"""

TARGETS = ((VENDING, "buy", 0), (LOTTERY, "bet", 3), (LOTTERY, "bet", 0), (DESK, "buy", 0), (CLUB, "collect", 0), (ROUTER, "relay", 0), (8999, "buy", 0))


async def gas_transferred(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        total = (await conn.execute(select(func.coalesce(func.sum(transfer.c.amount), 0)).where(transfer.c.kind == TransferKind.GAS, transfer.c.currency_id == RAPID))).scalar_one()
    return int(total)


def amount(data: st.DataObject) -> int:
    return data.draw(st.sampled_from((1, 300, 1_000, 10_000, 100_000, 130_000, 10**12)))


async def step(world: World, data: st.DataObject, day: int) -> None:
    kind = data.draw(st.sampled_from(("transfer", "vending", "lottery", "club", "post", "desk_buy", "desk_sell", "relay", "relay", "role")))
    caller = data.draw(st.sampled_from(USERS))
    pay = (data.draw(st.sampled_from(CURRENCIES)), amount(data)) if data.draw(st.booleans()) or kind in ("vending", "lottery", "desk_buy", "desk_sell", "relay") else None
    gas = data.draw(st.sampled_from(GAS_LIMITS))
    now = NOW + timedelta(days=day)
    note(f"{kind} caller={caller} pay={pay} gas={gas} day={day}")
    try:
        if kind == "transfer":
            recipient = data.draw(st.sampled_from((*USERS, LOTTERY, DESK, CLUB)))
            currency_id = data.draw(st.sampled_from(CURRENCIES))
            await transfers.transfer(world.engine, sender_id=caller, recipient_id=recipient, currency_id=currency_id, amount=amount(data), now=now)
        elif kind == "role":
            guild, role = data.draw(st.sampled_from(((VENDING_GUILD, VENDING_ROLE), (CLUB_GUILD, CLUB_ROLE))))
            if data.draw(st.booleans()):
                world.guilds.add_role(guild, caller, role)
            else:
                world.guilds.remove_role(guild, caller, role)
        elif kind == "vending":
            await world.call(VENDING, "buy", caller=caller, pay=pay, gas=gas, now=now)
        elif kind == "lottery":
            await world.call(LOTTERY, "bet", [data.draw(st.integers(0, 7))], caller=caller, pay=pay, gas=gas, now=now)
        elif kind == "club":
            await world.call(CLUB, "collect", caller=caller, pay=pay, gas=gas, now=now)
        elif kind == "post":
            await world.call(CLUB, "post", [data.draw(st.sampled_from(("hi", "", "x" * 201)))], caller=caller, pay=pay, gas=gas, now=now)
        elif kind == "desk_buy":
            await world.call(DESK, "buy", caller=caller, pay=pay, gas=gas, now=now)
        elif kind == "desk_sell":
            await world.call(DESK, "sell", caller=caller, pay=pay, gas=gas, now=now)
        else:
            target, function, guess = data.draw(st.sampled_from(TARGETS))
            sub_gas = data.draw(st.sampled_from(SUB_GAS))
            await world.call(ROUTER, "relay", [Account(target), function, guess, sub_gas], caller=caller, pay=pay, gas=gas, now=now)
    except DomainError as error:
        note(f"failed: {error.code}: {error.message}")


# Long operation sequences can exceed Hypothesis's per-example data budget; such examples are discarded, which is harmless here.
@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow])
@given(data=st.data())
async def test_random_contract_calls_keep_the_ledger_consistent(engine: AsyncEngine, data: st.DataObject) -> None:
    await truncate_all(engine)
    world = await seed(engine)
    await contracts.register_contract(engine, account_id=ROUTER, binary=compile_source(ROUTER_SOURCE), max_size=8000, now=NOW)
    if data.draw(st.booleans()):
        await create_pool(engine, user_id=BOB, currency_x=RAPID, currency_y=GOLD, amount_x=2_000_000, amount_y=1_000_000, fee_bps=50, now=NOW)
    supply = await supply_of(engine, RAPID)
    day = 0
    for _ in range(STEPS):
        day += data.draw(st.sampled_from((0, 0, 0, 1)))
        await step(world, data, day)
        report = await verify_invariants(engine)
        assert report.violations == []
        burned = await gas_transferred(engine)
        assert burned == world.gas_burned
        assert await supply_of(engine, RAPID) == supply - burned
