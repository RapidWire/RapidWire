from __future__ import annotations

from pathlib import Path

import pytest

from rapidwire.contract import storage_codec
from rapidwire.contract.costs import route_search_gas
from rapidwire.contract.errors import HostError
from rapidwire.contract.mock import POOL_ACCOUNT_BASE, SWAP_CHUNK_COUNT, SWAP_MAX_ROUTES, MockLedger, dump_state, from_json, load_state, to_json
from rapidwire.contract.runtime import contract_swap_config
from rapidwire.contract.types import Kind
from rapidwire.contract.values import Account, Attachment, Currency, make_record
from rapidwire.contract.vm import ExecutionContext
from rapidwire.core.routing import amount_out, enumerate_paths
from rapidwire.core.swap import allocation_chunk_hops, probe_amount
from rapidwire.db.tables import CONTRACT_STORAGE_MAX_KEYS, STAKE_CUSTODY_ACCOUNT_ID
from tests.unit.contract.compiled import GOLD, NOW, OTHER, RAPID, SELF, SENDER, SENDER_FUNDS, body, compile_program, ledger, run, value

THIRD = 4004


def deploy(mock: MockLedger, account: int, text: str) -> None:
    mock.deploy(account, compile_program(text))


async def test_failed_runs_undo_every_effect() -> None:
    text = body(
        f"""
        storage["k"] = 1
        transfer(Account({OTHER}), paid.currency, 10)
        discord.send(1, 2, "hello")
        revert("no")
        """
    )
    mock = ledger()
    mock.allow_discord(1, SELF)
    result = await run(text, mock=mock, attach=Attachment(RAPID, 100))
    assert (result.ok, result.error, result.message) == (False, "REVERTED", "no")
    assert result.outbox == () and [(t.kind, t.from_id, t.amount) for t in result.transfers] == [("gas", SENDER, result.gas_used)]
    assert mock.balance_of(SENDER, RAPID) == SENDER_FUNDS - result.gas_fee and mock.balance_of(SELF, RAPID) == 0 and mock.balance_of(OTHER, RAPID) == 0
    assert mock.storage_items(SELF) == {}


async def test_builtin_failures_are_runtime_errors_with_the_ledger_code() -> None:
    mock = ledger()
    result = await run(body(f"transfer(Account({OTHER}), currency('RAPID'), 5)\nreturn 0"), mock=mock)
    assert result.error == "RUNTIME" and "insufficient_balance" in result.message
    result = await run(body("transfer(self, currency('RAPID'), 5)\nreturn 0"), mock=mock)
    assert "invalid_argument" in result.message
    result = await run(body(f"transfer(Account({OTHER}), currency('RAPID'), 0)\nreturn 0"), mock=mock)
    assert "invalid_amount" in result.message
    result = await run(body("x = currency('NOPE')\nreturn 0"), mock=mock)
    assert "currency_not_found" in result.message


async def test_attachment_that_cannot_be_paid_fails_the_call() -> None:
    mock = ledger()
    result = await run(body("return paid.amount"), mock=mock, attach=Attachment(RAPID, SENDER_FUNDS + 1))
    assert (result.error, result.gas_used) == ("CALL_FAILED", 0)
    assert result.calls[-1].error == "CALL_FAILED"


async def test_try_call_undoes_only_the_callee() -> None:
    callee = """
    @public
    def take() -> int:
        storage["seen"] = True
        transfer(sender, paid.currency, 1)
        discord.send(9, 9, "from callee")
        revert("callee failed")
    """
    caller = body(
        f"""
        storage["before"] = 1
        discord.send(1, 1, "from caller")
        r = try_call(Account({OTHER}), "take", [], attach=(currency("RAPID"), 50))
        storage["after"] = r.error
        return balance(self, currency("RAPID"))
        """
    )
    mock = ledger()
    mock.set_balance(SELF, RAPID, 500)
    mock.allow_discord(1, SELF)
    mock.allow_discord(9, OTHER)
    deploy(mock, OTHER, callee)
    result = await run(caller, mock=mock)
    assert result.ok and result.value == 500
    assert mock.storage_items(SELF) == {"after": "REVERTED", "before": 1}
    assert mock.storage_items(OTHER) == {}
    assert [entry.message for entry in result.outbox] == ["from caller"]
    assert mock.balance_of(OTHER, RAPID) == 0


