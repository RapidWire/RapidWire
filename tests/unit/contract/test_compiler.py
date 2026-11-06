from __future__ import annotations

import pytest

from rapidwire.contract.assembler import disassemble
from rapidwire.contract.compiler import CompileError, compile_source
from rapidwire.contract.limits import DEFAULT_LIMITS
from rapidwire.contract.opcodes import Op
from rapidwire.contract.values import Account, Attachment
from tests.unit.contract.compiled import (
    FAILING_SHOP_SOURCE,
    NOW,
    OTHER,
    RAPID,
    SELF,
    SENDER,
    SENDER_FUNDS,
    TRY_CALL_SOURCE,
    VENDING_SOURCE,
    WORKING_SHOP_SOURCE,
    body,
    compile_program,
    ledger,
    run,
    source,
    value,
)

EXPRESSIONS: list[tuple[str, str, object]] = [
    ("return 7 // 2", "int", 3),
    ("x = -7\nreturn x // 2", "int", -4),
    ("x = -7\nreturn x % 3", "int", 2),
    ("x = 7\ny = -2\nreturn x % y", "int", -1),
    ("x = 5\nreturn x * 3 - 1", "int", 14),
    ("x = 5\nreturn -x + +x - (-x)", "int", 5),
    ("x = 2 if False else 3\nreturn x", "int", 3),
    ("a = 'ab'\nreturn a + 'cd'", "str", "abcd"),
    ("s = 'hello'\nreturn s[1:3] + s[:2] + s[3:] + s[-3:]", "str", "elhelollo"),
    ("s = 'hello'\nreturn s[1] + s[-1]", "str", "eo"),
    ("xs = [1, 2, 3, 4]\nreturn len(xs[1:]) * 10 + xs[-1]", "int", 34),
    ("xs = [1, 2, 3, 4]\nys = xs[1:3]\nreturn ys[0] * 10 + ys[1]", "int", 23),
    ("x = 5\nreturn 1 < x <= 5", "bool", True),
    ("x = 5\nreturn 1 < x < 3", "bool", False),
    ("x = 5\nreturn x == 5 and x != 4 and x >= 5 and x > 4 and x <= 5 and x < 6", "bool", True),
    ("a = 'abc'\nreturn a < 'abd'", "bool", True),
    ("x = True\nreturn not x", "bool", False),
    ("x = 1\nreturn not x > 2", "bool", True),
    ("x = False\ny = True\nreturn x or y", "bool", True),
    ("xs = [1, 2]\nreturn 2 in xs and 3 not in xs", "bool", True),
    ("d = {'a': 1}\nreturn 'a' in d and 'b' not in d", "bool", True),
    ("return 'ell' in 'hello'", "bool", True),
    ("s = 'hello'\nreturn 'xyz' not in s", "bool", True),
    ("x = 3\nreturn 'big' if x > 2 else 'small'", "str", "big"),
    ("x = 1\nreturn 'big' if x > 2 else 'small'", "str", "small"),
    ("a = b = 3\nreturn a + b", "int", 6),
    ("x = 10\nx += 5\nx -= 3\nx *= 2\nx //= 5\nx %= 3\nreturn x", "int", 1),
    ("s = 'a'\ns += 'b'\nreturn s", "str", "ab"),
    ("return f'a{1 + 1}b{True}c{\"d\"}'", "str", "a2bTruecd"),
    ("n = 5\nreturn f'n={n}, sender={sender}'", "str", f"n=5, sender={SENDER}"),
    ("return f''", "str", ""),
    ("return int('42') + int(True) + int(Account(5))", "int", 48),
    ("return str(5) + str(False) + str(Account(7)) + str('x')", "str", "5False7x"),
    ("return bool(0) or bool('') or bool([1])", "bool", True),
    ("x = 0\nreturn bool(x)", "bool", False),
    ("return min(3, 9) * 100 + max(3, 9) * 10 + abs(-2)", "int", 392),
    ("return Account(5) == Account(5) and Account(5) != Account(6)", "bool", True),
    ("return Currency(5) == Currency(5)", "bool", True),
    ("return 'a,b,c'.split(',')[2]", "str", "c"),
    ("return 'hello'.find('l') * 10 + 'hello'.find('z')", "int", 19),
    ("s = 'Hello'\nreturn s.startswith('He') and s.endswith('lo') and not s.startswith('x')", "bool", True),
    ("return ' Ab '.strip().upper() + 'Ab'.lower() + 'aXa'.replace('X', 'yy')", "str", "ABabayya"),
    ("return '-'.join(['a', 'b', 'c'])", "str", "a-b-c"),
    ("d = {'a': 1, 'b': 2}\nreturn d.get('a', 0) * 100 + d.get('z', 7) * 10 + len(d.keys()) + d.values()[1]", "int", 1 * 100 + 70 + 2 + 2),
    ("big = 123456789012345678901234567890\nreturn big % 1000", "int", 890),
    ("return 'caf\\u00e9' + '!'", "str", "caf" + chr(0xE9) + "!"),
]


