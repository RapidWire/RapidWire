"""Claims, which never expire: a claimant asks a payer for an amount of one currency, the payer pays or either party cancels while it is pending, and a new claim notifies the payer through the outbox only when the payer has allowed notifications from the claimant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, RowMapping, Select, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.db import Db, consistent_snapshot, reader
from rapidwire.core.errors import ClaimNotFound, CurrencyNotFound, InvalidArgument, InvalidState, PermissionDenied
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, OperationResult, run_operation
from rapidwire.core.outbox import enqueue
from rapidwire.core.paging import CLAIMS_PER_PAGE_DEFAULT, CLAIMS_PER_PAGE_MAX, Page, bounded_count, count_bounded, validate_paging
from rapidwire.core.types import ClaimStatus, ExecutionType, LabelBasis, Origin, OutboxKind, TransferKind
from rapidwire.core.validation import BIGINT_MAX, require_storable_int, require_user_account, validate_positive_amount
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX, claim, execution, notification_permission

__all__ = [
    "CLAIM_DESCRIPTION_MAX",
    "CLAIM_ID_MAX",
    "CancelClaimResult",
    "Claim",
    "ClaimRole",
    "CreateClaimResult",
    "PayClaimResult",
    "cancel_claim",
    "cancel_claim_in_tx",
    "cancel_pending_claims_for_currency",
    "create_claim",
    "create_claim_in_tx",
    "get_claim",
    "list_claims_for_user",
    "normalize_description",
    "pay_claim",
    "pay_claim_in_tx",
    "validate_claim_id",
]

CLAIM_ID_MAX = BIGINT_MAX


class ClaimRole(StrEnum):
    """Which side of a claim a listed user is on."""

    CLAIMANT = "claimant"
    PAYER = "payer"


@dataclass(frozen=True)
class _ClaimRow:
    """The claim columns a resolution needs, read under the claim's row lock."""

    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    status: ClaimStatus


@dataclass(frozen=True)
class Claim:
    """A claim with the origin and label basis of the executions that created and resolved it; resolved_execution_id (and its origin) is None while pending."""

    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    description: str | None
    status: ClaimStatus
    created_at: datetime
    updated_at: datetime
    created_execution_id: int
    created_execution_origin: Origin
    created_execution_label_basis: LabelBasis | None
    resolved_execution_id: int | None
    resolved_execution_origin: Origin | None
    resolved_execution_label_basis: LabelBasis | None

    @classmethod
    def from_row(cls, row: RowMapping) -> Claim:
        """A claim from a row of _claims_with_origins()."""
        values = dict(row)
        values["status"] = ClaimStatus(values["status"])
        values["created_execution_origin"] = Origin(values["created_execution_origin"])
        for key, kind in (("created_execution_label_basis", LabelBasis), ("resolved_execution_origin", Origin), ("resolved_execution_label_basis", LabelBasis)):
            values[key] = kind(values[key]) if values[key] is not None else None
        return cls(**values)


_CREATED = execution.alias("created_execution")
_RESOLVED = execution.alias("resolved_execution")


def _claims_with_origins() -> Select[Any]:
    """Every claim column plus the origin and label basis of its creating and resolving executions."""
    joined = claim.join(_CREATED, _CREATED.c.execution_id == claim.c.created_execution_id).outerjoin(_RESOLVED, _RESOLVED.c.execution_id == claim.c.resolved_execution_id)
    return select(
        claim,
        _CREATED.c.origin.label("created_execution_origin"),
        _CREATED.c.label_basis.label("created_execution_label_basis"),
        _RESOLVED.c.origin.label("resolved_execution_origin"),
        _RESOLVED.c.label_basis.label("resolved_execution_label_basis"),
    ).select_from(joined)


