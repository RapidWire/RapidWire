"""Constant-product swap math, swap path enumeration and multi-route allocation in exact integer arithmetic."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from rapidwire.db.tables import FEE_DENOMINATOR

MAX_HOPS = 5
# Path enumeration stops after this many partial-path extensions; below the limit it is exhaustive.
MAX_PATH_EXPANSIONS = 200_000
# Ranking and the greedy simulation run on amounts and reserves multiplied by this factor, so their per-hop flooring is negligible; realized amounts are never scaled.
SIMULATION_SCALE = 10**18

_RankEntry = tuple[int, int, tuple[int, ...], tuple[int, ...], tuple[int, ...]]
_Steps = tuple[tuple["PoolState", int], ...]
Leg = tuple[int, int]


def amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """Return floor(amount_in * (D - fee) * reserve_out / (reserve_in * D + amount_in * (D - fee))) with D = FEE_DENOMINATOR, or 0 for a non-positive input or an empty reserve."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    effective = amount_in * (FEE_DENOMINATOR - fee_bps)
    return effective * reserve_out // (reserve_in * FEE_DENOMINATOR + effective)


def chunk_sizes(amount_in: int, chunk_count: int) -> list[int]:
    """Split amount_in into min(chunk_count, amount_in) chunks whose sizes differ by at most one, larger chunks first."""
    if amount_in < 1 or chunk_count < 1:
        raise ValueError("amount_in and chunk_count must be positive")
    count = min(chunk_count, amount_in)
    base, remainder = divmod(amount_in, count)
    return [base + 1] * remainder + [base] * (count - remainder)


@dataclass(frozen=True, slots=True)
class PoolState:
    """Reserves and fee tier of one pool; currency_a is the smaller currency id."""

    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    reserve_a: int
    reserve_b: int

    def __post_init__(self) -> None:
        if self.currency_a >= self.currency_b:
            raise ValueError(f"pool {self.pool_id}: currency_a must be smaller than currency_b")
        if not 0 <= self.fee_bps < FEE_DENOMINATOR:
            raise ValueError(f"pool {self.pool_id}: fee_bps out of range")
        if self.reserve_a < 0 or self.reserve_b < 0:
            raise ValueError(f"pool {self.pool_id}: negative reserve")

    @property
    def is_empty(self) -> bool:
        return self.reserve_a == 0 or self.reserve_b == 0

    def side(self, currency: int) -> int:
        """Return 0 for currency_a and 1 for currency_b."""
        if currency == self.currency_a:
            return 0
        if currency == self.currency_b:
            return 1
        raise ValueError(f"currency {currency} is not in pool {self.pool_id}")

    def reserve_of(self, currency: int) -> int:
        return self.reserve_a if self.side(currency) == 0 else self.reserve_b

    def other_currency(self, currency: int) -> int:
        return self.currency_b if self.side(currency) == 0 else self.currency_a


@dataclass(frozen=True, slots=True)
class Path:
    """A swap path: currencies[k] enters pool_ids[k] and currencies[k + 1] leaves it; the target currency appears only at the end."""

    pool_ids: tuple[int, ...]
    currencies: tuple[int, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.pool_ids) <= MAX_HOPS or len(self.currencies) != len(self.pool_ids) + 1:
            raise ValueError("a path has 1 to MAX_HOPS pools and one more currency than pools")
        if len(set(self.pool_ids)) != len(self.pool_ids):
            raise ValueError("a path uses each pool at most once")
        if self.currencies[-1] in self.currencies[:-1]:
            raise ValueError("a path ends at its first arrival at the target currency")

    @property
    def hop_count(self) -> int:
        return len(self.pool_ids)

    @property
    def from_currency(self) -> int:
        return self.currencies[0]

    @property
    def to_currency(self) -> int:
        return self.currencies[-1]


@dataclass(frozen=True, slots=True)
class PathSearch:
    """Paths from enumerate_paths, best first; truncated is true when the expansion limit stopped the search with candidates left unexamined, and expansions counts the pool extensions the search simulated (at most max_expansions)."""

    paths: tuple[Path, ...]
    truncated: bool
    expansions: int = 0


@dataclass(frozen=True, slots=True)
class Hop:
    """The realized amounts of one pool on one route: amount_in of currency_in entered it and amount_out of currency_out left it."""

    pool_id: int
    currency_in: int
    currency_out: int
    amount_in: int
    amount_out: int


