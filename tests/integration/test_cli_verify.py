from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import URL, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.cli import EXIT_INVARIANT_VIOLATION, main
from rapidwire.config import reset_settings_cache
from rapidwire.db.tables import balance, pool
from rapidwire.logging import clear_secrets
from tests.integration.factories import create_currency, create_pool, mint

NOW = datetime(2026, 6, 1, tzinfo=UTC)
ALICE = 111


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_url: URL) -> Iterator[None]:
    """Point RAPIDWIRE_DB_* at the test database, run from an empty directory and restore logging afterwards."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", str(migrated_url.host))
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", str(migrated_url.port))
    monkeypatch.setenv("RAPIDWIRE_DB_NAME", str(migrated_url.database))
    monkeypatch.setenv("RAPIDWIRE_DB_USER", str(migrated_url.username))
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", str(migrated_url.password))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    reset_settings_cache()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.captureWarnings(False)
    clear_secrets()
    reset_settings_cache()


async def test_verify_invariants_command(db: AsyncEngine, cli_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 1_500, now=NOW)

    # main() runs its own event loop, so it is called from a worker thread.
    assert await asyncio.to_thread(main, ["verify-invariants"]) == 0
    captured = capsys.readouterr()
    # stdout carries only the report; the JSON log lines go to stderr.
    assert captured.out.splitlines() == ["currency 1 ONE (active): supply 1500, balance sum 0, staked 0: ok", "1 currencies checked, 0 pools checked, 0 violations"]
    assert '"msg": "invariants verified"' in captured.err
    assert '"currencies": 1, "pools": 0, "violations": 0' in captured.err

    async with db.begin() as conn:
        await conn.execute(update(balance).where(balance.c.account_id == ALICE).values(amount=1_400))
    assert await asyncio.to_thread(main, ["verify-invariants"]) == EXIT_INVARIANT_VIOLATION
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "currency 1 ONE (active): supply 1500, balance sum -100, staked 0: VIOLATION",
        "1 currencies checked, 0 pools checked, 2 violations",
        "violation [balance_transfer_mismatch]: balance of account 111 in currency 1 is 1400 but its transfers net to 1500",
        "violation [sum_not_zero]: balances of currency 1 sum to -100",
    ]


async def test_verify_invariants_reports_pools(db: AsyncEngine, cli_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await create_currency(db, currency_id=2, symbol="TWO")
    created = await create_pool(db, user_id=ALICE, currency_x=2, currency_y=1, amount_x=9_000, amount_y=4_000, fee_bps=50, now=NOW)

    assert await asyncio.to_thread(main, ["verify-invariants"]) == 0
    captured = capsys.readouterr()
    assert '"currencies": 2, "pools": 1, "violations": 0' in captured.err
    assert captured.out.splitlines() == [
        "currency 1 ONE (active): supply 4000, balance sum 0, staked 0: ok",
        "currency 2 TWO (active): supply 9000, balance sum 0, staked 0: ok",
        f"pool {created.pool_id} 1/2 fee 50 bps (active): reserves 4000/9000, custody 4000/9000, shares 6000 (1000 locked, 5000 provider): ok",
        "2 currencies checked, 1 pools checked, 0 violations",
    ]

    async with db.begin() as conn:
        await conn.execute(update(pool).where(pool.c.pool_id == created.pool_id).values(reserve_b=9_001))
    assert await asyncio.to_thread(main, ["verify-invariants"]) == EXIT_INVARIANT_VIOLATION
    assert capsys.readouterr().out.splitlines() == [
        "currency 1 ONE (active): supply 4000, balance sum 0, staked 0: ok",
        "currency 2 TWO (active): supply 9000, balance sum 0, staked 0: VIOLATION",
        f"pool {created.pool_id} 1/2 fee 50 bps (active): reserves 4000/9001, custody 4000/9000, shares 6000 (1000 locked, 5000 provider): VIOLATION",
        "2 currencies checked, 1 pools checked, 1 violations",
        f"violation [pool_custody_reserve]: custody of pool {created.pool_id} holds 9000 of currency 2 but its reserve is 9001",
    ]