async def test_rollback_after_a_long_history_is_exact() -> None:
    callee = """
    @public
    def take(fail: bool) -> int:
        storage["n"] = storage.get("n", 0) + paid.amount
        if fail:
            revert("no")
        return paid.amount
    """
    caller = body(
        f"""
        total = 0
        for i in range(300):
            total += int(call(Account({OTHER}), "take", [False], attach=(currency("RAPID"), 1)))
        r = try_call(Account({OTHER}), "take", [True], attach=(currency("RAPID"), 50))
        return total * 1000 + len(r.error)
        """
    )
    mock = ledger()
    mock.set_balance(SELF, RAPID, 1_000)
    deploy(mock, OTHER, callee)
    result = await run(caller, mock=mock)
    assert result.ok and result.value == 300 * 1000 + len("REVERTED")
    assert mock.storage_items(OTHER) == {"n": 300}
    assert mock.balance_of(SELF, RAPID) == 700 and mock.balance_of(OTHER, RAPID) == 300
    assert [t.kind for t in result.transfers].count("attachment") == 300
    assert mock._undo == []


async def test_failure_of_a_plain_call_fails_everything() -> None:
    callee = '@public\ndef boom() -> int:\n    revert("deep")\n'
    caller = body(f'storage["x"] = 1\nreturn int(call(Account({OTHER}), "boom"))')
    mock = ledger()
    deploy(mock, OTHER, callee)
    result = await run(caller, mock=mock)
    assert (result.error, result.message, result.failed_function) == ("REVERTED", "deep", "boom")
    assert mock.storage_items(SELF) == {}


async def test_call_failures_before_execution() -> None:
    callee = "@public\ndef f(n: int) -> int:\n    return n\n"
    mock = ledger()
    deploy(mock, OTHER, callee)
    mock.deploy(THIRD, compile_program(callee), active=False)
    text = body(
        f"""
        a = try_call(Account(9999), "f", [1]).message
        b = try_call(Account({THIRD}), "f", [1]).message
        c = try_call(Account({OTHER}), "g", [1]).message
        d = try_call(Account({OTHER}), "f", ["x"]).message
        e = try_call(Account({OTHER}), "f", [1], attach=(currency("RAPID"), 5)).message
        return a + "|" + b + "|" + c + "|" + d + "|" + e
        """,
        "str",
    )
    result = await run(text, mock=mock)
    messages = str(result.value).split("|")
    assert "has no contract" in messages[0]
    assert "paused" in messages[1]
    assert "no public function" in messages[2]
    assert "argument 0 must be int" in messages[3]
    assert messages[4] == "the attachment cannot be paid: The balance is insufficient."


async def test_reentrancy_and_depth() -> None:
    back = f'@public\ndef back() -> str:\n    return try_call(Account({SELF}), "main").error\n'
    mock = ledger()
    deploy(mock, OTHER, back)
    assert (await run(body(f'return str(call(Account({OTHER}), "back"))', "str"), mock=mock)).value == "CALL_FAILED"
    chain = ledger()
    accounts = [5000 + i for i in range(8)]
    for index, account in enumerate(accounts):
        if index + 1 < len(accounts):
            deploy(chain, account, f'@public\ndef go() -> int:\n    return 1 + int(call(Account({accounts[index + 1]}), "go"))\n')
        else:
            deploy(chain, account, "@public\ndef go() -> int:\n    return 0\n")
    result = await chain.run(accounts[0], "go", sender=SENDER, gas=10_000, now=NOW)
    assert result.error == "CALL_FAILED" and "depth limit" in result.message
    assert [(c.depth, c.contract_id, c.function, c.ok, c.error) for c in result.calls] == [(1, accounts[0], "go", False, "CALL_FAILED")]


async def test_gas_sub_limit_of_a_try_call() -> None:
    spin = "@public\ndef spin() -> int:\n    while True:\n        pass\n"
    mock = ledger()
    deploy(mock, OTHER, spin)
    text = body(f'r = try_call(Account({OTHER}), "spin", [], gas=100)\nreturn len(r.error)')
    result = await run(text, mock=mock, gas=1_000)
    assert result.ok and result.value == len("OUT_OF_GAS")
    assert 100 < result.gas_used < 130
    spin_call = next(call for call in result.calls if call.function == "spin")
    assert (spin_call.error, spin_call.gas_used) == ("OUT_OF_GAS", 100)
    exhausted = await run(body(f'r = try_call(Account({OTHER}), "spin")\nreturn 0'), mock=mock, gas=500)
    assert (exhausted.error, exhausted.gas_used) == ("OUT_OF_GAS", 500)


