from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from rapidwire.core import staking
from rapidwire.core.staking import MICROSECONDS_PER_HOUR, StakeInfo, compute_accrual, elapsed_microseconds, projected_amount
from rapidwire.db.tables import CARRY_SCALE, RATE_PPM_MAX
from rapidwire.db.types import AMOUNT_MAX

HOUR = MICROSECONDS_PER_HOUR
YEAR_HOURS = 24 * 365


def old_hourly(amount: int, rate_ppm: int, hours: int) -> int:
    """The old whole-hour compounding floor(a * (1 + r / 10**6) ** hours), computed exactly."""
    value = Fraction(amount) * Fraction(1_000_000 + rate_ppm, 1_000_000) ** hours
    return value.numerator // value.denominator


def exact_value(amount: int, carry: int) -> Fraction:
    return Fraction(amount) + Fraction(carry, CARRY_SCALE)


def assert_whole_hours(amount: int, rate_ppm: int, hours: int) -> None:
    result = compute_accrual(amount, 0, rate_ppm, hours * HOUR)
    expected = old_hourly(amount, rate_ppm, hours)
    if expected > AMOUNT_MAX:
        assert result.saturated, (amount, rate_ppm, hours)
        assert (result.amount, result.carry) == (AMOUNT_MAX + 1, 0)
    else:
        assert not result.saturated
        assert result.amount == expected, (amount, rate_ppm, hours)
        assert result.interest == expected - amount
        exact = Fraction(amount) * Fraction(1_000_000 + rate_ppm, 1_000_000) ** hours
        assert result.carry == (exact - expected) * CARRY_SCALE // 1


@pytest.mark.parametrize("amount", [1, 7, 999, 1_000_000, 123_456_789_012, 10**30 + 12345])
@pytest.mark.parametrize("rate_ppm", [1, 100, 5_000, 123_457, 500_000, RATE_PPM_MAX])
@pytest.mark.parametrize("hours", [1, 2, 3, 5, 24])
def test_whole_hours_equal_old_hourly_compounding(amount: int, rate_ppm: int, hours: int) -> None:
    assert_whole_hours(amount, rate_ppm, hours)


@pytest.mark.parametrize(("amount", "rate_ppm", "hours"), [(2**52, 500_000, 52), (2**60, 250_000, 30), (2**80, 500_000, 80), (2**80, 500_000, 40)])
def test_whole_hours_regressions(amount: int, rate_ppm: int, hours: int) -> None:
    assert_whole_hours(amount, rate_ppm, hours)


def test_whole_hours_power_of_two_amounts() -> None:
    for exponent in range(0, 127, 3):
        for rate in (1, 3, 125_000, 250_000, 500_000, 999_999, RATE_PPM_MAX):
            for hours in (1, 7, 30, 52, 100):
                assert_whole_hours(2**exponent, rate, hours)


def test_whole_hours_random_cases_equal_old_hourly() -> None:
    rng = random.Random(20261030)  # noqa: S311
    for _ in range(500):
        assert_whole_hours(rng.randrange(1, 10 ** rng.randrange(1, 36)), rng.randrange(0, RATE_PPM_MAX + 1), rng.randrange(1, 49))


