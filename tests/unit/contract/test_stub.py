from __future__ import annotations

import ast
import builtins
import os
from pathlib import Path

from mypy import api as mypy_api

from rapidwire.contract.builtins import BUILTINS_V1, BUILTINS_V1_BY_NAME
from rapidwire.contract.cli import stub_text
from rapidwire.contract.compiler import FUNCTION_BUILTINS, SPECIAL_FUNCTIONS, STUB_MODULE, TYPE_NAMES, VALUE_BUILTINS, compile_source
from rapidwire.contract.types import KIND_NAMES, RECORD_FIELDS, Kind, Type
from tests.unit.contract.compiled import FAILING_SHOP_SOURCE, TRY_CALL_SOURCE, VENDING_SOURCE, WORKING_SHOP_SOURCE, source

STUB = ast.parse(stub_text())
NAMESPACE_CLASSES = {"storage": "_Storage", "discord": "_Discord"}
LANGUAGE_FORMS = {"public", "revert", "call", "try_call", "Value", "storage", "discord"}


def annotation(t: Type) -> str:
    if t.kind is Kind.NONE:
        return "None"
    if t.kind is Kind.VALUE:
        return "Value"
    if t.args:
        return f"{KIND_NAMES[t.kind]}[{', '.join(annotation(arg) for arg in t.args)}]"
    return KIND_NAMES[t.kind]


def functions(body: list[ast.stmt]) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in body if isinstance(node, ast.FunctionDef)}


def classes() -> dict[str, ast.ClassDef]:
    return {node.name: node for node in STUB.body if isinstance(node, ast.ClassDef)}


def variables() -> dict[str, str]:
    return {node.target.id: ast.unparse(node.annotation) for node in STUB.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}


def signature(node: ast.FunctionDef, *, method: bool) -> tuple[list[tuple[str, str]], str]:
    args = node.args.args[1:] if method else node.args.args
    params = [(arg.arg, ast.unparse(arg.annotation) if arg.annotation is not None else "") for arg in args]
    returns = ast.unparse(node.returns) if node.returns is not None else ""
    return params, returns


def expected_signature(name: str) -> tuple[list[tuple[str, str]], str]:
    builtin = BUILTINS_V1_BY_NAME[name]
    params = [(p.name, "_T" if p.name == "default" else annotation(p.type)) for p in builtin.params]
    has_default = any(p.name == "default" for p in builtin.params)
    return params, "_T" if has_default else annotation(builtin.returns)


def test_every_registry_builtin_is_declared_with_its_signature() -> None:
    module_functions = functions(STUB.body)
    stub_classes = classes()
    stub_variables = variables()
    for builtin in BUILTINS_V1:
        name = builtin.name
        if name in VALUE_BUILTINS:
            assert stub_variables[name] == annotation(builtin.returns), name
        elif name in ("Account", "Currency"):
            init = functions(stub_classes[name].body)["__init__"]
            assert signature(init, method=True) == ([("id", "int")], "None")
        elif name.startswith(("str.", "dict.")):
            # Methods of the Python types of the same name, which the stub does not redeclare.
            owner, method = name.split(".")
            assert hasattr(str if owner == "str" else dict, method), name
        elif name.split(".")[0] in NAMESPACE_CLASSES:
            namespace, method = name.split(".")
            assert stub_variables[namespace] == NAMESPACE_CLASSES[namespace]
            declared = functions(stub_classes[NAMESPACE_CLASSES[namespace]].body)[method]
            assert signature(declared, method=True) == expected_signature(name), name
        else:
            assert signature(module_functions[name], method=False) == expected_signature(name), name


def test_optional_parameters_have_a_short_overload() -> None:
    owners = {"storage": next(node for node in STUB.body if isinstance(node, ast.ClassDef) and node.name == "_Storage").body, "": STUB.body}
    optional = [builtin for builtin in BUILTINS_V1 if builtin.required < builtin.arity]
    assert optional
    for builtin in optional:
        namespace, _, method = builtin.name.rpartition(".")
        body = owners[namespace]
        declared = [node for node in body if isinstance(node, ast.FunctionDef) and node.name == (method or builtin.name)]
        assert [ast.unparse(d) for node in declared for d in node.decorator_list] == ["overload", "overload"], builtin.name
        short = signature(declared[0], method=bool(namespace))
        assert short == ([(p.name, annotation(p.type)) for p in builtin.params[: builtin.required]], "Value"), builtin.name


