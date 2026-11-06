from __future__ import annotations

import pytest

from rapidwire.contract.compiler import CompileError, compile_source
from tests.unit.contract.compiled import body, compile_error

MAIN = "@public\ndef main() -> int:\n    return 0\n"

# (case id, source, line of the error, fragment of the message)
REJECTIONS: list[tuple[str, str, int, str]] = [
    ("import", "import os\n" + MAIN, 1, "import is not allowed"),
    ("from import", "from os import path\n" + MAIN, 1, "import is not allowed"),
    ("future import", "from __future__ import annotations\n" + MAIN, 1, "import is not allowed"),
    ("stub imported twice", "from rapidwire_contract import *\nfrom rapidwire_contract import *\n" + MAIN, 2, "imported twice"),
    ("class", MAIN + "class A:\n    pass\n", 4, "class definitions are not supported"),
    ("module statement", MAIN + "transfer(sender, currency('X'), 1)\n", 4, "only constants and function definitions"),
    ("module non-constant", "X = now()\n" + MAIN, 1, "constant int, str or bool expression"),
    ("module reassignment", "X = 1\nX = 2\n" + MAIN, 2, "already defined as a constant"),
    ("constant shadows builtin", "sender = 1\n" + MAIN, 1, "shadows a builtin"),
    ("function shadows builtin", MAIN + "def transfer() -> None:\n    pass\n", 4, "shadows a builtin"),
    ("global", body("global x\nreturn 0"), 3, "global is not supported"),
    ("nonlocal", body("nonlocal x\nreturn 0"), 3, "nonlocal is not supported"),
    ("try", body("try:\n    pass\nexcept Exception:\n    pass\nreturn 0"), 3, "try is not supported"),
    ("with", body("with x:\n    pass\nreturn 0"), 3, "with is not supported"),
    ("raise", body("raise ValueError()"), 3, "use revert(message)"),
    ("lambda", body("f = lambda: 1\nreturn 0"), 3, "lambda is not supported"),
    ("yield", body("yield 1\nreturn 0"), 3, "generators are not supported"),
    ("async def", MAIN + "async def f() -> None:\n    pass\n", 4, "async functions are not supported"),
    ("await", "@public\nasync def main() -> int:\n    return 0\n", 2, "async functions are not supported"),
    ("nested function", body("def inner() -> int:\n    return 1\nreturn 0"), 3, "nested functions are not supported"),
    ("walrus", body("if (x := 1) > 0:\n    return x\nreturn 0"), 3, "assignment expressions"),
    ("generator expression", body("x = len(i for i in range(3))\nreturn 0"), 3, "generator expressions are not supported"),
    ("set", body("x = {1, 2}\nreturn 0"), 3, "sets are not supported"),
    ("set comprehension", body("x = {i for i in range(2)}\nreturn 0"), 3, "sets are not supported"),
    ("tuple", body("x = (1, 2)\nreturn 0"), 3, "tuples are not supported"),
    ("unpacking", body("a, b = 1, 2\nreturn 0"), 3, "unpacking assignment is not supported"),
    ("loop unpacking", body("for a, b in []:\n    pass\nreturn 0"), 3, "a loop target must be a single name"),
    ("star args", MAIN + "def f(*args: int) -> None:\n    pass\n", 4, "*args is not supported"),
    ("kwargs", MAIN + "def f(**kw: int) -> None:\n    pass\n", 4, "**kwargs is not supported"),
    ("keyword-only", MAIN + "def f(*, a: int) -> None:\n    pass\n", 4, "keyword-only parameters"),
    ("default argument", MAIN + "def f(a: int = 1) -> None:\n    pass\n", 4, "default parameter values"),
    ("star call", body("xs = [1]\nreturn max(*xs)"), 4, "*args is not supported"),
    ("keyword call", body("return max(a=1, b=2)"), 3, "keyword arguments are not supported"),
    ("f-string conversion", body("return len(f'{1!r}')"), 3, "conversions"),
    ("f-string format spec", body("return len(f'{1:>3}')"), 3, "format specifications"),
    ("float", body("x = 1.5\nreturn 0"), 3, "floats are not supported"),
    ("complex", body("x = 1j\nreturn 0"), 3, "complex numbers are not supported"),
    ("bytes", body("x = b'a'\nreturn 0"), 3, "bytes are not supported"),
    ("true division", body("x = 4\nreturn x / 2"), 4, "use // for integer division"),
    ("power", body("x = 4\nreturn x ** 2"), 4, "** is not supported"),
    ("bit operators", body("x = 4\nreturn x & 1"), 4, "& is not supported"),
    ("invert", body("x = 4\nreturn ~x"), 4, "~ is not supported"),
    ("is", body("x = 4\nreturn int(x is 4)"), 4, "is is not supported"),
    ("dunder name", body("__x = 1\nreturn 0"), 3, "names beginning with __"),
    ("dunder attribute", body("s = 'a'\nreturn len(s.__class__)"), 4, "names beginning with __"),
    ("dunder function", MAIN + "def __f() -> None:\n    pass\n", 4, "names beginning with __"),
    ("print", body("print(1)\nreturn 0"), 3, "print is not an allowed builtin"),
    ("eval", body("return eval('1')"), 3, "eval is not an allowed builtin"),
    ("exec", body("exec('1')\nreturn 0"), 3, "exec is not an allowed builtin"),
    ("open", body("open('f')\nreturn 0"), 3, "open is not an allowed builtin"),
    ("unknown name", body("return missing"), 3, "name missing is not defined"),
    ("other decorator", "@staticmethod\ndef main() -> int:\n    return 0\n", 1, "the only allowed decorator is @public"),
    ("called decorator", "@public()\ndef main() -> int:\n    return 0\n", 1, "the only allowed decorator is @public"),
    ("missing parameter annotation", MAIN + "def f(a) -> None:\n    pass\n", 4, "needs a type annotation"),
    ("missing return annotation", MAIN + "def f(a: int):\n    pass\n", 4, "needs a return type annotation"),
    ("string annotation", MAIN + "def f(a: 'int') -> None:\n    pass\n", 4, "string annotations"),
    ("unknown type", MAIN + "def f(a: float) -> None:\n    pass\n", 4, "unknown type float"),
    ("bare list type", MAIN + "def f(a: list) -> None:\n    pass\n", 4, "list needs element types"),
    ("dict key type", MAIN + "def f(a: dict[list[int], int]) -> None:\n    pass\n", 4, "dict keys must be one of int, str, Account, Currency, not list[int]"),
    ("bool dict key annotation", MAIN + "def f(a: dict[bool, int]) -> None:\n    pass\n", 4, "dict keys must be one of int, str, Account, Currency, not bool"),
    ("bool dict key literal", body("d = {True: 1}\nreturn 0"), 3, "not bool"),
    ("bool dict key comprehension", body("d = {i > 0: i for i in range(2)}\nreturn 0"), 3, "not bool"),
    ("public list parameter", "@public\ndef main(xs: list[int]) -> int:\n    return 0\n", 2, "a public function parameter must be"),
    ("public None return", "@public\ndef main() -> None:\n    pass\n", 2, "a public function must return int, str or bool"),
    ("no public function", "def f() -> int:\n    return 0\n", 1, "no @public function"),
    ("no function", "X = 1\n", 1, "defines no functions"),
    ("duplicate parameter", MAIN + "def f(a: int, a: int) -> None:\n    pass\n", 4, "duplicate parameter a"),
    ("break outside loop", body("break"), 3, "break outside a loop"),
    ("continue outside loop", body("continue"), 3, "continue outside a loop"),
    ("while else", body("while False:\n    pass\nelse:\n    pass\nreturn 0"), 3, "while ... else"),
    ("for else", body("for i in range(2):\n    pass\nelse:\n    pass\nreturn 0"), 3, "for ... else"),
    ("missing return", body("x = 1\nif x > 0:\n    return 1"), 2, "can reach its end without returning"),
    ("return value from None function", MAIN + "def f() -> None:\n    return 1\n", 5, "returns None"),
    ("bare return in int function", body("return"), 3, "return needs a int value"),
    ("reassign constant", "X = 1\n" + body("X = 2\nreturn X"), 4, "cannot assign to constant X"),
    ("assign function name", MAIN + "def f() -> int:\n    main = 1\n    return 0\n", 5, "cannot assign to function main"),
    ("assign reserved", body("storage = 1\nreturn 0"), 3, "storage cannot be assigned"),
    ("del name", body("x = 1\ndel x\nreturn 0"), 4, "del only removes"),
    ("attribute assignment", body("x = paid\nx.amount = 1\nreturn 0"), 4, "attributes cannot be assigned"),
    ("storage read by index", body("return storage['k']"), 3, "storage.get(key, default)"),
    ("storage augmented", body("storage['k'] += 1\nreturn 0"), 3, "cannot be updated in place"),
    ("revert as value", body("x = revert('no')\nreturn 0"), 3, "revert() can only be used as a statement"),
    ("range outside loop", body("x = range(3)\nreturn 0"), 3, "only be used in a for loop"),
    ("zero range step", body("for i in range(0, 3, 0):\n    pass\nreturn 0"), 3, "step must not be zero"),
    ("calling sender", body("x = sender()\nreturn 0"), 3, "sender is not callable"),
    ("now as value", body("x = now\nreturn 0"), 3, "now is a function"),
    ("function as value", MAIN + "def f() -> int:\n    x = main\n    return 0\n", 5, "function main is not a value"),
    ("slice step", body("s = 'abc'\nreturn len(s[::2])"), 4, "slice steps"),
    ("slice assignment", body("xs = [1]\nxs[0:1] = [2]\nreturn 0"), 4, "slice assignment"),
    ("list += on element", body("xs = [[1]]\nxs[0] += [2]\nreturn 0"), 4, "+= on a list element"),
    ("unknown list method", body("xs = [1]\nxs.sort()\nreturn 0"), 4, "list has no method sort"),
    ("unknown str method", body("return len('a'.title())"), 3, "str has no method title"),
    ("unknown record field", body("return paid.price"), 3, "Paid has no field price"),
    ("unknown namespace function", body("discord.kick(1, sender)\nreturn 0"), 3, "discord has no function kick"),
    ("uncalled namespace member", body("x = discord.send\nreturn 0"), 3, "must be called"),
    ("uncalled method", body("s = 'a'\nx = s.upper\nreturn 0"), 4, "must be called"),
    ("attribute on int", body("x = 1\nreturn x.real"), 4, "int has no attribute real"),
    ("try_call keyword", body("r = try_call(sender, 'f', [], value=1)\nreturn 0"), 3, "accepts only the keywords attach and gas"),
    ("attach not a pair", body("r = try_call(sender, 'f', [], attach=1)\nreturn 0"), 3, "attach must be a pair"),
    ("call argument type", body("r = try_call(sender, 'f', [[1]])\nreturn 0"), 3, "contract call arguments must be"),
    ("too many call arguments", body("r = try_call(sender, 'f', [" + ", ".join(["1"] * 17) + "])\nreturn 0"), 3, "at most 16 arguments"),
    ("expression statement", body("1 + 2\nreturn 0"), 3, "has no effect"),
    ("None value", body("x = None\nreturn 0"), 3, "None is not a value"),
    ("None call result", MAIN + "def f() -> None:\n    pass\ndef g() -> int:\n    return f()\n", 7, "returns None and has no value"),
    ("integer overflow", body("return " + "9" * 80), 3, "integer constant outside"),
    ("constant overflow", "X = 2 * " + "9" * 77 + "\n" + MAIN, 1, "integer constant outside"),
    ("division by zero", "X = 1 // 0\n" + MAIN, 1, "division by zero in a constant expression"),
    ("constant string too long", "X = '" + "a" * 4000 + "' + '" + "a" * 100 + "'\n" + MAIN, 1, "string constant longer than 4096"),
    ("syntax error", body("return (1"), 3, "syntax error"),
    ("chained assignment to subscript", body("xs = [1]\na = xs[0] = 1\nreturn 0"), 4, "chained assignment is only supported to names"),
    ("loop over int", body("for i in 5:\n    pass\nreturn 0"), 3, "cannot iterate over int"),
    ("type parameters", "@public\ndef main[T]() -> int:\n    return 0\n", 2, "type parameters"),
]


