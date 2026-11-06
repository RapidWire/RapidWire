"""Pure parts of the worker: the shared Discord REST definitions, retry delays, nonces, rate-limit routes, job validation and the worker command's configuration checks."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api import deps
from rapidwire.cli import COMMANDS, EXIT_CONFIG, main
from rapidwire.discord_rest import (
    DEFAULT_RETRY_AFTER_SECONDS,
    DISCORD_API_BASE_URL,
    DISCORD_USER_AGENT,
    MAX_RETRY_AFTER_SECONDS,
    DiscordError,
    RateLimit,
    discord_client,
    read_error,
    read_rate_limit,
    retry_after,
)
from rapidwire.worker.dispatcher import MAX_DELIVERY_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, Dispatcher, _route_key, message_nonce, retry_delay
from rapidwire.worker.scheduler import Job


def test_user_agent_is_the_old_versions_and_shared_with_the_api() -> None:
    assert DISCORD_USER_AGENT == "DiscordBot (https://github.com/RapidWire/RapidWire, 1.0)"
    assert deps.DISCORD_USER_AGENT is DISCORD_USER_AGENT
    assert deps.DISCORD_API_BASE_URL is DISCORD_API_BASE_URL


async def test_client_headers() -> None:
    async with discord_client("secret-token") as client:
        assert client.headers["Authorization"] == "Bot secret-token"
        assert client.headers["User-Agent"] == DISCORD_USER_AGENT
        assert str(client.base_url) == DISCORD_API_BASE_URL + "/"
    async with discord_client(None) as client:
        assert "Authorization" not in client.headers


@pytest.mark.parametrize(
    ("response", "seconds"),
    [
        (httpx.Response(429, json={"retry_after": 1.5}), 1.5),
        (httpx.Response(429, headers={"Retry-After": "3"}), 3.0),
        (httpx.Response(429, json={"retry_after": "bad"}, headers={"Retry-After": "2"}), 2.0),
        (httpx.Response(429), DEFAULT_RETRY_AFTER_SECONDS),
        (httpx.Response(429, json={"retry_after": 10**9}), MAX_RETRY_AFTER_SECONDS),
        (httpx.Response(429, json={"retry_after": -4}), 0.0),
        (httpx.Response(429, json={"retry_after": True}), DEFAULT_RETRY_AFTER_SECONDS),
    ],
)
def test_retry_after(response: httpx.Response, seconds: float) -> None:
    assert retry_after(response) == seconds


def test_read_rate_limit() -> None:
    headers = {"X-RateLimit-Bucket": "abc", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.25", "X-RateLimit-Scope": "user"}
    assert read_rate_limit(httpx.Response(200, headers=headers)) == RateLimit(bucket="abc", remaining=0, reset_after=1.25, is_global=False, scope="user")
    assert read_rate_limit(httpx.Response(429, json={"global": True})).is_global
    assert read_rate_limit(httpx.Response(429, headers={"X-RateLimit-Global": "true"})).is_global
    assert read_rate_limit(httpx.Response(429, headers={"X-RateLimit-Scope": "global"})).is_global
    assert not read_rate_limit(httpx.Response(429, json={"global": False}, headers={"X-RateLimit-Scope": "shared"})).is_global
    assert read_rate_limit(httpx.Response(200)) == RateLimit(bucket=None, remaining=None, reset_after=None, is_global=False, scope=None)


def test_read_error() -> None:
    assert read_error(httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"})) == DiscordError(code="50013", message="Missing Permissions")
    assert read_error(httpx.Response(400, json={"message": "x" * 500})).message == "x" * 200
    assert read_error(httpx.Response(502, text="<html>")) == DiscordError(code=None, message=None)


def test_retry_delay_is_exponential_capped_and_jittered() -> None:
    assert retry_delay(1, rand=lambda: 0.0) == RETRY_BASE_SECONDS / 2
    assert retry_delay(1, rand=lambda: 1.0) == RETRY_BASE_SECONDS
    assert retry_delay(3, rand=lambda: 1.0) == RETRY_BASE_SECONDS * 4
    assert retry_delay(60, rand=lambda: 1.0) == RETRY_MAX_SECONDS
    assert retry_delay(60, rand=lambda: 0.0) == RETRY_MAX_SECONDS / 2
    assert MAX_DELIVERY_ATTEMPTS >= 2


def test_message_nonce_fits_discord() -> None:
    assert message_nonce(1) == "rw1"
    assert len(message_nonce(2**63 - 1)) <= 25


def test_route_key_separates_the_major_parameter() -> None:
    assert _route_key("POST", "channels/123/messages") == ("POST channels/:id/messages", "123")
    assert _route_key("POST", "channels/456/messages") == ("POST channels/:id/messages", "456")
    assert _route_key("PUT", "guilds/1/members/2/roles/3") == ("PUT guilds/:id/members/:id/roles/:id", "1")
    assert _route_key("POST", "users/@me/channels") == ("POST users/@me/channels", "")


async def test_expired_bucket_resets_are_pruned() -> None:
    async with discord_client(None) as client:
        dispatcher = Dispatcher(cast(AsyncEngine, None), client)
        exhausted = httpx.Response(200, headers={"X-RateLimit-Bucket": "chan", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0"})
        for channel in range(1, 1001):
            dispatcher._note_rate_limit(_route_key("GET", f"channels/{channel}"), exhausted, None)
        templates, resets = dispatcher.rate_limit_state()
    assert templates == 1
    assert resets <= 2


def test_job_interval_must_be_positive() -> None:
    async def tick() -> None:
        return None

    with pytest.raises(ValueError, match="interval"):
        Job("x", 0, tick)


def test_worker_is_implemented_and_needs_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert "worker" in COMMANDS
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "worker-test-password")
    assert main(["worker"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DISCORD_TOKEN is required" in capsys.readouterr().err
