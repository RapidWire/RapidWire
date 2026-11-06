"""Deadline.suspended(): time inside the block does not count against the wall-clock limit, nested suspensions count once, and the clock runs again afterwards."""

from __future__ import annotations

import pytest

from rapidwire.contract.errors import LimitExceeded
from rapidwire.contract.vm import Deadline


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_suspended_time_does_not_count() -> None:
    clock = _Clock()
    deadline = Deadline(2.0, clock)
    clock.now += 1.5
    with deadline.suspended():
        clock.now += 10.0
        with deadline.suspended():
            clock.now += 5.0
    deadline.check()
    assert deadline.at == pytest.approx(117.0)
    clock.now += 0.4
    deadline.check()
    clock.now += 0.2
    with pytest.raises(LimitExceeded):
        deadline.check()


def test_suspension_ends_when_the_block_raises() -> None:
    clock = _Clock()
    deadline = Deadline(1.0, clock)
    with pytest.raises(RuntimeError), deadline.suspended():
        clock.now += 3.0
        raise RuntimeError("boom")
    clock.now += 0.5
    deadline.check()
    clock.now += 0.6
    with pytest.raises(LimitExceeded):
        deadline.check()
