from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rapidwire.core import routing
from rapidwire.core.routing import (
    MAX_HOPS,
    MAX_PATH_EXPANSIONS,
    SIMULATION_SCALE,
    Allocation,
    Path,
    PoolState,
    allocate,
    amount_out,
    chunk_sizes,
    enumerate_paths,
    realize,
)
from rapidwire.db.tables import FEE_DENOMINATOR, FEE_TIERS_BPS
from rapidwire.db.types import AMOUNT_MAX


def pool(pool_id: int, x: int, y: int, fee_bps: int, reserve_x: int, reserve_y: int) -> PoolState:
    """Build a pool from an unordered currency pair, keeping each reserve with its currency."""
    if x < y:
        return PoolState(pool_id, x, y, fee_bps, reserve_x, reserve_y)
    return PoolState(pool_id, y, x, fee_bps, reserve_y, reserve_x)


def paths_of(pools: Sequence[PoolState], source: int, target: int, *, max_paths: int = 10**6, probe: int = 100) -> list[Path]:
    return list(enumerate_paths(pools, source, target, max_paths=max_paths, probe_amount=probe).paths)


def reference_paths(pools: Sequence[PoolState], source: int, target: int) -> set[tuple[int, ...]]:
    """Every valid path by plain exhaustive search, without the enumerator's pruning."""
    live = [p for p in pools if not p.is_empty]
    found: set[tuple[int, ...]] = set()

    def walk(currency: int, ids: tuple[int, ...]) -> None:
        if len(ids) == MAX_HOPS:
            return
        for p in live:
            if p.pool_id in ids or currency not in (p.currency_a, p.currency_b):
                continue
            following = p.other_currency(currency)
            if following == target:
                found.add((*ids, p.pool_id))
            else:
                walk(following, (*ids, p.pool_id))

    if source != target:
        walk(source, ())
    return found


def probe_output(path: Path, pools: Sequence[PoolState], probe: int) -> int:
    by_id = {p.pool_id: p for p in pools}
    amount = probe * SIMULATION_SCALE
    for index, pool_id in enumerate(path.pool_ids):
        p = by_id[pool_id]
        amount = amount_out(amount, p.reserve_of(path.currencies[index]) * SIMULATION_SCALE, p.reserve_of(path.currencies[index + 1]) * SIMULATION_SCALE, p.fee_bps)
    return amount


def rank_key(path: Path, pools: Sequence[PoolState], probe: int) -> tuple[int, int, tuple[int, ...]]:
    return (-probe_output(path, pools, probe), path.hop_count, path.pool_ids)


def check_consistent(allocation: Allocation, pools: Sequence[PoolState]) -> None:
    """Totals add up, every hop moves a positive amount, hops chain, and final reserves equal initial reserves plus every hop delta."""
    by_id = {p.pool_id: p for p in pools}
    assert allocation.amount_out == sum(route.amount_out for route in allocation.routes)
    if allocation.is_empty:
        assert allocation.amount_out == 0
        assert not allocation.final_reserves
        return
    assert allocation.amount_in == sum(route.amount_in for route in allocation.routes)
    assert len({route.path for route in allocation.routes}) == len(allocation.routes)
    deltas: dict[int, list[int]] = {}
    for route in allocation.routes:
        assert route.hops[0].amount_in == route.amount_in
        assert route.hops[-1].amount_out == route.amount_out
        assert tuple(hop.pool_id for hop in route.hops) == route.path.pool_ids
        for index, hop in enumerate(route.hops):
            assert hop.amount_in > 0
            assert hop.amount_out > 0
            assert (hop.currency_in, hop.currency_out) == (route.path.currencies[index], route.path.currencies[index + 1])
            if index:
                assert hop.amount_in == route.hops[index - 1].amount_out
            delta = deltas.setdefault(hop.pool_id, [0, 0])
            side = by_id[hop.pool_id].side(hop.currency_in)
            delta[side] += hop.amount_in
            delta[1 - side] -= hop.amount_out
    assert set(allocation.final_reserves) == set(deltas)
    for pool_id, (da, db) in deltas.items():
        p = by_id[pool_id]
        assert allocation.final_reserves[pool_id] == (p.reserve_a + da, p.reserve_b + db)
        assert min(allocation.final_reserves[pool_id]) >= 1


