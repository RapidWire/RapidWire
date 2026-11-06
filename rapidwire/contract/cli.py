"""The rwc developer command: compile contract sources, check and disassemble binaries, run contracts on the in-memory mock ledger, and write the editor stub."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, fields
from importlib import resources
from pathlib import Path
from typing import Any

from rapidwire.contract.assembler import disassemble
from rapidwire.contract.compiler import STUB_MODULE, CompileError, compile_source
from rapidwire.contract.errors import VerificationError, ascii_safe
from rapidwire.contract.format import HEADER, HEADER_SIZE, MAGIC
from rapidwire.contract.mock import DEFAULT_GAS, DEFAULT_MAX_SIZE, GAS_CURRENCY_ID, MAX_GAS, CallRecord, MockLedger, RunResult, dump_state, load_state, to_json
from rapidwire.contract.values import Account, Attachment, Currency, Scalar
from rapidwire.contract.verifier import VerifiedProgram, verify

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

DEFAULT_SELF = 1000
DEFAULT_SENDER = 2000
DEFAULT_SENDER_FUNDS = 1_000_000_000
STUB_FILE = f"{STUB_MODULE}.pyi"

ARG_TYPES = ("int", "str", "bool", "account", "currency")


class UsageError(Exception):
    """A command line value that cannot be used; reported with exit code 2."""


def _err(message: str) -> None:
    print(ascii_safe(message), file=sys.stderr)


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


def _compile_error(exc: CompileError, source: str | None) -> None:
    """Print a compile error, with its source line and a caret under its column when the source is known."""
    _err(str(exc))
    lines = re.split(r"\r\n|\r|\n", source) if source is not None else []
    if 1 <= exc.line <= len(lines):
        text = lines[exc.line - 1]
        _err(f"    {text}")
        # The line is printed ASCII-escaped, so the caret is indented by the escaped width of the text before the column.
        _err("    " + " " * len(ascii_safe(text[: max(exc.column - 1, 0)])) + "^")


def _load_program(path: str, max_size: int) -> VerifiedProgram:
    """Verify the binary at path, or compile it first when it is source; raises CompileError, VerificationError or OSError."""
    data = _read(path)
    if not data.startswith(MAGIC):
        data = compile_source(data.decode("utf-8"), filename=path)
    return verify(data, max_size=max_size)


def _compile(args: argparse.Namespace) -> int:
    source_path = Path(args.source)
    out = Path(args.output) if args.output else source_path.with_suffix(".rwc")
    if out.resolve() == source_path.resolve():
        raise UsageError(f"the output {out} is the source file itself; choose another path with -o")
    source = source_path.read_text(encoding="utf-8")
    try:
        binary = compile_source(source, filename=args.source)
    except CompileError as exc:
        _compile_error(exc, source)
        return EXIT_FAILURE
    out.write_bytes(binary)
    print(ascii_safe(f"wrote {out} ({len(binary)} bytes, sha256 {hashlib.sha256(binary).hexdigest()})"))
    if len(binary) > args.max_size:
        _err(f"warning: the binary is {len(binary)} bytes, larger than the {args.max_size}-byte registration limit")
    return EXIT_OK


def _verify(args: argparse.Namespace) -> int:
    data = _read(args.binary)
    try:
        program = verify(data, max_size=args.max_size)
    except VerificationError as exc:
        _err(f"error: {exc}")
        return EXIT_FAILURE
    print(f"OK sha256 {program.sha256} size {len(data)}")
    return EXIT_OK


def _disasm(args: argparse.Namespace) -> int:
    data = _read(args.binary)
    try:
        program = verify(data, max_size=args.max_size)
    except VerificationError as exc:
        _err(f"error: {exc}")
        return EXIT_FAILURE
    print(disassembly(program))
    return EXIT_OK


def disassembly(program: VerifiedProgram) -> str:
    """The header fields and function table of program followed by its assembler listing."""
    magic, version, flags, pool_length, table_length, code_length = HEADER.unpack_from(program.data, 0)
    lines = [
        f"; header: magic {magic.decode('ascii')}, format version {version}, flags {flags}, constant pool {pool_length} bytes, function table {table_length} bytes, code {code_length} bytes",
        f"; header size {HEADER_SIZE}, total size {len(program.data)}, sha256 {program.sha256}",
        "; function table:",
    ]
    for fn in program.functions:
        visibility = "public" if fn.public else "internal"
        start = fn.entry.code_offset
        lines.append(f";   {fn.index}: {visibility} {fn.signature()}, locals {fn.local_count}, max stack {fn.max_stack}, code {start}..{start + fn.entry.code_length - 1}")
    return "\n".join(lines) + "\n" + disassemble(program)


def _stub(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / STUB_FILE
    target.write_text(stub_text(), encoding="utf-8")
    print(ascii_safe(f"wrote {target}"))
    return EXIT_OK


def stub_text() -> str:
    """The packaged type stub of the contract language."""
    return resources.files("rapidwire.contract").joinpath(STUB_FILE).read_text(encoding="utf-8")


def _integer(text: str, what: str) -> int:
    try:
        return int(text, 10)
    except ValueError:
        raise UsageError(f"{what} must be an integer, not {text!r}") from None


def _currency_ref(ledger: MockLedger, text: str) -> Currency:
    try:
        return Currency(ledger.resolve_currency(int(text, 10) if text.lstrip("-").isdigit() else text))
    except KeyError as exc:
        raise UsageError(str(exc.args[0])) from None


def parse_arg(ledger: MockLedger, text: str) -> Scalar:
    """A TYPE:VALUE command line argument as a VM value; a currency is an ID or a symbol of the ledger."""
    kind, sep, value = text.partition(":")
    if not sep or kind not in ARG_TYPES:
        raise UsageError(f"--arg must be TYPE:VALUE with TYPE one of {', '.join(ARG_TYPES)}, not {text!r}")
    if kind == "int":
        return _integer(value, "an int argument")
    if kind == "str":
        return value
    if kind == "bool":
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
        raise UsageError(f"a bool argument must be true or false, not {value!r}")
    if kind == "account":
        ident = _integer(value, "an account argument")
        if ident < 1 or ident > 2**63 - 1:
            raise UsageError("an account ID must be a positive 64-bit integer")
        return Account(ident)
    return _currency_ref(ledger, value)


def _run(args: argparse.Namespace) -> int:
    if args.gas > MAX_GAS:
        raise UsageError(f"--gas must be at most {MAX_GAS}, the largest gas limit the server accepts")
    ledger = MockLedger(seed=args.seed, max_size=args.max_size)
    now: int | None = None
    if args.state:
        state_path = Path(args.state)
        document = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("the state file must hold a JSON object")
        now = load_state(ledger, document, base_dir=state_path.parent)
    else:
        ledger.add_currency(GAS_CURRENCY_ID, "RAPID", name="Rapid")
        ledger.set_balance(args.sender, GAS_CURRENCY_ID, DEFAULT_SENDER_FUNDS)
    if args.now is not None:
        now = args.now
    program = _load_program(args.program, args.max_size)
    ledger.deploy(args.self_id, program)
    for guild in args.allow_discord:
        ledger.allow_discord(guild, args.self_id)
    call_args = [parse_arg(ledger, text) for text in args.arg]
    attach: Attachment | None = None
    if args.pay:
        currency_text, sep, amount_text = args.pay.rpartition(":")
        if not sep:
            raise UsageError(f"--pay must be CURRENCY:AMOUNT, not {args.pay!r}")
        amount = _integer(amount_text, "the --pay amount")
        if amount <= 0:
            raise UsageError("the --pay amount must be positive")
        attach = Attachment(_currency_ref(ledger, currency_text), amount)
    result = asyncio.run(ledger.run(args.self_id, args.function, call_args, sender=args.sender, attach=attach, gas=args.gas, now=now))
    report = run_report(result, ledger)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return EXIT_OK if result.ok else EXIT_FAILURE


def run_report(result: RunResult, ledger: MockLedger) -> dict[str, Any]:
    """The JSON report of a run: outcome, gas, random values, outbox, transfers, nested calls, and the final balances and storage."""
    final = dump_state(ledger)
    return {
        "ok": result.ok,
        "result": result.value,
        "error": result.error or None,
        "message": result.message or None,
        "failed_function": result.failed_function,
        "failed_offset": result.failed_offset,
        "gas_used": result.gas_used,
        "gas_limit": result.gas_limit,
        "gas_fee": result.gas_fee,
        "random_values": list(result.random_values),
        "outbox": [{key: value for key, value in asdict(entry).items() if value is not None} for entry in result.outbox],
        "transfers": [asdict(item) for item in result.transfers],
        "calls": [_call_report(call) for call in result.calls],
        "balances": final["balances"],
        "storage": final["storage"],
    }


def _call_report(call: CallRecord) -> dict[str, Any]:
    # Built field by field: dataclasses.asdict would try to rebuild the VM's Account and Currency values in the arguments.
    report = {f.name: getattr(call, f.name) for f in fields(call)}
    report["args"] = [to_json(arg) for arg in call.args]
    report["attach"] = list(call.attach) if call.attach else None
    return report


def _positive_int(text: str) -> int:
    value = int(text, 10)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _account_id(text: str) -> int:
    value = _positive_int(text)
    if value > 2**63 - 1:
        raise argparse.ArgumentTypeError("must fit in a signed 64-bit integer")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rwc", description="RapidWire contract compiler, checker, disassembler and local runner.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    compile_cmd = commands.add_parser(
        "compile", help="Compile a contract source to an RWC1 binary.", description="Compile a contract source to an RWC1 binary; errors are printed as FILE:LINE:COLUMN: message."
    )
    compile_cmd.add_argument("source", help="contract source file")
    compile_cmd.add_argument("-o", "--output", help="binary to write (default: the source path with the .rwc suffix)")
    compile_cmd.add_argument("--max-size", type=_positive_int, default=DEFAULT_MAX_SIZE, help=f"warn when the binary is larger than this (default {DEFAULT_MAX_SIZE})")
    compile_cmd.set_defaults(handler=_compile)

    verify_cmd = commands.add_parser(
        "verify", help="Run the server's registration checks on a binary.", description="Run the server's registration checks; prints OK with the SHA-256 and size, or the error."
    )
    verify_cmd.add_argument("binary", help="RWC1 binary")
    verify_cmd.add_argument("--max-size", type=_positive_int, default=DEFAULT_MAX_SIZE, help=f"size limit in bytes (default {DEFAULT_MAX_SIZE})")
    verify_cmd.set_defaults(handler=_verify)

    disasm_cmd = commands.add_parser(
        "disasm", help="Print the header, function table and instructions of a binary.", description="Print the header, constant pool, function table and instruction listing of a verified binary."
    )
    disasm_cmd.add_argument("binary", help="RWC1 binary")
    disasm_cmd.add_argument("--max-size", type=_positive_int, default=DEFAULT_MAX_SIZE, help=f"size limit in bytes (default {DEFAULT_MAX_SIZE})")
    disasm_cmd.set_defaults(handler=_disasm)

    run_cmd = commands.add_parser(
        "run",
        help="Run a public function on the in-memory mock ledger.",
        description=(
            "Run a public function of a source or binary on the mock ledger and print a JSON report; exits 0 when the call succeeds and 1 when it fails."
            f" Without --state the ledger has the currency RAPID (ID {GAS_CURRENCY_ID}) and the sender holds {DEFAULT_SENDER_FUNDS} of it."
            " The state file is the JSON document described by rapidwire.contract.mock.load_state."
            " As on the server, Discord operations (send, add_role, remove_role, has_role) fail with permission_denied in a guild that has not allowed the contract;"
            " grant it with --allow-discord GUILD or a [guild, contract] pair in the state's discord_permissions, as a guild administrator does with /discord-permission allow."
            f" As on the server, the gas limit is 1 to {MAX_GAS}, the sender must hold the limit's cost (plus an attachment in the gas currency) before the call starts,"
            " and the fee for the gas used is burned from the sender whether the call succeeds or fails; with --state the gas currency is the state's gas_currency."
        ),
    )
    run_cmd.add_argument("program", help="contract source or RWC1 binary")
    run_cmd.add_argument("--function", required=True, help="public function to call")
    run_cmd.add_argument("--arg", action="append", default=[], metavar="TYPE:VALUE", help=f"an argument, TYPE one of {', '.join(ARG_TYPES)}; repeat in order")
    run_cmd.add_argument("--sender", type=_account_id, default=DEFAULT_SENDER, help=f"calling account (default {DEFAULT_SENDER})")
    run_cmd.add_argument("--self", dest="self_id", type=_account_id, default=DEFAULT_SELF, help=f"account the contract is registered to (default {DEFAULT_SELF})")
    run_cmd.add_argument("--pay", metavar="CURRENCY:AMOUNT", help="attach AMOUNT of CURRENCY (ID or symbol)")
    run_cmd.add_argument("--gas", type=_positive_int, default=DEFAULT_GAS, help=f"gas limit (default {DEFAULT_GAS})")
    run_cmd.add_argument("--state", help="JSON file seeding currencies, balances, roles, other contracts and storage")
    run_cmd.add_argument("--allow-discord", action="append", default=[], type=_account_id, metavar="GUILD", help="allow the contract's Discord operations in GUILD; repeat for more guilds")
    run_cmd.add_argument("--now", type=int, help="execution time in Unix seconds (default: the state's now, else the current time)")
    run_cmd.add_argument("--seed", type=int, help="seed random() for a reproducible run")
    run_cmd.add_argument("--max-size", type=_positive_int, default=DEFAULT_MAX_SIZE, help=f"binary size limit in bytes (default {DEFAULT_MAX_SIZE})")
    run_cmd.set_defaults(handler=_run)

    stub_cmd = commands.add_parser(
        "stub", help=f"Write {STUB_FILE} for editors and type checkers.", description=f"Write {STUB_FILE} into DIRECTORY so 'from {STUB_MODULE} import *' resolves next to the contract sources."
    )
    stub_cmd.add_argument("directory", nargs="?", default=".", help="directory to write into (default: the current directory)")
    stub_cmd.set_defaults(handler=_stub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one rwc command and return its exit code: 0 success, 1 failure, 2 usage error."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        result: int = args.handler(args)
    except UsageError as exc:
        _err(f"rwc {args.command}: {exc}")
        return EXIT_USAGE
    except CompileError as exc:
        try:
            source: str | None = Path(exc.filename).read_text(encoding="utf-8")
        except (OSError, ValueError):
            source = None
        _compile_error(exc, source)
        return EXIT_FAILURE
    except (VerificationError, ValueError, OSError) as exc:
        _err(f"rwc {args.command}: {exc}")
        return EXIT_FAILURE
    return result


if __name__ == "__main__":
    sys.exit(main())