@pytest.mark.parametrize(("text", "returns", "expected"), EXPRESSIONS)
async def test_expressions(text: str, returns: str, expected: object) -> None:
    assert await value(body(text, returns)) == expected


async def test_if_elif_else() -> None:
    text = """
    @public
    def classify(n: int) -> str:
        if n < 0:
            return "negative"
        elif n == 0:
            return "zero"
        elif n < 10:
            return "small"
        else:
            return "large"
    """
    for n, expected in ((-3, "negative"), (0, "zero"), (5, "small"), (50, "large")):
        assert await value(text, "classify", [n]) == expected


async def test_while_break_continue() -> None:
    text = body(
        """
        total = 0
        i = 0
        while True:
            i += 1
            if i > 10:
                break
            if i % 2 == 0:
                continue
            total += i
        return total
        """
    )
    assert await value(text) == 1 + 3 + 5 + 7 + 9


async def test_for_range_forms_and_loop_variable() -> None:
    text = body(
        """
        out = ""
        for i in range(3):
            out += str(i)
        for i in range(5, 8):
            out += str(i)
        for i in range(10, 0, -4):
            out += str(i)
        return out + ":" + str(i)
        """,
        "str",
    )
    assert await value(text) == "012" + "567" + "1062" + ":2"


async def test_range_arguments_are_evaluated_once() -> None:
    text = body(
        """
        n = 3
        count = 0
        for i in range(n):
            n += 1
            count += 1
        return count
        """
    )
    assert await value(text) == 3


async def test_for_over_list_str_and_dict() -> None:
    text = body(
        """
        out = ""
        for x in [1, 2, 3]:
            out += str(x)
        for c in "ab":
            out += c
        d = {"k": 1, "j": 2}
        for key in d:
            out += key
        for v in d.values():
            out += str(v)
        return out
        """,
        "str",
    )
    assert await value(text) == "123abkj12"


async def test_nested_loops_break_only_the_inner_loop() -> None:
    text = body(
        """
        pairs = 0
        for i in range(4):
            for j in range(4):
                if j > i:
                    break
                pairs += 1
        return pairs
        """
    )
    assert await value(text) == 10


async def test_list_operations_and_reference_semantics() -> None:
    text = """
    def fill(xs: list[int], n: int) -> None:
        for i in range(n):
            xs.append(i)

    @public
    def main() -> int:
        xs: list[int] = []
        fill(xs, 5)
        last = xs.pop()
        first = xs.pop(0)
        del xs[0]
        xs[0] = 9
        xs[1] *= 3
        alias = xs
        xs += [7, 8]
        xs += xs
        return last * 10000 + first * 1000 + len(alias) * 100 + xs[0] * 10 + xs[1] // 3
    """
    assert await value(text) == 4 * 10000 + 0 + 8 * 100 + 9 * 10 + 3


async def test_list_concatenation_makes_a_new_list() -> None:
    text = body(
        """
        xs = [1]
        ys = xs
        xs = xs + [2]
        return len(xs) * 10 + len(ys)
        """
    )
    assert await value(text) == 21


async def test_dict_operations() -> None:
    text = body(
        """
        d: dict[str, int] = {}
        d["a"] = 1
        d["b"] = 2
        d["a"] += 10
        del d["b"]
        nested: dict[int, list[str]] = {1: []}
        nested[1].append("x")
        return d["a"] * 10 + len(d) + len(nested[1])
        """
    )
    assert await value(text) == 112