def best_single(paths: Sequence[Path], pools: Sequence[PoolState], amount_in: int) -> int:
    return max((realize(paths, pools, [(index, amount_in)]).amount_out for index in range(len(paths))), default=0)


def path_flooring_bound(path: Path, pools: Sequence[PoolState]) -> Fraction:
    """Upper bound on what flooring costs one execution of path when its downstream pools only ever receive input in the path's direction.

    Each hop's floor loses less than one unit of that hop's output, and a unit entering the rest of the path yields at most the product of the downstream spot prices g * reserve_out / reserve_in on the initial reserves (the output is concave in the input and those spot prices only fall).
    """
    by_id = {p.pool_id: p for p in pools}
    spots = []
    for index, pool_id in enumerate(path.pool_ids):
        p = by_id[pool_id]
        spots.append(Fraction(FEE_DENOMINATOR - p.fee_bps, FEE_DENOMINATOR) * Fraction(p.reserve_of(path.currencies[index + 1]), p.reserve_of(path.currencies[index])))
    return sum((math.prod(spots[hop + 1 :], start=Fraction(1)) for hop in range(len(spots))), start=Fraction(0))


def flooring_tolerance(paths: Sequence[Path], pools: Sequence[PoolState], executions: int = 1) -> int:
    """Flooring bound when each path is executed at most `executions` times; the greedy decides on near-exact simulated outputs, so this bounds how far its realized total trails the same plan's exact value."""
    return math.ceil(executions * sum((path_flooring_bound(path, pools) for path in paths), start=Fraction(0)))


def planned_output(paths: Sequence[Path], pools: Sequence[PoolState], legs: Sequence[tuple[int, int]]) -> Fraction:
    """The total output of legs executed one at a time on scaled reserves, as the greedy simulates them."""
    by_id = {p.pool_id: p for p in pools}
    reserves = {p.pool_id: [p.reserve_a * SIMULATION_SCALE, p.reserve_b * SIMULATION_SCALE] for p in pools}
    total = 0
    for index, size in legs:
        path = paths[index]
        amount = size * SIMULATION_SCALE
        for position, pool_id in enumerate(path.pool_ids):
            side = by_id[pool_id].side(path.currencies[position])
            pair = reserves[pool_id]
            output = amount_out(amount, pair[side], pair[1 - side], by_id[pool_id].fee_bps)
            pair[side] += amount
            pair[1 - side] -= output
            amount = output
        total += amount
    return Fraction(total, SIMULATION_SCALE)


pool_graphs = st.lists(
    st.tuples(
        st.integers(1, 4),
        st.integers(1, 4),
        st.sampled_from(FEE_TIERS_BPS),
        st.integers(0, 10**6),
        st.integers(0, 10**6),
    ).filter(lambda spec: spec[0] != spec[1]),
    min_size=1,
    max_size=8,
).map(lambda specs: [pool(index + 1, *spec) for index, spec in enumerate(specs)])


# amount_out


def test_amount_out_exact_values_per_fee_tier() -> None:
    # floor(1000 * (10000 - fee) * 200000 / (100000 * 10000 + 1000 * (10000 - fee)))
    assert amount_out(1000, 100_000, 200_000, 100) == 1960
    assert amount_out(1000, 100_000, 200_000, 50) == 1970
    assert amount_out(1000, 100_000, 200_000, 10) == 1978
    assert FEE_TIERS_BPS == (100, 50, 10)


