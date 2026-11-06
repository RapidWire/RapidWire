"""A backup of a seeded ledger taken with the real mysqldump 8.4 as an unprivileged account, verified offline, restored both ways into fresh databases, and tampered with."""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.backups import ChecksumCache, list_backups
from rapidwire.backup.export import export_tables
from rapidwire.backup.jobs import BACKUP_JOB_NAME, BackupRunResult, run_backup
from rapidwire.backup.manifest import MANIFEST_NAME, Manifest
from rapidwire.backup.naming import BackupKind, format_stamp
from rapidwire.backup.restore import RestoreError, restore_backup
from rapidwire.backup.verify import verify_file
from rapidwire.config import Settings
from rapidwire.core import joblock
from rapidwire.core.db import consistent_snapshot
from rapidwire.db.engine import create_engine
from rapidwire.db.migrate import head_revision
from rapidwire.db.tables import metadata
from tests.conftest import truncate_all
from tests.integration.backup import mysql_client_wrapper as wrapper
from tests.integration.backup.conftest import DatabaseFactory
from tests.integration.backup.seed import ALICE, CLAIM_DESCRIPTION, RAPID, TOKEN_HASH, seed_ledger

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Generation:
    settings: Settings
    result: BackupRunResult
    argv_log: Path

    @property
    def dump(self) -> Path:
        assert self.result.dump_path is not None
        return self.result.dump_path

    @property
    def export(self) -> Path:
        assert self.result.export_path is not None
        return self.result.export_path

    @property
    def password(self) -> str:
        return self.settings.db_password.get_secret_value()


def _manifest(path: Path) -> Manifest:
    with tarfile.open(path, "r:gz") as archive:
        member = archive.extractfile(MANIFEST_NAME)
        assert member is not None
        return Manifest.from_bytes(member.read())


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def generation(engine: AsyncEngine, migrated_url: URL, app_account: tuple[str, str], mysql_clients: Path, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Generation]:
    """One backup run of the seeded ledger, taken as the application account, with the argument lists the client wrapper received."""
    await truncate_all(engine)
    await seed_ledger(engine)
    user, password = app_account
    directory = tmp_path_factory.mktemp("generation")
    settings = Settings(
        _env_file=None,
        db_host=migrated_url.host,
        db_port=migrated_url.port,
        db_name=migrated_url.database,
        db_user=user,
        db_password=password,
        backup_dir=directory / "backups",
        backup_retention=3,
    )
    argv_log = directory / "argv.log"
    app = create_engine(settings.database_url)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(wrapper.ARGV_LOG_ENV, str(argv_log))
            result = await run_backup(app, settings)
    finally:
        await app.dispose()
    assert result.ok, result.errors
    yield Generation(settings=settings, result=result, argv_log=argv_log)


def test_a_run_publishes_exactly_one_dump_and_one_export(generation: Generation) -> None:
    stamp = format_stamp(generation.result.started_at)
    directory = generation.settings.backup_dir
    names = [BackupKind.DUMP.file_name(stamp), BackupKind.EXPORT.file_name(stamp)]
    assert sorted(entry.name for entry in directory.iterdir()) == sorted(names)
    assert [file.name for file in sorted(list_backups(directory, ChecksumCache()) or [], key=lambda file: file.name)] == sorted(names)
    assert generation.result.deleted == ()
    assert generation.result.invariant_violations == 0


def test_the_password_never_reaches_an_argument_list(generation: Generation) -> None:
    calls = [line.split("\0") for line in generation.argv_log.read_text(encoding="utf-8").splitlines()]
    assert [call[0] for call in calls] == ["mysqldump"]
    assert calls[0][1].startswith("--defaults-file=/dev/fd/")
    assert all(generation.password not in argument for call in calls for argument in call)


