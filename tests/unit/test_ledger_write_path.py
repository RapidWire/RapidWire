"""Only rapidwire/core/ledger.py may write the balance and transfer tables; every other module may only read them."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "rapidwire"
LEDGER = PACKAGE / "core" / "ledger.py"
PROTECTED = frozenset({"balance", "transfer"})
WRITE_FUNCTIONS = frozenset({"insert", "update", "delete", "mysql_insert"})
WRITE_METHODS = frozenset({"insert", "update", "delete"})
RAW_WRITE = re.compile(r"\b(INSERT\s+(IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+`?(balance|transfer)`?(\s|$|\()", re.IGNORECASE)


def _table_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to the protected tables by imports from rapidwire.db.tables, plus the module alias itself."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rapidwire.db.tables":
            for name in node.names:
                if name.name in PROTECTED:
                    aliases[name.asname or name.name] = name.name
        if isinstance(node, ast.ImportFrom) and node.module == "rapidwire.db":
            for name in node.names:
                if name.name == "tables":
                    aliases[name.asname or "tables"] = "<module>"
    return aliases


def _table_of(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name) and aliases.get(node.id) in PROTECTED:
        return aliases[node.id]
    if isinstance(node, ast.Attribute) and node.attr in PROTECTED and isinstance(node.value, ast.Name) and (aliases.get(node.value.id) == "<module>" or node.value.id == "tables"):
        return node.attr
    return None


def write_sites(source: str) -> list[str]:
    """Descriptions of statements in source that write a protected table."""
    tree = ast.parse(source)
    aliases = _table_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in WRITE_FUNCTIONS and node.args:
                table = _table_of(node.args[0], aliases)
                if table is not None:
                    found.append(f"line {node.lineno}: {name}({table})")
            if isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS:
                table = _table_of(func.value, aliases)
                if table is not None:
                    found.append(f"line {node.lineno}: {table}.{func.attr}()")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and RAW_WRITE.search(node.value):
            found.append(f"line {node.lineno}: raw SQL write")
    return found


def test_only_the_ledger_writes_balances_and_transfers() -> None:
    offenders = {str(path.relative_to(PACKAGE)): sites for path in sorted(PACKAGE.rglob("*.py")) if path != LEDGER and (sites := write_sites(path.read_text(encoding="utf-8")))}
    assert offenders == {}


def test_the_scan_finds_the_ledger_writes() -> None:
    sites = write_sites(LEDGER.read_text(encoding="utf-8"))
    assert any("insert(transfer)" in site for site in sites)
    assert any("update(balance)" in site for site in sites)


def test_the_scan_catches_every_write_form_and_ignores_reads() -> None:
    writes = """
from sqlalchemy import delete, insert, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from rapidwire.db import tables
from rapidwire.db.tables import balance as bal, transfer

insert(transfer).values(amount=1)
update(bal).values(amount=2)
delete(bal)
mysql_insert(bal)
bal.update()
tables.transfer.delete()
SQL = "UPDATE balance SET amount = 0"
OTHER = "insert into `transfer` (amount) values (1)"
"""
    assert len(write_sites(writes)) == 8
    reads = """
from sqlalchemy import func, select, update
from rapidwire.db.tables import balance, currency, transfer

select(balance.c.amount).where(balance.c.account_id == 1).with_for_update()
select(func.sum(transfer.c.amount))
update(currency).values(name="x")
DOC = "Every balance update goes through the ledger."
SQL = "SELECT amount FROM balance"
"""
    assert write_sites(reads) == []