@pytest.mark.parametrize(("source", "line", "fragment"), [case[1:] for case in REJECTIONS], ids=[case[0] for case in REJECTIONS])
def test_rejections(source: str, line: int, fragment: str) -> None:
    error = compile_error(source)
    assert fragment in error.message
    assert error.line == line


TYPE_ERRORS: list[tuple[str, str, str]] = [
    ("int plus bool", body("x = 1\nreturn x + True"), "unsupported operand types for +: int and bool"),
    ("str plus int", body("s = 'a'\nreturn len(s + 1)"), "unsupported operand types for +: str and int"),
    ("bool arithmetic", body("x = True\nreturn x * 2"), "unsupported operand types for *: bool and int"),
    ("account and int", body("return int(sender == 5)"), "cannot compare Account and int with =="),
    ("account and currency", body("return int(sender == paid.currency)"), "cannot compare Account and Currency"),
    ("list equality", body("xs = [1]\nreturn int(xs == xs)"), "== compares int, str, bool, Account or Currency values, not list[int]"),
    ("ordering bools", body("x = True\nreturn int(x < x)"), "< compares two ints or two strs"),
    ("variable retyped", body("x = 1\nx = 'a'\nreturn 0"), "assignment to x: expected int, got str"),
    ("int condition", body("x = 1\nif x:\n    return 1\nreturn 0"), "a condition must be a bool, not int"),
    ("int while condition", body("x = 1\nwhile x:\n    x -= 1\nreturn 0"), "a condition must be a bool, not int"),
    ("not int", body("x = 1\nreturn int(not x)"), "not needs a bool"),
    ("and on ints", body("x = 1\nreturn int(x and x)"), "and needs bool operands"),
    ("wrong return type", body("return 'x'"), "return value: expected int, got str"),
    ("unnarrowed value return", body("return call(sender, 'f')"), "a Value must be narrowed"),
    ("unnarrowed value arithmetic", body("v = call(sender, 'f')\nreturn v + 1"), "unsupported operand types for +: Value and int"),
    ("unnarrowed value argument", body("transfer(sender, paid.currency, call(sender, 'f'))\nreturn 0"), "a Value must be narrowed"),
    ("empty list", body("xs = []\nreturn 0"), "cannot infer the element type of an empty list"),
    ("empty dict", body("d = {}\nreturn 0"), "cannot infer the types of an empty dict"),
    ("mixed list", body("xs = [1, 'a']\nreturn 0"), "list element: expected int, got str"),
    ("mixed dict values", body("d = {'a': 1, 'b': True}\nreturn 0"), "dict value: expected int, got bool"),
    ("append wrong type", body("xs = [1]\nxs.append('a')\nreturn 0"), "append() argument: expected int, got str"),
    ("index with str", body("xs = [1]\nreturn xs['a']"), "list index: expected int, got str"),
    ("dict key type", body("d = {'a': 1}\nreturn d[1]"), "dict key: expected str, got int"),
    ("str item assignment", body("s = 'ab'\ns[0] = 'c'\nreturn 0"), "str does not support item assignment"),
    ("builtin argument type", body("transfer(sender, 1, 2)\nreturn 0"), "argument currency of transfer() must be Currency, not int"),
    ("builtin arity", body("transfer(sender, paid.currency)\nreturn 0"), "transfer() takes 3 arguments, got 2"),
    ("function arity", MAIN + "def f(a: int) -> int:\n    return f()\n", "f() takes 1 argument, got 0"),
    ("function argument type", MAIN + "def f(a: int) -> int:\n    return f('x')\n", "argument a of f(): expected int, got str"),
    ("dict.get default", body("d = {'a': 1}\nreturn d.get('a', 'x')"), "argument default of dict.get() must be int, not str"),
    ("account from str", body("return int(Account('5'))"), "argument id of Account() must be int, not str"),
    ("in on list of other type", body("xs = [1]\nreturn int('a' in xs)"), "unsupported operand types for in: str and list[int]"),
    ("ternary branches", body("x = True\ny = 1 if x else 'a'\nreturn 0"), "both branches of a conditional expression"),
    ("store a record", body("storage['p'] = paid\nreturn 0"), "a Paid cannot be stored"),
    ("store a record default", body("p = storage.get('p', paid)\nreturn 0"), "a Paid cannot be stored"),
    ("len of int", body("return len(5)"), "len() needs a str, list or dict, not int"),
    ("int of list", body("return int([1])"), "int() cannot convert list[int]"),
    ("str of list", body("return len(str([1]))"), "str() cannot convert list[int]"),
    ("f-string of list", body("return len(f'{[1]}')"), "an f-string can only format"),
    ("call a local", body("x = 1\nreturn x()"), "x is a int, not a function"),
    ("shadowed builtin is not callable", "@public\ndef main(currency: Currency) -> int:\n    c = currency('RAPID')\n    return 0\n", "currency is a Currency, not a function"),
    ("type never inferred", body("x = x + 1\nreturn x"), "the type of x cannot be inferred where it is used"),
    ("redeclared annotation", body("x: int = 1\nx: str = 'a'\nreturn 0"), "x is already declared as int"),
    ("range of str", body("for i in range('a'):\n    pass\nreturn 0"), "range() argument: expected int, got str"),
    ("loop variable type", body("x = 'a'\nfor x in range(3):\n    pass\nreturn 0"), "x has type str, not int"),
    ("call target", body("r = try_call(1, 'f')\nreturn 0"), "try_call() target: expected Account, got int"),
    ("attach amount", body("r = try_call(sender, 'f', [], attach=(paid.currency, 'x'))\nreturn 0"), "attached amount: expected int, got str"),
    ("comparison of value", body("v = call(sender, 'f')\nreturn int(v == 1)"), "cannot compare Value and int"),
    ("bool of account", body("return int(bool(sender))"), "bool() cannot convert Account"),
    ("constant annotation mismatch", "X: str = 1\n" + MAIN, "constant X is declared str but its value is int"),
]


