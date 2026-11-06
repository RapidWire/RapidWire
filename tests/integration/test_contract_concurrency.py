"""Concurrent contract calls on separate pooled connections: calls of one contract serialize on its row, so storage counters and balances stay exact, and storms mixing nested and direct calls all succeed with invariants intact."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.limits import Limits
from rapidwire.contract.runtime import dry_run_slot, execution_slot
from rapidwire.contract.values import Attachment, Currency
from rapidwire.core import ledger, staking
from rapidwire.core.contract_calls import ContractCallResult, call_contract, dry_run_contract
from rapidwire.core.currency import burn
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.errors import ContractLimitExceeded
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import balance, contract
from tests.integration.contract_runtime_helpers import NOW, RAPID, assert_clean, config, deploy, executions, setup_gas, stored, transfers_of
from tests.integration.factories import balance_of, supply_of

STORM_TIMEOUT_SECONDS = 120
A = 8001
B = 8002

COUNTER = """
@public
def bump() -> int:
    n: int = storage.get("n", 0) + 1
    storage["n"] = n
    storage["last"] = sender
    return n
"""

FORWARDER = """
@public
def forward() -> int:
    mine: int = storage.get("n", 0) + 1
    storage["n"] = mine
    v: int = call(Account(8002), "bump", [], attach=(paid.currency, paid.amount))
    return v
"""


@pytest_asyncio.fixture(loop_scope="session")
async def wide(db: AsyncEngine, migrated_url: URL) -> AsyncIterator[AsyncEngine]:
    """An engine on the emptied test database with a pool large enough for every caller to hold its own connection."""
    eng = create_engine(migrated_url, pool_size=40, max_overflow=40)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every deadlock or lock wait timeout retry of run_operation()."""
    counted: list[int] = []
    original = ledger.retry_delay

    def counting(attempt: int) -> float:
        counted.append(attempt)
        return original(attempt)

    for name, module in list(sys.modules.items()):
        if name.startswith("rapidwire.") and getattr(module, "retry_delay", None) is original:
            monkeypatch.setattr(module, "retry_delay", counting)
    return counted


async def _storm(calls: Iterable[Awaitable[Any]]) -> list[Any]:
    async with asyncio.timeout(STORM_TIMEOUT_SECONDS):
        return await asyncio.gather(*calls)


def _call(engine: AsyncEngine, caller: int, contract_id: int, function: str, amount: int) -> Awaitable[ContractCallResult]:
    return call_contract(engine, None, caller_id=caller, contract_id=contract_id, function=function, args=[], attach=Attachment(Currency(RAPID), amount), gas_limit=1_000, config=config(), now=NOW)


async def test_two_callers_on_one_contract(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 10_000, 7002: 10_000})
    await deploy(wide, B, COUNTER)
    first, second = await _storm([_call(wide, 7001, B, "bump", 10), _call(wide, 7002, B, "bump", 20)])
    assert sorted([first.value, second.value]) == [1, 2]
    assert await stored(wide, B, "n") == 2
    assert await balance_of(wide, B, RAPID) == 30
    assert await balance_of(wide, 7001, RAPID) == 10_000 - 10 - first.gas_used
    assert await balance_of(wide, 7002, RAPID) == 10_000 - 20 - second.gas_used
    assert retries == []
    await assert_clean(wide)


async def test_caller_storm_with_nested_and_direct_calls(wide: AsyncEngine, retries: list[int]) -> None:
    callers = list(range(7001, 7021))
    await setup_gas(wide, dict.fromkeys(callers, 10_000))
    await deploy(wide, A, FORWARDER)
    await deploy(wide, B, COUNTER)
    supply = await supply_of(wide, RAPID)
    plan = [(caller, A if index % 2 else B, "forward" if index % 2 else "bump") for index, caller in enumerate(callers)]
    results = await _storm(_call(wide, caller, contract_id, function, 5) for caller, contract_id, function in plan)
    assert sorted(result.value for result in results) == list(range(1, len(callers) + 1))
    assert await stored(wide, B, "n") == len(callers)
    assert await stored(wide, A, "n") == len(callers) // 2
    assert await balance_of(wide, B, RAPID) == 5 * len(callers)
    assert await balance_of(wide, A, RAPID) == 0
    burned = 0
    for result, (caller, _, _) in zip(results, plan, strict=True):
        [gas] = [row for row in await transfers_of(wide, result.execution_id) if row["kind"] == "gas"]
        assert gas["amount"] == result.gas_used
        burned += result.gas_used
        assert await balance_of(wide, caller, RAPID) == 10_000 - 5 - result.gas_used
    assert await supply_of(wide, RAPID) == supply - burned
    assert retries == []
    await assert_clean(wide)


