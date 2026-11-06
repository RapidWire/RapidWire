"""Helpers for compiling contract sources and running them on the mock ledger."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from typing import Any

import pytest

from rapidwire.contract.compiler import CompileError, compile_source
from rapidwire.contract.mock import MockLedger, RunResult
from rapidwire.contract.values import Attachment, Currency, Scalar
from rapidwire.contract.verifier import VerifiedProgram, verify

SELF = 1001
SENDER = 2002
OTHER = 3003
RAPID = Currency(1)
GOLD = Currency(2)
NOW = 1_700_000_000
SENDER_FUNDS = 10_000_000

VENDING_SOURCE = """
from rapidwire_contract import *

PRICE = 100_000  # 100.000 in minimal units
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

TRY_CALL_SOURCE = """
from rapidwire_contract import *

SHOP = 3003

@public
def buy_from_shop() -> str:
    shop = Account(SHOP)
    rapid = currency("RAPID")
    r = try_call(shop, "buy", [], attach=(rapid, 100_000), gas=5_000)
    if r.ok:
        result = r.value
    else:
        transfer(sender, rapid, 100_000)  # refund the user ourselves
        return "shop failed: " + r.message
    return "bought: " + str(result)
"""

FAILING_SHOP_SOURCE = """
from rapidwire_contract import *

@public
def buy() -> str:
    storage["attempts"] = storage.get("attempts", 0) + 1
    revert("out of stock")
"""

WORKING_SHOP_SOURCE = """
from rapidwire_contract import *

@public
def buy() -> str:
    storage["sold"] = storage.get("sold", 0) + 1
    return "item-" + str(storage.get("sold", 0))
"""


def source(text: str) -> str:
    return textwrap.dedent(text)


def body(text: str, returns: str = "int", params: str = "") -> str:
    """A contract whose public function main(params) -> returns has the given body."""
    return "@public\ndef main(" + params + ") -> " + returns + ":\n" + textwrap.indent(textwrap.dedent(text).strip("\n"), "    ") + "\n"


def compile_program(text: str, *, max_size: int = 1_000_000) -> VerifiedProgram:
    """Compile text and run the registration checks on the result."""
    return verify(compile_source(source(text), filename="test.py"), max_size=max_size)


def ledger(*, seed: int | None = 7) -> MockLedger:
    mock = MockLedger(seed=seed, max_size=1_000_000, gas_currency_id=RAPID.id)
    mock.add_currency(RAPID.id, "RAPID")
    mock.add_currency(GOLD.id, "GOLD")
    mock.set_balance(SENDER, RAPID, SENDER_FUNDS)
    return mock


async def run(
    text: str,
    function: str = "main",
    args: Sequence[Scalar] = (),
    *,
    mock: MockLedger | None = None,
    gas: int = 100_000,
    attach: Attachment | None = None,
    sender: int = SENDER,
) -> RunResult:
    mock = mock if mock is not None else ledger()
    mock.deploy(SELF, compile_program(text))
    return await mock.run(SELF, function, args, sender=sender, attach=attach, gas=gas, now=NOW)


async def value(text: str, function: str = "main", args: Sequence[Scalar] = (), **kwargs: Any) -> Any:
    result = await run(text, function, args, **kwargs)
    assert result.ok, f"{result.error}: {result.message}"
    return result.value


def compile_error(text: str) -> CompileError:
    with pytest.raises(CompileError) as info:
        compile_source(source(text), filename="test.py")
    return info.value
