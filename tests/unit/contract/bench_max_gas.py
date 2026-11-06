"""The benchmark behind the max_gas setting: the worst-case gas per second of the VM, and the largest gas limit a call that uses all of it can be given while finishing well inside the wall-clock limit.

The families are every program of bench.py (pure VM work on a FakeHost) plus contracts on MockLedger that spend their gas on host calls: ledger reads, transfers, approvals, storage writes of the largest values, sha256 of the longest strings, Discord operations, nested contract calls with and without attachments, and swaps and quotes routed over several pools and fee tiers.
Each family loops one operation until its gas runs out; the slowest family in gas per second is the worst case.
Database time is not part of it: the wall-clock limit leaves out time spent in database work, which has its own host time limit.
max_gas = round_down(worst_gas_per_second * WALL_CLOCK_SECONDS * SAFETY_FACTOR), where round_down keeps only the leading digit.
Run directly (python -m tests.unit.contract.bench_max_gas [gas]) to print the gas per second of every family and the resulting max_gas.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from rapidwire.contract.compiler import compile_source
from rapidwire.contract.limits import WALL_CLOCK_SECONDS, Limits
from rapidwire.contract.mock import MockLedger
from rapidwire.contract.verifier import verify
from rapidwire.db.tables import FEE_TIERS_BPS
from tests.unit.contract.bench import BENCHMARKS, measure

SAFETY_FACTOR = 0.5
LIMITS = Limits(wall_clock_seconds=600.0)

CONTRACT = 1000
CALLER = 2000
OTHER = 3000
CALLEE = 4000
# The relay chain occupies RELAY_BASE .. RELAY_BASE + RELAY_HOPS; with the looping contract on top it reaches the contract depth limit.
RELAY_BASE = 5000
RELAY_HOPS = 4
RAPID_ID = 1
GOLD_ID = 2
BRIDGE_IDS = (3, 4, 5)
GUILD = 600_001
CHANNEL = 600_002
ROLE = 600_003
FUNDS = 10**30

PRELUDE = f"""from rapidwire_contract import *

GUILD = {GUILD}
CHANNEL = {CHANNEL}
ROLE = {ROLE}
BIG = "{"q" * 4093}"
LONGEST = "{"q" * 4096}"
"""

CALLEE_SOURCE = """from rapidwire_contract import *

@public
def ping() -> int:
    return 1

@public
def fail() -> int:
    revert("no")
"""

RELAY_SOURCE = """from rapidwire_contract import *

@public
def hop(depth: int) -> int:
    if depth == 0:
        return 1
    return int(call(Account(int(self) + 1), "hop", [depth - 1]))
