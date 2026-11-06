"""RuntimeConfig validation and construction from Settings, the stored forms of call arguments and results, argument validation, and the ledger's failure output fields."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from pydantic import ValidationError

from rapidwire.config import MAX_GAS_LIMIT, Settings
from rapidwire.contract.costs import BUILTIN_GAS, OPCODE_GAS
from rapidwire.contract.runtime import (
    CONTRACT_SWAP_CHUNK_COUNT,
    CONTRACT_SWAP_MAX_EXPANSIONS,
    MAX_CONCURRENT_DRY_RUNS,
    MAX_CONCURRENT_EXECUTIONS,
    RandomDraw,
    RuntimeConfig,
    call_input,
    dry_run_slot,
    encode_args,
    encode_result,
    execution_slot,
    failure_fields,
    validate_call_args,
)
from rapidwire.contract.values import Account, Attachment, Currency
from rapidwire.core.errors import InsufficientBalance, InvalidArgument
from rapidwire.core.ledger import LedgerUsageError, _error_output, failure_output, set_failure_output
from rapidwire.core.types import JsonObject, to_json_object


def test_from_settings_copies_the_gas_settings_and_shrinks_the_swap_search() -> None:
    settings = Settings(_env_file=None, db_password="password-1", gas_currency_id=55, gas_price=2, default_gas=10, max_gas=20, swap_chunk_count=7, swap_max_routes=3)
    runtime = RuntimeConfig.from_settings(settings)
    assert (runtime.gas_currency_id, runtime.gas_price, runtime.default_gas, runtime.max_gas, runtime.contract_max_size) == (55, 2, 10, 20, 8000)
    assert (runtime.swap.chunk_count, runtime.swap.max_routes, runtime.swap.max_expansions) == (7, 3, CONTRACT_SWAP_MAX_EXPANSIONS)
    wide = RuntimeConfig.from_settings(Settings(_env_file=None, db_password="password-1", swap_chunk_count=100))
    assert wide.swap.chunk_count == CONTRACT_SWAP_CHUNK_COUNT
    assert RuntimeConfig().swap.chunk_count == CONTRACT_SWAP_CHUNK_COUNT


@pytest.mark.parametrize(
    "overrides",
    [
        {"gas_price": 0},
        {"default_gas": 200, "max_gas": 100},
        {"gas_currency_id": True},
        {"has_role_timeout_seconds": 0},
        {"host_time_seconds": 0},
        {"gas_price": 10**38, "max_gas": 1, "default_gas": 1},
        {"contract_max_size": 8001},
        {"max_concurrent_executions": 0},
        {"max_concurrent_executions": True},
        {"max_concurrent_dry_runs": 0},
        {"max_concurrent_dry_runs": True},
    ],
)
def test_invalid_runtime_config_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must"):
        RuntimeConfig(**{"gas_currency_id": 1, **overrides})  # type: ignore[arg-type]


async def test_execution_slots_bound_concurrent_calls() -> None:
    config = RuntimeConfig(max_concurrent_executions=2)
    assert RuntimeConfig().max_concurrent_executions == MAX_CONCURRENT_EXECUTIONS
    running: list[int] = []
    peak = 0
    gate = asyncio.Event()

    async def work(index: int) -> None:
        nonlocal peak
        async with execution_slot(config):
            running.append(index)
            peak = max(peak, len(running))
            await gate.wait()
            running.remove(index)

    tasks = [asyncio.create_task(work(index)) for index in range(5)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert sorted(running) == [0, 1]
    gate.set()
    await asyncio.gather(*tasks)
    assert (peak, running) == (2, [])
    async with execution_slot(RuntimeConfig(max_concurrent_executions=1)), execution_slot(config), execution_slot(config):
        pass


async def test_dry_runs_have_their_own_smaller_slot_pool() -> None:
    config = RuntimeConfig(max_concurrent_executions=1, max_concurrent_dry_runs=1)
    assert RuntimeConfig().max_concurrent_dry_runs == MAX_CONCURRENT_DRY_RUNS < MAX_CONCURRENT_EXECUTIONS
    gate = asyncio.Event()
    entered: list[str] = []

    async def hold(slot: Callable[[RuntimeConfig], AbstractAsyncContextManager[None]], name: str) -> None:
        async with slot(config):
            entered.append(name)
            await gate.wait()

    dry_runs = [asyncio.create_task(hold(dry_run_slot, f"dry{index}")) for index in range(3)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert entered == ["dry0"]
    # A call gets its slot at once while every dry-run slot is taken and more dry runs wait.
    async with asyncio.timeout(1.0), execution_slot(config):
        entered.append("call")
    # A free call slot is not lent to the waiting dry runs.
    for _ in range(5):
        await asyncio.sleep(0)
    assert entered == ["dry0", "call"]
    gate.set()
    await asyncio.gather(*dry_runs)
    assert entered == ["dry0", "call", "dry1", "dry2"]


def test_max_gas_is_bounded() -> None:
    assert RuntimeConfig(max_gas=MAX_GAS_LIMIT).max_gas == MAX_GAS_LIMIT
    with pytest.raises(ValueError, match="max_gas must not exceed"):
        RuntimeConfig(max_gas=MAX_GAS_LIMIT + 1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, db_password="password-1", max_gas=MAX_GAS_LIMIT + 1)


def test_worst_case_failure_output_fits_well_under_the_packet_limit() -> None:
    """A call draws at most max_gas // BUILTIN_GAS["random"] values in total, at most limits.max_random_draws per execution; with every value as long as the int limit allows, the failed output stays under half of MySQL's default 64 MiB max_allowed_packet."""
    limits = RuntimeConfig().limits
    widest = -(limits.int_bound - 1)
    remaining = MAX_GAS_LIMIT // BUILTIN_GAS["random"]
    draws: list[RandomDraw] = []
    while remaining:
        count = min(limits.max_random_draws, remaining)
        draws.append(RandomDraw(len(draws), limits.max_contract_depth, 2**63 - 1, "f" * 64, (widest,) * count))
        remaining -= count
    assert len(draws) <= MAX_GAS_LIMIT // OPCODE_GAS["TRY_CALL"] + 1
    fields = failure_fields(kind="WALL_CLOCK_LIMIT", gas_used=MAX_GAS_LIMIT, gas_limit=MAX_GAS_LIMIT, random_values=(widest,) * limits.max_random_draws, draws=draws)
    size = len(json.dumps(_error_output_with(fields)))
    assert size < 32 * 1024 * 1024


