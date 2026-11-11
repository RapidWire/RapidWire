"""Seed helpers for integration tests: create a currency row with its issuance account, mint through the ledger, insert ledger rows under explicit IDs and times, create pools and add liquidity through the pools service, assemble and register contracts and write their storage, and read balances and supply."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.assembler import assemble
from rapidwire.contract.values import encode_value
from rapidwire.core import contracts, pools
from rapidwire.core.accounts import ensure_user_account, get_or_create_issuance_account
from rapidwire.core.clock import resolve_now
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import FEE_TIERS_BPS, balance, currency, execution, transfer

DEFAULT_CREATOR = 1000


@dataclass(frozen=True)
class SeededCurrency:
    currency_id: int
    issuance_account_id: int
    symbol: str


async def create_currency(
    engine: AsyncEngine,
    *,
    currency_id: int,
    symbol: str,
    name: str = "Coin",
    created_by: int = DEFAULT_CREATOR,
    hourly_rate_ppm: int = 0,
    now: datetime | None = None,
    **columns: Any,
) -> SeededCurrency:
    """Insert a currency row directly (no execution) with its issuance account and creator account; extra keyword arguments set other currency columns."""
    at = resolve_now(now)
    async with engine.begin() as conn:
        await ensure_user_account(conn, created_by, now=at)
        issuance = await get_or_create_issuance_account(conn, currency_id, now=at)
        await conn.execute(
            insert(currency).values(
                currency_id=currency_id,
                name=name,
                symbol=symbol,
                issuance_account_id=issuance,
                created_by=created_by,
                created_at=at,
                hourly_rate_ppm=hourly_rate_ppm,
                **columns,
            )
        )
    return SeededCurrency(currency_id=currency_id, issuance_account_id=issuance, symbol=symbol)


async def mint(engine: AsyncEngine, currency_id: int, account_id: int, amount: int, *, now: datetime | None = None) -> int:
    """Move amount from the currency's issuance account to account_id as a mint execution initiated by account_id and return the execution ID."""

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        row = (await tx.lock_currencies([currency_id]))[currency_id]
        issuance = row["issuance_account_id"]
        await tx.lock_balances([(issuance, currency_id), (account_id, currency_id)])
        transfer_id = await tx.move(execution_id, currency_id, issuance, account_id, amount, TransferKind.PRINCIPAL)
        return {"transfer_id": transfer_id}

    result = await run_operation(engine, type=ExecutionType.MINT, initiator_id=account_id, input={"currency_id": currency_id, "amount": amount}, body=body, now=now)
    return result.execution_id


async def insert_transfer(
    engine: AsyncEngine,
    *,
    execution_id: int,
    transfer_id: int,
    type: str,
    initiator: int,
    currency_id: int,
    source: int,
    destination: int,
    amount: int,
    at: datetime,
    input: Mapping[str, Any] | None = None,
) -> int:
    """Insert an execution and its one transfer under explicit IDs and time and apply the balance change, the way ledger rows that already exist elsewhere are loaded; returns the execution ID."""
    async with engine.begin() as conn:
        tx = LedgerTx(conn, at)
        await tx.require_accounts([initiator, source, destination])
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(source, currency_id), (destination, currency_id)])
        await conn.execute(
            insert(execution).values(
                execution_id=execution_id,
                type=str(type),
                initiator_id=initiator,
                status=ExecutionStatus.SUCCESS.value,
                input=dict(input) if input is not None else {"amount": str(amount)},
                output={},
                created_at=at,
            )
        )
        await conn.execute(
            insert(transfer).values(
                transfer_id=transfer_id,
                execution_id=execution_id,
                currency_id=currency_id,
                from_account=source,
                to_account=destination,
                amount=amount,
                kind=TransferKind.PRINCIPAL.value,
                created_at=at,
            )
        )
        await conn.execute(update(balance).where(balance.c.account_id == source, balance.c.currency_id == currency_id).values(amount=balance.c.amount - amount))
        await conn.execute(update(balance).where(balance.c.account_id == destination, balance.c.currency_id == currency_id).values(amount=balance.c.amount + amount))
    return execution_id


async def create_pool(
    engine: AsyncEngine,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    amount_x: int,
    amount_y: int,
    fee_bps: int = FEE_TIERS_BPS[0],
    mint_first: bool = True,
    now: datetime | None = None,
) -> pools.PoolCreated:
    """Create a pool through the pools service, first minting the deposit to user_id unless mint_first is False."""
    if mint_first:
        await mint(engine, currency_x, user_id, amount_x, now=now)
        await mint(engine, currency_y, user_id, amount_y, now=now)
    return await pools.create_pool(engine, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, amount_x=amount_x, amount_y=amount_y, now=now)


