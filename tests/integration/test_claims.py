from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.accounts import ensure_user_accounts
from rapidwire.core.claims import (
    CancelClaimResult,
    Claim,
    ClaimRole,
    CreateClaimResult,
    PayClaimResult,
    cancel_claim,
    cancel_pending_claims_for_currency,
    create_claim,
    get_claim,
    list_claims_for_user,
    pay_claim,
)
from rapidwire.core.errors import (
    ClaimNotFound,
    CurrencyNotFound,
    DomainError,
    InsufficientBalance,
    InvalidAmount,
    InvalidArgument,
    InvalidState,
    PermissionDenied,
)
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, run_operation
from rapidwire.core.types import ClaimStatus, ExecutionStatus, ExecutionType, LabelBasis, Origin, OutboxKind, OutboxStatus, TransferKind
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, claim, currency, execution, notification_permission, outbox
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 5, 3, tzinfo=UTC)
CUR = 11
OTHER_CUR = 12
CLAIMANT = 111
PAYER = 222
OUTSIDER = 333
# A non-ASCII character (HIRAGANA LETTER A) that MySQL stores as three bytes, for checking that the description limit counts characters.
WIDE_CHAR = chr(0x3042)


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="CLM", now=NOW)
    await create_currency(db, currency_id=OTHER_CUR, symbol="CLX", now=NOW)
    await mint(db, CUR, PAYER, 1_000, now=NOW)
    return db


async def _allow_notifications(engine: AsyncEngine, user_id: int, allowed_user_id: int) -> None:
    async with engine.begin() as conn:
        await ensure_user_accounts(conn, [user_id, allowed_user_id], now=NOW)
        await conn.execute(insert(notification_permission).values(user_id=user_id, allowed_user_id=allowed_user_id, created_at=NOW))


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one()
    return dict(row)


async def _count(engine: AsyncEngine, table: Any, *where: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one())


async def _outbox_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(outbox).order_by(outbox.c.outbox_id))).mappings().all()]


async def _assert_failed(engine: AsyncEngine, error: DomainError, type: str) -> None:
    assert error.execution_id is not None
    row = await _execution(engine, error.execution_id)
    assert (row["type"], row["status"]) == (type, ExecutionStatus.FAILED)
    assert row["output"] == {"error": {"code": error.code, "message": error.message}}
    assert await _count(engine, transfer_table, transfer_table.c.execution_id == error.execution_id) == 0
    assert await _count(engine, outbox, outbox.c.execution_id == error.execution_id) == 0


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def _new_claim(engine: AsyncEngine, amount: int = 300, *, currency_id: int = CUR, claimant_id: int = CLAIMANT, payer_id: int = PAYER) -> CreateClaimResult:
    return await create_claim(engine, claimant_id=claimant_id, payer_id=payer_id, currency_id=currency_id, amount=amount, description="lunch", now=NOW)


async def test_create_claim_without_permission_enqueues_nothing(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    assert created == CreateClaimResult(execution_id=created.execution_id, claim_id=created.claim_id, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=300, description="lunch")
    stored = await get_claim(funded, created.claim_id)
    assert stored == Claim(
        claim_id=created.claim_id,
        claimant_id=CLAIMANT,
        payer_id=PAYER,
        currency_id=CUR,
        amount=300,
        description="lunch",
        status=ClaimStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        created_execution_id=created.execution_id,
        created_execution_origin=Origin.NATIVE,
        created_execution_label_basis=None,
        resolved_execution_id=None,
        resolved_execution_origin=None,
        resolved_execution_label_basis=None,
    )
    row = await _execution(funded, created.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.CLAIM_CREATE, ExecutionStatus.SUCCESS, CLAIMANT)
    assert row["input"] == {"payer_id": str(PAYER), "currency_id": str(CUR), "amount": "300", "description": "lunch"}
    assert await _outbox_rows(funded) == []
    await _allow_notifications(funded, CLAIMANT, PAYER)
    await _new_claim(funded)
    assert await _outbox_rows(funded) == []
    await _assert_invariants(funded)


async def test_create_claim_with_permission_enqueues_dm_payload(funded: AsyncEngine) -> None:
    await _allow_notifications(funded, PAYER, CLAIMANT)
    created = await _new_claim(funded, 1_234)
    rows = await _outbox_rows(funded)
    assert len(rows) == 1
    assert rows[0]["kind"] == OutboxKind.CLAIM_CREATED
    assert rows[0]["status"] == OutboxStatus.PENDING
    assert rows[0]["execution_id"] == created.execution_id
    assert rows[0]["available_at"] == NOW
    assert rows[0]["payload"] == {
        "claim_id": str(created.claim_id),
        "claimant_id": str(CLAIMANT),
        "payer_id": str(PAYER),
        "currency_id": str(CUR),
        "currency_symbol": "CLM",
        "amount": "1234",
        "description": "lunch",
    }
    output = (await _execution(funded, created.execution_id))["output"]
    assert set(output) == {"claim_id", "claimant_id", "payer_id", "currency_id", "amount", "description"}


async def test_description_limit_counts_characters(funded: AsyncEngine) -> None:
    created = await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=1, description=WIDE_CHAR * 50, now=NOW)
    assert (await get_claim(funded, created.claim_id)).description == WIDE_CHAR * 50
    plain = await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=1, now=NOW)
    assert (await get_claim(funded, plain.claim_id)).description is None