SLOW_FORWARDER = """
@public
def forward() -> int:
    i: int = 0
    while i < 6000:
        i = i + 1
    v: int = call(Account(8002), "bump", [])
    return v
"""

GUILD = 111_111_111_111_111_111

WAITING_FORWARDER = """
@public
def forward() -> int:
    ok: bool = discord.has_role(111111111111111111, sender, 222222222222222222)
    v: int = call(Account(8002), "bump", [])
    return v

@public
def wait() -> bool:
    return discord.has_role(111111111111111111, sender, 222222222222222222)
"""


class _HeldGuildInfo(InMemoryGuildInfo):
    """A GuildInfo whose has_role() answers only once release is set; waiting counts the calls that are waiting."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.waiting = 0

    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        self.waiting += 1
        await self.release.wait()
        return True


# Bounds for held has_role() answers that only a hang reaches, so a call held in a contract stays held until the test releases it.
HELD_HAS_ROLE_SECONDS = 60.0
HELD_HOST_TIME_SECONDS = 120.0
WAIT_SECONDS = 30.0


async def _until(condition: Callable[[], bool | Awaitable[bool]], what: str) -> None:
    """Wait until condition holds, failing after WAIT_SECONDS."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        value = condition()
        if not isinstance(value, bool):
            value = await value
        if value:
            return
        assert time.monotonic() < deadline, f"{what} did not happen within {WAIT_SECONDS:.0f}s"
        await asyncio.sleep(0.01)


async def _lock_waits(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one())


async def _blocked_on_a_row_lock(engine: AsyncEngine) -> bool:
    return await _lock_waits(engine) > 0


def _plain(engine: AsyncEngine, caller: int, contract_id: int, function: str, guild_info: Any = None, *, args: list[Any] | None = None, **kwargs: Any) -> Coroutine[Any, Any, ContractCallResult]:
    return call_contract(engine, guild_info, caller_id=caller, contract_id=contract_id, function=function, args=args or [], gas_limit=100_000, config=config(**kwargs), now=NOW)


