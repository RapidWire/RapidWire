from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from rapidwire.config import Settings
from rapidwire.core.ledger import BalanceKey, LedgerUsageError
from rapidwire.core.routing import MAX_PATH_EXPANSIONS, Path, PoolState, realize
from rapidwire.core.swap import Movement, SwapConfig, probe_amount, schedule_transfers, swap_movements

USER, P, Q, R = 7, -10, -11, -12
C = 1


def _replay(ordered: Sequence[Movement], balances: Mapping[BalanceKey, int]) -> dict[BalanceKey, int]:
    """Apply the ordered movements, asserting that no source ever goes negative, and return the final balances."""
    held = dict(balances)
    for item in ordered:
        held[(item.from_account, item.currency_id)] -= item.amount
        held[(item.to_account, item.currency_id)] += item.amount
        assert held[(item.from_account, item.currency_id)] >= 0, item
    return held


def _totals(movements: Sequence[Movement]) -> dict[tuple[int, int, int], int]:
    totals: dict[tuple[int, int, int], int] = defaultdict(int)
    for item in movements:
        totals[(item.from_account, item.to_account, item.currency_id)] += item.amount
    return dict(totals)


def test_feasible_movements_keep_their_order() -> None:
    movements = [Movement(USER, P, 1, 50), Movement(P, Q, 2, 30), Movement(Q, USER, 3, 20)]
    balances = {(USER, 1): 50, (P, 1): 100, (P, 2): 100, (Q, 2): 100, (Q, 3): 100, (USER, 3): 0}
    assert schedule_transfers(movements, balances) == movements


def test_a_debit_waits_for_the_credit_that_covers_it() -> None:
    movements = [Movement(P, USER, C, 150), Movement(USER, P, C, 80)]
    balances = {(P, C): 100, (USER, C): 80}
    ordered = schedule_transfers(movements, balances)
    assert ordered == [Movement(USER, P, C, 80), Movement(P, USER, C, 150)]
    assert _replay(ordered, balances) == {(P, C): 30, (USER, C): 150}


def test_a_circulation_larger_than_the_balances_is_split() -> None:
    movements = [Movement(P, Q, C, 50), Movement(Q, P, C, 45)]
    balances = {(P, C): 10, (Q, C): 10}
    ordered = schedule_transfers(movements, balances)
    assert len(ordered) > 2
    assert _totals(ordered) == _totals(movements)
    assert _replay(ordered, balances) == {(P, C): 5, (Q, C): 15}


def test_an_uncoverable_movement_raises() -> None:
    with pytest.raises(LedgerUsageError):
        schedule_transfers([Movement(P, Q, C, 50)], {(P, C): 0, (Q, C): 1})
    with pytest.raises(ValueError, match="positive"):
        schedule_transfers([Movement(P, Q, C, 0)], {(P, C): 1, (Q, C): 1})


@given(
    margins=st.lists(st.integers(1, 50), min_size=3, max_size=3),
    moves=st.lists(st.tuples(st.integers(0, 2), st.integers(0, 2), st.integers(1, 3_000)), min_size=1, max_size=8),
)
def test_any_movement_set_with_positive_final_balances_is_scheduled(margins: list[int], moves: list[tuple[int, int, int]]) -> None:
    accounts = (P, Q, R)
    movements = [Movement(accounts[a], accounts[b], C, amount) for a, b, amount in moves if a != b]
    assume(movements)
    net = dict.fromkeys(accounts, 0)
    for item in movements:
        net[item.from_account] -= item.amount
        net[item.to_account] += item.amount
    # Each account starts with just enough that its final balance is its small positive margin.
    balances = {(account, C): max(0, -net[account]) + margin for account, margin in zip(accounts, margins, strict=True)}
    final = {(account, C): balances[(account, C)] + net[account] for account in accounts}
    ordered = schedule_transfers(movements, balances)
    assert _totals(ordered) == _totals(movements)
    assert _replay(ordered, balances) == final


def test_movements_of_a_pool_used_in_both_directions_are_not_netted() -> None:
    # Pool 1 (A/B) is entered with A on the first route and with B on the second, which then returns to A through pool 4.
    a, b, c = 1, 2, 3
    pools = [PoolState(1, a, b, 100, 50_000, 50_000), PoolState(2, b, c, 100, 50_000, 50_000), PoolState(3, a, b, 50, 50_000, 60_000), PoolState(4, a, c, 10, 50_000, 50_000)]
    paths = [Path(pool_ids=(1, 2), currencies=(a, b, c)), Path(pool_ids=(3, 1, 4), currencies=(a, b, a, c))]
    allocation = realize(paths, pools, [(0, 3_000), (1, 2_000)])
    custody = {pool_id: -100 - pool_id for pool_id in (1, 2, 3, 4)}
    movements = swap_movements(allocation, USER, custody)
    first, second = allocation.routes
    assert movements == [
        Movement(USER, -101, a, 3_000),
        Movement(-101, -102, b, first.hops[0].amount_out),
        Movement(-102, USER, c, first.amount_out),
        Movement(USER, -103, a, 2_000),
        Movement(-103, -101, b, second.hops[0].amount_out),
        Movement(-101, -104, a, second.hops[1].amount_out),
        Movement(-104, USER, c, second.amount_out),
    ]
    balances: dict[BalanceKey, int] = {(USER, a): 5_000, (USER, c): 0}
    for state in pools:
        balances[(custody[state.pool_id], state.currency_a)] = state.reserve_a
        balances[(custody[state.pool_id], state.currency_b)] = state.reserve_b
    final = _replay(schedule_transfers(movements, balances), balances)
    for pool_id, (reserve_a, reserve_b) in allocation.final_reserves.items():
        state = pools[pool_id - 1]
        assert (final[(custody[pool_id], state.currency_a)], final[(custody[pool_id], state.currency_b)]) == (reserve_a, reserve_b)
    assert final[(USER, c)] == allocation.amount_out


def test_probe_amount_is_one_chunk() -> None:
    assert probe_amount(1_000, 100) == 10
    assert probe_amount(1_001, 100) == 11
    assert probe_amount(7, 100) == 1
    assert probe_amount(1, 1) == 1


def test_swap_config_validates_and_reads_settings() -> None:
    assert SwapConfig(chunk_count=1, max_routes=1) == SwapConfig(1, 1, MAX_PATH_EXPANSIONS)
    for chunk_count, max_routes, max_expansions in ((0, 1, 1), (1, 0, 1), (True, 1, 1), (1, -3, 1), (1, 1, 0), (1, 1, False)):
        with pytest.raises(ValueError, match="positive integer"):
            SwapConfig(chunk_count=chunk_count, max_routes=max_routes, max_expansions=max_expansions)
    settings = Settings(db_password="password-1234", swap_chunk_count=7, swap_max_routes=3)
    assert SwapConfig.from_settings(settings) == SwapConfig(chunk_count=7, max_routes=3, max_expansions=MAX_PATH_EXPANSIONS)