def test_amount_out_floors_once() -> None:
    # Flooring the fee-adjusted input first (101 * 0.99 -> 99) would give 98.
    assert amount_out(101, 10**9, 10**9, 100) == 99
    assert 99 * 10**9 // (10**9 + 99) == 98


@pytest.mark.parametrize(("amount", "reserve_in", "reserve_out"), [(0, 10, 10), (-5, 10, 10), (5, 0, 10), (5, 10, 0)])
def test_amount_out_degenerate_inputs_give_zero(amount: int, reserve_in: int, reserve_out: int) -> None:
    assert amount_out(amount, reserve_in, reserve_out, 100) == 0


amounts = st.integers(1, 10**38 - 1)


@given(amount=amounts, reserve_in=amounts, reserve_out=amounts, fee=st.sampled_from(FEE_TIERS_BPS), extra=st.integers(1, 10**20))
def test_amount_out_properties(amount: int, reserve_in: int, reserve_out: int, fee: int, extra: int) -> None:
    out = amount_out(amount, reserve_in, reserve_out, fee)
    assert 0 <= out < reserve_out
    assert amount_out(amount + extra, reserve_in, reserve_out, fee) >= out
    assert (reserve_in + amount) * (reserve_out - out) >= reserve_in * reserve_out
    exact = Fraction(amount * (FEE_DENOMINATOR - fee) * reserve_out, reserve_in * FEE_DENOMINATOR + amount * (FEE_DENOMINATOR - fee))
    assert out == math.floor(exact)


# chunking


@given(amount=st.integers(1, 10**6), count=st.integers(1, 500))
def test_chunk_sizes(amount: int, count: int) -> None:
    sizes = chunk_sizes(amount, count)
    assert sum(sizes) == amount
    assert len(sizes) == min(amount, count)
    assert max(sizes) - min(sizes) <= 1
    assert min(sizes) >= 1
    assert sizes == sorted(sizes, reverse=True)


def test_chunk_sizes_examples() -> None:
    assert chunk_sizes(7, 100) == [1] * 7
    assert chunk_sizes(10, 3) == [4, 3, 3]
    with pytest.raises(ValueError):
        chunk_sizes(0, 3)


# enumerate_paths


def test_parallel_fee_tiers_are_separate_paths() -> None:
    pools = [pool(i + 1, 1, 2, fee, 10**6, 10**6) for i, fee in enumerate(FEE_TIERS_BPS)]
    paths = paths_of(pools, 1, 2, max_paths=10)
    # Lower fee pays more, so the 0.1% tier ranks first.
    assert [p.pool_ids for p in paths] == [(3,), (2,), (1,)]
    assert all(p.currencies == (1, 2) for p in paths)


def test_path_ends_at_first_arrival_and_may_revisit_source() -> None:
    pools = [pool(1, 1, 2, 100, 10**6, 10**6), pool(2, 2, 1, 50, 10**6, 10**6), pool(3, 1, 3, 100, 10**6, 10**6), pool(4, 3, 9, 100, 10**6, 10**6)]
    found = {(p.pool_ids, p.currencies) for p in paths_of(pools, 1, 3)}
    assert found == {((3,), (1, 3)), ((1, 2, 3), (1, 2, 1, 3)), ((2, 1, 3), (1, 2, 1, 3))}
    assert {p.pool_ids for p in paths_of(pools, 1, 2)} == {(1,), (2,)}


def test_at_most_five_hops() -> None:
    chain5 = [pool(i + 1, 10 + i, 11 + i, 100, 10**6, 10**6) for i in range(5)]
    assert [p.pool_ids for p in paths_of(chain5, 10, 15)] == [(1, 2, 3, 4, 5)]
    chain6 = [pool(i + 1, 10 + i, 11 + i, 100, 10**6, 10**6) for i in range(6)]
    assert paths_of(chain6, 10, 16) == []