@pytest.mark.parametrize(("source", "fragment"), [case[1:] for case in TYPE_ERRORS], ids=[case[0] for case in TYPE_ERRORS])
def test_type_errors(source: str, fragment: str) -> None:
    assert fragment in compile_error(source).message


def test_error_location_and_format() -> None:
    error = compile_error("@public\ndef main() -> int:\n    x = 1\n    return x + 'a'\n")
    assert (error.line, error.column) == (4, 12)
    assert str(error) == "test.py:4:12: unsupported operand types for +: int and str"
    assert error.filename == "test.py"


def test_the_first_error_is_reported() -> None:
    error = compile_error("@public\ndef main() -> int:\n    x = 1.5\n    y = 2 / 3\n    return 0\n")
    assert error.line == 3 and "floats" in error.message


def test_errors_in_unreachable_code_are_reported() -> None:
    error = compile_error(body("return 1\nx = 'a' + 1"))
    assert error.line == 4


def test_syntax_error_position() -> None:
    error = compile_error("@public\ndef main() -> int:\n    return 1 +\n")
    assert error.line == 3 and error.message.startswith("syntax error")


def test_null_bytes_are_rejected() -> None:
    with pytest.raises(CompileError, match="null bytes"):
        compile_source("X = 1\0\n")


def test_deep_nesting_is_a_compile_error() -> None:
    constant = "(" * 150 + "1" + " + 1)" * 150
    assert compile_source(body(f"x = 1\nreturn x + {constant}"))
    nested = "x" + " + (x" * 140 + ")" * 140
    error = compile_error(body(f"x = 1\nreturn {nested}"))
    assert "too deeply nested" in error.message


def test_too_many_locals() -> None:
    lines = "\n".join(f"v{i} = {i}" for i in range(300))
    assert "more than 256 local variables" in compile_error(body(lines + "\nreturn 0")).message