def _optional_str(op: OperationResult, key: str) -> str | None:
    value = op.output.get(key)
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class CreateClaimResult:
    """A created claim; replayed takes no part in equality."""

    execution_id: int
    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    description: str | None
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claimant_id": self.claimant_id,
            "payer_id": self.payer_id,
            "currency_id": self.currency_id,
            "amount": self.amount,
            "description": self.description,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> CreateClaimResult:
        return cls(
            execution_id=op.execution_id,
            claim_id=op.int_field("claim_id"),
            claimant_id=op.int_field("claimant_id"),
            payer_id=op.int_field("payer_id"),
            currency_id=op.int_field("currency_id"),
            amount=op.int_field("amount"),
            description=_optional_str(op, "description"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class PayClaimResult:
    """A paid claim and the principal transfer from payer to claimant; replayed takes no part in equality."""

    execution_id: int
    claim_id: int
    transfer_id: int
    payer_id: int
    claimant_id: int
    currency_id: int
    amount: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "transfer_id": self.transfer_id,
            "payer_id": self.payer_id,
            "claimant_id": self.claimant_id,
            "currency_id": self.currency_id,
            "amount": self.amount,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> PayClaimResult:
        return cls(
            execution_id=op.execution_id,
            claim_id=op.int_field("claim_id"),
            transfer_id=op.int_field("transfer_id"),
            payer_id=op.int_field("payer_id"),
            claimant_id=op.int_field("claimant_id"),
            currency_id=op.int_field("currency_id"),
            amount=op.int_field("amount"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class CancelClaimResult:
    """A canceled claim and the party who canceled it; replayed takes no part in equality."""

    execution_id: int
    claim_id: int
    canceled_by: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "canceled_by": self.canceled_by}

    @classmethod
    def from_operation(cls, op: OperationResult) -> CancelClaimResult:
        return cls(execution_id=op.execution_id, claim_id=op.int_field("claim_id"), canceled_by=op.int_field("canceled_by"), replayed=op.replayed)


def normalize_description(description: str | None) -> str | None:
    """Return the description to store: None for None, empty or whitespace-only text, else the text unchanged; raises InvalidArgument for non-text or more than CLAIM_DESCRIPTION_MAX characters."""
    if description is None:
        return None
    if not isinstance(description, str):
        raise InvalidArgument("The description must be text.")
    if not description.strip():
        return None
    if len(description) > CLAIM_DESCRIPTION_MAX:
        raise InvalidArgument(f"The description must be at most {CLAIM_DESCRIPTION_MAX} characters.")
    return description


def validate_claim_id(claim_id: int) -> int:
    """Return claim_id when it can name a claim; raises InvalidArgument for a non-integer and ClaimNotFound for an integer outside the claim ID range."""
    if isinstance(claim_id, bool) or not isinstance(claim_id, int):
        raise InvalidArgument("The claim ID must be an integer.")
    if not 1 <= claim_id <= CLAIM_ID_MAX:
        raise ClaimNotFound()
    return claim_id


async def create_claim_in_tx(
    tx: LedgerTx,
    execution_id: int,
    *,
    claimant_id: int,
    payer_id: int,
    currency_id: int,
    amount: int,
    description: str | None = None,
) -> CreateClaimResult:
    """Insert a pending claim created by execution_id and, when the payer allows notifications from the claimant, a claim_created outbox message, which the result does not reveal; raises InvalidAmount, InvalidArgument or CurrencyNotFound."""
    validate_positive_amount(amount)
    description = normalize_description(description)
    require_user_account(claimant_id, "claimant")
    require_user_account(payer_id, "payer")
    if claimant_id == payer_id:
        raise InvalidArgument("You cannot create a claim against yourself.")
    currency_row = (await tx.lock_currencies([currency_id]))[currency_id]
    await tx.require_accounts([claimant_id, payer_id])
    inserted = await tx.conn.execute(
        insert(claim).values(
            claimant_id=claimant_id,
            payer_id=payer_id,
            currency_id=currency_id,
            amount=amount,
            description=description,
            status=ClaimStatus.PENDING.value,
            created_at=tx.now,
            updated_at=tx.now,
            created_execution_id=execution_id,
            resolved_execution_id=None,
        )
    )
    key = inserted.inserted_primary_key
    if key is None:
        raise RuntimeError("claim insert returned no primary key")
    claim_id = int(key[0])
    permitted = select(notification_permission.c.user_id).where(notification_permission.c.user_id == payer_id, notification_permission.c.allowed_user_id == claimant_id)
    if (await tx.conn.execute(permitted)).first() is not None:
        payload = {
            "claim_id": claim_id,
            "claimant_id": claimant_id,
            "payer_id": payer_id,
            "currency_id": currency_id,
            "currency_symbol": currency_row["symbol"],
            "amount": amount,
            "description": description,
        }
        await enqueue(tx, kind=OutboxKind.CLAIM_CREATED, payload=payload, execution_id=execution_id)
    return CreateClaimResult(execution_id=execution_id, claim_id=claim_id, claimant_id=claimant_id, payer_id=payer_id, currency_id=currency_id, amount=amount, description=description)


async def create_claim(
    engine: AsyncEngine,
    *,
    claimant_id: int,
    payer_id: int,
    currency_id: int,
    amount: int,
    description: str | None = None,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> CreateClaimResult:
    """Create a claim as a claim_create execution initiated by the claimant."""
    require_storable_int(amount)
    description = normalize_description(description)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await create_claim_in_tx(tx, execution_id, claimant_id=claimant_id, payer_id=payer_id, currency_id=currency_id, amount=amount, description=description)
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.CLAIM_CREATE,
        initiator_id=claimant_id,
        input={"payer_id": payer_id, "currency_id": currency_id, "amount": amount, "description": description},
        body=body,
        idempotency_key=idempotency_key,
        now=now,
        user_accounts=[payer_id],
    )
    return CreateClaimResult.from_operation(op)


async def _lock_claim(tx: LedgerTx, claim_id: int) -> tuple[_ClaimRow, bool]:
    """Lock the claim's currency FOR SHARE (deleted or not), then the claim FOR UPDATE, and return the claim and whether its currency is deleted; raises ClaimNotFound.

    Of two concurrent attempts to resolve a claim, the second waits on the claim row and then finds it resolved.
    """
    validate_claim_id(claim_id)
    currency_id = (await tx.conn.execute(select(claim.c.currency_id).where(claim.c.claim_id == claim_id))).scalar_one_or_none()
    if currency_id is None:
        raise ClaimNotFound()
    currency_row = (await tx.lock_currencies([currency_id], include_deleted=True))[currency_id]
    columns = (claim.c.claim_id, claim.c.claimant_id, claim.c.payer_id, claim.c.currency_id, claim.c.amount, claim.c.status)
    row = (await tx.conn.execute(select(*columns).where(claim.c.claim_id == claim_id).with_for_update())).mappings().one()
    locked = _ClaimRow(claim_id=row["claim_id"], claimant_id=row["claimant_id"], payer_id=row["payer_id"], currency_id=row["currency_id"], amount=row["amount"], status=ClaimStatus(row["status"]))
    return locked, currency_row["deleted_at"] is not None


async def _resolve(tx: LedgerTx, execution_id: int, claim_id: int, status: ClaimStatus) -> None:
    await tx.conn.execute(update(claim).where(claim.c.claim_id == claim_id).values(status=status.value, resolved_execution_id=execution_id, updated_at=tx.now))


async def pay_claim_in_tx(tx: LedgerTx, execution_id: int, *, claim_id: int, payer_id: int) -> PayClaimResult:
    """Pay a pending claim: move its amount from payer to claimant and mark it paid by execution_id; raises ClaimNotFound, PermissionDenied, InvalidState, CurrencyNotFound or InsufficientBalance."""
    current, currency_deleted = await _lock_claim(tx, claim_id)
    if current.payer_id != payer_id:
        raise PermissionDenied("Only the payer of this claim can pay it.")
    if current.status is not ClaimStatus.PENDING:
        raise InvalidState(f"The claim is not pending (status: {current.status.value}).")
    if currency_deleted:
        raise CurrencyNotFound(f"Currency {current.currency_id} was not found.")
    await tx.lock_balances([(current.payer_id, current.currency_id), (current.claimant_id, current.currency_id)])
    transfer_id = await tx.move(execution_id, current.currency_id, current.payer_id, current.claimant_id, current.amount, TransferKind.PRINCIPAL)
    await _resolve(tx, execution_id, claim_id, ClaimStatus.PAID)
    return PayClaimResult(
        execution_id=execution_id,
        claim_id=claim_id,
        transfer_id=transfer_id,
        payer_id=current.payer_id,
        claimant_id=current.claimant_id,
        currency_id=current.currency_id,
        amount=current.amount,
    )


async def pay_claim(engine: AsyncEngine, *, claim_id: int, payer_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> PayClaimResult:
    """Pay a claim as a claim_pay execution initiated by the payer."""
    validate_claim_id(claim_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await pay_claim_in_tx(tx, execution_id, claim_id=claim_id, payer_id=payer_id)).to_output()

    op = await run_operation(engine, type=ExecutionType.CLAIM_PAY, initiator_id=payer_id, input={"claim_id": claim_id}, body=body, idempotency_key=idempotency_key, now=now)
    return PayClaimResult.from_operation(op)


async def cancel_claim_in_tx(tx: LedgerTx, execution_id: int, *, claim_id: int, user_id: int) -> CancelClaimResult:
    """Cancel a pending claim on behalf of its payer or claimant and mark it canceled by execution_id; raises ClaimNotFound, PermissionDenied or InvalidState."""
    current, _ = await _lock_claim(tx, claim_id)
    if user_id not in (current.payer_id, current.claimant_id):
        raise PermissionDenied("Only the payer or the claimant of this claim can cancel it.")
    if current.status is not ClaimStatus.PENDING:
        raise InvalidState(f"Only pending claims can be canceled (status: {current.status.value}).")
    await _resolve(tx, execution_id, claim_id, ClaimStatus.CANCELED)
    return CancelClaimResult(execution_id=execution_id, claim_id=claim_id, canceled_by=user_id)


async def cancel_claim(engine: AsyncEngine, *, claim_id: int, user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> CancelClaimResult:
    """Cancel a claim as a claim_cancel execution initiated by the payer or the claimant."""
    validate_claim_id(claim_id)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await cancel_claim_in_tx(tx, execution_id, claim_id=claim_id, user_id=user_id)).to_output()

    op = await run_operation(engine, type=ExecutionType.CLAIM_CANCEL, initiator_id=user_id, input={"claim_id": claim_id}, body=body, idempotency_key=idempotency_key, now=now)
    return CancelClaimResult.from_operation(op)


async def cancel_pending_claims_for_currency(tx: LedgerTx, execution_id: int, *, currency_id: int) -> int:
    """Cancel every pending claim of the currency as resolved by execution_id and return how many there were; the transaction must hold the currency FOR UPDATE (LedgerUsageError otherwise)."""
    if tx.currency_lock(currency_id) != "update":
        raise LedgerUsageError(f"currency {currency_id} must be locked FOR UPDATE before its pending claims are canceled")
    pending = select(claim.c.claim_id).where(claim.c.currency_id == currency_id, claim.c.status == ClaimStatus.PENDING.value).order_by(claim.c.claim_id).with_for_update()
    claim_ids = list((await tx.conn.execute(pending)).scalars())
    if claim_ids:
        await tx.conn.execute(update(claim).where(claim.c.claim_id.in_(claim_ids)).values(status=ClaimStatus.CANCELED.value, resolved_execution_id=execution_id, updated_at=tx.now))
    return len(claim_ids)


async def get_claim(db: Db, claim_id: int) -> Claim:
    """The claim with this ID; raises ClaimNotFound when there is none and InvalidArgument for a non-integer ID."""
    validate_claim_id(claim_id)
    async with reader(db) as conn:
        row = (await conn.execute(_claims_with_origins().where(claim.c.claim_id == claim_id))).mappings().one_or_none()
    if row is None:
        raise ClaimNotFound()
    return Claim.from_row(row)


async def list_claims_for_user(
    db: Db,
    user_id: int,
    *,
    page: int = 1,
    per_page: int = CLAIMS_PER_PAGE_DEFAULT,
    status: ClaimStatus | None = None,
    role: ClaimRole | None = None,
) -> Page[Claim]:
    """One page of the claims where the user is the claimant (role claimant), the payer (role payer) or either (no role), newest first, optionally only those with status; raises InvalidArgument for bad paging (see validate_paging()) or an unknown status or role."""
    offset = validate_paging(page, per_page, CLAIMS_PER_PAGE_MAX)
    conditions: list[ColumnElement[bool]]
    if role is None:
        conditions = [or_(claim.c.claimant_id == user_id, claim.c.payer_id == user_id)]
    else:
        try:
            side = ClaimRole(role)
        except ValueError:
            raise InvalidArgument(f"Unknown claim role {role!r}.") from None
        conditions = [(claim.c.claimant_id if side is ClaimRole.CLAIMANT else claim.c.payer_id) == user_id]
    if status is not None:
        try:
            wanted = ClaimStatus(status)
        except ValueError:
            raise InvalidArgument(f"Unknown claim status {status!r}.") from None
        conditions.append(claim.c.status == wanted.value)
    page_stmt = _claims_with_origins().where(*conditions).order_by(claim.c.claim_id.desc()).limit(per_page).offset(offset)
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [bounded_count(select(claim.c.claim_id).where(*conditions))])
        rows = (await conn.execute(page_stmt)).mappings().all()
    return Page(items=tuple(Claim.from_row(row) for row in rows), total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)
