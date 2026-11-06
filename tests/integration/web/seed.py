"""Seed data for the web explorer checks: currencies (one with the largest supply), transfers (one of nearly the largest amount), a stake, two fee tiers of one pool and a swap, a claim, a contract with storage and calls, failed executions, a legacy-origin transfer and claim, tokens and a backup file, all written through the services except the legacy rows."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.compiler import compile_source
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.core import accounts, claims, contract_calls, contracts, currency, pools, staking, swap, tokens, transfers
from rapidwire.core.errors import DomainError
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ClaimStatus, ExecutionStatus, ExecutionType, LabelBasis, Origin, TransferKind
from rapidwire.db.tables import FEE_TIERS_BPS, balance, execution, transfer
from rapidwire.db.tables import claim as claim_table
from rapidwire.db.types import AMOUNT_MAX

ALICE = 1_000_001
BOB = 1_000_002
CAROL = 1_000_003
CONTRACT = 1_000_004
OPERATOR = 900_001
GOLD_GUILD = 111_111_111_111_111_111
SILVER_GUILD = 222_222_222_222_222_222
HUGE_GUILD = 333_333_333_333_333_333
# The largest transfer of the seed, one minimal unit below AMOUNT_MAX, so the pages must fit a 38-digit amount.
HUGE_AMOUNT = AMOUNT_MAX - 1

# Text that would become elements if a page inserted it as HTML; attribute values are unquoted so the JSON string literals the pages show contain it unchanged.
CLAIM_MARKUP = "<b id=injected-claim>x</b><img id=injected-img>"
STORAGE_MARKUP = "<b id=injected-storage>storage</b><script id=injected-script>window.injected=1</script>"
INJECTED_IDS = ("injected-claim", "injected-img", "injected-storage", "injected-script")

CONTRACT_SOURCE = """from rapidwire_contract import *


@public
def note(text: str) -> str:
    storage["note"] = text
    roll = random(1, 6)
    storage["roll"] = roll
    return text


@public
def fail() -> int:
    revert("always fails")
    return 0
"""

BACKUP_NAME = "rapidwire-20260101T000000Z.sql.gz"


@dataclass(frozen=True)
class Seed:
    """The IDs and secrets the page checks look up."""

    bob: int
    contract: int
    gold: int
    gold_symbol: str
    huge: int
    huge_issuance: int
    huge_transfer: int
    transfer_execution: int
    transfer_id: int
    stake_execution: int
    pool_high: int
    pool_low: int
    swap_execution: int
    claim_execution: int
    contract_call_execution: int
    contract_failed_execution: int
    failed_transfer_execution: int
    legacy_execution: int
    legacy_transfer: int
    legacy_claim: int
    legacy_claim_execution: int
    operator_token: str
    other_token: str
    backup_name: str
    backup_sha256: str
    backup_size: int


async def _failed_execution(call: Any) -> int:
    try:
        await call
    except DomainError as error:
        assert error.execution_id is not None
        return error.execution_id
    raise AssertionError("the operation was expected to fail")


async def _insert_legacy_transfer(engine: AsyncEngine, *, currency_id: int, sender: int, recipient: int, amount: int, at: datetime) -> tuple[int, int]:
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(execution).values(
                type=ExecutionType.TRANSFER,
                origin=Origin.LEGACY,
                label_basis=LabelBasis.ROW_SHAPE,
                initiator_id=sender,
                status=ExecutionStatus.SUCCESS,
                input={"recipient_id": str(recipient), "currency_id": str(currency_id), "amount": str(amount)},
                output={},
                created_at=at,
            )
        )
        execution_id = int(result.lastrowid)
        moved = await conn.execute(
            insert(transfer).values(execution_id=execution_id, currency_id=currency_id, from_account=sender, to_account=recipient, amount=amount, kind=TransferKind.PRINCIPAL, created_at=at)
        )
        await conn.execute(update(balance).where(balance.c.account_id == sender, balance.c.currency_id == currency_id).values(amount=balance.c.amount - amount))
        await conn.execute(update(balance).where(balance.c.account_id == recipient, balance.c.currency_id == currency_id).values(amount=balance.c.amount + amount))
    return execution_id, int(moved.lastrowid)


async def _insert_legacy_claim(engine: AsyncEngine, *, claimant: int, payer: int, currency_id: int, amount: int, at: datetime) -> tuple[int, int]:
    """A pending claim imported from the old version, created by a legacy claim_create execution."""
    async with engine.begin() as conn:
        created = await conn.execute(
            insert(execution).values(
                type=ExecutionType.CLAIM_CREATE,
                origin=Origin.LEGACY,
                label_basis=LabelBasis.STATE_IMPORT,
                initiator_id=claimant,
                status=ExecutionStatus.SUCCESS,
                input={},
                output={},
                created_at=at,
            )
        )
        execution_id = int(created.lastrowid)
        inserted = await conn.execute(
            insert(claim_table).values(
                claimant_id=claimant,
                payer_id=payer,
                currency_id=currency_id,
                amount=amount,
                description=None,
                status=ClaimStatus.PENDING.value,
                created_at=at,
                updated_at=at,
                created_execution_id=execution_id,
            )
        )
    return execution_id, int(inserted.lastrowid)


def write_backup(backup_dir: Path) -> tuple[str, int]:
    """Write BACKUP_NAME with random content into backup_dir and return its SHA-256 and size."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    data = os.urandom(150_000)
    (backup_dir / BACKUP_NAME).write_bytes(data)
    return hashlib.sha256(data).hexdigest(), len(data)