async def test_discord_permissions_and_roles() -> None:
    mock = ledger()
    mock.allow_discord(1, SELF)
    mock.grant_role(1, SENDER, 5)
    assert (await run(body("discord.add_role(1, sender, 6)\nreturn int(discord.has_role(1, sender, 5))"), mock=mock)).value == 1
    denied = await run(body('discord.send(2, 3, "x")\nreturn 0'), mock=mock)
    assert denied.error == "RUNTIME" and "permission_denied" in denied.message
    for call, fragment in (
        ('discord.send(1, 0, "x")', "channel"),
        ('discord.send(1, 2, "")', "1 to 2000 characters"),
        ('discord.send(0, 2, "x")', "guild"),
        ("discord.add_role(1, sender, 0)", "role"),
    ):
        result = await run(body(f"{call}\nreturn 0"), mock=mock)
        assert result.error == "RUNTIME" and fragment in result.message, (call, result.message)
        assert result.outbox == ()
    invalid = await run(body("return int(discord.has_role(0, sender, 5))"), mock=mock)
    assert invalid.error == "RUNTIME" and "guild" in invalid.message


async def test_has_role_needs_the_discord_permission() -> None:
    mock = ledger()
    mock.grant_role(1, SENDER, 5)
    denied = await run(body("return int(discord.has_role(1, sender, 5))"), mock=mock)
    assert denied.error == "RUNTIME" and "permission_denied" in denied.message
    mock.allow_discord(1, SELF)
    assert await value(body("return int(discord.has_role(1, sender, 5))"), mock=mock) == 1


async def test_calls_failing_before_execution_are_recorded() -> None:
    mock = ledger()
    mock.deploy(OTHER, compile_program("@public\ndef f(n: int) -> int:\n    return n\n"))
    mock.deploy(THIRD, compile_program("@public\ndef f(n: int) -> int:\n    return n\n"), active=False)
    calls = body(
        f"""
        a = try_call(Account(9999), "f", [1]).ok
        b = try_call(Account({THIRD}), "f", [1]).ok
        c = try_call(Account({OTHER}), "g", [1]).ok
        d = try_call(Account({OTHER}), "f", ["x"]).ok
        return int(a or b or c or d)
        """
    )
    result = await run(calls, mock=mock)
    assert result.ok and result.value == 0
    failed = [(c.depth, c.contract_id, c.function, c.error, c.gas_used) for c in result.calls if not c.ok]
    assert failed == [(2, 9999, "f", "CALL_FAILED", 0), (2, THIRD, "f", "CALL_FAILED", 0), (2, OTHER, "g", "CALL_FAILED", 0), (2, OTHER, "f", "CALL_FAILED", 0)]
    missing = await mock.run(9999, "f", [1], sender=SENDER)
    assert missing.error == "CALL_FAILED" and [(c.depth, c.contract_id) for c in missing.calls] == [(1, 9999)]


async def test_gas_cap_prepayment_and_fees() -> None:
    mock = ledger()
    mock.deploy(SELF, compile_program(body("return paid.amount")))
    with pytest.raises(ValueError, match="between 1 and"):
        await mock.run(SELF, "main", sender=SENDER, gas=mock.max_gas + 1)
    with pytest.raises(ValueError, match="between 1 and"):
        await mock.run(SELF, "main", sender=SENDER, gas=0)
    mock.set_balance(OTHER, RAPID, 100)
    short = await mock.run(SELF, "main", sender=OTHER, gas=60, attach=Attachment(RAPID, 41))
    assert (short.error, short.gas_used) == ("CALL_FAILED", 0)
    assert "needs 101 of currency 1, but you hold 100" in short.message
    assert mock.balance_of(OTHER, RAPID) == 100
    paid = await mock.run(SELF, "main", sender=OTHER, gas=60, attach=Attachment(RAPID, 40))
    assert paid.ok and paid.value == 40
    assert mock.balance_of(OTHER, RAPID) == 100 - 40 - paid.gas_fee
    assert mock.balance_of(SELF, RAPID) == 40
    no_gas_currency = MockLedger(gas_currency_id=77)
    no_gas_currency.deploy(SELF, compile_program(body("return 0")))
    failed = await no_gas_currency.run(SELF, "main", sender=SENDER)
    assert failed.error == "CALL_FAILED" and "Currency 77 was not found" in failed.message