async def test_large_literals_are_built_in_chunks() -> None:
    items = ", ".join(str(i) for i in range(150))
    entries = ", ".join(f"'k{i}': {i}" for i in range(70))
    text = body(f"xs = [{items}]\nd = {{{entries}}}\nreturn len(xs) * 1000 + xs[149] + len(d) * 1000000 + d['k69']")
    assert await value(text) == 150 * 1000 + 149 + 70 * 1000000 + 69


async def test_list_and_dict_comprehensions() -> None:
    text = """
    @public
    def main() -> int:
        xs = [i * i for i in range(6) if i % 2 == 0]
        pairs = [a * 10 + b for a in range(3) for b in range(a) if a != b]
        names = {str(i): i * 2 for i in xs}
        words = [w.upper() for w in "a b".split(" ")]
        empty: list[list[int]] = [[] for _ in range(2)]
        return sum_all(xs) * 1000 + sum_all(pairs) + names["16"] * 100000 + len(words) * 10000000 + len(empty) * 100000000

    def sum_all(values: list[int]) -> int:
        total = 0
        for v in values:
            total += v
        return total
    """
    assert await value(text) == (0 + 4 + 16) * 1000 + (10 + 20 + 21) + 32 * 100000 + 2 * 10000000 + 2 * 100000000


async def test_comprehension_variables_have_their_own_scope() -> None:
    text = body(
        """
        x = 100
        ys = [x for x in range(3)]
        return x + len(ys)
        """
    )
    assert await value(text) == 103


async def test_module_constants_are_folded() -> None:
    text = """
    A = 100
    B = A * 2 + 1
    NAME = "co" + "in"
    FLAG = B > A and not False
    NEG = -(A // 3)

    @public
    def main() -> str:
        if FLAG:
            return NAME + str(B) + str(NEG)
        return ""
    """
    assert await value(text) == "coin201-33"
    program = compile_program(text)
    ops = [item.info.op for item in program.functions[0].listing]
    assert Op.MUL not in ops and Op.FLOORDIV not in ops and Op.CONCAT in ops
    assert program.program.constants.count("coin") == 1


async def test_failing_constant_operations_in_functions_fail_at_run_time() -> None:
    lazy = body("x = False\nreturn x and 1 // 0 == 0", "bool")
    assert await value(lazy) is False
    result = await run(body("return 1 // 0"))
    assert (result.error, result.message) == ("RUNTIME", "division by zero")
    overflow = await run(body(f"return {2**255} * 4"))
    assert (overflow.error, overflow.message) == ("RUNTIME", "int result exceeds the int limit")


async def test_large_int_constants_use_the_constant_pool() -> None:
    text = body("return 2 * 123456789012345678901234567890")
    program = compile_program(text)
    assert 246913578024691357802469135780 in program.program.constants
    assert await value(text) == 246913578024691357802469135780


async def test_recursion() -> None:
    text = """
    def fib(n: int) -> int:
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)

    @public
    def main(n: int) -> int:
        return fib(n)
    """
    assert await value(text, args=[15]) == 610


async def test_recursion_depth_is_bounded_by_the_frame_limit() -> None:
    text = """
    def down(n: int) -> int:
        if n == 0:
            return 0
        return 1 + down(n - 1)

    @public
    def main(n: int) -> int:
        return down(n)
    """
    deepest = DEFAULT_LIMITS.max_frames - 2
    assert await value(text, args=[deepest]) == deepest
    result = await run(text, args=[deepest + 1])
    assert not result.ok
    assert result.error == "RUNTIME"
    assert f"internal call depth limit of {DEFAULT_LIMITS.max_frames}" in result.message


async def test_none_functions_and_docstrings() -> None:
    text = '''
    """Module docstring."""

    def record(xs: list[str], item: str) -> None:
        """Function docstring."""
        if item == "":
            return
        xs.append(item)

    @public
    def main() -> int:
        xs: list[str] = []
        record(xs, "a")
        record(xs, "")
        pass
        return len(xs)
    '''
    assert await value(text) == 1


async def test_assert_and_revert() -> None:
    text = """
    def check(n: int) -> int:
        assert n > 0, "must be positive"
        assert n < 100
        if n == 50:
            revert("fifty: " + str(n))
        return n

    @public
    def main(n: int) -> int:
        return check(n)

    @public
    def dynamic(n: int) -> int:
        assert n > 0, "bad " + str(n)
        return n
    """
    assert await value(text, args=[5]) == 5
    for n, message in ((-1, "must be positive"), (200, "assertion failed"), (50, "fifty: 50")):
        result = await run(text, args=[n])
        assert (result.ok, result.error, result.message) == (False, "REVERTED", message)
        assert result.failed_function == "check"
    result = await run(text, "dynamic", [-3])
    assert (result.error, result.message) == ("REVERTED", "bad -3")


