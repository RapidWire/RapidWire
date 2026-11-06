"""The contract documents state the implementation's numbers: costs.md holds exactly the opcode and builtin cost tables, the surcharge constants and the gas setting defaults, and builtins.md lists every registry builtin with its exact signature, id and gas."""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType

from rapidwire.config import MAX_GAS_LIMIT, Settings
from rapidwire.contract import costs
from rapidwire.contract.builtins import BUILTINS_V1
from rapidwire.contract.limits import DEFAULT_LIMITS
from rapidwire.contract.opcodes import INSTRUCTION_SET_V1
from rapidwire.contract.runtime import CONTRACT_SWAP_CHUNK_COUNT, CONTRACT_SWAP_MAX_EXPANSIONS, MAX_CONCURRENT_DRY_RUNS, MAX_CONCURRENT_EXECUTIONS, RuntimeConfig
from tests.unit.doc_text import character_violations

DOCS = Path(__file__).resolve().parents[3] / "document" / "contracts"
COSTS = (DOCS / "costs.md").read_text(encoding="utf-8")
BUILTINS = (DOCS / "builtins.md").read_text(encoding="utf-8")
ROW = re.compile(r"^\|(.+)\|$")


def section(text: str, heading: str) -> str:
    """The body of the "## heading" section of text."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return match.group(1)


def rows(text: str) -> list[list[str]]:
    """The body rows of the markdown tables in text, as stripped cells."""
    found = []
    for line in text.splitlines():
        match = ROW.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if all(set(cell) <= {"-"} for cell in cells) or cells[0] in ("命令コード", "組み込み機能", "定数", "設定", "ID"):
            continue
        found.append(cells)
    return found


def code(cell: str) -> str:
    assert cell.startswith("`") and cell.endswith("`"), cell
    return cell[1:-1]


def test_opcode_table_equals_the_instruction_set() -> None:
    documented = {code(cells[0]): (int(cells[1], 16), int(cells[2])) for cells in rows(section(COSTS, "命令のコスト"))}
    expected = {info.mnemonic: (info.code, info.gas) for info in INSTRUCTION_SET_V1.ops.values()}
    assert documented == expected
    assert expected == {name: (INSTRUCTION_SET_V1.by_mnemonic(name).code, gas) for name, gas in costs.OPCODE_GAS.items()}


def test_builtin_cost_table_equals_the_registry() -> None:
    documented = {code(cells[0]): (int(cells[1]), int(cells[2])) for cells in rows(section(COSTS, "組み込み機能のコスト"))}
    assert documented == {builtin.name: (builtin.id, builtin.gas) for builtin in BUILTINS_V1}
    assert {name: gas for name, (_, gas) in documented.items()} == dict(costs.BUILTIN_GAS)


def test_surcharge_constants_equal_the_code() -> None:
    documented = {code(cells[0]): int(cells[1]) for cells in rows(section(COSTS, "加算の定数"))}
    expected = {name: value for name, value in vars(costs).items() if name.isupper() and type(value) is int}
    for table, mapping in vars(costs).items():
        if table.isupper() and isinstance(mapping, MappingProxyType) and table not in ("OPCODE_GAS", "BUILTIN_GAS"):
            expected.update({f"{table}[{name}]": weight for name, weight in mapping.items()})
    assert documented == expected


def test_every_surcharge_function_is_described() -> None:
    surcharges = section(COSTS, "サイズによる加算") + section(COSTS, "コスト 0 の命令の連続")
    functions = [name for name, value in vars(costs).items() if callable(value) and getattr(value, "__module__", None) == costs.__name__]
    assert functions
    for name in functions:
        assert f"`{name}(" in surcharges, name
    for constant in (name for name in vars(costs) if name.isupper() and name not in ("OPCODE_GAS", "BUILTIN_GAS")):
        assert constant in surcharges, constant


def test_gas_settings_equal_the_setting_defaults() -> None:
    documented = {code(cells[0]): int(cells[1]) for cells in rows(section(COSTS, "ガスの設定"))}
    fields = Settings.model_fields
    assert documented == {f"RAPIDWIRE_{name.upper()}": fields[name].default for name in ("gas_price", "default_gas", "max_gas")}
    language = (DOCS / "language.md").read_text(encoding="utf-8")
    assert f"`RAPIDWIRE_MAX_GAS`、既定 {fields['max_gas'].default:,}" in language
    assert f"サーバーの最大値 {fields['max_gas'].default:,} 以下" in (DOCS / "cli.md").read_text(encoding="utf-8")
    assert f"最大値そのものは {MAX_GAS_LIMIT} まで設定できる。" in section(COSTS, "ガスの設定")
    assert f"既定 {fields['max_gas'].default:,}、最大 {MAX_GAS_LIMIT:,} |" in language


def test_runtime_limits_equal_the_code() -> None:
    swap_budget = f"最大 {CONTRACT_SWAP_CHUNK_COUNT} 個の小口に対して最大 {CONTRACT_SWAP_MAX_EXPANSIONS} 回の展開"
    assert swap_budget in section(COSTS, "サイズによる加算")
    assert f"経路の展開を最大 {CONTRACT_SWAP_MAX_EXPANSIONS} 回まで行い、金額を最大 {CONTRACT_SWAP_CHUNK_COUNT} 個の小口に分ける" in BUILTINS
    limits = {cells[0]: cells[1] for cells in rows(section((DOCS / "language.md").read_text(encoding="utf-8"), "上限")) if len(cells) == 2}
    assert limits["最上位の呼び出しあたりのデータベース時間"].startswith(f"{RuntimeConfig().host_time_seconds:g} 秒")
    assert limits["最上位の呼び出しあたりの実時間"].startswith(f"{DEFAULT_LIMITS.wall_clock_seconds:g} 秒")
    assert limits["1 つのプロセスのイベントループ (API、Bot または `worker`) が同時に実行する最上位の呼び出し"] == str(MAX_CONCURRENT_EXECUTIONS) == str(RuntimeConfig().max_concurrent_executions)
    assert limits["1 つのプロセスのイベントループが専用の枠で同時に実行するドライラン"] == str(MAX_CONCURRENT_DRY_RUNS) == str(RuntimeConfig().max_concurrent_dry_runs)
    assert limits["コントラクトの実行あたりの `random()` の抽選"] == str(DEFAULT_LIMITS.max_random_draws)
    assert limits["呼び出しの連鎖上のコントラクトの実行"].startswith(f"{DEFAULT_LIMITS.max_contract_depth} (")
    assert limits["コントラクトの実行あたりの内部呼び出しフレーム"] == str(DEFAULT_LIMITS.max_frames)


def test_builtins_md_lists_every_builtin_with_its_exact_signature() -> None:
    documented = {code(cells[1]): (int(cells[0]), int(cells[2])) for cells in rows(section(BUILTINS, "シグネチャ"))}
    assert documented == {builtin.signature(): (builtin.id, builtin.gas) for builtin in BUILTINS_V1}


def test_documents_are_ascii_or_japanese_with_minimal_tables() -> None:
    for path in sorted(DOCS.rglob("*")):
        if path.suffix in (".md", ".py", ".json"):
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".md":
                assert character_violations(path) == [], path
            else:
                assert text.isascii(), path
            assert not re.search(r"^\|.*  \|", text, re.MULTILINE), path
