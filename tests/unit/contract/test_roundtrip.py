"""Every compiled program passes verify(), survives disassembly and reassembly byte for byte, and computes what Python computes."""

from __future__ import annotations

import asyncio
import operator
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rapidwire.contract.assembler import assemble, disassemble
from rapidwire.contract.compiler import compile_source
from rapidwire.contract.format import decode, encode
from rapidwire.contract.mock import DEFAULT_MAX_SIZE
from rapidwire.contract.values import Attachment
from rapidwire.contract.verifier import verify
from tests.unit.contract.compiled import FAILING_SHOP_SOURCE, OTHER, RAPID, SELF, SENDER, TRY_CALL_SOURCE, VENDING_SOURCE, WORKING_SHOP_SOURCE, ledger, source
from tests.unit.contract.test_stub import BROAD_SAMPLE

GAME_SOURCE = """
from rapidwire_contract import *

STAKE = 1_000
HOUSE_EDGE = 2


def payout(bet: int, roll: int) -> int:
    if roll > 50 + HOUSE_EDGE:
        return bet * 2
    return 0


@public
def play() -> str:
    assert paid.currency == currency("RAPID"), "pay in RAPID"
    assert paid.amount >= STAKE, "minimum bet is " + str(STAKE)
    roll = random(1, 100)
    won = payout(paid.amount, roll)
    history: list[int] = storage.get("rolls", [])
    history.append(roll)
    storage["rolls"] = history[-10:]
    if won > 0 and balance(self, paid.currency) >= won:
        transfer(sender, paid.currency, won)
        return f"won {won} with {roll}"
    return f"lost with {roll}"


@public
def rolls() -> str:
    history: list[int] = storage.get("rolls", [])
    return ",".join([str(r) for r in history])
"""

MEMBERS_SOURCE = """
from rapidwire_contract import *

GUILD = 42
MEMBER_ROLE = 7


@public
def join(name: str) -> int:
    if not discord.has_role(GUILD, sender, MEMBER_ROLE):
        revert("members only")
    members: dict[str, Account] = storage.get("members", {"": self})
    if name in members:
        revert("name taken")
    members[name] = sender
    storage["members"] = members
    return len(members) - 1


@public
def owner_of(name: str) -> str:
    members: dict[str, Account] = storage.get("members", {"": self})
    found = members.get(name, self)
    return "nobody" if found == self else str(found)
"""

CORPUS = {
    "vending": VENDING_SOURCE,
    "try_call": TRY_CALL_SOURCE,
    "failing_shop": FAILING_SHOP_SOURCE,
    "working_shop": WORKING_SHOP_SOURCE,
    "broad": BROAD_SAMPLE,
    "game": GAME_SOURCE,
    "members": MEMBERS_SOURCE,
}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_binaries_verify_and_reassemble_exactly(name: str) -> None:
    binary = compile_source(source(CORPUS[name]), filename=f"{name}.py")
    program = verify(binary, max_size=DEFAULT_MAX_SIZE)
    assert encode(decode(binary)) == binary
    assert assemble(disassemble(program)) == binary


async def test_game_runs_on_the_mock() -> None:
    mock = ledger(seed=5)
    mock.deploy(SELF, verify(compile_source(source(GAME_SOURCE)), max_size=DEFAULT_MAX_SIZE))
    mock.set_balance(SELF, RAPID, 1_000_000)
    outcomes = []
    for _ in range(12):
        result = await mock.run(SELF, "play", sender=SENDER, attach=Attachment(RAPID, 1_500), gas=1_000)
        assert result.ok, result.message
        roll = result.random_values[0]
        assert str(result.value).endswith(f"with {roll}")
        assert str(result.value).startswith("won 3000" if roll > 52 else "lost")
        outcomes.append(roll)
    history = await mock.run(SELF, "rolls", sender=SENDER, gas=1_000)
    assert history.value == ",".join(str(r) for r in outcomes[-10:])
    low = await mock.run(SELF, "play", sender=SENDER, attach=Attachment(RAPID, 10), gas=1_000)
    assert (low.error, low.message) == ("REVERTED", "minimum bet is 1000")


