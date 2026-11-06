"""The sample contracts of document/contracts/samples registered through the contracts service and called through call_contract on the real database, with Discord permissions and roles from InMemoryGuildInfo; every scenario keeps the ledger invariants, and rapidwire verify-invariants exits 0 afterwards."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.cli import main as rapidwire_main
from rapidwire.config import reset_settings_cache
from rapidwire.contract.compiler import compile_source
from rapidwire.contract.values import Account, Attachment, Currency
from rapidwire.core import contracts
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.errors import ContractReverted, ContractRuntimeFailure, DomainError
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import OutboxKind, TransferKind
from rapidwire.logging import clear_secrets
from tests.integration.contract_runtime_helpers import NOW, RAPID, assert_clean, config, executions, outbox_rows, setup_gas, stored, transfers_of
from tests.integration.factories import create_currency, create_pool, mint, supply_of

SAMPLES = Path(__file__).resolve().parents[2] / "document" / "contracts" / "samples"

ALICE = 7001
BOB = 7002
GUILD_OWNER = 7100
VENDING = 8101
LOTTERY = 8102
CLUB = 8103
DESK = 8104
GOLD = 900_002
VIP = 900_003
FUNDS = 10_000_000

VENDING_GUILD = 123456789012345678
VENDING_ROLE = 234567890123456789
CLUB_GUILD = 345678901234567890
CLUB_ROLE = 456789012345678901
CLUB_CHANNEL = 567890123456789012


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


class World:
    """The seeded ledger: RAPID (the gas currency), GOLD and VIP, Alice and Bob funded, the four samples registered, and the gas every committed call burned."""

    def __init__(self, engine: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
        self.engine = engine
        self.guilds = guilds
        self.gas_burned = 0

    async def call(
        self,
        contract_id: int,
        function: str,
        args: list[Any] | None = None,
        *,
        caller: int = ALICE,
        pay: tuple[int, int] | None = None,
        gas: int | None = None,
        now: datetime = NOW,
    ) -> ContractCallResult:
        """Call through call_contract and add the gas the call burned, whether it succeeded or failed as a recorded execution."""
        attach = None if pay is None else Attachment(Currency(pay[0]), pay[1])
        try:
            result = await call_contract(
                self.engine, self.guilds, caller_id=caller, contract_id=contract_id, function=function, args=args or [], attach=attach, gas_limit=gas, config=config(), now=now
            )
        except DomainError as error:
            if error.execution_id is not None:
                [row] = [row for row in await executions(self.engine) if row["execution_id"] == error.execution_id]
                self.gas_burned += int(row["output"]["gas_used"])
            raise
        self.gas_burned += result.gas_used
        return result


async def seed(engine: AsyncEngine) -> World:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(VENDING_GUILD, GUILD_OWNER)
    guilds.add_guild(CLUB_GUILD, GUILD_OWNER)
    await setup_gas(engine, {ALICE: FUNDS, BOB: FUNDS, LOTTERY: 500_000, CLUB: 10_000, DESK: 50_000})
    await create_currency(engine, currency_id=GOLD, symbol="GOLD", name="Gold", now=NOW)
    await create_currency(engine, currency_id=VIP, symbol="VIP", name="Vip", now=NOW)
    await mint(engine, GOLD, DESK, 1_000, now=NOW)
    await mint(engine, GOLD, ALICE, 50_000, now=NOW)
    await mint(engine, VIP, ALICE, 10_000, now=NOW)
    for account_id, name in ((VENDING, "role_vending"), (LOTTERY, "lottery"), (CLUB, "members_only"), (DESK, "exchange_broker")):
        path = SAMPLES / f"{name}.py"
        await contracts.register_contract(engine, account_id=account_id, binary=compile_source(path.read_text(encoding="utf-8"), filename=str(path)), max_size=8000, now=NOW)
    await allow_discord(engine, guilds, guild_id=VENDING_GUILD, account_id=VENDING, acting_user_id=GUILD_OWNER, now=NOW)
    await allow_discord(engine, guilds, guild_id=CLUB_GUILD, account_id=CLUB, acting_user_id=GUILD_OWNER, now=NOW)
    return World(engine, guilds)


async def moves(engine: AsyncEngine, execution_id: int) -> list[tuple[str, int, int, int, int]]:
    return [(row["kind"], row["currency_id"], row["from_account"], row["to_account"], int(row["amount"])) for row in await transfers_of(engine, execution_id)]


async def test_role_vending_machine(db: AsyncEngine) -> None:
    world = await seed(db)
    sold = await world.call(VENDING, "buy", pay=(RAPID, 130_000))
    assert sold.value == "OK"
    principal = [move for move in await moves(db, sold.execution_id) if move[0] == TransferKind.PRINCIPAL]
    assert principal == [(TransferKind.PRINCIPAL, RAPID, ALICE, VENDING, 130_000), (TransferKind.PRINCIPAL, RAPID, VENDING, ALICE, 30_000)]
    [row] = await outbox_rows(db)
    assert (row["kind"], row["execution_id"]) == (OutboxKind.DISCORD_ADD_ROLE, sold.execution_id)
    assert row["payload"] == {"contract_id": str(VENDING), "guild_id": str(VENDING_GUILD), "user_id": str(ALICE), "role_id": str(VENDING_ROLE)}
    world.guilds.add_role(VENDING_GUILD, BOB, VENDING_ROLE)
    with pytest.raises(ContractReverted, match="You already have the role"):
        await world.call(VENDING, "buy", caller=BOB, pay=(RAPID, 100_000))
    with pytest.raises(ContractReverted, match="Send at least 100 RAPID"):
        await world.call(VENDING, "buy", pay=(RAPID, 99_999))
    assert len(await outbox_rows(db)) == 1
    assert (await world.call(VENDING, "sold")).value == 1
    assert await stored(db, VENDING, "sold") == 1
    await assert_clean(db)


async def test_lottery(db: AsyncEngine) -> None:
    world = await seed(db)
    for guess in (1, 2, 3, 4, 5, 6):
        played = await world.call(LOTTERY, "bet", [guess], pay=(RAPID, 10_000))
        [roll] = played.random_values
        assert 1 <= roll <= 6
        won = roll == guess
        assert played.value == (f"rolled {roll}: you win 50000" if won else f"rolled {roll}: you lose")
        payouts = [move for move in await moves(db, played.execution_id) if move[0] == TransferKind.PRINCIPAL and move[2] == LOTTERY]
        assert payouts == ([(TransferKind.PRINCIPAL, RAPID, LOTTERY, ALICE, 50_000)] if won else [])
    stats = (await world.call(LOTTERY, "stats")).value
    assert isinstance(stats, str) and stats.startswith("6 games, ")
    with pytest.raises(ContractReverted, match="Guess a number from 1 to 6"):
        await world.call(LOTTERY, "bet", [9], pay=(RAPID, 10_000))
    await assert_clean(db)


async def test_members_only_club(db: AsyncEngine) -> None:
    world = await seed(db)
    world.guilds.add_role(CLUB_GUILD, BOB, CLUB_ROLE)
    collected = await world.call(CLUB, "collect")
    assert collected.value == 2_000
    assert [move for move in await moves(db, collected.execution_id) if move[0] == TransferKind.PRINCIPAL] == [(TransferKind.PRINCIPAL, RAPID, CLUB, ALICE, 2_000)]
    assert (await world.call(CLUB, "collect", caller=BOB)).value == 2_000
    with pytest.raises(ContractReverted, match="Already collected today"):
        await world.call(CLUB, "collect")
    world.guilds.remove_role(CLUB_GUILD, BOB, CLUB_ROLE)
    with pytest.raises(ContractReverted, match="Members only"):
        await world.call(CLUB, "post", ["hi"], caller=BOB)
    posted = await world.call(CLUB, "post", ["hello club"])
    [row] = await outbox_rows(db)
    assert (row["kind"], row["execution_id"]) == (OutboxKind.DISCORD_SEND, posted.execution_id)
    assert row["payload"] == {"contract_id": str(CLUB), "guild_id": str(CLUB_GUILD), "channel_id": str(CLUB_CHANNEL), "message": f"<@{ALICE}> hello club"}
    assert await stored(db, CLUB, "collected") == 4_000
    assert (await world.call(CLUB, "member", [Account(BOB)])).value is False
    await assert_clean(db)


async def test_exchange_broker(db: AsyncEngine) -> None:
    world = await seed(db)
    await create_pool(db, user_id=BOB, currency_x=RAPID, currency_y=GOLD, amount_x=2_000_000, amount_y=1_000_000, fee_bps=50, now=NOW)
    from_stock = await world.call(DESK, "buy", pay=(RAPID, 900))
    assert from_stock.value == 300
    assert [move[0:2] + move[2:4] for move in await moves(db, from_stock.execution_id) if move[0] == TransferKind.PRINCIPAL] == [
        (TransferKind.PRINCIPAL, RAPID, ALICE, DESK),
        (TransferKind.PRINCIPAL, GOLD, DESK, ALICE),
    ]
    via_dex = await world.call(DESK, "buy", pay=(RAPID, 30_000))
    assert via_dex.value == 10_000
    swapped = [move for move in await moves(db, via_dex.execution_id) if move[0] == TransferKind.PRINCIPAL and move[1] == GOLD and move[3] == DESK]
    assert len(swapped) == 1 and swapped[0][4] >= 10_000
    sold = await world.call(DESK, "sell", pay=(GOLD, 4_000))
    assert sold.value == 10_000
    with pytest.raises(ContractReverted, match="out of RAPID"):
        await world.call(DESK, "sell", pay=(GOLD, 40_000))
    with pytest.raises(ContractRuntimeFailure) as info:
        await world.call(DESK, "buy", pay=(RAPID, 9_000_000))
    [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["host_code"] == "slippage_exceeded"
    await assert_clean(db)


async def test_samples_burn_exactly_their_gas_and_pass_verify_invariants(db: AsyncEngine, cli_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    world = await seed(db)
    supply = await supply_of(db, RAPID)
    world.guilds.add_role(CLUB_GUILD, BOB, CLUB_ROLE)
    await world.call(VENDING, "buy", pay=(RAPID, 100_000))
    await world.call(LOTTERY, "bet", [3], pay=(RAPID, 1_000))
    await world.call(CLUB, "collect", caller=BOB)
    await world.call(DESK, "buy", pay=(RAPID, 300))
    with pytest.raises(ContractReverted):
        await world.call(DESK, "buy", pay=(GOLD, 300))
    assert world.gas_burned > 0
    assert await supply_of(db, RAPID) == supply - world.gas_burned
    await assert_clean(db)
    # main() runs its own event loop, so it is called from a worker thread.
    assert await asyncio.to_thread(rapidwire_main, ["verify-invariants"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].endswith(" 0 violations")
