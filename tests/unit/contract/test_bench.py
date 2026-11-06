from __future__ import annotations

import pytest

from tests.unit.contract.bench import BENCHMARKS, ROUTE_CASES, measure, route_search_micros_per_gas

GAS = 20_000
# Wall time per gas of every benchmark must stay within this factor of plain arithmetic; the bound leaves room for timer noise on shared machines.
MAX_RATIO = 4.0


@pytest.mark.parametrize("name", sorted(BENCHMARKS))
def test_benchmark_runs_until_out_of_gas(name: str) -> None:
    _, used = measure(name, GAS)
    assert used == GAS


def test_time_per_gas_stays_within_a_small_factor_of_arithmetic() -> None:
    baseline = min(measure("arithmetic", GAS)[0] for _ in range(5))
    slow = {}
    for name in BENCHMARKS:
        ratio = min(measure(name, GAS)[0] for _ in range(3)) / baseline
        if ratio > MAX_RATIO:
            slow[name] = ratio
    for name in ROUTE_CASES:
        ratio = min(route_search_micros_per_gas(name, max_expansions=5_000) for _ in range(3)) / baseline
        if ratio > MAX_RATIO:
            slow[name] = ratio
    assert slow == {}