async def add_liquidity(
    engine: AsyncEngine,
    *,
    user_id: int,
    currency_x: int,
    currency_y: int,
    amount_x: int,
    amount_y: int,
    fee_bps: int = FEE_TIERS_BPS[0],
    mint_first: bool = True,
    now: datetime | None = None,
) -> pools.LiquidityAdded:
    """Add liquidity through the pools service with amount_x and amount_y as the desired amounts, first minting them to user_id unless mint_first is False."""
    if mint_first:
        await mint(engine, currency_x, user_id, amount_x, now=now)
        await mint(engine, currency_y, user_id, amount_y, now=now)
    return await pools.add_liquidity(engine, user_id=user_id, currency_x=currency_x, currency_y=currency_y, fee_bps=fee_bps, amount_x_desired=amount_x, amount_y_desired=amount_y, now=now)


PING_CONTRACT = """
.function ping public returns=int
  PUSH_INT 1
  RET
.end
"""

SHOP_CONTRACT = """
.function buy public params=int,Account returns=str
  PUSH_CONST "OK"
  RET
.end
.function helper returns=int
  PUSH_INT 7
  RET
.end
.function count public params=str returns=int
  PUSH_INT 1
  RET
.end
.function flag public params=bool,Currency returns=bool
  PUSH_TRUE
  RET
.end
"""


def contract_binary(source: str = PING_CONTRACT) -> bytes:
    """Assemble contract source text into an RWC1 binary."""
    return assemble(source)


def contract_binary_of_size(size: int) -> bytes:
    """A valid binary of exactly size bytes: the ping contract padded with str constants of at most 4000 characters each."""

    def padded(length: int) -> bytes:
        parts = []
        while length > 0:
            chunk = min(length, 4000)
            parts.append('.const "' + "x" * chunk + '"')
            length -= chunk
        return assemble("\n".join(parts) + PING_CONTRACT)

    # Each constant costs a tag byte and a length of up to 2 bytes besides its characters, so the search starts below the target and skips lengths whose new constant jumps past it.
    base = len(padded(0))
    for length in range(max(0, size - base - 3 * (size // 4000 + 1)), max(1, size - base + 1)):
        data = padded(length)
        if len(data) == size:
            return data
    raise ValueError(f"cannot build a binary of exactly {size} bytes")


async def register_contract(engine: AsyncEngine, account_id: int, binary: bytes | None = None, *, max_size: int = 8000, now: datetime | None = None) -> contracts.ContractRegistered:
    """Register binary (the ping contract by default) as account_id's contract through the contracts service."""
    return await contracts.register_contract(engine, account_id=account_id, binary=contract_binary() if binary is None else binary, max_size=max_size, now=now)


async def put_storage(engine: AsyncEngine, account_id: int, key: str, value: Any, *, now: datetime | None = None) -> contracts.StorageWrite:
    """Write one storage entry of account_id's contract through put_storage_in_tx() in its own transaction, as the contract runtime would."""
    async with engine.begin() as conn:
        tx = LedgerTx(conn, resolve_now(now))
        await contracts.lock_contract_in_tx(tx, account_id, for_update=True)
        return await contracts.put_storage_in_tx(tx, account_id, key, encode_value(value))


async def delete_storage(engine: AsyncEngine, account_id: int, key: str, *, now: datetime | None = None) -> contracts.StorageWrite:
    """Remove one storage entry of account_id's contract through delete_storage_in_tx() in its own transaction, as the contract runtime would."""
    async with engine.begin() as conn:
        tx = LedgerTx(conn, resolve_now(now))
        await contracts.lock_contract_in_tx(tx, account_id, for_update=True)
        return await contracts.delete_storage_in_tx(tx, account_id, key)


async def balance_of(engine: AsyncEngine, account_id: int, currency_id: int) -> int:
    """The balance of the account in the currency, 0 when no row exists."""
    async with engine.connect() as conn:
        value = (await conn.execute(select(balance.c.amount).where(balance.c.account_id == account_id, balance.c.currency_id == currency_id))).scalar_one_or_none()
    return 0 if value is None else int(value)


async def supply_of(engine: AsyncEngine, currency_id: int) -> int:
    """The supply of the currency: the negated balance of its issuance account."""
    async with engine.connect() as conn:
        issuance = (await conn.execute(select(currency.c.issuance_account_id).where(currency.c.currency_id == currency_id))).scalar_one()
    return -await balance_of(engine, issuance, currency_id)
