"""The outbox dispatcher against a fake Discord: claim DMs, contract messages and role changes, skips, retries, rate limits, terminal failures, the delivery record of an execution, and concurrent dispatchers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.bot.views.claim import claim_notification_message
from rapidwire.config import Settings
from rapidwire.contract.values import Account
from rapidwire.core import claims, notifications, queries
from rapidwire.core.clock import FixedClock
from rapidwire.core.discord_permissions import deny_discord
from rapidwire.core.outbox import enqueue
from rapidwire.core.types import OutboxKind, OutboxStatus
from rapidwire.discord_rest import DISCORD_USER_AGENT, discord_client
from rapidwire.worker import dispatcher as dispatcher_module
from rapidwire.worker.dispatcher import ALLOWED_MENTIONS, MAX_DELIVERY_ATTEMPTS, RETRY_MAX_SECONDS, Dispatcher, DispatchStats, message_nonce
from rapidwire_client import ExecutionDetail
from rapidwire_client._parse import parse_as
from tests.integration.contract_runtime_helpers import CALLER, NOW
from tests.integration.worker.fake_discord import FakeDiscord
from tests.integration.worker.helpers import (
    CHANNEL,
    CLAIMANT,
    CONTRACT_A,
    CONTRACT_B,
    DISPATCH_AT,
    GUILD,
    OTHER_GUILD,
    OWNER,
    PAYER,
    ROLE,
    call,
    create_notified_claim,
    guilds,
    ledger_snapshot,
    outbox_rows,
    setup_contracts,
)

TOKEN = "fake-bot-token-for-tests"


@pytest.fixture
def fake() -> FakeDiscord:
    discord = FakeDiscord()
    discord.channels[CHANNEL] = GUILD
    return discord


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(DISPATCH_AT)


@pytest_asyncio.fixture(loop_scope="session")
async def http(fake: FakeDiscord) -> AsyncIterator[httpx.AsyncClient]:
    async with discord_client(TOKEN, transport=fake.transport()) as client:
        yield client


@pytest.fixture
def dispatcher(db: AsyncEngine, http: httpx.AsyncClient, clock: FixedClock) -> Dispatcher:
    return Dispatcher(db, http, clock=clock, rand=lambda: 0.0)


async def _only_row(engine: AsyncEngine) -> dict[str, Any]:
    [row] = await outbox_rows(engine)
    return row


async def test_claim_dm_is_sent_to_the_payer(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, clock: FixedClock) -> None:
    created = await create_notified_claim(db)
    row = await _only_row(db)
    before = await ledger_snapshot(db)

    assert await dispatcher.run_once() == DispatchStats(sent=1)

    dm = fake.dm_channels[PAYER]
    assert fake.paths() == [("POST", "users/@me/channels"), ("POST", f"channels/{dm}/messages")]
    assert json.loads(fake.requests[0].content) == {"recipient_id": str(PAYER)}
    for request in fake.requests:
        assert request.headers["Authorization"] == f"Bot {TOKEN}"
        assert request.headers["User-Agent"] == DISCORD_USER_AGENT
    [posted] = fake.posted
    expected = claim_notification_message(row["payload"])
    assert {key: value for key, value in posted.body.items() if key not in ("nonce", "enforce_nonce")} == expected
    assert (posted.body["nonce"], posted.body["enforce_nonce"]) == (message_nonce(row["outbox_id"]), True)
    assert [component["custom_id"] for component in expected["components"][0]["components"]] == [f"rw:claim:{action}:{created.claim_id}" for action in ("pay", "deny", "mute")]
    sent = await _only_row(db)
    assert (sent["status"], sent["attempts"], sent["completed_at"], sent["last_error"]) == ("sent", 1, clock.now(), None)
    assert sent["result"] == {"status": "sent", "channel_id": str(dm), "message_id": str(posted.message_id)}
    assert await ledger_snapshot(db) == before
    detail = await queries.get_execution(db, created.execution_id)
    assert detail is not None and detail.deliveries == ()


async def _pay(engine: AsyncEngine, claim_id: int) -> None:
    await claims.pay_claim(engine, claim_id=claim_id, payer_id=PAYER, now=NOW)


async def _cancel(engine: AsyncEngine, claim_id: int) -> None:
    await claims.cancel_claim(engine, claim_id=claim_id, user_id=CLAIMANT, now=NOW)


async def _deny(engine: AsyncEngine, claim_id: int) -> None:
    await notifications.deny_notifications(engine, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW)


@pytest.mark.parametrize(("change", "reason"), [(_pay, "claim_paid"), (_cancel, "claim_canceled"), (_deny, "notification_revoked")])
async def test_claim_dm_is_skipped_when_no_longer_wanted(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, change: Any, reason: str) -> None:
    created = await create_notified_claim(db)
    await change(db, created.claim_id)

    assert await dispatcher.run_once() == DispatchStats(skipped=1)

    assert fake.requests == []
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"]) == ("skipped", 0, {"status": "skipped", "reason": reason})


async def test_claim_dm_refused_by_the_payer_fails(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    await create_notified_claim(db)
    fake.dm_channels[PAYER] = 777_000
    fake.script("POST", "channels/777000/messages", httpx.Response(403, json={"code": 50007, "message": "Cannot send messages to this user"}))

    assert await dispatcher.run_once() == DispatchStats(failed=1)

    row = await _only_row(db)
    assert (row["status"], row["attempts"]) == ("failed", 1)
    assert row["result"] == {"status": "failed", "reason": "dm_refused", "http_status": "403", "code": "50007", "message": "Cannot send messages to this user"}


async def test_contract_message_is_sent_and_recorded_with_the_execution(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, clock: FixedClock, tmp_path: Path) -> None:
    await setup_contracts(db)
    result = await call(db, "shout", ["hi <@123> @everyone <@&456>"])
    before = await ledger_snapshot(db)
    pending = await queries.get_execution(db, result.execution_id)
    assert pending is not None
    [queued] = pending.deliveries
    assert (queued.status, queued.attempts, queued.next_attempt_at, queued.completed_at, queued.message_id) == (OutboxStatus.PENDING, 0, NOW, None, None)

    assert await dispatcher.run_once() == DispatchStats(sent=1)

    assert fake.paths() == [("GET", f"channels/{CHANNEL}"), ("POST", f"channels/{CHANNEL}/messages")]
    [posted] = fake.posted
    row = await _only_row(db)
    assert posted.body == {"content": "hi <@123> @everyone <@&456>", "allowed_mentions": ALLOWED_MENTIONS, "nonce": message_nonce(row["outbox_id"]), "enforce_nonce": True}
    assert ALLOWED_MENTIONS == {"parse": ["users"]}
    assert await ledger_snapshot(db) == before

    detail = await queries.get_execution(db, result.execution_id)
    assert detail is not None
    assert detail.deliveries == (
        queries.Delivery(
            outbox_id=row["outbox_id"],
            kind=OutboxKind.DISCORD_SEND,
            guild_id=GUILD,
            channel_id=CHANNEL,
            user_id=None,
            role_id=None,
            status=OutboxStatus.SENT,
            attempts=1,
            created_at=NOW,
            next_attempt_at=None,
            completed_at=clock.now(),
            message_id=posted.message_id,
            error=None,
        ),
    )

    settings = Settings(_env_file=None, db_password="api-test-password", backup_dir=tmp_path)
    app = create_app(settings, engine=db, guild_info=guilds(), discord_transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
        body = (await api.get(f"/v1/executions/{result.execution_id}")).json()
    assert body["deliveries"] == [
        {
            "outbox_id": str(row["outbox_id"]),
            "kind": "discord_send",
            "guild_id": str(GUILD),
            "channel_id": str(CHANNEL),
            "user_id": None,
            "role_id": None,
            "status": "sent",
            "attempts": 1,
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
            "next_attempt_at": None,
            "completed_at": clock.now().isoformat().replace("+00:00", "Z"),
            "message_id": str(posted.message_id),
            "error": None,
        }
    ]
    assert "hi <@123>" not in str(body["deliveries"])
    parsed = parse_as(ExecutionDetail, body)
    assert parsed.deliveries[0].message_id == posted.message_id and parsed.deliveries[0].error is None


async def test_nested_execution_deliveries_belong_to_the_child(db: AsyncEngine, dispatcher: Dispatcher) -> None:
    await setup_contracts(db)
    result = await call(db, "relay", [Account(CONTRACT_B), "inner"])
    await dispatcher.run_once()
    detail = await queries.get_execution(db, result.execution_id)
    assert detail is not None
    assert [(item.kind, item.status) for item in detail.deliveries] == [(OutboxKind.DISCORD_SEND, OutboxStatus.SENT)]
    [child] = detail.children
    assert [(item.kind, item.status) for item in child.deliveries] == [(OutboxKind.DISCORD_SEND, OutboxStatus.SENT)]


async def test_channel_of_another_guild_fails(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    await setup_contracts(db)
    fake.channels[CHANNEL] = OTHER_GUILD
    result = await call(db, "shout", ["hi"])

    assert await dispatcher.run_once() == DispatchStats(failed=1)

    assert fake.paths() == [("GET", f"channels/{CHANNEL}")]
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"]) == ("failed", 1, {"status": "failed", "reason": "channel_not_in_guild"})
    detail = await queries.get_execution(db, result.execution_id)
    assert detail is not None
    assert detail.deliveries[0].error == queries.DeliveryError(reason="channel_not_in_guild", http_status=None, code=None, message=None)


async def test_revoked_permission_fails_without_a_request(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    info = await setup_contracts(db)
    await call(db, "shout", ["hi"])
    await deny_discord(db, info, guild_id=GUILD, account_id=CONTRACT_A, acting_user_id=OWNER, now=NOW)

    assert await dispatcher.run_once() == DispatchStats(failed=1)

    assert fake.requests == []
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"]) == ("failed", 0, {"status": "failed", "reason": "permission_revoked"})


async def test_role_changes_are_made_with_an_audit_reason(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    await setup_contracts(db)
    result = await call(db, "grant", [Account(CALLER)])
    before = await ledger_snapshot(db)

    assert await dispatcher.run_once() == DispatchStats(sent=2)

    reason = f"RapidWire contract {CONTRACT_A} execution {result.execution_id}"
    assert fake.roles == [("PUT", GUILD, CALLER, ROLE, reason), ("DELETE", GUILD, CALLER, ROLE + 1, reason)]
    assert [(row["status"], row["result"]) for row in await outbox_rows(db)] == [("sent", {"status": "sent"})] * 2
    assert await ledger_snapshot(db) == before
    detail = await queries.get_execution(db, result.execution_id)
    assert detail is not None
    assert [(item.kind, item.user_id, item.role_id, item.channel_id, item.status) for item in detail.deliveries] == [
        (OutboxKind.DISCORD_ADD_ROLE, CALLER, ROLE, None, OutboxStatus.SENT),
        (OutboxKind.DISCORD_REMOVE_ROLE, CALLER, ROLE + 1, None, OutboxStatus.SENT),
    ]


@pytest.mark.parametrize(
    "limited",
    [
        httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 0.05, "global": False}, headers={"X-RateLimit-Bucket": "b1", "X-RateLimit-Scope": "user"}),
        httpx.Response(429, headers={"Retry-After": "0.05"}),
    ],
)
async def test_short_rate_limit_is_waited_out_in_place(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, limited: httpx.Response) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    fake.script("POST", f"channels/{CHANNEL}/messages", limited)

    assert await dispatcher.run_once() == DispatchStats(sent=1)

    assert fake.paths().count(("POST", f"channels/{CHANNEL}/messages")) == 2
    row = await _only_row(db)
    assert (row["status"], row["attempts"]) == ("sent", 1)


async def test_long_rate_limit_reschedules_without_using_an_attempt(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, clock: FixedClock) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    fake.script("POST", f"channels/{CHANNEL}/messages", httpx.Response(429, json={"retry_after": 30.0, "global": False}))

    assert await dispatcher.run_once() == DispatchStats(deferred=1)
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["available_at"]) == ("pending", 0, clock.now() + timedelta(seconds=30))
    assert row["last_error"] == "rate limited for 30.000 seconds"
    assert await dispatcher.run_once() == DispatchStats()

    clock.advance(timedelta(seconds=30))
    assert await dispatcher.run_once() == DispatchStats(sent=1)
    assert (await _only_row(db))["attempts"] == 1


async def test_exhausted_bucket_is_waited_for_before_the_next_request(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["one"])
    await call(db, "shout", ["two"])
    headers = {"X-RateLimit-Bucket": "chan", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"}
    fake.script("GET", f"channels/{CHANNEL}", httpx.Response(200, json={"id": str(CHANNEL), "guild_id": str(GUILD)}, headers=headers))

    assert await dispatcher.run_once() == DispatchStats(sent=1, deferred=1)
    assert fake.paths() == [("GET", f"channels/{CHANNEL}"), ("POST", f"channels/{CHANNEL}/messages")]
    rows = await outbox_rows(db)
    assert [(row["status"], row["attempts"]) for row in rows] == [("sent", 1), ("pending", 0)]
    assert rows[1]["last_error"] is not None and rows[1]["last_error"].startswith("rate limited for 29.")


async def test_rate_limits_are_kept_per_channel(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    other = CHANNEL + 10
    fake.channels[other] = GUILD
    await setup_contracts(db)
    await call(db, "shout", ["one"])
    await call(db, "shout_at", [other, "two"])
    await call(db, "shout", ["three"])
    exhausted = {"X-RateLimit-Bucket": "chan", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"}
    fresh = {"X-RateLimit-Bucket": "chan", "X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "1"}
    fake.script("GET", f"channels/{CHANNEL}", httpx.Response(200, json={"id": str(CHANNEL), "guild_id": str(GUILD)}, headers=exhausted))
    fake.script("GET", f"channels/{other}", httpx.Response(200, json={"id": str(other), "guild_id": str(GUILD)}, headers=fresh))

    assert await dispatcher.run_once() == DispatchStats(sent=2, deferred=1)

    assert [item.body["content"] for item in fake.posted] == ["one", "two"]
    assert [(row["status"], row["attempts"]) for row in await outbox_rows(db)] == [("sent", 1), ("sent", 1), ("pending", 0)]
    assert dispatcher.rate_limit_state() == (1, 1)


async def test_global_rate_limit_pauses_the_dispatcher(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    await call(db, "shout", ["again"])
    before = await outbox_rows(db)
    fake.script("GET", f"channels/{CHANNEL}", httpx.Response(429, json={"retry_after": 30.0, "global": True}, headers={"X-RateLimit-Global": "true"}))

    assert await dispatcher.run_once() == DispatchStats()

    assert len(fake.requests) == 1
    assert await outbox_rows(db) == before
    assert 25 < dispatcher.paused_for() <= 30
    assert await dispatcher.run_once() == DispatchStats()
    assert len(fake.requests) == 1


async def test_unauthorized_token_pauses_without_failing_rows(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, caplog: pytest.LogCaptureFixture) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    before = await outbox_rows(db)
    fake.script("GET", f"channels/{CHANNEL}", httpx.Response(401, json={"code": 0, "message": "401: Unauthorized"}))

    assert await dispatcher.run_once() == DispatchStats()

    assert await outbox_rows(db) == before
    assert dispatcher.paused_for() > 0
    assert "Discord refused the bot token with status 401; the dispatcher pauses" in caplog.messages
    assert TOKEN not in caplog.text
    assert await dispatcher.run_once() == DispatchStats()
    assert len(fake.requests) == 1


async def test_server_errors_are_retried_with_backoff(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, clock: FixedClock) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    fake.script("POST", f"channels/{CHANNEL}/messages", httpx.Response(503), httpx.Response(502))

    assert await dispatcher.run_once() == DispatchStats(retried=1)
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["available_at"], row["last_error"]) == ("pending", 1, clock.now() + timedelta(seconds=2.5), "Discord answered status 503")
    assert await dispatcher.run_once() == DispatchStats()

    clock.advance(timedelta(seconds=2.5))
    assert await dispatcher.run_once() == DispatchStats(retried=1)
    row = await _only_row(db)
    assert (row["attempts"], row["available_at"]) == (2, clock.now() + timedelta(seconds=5))

    clock.advance(timedelta(seconds=5))
    assert await dispatcher.run_once() == DispatchStats(sent=1)
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["last_error"]) == ("sent", 3, None)
    assert len(fake.posted) == 1


async def test_network_errors_are_retried(db: AsyncEngine, clock: FixedClock) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with discord_client(TOKEN, transport=httpx.MockTransport(refuse)) as failing:
        assert await Dispatcher(db, failing, clock=clock, rand=lambda: 1.0).run_once() == DispatchStats(retried=1)
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["available_at"], row["last_error"]) == ("pending", 1, clock.now() + timedelta(seconds=5), "request failed: ConnectTimeout")


async def test_attempt_limit_fails_the_row(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, clock: FixedClock) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    fake.script("GET", f"channels/{CHANNEL}", *[httpx.Response(500, json={"code": 0, "message": "500: Internal Server Error"}) for _ in range(MAX_DELIVERY_ATTEMPTS)])

    for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
        assert await dispatcher.run_once() == DispatchStats(retried=1)
        clock.advance(timedelta(seconds=RETRY_MAX_SECONDS))
    assert await dispatcher.run_once() == DispatchStats(failed=1)

    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["completed_at"]) == ("failed", MAX_DELIVERY_ATTEMPTS, clock.now())
    assert row["result"] == {"status": "failed", "reason": "retries_exhausted", "http_status": "500", "code": "0", "message": "500: Internal Server Error"}
    assert row["last_error"] == "Discord answered status 500"
    assert fake.posted == []


async def test_attempt_limit_after_network_errors_has_no_discord_answer(db: AsyncEngine, clock: FixedClock) -> None:
    await setup_contracts(db)
    executed = await call(db, "shout", ["hi"])

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with discord_client(TOKEN, transport=httpx.MockTransport(refuse)) as failing:
        dispatcher = Dispatcher(db, failing, clock=clock, rand=lambda: 0.0)
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            await dispatcher.run_once()
            clock.advance(timedelta(seconds=RETRY_MAX_SECONDS))

    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"], row["last_error"]) == ("failed", MAX_DELIVERY_ATTEMPTS, {"status": "failed", "reason": "retries_exhausted"}, "request failed: ConnectError")
    detail = await queries.get_execution(db, executed.execution_id)
    assert detail is not None
    assert detail.deliveries[0].error == queries.DeliveryError(reason="retries_exhausted", http_status=None, code=None, message=None)


async def test_crash_before_any_request_fails_without_an_attempt(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])

    async def crash(*args: object) -> bool:
        raise KeyError("boom")

    monkeypatch.setattr(dispatcher_module, "is_discord_allowed", crash)

    assert await dispatcher.run_once() == DispatchStats(failed=1)

    assert fake.requests == []
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"], row["last_error"]) == ("failed", 0, {"status": "failed", "reason": "internal_error"}, "internal error: KeyError")


async def test_crash_after_a_request_is_retried_as_an_attempt(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, monkeypatch: pytest.MonkeyPatch, clock: FixedClock) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])

    def crash(value: object) -> int | None:
        raise KeyError("boom")

    monkeypatch.setattr(dispatcher_module, "_snowflake", crash)

    assert await dispatcher.run_once() == DispatchStats(retried=1)

    assert fake.paths() == [("GET", f"channels/{CHANNEL}")]
    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["available_at"], row["last_error"]) == ("pending", 1, clock.now() + timedelta(seconds=2.5), "internal error: KeyError")


@pytest.mark.parametrize(
    ("method", "path", "response", "result"),
    [
        (
            "GET",
            f"channels/{CHANNEL}",
            httpx.Response(403, json={"code": 50001, "message": "Missing Access"}),
            {"status": "failed", "reason": "channel_unavailable", "http_status": "403", "code": "50001", "message": "Missing Access"},
        ),
        (
            "GET",
            f"channels/{CHANNEL}",
            httpx.Response(404, json={"code": 10003, "message": "Unknown Channel"}),
            {"status": "failed", "reason": "channel_unavailable", "http_status": "404", "code": "10003", "message": "Unknown Channel"},
        ),
        (
            "POST",
            f"channels/{CHANNEL}/messages",
            httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"}),
            {"status": "failed", "reason": "message_rejected", "http_status": "403", "code": "50013", "message": "Missing Permissions"},
        ),
        ("POST", f"channels/{CHANNEL}/messages", httpx.Response(400, text="not json"), {"status": "failed", "reason": "message_rejected", "http_status": "400"}),
    ],
)
async def test_client_errors_fail_at_once(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher, method: str, path: str, response: httpx.Response, result: dict[str, str]) -> None:
    await setup_contracts(db)
    executed = await call(db, "shout", ["hi"])
    fake.script(method, path, response)

    assert await dispatcher.run_once() == DispatchStats(failed=1)

    row = await _only_row(db)
    assert (row["status"], row["attempts"], row["result"]) == ("failed", 1, result)
    detail = await queries.get_execution(db, executed.execution_id)
    assert detail is not None
    error = detail.deliveries[0].error
    assert error is not None and (error.reason, error.http_status, error.code) == (result["reason"], int(result["http_status"]), result.get("code"))


async def test_malformed_rows_fail_and_the_loop_continues(db: AsyncEngine, fake: FakeDiscord, dispatcher: Dispatcher) -> None:
    async with db.begin() as conn:
        await enqueue(conn, kind=OutboxKind.DISCORD_SEND, payload={"guild_id": 1}, now=NOW)
        await enqueue(conn, kind="mystery", payload={}, now=NOW)
        await enqueue(conn, kind=OutboxKind.CLAIM_CREATED, payload={"claim_id": "x"}, now=NOW)
        await enqueue(conn, kind=OutboxKind.DISCORD_ADD_ROLE, payload={"contract_id": "1", "guild_id": "0", "user_id": "2", "role_id": "3"}, now=NOW)

    assert await dispatcher.run_once() == DispatchStats(failed=4)

    assert fake.requests == []
    assert [row["result"] for row in await outbox_rows(db)] == [
        {"status": "failed", "reason": "malformed_payload"},
        {"status": "failed", "reason": "unknown_kind"},
        {"status": "failed", "reason": "malformed_payload"},
        {"status": "failed", "reason": "malformed_payload"},
    ]


async def test_concurrent_dispatchers_send_each_message_once(db: AsyncEngine, fake: FakeDiscord, http: httpx.AsyncClient, clock: FixedClock) -> None:
    await setup_contracts(db)
    for index in range(12):
        await call(db, "shout", [f"message {index}"])
    fake.delay = 0.02

    first, second = await asyncio.gather(Dispatcher(db, http, clock=clock).run_once(), Dispatcher(db, http, clock=clock).run_once())

    assert first.sent + second.sent == 12
    assert first.sent > 0 and second.sent > 0
    posts = [request for request in fake.requests if request.method == "POST"]
    assert len(posts) == 12
    assert sorted(item.body["content"] for item in fake.posted) == sorted(f"message {index}" for index in range(12))
    assert {row["status"] for row in await outbox_rows(db)} == {"sent"}


async def test_rows_not_yet_due_are_left_alone(db: AsyncEngine, fake: FakeDiscord, http: httpx.AsyncClient) -> None:
    await setup_contracts(db)
    await call(db, "shout", ["hi"])
    early = Dispatcher(db, http, clock=FixedClock(NOW - timedelta(seconds=1)))
    assert await early.run_once() == DispatchStats()
    assert fake.requests == []
    assert await early.run_once(should_stop=lambda: True) == DispatchStats()