async def test_members_run_on_the_mock() -> None:
    mock = ledger()
    mock.deploy(SELF, verify(compile_source(source(MEMBERS_SOURCE)), max_size=DEFAULT_MAX_SIZE))
    unpermitted = await mock.run(SELF, "join", ["alice"], sender=SENDER)
    assert "permission_denied" in unpermitted.message
    mock.allow_discord(42, SELF)
    denied = await mock.run(SELF, "join", ["alice"], sender=SENDER)
    assert denied.message == "members only"
    mock.grant_role(42, SENDER, 7)
    mock.set_balance(OTHER, RAPID, 10_000)
    assert (await mock.run(SELF, "join", ["alice"], sender=SENDER, gas=1_000)).value == 1
    assert (await mock.run(SELF, "join", ["alice"], sender=SENDER, gas=1_000)).message == "name taken"
    assert (await mock.run(SELF, "owner_of", ["alice"], sender=OTHER, gas=1_000)).value == str(SENDER)
    assert (await mock.run(SELF, "owner_of", ["bob"], sender=OTHER, gas=1_000)).value == "nobody"


VARIABLES = ("a", "b", "c")
INT_OPS = ("+", "-", "*", "//", "%")
COMPARE_OPS = ("<", "<=", ">", ">=", "==", "!=")

int_trees = st.recursive(
    st.one_of(st.integers(-20, 20), st.sampled_from(VARIABLES)),
    lambda children: st.one_of(st.tuples(st.sampled_from(INT_OPS), children, children), st.tuples(st.just("neg"), children)),
    max_leaves=12,
)
bool_trees = st.recursive(
    st.tuples(st.sampled_from(COMPARE_OPS), int_trees, int_trees),
    lambda children: st.one_of(st.tuples(st.sampled_from(("and", "or")), children, children), st.tuples(st.just("not"), children)),
    max_leaves=6,
)


def render(tree: object) -> str:
    if isinstance(tree, int):
        return f"({tree})"
    if isinstance(tree, str):
        return tree
    assert isinstance(tree, tuple)
    if tree[0] in ("neg", "not"):
        return f"({'-' if tree[0] == 'neg' else 'not '}{render(tree[1])})"
    return f"({render(tree[1])} {tree[0]} {render(tree[2])})"


class DivisionByZero(Exception):
    pass


def evaluate(tree: object, env: dict[str, int]) -> int | bool:
    if isinstance(tree, bool):
        raise AssertionError("no bool leaves")
    if isinstance(tree, int):
        return tree
    if isinstance(tree, str):
        return env[tree]
    assert isinstance(tree, tuple)
    op = tree[0]
    if op == "neg":
        return -int(evaluate(tree[1], env))
    if op == "not":
        return not evaluate(tree[1], env)
    if op == "and":
        return bool(evaluate(tree[1], env)) and bool(evaluate(tree[2], env))
    if op == "or":
        return bool(evaluate(tree[1], env)) or bool(evaluate(tree[2], env))
    left = evaluate(tree[1], env)
    right = evaluate(tree[2], env)
    if op in ("//", "%") and right == 0:
        raise DivisionByZero
    result: int | bool = OPERATORS[op](left, right)
    return result


OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "//": operator.floordiv,
    "%": operator.mod,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def check(tree: object, returns: str, env: dict[str, int]) -> None:
    text = f"@public\ndef main(a: int, b: int, c: int) -> {returns}:\n    return {render(tree)}\n"
    binary = compile_source(text)
    program = verify(binary, max_size=DEFAULT_MAX_SIZE)
    assert assemble(disassemble(program)) == binary
    mock = ledger()
    mock.deploy(SELF, program)
    result = asyncio.run(mock.run(SELF, "main", [env["a"], env["b"], env["c"]], sender=SENDER, gas=100_000))
    try:
        expected = evaluate(tree, env)
    except DivisionByZero:
        assert (result.ok, result.error, result.message) == (False, "RUNTIME", "division by zero"), render(tree)
        return
    assert result.ok, (render(tree), result.message)
    assert result.value == expected and type(result.value) is type(expected), render(tree)


environments = st.fixed_dictionaries({name: st.integers(-50, 50) for name in VARIABLES})


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=int_trees, env=environments)
def test_int_expressions_match_python(tree: object, env: dict[str, int]) -> None:
    check(tree, "int", env)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=bool_trees, env=environments)
def test_bool_expressions_match_python(tree: object, env: dict[str, int]) -> None:
    check(tree, "bool", env)