async def test_settlement_fails_when_the_contract_spent_the_senders_funds() -> None:
    mock = ledger()
    mock.set_balance(OTHER, RAPID, 1_000)
    mock.set_allowance(OTHER, SELF, RAPID, 10_000)
    mock.deploy(SELF, compile_program(body(f"transfer_from(sender, Account({THIRD}), currency('RAPID'), 1000)\nstorage['k'] = 1\nreturn 0")))
    result = await mock.run(SELF, "main", sender=OTHER, gas=100)
    assert (result.ok, result.error, result.message) == (False, "SETTLEMENT", "The balance is insufficient.")
    assert result.gas_used > 0 and [(t.kind, t.amount) for t in result.transfers] == [("gas", result.gas_fee)]
    assert mock.balance_of(OTHER, RAPID) == 1_000 - result.gas_fee and mock.balance_of(THIRD, RAPID) == 0
    assert mock.storage_items(SELF) == {}
    assert mock.state.allowances[(OTHER, SELF, RAPID.id)] == 10_000


async def test_missing_currencies_fail_with_no_currency() -> None:
    mock = ledger()
    mock.deploy(SELF, compile_program(body("return paid.amount")))
    top = await mock.run(SELF, "main", sender=SENDER, attach=Attachment(Currency(99), 5))
    assert (top.error, top.message, top.gas_used) == ("CALL_FAILED", "Currency 99 was not found.", 0)
    callee = "@public\ndef take() -> int:\n    return paid.amount\n"
    deploy(mock, OTHER, callee)
    nested = await run(body(f"return len(try_call(Account({OTHER}), 'take', [], attach=(Currency(99), 5)).message)"), mock=mock)
    failed = [call for call in nested.calls if call.function == "take"]
    assert [(call.error, call.message) for call in failed] == [("CALL_FAILED", "the attachment's currency 99 was not found")]


async def test_builtin_validation_messages_match_the_server() -> None:
    mock = ledger()
    approve = await run(body(f"approve(Account({OTHER}), currency('RAPID'), -1)\nreturn 0"), mock=mock)
    assert approve.message == "approve failed: invalid_amount: The allowance must be zero or a positive integer number of minimal units."
    pair_first = await run(body("return add_liquidity(currency('RAPID'), currency('RAPID'), 7, -1, -1, -1, -1, 0)"), mock=mock)
    assert pair_first.message == "add_liquidity failed: invalid_argument: A pool needs two different currencies."
    tier_next = await run(body("return add_liquidity(currency('RAPID'), currency('GOLD'), 7, -1, -1, -1, -1, 0)"), mock=mock)
    assert "invalid_argument: The fee tier must be one of" in tier_next.message
    minimum = await run(body("return add_liquidity(currency('RAPID'), currency('GOLD'), 100, 5, 5, -1, 0, 1)"), mock=mock)
    assert minimum.message == "add_liquidity failed: invalid_amount: The minimum amount must be a non-negative integer number of minimal units."
    shares = await run(body("return add_liquidity(currency('RAPID'), currency('GOLD'), 100, 5, 5, 0, 0, 0)"), mock=mock)
    assert "invalid_argument: min_shares must be an integer from 1 to" in shares.message
    slippage = await run(body("return swap(currency('RAPID'), currency('GOLD'), 5, -1)"), mock=mock)
    assert slippage.message == "swap failed: invalid_amount: The minimum amount out must be a non-negative integer number of minimal units."


async def test_claim_descriptions_follow_the_ledger_rules() -> None:
    mock = ledger()
    blank = await value(body("c = create_claim(sender, currency('RAPID'), 5, '   ')\nreturn len(claim_info(c).description)"), mock=mock)
    assert blank == 0
    long_blank = await value(body(f"return create_claim(sender, currency('RAPID'), 5, '{' ' * 80}')"), mock=mock)
    assert long_blank > 0
    too_long = await run(body(f"return create_claim(self, currency('RAPID'), 5, '{'x' * 51}')"), mock=mock)
    assert "at most 50 characters" in too_long.message
    zero = await run(body(f"return create_claim(self, currency('RAPID'), 0, '{'x' * 51}')"), mock=mock)
    assert "invalid_amount" in zero.message