def test_carry_holds_the_fraction_of_a_whole_hour() -> None:
    result = compute_accrual(1_000, 0, 1_500, HOUR)
    # 1000 * 1.0015 = 1001.5 exactly.
    assert (result.amount, result.carry, result.interest) == (1_001, CARRY_SCALE // 2, 1)


def test_fractional_hours_are_not_lost() -> None:
    amount, rate = 1_000_000, 10_000
    once = compute_accrual(amount, 0, rate, HOUR)
    first = compute_accrual(amount, 0, rate, HOUR // 2)
    second = compute_accrual(first.amount, first.carry, rate, HOUR // 2)
    assert once.amount == 1_010_000
    assert first.interest > 0
    # The truncated carry can leave a result that is exactly an integer one carry unit short of it.
    assert 0 <= exact_value(once.amount, once.carry) - exact_value(second.amount, second.carry) < Fraction(2, CARRY_SCALE)
    other = compute_accrual(1_000_003, 0, 12_345, HOUR)
    half = compute_accrual(1_000_003, 0, 12_345, HOUR // 2)
    assert compute_accrual(half.amount, half.carry, 12_345, HOUR // 2).amount == other.amount


def test_half_hours_never_lose_more_than_the_carry_bound() -> None:
    rng = random.Random(7)  # noqa: S311
    for _ in range(200):
        amount = rng.randrange(1, 10**20)
        rate = rng.randrange(1, RATE_PPM_MAX + 1)
        once = compute_accrual(amount, 0, rate, HOUR)
        first = compute_accrual(amount, 0, rate, HOUR // 2)
        second = compute_accrual(first.amount, first.carry, rate, HOUR // 2)
        exact = Fraction(amount) * Fraction(1_000_000 + rate, 1_000_000)
        twice = exact_value(second.amount, second.carry)
        assert twice <= exact
        assert exact - twice < Fraction(4, CARRY_SCALE)
        assert second.amount in (once.amount, once.amount - 1)


def test_hours_plus_fraction_matches_exact_growth() -> None:
    amount, carry, rate = 123_456_789_012_345, 987_654_321, 33_333
    elapsed = 5 * HOUR + HOUR // 4
    result = compute_accrual(amount, carry, rate, elapsed)
    base = exact_value(amount, carry) * Fraction(1_000_000 + rate, 1_000_000) ** 5
    grown = exact_value(result.amount, result.carry)
    # The quarter-hour factor lies strictly between 1 and the hourly factor.
    assert base < grown < base * Fraction(1_000_000 + rate, 1_000_000)


def test_many_small_accruals_match_one_large_accrual() -> None:
    amount, rate = 5_000_000_000, 250_000
    one = compute_accrual(amount, 0, rate, 6 * HOUR)
    current = compute_accrual(amount, 0, 0, 0)
    step = 60_000_000
    for _ in range(6 * 60):
        current = compute_accrual(current.amount, current.carry, rate, step)
    assert current.amount == one.amount
    assert abs(exact_value(current.amount, current.carry) - exact_value(one.amount, one.carry)) < Fraction(6 * 60 * 4, CARRY_SCALE)


def test_rate_zero_and_zero_stake_produce_no_interest() -> None:
    assert compute_accrual(1_000, 123, 0, 10 * HOUR) == compute_accrual(1_000, 123, 0, 0)
    zero_rate = compute_accrual(1_000, 123, 0, 10 * HOUR)
    assert (zero_rate.amount, zero_rate.carry, zero_rate.interest) == (1_000, 123, 0)
    zero_stake = compute_accrual(0, 0, RATE_PPM_MAX, 10 * HOUR)
    assert (zero_stake.amount, zero_stake.carry, zero_stake.interest) == (0, 0, 0)


def test_negative_elapsed_is_zero() -> None:
    result = compute_accrual(1_000, 5, 500_000, -HOUR)
    assert (result.amount, result.carry, result.interest) == (1_000, 5, 0)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert elapsed_microseconds(start, start - timedelta(seconds=5)) == 0
    assert elapsed_microseconds(start, start + timedelta(seconds=1, microseconds=7)) == 1_000_007


def test_rate_cap_doubles_per_hour() -> None:
    assert compute_accrual(1_000, 0, RATE_PPM_MAX, HOUR).amount == 2_000
    assert compute_accrual(1_000, 0, RATE_PPM_MAX, 10 * HOUR).amount == 1_024_000
    with pytest.raises(ValueError, match="rate_ppm"):
        compute_accrual(1_000, 0, RATE_PPM_MAX + 1, HOUR)


def test_growth_beyond_amount_max_saturates() -> None:
    result = compute_accrual(10**37, 0, RATE_PPM_MAX, 100 * YEAR_HOURS * HOUR + 1)
    assert result.saturated
    assert (result.amount, result.carry, result.interest) == (AMOUNT_MAX + 1, 0, AMOUNT_MAX + 1 - 10**37)
    assert not compute_accrual(AMOUNT_MAX // 2, 0, RATE_PPM_MAX, HOUR - 1).saturated
    assert compute_accrual(AMOUNT_MAX // 2 + 1, 0, RATE_PPM_MAX, HOUR).saturated


def _count_powers(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the exponent of every pow() call compute_accrual makes."""
    steps: list[int] = []

    def counting_pow(base: int, exponent: int) -> int:
        steps.append(exponent)
        result: int = base**exponent
        return result

    monkeypatch.setattr(staking, "pow", counting_pow, raising=False)
    return steps


def test_saturation_stops_after_the_first_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # A century at the top rate passes AMOUNT_MAX within the first one-year step, so no further step's power is computed.
    steps = _count_powers(monkeypatch)
    assert compute_accrual(10**37, 0, RATE_PPM_MAX, 100 * YEAR_HOURS * HOUR).saturated
    assert steps == [YEAR_HOURS, YEAR_HOURS]


def test_decades_at_the_lowest_rate_reuse_one_step_power(monkeypatch: pytest.MonkeyPatch) -> None:
    steps = _count_powers(monkeypatch)
    long = compute_accrual(10**30, 0, 1, 100 * YEAR_HOURS * HOUR + 5 * HOUR)
    assert long.amount > 10**30
    # One power pair for all 100 full-year steps and one for the 5-hour remainder.
    assert steps == [YEAR_HOURS, YEAR_HOURS, 5, 5]
    ten_years = 10 * YEAR_HOURS
    result = compute_accrual(10**30, 0, 1, ten_years * HOUR)
    exact = Fraction(10**30) * Fraction(1_000_001, 1_000_000) ** ten_years
    # Whole-year steps floor at the carry scale, so the error stays within one carry unit per step.
    assert 0 <= exact - exact_value(result.amount, result.carry) < Fraction(10, CARRY_SCALE)


def test_deterministic() -> None:
    args = (987_654_321, 123_456_789, 77_777, 12_345_678_901)
    first = compute_accrual(*args)
    assert all(compute_accrual(*args) == first for _ in range(20))


@pytest.mark.parametrize(
    ("amount", "carry", "rate", "elapsed"),
    [(-1, 0, 0, 0), (1, -1, 0, 0), (1, CARRY_SCALE, 0, 0), (1, 0, -1, 0), (True, 0, 0, 0), (1, 0, 0, 1.5)],
)
def test_invalid_arguments(amount: int, carry: int, rate: int, elapsed: int) -> None:
    with pytest.raises(ValueError):
        compute_accrual(amount, carry, rate, elapsed)


def _info(amount: int, *, halted: bool = False) -> StakeInfo:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return StakeInfo(account_id=1, currency_id=2, symbol="X", name="X", amount=amount, carry=0, hourly_rate_ppm=RATE_PPM_MAX, interest_halted=halted, last_accrued_at=at)


def test_projected_amount() -> None:
    later = datetime(2026, 1, 1, 2, tzinfo=UTC)
    assert projected_amount(_info(1_000), later) == 4_000
    assert projected_amount(_info(1_000, halted=True), later) == 1_000
    assert projected_amount(_info(1_000), later, supply=1_000) == 4_000
    assert projected_amount(_info(1_000), later, supply=AMOUNT_MAX - 500) == 1_500
    assert projected_amount(_info(1_000), later, supply=AMOUNT_MAX) == 1_000
    assert projected_amount(_info(AMOUNT_MAX // 2), later) == AMOUNT_MAX