@dataclass(frozen=True, slots=True)
class Route:
    """The realized share of a swap that follows one path; every hop amount is positive."""

    path: Path
    amount_in: int
    amount_out: int
    hops: tuple[Hop, ...]


@dataclass(frozen=True, slots=True)
class Allocation:
    """A realized swap split over routes; final_reserves holds the (reserve_a, reserve_b) after the swap of every pool a route touches, and an input-side reserve there may exceed the amount column limit, which callers check."""

    amount_in: int
    amount_out: int
    routes: tuple[Route, ...]
    final_reserves: Mapping[int, tuple[int, int]]

    @property
    def is_empty(self) -> bool:
        return not self.routes


def _index_pools(pools: Iterable[PoolState]) -> dict[int, PoolState]:
    by_id: dict[int, PoolState] = {}
    for pool in pools:
        if pool.pool_id in by_id:
            raise ValueError(f"duplicate pool {pool.pool_id}")
        by_id[pool.pool_id] = pool
    return by_id


def _distances_to(adjacency: Mapping[int, Sequence[PoolState]], target: int) -> dict[int, int]:
    """Return the fewest pools needed to reach target from each currency, ignoring pool reuse."""
    distance = {target: 0}
    frontier = [target]
    while frontier:
        following: list[int] = []
        for currency in frontier:
            for pool in adjacency.get(currency, ()):
                other = pool.other_currency(currency)
                if other not in distance:
                    distance[other] = distance[currency] + 1
                    following.append(other)
        frontier = following
    return distance


def enumerate_paths(
    pools: Iterable[PoolState],
    from_currency: int,
    to_currency: int,
    *,
    max_paths: int,
    probe_amount: int,
    max_expansions: int = MAX_PATH_EXPANSIONS,
) -> PathSearch:
    """Return up to max_paths swap paths ranked by output for probe_amount on the current reserves, then fewer hops, then smaller pool ids; this is synchronous CPU-bound work.

    Candidates are examined level by level in lexicographic pool-id order, so a cut loses only candidates at or beyond the level where it stopped.
    """
    if max_paths < 1 or probe_amount < 1 or max_expansions < 0:
        raise ValueError("max_paths and probe_amount must be positive and max_expansions non-negative")
    by_id = _index_pools(pools)
    if from_currency == to_currency:
        return PathSearch(paths=(), truncated=False)
    adjacency: dict[int, list[PoolState]] = {}
    for pool_id in sorted(by_id):
        pool = by_id[pool_id]
        if pool.is_empty:
            continue
        adjacency.setdefault(pool.currency_a, []).append(pool)
        adjacency.setdefault(pool.currency_b, []).append(pool)
    distance = _distances_to(adjacency, to_currency)
    if from_currency not in distance:
        return PathSearch(paths=(), truncated=False)

    heap: list[_RankEntry] = []
    unreachable = MAX_HOPS + 1
    expansions = 0
    frontier: list[tuple[tuple[int, ...], tuple[int, ...], int]] = [((), (from_currency,), probe_amount * SIMULATION_SCALE)]

    def offer(output: int, ids: tuple[int, ...], currencies: tuple[int, ...]) -> None:
        entry = (output, -len(ids), tuple(-pool_id for pool_id in ids), ids, currencies)
        if len(heap) < max_paths:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)

    def expand_level(depth: int) -> bool:
        """Extend every frontier entry by one pool; return False when the expansion budget ran out first."""
        nonlocal expansions, frontier
        following_frontier: list[tuple[tuple[int, ...], tuple[int, ...], int]] = []
        for ids, currencies, amount in frontier:
            currency = currencies[-1]
            for pool in adjacency[currency]:
                if pool.pool_id in ids:
                    continue
                following = pool.other_currency(currency)
                if depth + 1 + distance.get(following, unreachable) > MAX_HOPS:
                    continue
                if expansions >= max_expansions:
                    return False
                expansions += 1
                output = amount_out(amount, pool.reserve_of(currency) * SIMULATION_SCALE, pool.reserve_of(following) * SIMULATION_SCALE, pool.fee_bps)
                # A zero output stays zero on every extension, so such a path cannot displace a positive-output path from a full heap.
                if output == 0 and len(heap) == max_paths and heap[0][0] > 0:
                    continue
                if following == to_currency:
                    offer(output, (*ids, pool.pool_id), (*currencies, following))
                else:
                    following_frontier.append(((*ids, pool.pool_id), (*currencies, following), output))
        frontier = following_frontier
        return True

    truncated = False
    for depth in range(MAX_HOPS):
        if not frontier:
            break
        if not expand_level(depth):
            truncated = True
            break
    paths = tuple(Path(pool_ids=entry[3], currencies=entry[4]) for entry in sorted(heap, reverse=True))
    return PathSearch(paths=paths, truncated=truncated, expansions=expansions)