async def test_calls_the_vm_refuses_are_recorded_like_the_server_records_them() -> None:
    mock = ledger()
    mock.deploy(OTHER, compile_program("@public\ndef f(n: int) -> int:\n    return n\n"))
    long_name = "x" * 70
    text = body(
        f"""
        v: Value = [1]
        many: list[int] = []
        while len(many) < 17:
            many.append(1)
        a = try_call(Account({OTHER}), "not a name", [1]).message
        b = try_call(Account({OTHER}), "{long_name}", [1]).message
        c = try_call(Account({OTHER}), "f", [v]).message
        d = try_call(Account({OTHER}), "f", many).message
        e = try_call(self, "main").message
        return a + "|" + b + "|" + c + "|" + d + "|" + e
        """,
        "str",
    )
    result = await run(text, mock=mock)
    assert result.ok
    assert str(result.value).split("|") == [
        "no public function 'not a name'",
        f"no public function '{'x' * 59}'...",
        "call argument 0 must be int, str, bool, Account or Currency, not list",
        "17 call arguments, more than 16",
        f"contract {SELF} is already executing",
    ]
    failed = [(c.depth, c.contract_id, c.sender_id, c.function, c.args, c.error, c.gas_used, c.random_values) for c in result.calls if not c.ok]
    assert failed == [
        (2, OTHER, SELF, "'not a name'", (1,), "CALL_FAILED", 0, ()),
        (2, OTHER, SELF, f"'{'x' * 59}'...", (1,), "CALL_FAILED", 0, ()),
        (2, OTHER, SELF, "f", (), "CALL_FAILED", 0, ()),
        (2, OTHER, SELF, "f", (), "CALL_FAILED", 0, ()),
        (2, SELF, SELF, "main", (), "CALL_FAILED", 0, ()),
    ]
    assert all(len(c.function) <= 64 for c in result.calls)


async def test_a_refused_call_under_call_fails_and_undoes_the_run() -> None:
    mock = ledger()
    mock.deploy(OTHER, compile_program("@public\ndef f(n: int) -> int:\n    return n\n"))
    result = await run(body(f'storage["k"] = 1\ntransfer(Account({OTHER}), paid.currency, 10)\nreturn int(call(Account({OTHER}), "no-name", [1]))'), mock=mock, attach=Attachment(RAPID, 100))
    assert (result.ok, result.error, result.message) == (False, "CALL_FAILED", "no public function 'no-name'")
    assert [(c.depth, c.function, c.error, c.gas_used) for c in result.calls] == [(1, "main", "CALL_FAILED", result.gas_used)]
    assert mock.storage_items(SELF) == {} and mock.balance_of(OTHER, RAPID) == 0 and mock.balance_of(SELF, RAPID) == 0
    assert [(t.kind, t.amount) for t in result.transfers] == [("gas", result.gas_fee)]


async def test_calls_keep_only_the_records_the_server_keeps() -> None:
    mock = ledger()
    deploy(mock, OTHER, '@public\ndef ok() -> int:\n    return 1\n\n@public\ndef fail() -> int:\n    revert("no")\n')
    middle = f"""
    @public
    def caught() -> int:
        a = int(call(Account({OTHER}), "ok"))
        r = try_call(Account({OTHER}), "fail")
        return a

    @public
    def uncaught() -> int:
        a = int(call(Account({OTHER}), "ok"))
        b = int(call(Account({OTHER}), "fail"))
        return a + b
    """
    deploy(mock, THIRD, middle)
    caught = await run(body(f'return int(call(Account({THIRD}), "caught"))'), mock=mock)
    assert caught.ok
    assert [(c.depth, c.function, c.ok) for c in caught.calls] == [(3, "ok", True), (3, "fail", False), (2, "caught", True), (1, "main", True)]
    inner = await run(body(f'r = try_call(Account({THIRD}), "uncaught")\nreturn len(r.error)'), mock=mock)
    assert inner.ok
    assert [(c.depth, c.function, c.ok, c.error) for c in inner.calls] == [(2, "uncaught", False, "REVERTED"), (1, "main", True, "")]
    failed = await run(body(f'return int(call(Account({THIRD}), "caught")) // 0'), mock=mock)
    assert (failed.ok, failed.error) == (False, "RUNTIME")
    assert [(c.depth, c.function, c.ok, c.error) for c in failed.calls] == [(1, "main", False, "RUNTIME")]


