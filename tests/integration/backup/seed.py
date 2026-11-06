"""A ledger with every kind of row a backup has to carry: currencies, transfers and a failed execution, an allowance, stakes, two pools with providers, a contract with binary code and storage, a claim with a non-ASCII description and its outbox notification, an outbox row whose payload is a JSON null document, an API token hash ending in zero bytes, and a job lock."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import allowances, claims, joblock, notifications, staking, tokens, transfers
from rapidwire.core.errors import DomainError
from rapidwire.db.tables import api_token
from tests.integration import factories

NOW = datetime(2026, 10, 31, 2, 0, 0, 123456, tzinfo=UTC)
RAPID = 111
OTHER = 222
THIRD = 333
ALICE = 1001
BOB = 1002
CAROL = 1003
DAVE = 1004
CONTRACT = 1005
CLAIM_DESCRIPTION = '\U00005bb6\U00008cc3 \U0001f3e0 "quoted"'
# A BINARY(32) value whose trailing zero bytes are data, not padding to strip.
TOKEN_HASH = bytes(range(1, 29)) + b"\x00\x00\x00\x00"


async def seed_ledger(engine: AsyncEngine) -> None:
    await factories.create_currency(engine, currency_id=RAPID, symbol="RAPID", name="Rapid", hourly_rate_ppm=100, now=NOW)
    await factories.create_currency(engine, currency_id=OTHER, symbol="OTH", name="Other", now=NOW)
    await factories.create_currency(engine, currency_id=THIRD, symbol="THR", name="Third", now=NOW)
    await factories.mint(engine, RAPID, ALICE, 10**37, now=NOW)
    await factories.mint(engine, OTHER, ALICE, 5_000_000_000, now=NOW)
    await transfers.transfer(engine, sender_id=ALICE, recipient_id=BOB, currency_id=RAPID, amount=123_456_789, now=NOW)
    with contextlib.suppress(DomainError):
        await transfers.transfer(engine, sender_id=BOB, recipient_id=ALICE, currency_id=OTHER, amount=1, now=NOW)
    await allowances.approve(engine, owner_id=ALICE, spender_id=CAROL, currency_id=RAPID, amount=77, now=NOW)
    await staking.deposit(engine, account_id=BOB, currency_id=RAPID, amount=1_000_000, now=NOW)
    await staking.deposit(engine, account_id=ALICE, currency_id=RAPID, amount=5, now=NOW + timedelta(hours=3))
    await factories.create_pool(engine, user_id=CAROL, currency_x=RAPID, currency_y=OTHER, amount_x=10**12, amount_y=2 * 10**12, now=NOW)
    await factories.add_liquidity(engine, user_id=DAVE, currency_x=RAPID, currency_y=OTHER, amount_x=10**9, amount_y=2 * 10**9, now=NOW)
    await factories.create_pool(engine, user_id=CAROL, currency_x=OTHER, currency_y=THIRD, amount_x=10**6, amount_y=10**6, fee_bps=10, now=NOW)
    await factories.register_contract(engine, CONTRACT, now=NOW)
    await factories.put_storage(engine, CONTRACT, "greeting", "\U00003053\U00003093\U0000306b\U00003061\U0000306f \U0001f30f", now=NOW)
    await factories.put_storage(engine, CONTRACT, "counts", {"a": 1, "b": 2}, now=NOW)
    await notifications.allow_notifications(engine, user_id=ALICE, allowed_user_id=BOB, now=NOW)
    await claims.create_claim(engine, claimant_id=BOB, payer_id=ALICE, currency_id=RAPID, amount=500, description=CLAIM_DESCRIPTION, now=NOW)
    await tokens.issue_token(engine, account_id=ALICE, now=NOW)
    async with engine.begin() as conn:
        await conn.execute(update(api_token).where(api_token.c.account_id == ALICE).values(token_hash=TOKEN_HASH))
        await conn.execute(
            text("INSERT INTO outbox (kind, payload, status, attempts, available_at, created_at) VALUES ('discord_send', CAST('null' AS JSON), 'pending', 0, :at, :at)"),
            {"at": NOW.replace(tzinfo=None)},
        )
    await joblock.try_acquire(engine, "seed-job", "seed-holder", timedelta(hours=1), now=NOW)
