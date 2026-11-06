from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rapidwire.backup import jobs
from rapidwire.backup.cli import run_backup_command
from rapidwire.backup.dump import DUMP_ARGUMENTS
from rapidwire.backup.jobs import BackupRunResult
from rapidwire.backup.manifest import FORMAT, FORMAT_VERSION
from rapidwire.backup.naming import BackupKind
from rapidwire.backup.retention import Generation, retained_stamps
from rapidwire.cli import EXIT_FAILURE, EXIT_INVARIANT_VIOLATION, EXIT_OK
from rapidwire.config import Settings
from rapidwire.core import joblock
from rapidwire.core.joblock import LeaseLost, LeaseUnavailable
from rapidwire.db import engine as engine_module
from rapidwire.worker.jobs import BACKUP_CHECK_INTERVAL_SECONDS, BACKUP_RETRY_DELAY
from tests.unit.doc_text import character_violations

DOC = Path(__file__).resolve().parents[3] / "document" / "backups.md"


class _Engine:
    async def dispose(self) -> None:
        return None


def backup_command_exit(monkeypatch: pytest.MonkeyPatch, *, lease_error: type[Exception] | None = None, failed: bool = False, violations: int | None = 0) -> int:
    """Run the on-demand backup command with the lease, the engine and the backup run replaced, and return its exit code."""

    @asynccontextmanager
    async def fake_lease(*args: object, **kwargs: object) -> AsyncIterator[None]:
        if lease_error is not None:
            raise lease_error(jobs.BACKUP_JOB_NAME)
        yield

    async def fake_run(engine: object, settings: Settings, *, now: datetime | None = None) -> BackupRunResult:
        return BackupRunResult(
            started_at=datetime(2026, 11, 5, 3, tzinfo=UTC),
            dump_path=None if failed else Path("dump"),
            export_path=Path("export"),
            deleted=(),
            errors=("dump failed",) if failed else (),
            invariant_violations=violations,
        )

    monkeypatch.setattr(jobs, "run_backup", fake_run)
    monkeypatch.setattr(joblock, "hold_lease", fake_lease)
    monkeypatch.setattr(engine_module, "create_engine", lambda *args, **kwargs: _Engine())
    return run_backup_command(Settings(db_password="unused-password"), argparse.Namespace(command="backup", backup_command=None))


def _complete(stamp: str) -> Generation:
    return Generation(stamp=stamp, files={BackupKind.DUMP: 0, BackupKind.EXPORT: 0})


def _incomplete(stamp: str) -> Generation:
    return Generation(stamp=stamp, files={BackupKind.DUMP: 0})


def _stamp(day: int) -> str:
    return f"202611{day:02d}T030000Z"


def test_the_document_states_the_dump_arguments_and_the_manifest_format() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert f"`mysqldump {' '.join(DUMP_ARGUMENTS)} <database>`" in text
    assert f'"format": "{FORMAT}"' in text
    assert f'"format_version": {FORMAT_VERSION}' in text
    assert character_violations(DOC) == []


def test_on_demand_exit_codes_match_the_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    text = DOC.read_text(encoding="utf-8")
    assert backup_command_exit(monkeypatch, lease_error=LeaseUnavailable) == EXIT_FAILURE
    assert "another backup is running" in capsys.readouterr().err
    assert f'リースがほかで保持されている間、要求に応じたコマンドは "another backup is running" を出力して {EXIT_FAILURE} で終了する' in text
    assert backup_command_exit(monkeypatch, lease_error=LeaseLost) == EXIT_FAILURE
    assert backup_command_exit(monkeypatch, failed=True) == EXIT_FAILURE
    assert backup_command_exit(monkeypatch, violations=0) == EXIT_OK
    assert backup_command_exit(monkeypatch, violations=None) == EXIT_OK
    assert backup_command_exit(monkeypatch, violations=2) == EXIT_INVARIANT_VIOLATION
    assert (
        f"要求に応じたコマンドは、両方のファイルを書き込めたときは {EXIT_OK}、書き込めたがスナップショットが台帳の不変条件に違反するときは {EXIT_INVARIANT_VIOLATION}"
        f" (実行はそのこともエラーレベルでログに出す)、実行が失敗したかリースを失ったときは {EXIT_FAILURE} で終了する。" in text
    )


def test_the_schedule_numbers_come_from_the_worker() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert f"worker は {BACKUP_CHECK_INTERVAL_SECONDS:g} 秒ごとに実行の期限が来たかを確認する" in text
    assert f"worker は次の試行まで {BACKUP_RETRY_DELAY.total_seconds() / 60:g} 分待ち" in text


def test_retention_follows_the_documented_rules() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "成功した実行は、自身を含めて最新の `RAPIDWIRE_BACKUP_RETENTION` 個の完全な世代を残し、それより古い世代のファイルをすべて削除する" in text
    assert "失敗した実行は完全な世代を 1 つも削除しない" in text
    assert "保持処理が残すのは 1 つで、今回の実行の世代が不完全ならそれ、そうでなければ最新のもの (どの完全な世代よりも新しい場合に限る) であり" in text
    older = [_complete(_stamp(day)) for day in range(1, 6)]
    stale_incomplete = _incomplete("20261103T040000Z")
    # A successful run keeps the newest complete generations, its own included, and no older incomplete one.
    success = [*older, stale_incomplete, _complete(_stamp(6))]
    assert retained_stamps(success, 3, current=_stamp(6), prune_complete=True) == {_stamp(4), _stamp(5), _stamp(6)}
    # A failed run keeps every complete generation and only its own incomplete one.
    failure = [*older, _incomplete(_stamp(6)), _incomplete(_stamp(7))]
    assert retained_stamps(failure, 3, current=_stamp(7), prune_complete=False) == {*(_stamp(day) for day in range(1, 6)), _stamp(7)}
    # A run that wrote nothing keeps the newest incomplete generation when it is newer than every complete one.
    assert retained_stamps(failure, 3, current=_stamp(8), prune_complete=False) == {*(_stamp(day) for day in range(1, 6)), _stamp(7)}