async def test_nested_call_and_direct_call_of_the_callee_do_not_deadlock(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 1_000_000, 7002: 1_000_000})
    await deploy(wide, A, SLOW_FORWARDER)
    await deploy(wide, B, COUNTER)
    first = asyncio.create_task(_plain(wide, 7001, A, "forward"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(_plain(wide, 7002, B, "bump"))
    results = await _storm([first, second])
    assert sorted(result.value for result in results) == [1, 2]
    assert retries == []
    await assert_clean(wide)


async def test_a_call_held_inside_a_contract_does_not_block_other_calls(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 1_000_000, 7002: 1_000_000, 7003: 1_000_000})
    await deploy(wide, A, WAITING_FORWARDER)
    await deploy(wide, B, COUNTER)
    await deploy(wide, 8003, COUNTER)
    await deploy(wide, 8004, WAITING_FORWARDER)
    guilds = _HeldGuildInfo()
    guilds.add_guild(GUILD, 7001)
    for account_id in (A, 8004):
        await allow_discord(wide, guilds, guild_id=GUILD, account_id=account_id, acting_user_id=7001, now=NOW)
    held = asyncio.create_task(_plain(wide, 7001, A, "forward", guilds, has_role_timeout_seconds=HELD_HAS_ROLE_SECONDS, host_time_seconds=HELD_HOST_TIME_SECONDS))
    held_config = config(has_role_timeout_seconds=HELD_HAS_ROLE_SECONDS, host_time_seconds=HELD_HOST_TIME_SECONDS)
    dry = asyncio.create_task(dry_run_contract(wide, guilds, caller_id=7003, contract_id=8004, function="wait", args=[], gas_limit=1_000, config=held_config, now=NOW))
    await _until(lambda: guilds.waiting == 2, "both calls waiting inside has_role()")
    # Both calls keep their transactions open until release is set, so finishing these calls first proves they did not wait for them.
    unrelated = await _plain(wide, 7002, 8003, "bump")
    direct = await _plain(wide, 7002, B, "bump")
    assert not held.done() and not dry.done()
    guilds.release.set()
    forwarded, dried = await _storm([held, dry])
    assert (unrelated.value, direct.value, forwarded.value) == (1, 1, 2)
    assert dried.value is True
    assert retries == []
    await assert_clean(wide)


async def test_calls_beyond_the_execution_slots_wait_before_their_transaction(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 1_000_000, 7002: 1_000_000})
    await deploy(wide, B, COUNTER)
    single = config(max_concurrent_executions=1, max_concurrent_dry_runs=1)
    async with execution_slot(single):
        waiting = asyncio.create_task(call_contract(wide, None, caller_id=7001, contract_id=B, function="bump", args=[], config=single, now=NOW))
        await asyncio.sleep(0.3)
        assert not waiting.done()
        assert [row for row in await executions(wide) if row["type"] == "contract_call"] == []
        # Dry runs have slots of their own, so a dry run does not wait for the call slots.
        dried = await asyncio.wait_for(dry_run_contract(wide, None, caller_id=7002, contract_id=B, function="bump", args=[], config=single, now=NOW), timeout=5.0)
        assert dried.value == 1
        # A call under other settings has slots of its own.
        assert (await _plain(wide, 7002, B, "bump")).value == 1
    [called] = await _storm([waiting])
    assert called.value == 2
    assert retries == []
    await assert_clean(wide)


async def test_saturated_dry_runs_do_not_block_a_call(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 1_000_000, 7002: 1_000_000})
    await deploy(wide, B, COUNTER)
    single = config(max_concurrent_executions=1, max_concurrent_dry_runs=1)
    async with dry_run_slot(single):
        dry = asyncio.create_task(dry_run_contract(wide, None, caller_id=7002, contract_id=B, function="bump", args=[], config=single, now=NOW))
        await asyncio.sleep(0.3)
        assert not dry.done()
        called = await asyncio.wait_for(call_contract(wide, None, caller_id=7001, contract_id=B, function="bump", args=[], config=single, now=NOW), timeout=5.0)
        assert called.value == 1
        assert not dry.done()
    [dried] = await _storm([dry])
    assert dried.value == 2
    assert retries == []
    await assert_clean(wide)


async def test_waiting_for_a_callee_lock_is_not_a_wall_clock_failure(wide: AsyncEngine, retries: list[int]) -> None:
    await setup_gas(wide, {7001: 1_000_000})
    await deploy(wide, A, SLOW_FORWARDER.replace("6000", "10"))
    await deploy(wide, B, COUNTER)
    async with wide.connect() as conn:
        holder = await conn.begin()
        await conn.execute(select(contract.c.account_id).where(contract.c.account_id == B).with_for_update())
        call = asyncio.create_task(_plain(wide, 7001, A, "forward", limits=Limits(wall_clock_seconds=1.0)))
        await _until(lambda: _blocked_on_a_row_lock(wide), "the nested call waiting for the callee's row lock")
        # Once the call is waiting, the sleep keeps it waiting for longer than its wall-clock limit.
        await asyncio.sleep(2.0)
        assert not call.done()
        await holder.rollback()
    result = await _storm([call])
    assert result[0].value == 1
    assert retries == []


