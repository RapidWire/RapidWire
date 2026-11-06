"""The sample contracts of document/contracts/samples: each compiles, verifies, disassembles, type-checks against the stub and behaves as its documentation says on the mock ledger, and the documented rwc invocations work."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from mypy import api as mypy_api

from rapidwire.contract.assembler import disassemble
from rapidwire.contract.cli import main, stub_text
from rapidwire.contract.compiler import STUB_MODULE, compile_source
from rapidwire.contract.mock import DEFAULT_GAS, DEFAULT_MAX_SIZE, GAS_CURRENCY_ID, MockLedger, RunResult, TransferRecord
from rapidwire.contract.values import Account, Attachment, Currency, Scalar
from rapidwire.contract.verifier import VerifiedProgram, verify

SAMPLES = Path(__file__).resolve().parents[3] / "document" / "contracts" / "samples"
NAMES = ("role_vending", "lottery", "members_only", "exchange_broker")

DESK = 1000
ALICE = 2000
BOB = 2001
NOW = 1_790_000_000
DAY = 86_400
RAPID = Currency(GAS_CURRENCY_ID)
GOLD = Currency(456789012345678901)
VIP = Currency(345678901234567890)

VENDING_GUILD = 123456789012345678
VENDING_ROLE = 234567890123456789
CLUB_GUILD = 345678901234567890
CLUB_ROLE = 456789012345678901
CLUB_CHANNEL = 567890123456789012

# The role vending example of the language specification, character for character, without the stub import.
SPEC_VENDING_EXAMPLE = """PRICE = 100_000  # 100.000 in minimal units
GUILD = 123456789012345678
ROLE = 234567890123456789

@public
def buy() -> str:
    if paid.currency != currency("RAPID") or paid.amount < PRICE:
        revert("Send at least 100 RAPID")
    if paid.amount > PRICE:
        transfer(sender, paid.currency, paid.amount - PRICE)
    discord.add_role(GUILD, sender, ROLE)
    return "OK"
"""


class FixedRandomLedger(MockLedger):
    """A mock ledger whose random() draws are the given offsets from low, in order."""

    def __init__(self, draws: Sequence[int]) -> None:
        super().__init__()
        self.draws = list(draws)

    def random_below(self, n: int) -> int:
        drawn = self.draws.pop(0)
        assert 0 <= drawn < n
        return drawn


def sample_source(name: str) -> str:
    return (SAMPLES / f"{name}.py").read_text(encoding="utf-8")


def sample_program(name: str) -> VerifiedProgram:
    return verify(compile_source(sample_source(name), filename=f"{name}.py"), max_size=DEFAULT_MAX_SIZE)


def world(name: str, ledger: MockLedger | None = None) -> MockLedger:
    """A ledger with RAPID, GOLD and VIP, Alice and Bob funded with RAPID, and the sample deployed on DESK."""
    mock = ledger if ledger is not None else MockLedger(seed=1)
    mock.add_currency(RAPID.id, "RAPID")
    mock.add_currency(GOLD.id, "GOLD")
    mock.add_currency(VIP.id, "VIP")
    mock.set_balance(ALICE, RAPID, 1_000_000)
    mock.set_balance(BOB, RAPID, 1_000_000)
    mock.deploy(DESK, sample_program(name))
    return mock


async def call(mock: MockLedger, function: str, args: Sequence[Scalar] = (), *, sender: int = ALICE, pay: tuple[Currency, int] | None = None, now: int = NOW) -> RunResult:
    attach = None if pay is None else Attachment(*pay)
    return await mock.run(DESK, function, args, sender=sender, attach=attach, gas=DEFAULT_GAS, now=now)


def succeeded(result: RunResult) -> Any:
    assert result.ok, f"{result.error}: {result.message}"
    assert result.gas_used <= DEFAULT_GAS
    return result.value


def moves(result: RunResult) -> list[TransferRecord]:
    """The run's transfers apart from the gas fee, after checking that the fee is the one burned transfer."""
    fees = [t for t in result.transfers if t.kind == "gas"]
    assert [t.amount for t in fees] == ([result.gas_fee] if result.gas_fee else [])
    return [t for t in result.transfers if t.kind != "gas"]


