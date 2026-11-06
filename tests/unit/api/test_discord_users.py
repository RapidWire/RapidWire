from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from rapidwire.api.discord_users import (
    AUTH_FAILURE_PAUSE_SECONDS,
    CACHE_CAPACITY,
    CACHE_TTL_SECONDS,
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_CONCURRENT_LOOKUPS,
    MAX_RETRY_AFTER_SECONDS,
    NOT_FOUND_TTL_SECONDS,
    DiscordUnavailable,
    DiscordUserNotFound,
    DiscordUsers,
)

BASE_URL = "https://discord.test/api/v10"
ALICE = 1269970084965912747


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Upstream:
    """A scripted Discord: names answers 200, statuses forces a status per user, and calls counts requests per user."""

    def __init__(self) -> None:
        self.names: dict[int, str] = {}
        self.statuses: dict[int, httpx.Response] = {}
        self.calls: dict[int, int] = {}
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        user_id = int(request.url.path.rsplit("/", 1)[1])
        assert request.url.path == f"/api/v10/users/{user_id}"
        self.calls[user_id] = self.calls.get(user_id, 0) + 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if user_id in self.statuses:
            return self.statuses[user_id]
        if user_id in self.names:
            return httpx.Response(200, json={"id": str(user_id), "username": self.names[user_id]})
        return httpx.Response(404, json={"message": "Unknown User", "code": 10013})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
async def client(upstream: Upstream) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(upstream)) as http:
        yield http


def _users(client: httpx.AsyncClient, clock: Clock, *, configured: bool = True, capacity: int = CACHE_CAPACITY) -> DiscordUsers:
    return DiscordUsers(client, configured=configured, capacity=capacity, clock=clock)


def test_defaults_match_the_old_cache() -> None:
    assert CACHE_CAPACITY == 100
    assert CACHE_TTL_SECONDS == 86_400


async def test_hit_does_not_call_discord(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    users = _users(client, clock)
    assert await users.username(ALICE) == "alice"
    upstream.names[ALICE] = "renamed"
    clock.now += CACHE_TTL_SECONDS
    assert await users.username(ALICE) == "alice"
    assert upstream.calls == {ALICE: 1}


async def test_expired_entry_is_refetched(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    users = _users(client, clock)
    await users.username(ALICE)
    upstream.names[ALICE] = "renamed"
    clock.now += CACHE_TTL_SECONDS + 0.5
    assert await users.username(ALICE) == "renamed"
    assert await users.username(ALICE) == "renamed"
    assert upstream.calls == {ALICE: 2}


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={"id": "1"}), httpx.Response(200, content=b"not json"), httpx.Response(429, json={"retry_after": 2.5})],
)
async def test_stale_name_is_served_when_discord_fails(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, response: httpx.Response) -> None:
    upstream.names[ALICE] = "alice"
    users = _users(client, clock)
    await users.username(ALICE)
    clock.now += CACHE_TTL_SECONDS + 1
    upstream.statuses[ALICE] = response
    assert await users.username(ALICE) == "alice"
    del upstream.statuses[ALICE]
    upstream.names[ALICE] = "renamed"
    clock.now += DEFAULT_RETRY_AFTER_SECONDS
    assert await users.username(ALICE) == "renamed"