async def test_a_failed_settlement_keeps_only_the_failed_top_level_record() -> None:
    mock = ledger()
    deploy(mock, OTHER, "@public\ndef ok() -> int:\n    return 1\n")
    mock.set_balance(THIRD, RAPID, 1_000)
    mock.set_allowance(THIRD, SELF, RAPID, 10_000)
    mock.deploy(SELF, compile_program(body(f"x = int(call(Account({OTHER}), 'ok'))\ntransfer_from(sender, Account({OTHER}), currency('RAPID'), 1000)\nreturn x")))
    result = await mock.run(SELF, "main", sender=THIRD, gas=200)
    assert (result.ok, result.error) == (False, "SETTLEMENT")
    assert [(c.depth, c.function, c.ok, c.value, c.error, c.message) for c in result.calls] == [(1, "main", False, None, "SETTLEMENT", "The balance is insufficient.")]


async def test_attaching_to_ones_own_contract_is_rejected() -> None:
    mock = ledger()
    mock.deploy(SENDER, compile_program(body("return 0")))
    with pytest.raises(ValueError, match="your own contract"):
        await mock.run(SENDER, "main", sender=SENDER, attach=Attachment(RAPID, 1))


async def test_storage_is_canonical_and_limited() -> None:
    mock = ledger()
    result = await run(body('storage["d"] = {"b": 1, "a": 2}\nd = storage.get("d", {"": 0})\nreturn d.keys()[0] + d.keys()[1]', "str"), mock=mock)
    assert result.value == "ab"
    stored = mock.state.storage[SELF]["d"]
    assert stored == storage_codec.encode({"a": 2, "b": 1})
    full = ledger()
    for index in range(CONTRACT_STORAGE_MAX_KEYS):
        full.set_storage(SELF, f"k{index}", 0)
    result = await run(body('storage["k0"] = 5\nstorage["new"] = 1\nreturn 0'), mock=full)
    assert result.error == "RUNTIME" and "contract_storage_limit" in result.message
    assert full.storage_value(SELF, "k0") == 0


async def test_claims_and_allowances() -> None:
    mock = ledger()
    mock.set_balance(SELF, RAPID, 1000)
    claim_id = mock.add_claim(OTHER, SELF, RAPID, 300, "rent")
    text = body(
        f"""
        pay_claim({claim_id})
        mine = create_claim(sender, currency("RAPID"), 5, "x")
        cancel_claim(mine)
        approve(Account({OTHER}), currency("RAPID"), 40)
        return allowance(self, Account({OTHER}), currency("RAPID")) + claim_info({claim_id}).amount
        """
    )
    result = await run(text, mock=mock)
    assert result.ok and result.value == 340
    assert mock.balance_of(OTHER, RAPID) == 300
    assert [claim.status for claim in mock.state.claims.values()] == ["paid", "canceled"]
    again = await run(body(f"pay_claim({claim_id})\nreturn 0"), mock=mock)
    assert "invalid_state" in again.message
    foreign = mock.add_claim(OTHER, THIRD, RAPID, 1)
    assert "permission_denied" in (await run(body(f"pay_claim({foreign})\nreturn 0"), mock=mock)).message
    assert "claim_not_found" in (await run(body("return claim_info(999).amount"), mock=mock)).message
    spend = body(f"transfer_from(sender, Account({OTHER}), currency('RAPID'), 30)\nreturn 0")
    assert "insufficient_allowance" in (await run(spend, mock=mock)).message
    mock.set_allowance(SENDER, SELF, RAPID, 30)
    assert (await run(spend, mock=mock)).ok
    assert mock.state.allowances[(SENDER, SELF, RAPID.id)] == 0


async def test_staking() -> None:
    mock = ledger()
    mock.set_balance(SELF, RAPID, 100)
    result = await run(body("stake(currency('RAPID'), 60)\nunstake(currency('RAPID'), 10)\nreturn staked(self, currency('RAPID'))"), mock=mock)
    assert result.value == 50
    assert mock.balance_of(SELF, RAPID) == 50 and mock.balance_of(STAKE_CUSTODY_ACCOUNT_ID, RAPID) == 50
    assert "insufficient_stake" in (await run(body("unstake(currency('RAPID'), 51)\nreturn 0"), mock=mock)).message