def test_the_manifest_describes_every_table_and_the_snapshot_invariants(generation: Generation) -> None:
    manifest = _manifest(generation.export)
    assert manifest.schema_revision == head_revision()
    assert [entry.spec.name for entry in manifest.tables] == sorted(metadata.tables)
    rows = {entry.spec.name: entry.rows for entry in manifest.tables}
    assert rows["api_token"] == 1
    assert rows["contract_version"] == 1
    assert rows["contract_storage"] == 2
    assert rows["outbox"] == 2
    assert rows["pool"] == 2
    assert rows["job_lock"] == 1
    assert manifest.invariants_ok
    assert manifest.invariant_violations == 0
    assert set(manifest.totals.currencies) == {111, 222, 333}
    assert all(item.balance_sum == 0 for item in manifest.totals.currencies.values())
    assert manifest.totals.currencies[RAPID].supply >= 10**37
    assert manifest.totals.currencies[RAPID].stake_total == 1_000_005
    assert len(manifest.totals.pools) == 2
    with tarfile.open(generation.export, "r:gz") as archive:
        assert archive.getnames() == [MANIFEST_NAME, *(entry.spec.file for entry in manifest.tables)]
        member = archive.extractfile("tables/claim.jsonl")
        assert member is not None
        claim = json.loads(member.readline())
        assert claim["description"] == CLAIM_DESCRIPTION
        assert claim["amount"] == "500"
        assert claim["payer_id"] == str(ALICE)
        member = archive.extractfile("tables/api_token.jsonl")
        assert member is not None
        assert json.loads(member.readline())["token_hash"] == TOKEN_HASH.hex()
        member = archive.extractfile("tables/outbox.jsonl")
        assert member is not None
        assert [json.loads(line)["payload"] for line in member][1] == [None]
        member = archive.extractfile("tables/currency.jsonl")
        assert member is not None
        assert "active_symbol" not in json.loads(member.readline())


def test_both_files_verify(generation: Generation) -> None:
    for path in (generation.dump, generation.export):
        report = verify_file(path)
        assert report.ok, (report.problems, report.unreadable)


async def _table_digests(engine: AsyncEngine, manifest: Manifest) -> dict[str, tuple[int, int, str]]:
    async with consistent_snapshot(engine) as conn:
        digests = await export_tables(conn, [entry.spec for entry in manifest.tables])
    return {digest.spec.name: (digest.rows, digest.size, digest.sha256) for digest in digests}


async def test_the_dump_restores_into_an_empty_database_with_every_row(generation: Generation, fresh_database: DatabaseFactory) -> None:
    target = await fresh_database()
    outcome = await restore_backup(generation.dump, target)
    assert outcome.ok, (outcome.report.violations, outcome.problems)
    assert outcome.report.currencies
    manifest = _manifest(generation.export)
    engine = create_engine(target.url)
    try:
        async with engine.connect() as conn:
            counts = {entry.spec.name: (await conn.execute(select(func.count()).select_from(text(f"`{entry.spec.name}`")))).scalar_one() for entry in manifest.tables}
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        digests = await _table_digests(engine, manifest)
    finally:
        await engine.dispose()
    assert counts == {entry.spec.name: entry.rows for entry in manifest.tables}
    assert revision == manifest.schema_revision
    # Nothing was written between the dump and the export, so the dump restores to exactly the exported rows.
    assert digests == {entry.spec.name: (entry.rows, entry.size, entry.sha256) for entry in manifest.tables}


async def test_the_export_restores_and_exports_again_to_the_same_checksums(generation: Generation, fresh_database: DatabaseFactory) -> None:
    target = await fresh_database()
    outcome = await restore_backup(generation.export, target)
    assert outcome.ok, (outcome.report.violations, outcome.problems)
    manifest = _manifest(generation.export)
    assert outcome.rows == sum(entry.rows for entry in manifest.tables)
    engine = create_engine(target.url)
    try:
        assert await _table_digests(engine, manifest) == {entry.spec.name: (entry.rows, entry.size, entry.sha256) for entry in manifest.tables}
    finally:
        await engine.dispose()