def test_empty_pools_and_unreachable_targets() -> None:
    pools = [pool(1, 1, 2, 100, 0, 10**6), pool(2, 1, 2, 50, 10**6, 10**6)]
    assert [p.pool_ids for p in paths_of(pools, 1, 2)] == [(2,)]
    assert paths_of(pools, 1, 3) == []
    assert paths_of(pools, 1, 1) == []


@settings(max_examples=150)
@given(pools=pool_graphs, source=st.integers(1, 4), target=st.integers(1, 4), probe=st.integers(1, 10**5), cap=st.integers(1, 12), data=st.data())
def test_enumeration_matches_reference(pools: list[PoolState], source: int, target: int, probe: int, cap: int, data: st.DataObject) -> None:
    expected = reference_paths(pools, source, target)
    search = enumerate_paths(pools, source, target, max_paths=10**6, probe_amount=probe)
    assert not search.truncated
    full = list(search.paths)
    assert {p.pool_ids for p in full} == expected
    for path in full:
        assert path.from_currency == source
        assert path.to_currency == target
        assert target not in path.currencies[:-1]
        assert len(set(path.pool_ids)) == path.hop_count <= MAX_HOPS
    keys = [rank_key(p, pools, probe) for p in full]
    assert keys == sorted(keys)
    capped = paths_of(pools, source, target, max_paths=cap, probe=probe)
    assert capped == full[:cap]
    shuffled = data.draw(st.permutations(pools))
    assert paths_of(shuffled, source, target, max_paths=cap, probe=probe) == capped


def test_cap_keeps_best_by_probe() -> None:
    # Two direct pools and two-hop detours through currency 3 with deep pools; the deep detour beats both direct pools.
    pools = [
        pool(1, 1, 2, 100, 1000, 1000),
        pool(2, 1, 2, 100, 10**6, 10**6),
        pool(3, 1, 3, 10, 10**7, 10**7),
        pool(4, 3, 2, 10, 10**7, 10**7),
        pool(5, 3, 2, 100, 500, 500),
    ]
    assert [p.pool_ids for p in paths_of(pools, 1, 2, max_paths=2)] == [(3, 4), (2,)]


def complete_graph(currencies: int, reserve: int) -> list[PoolState]:
    pools: list[PoolState] = []
    for x, y in itertools.combinations(range(1, currencies + 1), 2):
        for fee in FEE_TIERS_BPS:
            pools.append(pool(len(pools) + 1, x, y, fee, reserve, reserve))
    return pools


def deep_direct_graph() -> list[PoolState]:
    """Every pair of 14 currencies in three shallow tiers, plus a far deeper direct 1-2 pool with the highest id."""
    pools = complete_graph(14, 10**4)
    return [*pools, pool(len(pools) + 1, 1, 2, 10, 10**12, 10**12)]


def test_truncated_search_still_finds_deep_direct_pool() -> None:
    pools = deep_direct_graph()
    deep = pools[-1].pool_id
    search = enumerate_paths(pools, 1, 2, max_paths=32, probe_amount=1000)
    assert search.truncated
    assert len(search.paths) == 32
    assert search.paths[0].pool_ids == (deep,)
    assert {(p.pool_id,) for p in pools if (p.currency_a, p.currency_b) == (1, 2)} <= {path.pool_ids for path in search.paths}


