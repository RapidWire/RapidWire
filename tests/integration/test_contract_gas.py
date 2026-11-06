"""Gas of top-level contract calls: the prepay check, settlement of exactly used * price as one burning gas transfer, collection after a failure (the whole limit when the top-level limit runs out), and settlement the caller can no longer pay."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.values import Attachment, Currency
from rapidwire.core.allowances import approve
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.errors import ContractOutOfGas, DomainError, InsufficientBalance
from rapidwire.core.ledger import failure_output
from rapidwire.core.types import ExecutionStatus, TransferKind
from tests.integration.contract_runtime_helpers import CALLER, CONTRACT, NOW, RAPID, assert_clean, config, deploy, executions, setup_gas, transfers_of
from tests.integration.factories import balance_of, supply_of

SOURCE = """
@public
def zero() -> int:
    return 5

@public
def ping(x: int) -> int:
    return x + 1

@public
def spin() -> int:
    i = 0
    while i >= 0:
        i = i + 1
    return i

@public
def drain(amount: int) -> int:
    transfer_from(sender, self, currency("RAPID"), amount)
    return 1
"""


async def _call(engine: AsyncEngine, function: str, args: list[Any] | None = None, *, price: int = 1, **kwargs: Any) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=CONTRACT, function=function, args=args or [], config=config(gas_price=price), now=NOW, **kwargs)


async def _row(engine: AsyncEngine, execution_id: int | None) -> dict[str, Any]:
    [row] = [row for row in await executions(engine) if row["execution_id"] == execution_id]
    return row


@pytest.mark.parametrize("price", [1, 3])
async def test_settlement_burns_exactly_used_times_price(db: AsyncEngine, price: int) -> None:
    issuance = await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SOURCE)
    supply = await supply_of(db, RAPID)
    result = await _call(db, "ping", [41], price=price)
    assert result.value == 42
    assert result.gas_used == 1
    [gas] = await transfers_of(db, result.execution_id)
    assert (gas["kind"], gas["from_account"], gas["to_account"], gas["amount"]) == (TransferKind.GAS, CALLER, issuance, price)
    assert await supply_of(db, RAPID) == supply - price
    assert await balance_of(db, CALLER, RAPID) == 10_000 - price
    assert (result.gas_currency_id, result.gas_price, result.gas_cost) == (RAPID, price, price)
    await assert_clean(db)


async def test_a_replay_reports_the_gas_terms_of_the_original_call(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SOURCE)
    first = await _call(db, "ping", [1], price=3, idempotency_key="priced")
    replayed = await _call(db, "ping", [1], price=7, idempotency_key="priced")
    assert replayed.replayed
    assert (replayed.gas_price, replayed.gas_cost, replayed.gas_currency_id) == (3, 3, RAPID) == (first.gas_price, first.gas_cost, first.gas_currency_id)
    with pytest.raises(ContractOutOfGas) as failed:
        await _call(db, "spin", gas_limit=50, price=2, idempotency_key="spun")
    with pytest.raises(ContractOutOfGas) as again:
        await _call(db, "spin", gas_limit=50, price=9, idempotency_key="spun")
    assert again.value.execution_id == failed.value.execution_id
    for error in (failed.value, again.value):
        fields = failure_output(error)
        assert (str(fields["gas_price"]), str(fields["gas_currency_id"])) == ("2", str(RAPID))
    row = await _row(db, failed.value.execution_id)
    assert (row["output"]["gas_price"], row["output"]["gas_currency_id"]) == ("2", str(RAPID))


async def test_zero_gas_makes_no_gas_transfer(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, SOURCE)
    result = await _call(db, "zero")
    assert (result.value, result.gas_used) == (5, 0)
    assert await transfers_of(db, result.execution_id) == []


async def test_gas_limit_defaults_and_can_be_given(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 200_000})
    await deploy(db, CONTRACT, SOURCE)
    assert (await _call(db, "ping", [1])).gas_limit == 128
    assert (await _call(db, "ping", [1], gas_limit=7)).gas_limit == 7
    assert (await _call(db, "ping", [1], gas_limit=100_000)).gas_limit == 100_000


async def test_top_level_out_of_gas_collects_the_whole_limit(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SOURCE)
    with pytest.raises(ContractOutOfGas) as info:
        await _call(db, "spin", gas_limit=50, price=2)
    row = await _row(db, info.value.execution_id)
    assert row["status"] == ExecutionStatus.FAILED
    assert (row["output"]["kind"], row["output"]["gas_used"], row["output"]["gas_limit"]) == ("OUT_OF_GAS", "50", "50")
    [gas] = await transfers_of(db, row["execution_id"])
    assert (gas["kind"], gas["from_account"], gas["to_account"], gas["amount"]) == (TransferKind.GAS, CALLER, issuance, 100)
    assert await balance_of(db, CALLER, RAPID) == 10_000 - 100
    await assert_clean(db)


@pytest.mark.parametrize(("funds", "attach"), [(127, None), (200, 100)])
async def test_unaffordable_gas_limit_is_recorded_and_collects_nothing(db: AsyncEngine, funds: int, attach: int | None) -> None:
    await setup_gas(db, {CALLER: funds})
    await deploy(db, CONTRACT, SOURCE)
    attachment = None if attach is None else Attachment(Currency(RAPID), attach)
    with pytest.raises(InsufficientBalance) as info:
        await _call(db, "ping", [1], attach=attachment)
    row = await _row(db, info.value.execution_id)
    assert row["status"] == ExecutionStatus.FAILED
    assert (row["output"]["kind"], row["output"]["reason"], row["output"]["gas_used"]) == ("CALL_FAILED", "INSUFFICIENT_BALANCE", "0")
    assert await transfers_of(db, row["execution_id"]) == []
    assert await balance_of(db, CALLER, RAPID) == funds
    result = await _call(db, "ping", [1], gas_limit=funds - (attach or 0))
    assert result.gas_used == 1
    await assert_clean(db)


async def test_settlement_after_transfer_from_drains_the_caller(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {CALLER: 1_000})
    await deploy(db, CONTRACT, SOURCE)
    await approve(db, owner_id=CALLER, spender_id=CONTRACT, currency_id=RAPID, amount=1_000, now=NOW)
    with pytest.raises(InsufficientBalance) as info:
        await _call(db, "drain", [1_000])
    error: DomainError = info.value
    row = await _row(db, error.execution_id)
    assert row["status"] == ExecutionStatus.FAILED
    assert row["output"]["kind"] == "SETTLEMENT"
    used = int(row["output"]["gas_used"])
    assert used > 0
    [gas] = await transfers_of(db, row["execution_id"])
    assert (gas["kind"], gas["from_account"], gas["to_account"], gas["amount"]) == (TransferKind.GAS, CALLER, issuance, used)
    assert await balance_of(db, CONTRACT, RAPID) == 0
    assert await balance_of(db, CALLER, RAPID) == 1_000 - used
    partial = await _call(db, "drain", [500])
    assert await balance_of(db, CONTRACT, RAPID) == 500
    assert await balance_of(db, CALLER, RAPID) == 1_000 - used - 500 - partial.gas_used
    await assert_clean(db)