@pytest.mark.parametrize("description", ["x" * 51, WIDE_CHAR * 51, 5])
async def test_bad_description_is_rejected_before_any_execution(funded: AsyncEngine, description: Any) -> None:
    with pytest.raises(InvalidArgument) as caught:
        await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=1, description=description, now=NOW)
    assert caught.value.execution_id is None
    assert await _count(funded, execution, execution.c.type == ExecutionType.CLAIM_CREATE) == 0
    assert await _count(funded, claim) == 0


@pytest.mark.parametrize("description", [None, "", "   ", "\t\n"])
async def test_blank_description_is_stored_as_none(funded: AsyncEngine, description: str | None) -> None:
    await _allow_notifications(funded, PAYER, CLAIMANT)
    created = await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=5, description=description, now=NOW)
    assert created.description is None
    assert (await get_claim(funded, created.claim_id)).description is None
    assert (await _execution(funded, created.execution_id))["input"]["description"] is None
    rows = await _outbox_rows(funded)
    assert len(rows) == 1
    assert rows[0]["payload"]["description"] is None
    assert rows[0]["payload"]["amount"] == "5"


async def test_idempotent_create_claim_replay(funded: AsyncEngine) -> None:
    await _allow_notifications(funded, PAYER, CLAIMANT)
    first = await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=9, description="tea", now=NOW, idempotency_key="claim-1")
    again = await create_claim(funded, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=9, description="tea", now=NOW, idempotency_key="claim-1")
    assert again == first
    assert again.replayed
    assert await _count(funded, claim) == 1
    assert len(await _outbox_rows(funded)) == 1


async def test_idempotent_cancel_replay_and_failure_replay(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    first = await cancel_claim(funded, claim_id=created.claim_id, user_id=PAYER, now=NOW, idempotency_key="cancel-1")
    again = await cancel_claim(funded, claim_id=created.claim_id, user_id=PAYER, now=NOW, idempotency_key="cancel-1")
    assert again == first
    assert again.replayed
    assert await _count(funded, execution, execution.c.type == ExecutionType.CLAIM_CANCEL) == 1
    with pytest.raises(InvalidState) as failed:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW, idempotency_key="pay-late")
    with pytest.raises(InvalidState) as replayed:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW, idempotency_key="pay-late")
    assert failed.value.execution_id is not None
    assert replayed.value.execution_id == failed.value.execution_id
    assert await _count(funded, execution, execution.c.type == ExecutionType.CLAIM_PAY) == 1