async def seed_explorer(engine: AsyncEngine, backup_dir: Path, *, runtime: RuntimeConfig) -> Seed:
    """Write the explorer seed into an empty migrated database and one backup file into backup_dir; the gas currency is runtime.gas_currency_id."""
    guilds = InMemoryGuildInfo()
    rapid_id = runtime.gas_currency_id
    for guild_id in (rapid_id, GOLD_GUILD, SILVER_GUILD, HUGE_GUILD):
        guilds.add_guild(guild_id, ALICE)
    await currency.create_currency(engine, guilds, guild_id=rapid_id, user_id=ALICE, name="Rapid", symbol="RAPID", initial_supply=10_000_000_000)
    await currency.create_currency(engine, guilds, guild_id=GOLD_GUILD, user_id=ALICE, name="Gold", symbol="GOLD", initial_supply=50_000_000_000, hourly_rate_ppm=100)
    await currency.create_currency(engine, guilds, guild_id=SILVER_GUILD, user_id=ALICE, name="Silver", symbol="SILV", initial_supply=50_000_000_000)
    await currency.mint(engine, guilds, currency_id=GOLD_GUILD, user_id=ALICE, amount=1_000_000)

    transfer_amount = 1_234_567
    sent = await transfers.transfer(engine, sender_id=ALICE, recipient_id=BOB, currency_id=GOLD_GUILD, amount=transfer_amount)
    await transfers.transfer(engine, sender_id=ALICE, recipient_id=BOB, currency_id=SILVER_GUILD, amount=500_000)
    await transfers.transfer(engine, sender_id=ALICE, recipient_id=BOB, currency_id=rapid_id, amount=100_000_000)
    stake = await staking.deposit(engine, account_id=BOB, currency_id=GOLD_GUILD, amount=200_000)

    high = await pools.create_pool(engine, user_id=ALICE, currency_x=GOLD_GUILD, currency_y=SILVER_GUILD, fee_bps=FEE_TIERS_BPS[0], amount_x=10_000_000, amount_y=20_000_000)
    low = await pools.create_pool(engine, user_id=ALICE, currency_x=GOLD_GUILD, currency_y=SILVER_GUILD, fee_bps=FEE_TIERS_BPS[1], amount_x=5_000_000, amount_y=10_000_000)
    swapped = await swap.swap(engine, user_id=BOB, from_currency=GOLD_GUILD, to_currency=SILVER_GUILD, amount_in=50_000, min_out=0, config=swap.SwapConfig(chunk_count=100, max_routes=32))

    claim = await claims.create_claim(engine, claimant_id=CAROL, payer_id=BOB, currency_id=GOLD_GUILD, amount=5_000, description=CLAIM_MARKUP)

    await contracts.register_contract(engine, account_id=CONTRACT, binary=compile_source(CONTRACT_SOURCE), max_size=8000)
    called = await contract_calls.call_contract(engine, guilds, caller_id=BOB, contract_id=CONTRACT, function="note", args=[STORAGE_MARKUP], gas_limit=10_000, config=runtime)
    call_failed = await _failed_execution(contract_calls.call_contract(engine, guilds, caller_id=BOB, contract_id=CONTRACT, function="fail", gas_limit=10_000, config=runtime))
    transfer_failed = await _failed_execution(transfers.transfer(engine, sender_id=CAROL, recipient_id=BOB, currency_id=GOLD_GUILD, amount=10**15))

    legacy_amount = 7_000
    legacy_execution, legacy_transfer = await _insert_legacy_transfer(engine, currency_id=GOLD_GUILD, sender=ALICE, recipient=BOB, amount=legacy_amount, at=datetime(2025, 6, 1, 12, 0, tzinfo=UTC))
    legacy_claim_execution, legacy_claim = await _insert_legacy_claim(engine, claimant=ALICE, payer=BOB, currency_id=GOLD_GUILD, amount=3_000, at=datetime(2025, 6, 2, 12, 0, tzinfo=UTC))

    await currency.create_currency(engine, guilds, guild_id=HUGE_GUILD, user_id=ALICE, name="Huge", symbol="HUGE", initial_supply=AMOUNT_MAX)
    huge_sent = await transfers.transfer(engine, sender_id=ALICE, recipient_id=BOB, currency_id=HUGE_GUILD, amount=HUGE_AMOUNT)
    async with engine.connect() as conn:
        huge_issuance = await accounts.issuance_account_id(conn, HUGE_GUILD)
    assert huge_issuance is not None

    operator_token = await tokens.issue_token(engine, account_id=OPERATOR)
    other_token = await tokens.issue_token(engine, account_id=BOB)

    backup_sha256, backup_size = write_backup(backup_dir)

    return Seed(
        bob=BOB,
        contract=CONTRACT,
        gold=GOLD_GUILD,
        gold_symbol="GOLD",
        huge=HUGE_GUILD,
        huge_issuance=huge_issuance,
        huge_transfer=huge_sent.transfer_id,
        transfer_execution=sent.execution_id,
        transfer_id=sent.transfer_id,
        stake_execution=stake.execution_id,
        pool_high=high.pool_id,
        pool_low=low.pool_id,
        swap_execution=swapped.execution_id,
        claim_execution=claim.execution_id,
        contract_call_execution=called.execution_id,
        contract_failed_execution=call_failed,
        failed_transfer_execution=transfer_failed,
        legacy_execution=legacy_execution,
        legacy_transfer=legacy_transfer,
        legacy_claim=legacy_claim,
        legacy_claim_execution=legacy_claim_execution,
        operator_token=operator_token.token,
        other_token=other_token.token,
        backup_name=BACKUP_NAME,
        backup_sha256=backup_sha256,
        backup_size=backup_size,
    )
