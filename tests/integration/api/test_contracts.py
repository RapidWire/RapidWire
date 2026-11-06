"""Contract endpoints over HTTP: registration and the public reads of versions, binaries, functions and storage, calls with typed values, dry runs, and lock, pause, resume and delete."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.deps import ApiState, set_state
from rapidwire.api.routers.contracts import REGISTER_BODY_SLACK, SHA256_HEADER, register_body_limit
from rapidwire.config import Settings
from rapidwire.contract.compiler import compile_source
from rapidwire.core.contracts import LOCK_HOURS_MAX
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.db.tables import execution, transfer
from tests.integration.api.conftest import AuthHeaders
from tests.integration.factories import balance_of, create_currency, mint

OWNER = 7101
CALLER = 7102
OTHER = 7103
GUILD = 610_001
GUILD_OWNER = 610_002
ROLE = 610_003
PRELUDE = "from rapidwire_contract import *\n"

SOURCE = """
@public
def bump(step: int) -> int:
    n: int = storage.get("n", 0) + step
    storage["n"] = n
    return n

@public
def echo(a: int, b: str, c: bool, d: Account, e: Currency) -> str:
    return str(a) + b + str(int(c)) + str(int(d)) + str(int(e))

@public
def flag() -> bool:
    return True

@public
def fill(who: Account) -> int:
    storage["list"] = [1, -2, 3]
    m: dict[str, Account] = {"bb": who, "a": Account(5)}
    storage["dict"] = m
    storage["a/b"] = "slash"
    return 3

@public
def buy(price: int) -> int:
    if paid.amount < price:
        revert("Send at least the price")
    return paid.amount

@public
def fail() -> int:
    storage["x"] = 1
    revert("nope")

@public
def roll() -> int:
    return random(1, 6)

@public
def check(user: Account) -> bool:
    return discord.has_role(610001, user, 610003)