def _steps(path: Path, pools: Mapping[int, PoolState]) -> _Steps:
    """Resolve a path to (pool, input side) pairs, checking that its currencies match the pools."""
    steps: list[tuple[PoolState, int]] = []
    for index, pool_id in enumerate(path.pool_ids):
        pool = pools.get(pool_id)
        if pool is None:
            raise ValueError(f"path uses unknown pool {pool_id}")
        currency_in = path.currencies[index]
        if pool.other_currency(currency_in) != path.currencies[index + 1]:
            raise ValueError(f"path currencies do not match pool {pool_id}")
        steps.append((pool, pool.side(currency_in)))
    return tuple(steps)


def _resolve(paths: Sequence[Path], pools: Iterable[PoolState]) -> list[_Steps]:
    """Resolve every path, checking that all of them connect the same pair of currencies."""
    if paths and any((path.from_currency, path.to_currency) != (paths[0].from_currency, paths[0].to_currency) for path in paths):
        raise ValueError("all paths must share the same source and target currencies")
    by_id = _index_pools(pools)
    return [_steps(path, by_id) for path in paths]


def _simulate(steps: _Steps, reserves: Mapping[int, list[int]], amount: int) -> int:
    for pool, side in steps:
        pair = reserves[pool.pool_id]
        amount = amount_out(amount, pair[side], pair[1 - side], pool.fee_bps)
    return amount


def _apply(steps: _Steps, reserves: Mapping[int, list[int]], amount: int) -> None:
    for pool, side in steps:
        pair = reserves[pool.pool_id]
        output = amount_out(amount, pair[side], pair[1 - side], pool.fee_bps)
        pair[side] += amount
        pair[1 - side] -= output
        amount = output


def _greedy_legs(all_steps: Sequence[_Steps], amount_in: int, chunk_count: int) -> list[Leg]:
    """Assign each chunk in turn to the path with the largest simulated output, updating the simulated reserves of the chosen path's pools, and return the (path index, chunk) sequence."""
    reserves: dict[int, list[int]] = {}
    paths_by_pool: dict[int, list[int]] = {}
    for index, steps in enumerate(all_steps):
        for pool, _ in steps:
            reserves.setdefault(pool.pool_id, [pool.reserve_a * SIMULATION_SCALE, pool.reserve_b * SIMULATION_SCALE])
            paths_by_pool.setdefault(pool.pool_id, []).append(index)
    legs: list[Leg] = []
    cached: list[int | None] = [None] * len(all_steps)
    previous_size = 0
    for size in chunk_sizes(amount_in, chunk_count):
        if size != previous_size:
            cached = [None] * len(all_steps)
            previous_size = size
        best_index = 0
        best_output = -1
        for index, steps in enumerate(all_steps):
            output = cached[index]
            if output is None:
                output = _simulate(steps, reserves, size * SIMULATION_SCALE)
                cached[index] = output
            if output > best_output:
                best_index, best_output = index, output
        _apply(all_steps[best_index], reserves, size * SIMULATION_SCALE)
        legs.append((best_index, size))
        for pool, _ in all_steps[best_index]:
            for index in paths_by_pool[pool.pool_id]:
                cached[index] = None
    return legs