async def test_short_circuit_skips_the_right_operand() -> None:
    text = """
    def boom() -> bool:
        revert("evaluated")

    @public
    def main() -> bool:
        x = False
        y = True
        if x and boom():
            return False
        return (x and boom()) or (y or boom())
    """
    assert await value(text) is True


async def test_chained_comparison_evaluates_each_operand_once() -> None:
    text = """
    def bump(xs: list[int]) -> int:
        xs.append(1)
        return 5

    @public
    def main() -> int:
        xs: list[int] = []
        ok = 1 < bump(xs) < 10
        return len(xs) * 10 + int(ok)
    """
    assert await value(text) == 11


async def test_item_assignment_evaluates_the_value_first() -> None:
    text = """
    def next_index(xs: list[int]) -> int:
        xs.append(0)
        return len(xs) - 1

    @public
    def main() -> int:
        xs = [5]
        xs[len(xs) - 1] = next_index(xs)
        return xs[0] * 10 + xs[1] + len(xs) * 100
    """
    assert await value(text) == 251


async def test_context_builtins() -> None:
    text = body("return str(now()) + ':' + str(sender) + ':' + str(int(self)) + ':' + str(paid.amount) + ':' + str(paid.currency)", "str")
    assert await value(text) == f"{NOW}:{SENDER}:{SELF}:0:0"
    paid = await value(text, attach=Attachment(RAPID, 25))
    assert paid == f"{NOW}:{SENDER}:{SELF}:25:{RAPID.id}"


async def test_storage_roundtrip_and_typed_reads() -> None:
    text = """
    @public
    def save() -> int:
        storage["count"] = storage.get("count", 0) + 1
        storage["names"] = ["a", "b"]
        storage.set("map", {"x": 1})
        storage["gone"] = True
        del storage["gone"]
        return storage.get("count", 0)

    @public
    def load() -> str:
        names: list[str] = storage.get("names", [])
        m = storage.get("map", {"": 0})
        return str(storage.get("count", 0)) + names[1] + str(m["x"]) + str(storage.get("gone", False))

    @public
    def wrong() -> int:
        storage["count"] = "text"
        return storage.get("count", 0)
    """
    mock = ledger()
    mock.deploy(SELF, compile_program(text))
    for expected in (1, 2):
        result = await mock.run(SELF, "save", sender=SENDER, gas=10_000)
        assert result.ok and result.value == expected
    assert (await mock.run(SELF, "load", sender=SENDER, gas=10_000)).value == "2b1False"
    assert mock.storage_items(SELF) == {"count": 2, "map": {"x": 1}, "names": ["a", "b"]}
    result = await mock.run(SELF, "wrong", sender=SENDER, gas=10_000)
    assert (result.error, result.message) == ("RUNTIME", "expected int, got str")
    assert mock.storage_value(SELF, "count") == 2


async def test_storage_reads_without_a_default() -> None:
    mock = ledger()
    mock.set_storage(OTHER, "price", 42)
    text = body(
        f"""
        storage["n"] = 5
        n: int = storage.get("n")
        p = int(read_storage(Account({OTHER}), "price"))
        return n + p
        """
    )
    assert await value(text, mock=mock) == 47
    missing = await run(body('x: int = storage.get("nothing")\nreturn x'))
    assert missing.error == "RUNTIME" and "not found" in missing.message


async def test_endless_loops_charge_gas_on_every_iteration() -> None:
    spin = await run(body("while True:\n    pass"), gas=200)
    assert (spin.error, spin.gas_used) == ("OUT_OF_GAS", 200)
    text = body(
        """
        i = 0
        while True:
            i += 1
            if i < 5:
                continue
            break
        return i
        """
    )
    assert await value(text) == 5


async def test_read_storage_of_another_contract() -> None:
    mock = ledger()
    mock.set_storage(OTHER, "price", 42)
    text = body(f"return read_storage(Account({OTHER}), 'price', 0) + read_storage(Account({OTHER}), 'missing', 1)")
    assert await value(text, mock=mock) == 43


