from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.allowances import get_allowance
from rapidwire.core.tokens import generate_token, issue_token, revoke_tokens
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX, account, execution, stake, transfer
from tests.integration.api.conftest import AuthHeaders, bearer
from tests.integration.factories import balance_of, create_currency, mint

ALICE = 7101
BOB = 7102
CAROL = 7103
CUR = 5151
MISSING_CUR = 5999

# Every write endpoint of the router with a valid body, for the checks that apply to all of them.
WRITES: list[tuple[str, dict[str, Any] | None]] = [
    ("/v1/transfers", {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    ("/v1/transfers/from", {"source_id": str(BOB), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "1"}),
    ("/v1/allowances", {"spender_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    ("/v1/claims", {"payer_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    ("/v1/claims/1/pay", None),
    ("/v1/claims/1/cancel", None),
    ("/v1/stakes/deposit", {"currency_id": str(CUR), "amount": "1"}),
    ("/v1/stakes/withdraw", {"currency_id": str(CUR), "amount": "1"}),
    ("/v1/tokens/revoke", None),
    ("/v1/currencies", {"guild_id": "6001", "name": "Gold", "symbol": "GLD", "supply": "1"}),
    (f"/v1/currencies/{CUR}/mint", {"amount": "1"}),
    (f"/v1/currencies/{CUR}/burn", {"amount": "1"}),
    (f"/v1/currencies/{CUR}/renounce", None),
    (f"/v1/currencies/{CUR}/rate-change", {"hourly_rate_ppm": "10"}),
    (f"/v1/currencies/{CUR}/rate-change/apply", None),
    (f"/v1/currencies/{CUR}/delete", None),
    (f"/v1/currencies/{CUR}/delete/finalize", None),
    (f"/v1/currencies/{CUR}/delegation/enable", None),
    (f"/v1/currencies/{CUR}/delegation/disable", None),
    ("/v1/notifications/allow", {"user_id": str(BOB)}),
    ("/v1/notifications/deny", {"user_id": str(BOB)}),
    (f"/v1/guilds/{CUR}/discord-permissions/allow", {"account_id": str(BOB)}),
    (f"/v1/guilds/{CUR}/discord-permissions/deny", {"account_id": str(BOB)}),
]


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="LWT")
    await mint(db, CUR, ALICE, 10_000)
    return db


async def count_executions(engine: AsyncEngine, type: str | None = None) -> int:
    stmt = select(func.count()).select_from(execution)
    if type is not None:
        stmt = stmt.where(execution.c.type == type)
    async with engine.connect() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def count_transfers(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(transfer))).scalar_one())


async def account_exists(engine: AsyncEngine, account_id: int) -> bool:
    async with engine.connect() as conn:
        return (await conn.execute(select(account.c.account_id).where(account.c.account_id == account_id))).first() is not None


def expect_error(response: httpx.Response, status: int, code: str, *, recorded: bool) -> dict[str, Any]:
    """Assert the error envelope; recorded says whether it must carry the failed execution's ID."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert error["code"] == code, error
    assert ("execution_id" in error) is recorded, error
    if recorded:
        assert error["execution_id"].isdigit()
    return error


async def test_transfer(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    response = await client.post("/v1/transfers", json={"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1.25"}, headers=await auth_headers(ALICE))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution_id"].isdigit() and body["transfer_id"].isdigit()
    assert {key: body[key] for key in ("sender_id", "recipient_id", "currency_id", "amount")} == {"sender_id": str(ALICE), "recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1.250"}
    assert "idempotency-replayed" not in response.headers
    assert (await balance_of(funded, ALICE, CUR), await balance_of(funded, BOB, CUR)) == (8750, 1250)


async def test_transfer_from_spends_the_allowance(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    approved = await client.post("/v1/allowances", json={"spender_id": str(BOB), "currency_id": str(CUR), "amount": "3"}, headers=await auth_headers(ALICE))
    assert approved.status_code == 200, approved.text
    bob = await auth_headers(BOB)
    response = await client.post("/v1/transfers/from", json={"source_id": str(ALICE), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "1"}, headers=bob)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["spender_id"] == str(BOB) and body["source_id"] == str(ALICE) and body["recipient_id"] == str(CAROL)
    assert (body["amount"], body["allowance_remaining"]) == ("1.000", "2.000")
    assert await balance_of(funded, CAROL, CUR) == 1000
    too_much = await client.post("/v1/transfers/from", json={"source_id": str(ALICE), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "2.001"}, headers=bob)
    expect_error(too_much, 409, "insufficient_allowance", recorded=True)
    assert await get_allowance(funded, ALICE, BOB, CUR) == 2000


async def test_approve_overwrites_and_accepts_zero(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(ALICE)
    first = await client.post("/v1/allowances", json={"spender_id": str(BOB), "currency_id": str(CUR), "amount": "5.5"}, headers=headers)
    assert first.status_code == 200, first.text
    assert {key: first.json()[key] for key in ("owner_id", "spender_id", "currency_id", "old_amount", "new_amount")} == {
        "owner_id": str(ALICE),
        "spender_id": str(BOB),
        "currency_id": str(CUR),
        "old_amount": "0.000",
        "new_amount": "5.500",
    }
    zero = await client.post("/v1/allowances", json={"spender_id": str(BOB), "currency_id": str(CUR), "amount": "0"}, headers=headers)
    assert zero.status_code == 200, zero.text
    assert (zero.json()["old_amount"], zero.json()["new_amount"]) == ("5.500", "0.000")
    assert await get_allowance(funded, ALICE, BOB, CUR) == 0
    self_approval = await client.post("/v1/allowances", json={"spender_id": str(ALICE), "currency_id": str(CUR), "amount": "1"}, headers=headers)
    expect_error(self_approval, 422, "invalid_argument", recorded=True)


async def test_claim_create_pay_and_cancel(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    alice, bob, carol = await auth_headers(ALICE), await auth_headers(BOB), await auth_headers(CAROL)
    created = await client.post("/v1/claims", json={"payer_id": str(ALICE), "currency_id": str(CUR), "amount": "2", "description": "lunch"}, headers=bob)
    assert created.status_code == 200, created.text
    claim = created.json()
    assert {key: claim[key] for key in ("claimant_id", "payer_id", "currency_id", "amount", "description")} == {
        "claimant_id": str(BOB),
        "payer_id": str(ALICE),
        "currency_id": str(CUR),
        "amount": "2.000",
        "description": "lunch",
    }
    claim_id = claim["claim_id"]
    expect_error(await client.post(f"/v1/claims/{claim_id}/pay", headers=carol), 403, "permission_denied", recorded=True)
    paid = await client.post(f"/v1/claims/{claim_id}/pay", headers=alice)
    assert paid.status_code == 200, paid.text
    assert {key: paid.json()[key] for key in ("claim_id", "payer_id", "claimant_id", "amount")} == {"claim_id": claim_id, "payer_id": str(ALICE), "claimant_id": str(BOB), "amount": "2.000"}
    assert paid.json()["transfer_id"].isdigit()
    assert await balance_of(funded, BOB, CUR) == 2000
    expect_error(await client.post(f"/v1/claims/{claim_id}/pay", headers=alice), 409, "invalid_state", recorded=True)

    second = (await client.post("/v1/claims", json={"payer_id": str(ALICE), "currency_id": str(CUR), "amount": "1", "description": "   "}, headers=bob)).json()
    assert second["description"] is None
    expect_error(await client.post(f"/v1/claims/{second['claim_id']}/cancel", headers=carol), 403, "permission_denied", recorded=True)
    canceled = await client.post(f"/v1/claims/{second['claim_id']}/cancel", headers=alice)
    assert canceled.status_code == 200, canceled.text
    assert {key: canceled.json()[key] for key in ("claim_id", "canceled_by")} == {"claim_id": second["claim_id"], "canceled_by": str(ALICE)}
    created_before = await count_executions(funded, ExecutionType.CLAIM_CREATE)
    too_long = await client.post("/v1/claims", json={"payer_id": str(ALICE), "currency_id": str(CUR), "amount": "1", "description": "x" * (CLAIM_DESCRIPTION_MAX + 1)}, headers=bob)
    error = expect_error(too_long, 422, "validation_error", recorded=False)
    assert [(detail["loc"], detail["type"]) for detail in error["details"]] == [(["body", "description"], "string_too_long")]
    assert await count_executions(funded, ExecutionType.CLAIM_CREATE) == created_before
    longest = await client.post("/v1/claims", json={"payer_id": str(ALICE), "currency_id": str(CUR), "amount": "1", "description": "x" * CLAIM_DESCRIPTION_MAX}, headers=bob)
    assert longest.status_code == 200, longest.text
    assert longest.json()["description"] == "x" * CLAIM_DESCRIPTION_MAX
    expect_error(await client.post("/v1/claims", json={"payer_id": str(BOB), "currency_id": str(CUR), "amount": "1"}, headers=bob), 422, "invalid_argument", recorded=True)
    expect_error(await client.post("/v1/claims", json={"payer_id": str(ALICE), "currency_id": str(MISSING_CUR), "amount": "1"}, headers=bob), 404, "currency_not_found", recorded=True)
    expect_error(await client.post("/v1/claims/999999/pay", headers=alice), 404, "claim_not_found", recorded=True)
    for bad in ("0", "abc", "-1", str(2**63)):
        expect_error(await client.post(f"/v1/claims/{bad}/cancel", headers=alice), 422, "validation_error", recorded=False)


async def test_stake_deposit_and_withdraw(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(ALICE)
    deposited = await client.post("/v1/stakes/deposit", json={"currency_id": str(CUR), "amount": "4"}, headers=headers)
    assert deposited.status_code == 200, deposited.text
    body = deposited.json()
    assert {key: body[key] for key in ("account_id", "currency_id", "amount", "stake_amount", "interest", "interest_execution_id")} == {
        "account_id": str(ALICE),
        "currency_id": str(CUR),
        "amount": "4.000",
        "stake_amount": "4.000",
        "interest": "0.000",
        "interest_execution_id": None,
    }
    withdrawn = await client.post("/v1/stakes/withdraw", json={"currency_id": str(CUR), "amount": "1"}, headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert (withdrawn.json()["amount"], withdrawn.json()["stake_amount"]) == ("1.000", "3.000")
    assert await balance_of(funded, ALICE, CUR) == 7000
    expect_error(await client.post("/v1/stakes/withdraw", json={"currency_id": str(CUR), "amount": "3.001"}, headers=headers), 409, "insufficient_stake", recorded=True)
    async with funded.connect() as conn:
        assert (await conn.execute(select(stake.c.amount).where(stake.c.account_id == ALICE))).scalar_one() == 3000


async def test_writes_need_a_valid_token(client: httpx.AsyncClient, funded: AsyncEngine) -> None:
    revoked = await issue_token(funded, account_id=ALICE)
    await revoke_tokens(funded, account_id=ALICE)
    before = await count_executions(funded)
    cases: list[dict[str, str]] = [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer a b"}, bearer(generate_token()), bearer(revoked.token)]
    for path, body in WRITES:
        for headers in cases:
            response = await client.post(path, json=body, headers=headers)
            expect_error(response, 401, "unauthorized", recorded=False)
            assert response.headers["www-authenticate"] == "Bearer", path
    assert await count_executions(funded) == before
    assert not await account_exists(funded, BOB)


async def test_initiator_is_never_a_body_field(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    body = {"sender_id": str(BOB), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "1"}
    error = expect_error(await client.post("/v1/transfers", json=body, headers=await auth_headers(ALICE)), 422, "validation_error", recorded=False)
    assert [(detail["loc"], detail["type"]) for detail in error["details"]] == [(["body", "<extra>"], "extra_forbidden")]
    assert await count_executions(funded, ExecutionType.TRANSFER) == 0


async def test_same_key_replays_the_first_result(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "pay-bob-1"}
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}
    first = await client.post("/v1/transfers", json=payload, headers=headers)
    again = await client.post("/v1/transfers", json=payload, headers=headers)
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert "idempotency-replayed" not in first.headers
    assert again.headers["idempotency-replayed"] == "true"
    assert await count_executions(funded, ExecutionType.TRANSFER) == 1
    assert await count_transfers(funded) == 2
    assert await balance_of(funded, BOB, CUR) == 1000


async def test_failed_execution_replays_its_envelope(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "too-much"}
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "10.001"}
    first = await client.post("/v1/transfers", json=payload, headers=headers)
    error = expect_error(first, 409, "insufficient_balance", recorded=True)
    again = await client.post("/v1/transfers", json=payload, headers=headers)
    assert again.status_code == 409
    assert again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true"
    async with funded.connect() as conn:
        status = (await conn.execute(select(execution.c.status).where(execution.c.execution_id == int(error["execution_id"])))).scalar_one()
    assert status == "failed"
    assert await count_executions(funded, ExecutionType.TRANSFER) == 1


async def test_key_reuse_with_another_body_conflicts(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "k-1"}
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}
    assert (await client.post("/v1/transfers", json=payload, headers=headers)).status_code == 200
    expect_error(await client.post("/v1/transfers", json={**payload, "amount": "2"}, headers=headers), 422, "idempotency_conflict", recorded=False)
    expect_error(await client.post("/v1/stakes/deposit", json={"currency_id": str(CUR), "amount": "1"}, headers=headers), 422, "idempotency_conflict", recorded=False)
    assert await count_executions(funded, ExecutionType.TRANSFER) == 1
    assert await count_executions(funded, ExecutionType.STAKE_DEPOSIT) == 0
    assert await balance_of(funded, BOB, CUR) == 1000


async def test_keys_are_scoped_to_the_caller(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(funded, CUR, BOB, 5000)
    key = {"Idempotency-Key": "shared-key"}
    by_alice = await client.post("/v1/transfers", json={"recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "1"}, headers={**await auth_headers(ALICE), **key})
    by_bob = await client.post("/v1/transfers", json={"recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "2"}, headers={**await auth_headers(BOB), **key})
    assert (by_alice.status_code, by_bob.status_code) == (200, 200), by_bob.text
    assert "idempotency-replayed" not in by_bob.headers
    assert by_alice.json()["execution_id"] != by_bob.json()["execution_id"]
    assert await balance_of(funded, CAROL, CUR) == 3000


async def test_concurrent_requests_with_one_key_run_once(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "storm"}
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1.5"}
    responses = await asyncio.gather(*(client.post("/v1/transfers", json=payload, headers=headers) for _ in range(10)))
    assert [response.status_code for response in responses] == [200] * 10, [response.text for response in responses]
    assert len({response.text for response in responses}) == 1
    assert sum(1 for response in responses if "idempotency-replayed" not in response.headers) == 1
    assert await count_executions(funded, ExecutionType.TRANSFER) == 1
    assert await balance_of(funded, BOB, CUR) == 1500
    assert await balance_of(funded, ALICE, CUR) == 8500


@pytest.mark.parametrize("amount", [1, 1.5, "1.2345", "-1", "1e3", "", " 1", "0", "01", ".5", None, True, "1" * 60])
async def test_transfer_amounts_are_strict(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders, amount: object) -> None:
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": amount}
    error = expect_error(await client.post("/v1/transfers", json=payload, headers=await auth_headers(ALICE)), 422, "validation_error", recorded=False)
    assert [detail["loc"] for detail in error["details"]] == [["body", "amount"]]
    assert await count_executions(funded, ExecutionType.TRANSFER) == 0
    assert not await account_exists(funded, BOB)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/v1/transfers/from", "amount"),
        ("/v1/claims", "amount"),
        ("/v1/stakes/deposit", "amount"),
        ("/v1/stakes/withdraw", "amount"),
        (f"/v1/currencies/{CUR}/mint", "amount"),
        (f"/v1/currencies/{CUR}/burn", "amount"),
    ],
)
async def test_positive_amount_fields_reject_zero_and_numbers(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders, path: str, field: str) -> None:
    [body] = [body for write_path, body in WRITES if write_path == path]
    assert body is not None
    headers = await auth_headers(ALICE)
    before = await count_executions(funded)
    for amount in ("0", "0.000", 1, "-1", "1.0001"):
        error = expect_error(await client.post(path, json={**body, field: amount}, headers=headers), 422, "validation_error", recorded=False)
        assert [detail["loc"] for detail in error["details"]] == [["body", field]]
    assert await count_executions(funded) == before


@pytest.mark.parametrize("amount", [0, "-1", "0.0001", "1e3"])
async def test_approve_amount_format(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders, amount: object) -> None:
    payload = {"spender_id": str(BOB), "currency_id": str(CUR), "amount": amount}
    expect_error(await client.post("/v1/allowances", json=payload, headers=await auth_headers(ALICE)), 422, "validation_error", recorded=False)
    assert await count_executions(funded, ExecutionType.APPROVE) == 0


@pytest.mark.parametrize("field", ["recipient_id", "currency_id"])
@pytest.mark.parametrize("value", [7102, "-5", "0", "07102", "abc", str(2**63)])
async def test_id_fields_are_strict(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders, field: str, value: object) -> None:
    payload: dict[str, object] = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1", field: value}
    error = expect_error(await client.post("/v1/transfers", json=payload, headers=await auth_headers(ALICE)), 422, "validation_error", recorded=False)
    assert [detail["loc"] for detail in error["details"]] == [["body", field]]
    assert await count_executions(funded, ExecutionType.TRANSFER) == 0


async def test_domain_failures_are_recorded_and_mapped(client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(ALICE)
    insufficient = await client.post("/v1/transfers", json={"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "10.001"}, headers=headers)
    expect_error(insufficient, 409, "insufficient_balance", recorded=True)
    missing = await client.post("/v1/transfers", json={"recipient_id": str(BOB), "currency_id": str(MISSING_CUR), "amount": "1"}, headers=headers)
    expect_error(missing, 404, "currency_not_found", recorded=True)
    to_self = await client.post("/v1/transfers", json={"recipient_id": str(ALICE), "currency_id": str(CUR), "amount": "1"}, headers=headers)
    expect_error(to_self, 422, "invalid_argument", recorded=True)
    async with funded.connect() as conn:
        statuses = (await conn.execute(select(execution.c.status).where(execution.c.type == ExecutionType.TRANSFER))).scalars().all()
    assert list(statuses) == ["failed"] * 3
    assert await count_transfers(funded) == 1
    assert await balance_of(funded, ALICE, CUR) == 10_000
