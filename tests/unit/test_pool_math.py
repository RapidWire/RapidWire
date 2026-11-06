from __future__ import annotations

import math
from decimal import Decimal, localcontext

import pytest

from rapidwire.core.errors import InvalidArgument
from rapidwire.core.pools import canonical_pair, deposit_shares, initial_shares, optimal_deposit, pro_rata_payouts, share_percent, validate_fee_tier, withdrawal_amounts
from rapidwire.db.tables import FEE_TIERS_BPS


@pytest.mark.parametrize(("a", "b", "expected"), [(1, 1, 1), (2, 2, 2), (1000, 1001, 1000), (1001, 1001, 1001), (10, 1000, 100), (3, 5, 3), (10**38 - 1, 10**38 - 1, 10**38 - 1)])
def test_initial_shares_floor_the_square_root(a: int, b: int, expected: int) -> None:
    assert initial_shares(a, b) == expected == math.isqrt(a * b)


def test_optimal_deposit_uses_all_of_a_when_b_suffices() -> None:
    # Price 2 b per a: desired 100 a needs 200 b, which fits in 300.
    assert optimal_deposit(100, 300, 1_000, 2_000) == (100, 200)
    # Exactly enough b also keeps all of a.
    assert optimal_deposit(100, 200, 1_000, 2_000) == (100, 200)
    # b_opt is floored.
    assert optimal_deposit(10, 100, 3_000, 1_000) == (10, 3)


def test_optimal_deposit_uses_all_of_b_when_b_is_short() -> None:
    assert optimal_deposit(100, 150, 1_000, 2_000) == (75, 150)
    # a_opt is floored and never exceeds the desired a.
    a, b = optimal_deposit(100, 7, 1_000, 3_000)
    assert (a, b) == (2, 7)
    assert a <= 100


def test_deposit_shares_take_the_smaller_ratio() -> None:
    assert deposit_shares(100, 200, 1_000, 2_000, 1_414) == 141
    assert deposit_shares(100, 150, 1_000, 2_000, 1_000) == 75
    assert deposit_shares(1, 1, 1_000_000, 1_000_000, 1_000) == 0


def test_withdrawal_amounts_floor_each_side() -> None:
    assert withdrawal_amounts(1, 1_000, 3_333, 3) == (333, 1_111)
    assert withdrawal_amounts(2, 10, 10, 3) == (6, 6)
    assert withdrawal_amounts(3, 10, 10, 3) == (10, 10)
    with pytest.raises(ValueError, match="total_shares"):
        withdrawal_amounts(1, 1, 1, 0)


def test_pro_rata_payouts_give_the_remainder_to_the_largest_holder() -> None:
    payouts = pro_rata_payouts(100, {7: 1, 5: 1, 9: 2})
    assert payouts == {5: 25, 7: 25, 9: 50}
    payouts = pro_rata_payouts(10, {7: 1, 5: 1, 9: 1})
    # floor(10/3) = 3 each, remainder 1 goes to the lowest ID among the tied largest holders.
    assert payouts == {5: 4, 7: 3, 9: 3}
    payouts = pro_rata_payouts(11, {3: 2, 8: 5})
    assert payouts == {3: 3, 8: 8}
    assert sum(payouts.values()) == 11
    assert pro_rata_payouts(0, {1: 5}) == {1: 0}
    assert pro_rata_payouts(5, {}) == {}
    with pytest.raises(ValueError, match="positive"):
        pro_rata_payouts(5, {1: 0})


def test_share_percent_rounds_exact_halves_to_even() -> None:
    assert share_percent(1234, 10_000) == "12.34"
    assert share_percent(1, 3) == "33.33"
    assert share_percent(2, 3) == "66.67"
    assert share_percent(5, 5) == "100.00"
    assert share_percent(0, 5) == "0.00"
    # Exact halves of the last digit go to the even neighbour: 0.125% -> 0.12, 0.375% -> 0.38, 0.005% -> 0.00, 0.015% -> 0.02.
    assert share_percent(1, 800) == "0.12"
    assert share_percent(3, 800) == "0.38"
    assert share_percent(1, 20_000) == "0.00"
    assert share_percent(3, 20_000) == "0.02"
    assert share_percent(10**38 - 2, 10**38 - 1) == "100.00"


@pytest.mark.parametrize(("shares", "total"), [(1, 3), (2, 3), (1, 7), (5, 8), (1, 800), (3, 800), (1, 20_000), (19_000, 22_000), (123_456_789, 987_654_321), (10**38 - 2, 10**38 - 1)])
def test_share_percent_matches_decimal_format(shares: int, total: int) -> None:
    with localcontext(prec=200):
        expected = f"{Decimal(shares) / Decimal(total) * 100:.2f}"
    assert share_percent(shares, total) == expected


def test_pair_and_fee_validation() -> None:
    pair = canonical_pair(9, 4)
    assert (pair.currency_a, pair.currency_b, pair.swapped) == (4, 9, True)
    assert pair.to_ab(90, 40) == (40, 90)
    assert pair.to_xy(40, 90) == (90, 40)
    straight = canonical_pair(4, 9)
    assert (straight.swapped, straight.to_ab(1, 2)) == (False, (1, 2))
    for bad in ((3, 3), (0, 1), (1, -2), (True, 2), (1, 2**63)):
        with pytest.raises(InvalidArgument):
            canonical_pair(*bad)
    for tier in FEE_TIERS_BPS:
        assert validate_fee_tier(tier) == tier
    for bad_tier in (0, 30, 1000, True, "100"):
        with pytest.raises(InvalidArgument, match="fee tier"):
            validate_fee_tier(bad_tier)