"""


def looping(setup: str, body: str) -> str:
    """A contract whose main() runs setup once and then repeats body until the gas runs out."""
    return PRELUDE + f"\n@public\ndef main() -> int:\n    rapid = Currency({RAPID_ID})\n    gold = Currency({GOLD_ID})\n{setup}\n    while True:\n        {body}\n"


HOST_BENCHMARKS: dict[str, str] = {
    "host_balance": looping("", "balance(sender, rapid)"),
    "host_currency_lookup": looping("", 'currency("GOLD")'),
    "host_currency_info": looping("", "currency_info(rapid)"),
    "host_allowance": looping("", "allowance(sender, self, rapid)"),
    "host_staked": looping("", "staked(sender, rapid)"),
    "host_has_role": looping("", "discord.has_role(GUILD, sender, ROLE)"),
    "host_read_storage": looping("", f'read_storage(Account({OTHER}), "k", 0)'),
    "host_storage_get_big_str": looping('    storage["k"] = BIG', 'storage.get("k", "")'),
    "host_transfer": looping("", "transfer(sender, rapid, 1)"),
    "host_transfer_from": looping("", f"transfer_from(sender, Account({OTHER}), rapid, 1)"),
    "host_approve": looping("", "approve(sender, rapid, 5)"),
    "host_storage_set_big_str": looping("", 'storage["k"] = BIG'),
    "host_storage_set_int_list": looping("    values = [i % 100 for i in range(1024)]", 'storage["k"] = values'),
    "host_storage_set_dict": looping("    entries = {str(i): i for i in range(300)}", 'storage["k"] = entries'),
    "host_storage_delete": looping("", 'del storage["k"]'),
    "host_sha256_longest": looping("", "sha256(LONGEST)"),
    "host_discord_send": looping("", 'discord.send(GUILD, CHANNEL, "x")'),
    "host_discord_add_role": looping("", "discord.add_role(GUILD, sender, ROLE)"),
    "host_call": looping("", f'call(Account({CALLEE}), "ping")'),
    "host_try_call_revert": looping("", f'try_call(Account({CALLEE}), "fail")'),
    "host_call_attach": looping("", f'call(Account({CALLEE}), "ping", [], attach=(rapid, 1))'),
    "host_call_chain": looping("", f'call(Account({RELAY_BASE}), "hop", [{RELAY_HOPS}])'),
    "dex_swap": looping("", "swap(rapid, gold, 1_000_000, 0)"),
    "dex_swap_small": looping("", "swap(rapid, gold, 10, 0)"),
    "dex_quote": looping("", "quote(rapid, gold, 1_000_000)"),
}


def _ledger(name: str) -> MockLedger:
    ledger = MockLedger(limits=LIMITS, seed=1, gas_currency_id=RAPID_ID, max_gas=10**12)
    ledger.add_currency(RAPID_ID, "RAPID")
    ledger.add_currency(GOLD_ID, "GOLD")
    for currency_id in BRIDGE_IDS:
        ledger.add_currency(currency_id, f"BRIDGE{currency_id}")
    for account in (CONTRACT, CALLER, OTHER):
        ledger.set_balance(account, RAPID_ID, FUNDS)
    ledger.set_allowance(CALLER, CONTRACT, RAPID_ID, FUNDS)
    ledger.set_stake(CALLER, RAPID_ID, 1000)
    # Every fee tier between every pair of RAPID, GOLD and the bridge currencies, so a route search meets many one- and multi-hop paths.
    hub = (RAPID_ID, GOLD_ID, *BRIDGE_IDS)
    for index, currency_x in enumerate(hub):
        for currency_y in hub[index + 1 :]:
            for fee_bps in FEE_TIERS_BPS:
                ledger.add_pool(currency_x, currency_y, fee_bps, 10**15, 10**15)
    ledger.allow_discord(GUILD, CONTRACT)
    ledger.grant_role(GUILD, CALLER, ROLE)
    ledger.set_storage(OTHER, "k", 7)
    ledger.deploy(CALLEE, verify(compile_source(CALLEE_SOURCE), max_size=1_000_000, limits=LIMITS))
    relay = verify(compile_source(RELAY_SOURCE), max_size=1_000_000, limits=LIMITS)
    for offset in range(RELAY_HOPS + 1):
        ledger.deploy(RELAY_BASE + offset, relay)
    ledger.deploy(CONTRACT, verify(compile_source(HOST_BENCHMARKS[name], filename=f"{name}.py"), max_size=1_000_000, limits=LIMITS))
    return ledger


def measure_host(name: str, gas: int) -> tuple[float, int]:
    """(gas per second, gas used) of one host family run on MockLedger until its gas runs out; raises AssertionError when it stops for another reason."""
    ledger = _ledger(name)
    runner = asyncio.new_event_loop()
    try:
        start = time.perf_counter()
        result = runner.run_until_complete(ledger.run(CONTRACT, "main", sender=CALLER, gas=gas, now=1_790_000_000))
        elapsed = time.perf_counter() - start
    finally:
        runner.close()
    if result.error != "OUT_OF_GAS" or result.gas_used != gas:
        raise AssertionError(f"{name}: stopped with {result.error} after {result.gas_used} gas: {result.message}")
    return result.gas_used / elapsed, result.gas_used


def measure_pure(name: str, gas: int) -> tuple[float, int]:
    """(gas per second, gas used) of one bench.py family."""
    micros, used = measure(name, gas)
    return 1e6 / micros, used


@dataclass(frozen=True)
class Measurement:
    name: str
    gas_per_second: float


def families() -> dict[str, Callable[[str, int], tuple[float, int]]]:
    """Every family by name, with its measuring function."""
    found: dict[str, Callable[[str, int], tuple[float, int]]] = dict.fromkeys(BENCHMARKS, measure_pure)
    found.update(dict.fromkeys(HOST_BENCHMARKS, measure_host))
    return found


def measure_all(gas: int, repeats: int = 3) -> list[Measurement]:
    """The best of repeats runs of every family, slowest first."""
    results = [Measurement(name, max(run(name, gas)[0] for _ in range(repeats))) for name, run in families().items()]
    return sorted(results, key=lambda item: item.gas_per_second)


def worst(results: list[Measurement]) -> Measurement:
    """The slowest family, which sets max_gas."""
    return results[0]


def round_down(value: int) -> int:
    """value with every digit after the leading one set to zero."""
    if value < 10:
        return value
    unit: int = 10 ** (len(str(value)) - 1)
    return value // unit * unit


def raw_max_gas(gas_per_second: float) -> int:
    """floor(gas_per_second * WALL_CLOCK_SECONDS * SAFETY_FACTOR)."""
    return math.floor(gas_per_second * WALL_CLOCK_SECONDS * SAFETY_FACTOR)


def max_gas_for(gas_per_second: float) -> int:
    """The max_gas value the formula gives for a worst case of gas_per_second."""
    return round_down(raw_max_gas(gas_per_second))


if __name__ == "__main__":
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    measured = measure_all(budget)
    for item in measured:
        print(f"{item.name:28} {item.gas_per_second:14,.0f} gas/s")
    slowest = worst(measured)
    print(f"worst family: {slowest.name} at {slowest.gas_per_second:,.0f} gas/s")
    print(f"floor({slowest.gas_per_second:,.0f} * {WALL_CLOCK_SECONDS} s * {SAFETY_FACTOR}) = {raw_max_gas(slowest.gas_per_second):,}, rounded down to {max_gas_for(slowest.gas_per_second):,}")