@pytest.mark.parametrize(("claim_id", "error"), [(True, InvalidArgument), ("1", InvalidArgument), (1.0, InvalidArgument), (0, ClaimNotFound), (2**63, ClaimNotFound)])
async def test_bad_claim_id_is_rejected_before_any_execution(funded: AsyncEngine, claim_id: Any, error: type[DomainError]) -> None:
    with pytest.raises(error) as paying:
        await pay_claim(funded, claim_id=claim_id, payer_id=PAYER, now=NOW)
    with pytest.raises(error) as canceling:
        await cancel_claim(funded, claim_id=claim_id, user_id=PAYER, now=NOW)
    with pytest.raises(error):
        await get_claim(funded, claim_id)
    assert paying.value.execution_id is None
    assert canceling.value.execution_id is None
    assert await _count(funded, execution, execution.c.type.in_([ExecutionType.CLAIM_PAY, ExecutionType.CLAIM_CANCEL])) == 0


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"amount": 0}, InvalidAmount),
        ({"amount": -3}, InvalidAmount),
        ({"payer_id": CLAIMANT}, InvalidArgument),
        ({"payer_id": STAKE_CUSTODY_ACCOUNT_ID}, InvalidArgument),
        ({"currency_id": 999}, CurrencyNotFound),
    ],
)
async def test_rejected_claim_creation_is_recorded_and_creates_nothing(funded: AsyncEngine, overrides: dict[str, Any], error: type[DomainError]) -> None:
    await _allow_notifications(funded, PAYER, CLAIMANT)
    args: dict[str, Any] = {"claimant_id": CLAIMANT, "payer_id": PAYER, "currency_id": CUR, "amount": 10, "description": None} | overrides
    with pytest.raises(error) as caught:
        await create_claim(funded, now=NOW, **args)
    await _assert_failed(funded, caught.value, ExecutionType.CLAIM_CREATE)
    assert await _count(funded, claim) == 0
    assert await _outbox_rows(funded) == []


async def test_pay_claim_moves_funds_and_marks_paid(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    later = NOW + timedelta(minutes=5)
    paid = await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=later)
    assert paid == PayClaimResult(execution_id=paid.execution_id, claim_id=created.claim_id, transfer_id=paid.transfer_id, payer_id=PAYER, claimant_id=CLAIMANT, currency_id=CUR, amount=300)
    assert await balance_of(funded, PAYER, CUR) == 700
    assert await balance_of(funded, CLAIMANT, CUR) == 300
    stored = await get_claim(funded, created.claim_id)
    assert (stored.status, stored.resolved_execution_id, stored.updated_at) == (ClaimStatus.PAID, paid.execution_id, later)
    row = await _execution(funded, paid.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.CLAIM_PAY, ExecutionStatus.SUCCESS, PAYER)
    async with funded.connect() as conn:
        moved = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == paid.execution_id))).mappings().all()
    assert [(m["transfer_id"], m["from_account"], m["to_account"], m["amount"], m["kind"]) for m in moved] == [(paid.transfer_id, PAYER, CLAIMANT, 300, TransferKind.PRINCIPAL)]
    await _assert_invariants(funded)


@pytest.mark.parametrize(("payer_id", "error"), [(CLAIMANT, PermissionDenied), (OUTSIDER, PermissionDenied)])
async def test_only_the_payer_can_pay(funded: AsyncEngine, payer_id: int, error: type[DomainError]) -> None:
    created = await _new_claim(funded)
    with pytest.raises(error) as caught:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=payer_id, now=NOW)
    await _assert_failed(funded, caught.value, ExecutionType.CLAIM_PAY)
    assert (await get_claim(funded, created.claim_id)).status is ClaimStatus.PENDING
    assert await balance_of(funded, PAYER, CUR) == 1_000


async def test_pay_with_insufficient_balance_keeps_claim_pending(funded: AsyncEngine) -> None:
    created = await _new_claim(funded, 1_001)
    with pytest.raises(InsufficientBalance) as caught:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW)
    await _assert_failed(funded, caught.value, ExecutionType.CLAIM_PAY)
    stored = await get_claim(funded, created.claim_id)
    assert (stored.status, stored.resolved_execution_id) == (ClaimStatus.PENDING, None)
    await _assert_invariants(funded)


async def test_missing_claim(funded: AsyncEngine) -> None:
    with pytest.raises(ClaimNotFound) as paying:
        await pay_claim(funded, claim_id=424242, payer_id=PAYER, now=NOW)
    await _assert_failed(funded, paying.value, ExecutionType.CLAIM_PAY)
    with pytest.raises(ClaimNotFound) as canceling:
        await cancel_claim(funded, claim_id=424242, user_id=PAYER, now=NOW)
    await _assert_failed(funded, canceling.value, ExecutionType.CLAIM_CANCEL)
    with pytest.raises(ClaimNotFound):
        await get_claim(funded, 424242)


