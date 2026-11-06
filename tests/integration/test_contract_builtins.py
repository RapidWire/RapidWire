"""Every builtin family through LedgerHost against the real ledger: context values, balances and currencies, transfers and allowances, claims, staking, the DEX, storage with its limits, and the hashing and random helpers, all acting as the contract's own account."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract import runtime
from rapidwire.contract.costs import BUILTIN_GAS, route_search_gas
from rapidwire.contract.values import Account, Attachment, Currency
from rapidwire.core import claims, contracts, staking
from rapidwire.core import swap as swap_module
from rapidwire.core.allowances import approve, get_allowance
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.errors import ContractOutOfGas, ContractRuntimeFailure, DomainError, NoRoute
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, execution, pool_share
from tests.integration.contract_runtime_helpers import CALLER, NOW, RAPID, assert_clean, config, deploy, executions, setup_gas, stored, transfers_of
from tests.integration.factories import balance_of, create_currency, create_pool, mint

C = 8001
OTHER = 8002
GEM = 900_002
FRIEND = 7050
LP = 7060

SOURCE = """
@public
def context() -> str:
    return str(now()) + "|" + str(int(paid.currency)) + "|" + str(paid.amount) + "|" + str(int(sender)) + "|" + str(int(self))

@public
def info() -> str:
    c = currency("GEM")
    i = currency_info(c)
    return i.symbol + "|" + i.name + "|" + str(i.supply) + "|" + str(int(i.issuer)) + "|" + str(balance(self, c)) + "|" + str(i.created_at)

@public
def no_currency() -> int:
    c = currency("NOPE")
    return 1

@public
def pay(to: Account, amount: int) -> int:
    transfer(to, currency("GEM"), amount)
    return balance(to, currency("GEM"))

@public
def pull(owner: Account, amount: int) -> int:
    transfer_from(owner, self, currency("GEM"), amount)
    return allowance(owner, self, currency("GEM"))

@public
def allow(spender: Account, amount: int) -> int:
    approve(spender, currency("GEM"), amount)
    return allowance(self, spender, currency("GEM"))

@public
def claim(payer: Account, amount: int) -> int:
    return create_claim(payer, currency("GEM"), amount, "fee")

@public
def claim_state(claim_id: int) -> str:
    c = claim_info(claim_id)
    return c.status + "|" + str(c.amount) + "|" + c.description + "|" + str(int(c.claimant)) + "|" + str(int(c.payer))

@public
def settle(claim_id: int) -> str:
    pay_claim(claim_id)
    return claim_info(claim_id).status

@public
def drop_claim(claim_id: int) -> str:
    cancel_claim(claim_id)
    return claim_info(claim_id).status

@public
def stake_it(amount: int) -> int:
    stake(currency("GEM"), amount)
    return staked(self, currency("GEM"))

@public
def unstake_it(amount: int) -> int:
    unstake(currency("GEM"), amount)
    return staked(self, currency("GEM"))

@public
def dex(amount: int) -> str:
    q = quote(currency("GEM"), currency("RAPID"), amount)
    out = swap(currency("GEM"), currency("RAPID"), amount, q.amount_out)
    return str(q.amount_out) + "|" + str(out) + "|" + str(q.route_count) + "|" + str(q.truncated)

@public
def provide(x: int, y: int) -> int:
    return add_liquidity(currency("GEM"), currency("RAPID"), 100, x, y, 0, 0, 1)

@public
def take_out(shares: int) -> str:
    r = remove_liquidity(currency("GEM"), currency("RAPID"), 100, shares, 0, 0)
    return str(r[0]) + "|" + str(r[1])

@public
def keep(key: str, v: int) -> int:
    storage[key] = v
    storage["list"] = [1, 2, 3]
    return storage.get(key, -1)

@public
def load(key: str) -> int:
    return storage.get(key, -1)

@public
def forget(key: str) -> int:
    del storage[key]
    return storage.get(key, -1)

@public
def peek(account: Account, key: str) -> int:
    return read_storage(account, key, -1)

@public
def fill(text: str, count: int) -> int:
    i = 0
    while i < count:
        storage["k" + str(i)] = text
        i = i + 1
    return i

@public
def digest(text: str) -> str:
    return sha256(text)