async def test_swaps_follow_the_constant_product_formula() -> None:
    mock = ledger()
    pool = mock.add_pool(RAPID, GOLD, 100, 1_000_000, 3_000_000)
    mock.set_balance(SELF, RAPID, 10_000)
    expected = amount_out(10_000, 1_000_000, 3_000_000, 100)
    result = await run(body("q = quote(currency('RAPID'), currency('GOLD'), 10000)\nreturn swap(currency('RAPID'), currency('GOLD'), 10000, q.amount_out)"), mock=mock)
    assert result.ok and result.value == expected
    assert (pool.reserve_a, pool.reserve_b) == (1_010_000, 3_000_000 - expected)
    assert mock.balance_of(SELF, GOLD) == expected and mock.balance_of(SELF, RAPID) == 0
    assert mock.balance_of(POOL_ACCOUNT_BASE - pool.pool_id, GOLD) == pool.reserve_b
    mock.set_balance(SELF, RAPID, 10)
    slipped = await run(body("return swap(currency('RAPID'), currency('GOLD'), 10, 1000)"), mock=mock)
    assert "slippage_exceeded" in slipped.message
    assert "currency_not_found" in (await run(body("return quote(currency('RAPID'), Currency(77), 5).amount_out"), mock=mock)).message
    mock.add_currency(3, "SILVER")
    assert "no_route" in (await run(body("return quote(currency('RAPID'), currency('SILVER'), 5).amount_out"), mock=mock)).message


async def test_swaps_and_quotes_charge_the_route_search_like_the_server() -> None:
    mock = ledger()
    third = mock.add_currency(3, "SILVER")
    mock.add_pool(RAPID, GOLD, 100, 1_000_000, 3_000_000)
    mock.add_pool(GOLD, third, 50, 2_000_000, 2_000_000)
    mock.add_pool(RAPID, third, 10, 500_000, 400_000)
    mock.set_balance(SELF, RAPID, 50_000)
    config = contract_swap_config(SWAP_CHUNK_COUNT, SWAP_MAX_ROUTES)
    assert mock.swap_config == config

    def expected(amount: int, target: Currency = third) -> int:
        pools = [pool.state() for pool in mock.state.pools.values()]
        search = enumerate_paths(pools, RAPID.id, target.id, max_paths=config.max_routes, probe_amount=probe_amount(amount, config.chunk_count), max_expansions=config.max_expansions)
        assert search.expansions > 0 and search.paths
        return route_search_gas(search.expansions, allocation_chunk_hops(search.paths, amount, config.chunk_count))

    def context() -> ExecutionContext:
        return ExecutionContext.top_level(self_account=Account(SELF), sender=Account(SENDER), paid=None, now=NOW, gas_limit=10**9)

    ctx = context()
    want = expected(10_000)
    await mock.quote(ctx, RAPID, third, 10_000)
    assert ctx.gas.used == want
    ctx = context()
    want = expected(10_000)
    await mock.swap(ctx, RAPID, third, 10_000, 0)
    assert ctx.gas.used == want
    broke = context()
    want = expected(10**9)
    with pytest.raises(HostError, match="insufficient_balance"):
        await mock.swap(broke, RAPID, third, 10**9, 0)
    assert broke.gas.used == want
    lonely = mock.add_currency(4, "LEAD")
    unrouted = context()
    with pytest.raises(HostError, match="no_route"):
        await mock.quote(unrouted, RAPID, lonely, 10)
    assert unrouted.gas.used == 0
    tin = mock.add_currency(5, "TIN")
    mock.add_pool(RAPID, tin, 100, 1_000_000, 2_000)
    worthless = context()
    want = expected(1, tin)
    with pytest.raises(HostError, match="no_route"):
        await mock.quote(worthless, RAPID, tin, 1)
    assert worthless.gas.used == want > 0


async def test_liquidity_round_trip() -> None:
    mock = ledger()
    pool = mock.add_pool(RAPID, GOLD, 50, 1_000_000, 1_000_000)
    mock.set_balance(SELF, RAPID, 10_000)
    mock.set_balance(SELF, GOLD, 10_000)
    text = body(
        "s = add_liquidity(currency('GOLD'), currency('RAPID'), 50, 10000, 5000, 0, 0, 1)\nback = remove_liquidity(currency('GOLD'), currency('RAPID'), 50, s, 0, 0)\nreturn f'{s}:{back[0]}:{back[1]}'",
        "str",
    )
    result = await run(text, mock=mock)
    assert result.ok and result.value == "5000:5000:5000"
    assert pool.shares.get(SELF) is None
    assert (pool.reserve_a, pool.reserve_b, pool.total_shares) == (1_000_000, 1_000_000, 1_000_000)
    assert mock.balance_of(SELF, RAPID) == 10_000 and mock.balance_of(SELF, GOLD) == 10_000
    assert "pool_not_found" in (await run(body("return add_liquidity(currency('GOLD'), currency('RAPID'), 10, 1, 1, 0, 0, 1)"), mock=mock)).message
    assert "insufficient_shares" in (await run(body("return remove_liquidity(currency('GOLD'), currency('RAPID'), 50, 1, 0, 0)[0]"), mock=mock)).message