async def test_value_narrowing() -> None:
    callee = """
    @public
    def number() -> int:
        return 41

    @public
    def text() -> str:
        return "7"
    """
    mock = ledger()
    mock.deploy(OTHER, compile_program(callee))
    text = body(
        f"""
        other = Account({OTHER})
        a: int = call(other, "number")
        b = int(call(other, "text"))
        r = try_call(other, "number", [])
        c: int = r.value
        d = str(call(other, "number")) + str(r.value)
        flag = bool(try_call(other, "number").ok)
        return a + b + c + len(d) + int(flag)
        """
    )
    assert await value(text, mock=mock) == 41 + 7 + 41 + 4 + 1


async def test_contract_calls_with_arguments_attach_and_gas() -> None:
    callee = """
    @public
    def pay(note: str, times: int, who: Account) -> str:
        return note + str(times) + str(who) + ":" + str(paid.amount)
    """
    mock = ledger()
    mock.set_balance(SELF, RAPID, 1000)
    mock.deploy(OTHER, compile_program(callee))
    text = body(
        f"""
        a = str(call(Account({OTHER}), "pay", ["n", 2, sender], attach=(currency("RAPID"), 10), gas=500))
        return a
        """,
        "str",
    )
    assert await value(text, mock=mock) == f"n2{SENDER}:10"
    assert mock.balance_of(OTHER, RAPID) == 10
    plain = body(f'return str(call(Account({OTHER}), "pay", ["n", 4, sender]))', "str")
    assert await value(plain, mock=mock) == f"n4{SENDER}:0"


async def test_call_argument_list_variable() -> None:
    callee = """
    @public
    def add(a: int, b: int) -> int:
        return a + b
    """
    mock = ledger()
    mock.deploy(OTHER, compile_program(callee))
    text = body(f"xs = [20, 22]\nreturn int(call(Account({OTHER}), 'add', xs))")
    program = compile_program(text)
    assert any(item.info.op is Op.CALL_CONTRACT and item.operands[0] == 4 for item in program.functions[0].listing)
    assert await value(text, mock=mock) == 42


async def test_call_failure_propagates_and_try_call_catches() -> None:
    mock = ledger()
    mock.deploy(OTHER, compile_program(FAILING_SHOP_SOURCE))
    plain = body(f"return str(call(Account({OTHER}), 'buy'))", "str")
    result = await run(plain, mock=mock)
    assert (result.ok, result.error, result.message, result.failed_function) == (False, "REVERTED", "out of stock", "buy")
    caught = body(
        f"""
        r = try_call(Account({OTHER}), "buy")
        missing = try_call(Account({OTHER}), "nope")
        nobody = try_call(Account(999), "buy")
        return r.error + ":" + r.message + "|" + missing.error + "|" + nobody.error + "|" + str(r.ok)
        """,
        "str",
    )
    assert await value(caught, mock=mock) == "REVERTED:out of stock|CALL_FAILED|CALL_FAILED|False"
    assert mock.storage_items(OTHER) == {}


async def test_spec_vending_machine() -> None:
    mock = ledger()
    mock.deploy(SELF, compile_program(VENDING_SOURCE))
    denied = await mock.run(SELF, "buy", sender=SENDER, attach=Attachment(RAPID, 150_000), now=NOW)
    assert denied.error == "RUNTIME" and "permission_denied" in denied.message
    assert [(t.kind, t.amount) for t in denied.transfers] == [("gas", denied.gas_used)]
    mock.allow_discord(123456789012345678, SELF)
    result = await mock.run(SELF, "buy", sender=SENDER, attach=Attachment(RAPID, 150_000), now=NOW)
    assert result.ok and result.value == "OK"
    assert result.gas_used == 29 and result.gas_fee == 29
    assert [(e.kind, e.guild, e.user, e.role) for e in result.outbox] == [("discord_add_role", 123456789012345678, SENDER, 234567890123456789)]
    assert [(t.kind, t.from_id, t.to_id, t.amount) for t in result.transfers[:2]] == [("attachment", SENDER, SELF, 150_000), ("transfer", SELF, SENDER, 50_000)]
    assert [(t.kind, t.from_id, t.amount) for t in result.transfers[2:]] == [("gas", SENDER, 29)]
    fees = denied.gas_fee + result.gas_fee
    assert mock.balance_of(SENDER, RAPID) == SENDER_FUNDS - 100_000 - fees
    assert mock.balance_of(SELF, RAPID) == 100_000
    exact = await mock.run(SELF, "buy", sender=SENDER, attach=Attachment(RAPID, 100_000), now=NOW)
    assert exact.ok and exact.gas_used == 17 and [t.kind for t in exact.transfers] == ["attachment", "gas"]
    short = await mock.run(SELF, "buy", sender=SENDER, attach=Attachment(RAPID, 99_999), now=NOW)
    assert (short.ok, short.error, short.message) == (False, "REVERTED", "Send at least 100 RAPID")
    assert short.outbox == () and [t.kind for t in short.transfers] == ["gas"]
    fees += exact.gas_fee + short.gas_fee
    assert mock.balance_of(SENDER, RAPID) == SENDER_FUNDS - 200_000 - fees