@public
def dice() -> int:
    return random(1, 6) + random(10, 10)
"""


async def _setup(engine: AsyncEngine) -> None:
    await setup_gas(engine, {CALLER: 1_000_000, C: 100_000})
    await create_currency(engine, currency_id=GEM, symbol="GEM", name="Gem", created_by=4_000, now=NOW)
    await mint(engine, GEM, C, 50_000, now=NOW)
    await mint(engine, GEM, CALLER, 10_000, now=NOW)
    await deploy(engine, C, SOURCE)
    await deploy(engine, OTHER, SOURCE)


async def _call(engine: AsyncEngine, function: str, args: list[Any] | None = None, **kwargs: Any) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=C, function=function, args=args or [], gas_limit=50_000, config=config(), now=NOW, **kwargs)


async def _principal(engine: AsyncEngine, execution_id: int) -> list[tuple[Any, ...]]:
    return [(row["currency_id"], row["from_account"], row["to_account"], row["amount"]) for row in await transfers_of(engine, execution_id) if row["kind"] == TransferKind.PRINCIPAL]


async def test_context_values(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "context", attach=Attachment(Currency(GEM), 7))
    assert result.value == f"{int(NOW.timestamp())}|{GEM}|7|{CALLER}|{C}"
    assert (await _call(db, "context")).value == f"{int(NOW.timestamp())}|0|0|{CALLER}|{C}"


async def test_balances_and_currencies(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "info")
    assert result.value == f"GEM|Gem|60000|4000|50000|{int(NOW.timestamp())}"
    with pytest.raises(ContractRuntimeFailure) as info:
        await _call(db, "no_currency")
    [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["host_code"] == "currency_not_found"


async def test_transfer_transfer_from_approve_and_allowance(db: AsyncEngine) -> None:
    await _setup(db)
    paid = await _call(db, "pay", [Account(FRIEND), 300])
    assert paid.value == 300
    assert await _principal(db, paid.execution_id) == [(GEM, C, FRIEND, 300)]
    await approve(db, owner_id=CALLER, spender_id=C, currency_id=GEM, amount=1_000, now=NOW)
    pulled = await _call(db, "pull", [Account(CALLER), 400])
    assert pulled.value == 600
    assert await _principal(db, pulled.execution_id) == [(GEM, CALLER, C, 400)]
    allowed = await _call(db, "allow", [Account(FRIEND), 55])
    assert allowed.value == 55
    assert await get_allowance(db, C, FRIEND, GEM) == 55
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "pull", [Account(CALLER), 601])
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "pay", [Account(-5), 1])
    assert await balance_of(db, C, GEM) == 50_000 - 300 + 400
    await assert_clean(db)


async def test_claims(db: AsyncEngine) -> None:
    await _setup(db)
    created = await _call(db, "claim", [Account(FRIEND), 250])
    claim_id = created.value
    assert isinstance(claim_id, int)
    assert (await _call(db, "claim_state", [claim_id])).value == f"pending|250|fee|{C}|{FRIEND}"
    [row] = [row for row in await executions(db) if row["execution_id"] == created.execution_id]
    assert (await claims.get_claim(db, claim_id)).created_execution_id == row["execution_id"]
    incoming = await claims.create_claim(db, claimant_id=FRIEND, payer_id=C, currency_id=GEM, amount=120, now=NOW)
    settled = await _call(db, "settle", [incoming.claim_id])
    assert settled.value == "paid"
    assert await _principal(db, settled.execution_id) == [(GEM, C, FRIEND, 120)]
    second = await claims.create_claim(db, claimant_id=FRIEND, payer_id=C, currency_id=GEM, amount=5, now=NOW)
    assert (await _call(db, "drop_claim", [second.claim_id])).value == "canceled"
    assert (await _call(db, "drop_claim", [claim_id])).value == "canceled"
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "settle", [second.claim_id])
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "claim_state", [10**12])
    await assert_clean(db)


async def test_stake_and_unstake(db: AsyncEngine) -> None:
    await _setup(db)
    staked = await _call(db, "stake_it", [2_000])
    assert staked.value == 2_000
    assert await _principal(db, staked.execution_id) == [(GEM, C, STAKE_CUSTODY_ACCOUNT_ID, 2_000)]
    info = await staking.get_stake(db, C, GEM)
    assert info is not None
    assert info.amount == 2_000
    left = await _call(db, "unstake_it", [500])
    assert left.value == 1_500
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "unstake_it", [5_000])
    await assert_clean(db)


async def test_swap_quote_and_liquidity(db: AsyncEngine) -> None:
    await _setup(db)
    await create_pool(db, user_id=LP, currency_x=GEM, currency_y=RAPID, amount_x=100_000, amount_y=100_000, fee_bps=100, now=NOW)
    swapped = await _call(db, "dex", [1_000])
    quoted, out, routes, truncated = str(swapped.value).split("|")
    assert quoted == out
    assert (int(routes), truncated) == (1, "False")
    moves = await _principal(db, swapped.execution_id)
    assert [(currency_id, amount) for currency_id, _, _, amount in moves] == [(GEM, 1_000), (RAPID, int(out))]
    provided = await _call(db, "provide", [10_000, 20_000])
    shares = provided.value
    assert isinstance(shares, int)
    assert shares > 0
    async with db.connect() as conn:
        held = (await conn.execute(select(pool_share.c.shares).where(pool_share.c.account_id == C))).scalar_one()
    assert held == shares
    removed = await _call(db, "take_out", [shares])
    gem_out, rapid_out = (int(part) for part in str(removed.value).split("|"))
    assert gem_out > 0
    assert rapid_out > 0
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "take_out", [1])
    async with db.connect() as conn:
        types = set((await conn.execute(select(execution.c.type))).scalars())
    assert ExecutionType.SWAP.value not in types
    assert ExecutionType.LIQUIDITY_ADD.value not in types
    await assert_clean(db)


QUOTE_SOURCE = """
@public
def q(amount: int) -> int:
    r = quote(currency("GEM"), currency("RAPID"), amount)
    return r.amount_out