def reverted(result: RunResult, message: str) -> None:
    assert (result.ok, result.error, result.message) == (False, "REVERTED", message)
    assert moves(result) == [] and result.outbox == ()


@pytest.mark.parametrize("name", NAMES)
def test_sample_compiles_verifies_and_disassembles(name: str) -> None:
    program = sample_program(name)
    assert len(program.data) <= DEFAULT_MAX_SIZE
    listing = disassemble(program)
    for fn in program.public_functions:
        assert f".function {fn.name} public" in listing


def test_samples_type_check_against_the_stub(tmp_path: Path) -> None:
    (tmp_path / f"{STUB_MODULE}.pyi").write_text(stub_text(), encoding="utf-8")
    paths = []
    for name in NAMES:
        path = tmp_path / f"{name}.py"
        path.write_text(sample_source(name), encoding="utf-8")
        paths.append(str(path))
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\n", encoding="utf-8")
    stdout, stderr, status = mypy_api.run(["--strict", "--no-incremental", "--cache-dir", os.devnull, "--config-file", str(config), "--no-site-packages", *paths])
    assert status == 0, stdout + stderr


async def test_the_specification_example_compiles_and_runs_verbatim() -> None:
    mock = MockLedger(seed=1)
    mock.add_currency(RAPID.id, "RAPID")
    mock.set_balance(ALICE, RAPID, 1_000_000)
    mock.allow_discord(VENDING_GUILD, DESK)
    mock.deploy(DESK, verify(compile_source(SPEC_VENDING_EXAMPLE), max_size=DEFAULT_MAX_SIZE))
    result = await call(mock, "buy", pay=(RAPID, 150_000))
    assert succeeded(result) == "OK"
    assert [(t.kind, t.from_id, t.to_id, t.amount) for t in moves(result)] == [("attachment", ALICE, DESK, 150_000), ("transfer", DESK, ALICE, 50_000)]


async def test_role_vending_sells_the_role() -> None:
    mock = world("role_vending")
    mock.allow_discord(VENDING_GUILD, DESK)
    exact = await call(mock, "buy", pay=(RAPID, 100_000))
    assert succeeded(exact) == "OK"
    assert [(e.kind, e.guild, e.user, e.role) for e in exact.outbox] == [("discord_add_role", VENDING_GUILD, ALICE, VENDING_ROLE)]
    over = await call(mock, "buy", sender=BOB, pay=(RAPID, 130_000))
    assert succeeded(over) == "OK"
    assert [(t.kind, t.to_id, t.amount) for t in moves(over)] == [("attachment", DESK, 130_000), ("transfer", BOB, 30_000)]
    assert mock.balance_of(DESK, RAPID) == 200_000
    assert succeeded(await call(mock, "sold")) == 2
    assert succeeded(await call(mock, "price")) == 100_000


async def test_role_vending_refusals() -> None:
    mock = world("role_vending")
    mock.allow_discord(VENDING_GUILD, DESK)
    mock.set_balance(ALICE, GOLD, 500_000)
    reverted(await call(mock, "buy", pay=(RAPID, 99_999)), "Send at least 100 RAPID")
    reverted(await call(mock, "buy", pay=(GOLD, 100_000)), "Send at least 100 RAPID")
    reverted(await call(mock, "buy"), "Send at least 100 RAPID")
    mock.grant_role(VENDING_GUILD, BOB, VENDING_ROLE)
    refused = await call(mock, "buy", sender=BOB, pay=(RAPID, 100_000))
    reverted(refused, "You already have the role")
    assert mock.balance_of(BOB, RAPID) == 1_000_000 - refused.gas_fee
    assert succeeded(await call(mock, "sold")) == 0


async def test_role_vending_needs_the_discord_permission() -> None:
    mock = world("role_vending")
    result = await call(mock, "buy", pay=(RAPID, 100_000))
    assert (result.ok, result.error) == (False, "RUNTIME")
    assert "permission_denied" in result.message
    assert moves(result) == []
    assert mock.balance_of(ALICE, RAPID) == 1_000_000 - result.gas_fee