async def test_restore_refuses_a_database_that_is_not_empty(generation: Generation, fresh_database: DatabaseFactory) -> None:
    target = await fresh_database()
    engine = create_engine(target.url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE keep_me (x INT PRIMARY KEY)"))
    finally:
        await engine.dispose()
    for path in (generation.dump, generation.export):
        with pytest.raises(RestoreError, match="not empty"):
            await restore_backup(path, target)


def _rewrite_export(source: Path, destination: Path, change: Callable[[str, bytes], bytes | None], extra: list[tuple[str, bytes]] | None = None) -> Path:
    """Copy an export archive, passing each member through change (None drops it) and appending extra members."""
    buffer = io.BytesIO()
    with tarfile.open(source, "r:gz") as old, tarfile.open(fileobj=buffer, mode="w:gz") as new:
        for member in old.getmembers():
            handle = old.extractfile(member)
            assert handle is not None
            data = change(member.name, handle.read())
            if data is None:
                continue
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            new.addfile(info, io.BytesIO(data))
        for name, data in extra or []:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            new.addfile(info, io.BytesIO(data))
    destination.write_bytes(buffer.getvalue())
    return destination


def _cli(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    base = {name: value for name, value in os.environ.items() if not name.startswith("RAPIDWIRE_") or name.startswith("RAPIDWIRE_TEST_")}
    return subprocess.run([sys.executable, "-m", "rapidwire", *args], capture_output=True, text=True, env={**base, **(env or {})}, cwd=cwd, timeout=600, check=False)


def _change_balance_line(name: str, data: bytes) -> bytes:
    if name != "tables/balance.jsonl":
        return data
    lines = data.splitlines(keepends=True)
    row = json.loads(lines[-1])
    row["amount"] = str(int(row["amount"]) + 1)
    lines[-1] = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    return b"".join(lines)


def _change_manifest_supply(name: str, data: bytes) -> bytes:
    if name != MANIFEST_NAME:
        return data
    document = json.loads(data)
    item = document["invariants"]["currencies"][0]
    item["supply"] = str(int(item["supply"]) + 1)
    return json.dumps(document).encode()


def _swap_transfer_lines(name: str, data: bytes) -> bytes:
    if name != "tables/transfer.jsonl":
        return data
    lines = data.splitlines(keepends=True)
    lines[0], lines[1] = lines[1], lines[0]
    return b"".join(lines)


def _retype_balance_amount(name: str, data: bytes) -> bytes:
    if name != MANIFEST_NAME:
        return data
    document = json.loads(data)
    for table in document["tables"]:
        if table["name"] == "balance":
            for column in table["columns"]:
                if column["name"] == "amount":
                    column["encoding"] = "string"
    return json.dumps(document).encode()


def test_a_manifest_without_the_invariant_columns_is_unreadable(generation: Generation, tmp_path: Path) -> None:
    tampered = _rewrite_export(generation.export, tmp_path / "retyped.tar.gz", _retype_balance_amount)
    assert "table balance has column amount as string, expected decimal" in (verify_file(tampered).unreadable or "")
    result = _cli("backup", "verify", str(tampered))
    assert result.returncode == 1
    assert result.stdout.startswith(f"UNREADABLE {tampered}")


@pytest.mark.parametrize(
    ("change", "extra", "expected"),
    [
        (_change_balance_line, None, ["tables/balance.jsonl has", "invariant values differ", "sum to 1, not 0"]),
        (_change_manifest_supply, None, ["invariant values differ: currency 111: supply is"]),
        (lambda name, data: None if name == "tables/stake.jsonl" else data, None, ["missing 'tables/stake.jsonl'", "invariant values differ"]),
        (lambda name, data: data, [("tables/extra.jsonl", b"{}\n")], ["unexpected member 'tables/extra.jsonl'"]),
        (lambda name, data: data, [("../evil.jsonl", b"{}\n")], ["unsafe path"]),
        (lambda name, data: data, [("tables/balance.jsonl", b"")], ["'tables/balance.jsonl' twice"]),
        (_swap_transfer_lines, None, ["tables/transfer.jsonl line 2: the row is not after the previous one in primary key order", "tables/transfer.jsonl has"]),
    ],
    ids=["changed-line", "changed-manifest-invariant", "removed-member", "unexpected-member", "unsafe-path", "duplicate-member", "reordered-rows"],
)
def test_tampered_exports_are_detected(generation: Generation, tmp_path: Path, change: Callable[[str, bytes], bytes | None], extra: list[tuple[str, bytes]] | None, expected: list[str]) -> None:
    tampered = _rewrite_export(generation.export, tmp_path / "tampered.tar.gz", change, extra)
    report = verify_file(tampered)
    assert report.unreadable is None
    text_out = "\n".join(report.problems)
    for fragment in expected:
        assert fragment in text_out
    result = _cli("backup", "verify", str(tampered))
    assert result.returncode == 4, result.stdout + result.stderr
    assert result.stdout.startswith(f"MISMATCH {tampered}")


def test_an_export_without_its_manifest_first_is_unreadable(generation: Generation, tmp_path: Path) -> None:
    tampered = _rewrite_export(generation.export, tmp_path / "nomanifest.tar.gz", lambda name, data: None if name == MANIFEST_NAME else data)
    assert "is not manifest.json" in (verify_file(tampered).unreadable or "")
    assert _cli("backup", "verify", str(tampered)).returncode == 1


def test_tampered_dumps_are_detected(generation: Generation, tmp_path: Path) -> None:
    data = generation.dump.read_bytes()
    truncated = tmp_path / "truncated.sql.gz"
    truncated.write_bytes(data[: len(data) // 2])
    unfinished = tmp_path / "unfinished.sql.gz"
    sql = gzip.decompress(data)
    unfinished.write_bytes(gzip.compress(sql[: sql.rindex(b"-- Dump completed")]))
    assert verify_file(truncated).unreadable is not None
    assert "incomplete" in "\n".join(verify_file(unfinished).problems)
    assert _cli("backup", "verify", str(truncated)).returncode == 1
    assert _cli("backup", "verify", str(unfinished)).returncode == 4
    assert _cli("backup", "verify", str(generation.dump), str(generation.export)).returncode == 0


async def test_restoring_a_dump_checks_its_header_and_completion_marker(generation: Generation, fresh_database: DatabaseFactory, tmp_path: Path) -> None:
    sql = gzip.decompress(generation.dump.read_bytes())
    unfinished = tmp_path / "unfinished.sql.gz"
    unfinished.write_bytes(gzip.compress(sql[: sql.rindex(b"-- Dump completed")]))
    target = await fresh_database()
    outcome = await restore_backup(unfinished, target)
    assert not outcome.ok
    assert outcome.report.ok
    assert outcome.problems == ["the dump does not end with '-- Dump completed', so it is incomplete and the restored database may be missing data"]

    headless = tmp_path / "headless.sql.gz"
    headless.write_bytes(gzip.compress(b"DROP TABLE IF EXISTS `account`;\n" + sql))
    other = await fresh_database()
    with pytest.raises(RestoreError, match="mysqldump's header"):
        await restore_backup(headless, other)


def _app_env(generation: Generation, directory: Path) -> dict[str, str]:
    settings = generation.settings
    return {
        "RAPIDWIRE_DB_HOST": settings.db_host,
        "RAPIDWIRE_DB_PORT": str(settings.db_port),
        "RAPIDWIRE_DB_NAME": settings.db_name,
        "RAPIDWIRE_DB_USER": settings.db_user,
        "RAPIDWIRE_DB_PASSWORD": generation.password,
        "RAPIDWIRE_BACKUP_DIR": str(directory),
    }


async def test_the_backup_command_writes_a_generation_and_respects_the_lease(generation: Generation, engine: AsyncEngine, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    result = _cli("backup", env=_app_env(generation, directory), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    names = sorted(entry.name for entry in directory.iterdir())
    assert [name.split("-")[1] for name in names] == ["dump", "export"]
    messages = [json.loads(line)["msg"] for line in result.stdout.splitlines()]
    assert "backup finished" in messages
    assert generation.password not in result.stdout + result.stderr

    assert await joblock.try_acquire(engine, BACKUP_JOB_NAME, "someone-else", timedelta(minutes=5))
    try:
        held = _cli("backup", env=_app_env(generation, directory), cwd=tmp_path)
    finally:
        await joblock.release(engine, BACKUP_JOB_NAME, "someone-else")
    assert held.returncode == 1
    assert "another backup is running" in held.stderr
    assert sorted(entry.name for entry in directory.iterdir()) == names


async def test_the_restore_command_reads_the_password_from_the_environment(generation: Generation, fresh_database: DatabaseFactory, tmp_path: Path) -> None:
    target = await fresh_database()
    url = f"mysql://{target.user}@{target.host}:{target.port}/{target.database}"
    result = _cli("backup", "restore", "--target-url", url, str(generation.export), env={"MYSQL_PWD": target.password}, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "restore verified"
    assert "0 violations" in result.stdout
    assert target.password not in result.stdout + result.stderr

    other = await fresh_database()
    url = f"mysql://{other.user}@{other.host}:{other.port}/{other.database}"
    result = _cli("backup", "restore", "--target-url", url, str(generation.dump), env={"MYSQL_PWD": other.password}, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.password not in result.stdout + result.stderr


async def test_restoring_a_tampered_export_reports_the_mismatch(generation: Generation, fresh_database: DatabaseFactory, tmp_path: Path) -> None:
    tampered = _rewrite_export(generation.export, tmp_path / "tampered.tar.gz", _change_balance_line)
    target = await fresh_database()
    outcome = await restore_backup(tampered, target)
    assert not outcome.ok
    assert any("restored table balance exports as" in problem for problem in outcome.problems)
    assert any(violation.check == "sum_not_zero" for violation in outcome.report.violations)