async def test_stale_name_is_served_on_transport_errors(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    users = _users(client, clock)
    await users.username(ALICE)
    clock.now += CACHE_TTL_SECONDS + 1
    upstream.error = httpx.ConnectTimeout("timed out")
    assert await users.username(ALICE) == "alice"
    upstream.error = None
    upstream.names[ALICE] = "renamed"
    assert await users.username(ALICE) == "renamed"


async def test_unknown_user_is_not_found_and_remembered_briefly(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    users = _users(client, clock)
    with pytest.raises(DiscordUserNotFound) as raised:
        await users.username(ALICE)
    assert raised.value.code == "user_not_found"
    upstream.names[ALICE] = "alice"
    clock.now += NOT_FOUND_TTL_SECONDS
    with pytest.raises(DiscordUserNotFound):
        await users.username(ALICE)
    assert upstream.calls == {ALICE: 1}
    clock.now += 1
    assert await users.username(ALICE) == "alice"
    assert upstream.calls == {ALICE: 2}


async def test_not_found_on_refresh_keeps_the_cached_name(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    # As in the old version, a user Discord no longer finds keeps the name it had.
    upstream.names[ALICE] = "alice"
    users = _users(client, clock)
    await users.username(ALICE)
    del upstream.names[ALICE]
    clock.now += CACHE_TTL_SECONDS + 1
    assert await users.username(ALICE) == "alice"
    assert await users.username(ALICE) == "alice"
    assert ALICE in users
    assert upstream.calls == {ALICE: 2}
    upstream.names[ALICE] = "back"
    clock.now += NOT_FOUND_TTL_SECONDS + 1
    assert await users.username(ALICE) == "back"


async def test_negative_cache_is_bounded(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    users = _users(client, clock, capacity=2)
    for user_id in (1, 2, 3):
        with pytest.raises(DiscordUserNotFound):
            await users.username(user_id)
    with pytest.raises(DiscordUserNotFound):
        await users.username(1)
    assert upstream.calls == {1: 2, 2: 1, 3: 1}


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_token_pauses_lookups_and_logs_once(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, caplog: pytest.LogCaptureFixture, status: int) -> None:
    other = ALICE - 1
    upstream.statuses[ALICE] = httpx.Response(status)
    upstream.statuses[other] = httpx.Response(status)
    users = _users(client, clock)
    with caplog.at_level(logging.WARNING, logger="rapidwire.api.discord_users"):
        with pytest.raises(DiscordUnavailable) as raised:
            await users.username(ALICE)
        assert raised.value.retry_after == pytest.approx(AUTH_FAILURE_PAUSE_SECONDS)
        clock.now += 10
        with pytest.raises(DiscordUnavailable) as paused:
            await users.username(other)
        assert paused.value.retry_after == pytest.approx(AUTH_FAILURE_PAUSE_SECONDS - 10)
        assert upstream.calls == {ALICE: 1}
        clock.now += AUTH_FAILURE_PAUSE_SECONDS
        with pytest.raises(DiscordUnavailable):
            await users.username(other)
    assert upstream.calls == {ALICE: 1, other: 1}
    assert len([record for record in caplog.records if str(status) in record.getMessage()]) == 1


async def test_upstream_requests_are_bounded(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    active = 0
    peak = 0
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1
        user_id = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"id": user_id, "username": f"user{user_id}"})

    async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as bounded:
        users = DiscordUsers(bounded, configured=True, clock=clock)
        waiters = [asyncio.ensure_future(users.username(user_id)) for user_id in range(1, 3 * MAX_CONCURRENT_LOOKUPS + 1)]
        await asyncio.sleep(0.05)
        assert peak == MAX_CONCURRENT_LOOKUPS
        gate.set()
        assert await asyncio.gather(*waiters) == [f"user{user_id}" for user_id in range(1, 3 * MAX_CONCURRENT_LOOKUPS + 1)]
    assert peak == MAX_CONCURRENT_LOOKUPS


async def test_close_answers_in_flight_lookups_without_errors(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, caplog: pytest.LogCaptureFixture) -> None:
    upstream.names[ALICE] = "alice"
    upstream.names[ALICE - 1] = "bob"
    users = _users(client, clock)
    await users.username(ALICE)
    clock.now += CACHE_TTL_SECONDS + 1
    upstream.gate = asyncio.Event()
    stale = asyncio.ensure_future(users.username(ALICE))
    uncached = asyncio.ensure_future(users.username(ALICE - 1))
    await asyncio.sleep(0.01)
    with caplog.at_level(logging.ERROR):
        await users.aclose()
        assert await stale == "alice"
        with pytest.raises(DiscordUnavailable):
            await uncached
        with pytest.raises(DiscordUnavailable):
            await users.username(ALICE - 1)
    assert caplog.records == []


async def test_closed_http_client_is_unavailable(clock: Clock, upstream: Upstream) -> None:
    closed = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(upstream))
    await closed.aclose()
    with pytest.raises(DiscordUnavailable):
        await _users(closed, clock).username(ALICE)


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(403), httpx.Response(429, json={"retry_after": 1})])
async def test_failure_without_cache_is_unavailable(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, response: httpx.Response) -> None:
    upstream.statuses[ALICE] = response
    with pytest.raises(DiscordUnavailable):
        await _users(client, clock).username(ALICE)