async def test_spec_try_call_snippet() -> None:
    mock = ledger()
    mock.deploy(SELF, compile_program(TRY_CALL_SOURCE))
    mock.deploy(OTHER, compile_program(FAILING_SHOP_SOURCE))
    result = await mock.run(SELF, "buy_from_shop", sender=SENDER, attach=Attachment(RAPID, 100_000), gas=10_000, now=NOW)
    assert result.ok and result.value == "shop failed: out of stock"
    assert mock.balance_of(SENDER, RAPID) == SENDER_FUNDS - result.gas_fee
    assert mock.balance_of(SELF, RAPID) == 0 and mock.balance_of(OTHER, RAPID) == 0
    assert mock.storage_items(OTHER) == {}
    assert [(c.depth, c.function, c.ok, c.error) for c in result.calls] == [(2, "buy", False, "REVERTED"), (1, "buy_from_shop", True, "")]
    working = ledger()
    working.deploy(SELF, compile_program(TRY_CALL_SOURCE))
    working.deploy(OTHER, compile_program(WORKING_SHOP_SOURCE))
    result = await working.run(SELF, "buy_from_shop", sender=SENDER, attach=Attachment(RAPID, 100_000), gas=10_000, now=NOW)
    assert result.ok and result.value == "bought: item-1"
    assert working.balance_of(OTHER, RAPID) == 100_000
    assert working.storage_items(OTHER) == {"sold": 1}


async def test_host_builtins_through_the_language() -> None:
    mock = ledger()
    mock.set_balance(SELF, RAPID, 5_000)
    mock.set_allowance(SENDER, SELF, RAPID, 300)
    mock.add_pool(RAPID, "GOLD", 100, 1_000_000, 2_000_000)
    mock.set_balance(SELF, "GOLD", 10_000)
    mock.grant_role(7, SENDER, 9)
    mock.allow_discord(7, SELF)
    text = body(
        f"""
        rapid = currency("RAPID")
        gold = currency("GOLD")
        info = currency_info(rapid)
        out = info.symbol + ":" + info.name + ":" + str(info.issuer) + ";"
        transfer(Account({OTHER}), rapid, 100)
        transfer_from(sender, Account({OTHER}), rapid, 50)
        approve(Account({OTHER}), rapid, 25)
        out += str(allowance(self, Account({OTHER}), rapid)) + "," + str(allowance(sender, self, rapid)) + ";"
        claim = create_claim(sender, rapid, 70, "dinner")
        c = claim_info(claim)
        out += c.status + ":" + str(c.amount) + ":" + c.description + ";"
        cancel_claim(claim)
        out += claim_info(claim).status + ";"
        stake(rapid, 1000)
        unstake(rapid, 400)
        out += str(staked(self, rapid)) + ";"
        q = quote(rapid, gold, 1000)
        got = swap(rapid, gold, 1000, q.amount_out)
        out += str(got == q.amount_out) + ":" + str(q.route_count) + ";"
        shares = add_liquidity(rapid, gold, 100, 1000, 5000, 0, 0, 1)
        back = remove_liquidity(gold, rapid, 100, shares, 0, 0)
        out += str(shares) + ":" + str(back[0]) + ":" + str(back[1]) + ";"
        out += str(discord.has_role(7, sender, 9)) + str(discord.has_role(7, sender, 8)) + ";"
        discord.send(7, 70, "hi")
        discord.remove_role(7, sender, 9)
        out += sha256("abc")[:8] + ";"
        out += str(balance(self, rapid))
        return out
        """,
        "str",
    )
    result = await run(text, mock=mock)
    assert result.ok, result.message
    parts = str(result.value).split(";")
    assert parts[0] == "RAPID:Rapid:1"
    assert parts[1] == "25,250"
    assert parts[2:4] == ["pending:70:dinner", "canceled"]
    assert parts[4] == "600"
    assert parts[5] == "True:1"
    shares, back_gold, back_rapid = (int(p) for p in parts[6].split(":"))
    assert shares > 0 and back_gold > 0 and back_rapid > 0
    assert parts[7] == "TrueFalse"
    assert parts[8] == "ba7816bf"
    assert [(e.kind, e.guild) for e in result.outbox] == [("discord_send", 7), ("discord_remove_role", 7)]


