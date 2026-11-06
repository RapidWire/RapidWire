"""The max_gas default stays within what the VM's worst-case throughput on this machine allows, so a change that slows the VM down, or a cheaper cost table, fails here."""

from __future__ import annotations

import pytest

from rapidwire.config import Settings
from rapidwire.contract.runtime import RuntimeConfig
from tests.unit.contract.bench_max_gas import HOST_BENCHMARKS, families, max_gas_for, measure_all, measure_host, raw_max_gas, round_down, worst

GAS = 20_000
# CI machines may be several times slower than the machine the default was chosen on; the default may use at most this multiple of what the local measurement allows.
CI_MARGIN = 4

DEFAULT_MAX_GAS: int = Settings.model_fields["max_gas"].default


def test_round_down_keeps_the_leading_digit() -> None:
    assert [round_down(value) for value in (0, 7, 10, 99, 250_489, 1_000_000, 1_999_999)] == [0, 7, 10, 90, 200_000, 1_000_000, 1_000_000]
    assert max_gas_for(250_489.0) == round_down(raw_max_gas(250_489.0)) == 200_000


def test_the_runtime_default_matches_the_setting() -> None:
    assert RuntimeConfig(gas_currency_id=1).max_gas == DEFAULT_MAX_GAS
    assert Settings.model_fields["default_gas"].default <= DEFAULT_MAX_GAS


@pytest.mark.parametrize("name", sorted(HOST_BENCHMARKS))
def test_host_family_runs_until_out_of_gas(name: str) -> None:
    _, used = measure_host(name, 2_000)
    assert used == 2_000


def test_default_max_gas_fits_the_measured_worst_case() -> None:
    results = measure_all(GAS)
    assert {item.name for item in results} == set(families())
    slowest = worst(results)
    allowed = raw_max_gas(slowest.gas_per_second)
    assert CI_MARGIN * allowed >= DEFAULT_MAX_GAS, f"{slowest.name} runs {slowest.gas_per_second:.0f} gas/s, which allows max_gas {allowed}"