def test_expansion_limit_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    small = complete_graph(6, 10**6)
    full = {p.pool_ids for p in paths_of(small, 1, 2)}
    calls = 0
    original: Callable[[int, int, int, int], int] = routing.amount_out

    def counting(amount: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
        nonlocal calls
        calls += 1
        return original(amount, reserve_in, reserve_out, fee_bps)

    monkeypatch.setattr(routing, "amount_out", counting)
    for limit in (0, 1, 7, 500):
        calls = 0
        limited = enumerate_paths(small, 1, 2, max_paths=10**6, probe_amount=100, max_expansions=limit)
        assert calls <= limit
        assert len(limited.paths) <= limit
        assert limited.truncated
        assert {p.pool_ids for p in limited.paths} <= full

    pools = deep_direct_graph()
    first_level = sum(1 for p in pools if 1 in (p.currency_a, p.currency_b))
    calls = 0
    level_one = enumerate_paths(pools, 1, 2, max_paths=10**6, probe_amount=1000, max_expansions=first_level)
    assert calls == first_level
    assert level_one.truncated
    # The whole first level fits the budget, so every direct pool is found and nothing longer.
    assert {path.pool_ids for path in level_one.paths} == {(p.pool_id,) for p in pools if (p.currency_a, p.currency_b) == (1, 2)}
    calls = 0
    capped = enumerate_paths(pools, 1, 2, max_paths=32, probe_amount=1000)
    assert capped.truncated
    assert calls == MAX_PATH_EXPANSIONS
    assert capped.paths[0].pool_ids == (pools[-1].pool_id,)


def test_search_reports_its_expansion_count(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original: Callable[[int, int, int, int], int] = routing.amount_out

    def counting(amount: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
        nonlocal calls
        calls += 1
        return original(amount, reserve_in, reserve_out, fee_bps)

    monkeypatch.setattr(routing, "amount_out", counting)
    small = complete_graph(5, 10**6)
    for limit in (0, 3, MAX_PATH_EXPANSIONS):
        calls = 0
        search = enumerate_paths(small, 1, 2, max_paths=10**6, probe_amount=100, max_expansions=limit)
        assert search.expansions == calls <= limit
    assert enumerate_paths(small, 1, 1, max_paths=1, probe_amount=1).expansions == 0
    assert enumerate_paths([pool(1, 3, 4, 100, 10, 10)], 1, 2, max_paths=1, probe_amount=1).expansions == 0


# allocate


def test_no_paths_or_no_output_gives_empty_allocation() -> None:
    pools = [pool(1, 1, 2, 100, 10**9, 10)]
    paths = paths_of(pools, 1, 2, probe=1)
    assert allocate([], pools, 100, chunk_count=10).is_empty
    tiny = allocate(paths, pools, 5, chunk_count=10)
    assert tiny.is_empty
    assert tiny.amount_out == 0
    check_consistent(tiny, pools)


def test_single_path_takes_everything() -> None:
    pools = [pool(1, 1, 2, 100, 10**6, 2 * 10**6)]
    allocation = allocate(paths_of(pools, 1, 2), pools, 1000, chunk_count=10)
    assert allocation.amount_out == amount_out(1000, 10**6, 2 * 10**6, 100)
    assert len(allocation.routes) == 1
    check_consistent(allocation, pools)


def test_amount_below_chunk_count() -> None:
    pools = [pool(1, 1, 2, 100, 1000, 10**6), pool(2, 1, 2, 10, 1000, 10**6)]
    paths = paths_of(pools, 1, 2, probe=1)
    allocation = allocate(paths, pools, 3, chunk_count=100)
    assert allocation.amount_in == 3
    assert allocation.amount_out >= best_single(paths, pools, 3)
    check_consistent(allocation, pools)


def test_equal_parallel_pools_split_evenly() -> None:
    pools = [pool(1, 1, 2, 50, 10**6, 10**6), pool(2, 1, 2, 50, 10**6, 10**6)]
    paths = paths_of(pools, 1, 2, probe=1000)
    allocation = allocate(paths, pools, 100_000, chunk_count=100)
    assert sorted(route.amount_in for route in allocation.routes) == [50_000, 50_000]
    assert allocation.amount_out > best_single(paths, pools, 100_000)
    check_consistent(allocation, pools)


def test_input_reserve_may_exceed_amount_limit() -> None:
    pools = [pool(1, 1, 2, 100, AMOUNT_MAX, AMOUNT_MAX)]
    allocation = allocate(paths_of(pools, 1, 2, probe=10**30), pools, AMOUNT_MAX, chunk_count=100)
    check_consistent(allocation, pools)
    assert allocation.final_reserves[1][0] > AMOUNT_MAX
    assert 1 <= allocation.final_reserves[1][1] < AMOUNT_MAX


@settings(max_examples=150)
@given(pools=pool_graphs, source=st.integers(1, 4), target=st.integers(1, 4), amount=st.integers(1, 10**6), chunks=st.integers(1, 40), cap=st.integers(1, 8))
def test_allocation_never_below_best_single_path(pools: list[PoolState], source: int, target: int, amount: int, chunks: int, cap: int) -> None:
    probe = chunk_sizes(amount, chunks)[0]
    paths = paths_of(pools, source, target, max_paths=cap, probe=probe)
    allocation = allocate(paths, pools, amount, chunk_count=chunks)
    check_consistent(allocation, pools)
    assert allocation.amount_out >= best_single(paths, pools, amount)
    assert allocation.is_empty == (allocation.amount_out == 0)
    if not allocation.is_empty:
        assert allocation.amount_in == amount


disjoint_cases = st.lists(
    st.lists(st.tuples(st.sampled_from(FEE_TIERS_BPS), st.integers(200, 5000), st.integers(200, 5000)), min_size=1, max_size=2),
    min_size=2,
    max_size=3,
)


@settings(max_examples=120)
@given(spec=disjoint_cases, chunks=st.integers(2, 8), per_chunk=st.integers(1, 300))
def test_greedy_matches_brute_force_on_disjoint_paths(spec: list[list[tuple[int, int, int]]], chunks: int, per_chunk: int) -> None:
    pools: list[PoolState] = []
    paths: list[Path] = []
    for index, hops in enumerate(spec):
        currencies = [1] + [100 + 10 * index + h for h in range(len(hops) - 1)] + [2]
        ids = []
        for h, (fee, reserve_in, reserve_out) in enumerate(hops):
            pools.append(pool(len(pools) + 1, currencies[h], currencies[h + 1], fee, reserve_in, reserve_out))
            ids.append(len(pools))
        paths.append(Path(tuple(ids), tuple(currencies)))
    amount = chunks * per_chunk
    allocation = allocate(paths, pools, amount, chunk_count=chunks)
    check_consistent(allocation, pools)
    optimum = 0
    for counts in itertools.product(range(chunks + 1), repeat=len(paths)):
        if sum(counts) == chunks:
            legs = [(index, count * per_chunk) for index, count in enumerate(counts) if count]
            optimum = max(optimum, realize(paths, pools, legs).amount_out)
    # Disjoint paths are each executed in one piece, so one execution per path bounds the flooring.
    assert allocation.amount_out <= optimum
    assert optimum - allocation.amount_out <= flooring_tolerance(paths, pools)


two_pool_cases = st.tuples(st.sampled_from(FEE_TIERS_BPS), st.integers(50, 3000), st.integers(50, 3000), st.sampled_from(FEE_TIERS_BPS), st.integers(50, 3000), st.integers(50, 3000))


@settings(max_examples=120)
@given(spec=two_pool_cases, amount=st.integers(1, 150), coarse=st.integers(1, 4))
def test_unit_chunks_reach_integer_optimum_on_parallel_pools(spec: tuple[int, int, int, int, int, int], amount: int, coarse: int) -> None:
    fee1, x1, y1, fee2, x2, y2 = spec
    pools = [pool(1, 1, 2, fee1, x1, y1), pool(2, 1, 2, fee2, x2, y2)]
    paths = [Path((1,), (1, 2)), Path((2,), (1, 2))]
    tolerance = flooring_tolerance(paths, pools)
    optimum = max(realize(paths, pools, [leg for leg in ((0, split), (1, amount - split)) if leg[1]]).amount_out for split in range(amount + 1))
    fine = allocate(paths, pools, amount, chunk_count=amount)
    rough = allocate(paths, pools, amount, chunk_count=coarse)
    check_consistent(fine, pools)
    assert rough.amount_out <= optimum
    assert fine.amount_out <= optimum
    assert optimum - fine.amount_out <= tolerance
    # The fine grid is never further from the optimum than the rough grid, up to flooring.
    assert optimum - fine.amount_out <= max(optimum - rough.amount_out, tolerance)


def shared_topology(spec: Sequence[tuple[int, int, int]]) -> tuple[list[PoolState], list[Path]]:
    """Pool 1 is 1-2 direct; pools 2 and 5 take 1 to 3; pools 3 and 4 take 3 to 2.

    Paths (2, 3) and (2, 4) share their first pool and diverge downstream, and (5, 3) joins (2, 3) at pool 3; every downstream pool only receives currency 3.
    """
    pairs = [(1, 2), (1, 3), (3, 2), (3, 2), (1, 3)]
    pools = [pool(index + 1, x, y, fee, reserve_in, reserve_out) for index, ((x, y), (fee, reserve_in, reserve_out)) in enumerate(zip(pairs, spec, strict=True))]
    paths = [Path((1,), (1, 2)), Path((2, 3), (1, 3, 2)), Path((2, 4), (1, 3, 2)), Path((5, 3), (1, 3, 2))]
    return pools, paths


shared_cases = st.lists(st.tuples(st.sampled_from(FEE_TIERS_BPS), st.integers(500, 5000), st.integers(500, 5000)), min_size=5, max_size=5)


@settings(max_examples=60, deadline=None)
@given(spec=shared_cases, chunks=st.integers(2, 5), amount=st.integers(100, 4000), fine=st.integers(20, 60))
def test_shared_pools_follow_the_plan_and_beat_coarse_brute_force(spec: list[tuple[int, int, int]], chunks: int, amount: int, fine: int) -> None:
    pools, paths = shared_topology(spec)
    by_id = {p.pool_id: p for p in pools}
    steps = [routing._steps(path, by_id) for path in paths]
    legs = routing._greedy_legs(steps, amount, fine)
    executed = realize(paths, pools, legs)
    check_consistent(executed, pools)
    # Execution follows the simulated plan: it only loses what flooring costs, at most one execution of a path per leg.
    assert executed.amount_out >= planned_output(paths, pools, legs) - flooring_tolerance(paths, pools, executions=len(legs))
    allocation = allocate(paths, pools, amount, chunk_count=fine)
    check_consistent(allocation, pools)
    assert allocation.amount_out >= max(executed.amount_out, best_single(paths, pools, amount))
    # Every ordered assignment of the coarse chunks, the strongest plan the coarse grid admits.
    sizes = chunk_sizes(amount, chunks)
    coarse_best = max(realize(paths, pools, list(zip(order, sizes, strict=True))).amount_out for order in itertools.product(range(len(paths)), repeat=len(sizes)))
    assert allocation.amount_out >= coarse_best - flooring_tolerance(paths, pools, executions=fine)


def test_execution_follows_plan_when_paths_share_a_pool() -> None:
    # Index-order execution of per-path totals would push all of path 1 through pool 2 before path 2, unlike the interleaved plan.
    pools, paths = shared_topology([(50, 3890, 1493), (10, 1086, 4836), (100, 4318, 4232), (100, 540, 4570), (10, 4226, 1464)])
    by_id = {p.pool_id: p for p in pools}
    legs = routing._greedy_legs([routing._steps(path, by_id) for path in paths], 2533, 8)
    plan = planned_output(paths, pools, legs)
    executed = allocate(paths, pools, 2533, chunk_count=8)
    check_consistent(executed, pools)
    totals = [sum(size for index, size in legs if index == path_index) for path_index in range(len(paths))]
    sequential = realize(paths, pools, [(index, total) for index, total in enumerate(totals) if total])
    assert executed.amount_out >= plan - flooring_tolerance(paths, pools, executions=len(legs))
    assert (executed.amount_out, sequential.amount_out) == (4680, 3910)
    shared = [route for route in executed.routes if 2 in route.path.pool_ids]
    assert sum(hop.amount_in for route in shared for hop in route.hops if hop.pool_id == 2) == executed.final_reserves[2][0] - by_id[2].reserve_a


def test_single_route_kept_when_split_pays_no_more() -> None:
    pools = [pool(1, 1, 2, 100, 10**9, 10**9), pool(2, 1, 2, 100, 10**9, 10**9)]
    paths = [Path((1,), (1, 2)), Path((2,), (1, 2))]
    # The greedy splits evenly, but flooring makes 5 + 5 pay 4 + 4 while 10 on one pool pays 9.
    assert realize(paths, pools, [(0, 5), (1, 5)]).amount_out == 8
    allocation = allocate(paths, pools, 10, chunk_count=10)
    assert [route.path for route in allocation.routes] == [paths[0]]
    assert allocation.amount_out == realize(paths, pools, [(0, 10)]).amount_out == 9


def test_routes_that_realize_nothing_are_merged_or_dropped() -> None:
    pools = [pool(1, 1, 2, 100, 28, 43), pool(2, 1, 2, 10, 10**6, 497_082)]
    paths = [Path((1,), (1, 2)), Path((2,), (1, 2))]
    by_id = {p.pool_id: p for p in pools}
    # The greedy gives one unit to the deep pool, whose realized output floors to zero.
    legs = routing._greedy_legs([routing._steps(path, by_id) for path in paths], 22, 22)
    assert sorted({index for index, _ in legs}) == [0, 1]
    assert sum(size for index, size in legs if index == 1) == 1
    raw = routing._execute(paths, [routing._steps(path, by_id) for path in paths], legs)
    assert [route.amount_out for route in raw.routes] == [18, 0]
    dropped = realize(paths, pools, legs)
    check_consistent(dropped, pools)
    assert [(route.path, route.amount_in) for route in dropped.routes] == [(paths[0], 21)]
    allocation = allocate(paths, pools, 22, chunk_count=22)
    check_consistent(allocation, pools)
    assert [(route.path, route.amount_in, route.amount_out) for route in allocation.routes] == [(paths[0], 22, 18)]


def test_invalid_arguments() -> None:
    pools = [pool(1, 1, 2, 100, 10, 10), pool(2, 1, 3, 100, 10, 10)]
    paths = [Path((1,), (1, 2))]
    with pytest.raises(ValueError):
        allocate(paths, pools, 0, chunk_count=1)
    with pytest.raises(ValueError):
        allocate(paths, pools, 1, chunk_count=0)
    with pytest.raises(ValueError):
        allocate([Path((1,), (1, 3))], pools, 1, chunk_count=1)
    with pytest.raises(ValueError):
        allocate([Path((9,), (1, 2))], pools, 1, chunk_count=1)
    with pytest.raises(ValueError):
        allocate([paths[0], Path((2,), (1, 3))], pools, 1, chunk_count=1)
    with pytest.raises(ValueError):
        allocate([paths[0], paths[0]], pools, 1, chunk_count=1)
    with pytest.raises(ValueError):
        realize([paths[0], Path((2,), (1, 3))], pools, [(0, 1)])
    with pytest.raises(ValueError):
        realize(paths, pools, [(0, 0)])
    with pytest.raises(ValueError):
        realize(paths, pools, [(1, 1)])
    with pytest.raises(ValueError):
        enumerate_paths([*pools, *pools], 1, 2, max_paths=1, probe_amount=1)
    with pytest.raises(ValueError):
        Path((1, 1), (1, 2, 1))
    with pytest.raises(ValueError):
        Path((1, 2), (2, 1, 2))
    with pytest.raises(ValueError):
        Path((1, 3, 2), (1, 2, 4, 2))
    with pytest.raises(ValueError):
        PoolState(1, 2, 1, 100, 1, 1)
    with pytest.raises(ValueError):
        PoolState(1, 1, 2, FEE_DENOMINATOR, 1, 1)