@pytest.mark.parametrize("operation", ["burn", "stake"])
async def test_settlement_racing_a_supply_operation_of_the_caller_succeeds_after_a_retry(wide: AsyncEngine, retries: list[int], operation: str) -> None:
    await setup_gas(wide, {7001: 1_000_000})
    await deploy(wide, A, WAITING_FORWARDER)
    guilds = _HeldGuildInfo()
    guilds.add_guild(GUILD, 7001)
    await allow_discord(wide, guilds, guild_id=GUILD, account_id=A, acting_user_id=7001, now=NOW)
    supply = await supply_of(wide, RAPID)
    held = asyncio.create_task(_plain(wide, 7001, A, "wait", guilds, has_role_timeout_seconds=HELD_HAS_ROLE_SECONDS, host_time_seconds=HELD_HOST_TIME_SECONDS))
    await _until(lambda: guilds.waiting == 1, "the call waiting inside has_role()")
    if operation == "burn":
        other: asyncio.Task[Any] = asyncio.create_task(burn(wide, account_id=7001, currency_id=RAPID, amount=5, now=NOW))
    else:
        other = asyncio.create_task(staking.deposit(wide, account_id=7001, currency_id=RAPID, amount=5, now=NOW))
    await _until(lambda: _blocked_on_a_row_lock(wide), f"the {operation} waiting for the caller's locked rows")
    guilds.release.set()
    called, _ = await _storm([held, other])
    assert called.value is True
    assert len(retries) <= 2
    expected_supply = supply - called.gas_used - (5 if operation == "burn" else 0)
    assert await supply_of(wide, RAPID) == expected_supply
    assert await balance_of(wide, 7001, RAPID) == 1_000_000 - 5 - called.gas_used
    await assert_clean(wide)


TRANSFERRER = """
@public
def pay(amount: int) -> str:
    transfer(Account(7100), currency("RAPID"), amount)
    return "paid"
"""


async def test_host_time_overrun_takes_precedence_over_a_failing_builtin(wide: AsyncEngine) -> None:
    await setup_gas(wide, {7001: 1_000_000, A: 1})
    await deploy(wide, A, TRANSFERRER)
    async with wide.connect() as conn:
        holder = await conn.begin()
        await conn.execute(select(balance.c.amount).where(balance.c.account_id == A, balance.c.currency_id == RAPID).with_for_update())
        call = asyncio.create_task(_plain(wide, 7001, A, "pay", host_time_seconds=0.3, args=[1_000]))
        await _until(lambda: _blocked_on_a_row_lock(wide), "the transfer waiting for the held balance row")
        # Once the call is waiting, the sleep keeps it waiting for longer than its host-time limit.
        await asyncio.sleep(0.8)
        await holder.rollback()
    with pytest.raises(ContractLimitExceeded) as info:
        await _storm([call])
    [row] = [row for row in await executions(wide) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["kind"] == "HOST_TIME_LIMIT"
    assert "host_code" not in row["output"]
    await assert_clean(wide)


SHIELDED_FORWARDER = """
@public
def forward() -> str:
    r = try_call(Account(8002), "bump", [])
    return r.error
"""


async def test_host_time_beyond_its_limit_fails_the_whole_call(wide: AsyncEngine) -> None:
    await setup_gas(wide, {7001: 1_000_000})
    await deploy(wide, A, SHIELDED_FORWARDER)
    await deploy(wide, B, COUNTER)
    async with wide.connect() as conn:
        holder = await conn.begin()
        await conn.execute(select(contract.c.account_id).where(contract.c.account_id == B).with_for_update())
        call = asyncio.create_task(_plain(wide, 7001, A, "forward", host_time_seconds=0.3))
        await _until(lambda: _blocked_on_a_row_lock(wide), "the nested call waiting for the callee's row lock")
        # Once the call is waiting, the sleep keeps it waiting for longer than its host-time limit.
        await asyncio.sleep(0.8)
        await holder.rollback()
    with pytest.raises(ContractLimitExceeded) as info:
        await _storm([call])
    rows = [row for row in await executions(wide) if row["execution_id"] == info.value.execution_id]
    assert [(row["output"]["kind"], row["parent_execution_id"]) for row in rows] == [("HOST_TIME_LIMIT", None)]
    assert len(await executions(wide)) == 1
    [gas] = await transfers_of(wide, info.value.execution_id)
    assert gas["kind"] == "gas"
    await assert_clean(wide)