async def test_random_values_are_reported() -> None:
    text = body("return random(1, 6) * 10 + random(1, 6)")
    first = await run(text, mock=ledger(seed=3))
    second = await run(text, mock=ledger(seed=3))
    assert first.value == second.value
    assert len(first.random_values) == 2 and all(1 <= v <= 6 for v in first.random_values)
    assert first.value == first.random_values[0] * 10 + first.random_values[1]


async def test_while_true_without_break_needs_no_trailing_return() -> None:
    text = body(
        """
        i = 0
        while True:
            i += 1
            if i == 4:
                return i
        """
    )
    assert await value(text) == 4


async def test_unreachable_statements_are_checked_but_not_emitted() -> None:
    text = body("return 1\nx = 2\nreturn x")
    program = compile_program(text)
    assert [item.info.op for item in program.functions[0].listing] == [Op.PUSH_INT, Op.RET]


async def test_annotated_declaration_without_value() -> None:
    text = body(
        """
        x: int
        if True:
            x = 3
        return x
        """
    )
    assert await value(text) == 3


async def test_reading_an_unassigned_local_is_a_runtime_error() -> None:
    text = body(
        """
        flag = False
        if flag:
            x = 1
        return x
        """
    )
    result = await run(text)
    assert result.error == "RUNTIME" and "read before it is assigned" in result.message


async def test_list_extension_reads_the_list_before_the_value() -> None:
    text = """
    def boom() -> list[int]:
        revert("value evaluated")

    @public
    def main(c: bool) -> int:
        if c:
            xs = [1]
        xs += boom()
        return 0
    """
    result = await run(text, args=[False])
    assert result.error == "RUNTIME" and "read before it is assigned" in result.message


async def test_locals_assigned_later_in_a_loop_body() -> None:
    text = body(
        """
        total = 0
        for i in range(4):
            if i > 0:
                total += prev * 10
            prev = i
        return total
        """
    )
    assert await value(text) == (0 + 1 + 2) * 10
    early = await run(body("y = x\nx = 1\nreturn y"))
    assert early.error == "RUNTIME" and "read before it is assigned" in early.message


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("assert False", "assertion failed"),
        ("assert 1 == 2", "assertion failed"),
        ("assert False, 'stop ' + 'here'", "stop here"),
        ("x = 1\nif x > 0:\n    assert False, 'in a branch'\nelse:\n    assert 1 > 2", "in a branch"),
    ],
)
async def test_constant_false_assert_ends_a_function(text: str, message: str) -> None:
    result = await run(body(text))
    assert (result.error, result.message) == ("REVERTED", message)


async def test_item_assignment_follows_python_evaluation_order() -> None:
    text = """
    def touch(log: list[str], name: str, result: int) -> int:
        log.append(name)
        return result

    def grid(log: list[str]) -> list[list[int]]:
        log.append("container")
        return [[0, 0]]

    @public
    def main() -> str:
        log: list[str] = []
        l = [1]
        d: dict[int, int] = {}
        d[touch(log, "key", 0)] = touch(log, "value", 7)
        g = grid(log)
        g[touch(log, "row", 0)][touch(log, "column", 1)] = touch(log, "cell", 9)
        storage[str(touch(log, "skey", 3))] = touch(log, "svalue", 4)
        return ",".join(log) + ":" + str(d[0]) + str(g[0][1]) + str(storage.get("3", 0))
    """
    assert await value(text) == "value,key,container,cell,row,column,svalue,skey:794"
    order = """
    def boom() -> int:
        revert("value first")

    @public
    def main() -> int:
        xs = [1]
        xs[5] = boom()
        return 0
    """
    assert (await run(order)).message == "value first"