@pytest.mark.parametrize("user_id", [CLAIMANT, PAYER])
async def test_cancel_by_either_party(funded: AsyncEngine, user_id: int) -> None:
    created = await _new_claim(funded)
    canceled = await cancel_claim(funded, claim_id=created.claim_id, user_id=user_id, now=NOW)
    assert canceled == CancelClaimResult(execution_id=canceled.execution_id, claim_id=created.claim_id, canceled_by=user_id)
    stored = await get_claim(funded, created.claim_id)
    assert (stored.status, stored.resolved_execution_id) == (ClaimStatus.CANCELED, canceled.execution_id)
    row = await _execution(funded, canceled.execution_id)
    assert (row["type"], row["initiator_id"]) == (ExecutionType.CLAIM_CANCEL, user_id)
    with pytest.raises(InvalidState) as paying:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW)
    await _assert_failed(funded, paying.value, ExecutionType.CLAIM_PAY)
    with pytest.raises(InvalidState) as again:
        await cancel_claim(funded, claim_id=created.claim_id, user_id=user_id, now=NOW)
    await _assert_failed(funded, again.value, ExecutionType.CLAIM_CANCEL)
    assert await balance_of(funded, PAYER, CUR) == 1_000


async def test_outsider_cannot_cancel_and_paid_claim_cannot_be_canceled(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    with pytest.raises(PermissionDenied) as outsider:
        await cancel_claim(funded, claim_id=created.claim_id, user_id=OUTSIDER, now=NOW)
    await _assert_failed(funded, outsider.value, ExecutionType.CLAIM_CANCEL)
    paid = await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW)
    with pytest.raises(InvalidState):
        await cancel_claim(funded, claim_id=created.claim_id, user_id=CLAIMANT, now=NOW)
    with pytest.raises(InvalidState):
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW)
    stored = await get_claim(funded, created.claim_id)
    assert (stored.status, stored.resolved_execution_id) == (ClaimStatus.PAID, paid.execution_id)
    assert await balance_of(funded, CLAIMANT, CUR) == 300


async def test_claim_of_deleted_currency_cannot_be_paid(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    async with funded.begin() as conn:
        await conn.execute(update(currency).where(currency.c.currency_id == CUR).values(deleted_at=NOW))
    with pytest.raises(CurrencyNotFound) as caught:
        await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW)
    await _assert_failed(funded, caught.value, ExecutionType.CLAIM_PAY)


async def test_idempotent_pay_replay_pays_once(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    first = await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW, idempotency_key="pay-claim")
    again = await pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW, idempotency_key="pay-claim")
    assert again == first
    assert again.replayed
    assert await balance_of(funded, CLAIMANT, CUR) == 300
    assert await _count(funded, execution, execution.c.type == ExecutionType.CLAIM_PAY) == 1


async def test_concurrent_double_pay_pays_exactly_once(funded: AsyncEngine) -> None:
    created = await _new_claim(funded, 400)
    outcomes = await asyncio.gather(*(pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW) for _ in range(2)), return_exceptions=True)
    paid = [o for o in outcomes if isinstance(o, PayClaimResult)]
    rejected = [o for o in outcomes if not isinstance(o, PayClaimResult)]
    assert len(paid) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], InvalidState)
    assert await balance_of(funded, PAYER, CUR) == 600
    assert await balance_of(funded, CLAIMANT, CUR) == 400
    assert (await get_claim(funded, created.claim_id)).resolved_execution_id == paid[0].execution_id
    await _assert_invariants(funded)


async def test_cancel_pending_claims_for_currency(funded: AsyncEngine) -> None:
    paid = await _new_claim(funded)
    await pay_claim(funded, claim_id=paid.claim_id, payer_id=PAYER, now=NOW)
    pending = [await _new_claim(funded), await _new_claim(funded, claimant_id=OUTSIDER)]
    other = await _new_claim(funded, currency_id=OTHER_CUR)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR], for_update=True)
        return {"canceled": await cancel_pending_claims_for_currency(tx, execution_id, currency_id=CUR)}

    later = NOW + timedelta(hours=1)
    result = await run_operation(funded, type=ExecutionType.CURRENCY_DELETE_FINALIZE, initiator_id=PAYER, input={"currency_id": CUR}, body=body, now=later)
    assert result.int_field("canceled") == 2
    for created in pending:
        stored = await get_claim(funded, created.claim_id)
        assert (stored.status, stored.resolved_execution_id, stored.updated_at) == (ClaimStatus.CANCELED, result.execution_id, later)
    assert (await get_claim(funded, paid.claim_id)).status is ClaimStatus.PAID
    assert (await get_claim(funded, other.claim_id)).status is ClaimStatus.PENDING