"""


def _binary(source: str = SOURCE) -> bytes:
    return compile_source(PRELUDE + source)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def _count(engine: AsyncEngine, table: Any, *where: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one())


async def _execution(engine: AsyncEngine, execution_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(execution).where(execution.c.execution_id == int(execution_id)))).mappings().one())


@pytest.fixture
async def gas(db: AsyncEngine, api_settings: Settings) -> int:
    currency_id = api_settings.gas_currency_id
    await create_currency(db, currency_id=currency_id, symbol="RAPID", name="Rapid")
    for account_id in (CALLER, OTHER):
        await mint(db, currency_id, account_id, 1_000_000)
    return currency_id


@pytest.fixture
async def deployed(client: httpx.AsyncClient, gas: int, auth_headers: AuthHeaders) -> dict[str, Any]:
    response = await client.post(f"/v1/contracts/{OWNER}/versions", json={"binary": _b64(_binary())}, headers=await auth_headers(OWNER))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _call(client: httpx.AsyncClient, headers: dict[str, str], function: str, args: list[dict[str, Any]] | None = None, **extra: Any) -> httpx.Response:
    return await client.post(f"/v1/contracts/{OWNER}/call", json={"function": function, "args": args or [], **extra}, headers=headers)


def _error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    return error


async def test_register_and_read_versions(client: httpx.AsyncClient, gas: int, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    headers = await auth_headers(OWNER)
    first = _binary()
    response = await client.post(f"/v1/contracts/{OWNER}/versions", json={"binary": _b64(first)}, headers=headers)
    assert response.status_code == 200, response.text
    registered = response.json()
    assert registered | {"execution_id": None} == {
        "execution_id": None,
        "account_id": str(OWNER),
        "version": 1,
        "sha256": hashlib.sha256(first).hexdigest(),
        "size": len(first),
        "status": "active",
        "revived": False,
    }
    second = _binary(SOURCE + "\n@public\ndef extra() -> int:\n    return 1\n")
    assert (await client.post(f"/v1/contracts/{OWNER}/versions", json={"binary": _b64(second)}, headers=headers)).json()["version"] == 2

    info = (await client.get(f"/v1/contracts/{OWNER}")).json()
    assert (info["status"], info["current_version"], info["version_count"], info["sha256"], info["size"], info["locked"], info["locked_until"]) == (
        "active",
        2,
        2,
        hashlib.sha256(second).hexdigest(),
        len(second),
        False,
        None,
    )
    assert info["created_execution_id"] == registered["execution_id"]
    names = [fn["name"] for fn in info["functions"]]
    assert names == sorted(names) and "extra" in names
    echo = next(fn for fn in info["functions"] if fn["name"] == "echo")
    assert echo == {"name": "echo", "params": ["int", "str", "bool", "Account", "Currency"], "returns": "str", "signature": "echo(int, str, bool, Account, Currency) -> str"}

    functions = (await client.get(f"/v1/contracts/{OWNER}/functions")).json()
    assert (functions["account_id"], functions["version"], functions["status"]) == (str(OWNER), 2, "active")
    assert functions["functions"] == info["functions"]

    versions = (await client.get(f"/v1/contracts/{OWNER}/versions")).json()
    assert [(v["version"], v["sha256"], v["size"]) for v in versions["items"]] == [(2, hashlib.sha256(second).hexdigest(), len(second)), (1, hashlib.sha256(first).hexdigest(), len(first))]
    assert (versions["total"], versions["per_page"]) == (2, 15)
    assert versions["items"][1]["created_execution_id"] == registered["execution_id"]

    detail = (await client.get(f"/v1/contracts/{OWNER}/versions/1")).json()
    assert base64.b64decode(detail["binary"], validate=True) == first
    assert (detail["account_id"], detail["version"], detail["sha256"]) == (str(OWNER), 1, hashlib.sha256(first).hexdigest())

    download = await client.get(f"/v1/contracts/{OWNER}/versions/1/binary")
    assert download.status_code == 200
    assert download.content == first
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers[SHA256_HEADER] == hashlib.sha256(first).hexdigest()
    assert download.headers["repr-digest"] == f"sha-256=:{base64.b64encode(hashlib.sha256(first).digest()).decode()}:"
    assert download.headers["content-disposition"] == f'attachment; filename="contract-{OWNER}-v1.rwc"'

    _error(await client.get(f"/v1/contracts/{OWNER}/versions/3"), 404, "not_found")
    _error(await client.get(f"/v1/contracts/{OWNER}/versions/3/binary"), 404, "not_found")
    _error(await client.get(f"/v1/contracts/{OTHER}"), 404, "contract_not_found")
    _error(await client.get(f"/v1/contracts/{OTHER}/functions"), 404, "contract_not_found")
    _error(await client.get("/v1/contracts/007"), 422, "validation_error")
    assert (await client.get(f"/v1/contracts/{OTHER}/versions")).json()["items"] == []


async def test_registration_replays_under_one_key(client: httpx.AsyncClient, gas: int, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    headers = {**await auth_headers(OWNER), "Idempotency-Key": "register-1"}
    url = f"/v1/contracts/{OWNER}/versions"
    payload = {"binary": _b64(_binary())}
    first = await client.post(url, json=payload, headers=headers)
    again = await client.post(url, json=payload, headers=headers)
    assert first.status_code == again.status_code == 200, again.text
    assert "idempotency-replayed" not in first.headers
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, execution, execution.c.type == "contract_update") == 1
    assert (await client.get(f"/v1/contracts/{OWNER}")).json()["current_version"] == 1
    other = {"binary": _b64(_binary(SOURCE + "\n@public\ndef extra() -> int:\n    return 1\n"))}
    _error(await client.post(url, json=other, headers=headers), 422, "idempotency_conflict")
    assert await _count(db, execution, execution.c.type == "contract_update") == 1


async def test_registration_errors_record_nothing(client: httpx.AsyncClient, gas: int, auth_headers: AuthHeaders, db: AsyncEngine, api_settings: Settings) -> None:
    headers = await auth_headers(OWNER)
    url = f"/v1/contracts/{OWNER}/versions"
    invalid = _error(await client.post(url, json={"binary": _b64(b"RWC1 not really")}, headers=headers), 422, "invalid_contract")
    assert "execution_id" not in invalid
    max_size = api_settings.contract_max_size
    _error(await client.post(url, json={"binary": _b64(_binary() + b"\x00" * max_size)}, headers=headers), 422, "invalid_contract")
    _error(await client.post(url, json={"binary": _b64(b"\x00" * (max_size + 1))}, headers=headers), 422, "invalid_contract")
    limit = register_body_limit(max_size)
    largest = _b64(b"\x00" * max_size)
    fits = await client.post(url, content=b'{"binary": "' + largest.encode() + b'"}' + b" " * (limit - len(largest) - 14), headers={**headers, "Content-Type": "application/json"})
    _error(fits, 422, "invalid_contract")
    # Every base64 character of b"\xff" bytes is "/", which JSON may write as the escape "\/".
    escaped = _b64(b"\xff" * max_size).replace("/", "\\/").encode()
    escaped_body = b'{"binary": "' + escaped + b'"}'
    assert len(escaped_body) <= limit
    assert len(escaped_body) > len(largest) + REGISTER_BODY_SLACK
    _error(await client.post(url, content=escaped_body, headers={**headers, "Content-Type": "application/json"}), 422, "invalid_contract")
    too_large = await client.post(url, content=b'{"binary": "' + b"A" * (limit - 13) + b'"}', headers={**headers, "Content-Type": "application/json"})
    _error(too_large, 413, "payload_too_large")
    assert too_large.headers["connection"] == "close"

    async def streamed() -> AsyncIterator[bytes]:
        yield b'{"binary": "'
        for _ in range(limit // 1024 + 1):
            yield b"A" * 1024
        yield b'"}'

    _error(await client.post(url, content=streamed(), headers={**headers, "Content-Type": "application/json"}), 413, "payload_too_large")
    bad = _error(await client.post(url, json={"binary": "not base64!"}, headers=headers), 422, "validation_error")
    assert bad["details"][0]["loc"] == ["body", "binary"]
    _error(await client.post(url, json={"binary": 5}, headers=headers), 422, "validation_error")
    _error(await client.post(url, json={"binary": _b64(_binary()), "extra": 1}, headers=headers), 422, "validation_error")
    for text in ("QUI", "QUI=\n", "QU-_"):
        _error(await client.post(url, json={"binary": text}, headers=headers), 422, "validation_error")
    missing = _error(await client.post(url, json={}, headers=headers), 422, "validation_error")
    assert missing["details"][0]["loc"] == ["body", "binary"]
    _error(await client.post(url, content=b"{", headers={**headers, "Content-Type": "application/json"}), 422, "validation_error")
    _error(await client.post(f"/v1/contracts/{CALLER}/versions", json={"binary": _b64(_binary())}, headers=headers), 403, "permission_denied")
    _error(await client.post(url, json={"binary": _b64(_binary())}), 401, "unauthorized")
    assert await _count(db, execution, execution.c.type == "contract_update") == 0


async def test_storage_reads(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders) -> None:
    filled = await _call(client, await auth_headers(CALLER), "fill", [{"type": "Account", "value": str(CALLER)}])
    assert filled.status_code == 200, filled.text
    page = (await client.get(f"/v1/contracts/{OWNER}/storage")).json()
    entries = {item["key"]: item for item in page["items"]}
    assert [item["key"] for item in page["items"]] == ["a/b", "dict", "list"]
    assert entries["list"]["value"] == {"type": "list", "value": [{"type": "int", "value": "1"}, {"type": "int", "value": "-2"}, {"type": "int", "value": "3"}]}
    # Storage dicts read back in canonical key order: the shorter str key first.
    assert entries["dict"]["value"] == {
        "type": "dict",
        "value": [{"key": {"type": "str", "value": "a"}, "value": {"type": "Account", "value": "5"}}, {"key": {"type": "str", "value": "bb"}, "value": {"type": "Account", "value": str(CALLER)}}],
    }
    assert entries["a/b"]["value"] == {"type": "str", "value": "slash"}
    assert all(isinstance(item["size"], int) and item["updated_at"].endswith("Z") for item in page["items"])
    one = (await client.get(f"/v1/contracts/{OWNER}/storage/dict")).json()
    assert one == {"account_id": str(OWNER), "key": "dict", "value": entries["dict"]["value"], "size": entries["dict"]["size"], "updated_at": entries["dict"]["updated_at"]}
    slashed = (await client.get(f"/v1/contracts/{OWNER}/storage/a/b")).json()
    assert slashed["value"] == {"type": "str", "value": "slash"}
    _error(await client.get(f"/v1/contracts/{OWNER}/storage/absent"), 404, "not_found")
    _error(await client.get(f"/v1/contracts/{OTHER}/storage/n"), 404, "contract_not_found")
    assert (await client.get(f"/v1/contracts/{OTHER}/storage")).json()["items"] == []


async def test_call_with_typed_arguments(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, gas: int) -> None:
    headers = await auth_headers(CALLER)
    args: list[dict[str, Any]] = [
        {"type": "int", "value": "12"},
        {"type": "str", "value": "x"},
        {"type": "bool", "value": True},
        {"type": "Account", "value": "34"},
        {"type": "Currency", "value": "56"},
    ]
    response = await _call(client, headers, "echo", args)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == {"type": "str", "value": "12x13456"}
    assert (body["contract_id"], body["function"], body["gas_limit"], body["gas_currency_id"], body["gas_price"], body["random"]) == (str(OWNER), "echo", "128", str(gas), "1", [])
    assert body["gas_cost"] == f"0.{int(body['gas_used']):03d}"
    row = await _execution(db, body["execution_id"])
    assert row["input"]["args"] == args
    assert row["initiator_id"] == CALLER
    assert (await _call(client, headers, "flag")).json()["value"] == {"type": "bool", "value": True}
    bumped = await _call(client, headers, "bump", [{"type": "int", "value": "5"}])
    assert bumped.json()["value"] == {"type": "int", "value": "5"}
    rolled = (await _call(client, headers, "roll")).json()
    assert rolled["random"] == [rolled["value"]["value"]]


async def test_call_with_attachment_and_gas_limit(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, gas: int) -> None:
    before = await balance_of(db, CALLER, gas)
    response = await _call(client, await auth_headers(CALLER), "buy", [{"type": "int", "value": "400"}], attach={"currency_id": str(gas), "amount": "0.5"}, gas_limit="1000")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["value"], body["gas_limit"]) == ({"type": "int", "value": "500"}, "1000")
    assert await balance_of(db, OWNER, gas) == 500
    assert await balance_of(db, CALLER, gas) == before - 500 - int(body["gas_used"])
    row = await _execution(db, body["execution_id"])
    assert row["input"]["attach"] == {"currency_id": str(gas), "amount": "500"}


async def test_revert_is_recorded_with_its_gas(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, gas: int) -> None:
    before = await balance_of(db, CALLER, gas)
    error = _error(await _call(client, await auth_headers(CALLER), "fail"), 422, "contract_reverted")
    assert error["kind"] == "REVERTED"
    assert error["message"] == "nope"
    row = await _execution(db, error["execution_id"])
    assert row["status"] == "reverted"
    used = int(row["output"]["gas_used"])
    assert used > 0
    assert await balance_of(db, CALLER, gas) == before - used
    assert await _count(db, transfer, transfer.c.execution_id == int(error["execution_id"])) == 1
    assert (await client.get(f"/v1/contracts/{OWNER}/storage/x")).status_code == 404


async def test_call_argument_errors(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, api_settings: Settings) -> None:
    headers = await auth_headers(CALLER)
    calls_before = await _count(db, execution, execution.c.type == "contract_call")
    _error(await _call(client, headers, "bump", [{"type": "int", "value": "1"}], gas_limit=str(api_settings.max_gas + 1)), 422, "invalid_argument")
    _error(await _call(client, headers, "bump", [{"type": "int", "value": "1"}], gas_limit=1000), 422, "validation_error")
    misspelled = _error(await _call(client, headers, "bump", [{"type": "int", "value": "1"}], gas_limt="1000"), 422, "validation_error")
    assert (misspelled["details"][0]["loc"], misspelled["details"][0]["type"]) == (["body", "<extra>"], "extra_forbidden")
    malformed: list[dict[str, Any]] = [
        {"type": "int", "value": 5},
        {"type": "int", "value": "05"},
        {"type": "float", "value": "1"},
        {"type": "bool", "value": "true"},
        {"type": "int"},
        {"type": "Account", "value": str(2**63)},
    ]
    for bad in malformed:
        error = _error(await _call(client, headers, "bump", [bad]), 422, "validation_error")
        assert error["details"][0]["loc"] == ["body", "args", 0]
        assert error["details"][0]["type"] == "typed_value"
    assert await _count(db, execution, execution.c.type == "contract_call") == calls_before
    mismatch = _error(await _call(client, headers, "bump", [{"type": "str", "value": "1"}]), 422, "contract_call_failed")
    assert (mismatch["kind"], mismatch["reason"]) == ("CALL_FAILED", "BAD_ARGUMENTS")
    assert (await _execution(db, mismatch["execution_id"]))["status"] == "failed"
    unknown = _error(await _call(client, headers, "nothing"), 422, "contract_call_failed")
    assert unknown["reason"] == "NO_FUNCTION"
    container = _error(await _call(client, headers, "bump", [{"type": "list", "value": []}]), 422, "invalid_argument")
    assert "execution_id" not in container
    missing = _error(await client.post(f"/v1/contracts/{OTHER}/call", json={"function": "bump"}, headers=headers), 404, "contract_not_found")
    assert missing["reason"] == "NO_CONTRACT"


async def test_dry_run_changes_nothing(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, gas: int) -> None:
    url = f"/v1/contracts/{OWNER}/dry-run"
    payload = {"function": "buy", "args": [{"type": "int", "value": "100"}], "attach": {"currency_id": str(gas), "amount": "0.2"}, "gas_limit": "500"}
    anonymous = _error(await client.post(url, json=payload), 401, "unauthorized")
    assert "execution_id" not in anonymous
    headers = await auth_headers(CALLER)
    # Issuing the token is itself an execution.
    executions_before = await _count(db, execution)
    balance_before = await balance_of(db, CALLER, gas)
    response = await client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ok"], body["value"], body["error"], body["gas_limit"], body["gas_currency_id"], body["gas_price"]) == (True, {"type": "int", "value": "200"}, None, "500", str(gas), "1")
    assert body["gas_cost"] == f"0.{int(body['gas_used']):03d}"
    kinds = sorted(item["kind"] for item in body["transfers"])
    assert kinds == ["gas", "principal"]
    principal = next(item for item in body["transfers"] if item["kind"] == "principal")
    assert (principal["from_account"], principal["to_account"], principal["amount"]) == (str(CALLER), str(OWNER), "0.200")
    assert await _count(db, execution) == executions_before
    assert await balance_of(db, CALLER, gas) == balance_before
    reverted = (await client.post(url, json={"function": "fail"}, headers=headers)).json()
    assert reverted["ok"] is False and reverted["value"] is None
    assert (reverted["error"]["code"], reverted["error"]["kind"], reverted["error"]["message"]) == ("contract_reverted", "REVERTED", "nope")
    assert int(reverted["gas_used"]) > 0
    stored = (await client.post(url, json={"function": "bump", "args": [{"type": "int", "value": "9"}]}, headers=headers)).json()
    assert stored["value"] == {"type": "int", "value": "9"}
    assert await _count(db, execution) == executions_before
    assert await balance_of(db, CALLER, gas) == balance_before
    assert await balance_of(db, OWNER, gas) == 0
    assert (await client.get(f"/v1/contracts/{OWNER}/storage")).json()["items"] == []

    for caller in (str(OTHER), str(CALLER)):
        named = _error(await client.post(url, json={"function": "flag", "caller_id": caller}, headers=headers), 422, "validation_error")
        assert (named["details"][0]["loc"], named["details"][0]["type"]) == (["body", "<extra>"], "extra_forbidden")
    keyed = _error(await client.post(url, json={"function": "flag"}, headers={**headers, "Idempotency-Key": "k"}), 422, "validation_error")
    assert keyed["details"][0]["loc"] == ["header", "Idempotency-Key"]
    absent = (await client.post(f"/v1/contracts/{OTHER}/dry-run", json={"function": "flag"}, headers=headers)).json()
    assert (absent["ok"], absent["error"]["code"], absent["error"]["reason"]) == (False, "contract_not_found", "NO_CONTRACT")
    assert await _count(db, execution) == executions_before


async def test_lock_pause_resume_delete(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    owner = await auth_headers(OWNER)
    caller = await auth_headers(CALLER)
    base = f"/v1/contracts/{OWNER}"
    assert (await _call(client, caller, "bump", [{"type": "int", "value": "1"}])).status_code == 200
    _error(await client.post(f"{base}/pause", headers=caller), 403, "permission_denied")
    paused = await client.post(f"{base}/pause", headers=owner)
    assert paused.status_code == 200
    assert paused.json() | {"execution_id": None} == {"execution_id": None, "account_id": str(OWNER), "status": "paused"}
    blocked = _error(await _call(client, caller, "flag"), 409, "contract_paused")
    assert blocked["reason"] == "INACTIVE"
    _error(await client.post(f"{base}/pause", headers=owner), 409, "invalid_state")
    _error(await client.post(f"{base}/lock", json={"hours": 1}, headers=owner), 409, "invalid_state")
    assert (await client.post(f"{base}/resume", headers=owner)).json()["status"] == "active"

    _error(await client.post(f"{base}/lock", json={"hours": 1, "until": "2030-01-01T00:00:00Z"}, headers=owner), 422, "validation_error")
    _error(await client.post(f"{base}/lock", json={}, headers=owner), 422, "validation_error")
    _error(await client.post(f"{base}/lock", json={"until": "2030-01-01T00:00:00"}, headers=owner), 422, "validation_error")
    _error(await client.post(f"{base}/lock", json={"hours": 0}, headers=owner), 422, "validation_error")
    locked = await client.post(f"{base}/lock", json={"hours": 2}, headers=owner)
    assert locked.status_code == 200, locked.text
    until = datetime.fromisoformat(locked.json()["locked_until"])
    assert timedelta(hours=1, minutes=59) < until - datetime.now(UTC) <= timedelta(hours=2)
    assert locked.json()["previous_locked_until"] is None
    extended = (await client.post(f"{base}/lock", json={"until": (until + timedelta(days=1)).isoformat()}, headers=owner)).json()
    assert datetime.fromisoformat(extended["previous_locked_until"]) == until
    _error(await client.post(f"{base}/lock", json={"hours": 1}, headers=owner), 409, "invalid_state")
    info = (await client.get(base)).json()
    assert info["locked"] is True
    assert datetime.fromisoformat(info["locked_until"]) == until + timedelta(days=1)
    _error(await client.post(f"{base}/pause", headers=owner), 409, "contract_locked")
    _error(await client.post(f"{base}/delete", headers=owner), 409, "contract_locked")
    _error(await client.post(f"{base}/versions", json={"binary": _b64(_binary())}, headers=owner), 409, "contract_locked")

    other = await auth_headers(OTHER)
    other_base = f"/v1/contracts/{OTHER}"
    assert (await client.post(f"{other_base}/versions", json={"binary": _b64(_binary())}, headers=other)).status_code == 200
    assert (await client.post(f"{other_base}/call", json={"function": "bump", "args": [{"type": "int", "value": "4"}]}, headers=caller)).status_code == 200
    deleted = await client.post(f"{other_base}/delete", headers=other)
    assert deleted.status_code == 200
    assert deleted.json() | {"execution_id": None, "storage_bytes_cleared": None} == {
        "execution_id": None,
        "account_id": str(OTHER),
        "version": 1,
        "storage_keys_cleared": 1,
        "storage_bytes_cleared": None,
    }
    gone = (await client.get(other_base)).json()
    assert (gone["status"], gone["functions"], gone["storage_keys"]) == ("deleted", [], 0)
    assert gone["deleted_at"] is not None
    assert (await client.get(f"{other_base}/versions/1/binary")).status_code == 200
    _error(await client.post(f"{other_base}/delete", headers=other), 404, "contract_not_found")
    revived = (await client.post(f"{other_base}/versions", json={"binary": _b64(_binary())}, headers=other)).json()
    assert (revived["version"], revived["revived"], revived["status"]) == (2, True, "active")
    for path in ("lock", "pause", "resume", "delete"):
        _error(await client.post(f"{base}/{path}", json={"hours": 1}), 401, "unauthorized")


@pytest.mark.parametrize(
    "body",
    [
        {"hours": True},
        {"hours": "2"},
        {"hours": 2.0},
        {"hours": 2.5},
        {"hours": LOCK_HOURS_MAX + 1},
        {"hours": None},
        {"until": 1893456000},
        {"until": 1893456000.5},
        {"until": True},
        {"until": "2030-01-01T00:00:00"},
        {"until": "not a time"},
        {"until": "9999-12-31T23:59:59Z"},
        {"until": "9999-12-31T23:59:59-23:59"},
    ],
)
async def test_lock_body_is_strict(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, body: dict[str, Any]) -> None:
    error = _error(await client.post(f"/v1/contracts/{OWNER}/lock", json=body, headers=await auth_headers(OWNER)), 422, "validation_error")
    assert error["details"][0]["loc"][:2] == ["body", next(iter(body))] or error["details"][0]["loc"] == ["body"]
    assert await _count(db, execution, execution.c.type == "contract_lock") == 0


async def test_lock_until_is_capped_at_the_longest_duration(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(OWNER)
    url = f"/v1/contracts/{OWNER}/lock"
    beyond = datetime.now(UTC) + timedelta(hours=LOCK_HOURS_MAX, minutes=5)
    error = _error(await client.post(url, json={"until": beyond.isoformat()}, headers=headers), 422, "validation_error")
    assert error["details"][0]["loc"] == ["body", "until"]
    within = datetime.now(UTC) + timedelta(hours=LOCK_HOURS_MAX - 1)
    locked = await client.post(url, json={"until": within.astimezone(timezone(timedelta(hours=9))).isoformat()}, headers=headers)
    assert locked.status_code == 200, locked.text
    assert datetime.fromisoformat(locked.json()["locked_until"]) == within


async def test_lock_for_hours_replays_under_one_key(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    headers = {**await auth_headers(OWNER), "Idempotency-Key": "lock-2h"}
    url = f"/v1/contracts/{OWNER}/lock"
    first = await client.post(url, json={"hours": 2}, headers=headers)
    assert first.status_code == 200, first.text
    again = await client.post(url, json={"hours": 2}, headers=headers)
    assert again.status_code == 200, again.text
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, execution, execution.c.type == "contract_lock") == 1
    row = await _execution(db, first.json()["execution_id"])
    assert row["input"] == {"account_id": str(OWNER), "hours": "2"}
    assert datetime.fromisoformat(first.json()["locked_until"]) == row["created_at"] + timedelta(hours=2)
    _error(await client.post(url, json={"hours": 3}, headers=headers), 422, "idempotency_conflict")


async def test_call_gas_fields_come_from_the_execution(api_app: FastAPI, client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(CALLER), "Idempotency-Key": "gas-fields"}
    first = await _call(client, headers, "bump", [{"type": "int", "value": "1"}])
    assert first.status_code == 200, first.text
    state: ApiState = api_app.state.rapidwire
    # A changed price applies to new calls, while the replay reports what the recorded call was charged.
    set_state(api_app, dataclasses.replace(state, runtime_config=dataclasses.replace(state.runtime_config, gas_price=7)))
    try:
        again = await _call(client, headers, "bump", [{"type": "int", "value": "1"}])
    finally:
        set_state(api_app, state)
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert (first.json()["gas_price"], first.json()["gas_cost"]) == ("1", f"0.{int(first.json()['gas_used']):03d}")


async def test_call_replays_under_one_key(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    headers = {**await auth_headers(CALLER), "Idempotency-Key": "call-1"}
    calls_before = await _count(db, execution, execution.c.type == "contract_call")
    first = await _call(client, headers, "bump", [{"type": "int", "value": "3"}])
    again = await _call(client, headers, "bump", [{"type": "int", "value": "3"}])
    assert first.status_code == again.status_code == 200
    assert "idempotency-replayed" not in first.headers
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, execution, execution.c.type == "contract_call") == calls_before + 1
    assert (await client.get(f"/v1/contracts/{OWNER}/storage/n")).json()["value"] == {"type": "int", "value": "3"}
    _error(await _call(client, headers, "bump", [{"type": "int", "value": "4"}]), 422, "idempotency_conflict")
    failed = await _call(client, {**headers, "Idempotency-Key": "call-2"}, "fail")
    replayed = await _call(client, {**headers, "Idempotency-Key": "call-2"}, "fail")
    assert replayed.status_code == failed.status_code == 422
    assert replayed.json() == failed.json()
    assert replayed.headers["idempotency-replayed"] == "true"


async def test_has_role_uses_the_guild_info(client: httpx.AsyncClient, deployed: dict[str, Any], auth_headers: AuthHeaders, db: AsyncEngine, guild_info: InMemoryGuildInfo) -> None:
    guild_info.add_guild(GUILD, GUILD_OWNER)
    guild_info.add_role(GUILD, CALLER, ROLE)
    await allow_discord(db, guild_info, guild_id=GUILD, account_id=OWNER, acting_user_id=GUILD_OWNER)
    headers = await auth_headers(CALLER)
    member = await _call(client, headers, "check", [{"type": "Account", "value": str(CALLER)}], gas_limit="1000")
    assert member.json()["value"] == {"type": "bool", "value": True}
    stranger = await _call(client, headers, "check", [{"type": "Account", "value": str(OTHER)}], gas_limit="1000")
    assert stranger.json()["value"] == {"type": "bool", "value": False}


async def test_writes_need_a_token(client: httpx.AsyncClient, deployed: dict[str, Any]) -> None:
    _error(await _call(client, {}, "flag"), 401, "unauthorized")
    _error(await _call(client, {"Authorization": "Bearer nope"}, "flag"), 401, "unauthorized")
    for headers in ({}, {"Authorization": "Bearer nope"}):
        _error(await client.post(f"/v1/contracts/{OWNER}/dry-run", json={"function": "flag"}, headers=headers), 401, "unauthorized")