async def test_currency_info_and_supply() -> None:
    mock = ledger()
    mock.set_balance(OTHER, RAPID, 5)
    result = await run(body("i = currency_info(currency('RAPID'))\nreturn i.supply + int(i.currency) * 0 + i.hourly_rate_ppm"), mock=mock)
    assert result.value == SENDER_FUNDS + 5


async def test_random_values_of_nested_calls_are_recorded() -> None:
    callee = "@public\ndef roll() -> int:\n    return random(1, 100)\n"
    mock = ledger(seed=11)
    deploy(mock, OTHER, callee)
    result = await run(body(f'return int(call(Account({OTHER}), "roll")) + random(1, 1)'), mock=mock)
    roll = next(call for call in result.calls if call.function == "roll")
    assert len(roll.random_values) == 1 and result.random_values == (1,)
    assert result.value == roll.random_values[0] + 1


def test_json_values_round_trip() -> None:
    values = [1, "x", True, Account(5), Currency(6), [1, [2]], {"a": 1}, {1: "x", Account(2): True}]
    for item in values:
        assert from_json(to_json(item)) == item
    assert to_json(make_record(Kind.PAID, currency=Currency(1), amount=3)) == {"$record": "Paid", "currency": {"$currency": 1}, "amount": 3}
    for bad in (1.5, None, {"$other": 1}, {"$dict": [[[1], 2]]}):
        with pytest.raises(ValueError):
            from_json(bad)


def test_state_documents_round_trip(tmp_path: Path) -> None:
    (tmp_path / "shop.py").write_text("@public\ndef buy() -> str:\n    return 'ok'\n", encoding="utf-8")
    document = {
        "now": 123,
        "currencies": [{"id": 1, "symbol": "RAPID"}, {"id": 2, "symbol": "GOLD", "name": "Gold"}],
        "balances": [{"account": 10, "currency": "RAPID", "amount": 500}],
        "allowances": [{"owner": 10, "spender": 11, "currency": 1, "amount": 7}],
        "stakes": [{"account": 10, "currency": "GOLD", "amount": 3}],
        "pools": [{"currency_x": "GOLD", "currency_y": "RAPID", "fee_bps": 10, "reserve_x": 5000, "reserve_y": 6000, "shares": {"10": 100}}],
        "claims": [{"claimant": 10, "payer": 11, "currency": "RAPID", "amount": 9}],
        "roles": [[1, 10, 2]],
        "discord_permissions": [[1, 3003]],
        "contracts": [{"account": 3003, "source": "shop.py"}],
        "storage": {"3003": {"k": {"$account": 4}}},
    }
    mock = MockLedger()
    assert load_state(mock, document, base_dir=tmp_path) == 123
    dumped = dump_state(mock)
    assert {"account": 10, "currency": 1, "amount": 500} in dumped["balances"]
    assert dumped["allowances"] == [{"owner": 10, "spender": 11, "currency": 1, "amount": 7}]
    assert dumped["stakes"] == [{"account": 10, "currency": 2, "amount": 3}]
    assert dumped["pools"] == [{"currency_x": 1, "currency_y": 2, "fee_bps": 10, "reserve_x": 6000, "reserve_y": 5000, "shares": {"10": 100}}]
    assert dumped["storage"] == {"3003": {"k": {"$account": 4}}}
    assert mock.roles == {(1, 10, 2)} and mock.discord_permissions == {(1, 3003)}
    assert 3003 in mock.contracts
    reloaded = MockLedger()
    load_state(reloaded, {"currencies": document["currencies"], **dumped}, base_dir=tmp_path)
    assert dump_state(reloaded) == dumped
    with pytest.raises(ValueError, match="unknown state keys"):
        load_state(MockLedger(), {"balance": []}, base_dir=tmp_path)
    with pytest.raises(ValueError, match="malformed"):
        load_state(MockLedger(), {"balances": [{"account": 1}]}, base_dir=tmp_path)