async def test_lottery_pays_a_winner_and_keeps_a_losers_stake() -> None:
    mock = world("lottery", FixedRandomLedger([3, 0]))
    mock.set_balance(DESK, RAPID, 100_000)
    win = await call(mock, "bet", [4], pay=(RAPID, 10_000))
    assert succeeded(win) == "rolled 4: you win 50000"
    assert win.random_values == (4,)
    assert [(t.kind, t.to_id, t.amount) for t in moves(win)] == [("attachment", DESK, 10_000), ("transfer", ALICE, 50_000)]
    lose = await call(mock, "bet", [6], sender=BOB, pay=(RAPID, 10_000))
    assert succeeded(lose) == "rolled 1: you lose"
    assert mock.balance_of(DESK, RAPID) == 100_000 + 10_000 - 50_000 + 10_000
    assert succeeded(await call(mock, "stats")) == "2 games, 1 wins, 50000 paid out"


async def test_lottery_refusals() -> None:
    mock = world("lottery", FixedRandomLedger([]))
    mock.set_balance(ALICE, GOLD, 100_000)
    reverted(await call(mock, "bet", [3], pay=(RAPID, 10_000)), "The bank cannot cover this bet right now")
    mock.set_balance(DESK, RAPID, 1_000_000)
    reverted(await call(mock, "bet", [3], pay=(GOLD, 10_000)), "Bets are placed in RAPID")
    reverted(await call(mock, "bet", [3], pay=(RAPID, 999)), "Bet between 1000 and 50000 minimal units")
    reverted(await call(mock, "bet", [3], pay=(RAPID, 50_001)), "Bet between 1000 and 50000 minimal units")
    reverted(await call(mock, "bet", [0], pay=(RAPID, 10_000)), "Guess a number from 1 to 6")
    reverted(await call(mock, "bet", [7], pay=(RAPID, 10_000)), "Guess a number from 1 to 6")
    assert succeeded(await call(mock, "stats")) == "0 games, 0 wins, 0 paid out"


async def test_members_collect_once_a_day() -> None:
    mock = world("members_only")
    mock.set_balance(DESK, RAPID, 5_000)
    mock.set_balance(ALICE, VIP, 10_000)
    mock.grant_role(CLUB_GUILD, BOB, CLUB_ROLE)
    mock.allow_discord(CLUB_GUILD, DESK)
    holder = await call(mock, "collect")
    assert succeeded(holder) == 2_000
    assert [(t.kind, t.from_id, t.to_id, t.amount) for t in moves(holder)] == [("transfer", DESK, ALICE, 2_000)]
    assert succeeded(await call(mock, "collect", sender=BOB)) == 2_000
    reverted(await call(mock, "collect"), "Already collected today")
    reverted(await call(mock, "collect", now=NOW + DAY), "The reward pool is empty")
    mock.set_balance(DESK, RAPID, 5_000)
    assert succeeded(await call(mock, "collect", now=NOW + DAY)) == 2_000
    assert mock.storage_value(DESK, f"day:{ALICE}") == (NOW + DAY) // DAY
    assert mock.storage_value(DESK, "collected") == 6_000


async def test_non_members_are_turned_away() -> None:
    mock = world("members_only")
    mock.set_balance(DESK, RAPID, 5_000)
    mock.set_balance(ALICE, VIP, 9_999)
    mock.allow_discord(CLUB_GUILD, DESK)
    reverted(await call(mock, "collect"), "Members only: hold 10 VIP or the member role")
    reverted(await call(mock, "post", ["hello"]), "Members only: hold 10 VIP or the member role")
    assert succeeded(await call(mock, "member", [Account(ALICE)])) is False
    mock.set_balance(ALICE, VIP, 10_000)
    assert succeeded(await call(mock, "member", [Account(ALICE)])) is True


async def test_members_post_to_the_club_channel() -> None:
    mock = world("members_only")
    mock.grant_role(CLUB_GUILD, ALICE, CLUB_ROLE)
    mock.allow_discord(CLUB_GUILD, DESK)
    posted = await call(mock, "post", ["  hello club  "])
    assert succeeded(posted) == "posted"
    assert [(e.kind, e.guild, e.channel, e.message) for e in posted.outbox] == [("discord_send", CLUB_GUILD, CLUB_CHANNEL, f"<@{ALICE}> hello club")]
    reverted(await call(mock, "post", ["   "]), "A post has 1 to 200 characters")
    reverted(await call(mock, "post", ["x" * 201]), "A post has 1 to 200 characters")


