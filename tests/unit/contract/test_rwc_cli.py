from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rapidwire.contract.cli import DEFAULT_SELF, DEFAULT_SENDER, DEFAULT_SENDER_FUNDS, main, stub_text
from rapidwire.contract.compiler import STUB_MODULE
from rapidwire.contract.mock import GAS_CURRENCY_ID, ISSUANCE_ACCOUNT_BASE, MAX_GAS
from tests.unit.contract.compiled import FAILING_SHOP_SOURCE, TRY_CALL_SOURCE, VENDING_SOURCE, source


def write(path: Path, text: str) -> Path:
    path.write_text(source(text), encoding="utf-8")
    return path


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_compile_writes_the_binary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    assert main(["compile", str(src)]) == 0
    out = capsys.readouterr().out
    binary = tmp_path / "vending.rwc"
    assert binary.read_bytes().startswith(b"RWC1")
    assert f"wrote {binary}" in out and "sha256" in out
    target = tmp_path / "custom.bin"
    assert main(["compile", str(src), "-o", str(target)]) == 0
    assert target.read_bytes() == binary.read_bytes()


def test_compile_errors_are_line_numbered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "bad.py", "@public\ndef main() -> int:\n    return 1 + 'a'\n")
    assert main(["compile", str(src)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == f"{src}:3:12: unsupported operand types for +: int and str"
    assert err[1] == "        return 1 + 'a'"
    assert err[2] == "    " + " " * 11 + "^"
    assert not (tmp_path / "bad.rwc").exists()


def test_compile_warns_about_the_size_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    assert main(["compile", str(src), "--max-size", "50"]) == 0
    assert "larger than the 50-byte registration limit" in capsys.readouterr().err


def test_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    main(["compile", str(src)])
    capsys.readouterr()
    binary = tmp_path / "vending.rwc"
    assert main(["verify", str(binary)]) == 0
    out = capsys.readouterr().out.split()
    assert out[0] == "OK" and out[1] == "sha256" and len(out[2]) == 64 and out[3:] == ["size", str(binary.stat().st_size)]
    assert main(["verify", str(binary), "--max-size", "10"]) == 1
    assert "larger than the 10-byte limit" in capsys.readouterr().err
    broken = tmp_path / "broken.rwc"
    broken.write_bytes(binary.read_bytes()[:-1])
    assert main(["verify", str(broken)]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_disasm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    main(["compile", str(src)])
    capsys.readouterr()
    assert main(["disasm", str(tmp_path / "vending.rwc")]) == 0
    out = capsys.readouterr().out
    assert "; header: magic RWC1, format version 1, flags 0" in out
    assert "; function table:" in out and ";   0: public buy() -> str" in out
    assert '.const "Send at least 100 RAPID"' in out
    assert "CALL_BUILTIN currency 1" in out and "; @0" in out and "JUMP_IF_TRUE L" in out
    garbage = tmp_path / "garbage.rwc"
    garbage.write_bytes(b"nope")
    assert main(["disasm", str(garbage)]) == 1


def test_run_the_vending_machine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    code, denied = run_json(capsys, "run", str(src), "--function", "buy", "--pay", "RAPID:150000")
    assert code == 1 and "permission_denied" in denied["message"] and denied["outbox"] == []
    code, report = run_json(capsys, "run", str(src), "--function", "buy", "--pay", "RAPID:150000", "--allow-discord", "123456789012345678")
    assert code == 0
    assert report["ok"] is True and report["result"] == "OK" and report["gas_used"] == 29 and report["gas_limit"] == 128
    assert report["outbox"] == [{"kind": "discord_add_role", "contract_id": DEFAULT_SELF, "guild": 123456789012345678, "user": DEFAULT_SENDER, "role": 234567890123456789}]
    assert report["transfers"][:2] == [
        {"kind": "attachment", "from_id": DEFAULT_SENDER, "to_id": DEFAULT_SELF, "currency_id": GAS_CURRENCY_ID, "amount": 150000},
        {"kind": "transfer", "from_id": DEFAULT_SELF, "to_id": DEFAULT_SENDER, "currency_id": GAS_CURRENCY_ID, "amount": 50000},
    ]
    assert report["transfers"][2:] == [{"kind": "gas", "from_id": DEFAULT_SENDER, "to_id": ISSUANCE_ACCOUNT_BASE - GAS_CURRENCY_ID, "currency_id": GAS_CURRENCY_ID, "amount": 29}]
    assert {"account": DEFAULT_SENDER, "currency": GAS_CURRENCY_ID, "amount": DEFAULT_SENDER_FUNDS - 100000 - 29} in report["balances"]
    code, report = run_json(capsys, "run", str(src), "--function", "buy", "--pay", f"{GAS_CURRENCY_ID}:10")
    assert code == 1
    assert (report["ok"], report["error"], report["message"], report["outbox"]) == (False, "REVERTED", "Send at least 100 RAPID", [])
    assert [transfer["kind"] for transfer in report["transfers"]] == ["gas"]


def test_run_from_a_binary_with_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(
        tmp_path / "args.py",
        """
        @public
        def main(a: int, b: str, c: bool, d: Account, e: Currency) -> str:
            storage["last"] = b
            return str(a) + b + str(c) + str(d) + str(e) + str(random(1, 1000))
        """,
    )
    main(["compile", str(src)])
    capsys.readouterr()
    argv = ["run", str(tmp_path / "args.rwc"), "--function", "main", "--arg", "int:-5", "--arg", "str:x:y", "--arg", "bool:true", "--arg", "account:77", "--arg", "currency:RAPID", "--seed", "4"]
    code, first = run_json(capsys, *argv, "--gas", "1000")
    assert code == 0
    roll = first["random_values"][0]
    assert first["result"] == f"-5x:yTrue77{GAS_CURRENCY_ID}{roll}"
    assert first["storage"] == {str(DEFAULT_SELF): {"last": "x:y"}}
    _, second = run_json(capsys, *argv, "--gas", "1000")
    assert second["random_values"] == first["random_values"]


def test_run_with_a_state_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path / "shop.py", FAILING_SHOP_SOURCE)
    src = write(tmp_path / "caller.py", TRY_CALL_SOURCE)
    state = {
        "now": 1234,
        "gas_currency": 1,
        "currencies": [{"id": 1, "symbol": "RAPID"}],
        "balances": [{"account": 2002, "currency": "RAPID", "amount": 500000}],
        "contracts": [{"account": 3003, "source": "shop.py"}],
        "roles": [[1, 2002, 3]],
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, report = run_json(capsys, "run", str(src), "--function", "buy_from_shop", "--state", str(state_path), "--sender", "2002", "--self", "1001", "--pay", "RAPID:100000", "--gas", "10000")
    assert code == 0
    assert report["result"] == "shop failed: out of stock"
    calls = report["calls"]
    assert [(c["depth"], c["function"], c["ok"], c["error"]) for c in calls] == [(2, "buy", False, "REVERTED"), (1, "buy_from_shop", True, "")]
    assert {"account": 2002, "currency": 1, "amount": 500000 - report["gas_fee"]} in report["balances"]


def test_run_reports_refused_nested_calls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "self_call.py", '@public\ndef main() -> str:\n    return try_call(self, "main").message + "|" + try_call(self, "bad name").message\n')
    code, report = run_json(capsys, "run", str(src), "--function", "main", "--gas", "1000")
    assert code == 0
    assert report["result"] == f"contract {DEFAULT_SELF} is already executing|contract {DEFAULT_SELF} is already executing"
    refused = [(c["depth"], c["contract_id"], c["function"], c["args"], c["ok"], c["error"], c["gas_used"], c["random_values"]) for c in report["calls"] if not c["ok"]]
    assert refused == [(2, DEFAULT_SELF, "main", [], False, "CALL_FAILED", 0, []), (2, DEFAULT_SELF, "'bad name'", [], False, "CALL_FAILED", 0, [])]


def test_run_reports_runtime_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "spin.py", "@public\ndef main() -> int:\n    while True:\n        pass\n")
    code, report = run_json(capsys, "run", str(src), "--function", "main", "--gas", "50")
    assert code == 1
    assert (report["error"], report["gas_used"], report["failed_function"]) == ("OUT_OF_GAS", 50, "main")


@pytest.mark.parametrize(
    "extra",
    [
        ["--arg", "float:1"],
        ["--arg", "int:x"],
        ["--arg", "bool:maybe"],
        ["--arg", "account:0"],
        ["--arg", "currency:NOPE"],
        ["--pay", "RAPID"],
        ["--pay", "RAPID:-1"],
        ["--gas", "0"],
    ],
)
def test_run_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    assert main(["run", str(src), "--function", "buy", *extra]) == 2
    assert capsys.readouterr().err


def test_usage_and_input_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert main(["run", "x.py"]) == 2
    assert main(["nonsense"]) == 2
    assert main(["compile", str(tmp_path / "missing.py")]) == 1
    assert main(["verify", str(tmp_path / "missing.rwc")]) == 1
    bad_state = tmp_path / "state.json"
    bad_state.write_text("[]", encoding="utf-8")
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    assert main(["run", str(src), "--function", "buy", "--state", str(bad_state)]) == 1
    broken = write(tmp_path / "broken.py", "@public\ndef main() -> int:\n    return x\n")
    assert main(["run", str(broken), "--function", "main"]) == 1
    assert f"{broken}:3:12: name x is not defined" in capsys.readouterr().err


def test_compile_refuses_to_overwrite_the_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.rwc", VENDING_SOURCE)
    before = src.read_bytes()
    assert main(["compile", str(src)]) == 2
    assert main(["compile", str(src), "-o", str(tmp_path / "." / "vending.rwc")]) == 2
    assert "is the source file itself" in capsys.readouterr().err
    assert src.read_bytes() == before


def test_run_prints_compile_errors_with_a_caret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "bad.py", "@public\ndef main() -> int:\n    return 1 + 'a'\n")
    assert main(["run", str(src), "--function", "main"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [f"{src}:3:12: unsupported operand types for +: int and str", "        return 1 + 'a'", "    " + " " * 11 + "^"]


def test_caret_lines_up_under_escaped_non_ascii_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accented = chr(0xE9) * 2
    src = tmp_path / "accent.py"
    src.write_text(f"@public\ndef main() -> int:\n    s = '{accented}'; return s + 1\n", encoding="utf-8")
    assert main(["compile", str(src)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[1] == "        s = '\\xe9\\xe9'; return s + 1"
    assert err[2].index("^") == err[1].index("s + 1")


def test_run_rejects_a_gas_limit_above_the_server_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write(tmp_path / "vending.py", VENDING_SOURCE)
    assert main(["run", str(src), "--function", "buy", "--gas", str(MAX_GAS + 1)]) == 2
    assert f"at most {MAX_GAS}" in capsys.readouterr().err


def test_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["stub", str(tmp_path / "editor")]) == 0
    assert (tmp_path / "editor" / f"{STUB_MODULE}.pyi").read_text(encoding="utf-8") == stub_text()
    assert "wrote" in capsys.readouterr().out


def test_module_entry_point() -> None:
    completed = subprocess.run([sys.executable, "-m", "rapidwire.contract.cli", "--help"], capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0
    for command in ("compile", "verify", "disasm", "run", "stub"):
        assert command in completed.stdout