@pytest.mark.parametrize("for_update", [None, False])
async def test_cancel_pending_claims_needs_the_currency_for_update(funded: AsyncEngine, for_update: bool | None) -> None:
    pending = await _new_claim(funded)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        if for_update is not None:
            await tx.lock_currencies([CUR], for_update=for_update)
        return {"canceled": await cancel_pending_claims_for_currency(tx, execution_id, currency_id=CUR)}

    with pytest.raises(LedgerUsageError):
        await run_operation(funded, type=ExecutionType.CURRENCY_DELETE_FINALIZE, initiator_id=PAYER, input={"currency_id": CUR}, body=body, now=NOW)
    assert (await get_claim(funded, pending.claim_id)).status is ClaimStatus.PENDING


async def test_list_claims_for_user_pages_newest_first(funded: AsyncEngine) -> None:
    created = [await _new_claim(funded, amount) for amount in range(1, 18)]
    unrelated = await _new_claim(funded, claimant_id=OUTSIDER, payer_id=444)
    reverse = await _new_claim(funded, claimant_id=PAYER, payer_id=CLAIMANT)
    await cancel_claim(funded, claim_id=created[0].claim_id, user_id=CLAIMANT, now=NOW)

    first_page = await list_claims_for_user(funded, CLAIMANT)
    assert (len(first_page.items), first_page.total, first_page.pages, first_page.per_page) == (15, 18, 2, 15)
    assert first_page.items[0].claim_id == reverse.claim_id
    assert [c.claim_id for c in first_page.items[1:]] == [c.claim_id for c in reversed(created[3:])]
    second_page = await list_claims_for_user(funded, CLAIMANT, page=2)
    assert [c.claim_id for c in second_page.items] == [c.claim_id for c in reversed(created[:3])]
    assert (await list_claims_for_user(funded, CLAIMANT, page=3)).items == ()
    assert unrelated.claim_id not in {c.claim_id for c in first_page.items + second_page.items}
    canceled = await list_claims_for_user(funded, CLAIMANT, status=ClaimStatus.CANCELED)
    assert [c.claim_id for c in canceled.items] == [created[0].claim_id]
    assert canceled.total == 1
    async with funded.connect() as conn:
        assert [c.claim_id for c in (await list_claims_for_user(conn, OUTSIDER)).items] == [unrelated.claim_id]
    for page, per_page in ((0, 15), (1, 0), (1, 101)):
        with pytest.raises(InvalidArgument):
            await list_claims_for_user(funded, CLAIMANT, page=page, per_page=per_page)
    with pytest.raises(InvalidArgument):
        await list_claims_for_user(funded, CLAIMANT, status="settled")  # type: ignore[arg-type]


async def test_list_claims_for_user_filters_by_role(funded: AsyncEngine) -> None:
    made = [await _new_claim(funded, amount) for amount in (1, 2)]
    against = await _new_claim(funded, claimant_id=PAYER, payer_id=CLAIMANT)
    await cancel_claim(funded, claim_id=made[0].claim_id, user_id=CLAIMANT, now=NOW)

    as_claimant = await list_claims_for_user(funded, CLAIMANT, role=ClaimRole.CLAIMANT)
    assert ([c.claim_id for c in as_claimant.items], as_claimant.total) == ([made[1].claim_id, made[0].claim_id], 2)
    as_payer = await list_claims_for_user(funded, CLAIMANT, role=ClaimRole.PAYER)
    assert ([c.claim_id for c in as_payer.items], as_payer.total) == ([against.claim_id], 1)
    both = await list_claims_for_user(funded, CLAIMANT)
    assert [c.claim_id for c in both.items] == [against.claim_id, made[1].claim_id, made[0].claim_id]
    canceled_made = await list_claims_for_user(funded, CLAIMANT, role=ClaimRole.CLAIMANT, status=ClaimStatus.CANCELED)
    assert [c.claim_id for c in canceled_made.items] == [made[0].claim_id]
    with pytest.raises(InvalidArgument):
        await list_claims_for_user(funded, CLAIMANT, role="owner")  # type: ignore[arg-type]


