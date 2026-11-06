"""Worst-case gas throughput programs: each loops one expensive operation until its gas runs out, so its wall time per gas can be compared with plain arithmetic.

Run directly (python -m tests.unit.contract.bench [gas]) to print microseconds per gas for every program; the benchmark that sets the maximum gas setting uses the slowest one.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from rapidwire.contract.costs import BUILTIN_GAS, route_search_gas
from rapidwire.contract.errors import ContractFailure, OutOfGas
from rapidwire.contract.limits import Limits
from rapidwire.contract.values import Account, encode_value
from rapidwire.contract.verifier import VerifiedProgram
from rapidwire.contract.vm import ExecutionContext, execute
from rapidwire.core.routing import MAX_PATH_EXPANSIONS, PoolState, allocate, enumerate_paths
from tests.unit.contract.helpers import SELF, SENDER, FakeHost, program, seeded

LIMITS = Limits(wall_clock_seconds=600.0)


def loop(setup: str, body: str, locals_: int = 12) -> str:
    """A main function that runs setup once and then repeats body forever (body must leave the stack empty)."""
    return f".function main public returns=int locals={locals_}\n{setup}\nforever:\n{body}\nPUSH_TRUE\nJUMP_IF_TRUE forever\nPUSH_INT 0\nRET\n.end\n"


_labels = itertools.count()


def counted(count: int, body: str, *, counter: int = 1) -> str:
    """Instructions running body count times with locals counter .. counter + 3 (the loop variable is counter + 3)."""
    c, stop, step, var = counter, counter + 1, counter + 2, counter + 3
    label = next(_labels)
    return f"PUSH_INT 0\nSTORE {c}\nPUSH_INT {count}\nSTORE {stop}\nPUSH_INT 1\nSTORE {step}\nfill{label}:\nFOR_RANGE {c} {stop} {step} {var} filled{label}\n{body}\nJUMP fill{label}\nfilled{label}:"


def dict_1024(target: int) -> str:
    return f"BUILD_DICT 0\nSTORE {target}\n" + counted(1024, f"LOAD {target}\nLOAD 4\nLOAD 4\nINDEX_SET")


BIG = json.dumps("q" * 4000)
INT_LIST_1024 = "BUILD_LIST 0\nSTORE 0\n" + counted(1024, "LOAD 0\nLOAD 4\nAPPEND")
STR_LIST_1024 = "BUILD_LIST 0\nSTORE 0\n" + counted(1024, 'LOAD 0\nPUSH_CONST "abcdefgh"\nLOAD 4\nTO_STR\nCONCAT\nAPPEND')
NESTED_LIST = "BUILD_LIST 0\nSTORE 0\n" + counted(1024, "LOAD 0\nPUSH_INT 1\nBUILD_LIST 1\nAPPEND")
# About 70k references held live (70 copies of a 1024-element list, about 1.7 MB accounted), so walks are frequent and long.
BIG_LIVE = INT_LIST_1024 + "\nBUILD_LIST 0\nSTORE 5\n" + counted(70, "LOAD 5\nLOAD 0\nPUSH_INT 0\nSLICE 1\nAPPEND", counter=6)
FREE_RUN = "\n".join(["PUSH_INT 1", "POP"] * 11)

PEER = Account(3003)
M61 = 2**61 - 1
# 1022 int keys in 146 hash classes of 7 colliding keys each (k and k + m * (2**61 - 1) hash alike, and a signed 64-bit key range holds at most 8 per class), and a missing key of a full class.
COLLIDING_DICT = "BUILD_DICT 0\nSTORE 0\n" + counted(146, "\n".join(f"LOAD 0\nLOAD 4\nPUSH_CONST {m * M61}\nADD\nPUSH_INT 0\nINDEX_SET" for m in range(-4, 3))) + f"\nPUSH_CONST {5 + 3 * M61}\nSTORE 9"
ACCOUNT_LIST = "BUILD_LIST 0\nSTORE 0\n" + counted(1024, "LOAD 0\nLOAD 4\nPUSH_INT 1\nADD\nCALL_BUILTIN Account 1\nAPPEND") + "\nPUSH_INT 99999\nCALL_BUILTIN Account 1\nSTORE 9"
RECORD_LIST = "BUILD_LIST 0\nSTORE 0\n" + counted(1024, "LOAD 0\nCALL_BUILTIN paid 0\nAPPEND") + "\nPUSH_INT 7\nSTORE 9"
ACCOUNT_DICT = "BUILD_DICT 0\nSTORE 0\n" + counted(1024, "LOAD 0\nLOAD 4\nPUSH_INT 1\nADD\nCALL_BUILTIN Account 1\nPUSH_INT 0\nINDEX_SET") + "\nPUSH_INT 5\nCALL_BUILTIN Account 1\nSTORE 9"
# A callee that builds a shared binary tree of 4096 True leaves and fails to store it once the encoding passes the byte limit.
FAILING_SET_CALLEE = (
    ".function boom public returns=int\nPUSH_TRUE\nBUILD_LIST 1\n" + "DUP\nBUILD_LIST 2\n" * 12 + 'STORE 0\nPUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n'
)
# A callee whose value is about 3 KB of True and a record at the end, which cannot be stored.
RECORD_TAIL_CALLEE = (
    ".function boom public returns=int\nPUSH_TRUE\nBUILD_LIST 1\n"
    + "DUP\nCONCAT\n" * 10
    + 'DUP\nDUP\nCALL_BUILTIN paid 0\nBUILD_LIST 4\nSTORE 0\nPUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n'
)
REVERT_CALLEE = ".function boom public returns=int\nPUSH_TRUE\nBUILD_LIST 1\n" + "DUP\nBUILD_LIST 2\n" * 12 + 'STORE 0\nPUSH_CONST "no"\nREVERT\n.end\n'
STR_INT_DICT_300 = "BUILD_DICT 0\nSTORE 0\n" + counted(300, "LOAD 0\nLOAD 4\nTO_STR\nLOAD 4\nINDEX_SET")
TRY_BOOM = loop(f"PUSH_INT {PEER.id}\nCALL_BUILTIN Account 1\nSTORE 0", 'LOAD 0\nPUSH_CONST "boom"\nTRY_CALL 0 0\nPOP')


@dataclass(frozen=True)
class Bench:
    source: str
    storage: Callable[[], dict[str, bytes]] | None = None
    callee: str | None = None
    """Source of the contract registered at PEER, for programs that call another contract."""


BENCHMARKS: dict[str, Bench] = {
    "arithmetic": Bench(loop("PUSH_INT 0\nSTORE 0", "LOAD 0\nPUSH_INT 1\nADD\nSTORE 0")),
    "free_run": Bench(loop("", FREE_RUN)),
    "call": Bench(
        loop("", 'PUSH_INT 1\nPUSH_CONST "a"\nPUSH_TRUE\nCALL f') + ".function f params=int,str,bool\nRET\n.end\n",
    ),
    "call_record_params": Bench(
        loop("", "CALL_BUILTIN paid 0\nCALL_BUILTIN paid 0\nCALL_BUILTIN paid 0\nCALL f") + ".function f params=Paid,Paid,Paid\nRET\n.end\n",
    ),
    "dict_keys_big_live": Bench(loop(BIG_LIVE + "\n" + dict_1024(10), "LOAD 10\nCALL_BUILTIN dict.keys 1\nPOP")),
    "dict_keys": Bench(loop(dict_1024(0), "LOAD 0\nCALL_BUILTIN dict.keys 1\nPOP")),
    "load_big_str_big_live": Bench(loop(BIG_LIVE + f"\nPUSH_CONST {BIG}\nSTORE 10", "LOAD 10\nPOP")),
    "storage_get_bools": Bench(loop("", 'PUSH_CONST "k"\nCALL_BUILTIN storage.get 1\nPOP'), lambda: {"k": encode_value([True] * 1024)}),
    "storage_get_big_str": Bench(loop("", 'PUSH_CONST "k"\nCALL_BUILTIN storage.get 1\nPOP'), lambda: {"k": encode_value("q" * 4000)}),
    "storage_set_bools": Bench(loop("PUSH_TRUE\nBUILD_LIST 1\nSTORE 0\n" + counted(10, "LOAD 0\nLOAD 0\nCONCAT\nSTORE 0"), 'PUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2')),
    "check_type_nested": Bench(loop(NESTED_LIST, "LOAD 0\nCHECK_TYPE list[list[int]]\nPOP")),
    "check_type_ints": Bench(loop(INT_LIST_1024, "LOAD 0\nCHECK_TYPE list[int]\nPOP")),
    "contains_strict": Bench(loop(INT_LIST_1024, "PUSH_INT 1\nLOAD 0\nCONTAINS\nPOP")),
    "contains_str_list": Bench(loop(STR_LIST_1024, 'PUSH_CONST "zzzzzzzzzz"\nLOAD 0\nCONTAINS\nPOP')),
    "build_dict_64": Bench(loop("", "\n".join(f"PUSH_INT {i}\nPUSH_INT {i}" for i in range(64)) + "\nBUILD_DICT 64\nPOP")),
    "build_list_128": Bench(loop("", "\n".join(["PUSH_INT 1"] * 128) + "\nBUILD_LIST 128\nPOP")),
    "slice_str_list": Bench(loop(STR_LIST_1024, "LOAD 0\nPUSH_INT 0\nSLICE 1\nPOP")),
    "concat_4k": Bench(loop(f"PUSH_CONST {json.dumps('q' * 2048)}\nSTORE 0", "LOAD 0\nLOAD 0\nCONCAT\nPOP")),
    "split": Bench(loop(f"PUSH_CONST {json.dumps(','.join(['a'] * 1000))}\nSTORE 0", 'LOAD 0\nPUSH_CONST ","\nCALL_BUILTIN str.split 2\nPOP')),
    "join": Bench(loop(STR_LIST_1024, 'PUSH_CONST ""\nLOAD 0\nPUSH_INT 0\nPUSH_INT 300\nSLICE 3\nCALL_BUILTIN str.join 2\nPOP')),
    "dict_values": Bench(loop(dict_1024(0), "LOAD 0\nCALL_BUILTIN dict.values 1\nPOP")),
    "sha256_4k": Bench(loop(f"PUSH_CONST {BIG}\nSTORE 0", "LOAD 0\nCALL_BUILTIN sha256 1\nPOP")),
    "for_each_str_list": Bench(loop(STR_LIST_1024, "PUSH_INT 0\nSTORE 1\nl:\nFOR_EACH 0 1 2 d\nJUMP l\nd:")),
    "dict_contains_colliding_ints": Bench(loop(COLLIDING_DICT, "LOAD 9\nLOAD 0\nCONTAINS\nPOP")),
    "list_contains_accounts": Bench(loop(ACCOUNT_LIST, "LOAD 9\nLOAD 0\nCONTAINS\nPOP")),
    "list_contains_records": Bench(loop(RECORD_LIST, "LOAD 9\nLOAD 0\nCONTAINS\nPOP")),
    "dict_get_account_keys": Bench(loop(ACCOUNT_DICT, "LOAD 0\nLOAD 9\nINDEX_GET\nPOP")),
    "sender_run": Bench(loop("", "CALL_BUILTIN sender 0\nPOP\n" * 7)),
    "paid_run": Bench(loop("", "CALL_BUILTIN paid 0\nPOP\n" * 7)),
    "field_run": Bench(loop("CALL_BUILTIN paid 0\nSTORE 0", "LOAD 0\nGET_FIELD Paid amount\nPOP\n" * 3)),
    "push_const_str_run": Bench(loop("", 'PUSH_CONST "abcdef"\nPOP\n' * 7)),
    "load_str_run": Bench(loop('PUSH_CONST "abcdef"\nSTORE 0', "LOAD 0\nPOP\n" * 7)),
    "ret_run": Bench(loop("", "CALL f\n" * 4) + ".function f\nRET\n.end\n"),
    "try_call_failing_set_tree": Bench(TRY_BOOM, callee=FAILING_SET_CALLEE),
    "try_call_failing_set_record_tail": Bench(TRY_BOOM, callee=RECORD_TAIL_CALLEE),
    "try_call_revert": Bench(TRY_BOOM, callee=REVERT_CALLEE),
    "storage_set_int_list": Bench(loop("BUILD_LIST 0\nSTORE 0\n" + counted(1000, "LOAD 0\nLOAD 4\nPUSH_INT 100\nMOD\nAPPEND"), 'PUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2')),
    "storage_set_str_int_dict": Bench(loop(STR_INT_DICT_300, 'PUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2')),
    "storage_get_ints": Bench(loop("", 'PUSH_CONST "k"\nCALL_BUILTIN storage.get 1\nPOP'), lambda: {"k": encode_value([i % 100 for i in range(1000)])}),
    "storage_get_str_int_dict": Bench(loop("", 'PUSH_CONST "k"\nCALL_BUILTIN storage.get 1\nPOP'), lambda: {"k": encode_value({str(i): i for i in range(300)})}),
}


def measure(name: str, gas: int) -> tuple[float, int]:
    """(microseconds per gas, gas used) of one benchmark program run until gas runs out; raises AssertionError when the program stops for another reason."""
    bench = BENCHMARKS[name]
    verified: VerifiedProgram = program(bench.source, limits=LIMITS)
    host = FakeHost(canonical=True)
    if bench.storage is not None:
        host.storage[SELF] = bench.storage()
    if bench.callee is not None:
        host.contracts[PEER] = program(bench.callee, limits=LIMITS)
    ctx = ExecutionContext.top_level(self_account=SELF, sender=SENDER, paid=None, now=1, gas_limit=gas, limits=LIMITS)
    loop_runner = asyncio.new_event_loop()
    try:
        start = time.perf_counter()
        try:
            loop_runner.run_until_complete(execute(verified, "main", [], host=host, context=ctx))
        except OutOfGas as exc:
            if exc.level != 0:
                raise AssertionError(f"{name}: ran out of a sub-limit") from exc
        except ContractFailure as exc:
            raise AssertionError(f"{name}: stopped with {type(exc).__name__}: {exc}") from exc
        elapsed = time.perf_counter() - start
    finally:
        loop_runner.close()
    return elapsed / ctx.gas.used * 1e6, ctx.gas.used


# (currencies, pools) of the route-search workloads: from a single pool, where allocation dominates, to a dense graph whose search hits the expansion limit.
ROUTE_CASES = {"route_one_pool": (2, 1), "route_20_pools": (6, 20), "route_80_pools": (12, 80), "route_300_pools": (20, 300)}


def route_search_micros_per_gas(name: str, *, chunk_count: int = 100, max_expansions: int = MAX_PATH_EXPANSIONS) -> float:
    """Microseconds per gas of one quote's route search (enumeration and allocation) priced at the base quote cost plus route_search_gas()."""
    currencies, count = ROUTE_CASES[name]
    rng = seeded(count)
    pools = []
    for pool_id in range(count):
        a, b = sorted(rng.sample(range(1, currencies + 1), 2))
        pools.append(PoolState(pool_id, a, b, rng.choice([100, 50, 10]), rng.randint(10**6, 10**9), rng.randint(10**6, 10**9)))
    start = time.perf_counter()
    search = enumerate_paths(pools, 1, 2, max_paths=32, probe_amount=1000, max_expansions=max_expansions)
    allocate(search.paths, pools, 1000, chunk_count=chunk_count)
    elapsed = time.perf_counter() - start
    chunk_hops = chunk_count * sum(len(path.pool_ids) for path in search.paths)
    return elapsed / (BUILTIN_GAS["quote"] + route_search_gas(search.expansions, chunk_hops)) * 1e6


def report(gas: int) -> dict[str, float]:
    """Microseconds per gas of every benchmark and route-search workload (best of three runs)."""
    results = {name: min(measure(name, gas)[0] for _ in range(3)) for name in BENCHMARKS}
    results.update({name: min(route_search_micros_per_gas(name) for _ in range(3)) for name in ROUTE_CASES})
    return results


if __name__ == "__main__":
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
    results = report(budget)
    baseline = results["arithmetic"]
    for bench_name, micros in sorted(results.items(), key=lambda item: -item[1]):
        print(f"{bench_name:24} {micros:8.3f} us/gas  {micros / baseline:5.2f}x")
