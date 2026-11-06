"""User transfers of any positive number of minimal units between user accounts: transfer() moves the sender's own funds and transfer_from() moves an owner's funds against the allowance the owner gave the spender."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.allowances import record_change
from rapidwire.core.errors import InsufficientAllowance, InvalidArgument
from rapidwire.core.ledger import LedgerTx, OperationResult, run_operation
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.core.validation import require_storable_int, require_user_account, validate_positive_amount
from rapidwire.db.tables import allowance

__all__ = [
    "TransferFromResult",
    "TransferResult",
    "transfer",
    "transfer_from",
    "transfer_from_in_tx",
    "transfer_in_tx",
]


@dataclass(frozen=True)
class TransferResult:
    """A completed transfer; replayed is True when an idempotency key returned the stored result, and it takes no part in equality."""

    execution_id: int
    transfer_id: int
    sender_id: int
    recipient_id: int
    currency_id: int
    amount: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"transfer_id": self.transfer_id, "sender_id": self.sender_id, "recipient_id": self.recipient_id, "currency_id": self.currency_id, "amount": self.amount}

    @classmethod
    def from_operation(cls, op: OperationResult) -> TransferResult:
        return cls(
            execution_id=op.execution_id,
            transfer_id=op.int_field("transfer_id"),
            sender_id=op.int_field("sender_id"),
            recipient_id=op.int_field("recipient_id"),
            currency_id=op.int_field("currency_id"),
            amount=op.int_field("amount"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class TransferFromResult:
    """A completed transfer_from; allowance_remaining is the spender's allowance after the transfer, and replayed takes no part in equality."""

    execution_id: int
    transfer_id: int
    spender_id: int
    owner_id: int
    recipient_id: int
    currency_id: int
    amount: int
    allowance_remaining: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "spender_id": self.spender_id,
            "owner_id": self.owner_id,
            "recipient_id": self.recipient_id,
            "currency_id": self.currency_id,
            "amount": self.amount,
            "allowance_remaining": self.allowance_remaining,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> TransferFromResult:
        return cls(
            execution_id=op.execution_id,
            transfer_id=op.int_field("transfer_id"),
            spender_id=op.int_field("spender_id"),
            owner_id=op.int_field("owner_id"),
            recipient_id=op.int_field("recipient_id"),
            currency_id=op.int_field("currency_id"),
            amount=op.int_field("amount"),
            allowance_remaining=op.int_field("allowance_remaining"),
            replayed=op.replayed,
        )


async def transfer_in_tx(tx: LedgerTx, execution_id: int, *, sender_id: int, recipient_id: int, currency_id: int, amount: int) -> TransferResult:
    """Move amount of the currency from sender to recipient as one principal transfer of execution_id; raises InvalidAmount, InvalidArgument, CurrencyNotFound or InsufficientBalance."""
    validate_positive_amount(amount)
    require_user_account(sender_id, "sender")
    require_user_account(recipient_id, "recipient")
    if sender_id == recipient_id:
        raise InvalidArgument("You cannot transfer to yourself.")
    await tx.lock_currencies([currency_id])
    await tx.lock_balances([(sender_id, currency_id), (recipient_id, currency_id)])
    transfer_id = await tx.move(execution_id, currency_id, sender_id, recipient_id, amount, TransferKind.PRINCIPAL)
    return TransferResult(execution_id=execution_id, transfer_id=transfer_id, sender_id=sender_id, recipient_id=recipient_id, currency_id=currency_id, amount=amount)


async def transfer(
    engine: AsyncEngine,
    *,
    sender_id: int,
    recipient_id: int,
    currency_id: int,
    amount: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> TransferResult:
    """Transfer amount from sender to recipient as a transfer execution initiated by the sender."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await transfer_in_tx(tx, execution_id, sender_id=sender_id, recipient_id=recipient_id, currency_id=currency_id, amount=amount)
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.TRANSFER,
        initiator_id=sender_id,
        input={"recipient_id": recipient_id, "currency_id": currency_id, "amount": amount},
        body=body,
        idempotency_key=idempotency_key,
        now=now,
        user_accounts=[recipient_id],
    )
    return TransferResult.from_operation(op)


async def transfer_from_in_tx(
    tx: LedgerTx,
    execution_id: int,
    *,
    spender_id: int,
    owner_id: int,
    recipient_id: int,
    currency_id: int,
    amount: int,
) -> TransferFromResult:
    """Spend amount of the owner's allowance to the spender, moving it from owner to recipient and recording the allowance change against execution_id.

    Locks the currency, then the allowance row, then both balances; raises InvalidAmount, InvalidArgument, CurrencyNotFound, InsufficientAllowance or InsufficientBalance, and a failure leaves the allowance unchanged.
    """
    validate_positive_amount(amount)
    require_user_account(spender_id, "spender")
    require_user_account(owner_id, "owner")
    require_user_account(recipient_id, "recipient")
    if spender_id == owner_id:
        raise InvalidArgument("The owner cannot spend an allowance on their own funds; use a transfer instead.")
    if owner_id == recipient_id:
        raise InvalidArgument("The owner and the recipient must differ.")
    await tx.lock_currencies([currency_id])
    key = (allowance.c.owner_id == owner_id, allowance.c.spender_id == spender_id, allowance.c.currency_id == currency_id)
    current = (await tx.conn.execute(select(allowance.c.amount).where(*key).with_for_update())).scalar_one_or_none()
    available = 0 if current is None else int(current)
    if available < amount:
        raise InsufficientAllowance()
    await tx.lock_balances([(owner_id, currency_id), (recipient_id, currency_id)])
    transfer_id = await tx.move(execution_id, currency_id, owner_id, recipient_id, amount, TransferKind.PRINCIPAL)
    remaining = available - amount
    await tx.conn.execute(update(allowance).where(*key).values(amount=remaining, updated_at=tx.now))
    await record_change(tx, execution_id, owner_id, spender_id, currency_id, available, remaining)
    return TransferFromResult(
        execution_id=execution_id,
        transfer_id=transfer_id,
        spender_id=spender_id,
        owner_id=owner_id,
        recipient_id=recipient_id,
        currency_id=currency_id,
        amount=amount,
        allowance_remaining=remaining,
    )


async def transfer_from(
    engine: AsyncEngine,
    *,
    spender_id: int,
    owner_id: int,
    recipient_id: int,
    currency_id: int,
    amount: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> TransferFromResult:
    """Move amount from owner to recipient against the owner's allowance to the spender, as a transfer_from execution initiated by the spender."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await transfer_from_in_tx(tx, execution_id, spender_id=spender_id, owner_id=owner_id, recipient_id=recipient_id, currency_id=currency_id, amount=amount)
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.TRANSFER_FROM,
        initiator_id=spender_id,
        input={"owner_id": owner_id, "recipient_id": recipient_id, "currency_id": currency_id, "amount": amount},
        body=body,
        idempotency_key=idempotency_key,
        now=now,
        user_accounts=[owner_id, recipient_id],
    )
    return TransferFromResult.from_operation(op)