async def test_unconfigured_client_never_calls_discord(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    with pytest.raises(DiscordUnavailable) as raised:
        await _users(client, clock, configured=False).username(ALICE)
    assert raised.value.retry_after is None
    assert upstream.calls == {}


async def test_rate_limit_pauses_upstream_requests(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    other = ALICE - 1
    upstream.statuses[ALICE] = httpx.Response(429, json={"retry_after": 30.0, "global": True})
    upstream.names[other] = "bob"
    users = _users(client, clock)
    with pytest.raises(DiscordUnavailable) as raised:
        await users.username(ALICE)
    assert raised.value.retry_after == pytest.approx(30.0)
    clock.now += 10
    with pytest.raises(DiscordUnavailable) as again:
        await users.username(other)
    assert again.value.retry_after == pytest.approx(20.0)
    assert upstream.calls == {ALICE: 1}
    clock.now += 20
    assert await users.username(other) == "bob"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(429, headers={"Retry-After": "7"}), 7.0),
        (httpx.Response(429, json={"retry_after": "x"}, headers={"Retry-After": "3"}), 3.0),
        (httpx.Response(429), DEFAULT_RETRY_AFTER_SECONDS),
        (httpx.Response(429, json={"retry_after": 10**9}), MAX_RETRY_AFTER_SECONDS),
        (httpx.Response(429, json={"retry_after": True}), DEFAULT_RETRY_AFTER_SECONDS),
    ],
)
async def test_rate_limit_wait_sources(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, response: httpx.Response, expected: float) -> None:
    upstream.statuses[ALICE] = response
    with pytest.raises(DiscordUnavailable) as raised:
        await _users(client, clock).username(ALICE)
    assert raised.value.retry_after == pytest.approx(expected)


async def test_least_recently_used_entry_is_evicted(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    for user_id in (1, 2, 3, 4):
        upstream.names[user_id] = f"user{user_id}"
    users = _users(client, clock, capacity=3)
    for user_id in (1, 2, 3):
        await users.username(user_id)
    await users.username(1)
    await users.username(4)
    assert len(users) == 3
    assert [user_id in users for user_id in (1, 2, 3, 4)] == [True, False, True, True]
    await users.username(2)
    assert upstream.calls == {1: 1, 2: 2, 3: 1, 4: 1}


async def test_concurrent_requests_share_one_fetch(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    upstream.gate = asyncio.Event()
    users = _users(client, clock)
    waiters = [asyncio.ensure_future(users.username(ALICE)) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert upstream.calls == {ALICE: 1}
    upstream.gate.set()
    assert await asyncio.gather(*waiters) == ["alice"] * 5
    assert upstream.calls == {ALICE: 1}


async def test_shared_fetch_survives_a_cancelled_waiter(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.names[ALICE] = "alice"
    upstream.gate = asyncio.Event()
    users = _users(client, clock)
    first = asyncio.ensure_future(users.username(ALICE))
    second = asyncio.ensure_future(users.username(ALICE))
    await asyncio.sleep(0.01)
    first.cancel()
    upstream.gate.set()
    assert await second == "alice"
    with pytest.raises(asyncio.CancelledError):
        await first
    assert ALICE in users
    assert upstream.calls == {ALICE: 1}


async def test_concurrent_not_found_is_shared(client: httpx.AsyncClient, clock: Clock, upstream: Upstream) -> None:
    upstream.gate = asyncio.Event()
    users = _users(client, clock)
    waiters = [asyncio.ensure_future(users.username(ALICE)) for _ in range(3)]
    await asyncio.sleep(0.01)
    upstream.gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, DiscordUserNotFound) for result in results)
    assert upstream.calls == {ALICE: 1}


@pytest.mark.parametrize("user_id", [0, -1, 2**63, True])
async def test_invalid_ids_are_rejected_before_discord(client: httpx.AsyncClient, clock: Clock, upstream: Upstream, user_id: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        await _users(client, clock).username(user_id)
    assert upstream.calls == {}


def test_invalid_configuration_is_rejected(client: httpx.AsyncClient, clock: Clock) -> None:
    with pytest.raises(ValueError, match="capacity"):
        DiscordUsers(client, configured=True, capacity=0, clock=clock)
    with pytest.raises(ValueError, match="max_concurrent"):
        DiscordUsers(client, configured=True, max_concurrent=0, clock=clock)