async def test_exchange_sells_from_inventory_and_buys_back() -> None:
    mock = world("exchange_broker")
    mock.set_balance(DESK, GOLD, 50_000)
    mock.set_balance(DESK, RAPID, 100_000)
    bought = await call(mock, "buy", pay=(RAPID, 30_001))
    assert succeeded(bought) == 10_000
    assert [(t.kind, t.from_id, t.to_id, t.currency_id, t.amount) for t in moves(bought)] == [("attachment", ALICE, DESK, RAPID.id, 30_001), ("transfer", DESK, ALICE, GOLD.id, 10_000)]
    sold = await call(mock, "sell", pay=(GOLD, 4_001))
    assert succeeded(sold) == 10_002
    assert mock.balance_of(ALICE, GOLD) == 5_999
    assert succeeded(await call(mock, "quote_buy", [9])) == 3
    assert succeeded(await call(mock, "quote_sell", [3])) == 7
    assert succeeded(await call(mock, "inventory")) == f"{40_000 + 4_001} GOLD, {100_000 + 30_001 - 10_002} RAPID"


async def test_exchange_fills_a_short_inventory_on_the_dex() -> None:
    mock = world("exchange_broker")
    mock.set_balance(DESK, GOLD, 1_000)
    mock.add_pool(RAPID, GOLD, 50, 2_000_000, 1_000_000)
    bought = await call(mock, "buy", pay=(RAPID, 30_000))
    assert succeeded(bought) == 10_000
    principal = moves(bought)
    assert [t.kind for t in principal] == ["attachment", "swap", "swap", "transfer"]
    received = principal[2].amount
    assert received >= 10_000
    assert mock.balance_of(DESK, GOLD) == 1_000 + received - 10_000
    assert mock.balance_of(ALICE, GOLD) == 10_000


async def test_exchange_refusals() -> None:
    mock = world("exchange_broker")
    mock.set_balance(ALICE, GOLD, 100_000)
    mock.add_pool(RAPID, GOLD, 50, 10_000_000, 1_000_000)
    slipped = await call(mock, "buy", pay=(RAPID, 30_000))
    assert (slipped.ok, slipped.error) == (False, "RUNTIME")
    assert "slippage_exceeded" in slipped.message
    assert moves(slipped) == []
    assert mock.balance_of(ALICE, RAPID) == 1_000_000 - slipped.gas_fee
    reverted(await call(mock, "buy", pay=(GOLD, 30_000)), "Pay in RAPID to buy GOLD")
    reverted(await call(mock, "buy", pay=(RAPID, 2)), "Pay at least 3 RAPID minimal units")
    reverted(await call(mock, "sell", pay=(RAPID, 10)), "Pay in GOLD to sell it")
    reverted(await call(mock, "sell", pay=(GOLD, 10)), "The desk is out of RAPID; try a smaller amount")


# The rwc run invocations the samples' documentation shows, with their expected results.
CLI_RUNS: dict[str, tuple[list[str], Any]] = {
    "role_vending": (["--function", "buy", "--pay", "RAPID:120000"], "OK"),
    "lottery": (["--function", "bet", "--arg", "int:4", "--pay", "RAPID:10000", "--seed", "3"], "rolled 2: you lose"),
    "members_only": (["--function", "collect"], 2000),
    "exchange_broker": (["--function", "buy", "--pay", "RAPID:30000"], 10000),
}


@pytest.mark.parametrize("name", NAMES)
def test_rwc_compile_verify_disasm_and_run(name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary = tmp_path / f"{name}.rwc"
    assert main(["compile", str(SAMPLES / f"{name}.py"), "-o", str(binary)]) == 0
    assert f"wrote {binary}" in capsys.readouterr().out
    assert binary.read_bytes() == compile_source(sample_source(name), filename=str(SAMPLES / f"{name}.py"))
    assert main(["verify", str(binary)]) == 0
    assert capsys.readouterr().out.startswith("OK sha256 ")
    assert main(["disasm", str(binary)]) == 0
    assert capsys.readouterr().out.startswith("; header: magic RWC1, format version 1")
    args, expected = CLI_RUNS[name]
    assert main(["run", str(binary), "--state", str(SAMPLES / f"{name}.state.json"), *args]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["ok"], report["result"]) == (True, expected)
    assert report["gas_used"] <= DEFAULT_GAS