def test_the_stub_declares_nothing_the_compiler_rejects() -> None:
    declared = set(functions(STUB.body)) | set(classes()) | set(variables())
    declared |= {node.targets[0].id for node in STUB.body if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    public = {name for name in declared if not name.startswith("_")}
    known = set(FUNCTION_BUILTINS) | VALUE_BUILTINS | set(TYPE_NAMES) | SPECIAL_FUNCTIONS | LANGUAGE_FORMS
    assert public <= known, public - known
    assert {name for name in TYPE_NAMES if not hasattr(builtins, name)} <= declared


def test_record_classes_match_the_record_layouts() -> None:
    stub_classes = classes()
    for kind, layout in RECORD_FIELDS.items():
        properties = functions(stub_classes[KIND_NAMES[kind]].body)
        assert list(properties) == [f.name for f in layout], kind
        for record_field in layout:
            assert signature(properties[record_field.name], method=True) == ([], annotation(record_field.type)), (kind, record_field.name)


def test_the_stub_is_packaged_next_to_the_compiler() -> None:
    packaged = Path(__file__).resolve().parents[3] / "rapidwire" / "contract" / f"{STUB_MODULE}.pyi"
    assert packaged.read_text(encoding="utf-8") == stub_text()


BROAD_SAMPLE = """
from rapidwire_contract import *

LIMIT = 10
NAME = "pool"


def total(values: list[int]) -> int:
    result = 0
    for v in values:
        result += v
    return result


@public
def deposit(amount: int, who: Account, cur: Currency) -> str:
    assert amount > 0, "amount must be positive"
    history: list[int] = storage.get("history", [])
    history.append(amount)
    storage["history"] = history
    owners: dict[str, int] = storage.get("owners", {})
    owners[str(who)] = owners.get(str(who), 0) + amount
    storage["owners"] = owners
    squares = [x * x for x in history if x < LIMIT]
    names = {str(i): i for i in range(3)}
    info = currency_info(cur)
    q = quote(cur, currency("GOLD"), amount)
    if discord.has_role(1, who, 2) and q.amount_out > 0:
        discord.send(1, 2, f"{info.symbol} {amount} from {who}")
    r = try_call(who, "notify", [amount, NAME], gas=100)
    message = r.message if not r.ok else "ok"
    value: int = r.value if r.ok else 0
    del storage["missing"]
    return message + ":" + str(total(squares) + len(names) + value + balance(self, cur) + now() + random(1, 2))
"""


def run_mypy(directory: Path, *files: Path) -> tuple[str, int]:
    config = directory / "mypy.ini"
    config.write_text("[mypy]\n", encoding="utf-8")
    stdout, stderr, status = mypy_api.run(["--strict", "--no-incremental", "--cache-dir", os.devnull, "--config-file", str(config), "--no-site-packages", *(str(f) for f in files)])
    return stdout + stderr, status


def test_mypy_accepts_the_samples_against_the_stub(tmp_path: Path) -> None:
    (tmp_path / f"{STUB_MODULE}.pyi").write_text(stub_text(), encoding="utf-8")
    samples = {
        "vending.py": VENDING_SOURCE,
        "try_call.py": TRY_CALL_SOURCE,
        "failing_shop.py": FAILING_SHOP_SOURCE,
        "working_shop.py": WORKING_SHOP_SOURCE,
        "broad.py": BROAD_SAMPLE,
    }
    paths = []
    for name, text in samples.items():
        path = tmp_path / name
        path.write_text(source(text), encoding="utf-8")
        paths.append(path)
        assert compile_source(source(text), filename=name)
    output, status = run_mypy(tmp_path, *paths)
    assert status == 0, output


def test_mypy_reports_type_errors_against_the_stub(tmp_path: Path) -> None:
    (tmp_path / f"{STUB_MODULE}.pyi").write_text(stub_text(), encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("from rapidwire_contract import *\n\n@public\ndef main() -> str:\n    transfer(sender, 1, 2)\n    return balance(self, currency('X'))\n", encoding="utf-8")
    output, status = run_mypy(tmp_path, bad)
    assert status == 1
    assert 'Argument 2 to "transfer" has incompatible type "int"; expected "Currency"' in output
    assert 'Incompatible return value type (got "int", expected "str")' in output
