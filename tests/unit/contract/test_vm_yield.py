"""The interpreter yields to the event loop at every periodic deadline check with the wall clock stopped, so a compute-bound contract shares the loop and other tasks' time is not charged to it."""

from __future__ import annotations

import asyncio

import pytest

from rapidwire.contract.compiler import compile_source
from rapidwire.contract.errors import LimitExceeded, LimitResource
from rapidwire.contract.limits import DEADLINE_CHECK_INTERVAL, Limits
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.contract.vm import Deadline, execute
from tests.unit.contract.helpers import FakeHost, context

COUNT = """from rapidwire_contract import *

@public
def count(n: int) -> int:
    i: int = 0
    while i < n:
        i = i + 1
    return i
"""
LOOPS = 20_000


def _program() -> VerifiedProgram:
    return verify(compile_source(COUNT), max_size=8000)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_other_tasks_run_while_a_contract_computes() -> None:
    progress: list[int] = []
    finished = asyncio.Event()

    async def ticker() -> None:
        while not finished.is_set():
            progress.append(len(progress))
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = len(progress)
    try:
        outcome = await execute(_program(), "count", [LOOPS], host=FakeHost(), context=context(gas=10**9))
    finally:
        finished.set()
        await task
    assert outcome.value == LOOPS
    # The loop runs several instructions per iteration, so it reaches many deadline checks, and the ticker ran at each of them.
    assert len(progress) - started >= LOOPS // DEADLINE_CHECK_INTERVAL


async def test_time_spent_in_other_tasks_is_not_charged() -> None:
    clock = _Clock()
    stop = asyncio.Event()

    async def slow_neighbour() -> None:
        while not stop.is_set():
            clock.now += 5.0
            await asyncio.sleep(0)

    neighbour = asyncio.create_task(slow_neighbour())
    await asyncio.sleep(0)
    try:
        ctx = context(gas=10**9, limits=Limits(wall_clock_seconds=1.0), clock=clock)
        outcome = await execute(_program(), "count", [LOOPS], host=FakeHost(), context=ctx)
    finally:
        stop.set()
        await neighbour
    assert outcome.value == LOOPS
    assert clock.now > 1000.0 + 5.0 * (LOOPS // DEADLINE_CHECK_INTERVAL)


async def test_the_contract_own_time_still_counts() -> None:
    clock = _Clock()

    def ticking() -> float:
        clock.now += 0.001
        return clock.now

    ctx = context(gas=10**12, limits=Limits(wall_clock_seconds=1.0), clock=ticking)
    with pytest.raises(LimitExceeded) as caught:
        await execute(_program(), "count", [10**12], host=FakeHost(), context=ctx)
    assert caught.value.resource is LimitResource.WALL_CLOCK


async def test_pause_stops_the_clock() -> None:
    clock = _Clock()
    deadline = Deadline(1.0, clock)

    async def jump() -> None:
        clock.now += 50.0

    task = asyncio.create_task(jump())
    await deadline.pause()
    await task
    assert clock.now == 1050.0
    deadline.check()
    assert deadline.at == pytest.approx(1051.0)