@public
def s(amount: int) -> int:
    return swap(currency("GEM"), currency("RAPID"), amount, 0)
"""


async def _gas_with_and_without_route_charge(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, function: str, amount: int) -> tuple[int, int]:
    await deploy(db, 8030, QUOTE_SOURCE)
    gas: list[int] = []
    for charged in (True, False):
        if not charged:
            monkeypatch.setattr(runtime, "route_search_gas", lambda expansions, chunk_hops=0: 0)
        key = f"{function}-{charged}"
        result = await call_contract(db, None, caller_id=CALLER, contract_id=8030, function=function, args=[amount], gas_limit=50_000, config=config(), now=NOW, idempotency_key=key)
        gas.append(result.gas_used)
    return gas[0], gas[1]


@pytest.mark.parametrize("function", ["q", "s"])
async def test_quote_and_swap_pay_their_base_cost_plus_route_search_gas(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, function: str) -> None:
    await _setup(db)
    await create_pool(db, user_id=LP, currency_x=GEM, currency_y=RAPID, amount_x=100_000, amount_y=100_000, fee_bps=100, now=NOW)
    await create_pool(db, user_id=LP, currency_x=GEM, currency_y=RAPID, amount_x=50_000, amount_y=50_000, fee_bps=50, now=NOW)
    await mint(db, GEM, 8030, 10_000, now=NOW)
    swap_config = config().swap
    quoted = await swap_module.quote_swap(db, from_currency=GEM, to_currency=RAPID, amount_in=1_000, config=swap_config)
    plan = await swap_module.plan_swap(db, from_currency=GEM, to_currency=RAPID, amount_in=1_000, config=swap_config)
    assert quoted.expansions == plan.expansions == 2
    assert quoted.chunk_hops == swap_module.allocation_chunk_hops(plan.paths, 1_000, swap_config.chunk_count) == 20
    charged, uncharged = await _gas_with_and_without_route_charge(db, monkeypatch, function, 1_000)
    assert charged - uncharged == route_search_gas(2, 20)
    assert BUILTIN_GAS["quote" if function == "q" else "swap"] == 20


async def test_a_quote_that_finds_no_route_still_pays_route_search_gas(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(db)
    for fee_bps in (100, 50):
        await create_pool(db, user_id=LP, currency_x=GEM, currency_y=RAPID, amount_x=100_000, amount_y=100, fee_bps=fee_bps, now=NOW)
    await deploy(db, 8030, QUOTE_SOURCE)
    searched: list[tuple[int, int]] = []
    with pytest.raises(NoRoute):
        await swap_module.quote_swap(db, from_currency=GEM, to_currency=RAPID, amount_in=1, config=config().swap, on_search=lambda expansions, hops: searched.append((expansions, hops)))
    assert searched == [(2, 2)]
    used: list[int] = []
    for charged in (True, False):
        if not charged:
            monkeypatch.setattr(runtime, "route_search_gas", lambda expansions, chunk_hops=0: 0)
        with pytest.raises(ContractRuntimeFailure) as info:
            await call_contract(db, None, caller_id=CALLER, contract_id=8030, function="q", args=[1], gas_limit=1_000, config=config(), now=NOW)
        [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
        assert row["output"]["host_code"] == "no_route"
        used.append(int(row["output"]["gas_used"]))
    assert used[0] - used[1] == route_search_gas(2, 2)


async def test_route_search_gas_out_of_gas_moves_nothing(db: AsyncEngine) -> None:
    await _setup(db)
    await create_pool(db, user_id=LP, currency_x=GEM, currency_y=RAPID, amount_x=100_000, amount_y=100_000, fee_bps=100, now=NOW)
    await deploy(db, 8030, QUOTE_SOURCE)
    await mint(db, GEM, 8030, 10_000, now=NOW)
    with pytest.raises(ContractOutOfGas) as info:
        await call_contract(db, None, caller_id=CALLER, contract_id=8030, function="s", args=[1_000], gas_limit=30, config=config(), now=NOW)
    assert [row["kind"] for row in await transfers_of(db, info.value.execution_id)] == [TransferKind.GAS]
    assert await balance_of(db, 8030, GEM) == 10_000
    await assert_clean(db)


async def test_storage_get_set_delete_and_read_storage(db: AsyncEngine) -> None:
    await _setup(db)
    assert (await _call(db, "keep", ["a", 41])).value == 41
    assert await stored(db, C, "a") == 41
    assert await stored(db, C, "list") == [1, 2, 3]
    assert (await _call(db, "load", ["a"])).value == 41
    assert (await _call(db, "load", ["zz"])).value == -1
    other = await call_contract(db, None, caller_id=CALLER, contract_id=OTHER, function="peek", args=[Account(C), "a"], gas_limit=1_000, config=config(), now=NOW)
    assert other.value == 41
    assert (await _call(db, "forget", ["a"])).value == -1
    assert await stored(db, C, "a") is None
    assert (await _call(db, "forget", ["never"])).value == -1
    info = await contracts.get_contract(db, C)
    assert info is not None
    assert info.storage_keys == 1


@pytest.mark.parametrize(("args", "host_code"), [(["x" * 4000, 70], "contract_storage_limit"), (["k" * 70, 1], None)])
async def test_storage_limits(db: AsyncEngine, args: list[Any], host_code: str | None) -> None:
    await _setup(db)
    if host_code is None:
        with pytest.raises(ContractRuntimeFailure) as info:
            await _call(db, "keep", args)
    else:
        with pytest.raises(ContractRuntimeFailure) as info:
            await _call(db, "fill", args)
    error: DomainError = info.value
    [row] = [row for row in await executions(db) if row["execution_id"] == error.execution_id]
    assert row["output"].get("host_code") == host_code
    stored_info = await contracts.get_contract(db, C)
    assert stored_info is not None
    assert (stored_info.storage_keys, stored_info.storage_bytes) == (0, 0)
    await assert_clean(db)


async def test_sha256_and_random(db: AsyncEngine) -> None:
    await _setup(db)
    assert (await _call(db, "digest", ["abc"])).value == hashlib.sha256(b"abc").hexdigest()
    rolled = await _call(db, "dice")
    assert isinstance(rolled.value, int)
    first = rolled.value - 10
    assert 1 <= first <= 6
    assert rolled.random_values == (first, 10)
    [row] = [row for row in await executions(db) if row["execution_id"] == rolled.execution_id]
    assert row["output"]["random"] == [str(first), "10"]