async def test_short_circuit_folding() -> None:
    text = """
    FLAG = True or 1 // 0 == 0

    @public
    def main(c: bool) -> int:
        n = 0
        while True or c:
            n += 1
            if n == 3:
                break
        if not (False and c):
            n += 10
        return n + int(FLAG)
    """
    assert await value(text, args=[False]) == 14
    with pytest.raises(CompileError, match="cannot compare"):
        compile_source(body("x = True or (1 == 'a')\nreturn 0"))


async def test_endless_conditions_compile_and_run_out_of_gas() -> None:
    for condition in ("True or c", "not (False and c)", "1 < 2 or c"):
        text = f"@public\ndef main(c: bool) -> int:\n    while {condition}:\n        pass\n"
        result = await run(text, args=[False], gas=300)
        assert (result.error, result.gas_used) == ("OUT_OF_GAS", 300), condition


async def test_call_keywords_are_evaluated_in_source_order() -> None:
    callee = "@public\ndef take() -> int:\n    return paid.amount\n"
    mock = ledger()
    mock.set_balance(SELF, RAPID, 100)
    mock.deploy(OTHER, compile_program(callee))
    text = f"""
    def note(log: list[str], name: str, result: int) -> int:
        log.append(name)
        return result

    @public
    def main() -> str:
        log: list[str] = []
        got = int(call(Account({OTHER}), "take", [], gas=note(log, "gas", 500), attach=(currency("RAPID"), note(log, "amount", 5))))
        return ",".join(log) + ":" + str(got)
    """
    assert await value(text, mock=mock) == "gas,amount:5"


async def test_containers_narrow_only_from_fresh_storage_reads() -> None:
    mock = ledger()
    mock.set_storage(SELF, "xs", [1, 2])
    text = body("xs: list[int] = storage.get('xs')\nys: list[int] = read_storage(self, 'xs')\nreturn xs[0] + ys[1]")
    assert await value(text, mock=mock) == 3
    hole = body("v = storage.get('xs')\na: list[int] = v\nb: list[str] = v\nreturn 0")
    with pytest.raises(CompileError, match="can only be narrowed from a Value read directly"):
        compile_source(hole)


def test_lone_surrogates_and_character_columns() -> None:
    with pytest.raises(CompileError, match="lone surrogate") as info:
        compile_source(body("return len('\\ud800')"))
    assert (info.value.line, info.value.column) == (3, 16)
    accented = chr(0xE9) * 3
    with pytest.raises(CompileError) as info:
        compile_source(body(f"s = '{accented}'; return s + 1", "str"))
    assert info.value.column == len(f"    s = '{accented}'; return ") + 1


async def test_parameters_may_shadow_builtins() -> None:
    text = """
    @public
    def main(currency: Currency, balance: int) -> int:
        return balance + int(currency)
    """
    program = compile_program(text)
    assert program.public_function("main") is not None
    assert await value(text, args=[RAPID, 5]) == 5 + RAPID.id


def test_disassembly_contains_the_function_table_and_instructions() -> None:
    program = compile_program(VENDING_SOURCE)
    text = disassemble(program)
    assert ".function buy public returns=str" in text
    assert "CALL_BUILTIN discord.add_role 3" in text
    assert "REVERT" in text and "; @0" in text


def test_functions_are_laid_out_in_declaration_order() -> None:
    text = """
    def helper() -> int:
        return 1

    @public
    def main() -> int:
        return helper()
    """
    program = compile_program(text)
    assert [(fn.name, fn.public) for fn in program.functions] == [("helper", False), ("main", True)]
    assert program.functions[1].params == ()


def test_compile_is_deterministic() -> None:
    assert compile_source(source(VENDING_SOURCE)) == compile_source(source(VENDING_SOURCE))


async def test_public_function_arguments_of_every_type() -> None:
    text = """
    @public
    def main(a: int, b: str, c: bool, d: Account, e: Currency) -> str:
        return str(a) + b + str(c) + str(d) + str(e)
    """
    assert await value(text, args=[1, "x", True, Account(5), RAPID]) == f"1xTrue5{RAPID.id}"
    result = await run(text, args=[1, "x", 1, Account(5), RAPID])
    assert (result.error, result.gas_used) == ("CALL_FAILED", 0)