def _error_output_with(fields: dict[str, object]) -> JsonObject:
    error = InsufficientBalance()
    set_failure_output(error, fields)
    return to_json_object(_error_output(error))


def test_arguments_and_results_are_stored_typed() -> None:
    args = (5, "x", False, Account(9), Currency(11))
    assert to_json_object({"args": encode_args(args)})["args"] == [
        {"type": "int", "value": "5"},
        {"type": "str", "value": "x"},
        {"type": "bool", "value": False},
        {"type": "Account", "value": "9"},
        {"type": "Currency", "value": "11"},
    ]
    assert encode_result(True) == {"value": True, "value_type": "bool"}
    assert encode_result("5") == {"value": "5", "value_type": "str"}
    stored = to_json_object(call_input(contract_id=3, function="f", args=(), attach=Attachment(Currency(4), 6), gas_limit=None))
    assert stored == {"contract_id": "3", "function": "f", "args": [], "attach": {"currency_id": "4", "amount": "6"}, "gas_limit": None}


@pytest.mark.parametrize("args", [[1.0], [None], [b"x"], [[1]], [2**256], ["x" * 4097], "abc", list(range(17))])
def test_unstorable_arguments_are_rejected(args: object) -> None:
    with pytest.raises(InvalidArgument):
        validate_call_args(args)  # type: ignore[arg-type]


def test_failure_output_fields_are_stored_beside_the_error() -> None:
    error = InsufficientBalance()
    assert failure_output(error) == {}
    assert _error_output(error) == {"error": {"code": "insufficient_balance", "message": error.message}}
    set_failure_output(error, {"gas_used": 3, "kind": "SETTLEMENT"})
    assert failure_output(error) == {"gas_used": 3, "kind": "SETTLEMENT"}
    assert _error_output(error) == {"gas_used": 3, "kind": "SETTLEMENT", "error": {"code": "insufficient_balance", "message": error.message}}
    with pytest.raises(LedgerUsageError):
        set_failure_output(error, {"error": 1})