def _execute(paths: Sequence[Path], all_steps: Sequence[_Steps], legs: Sequence[Leg]) -> Allocation:
    """Execute legs in order from the initial reserves, one floor per pool per batch, and sum each path's amounts into one route."""
    count = len(paths)
    sharers: list[list[int]] = []
    pool_sets = [{pool.pool_id for pool, _ in steps} for steps in all_steps]
    for index in range(count):
        sharers.append([other for other in range(count) if other != index and pool_sets[index] & pool_sets[other]])
    reserves: dict[int, list[int]] = {}
    hop_in = [[0] * len(steps) for steps in all_steps]
    hop_out = [[0] * len(steps) for steps in all_steps]
    inputs = [0] * count
    pending = [0] * count

    def flush(index: int) -> None:
        amount = pending[index]
        pending[index] = 0
        for position, (pool, side) in enumerate(all_steps[index]):
            pair = reserves.setdefault(pool.pool_id, [pool.reserve_a, pool.reserve_b])
            output = amount_out(amount, pair[side], pair[1 - side], pool.fee_bps)
            if output >= pair[1 - side]:
                raise AssertionError(f"pool {pool.pool_id} would be drained")
            pair[side] += amount
            pair[1 - side] -= output
            hop_in[index][position] += amount
            hop_out[index][position] += output
            amount = output

    # Consecutive legs of a path are batched until a leg of another path sharing one of its pools arrives, so every pool sees the operations in leg order while disjoint paths execute in one piece.
    for index, amount in legs:
        for other in sharers[index]:
            if pending[other]:
                flush(other)
        pending[index] += amount
        inputs[index] += amount
    for index in range(count):
        if pending[index]:
            flush(index)

    routes: list[Route] = []
    for index, path in enumerate(paths):
        if inputs[index] == 0:
            continue
        hops = tuple(Hop(pool_id, path.currencies[position], path.currencies[position + 1], hop_in[index][position], hop_out[index][position]) for position, pool_id in enumerate(path.pool_ids))
        routes.append(Route(path=path, amount_in=inputs[index], amount_out=hops[-1].amount_out, hops=hops))
    return Allocation(
        amount_in=sum(inputs),
        amount_out=sum(route.amount_out for route in routes),
        routes=tuple(routes),
        final_reserves={pool_id: (pair[0], pair[1]) for pool_id, pair in sorted(reserves.items())},
    )


def _dead_paths(paths: Sequence[Path], allocation: Allocation) -> set[int]:
    """Return the indices of paths whose route realized no output; a positive route output implies every hop amount of that route is positive."""
    return {paths.index(route.path) for route in allocation.routes if route.amount_out == 0}


def realize(paths: Sequence[Path], pools: Iterable[PoolState], legs: Sequence[Leg]) -> Allocation:
    """Execute (path index, amount) legs in order from the initial reserves; legs of paths whose route realizes nothing are left out and the rest re-executed, so amount_in can be below the legs' total."""
    if len(set(paths)) != len(paths):
        raise ValueError("paths must be distinct")
    if any(not 0 <= index < len(paths) or amount < 1 for index, amount in legs):
        raise ValueError("each leg needs a valid path index and a positive amount")
    all_steps = _resolve(paths, pools)
    current = list(legs)
    while True:
        allocation = _execute(paths, all_steps, current)
        dead = _dead_paths(paths, allocation)
        if not dead:
            return allocation
        current = [leg for leg in current if leg[0] not in dead]


def _merge_dead_routes(paths: Sequence[Path], all_steps: Sequence[_Steps], legs: Sequence[Leg]) -> Allocation | None:
    """Move the legs of routes that realize nothing onto the best-paying route until every route pays, or return None when nothing pays."""
    current = list(legs)
    while True:
        allocation = _execute(paths, all_steps, current)
        dead = _dead_paths(paths, allocation)
        if not dead:
            return allocation
        paying = [route for route in allocation.routes if route.amount_out > 0]
        if not paying:
            return None
        target = paths.index(max(paying, key=lambda route: route.amount_out).path)
        current = [(target, amount) if index in dead else (index, amount) for index, amount in current]


def allocate(paths: Sequence[Path], pools: Iterable[PoolState], amount_in: int, *, chunk_count: int) -> Allocation:
    """Split amount_in over paths to maximize the total output, never below the best single path; return an empty allocation when nothing can be received."""
    if amount_in < 1 or chunk_count < 1:
        raise ValueError("amount_in and chunk_count must be positive")
    if len(set(paths)) != len(paths):
        raise ValueError("paths must be distinct")
    empty = Allocation(amount_in=amount_in, amount_out=0, routes=(), final_reserves={})
    all_steps = _resolve(paths, pools)
    if not paths:
        return empty

    candidates: list[Allocation] = []
    best_single: Allocation | None = None
    for index in range(len(paths)):
        single = _execute(paths, all_steps, [(index, amount_in)])
        if single.amount_out > 0 and (best_single is None or single.amount_out > best_single.amount_out):
            best_single = single
    if best_single is not None:
        candidates.append(best_single)
    multi = _merge_dead_routes(paths, all_steps, _greedy_legs(all_steps, amount_in, chunk_count))
    if multi is not None:
        candidates.append(multi)
    if not candidates:
        return empty
    # min keeps the first of equal keys, so the single path wins unless the split is strictly better.
    return min(candidates, key=lambda allocation: (-allocation.amount_out, len(allocation.routes)))