async def test_claims_carry_the_origin_of_their_executions(funded: AsyncEngine) -> None:
    created = await _new_claim(funded)
    canceled = await cancel_claim(funded, claim_id=created.claim_id, user_id=PAYER, now=NOW)
    async with funded.begin() as conn:
        tx = LedgerTx(conn, NOW)
        legacy_execution = await tx.create_execution(type=ExecutionType.CLAIM_CREATE, initiator_id=CLAIMANT, input={}, origin=Origin.LEGACY, label_basis=LabelBasis.STATE_IMPORT)
        inserted = await conn.execute(
            insert(claim).values(
                claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CUR, amount=7, status=ClaimStatus.PENDING.value, created_at=NOW, updated_at=NOW, created_execution_id=legacy_execution
            )
        )
    key = inserted.inserted_primary_key
    assert key is not None
    legacy_id = int(key[0])
    resolved = await get_claim(funded, created.claim_id)
    assert (resolved.created_execution_origin, resolved.resolved_execution_id, resolved.resolved_execution_origin, resolved.resolved_execution_label_basis) == (
        Origin.NATIVE,
        canceled.execution_id,
        Origin.NATIVE,
        None,
    )
    listed = {c.claim_id: c for c in (await list_claims_for_user(funded, PAYER)).items}
    legacy = listed[legacy_id]
    assert (legacy.created_execution_origin, legacy.created_execution_label_basis, legacy.resolved_execution_origin) == (Origin.LEGACY, LabelBasis.STATE_IMPORT, None)


async def test_pay_and_cancel_race_resolves_each_claim_once(funded: AsyncEngine) -> None:
    paid_total = 0
    for _ in range(15):
        created = await _new_claim(funded, 10)
        outcomes = await asyncio.gather(
            pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW),
            cancel_claim(funded, claim_id=created.claim_id, user_id=CLAIMANT, now=NOW),
            return_exceptions=True,
        )
        won = [o for o in outcomes if not isinstance(o, BaseException)]
        lost = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(won) == 1
        assert len(lost) == 1
        assert isinstance(lost[0], InvalidState)
        stored = await get_claim(funded, created.claim_id)
        assert stored.resolved_execution_id == won[0].execution_id
        if isinstance(won[0], PayClaimResult):
            paid_total += 10
            assert stored.status is ClaimStatus.PAID
        else:
            assert stored.status is ClaimStatus.CANCELED
    assert await balance_of(funded, CLAIMANT, CUR) == paid_total
    assert await balance_of(funded, PAYER, CUR) == 1_000 - paid_total
    await _assert_invariants(funded)


@pytest.mark.parametrize("pause", [0.0, 0.2])
async def test_cascade_cancel_and_pay_race_resolves_claim_once(funded: AsyncEngine, pause: float) -> None:
    created = await _new_claim(funded, 10)

    async def cascade(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR], for_update=True)
        await asyncio.sleep(pause)
        return {"canceled": await cancel_pending_claims_for_currency(tx, execution_id, currency_id=CUR)}

    swept, paying = await asyncio.gather(
        run_operation(funded, type=ExecutionType.CURRENCY_DELETE_FINALIZE, initiator_id=OUTSIDER, input={}, body=cascade, now=NOW),
        pay_claim(funded, claim_id=created.claim_id, payer_id=PAYER, now=NOW),
        return_exceptions=True,
    )
    assert not isinstance(swept, BaseException)
    stored = await get_claim(funded, created.claim_id)
    if isinstance(paying, PayClaimResult):
        assert swept.int_field("canceled") == 0
        assert (stored.status, stored.resolved_execution_id) == (ClaimStatus.PAID, paying.execution_id)
        assert await balance_of(funded, CLAIMANT, CUR) == 10
    else:
        assert isinstance(paying, InvalidState)
        assert swept.int_field("canceled") == 1
        assert (stored.status, stored.resolved_execution_id) == (ClaimStatus.CANCELED, swept.execution_id)
        assert await balance_of(funded, CLAIMANT, CUR) == 0
    await _assert_invariants(funded)
