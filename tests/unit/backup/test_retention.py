from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rapidwire.api.backups import is_backup_name
from rapidwire.backup.jobs import BACKUP_JOB_NAME, next_backup_due
from rapidwire.backup.naming import BackupKind, format_stamp, is_leftover_name, parse_backup_name, partial_name, staging_name
from rapidwire.backup.retention import apply_retention, list_generations, remove_leftovers

STAMPS = ["20261028T030000Z", "20261029T030000Z", "20261030T030000Z", "20261031T030000Z"]


def _touch(directory: Path, name: str, moment: datetime | None = None) -> Path:
    path = directory / name
    path.write_bytes(b"x")
    if moment is not None:
        os.utime(path, (moment.timestamp(), moment.timestamp()))
    return path


def _generation(directory: Path, stamp: str, moment: datetime | None = None, kinds: tuple[BackupKind, ...] = tuple(BackupKind)) -> None:
    for kind in kinds:
        _touch(directory, kind.file_name(stamp), moment)


def test_names_follow_the_api_conventions() -> None:
    stamp = format_stamp(datetime(2026, 10, 31, 3, 0, 0, 123456, tzinfo=UTC))
    assert stamp == "20261031T030000Z"
    assert BackupKind.DUMP.file_name(stamp) == "rapidwire-dump-20261031T030000Z.sql.gz"
    assert BackupKind.EXPORT.file_name(stamp) == "rapidwire-export-20261031T030000Z.tar.gz"
    for kind in BackupKind:
        assert is_backup_name(kind.file_name(stamp))
        assert parse_backup_name(kind.file_name(stamp)) is not None
        assert not is_backup_name(partial_name(kind, stamp))
        assert is_leftover_name(partial_name(kind, stamp))
    assert not is_backup_name(staging_name(stamp))
    assert is_leftover_name(staging_name(stamp))
    assert BACKUP_JOB_NAME == "backup"


@pytest.mark.parametrize(
    "name",
    [
        "rapidwire-dump-20261031T030000Z.tar.gz",
        "rapidwire-export-20261031T030000Z.sql.gz",
        "rapidwire-dump-20261331T030000Z.sql.gz",
        "rapidwire-dump-20261031T030000Z.sql.gz.sha256",
        "other-dump-20261031T030000Z.sql.gz",
        "rapidwire-dump-20261031T030000.sql.gz",
    ],
)
def test_foreign_names_are_not_backups(name: str) -> None:
    assert parse_backup_name(name) is None
    assert not is_leftover_name(name)


def test_format_stamp_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_stamp(datetime(2026, 10, 31))


def test_retention_keeps_the_newest_generations_and_deletes_both_files_of_older_ones(tmp_path: Path) -> None:
    for stamp in STAMPS:
        _generation(tmp_path, stamp)
    foreign = ["notes.txt", "rapidwire-dump-20200101T000000Z.sql.gz.sha256", "manual-backup.sql.gz"]
    for name in foreign:
        _touch(tmp_path, name)
    leftovers = [partial_name(BackupKind.DUMP, "20200101T000000Z"), partial_name(BackupKind.EXPORT, "20200101T000000Z")]
    for name in leftovers:
        _touch(tmp_path, name)

    deleted = apply_retention(tmp_path, 2)

    assert deleted == sorted(kind.file_name(stamp) for stamp in STAMPS[:2] for kind in BackupKind)
    remaining = sorted(path.name for path in tmp_path.iterdir())
    assert remaining == sorted([*foreign, *leftovers, *(kind.file_name(stamp) for stamp in STAMPS[2:] for kind in BackupKind)])


def test_retention_counts_only_complete_generations_and_deletes_older_incomplete_ones(tmp_path: Path) -> None:
    late = datetime(2030, 1, 1, tzinfo=UTC)
    # The oldest generation has the newest modification time; the timestamp in the name decides.
    _generation(tmp_path, STAMPS[0], late)
    _generation(tmp_path, STAMPS[1], kinds=(BackupKind.DUMP,))
    _generation(tmp_path, STAMPS[2])
    assert apply_retention(tmp_path, 2, current=STAMPS[2]) == [BackupKind.DUMP.file_name(STAMPS[1])]
    generations = list_generations(tmp_path)
    assert [generation.stamp for generation in generations] == [STAMPS[0], STAMPS[2]]
    assert all(generation.complete for generation in generations)


def test_retention_keeps_only_the_newest_incomplete_generation_newer_than_every_complete_one(tmp_path: Path) -> None:
    _generation(tmp_path, STAMPS[0])
    _generation(tmp_path, STAMPS[1], kinds=(BackupKind.DUMP,))
    _generation(tmp_path, STAMPS[2], kinds=(BackupKind.EXPORT,))
    _generation(tmp_path, STAMPS[3], kinds=(BackupKind.DUMP,))
    deleted = apply_retention(tmp_path, 1, current=STAMPS[3], prune_complete=False)
    assert deleted == [BackupKind.DUMP.file_name(STAMPS[1]), BackupKind.EXPORT.file_name(STAMPS[2])]
    assert [(generation.stamp, generation.complete) for generation in list_generations(tmp_path)] == [(STAMPS[0], True), (STAMPS[3], False)]


def test_failed_runs_prune_incomplete_generations_but_never_complete_ones(tmp_path: Path) -> None:
    for stamp in STAMPS[:3]:
        _generation(tmp_path, stamp)
    for index in range(5):
        stamp = f"2026110{index + 1}T030000Z"
        _generation(tmp_path, stamp, kinds=(BackupKind.DUMP,))
        apply_retention(tmp_path, 2, current=stamp, prune_complete=False)
    generations = list_generations(tmp_path)
    assert [generation.stamp for generation in generations] == [*STAMPS[:3], "20261105T030000Z"]
    assert len(list(tmp_path.iterdir())) == 3 * 2 + 1


def test_a_successful_run_after_failures_bounds_the_directory(tmp_path: Path) -> None:
    for stamp in STAMPS:
        _generation(tmp_path, stamp, kinds=(BackupKind.DUMP,))
    _generation(tmp_path, "20261101T030000Z")
    apply_retention(tmp_path, 3, current="20261101T030000Z")
    assert [generation.stamp for generation in list_generations(tmp_path)] == ["20261101T030000Z"]


def test_the_current_generation_survives_a_backward_clock_step(tmp_path: Path) -> None:
    for stamp in STAMPS[1:]:
        _generation(tmp_path, stamp)
    _generation(tmp_path, STAMPS[0])
    deleted = apply_retention(tmp_path, 2, current=STAMPS[0])
    assert deleted == sorted(kind.file_name(stamp) for stamp in STAMPS[1:3] for kind in BackupKind)
    assert [generation.stamp for generation in list_generations(tmp_path)] == [STAMPS[0], STAMPS[3]]
    _generation(tmp_path, "20261001T030000Z", kinds=(BackupKind.DUMP,))
    assert apply_retention(tmp_path, 2, current="20261001T030000Z", prune_complete=False) == []


def test_a_failed_run_after_a_backward_clock_step_leaves_one_incomplete_generation(tmp_path: Path) -> None:
    _generation(tmp_path, STAMPS[0])
    _generation(tmp_path, "20261105T030000Z", kinds=(BackupKind.DUMP,))
    _generation(tmp_path, "20261001T030000Z", kinds=(BackupKind.EXPORT,))
    deleted = apply_retention(tmp_path, 2, current="20261001T030000Z", prune_complete=False)
    assert deleted == [BackupKind.DUMP.file_name("20261105T030000Z")]
    assert [(generation.stamp, generation.complete) for generation in list_generations(tmp_path)] == [("20261001T030000Z", False), (STAMPS[0], True)]


def test_retention_with_fewer_generations_deletes_nothing(tmp_path: Path) -> None:
    _generation(tmp_path, STAMPS[0])
    assert apply_retention(tmp_path, 14) == []
    assert apply_retention(tmp_path / "missing", 1) == []
    with pytest.raises(ValueError, match="at least one"):
        apply_retention(tmp_path, 0)


def test_retention_skips_symbolic_links_and_directories(tmp_path: Path) -> None:
    for stamp in STAMPS[:2]:
        _generation(tmp_path, stamp)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    (tmp_path / BackupKind.DUMP.file_name("20200101T000000Z")).symlink_to(target)
    (tmp_path / BackupKind.EXPORT.file_name("20200101T000000Z")).mkdir()
    apply_retention(tmp_path, 1)
    assert target.read_bytes() == b"keep"
    assert (tmp_path / BackupKind.DUMP.file_name("20200101T000000Z")).is_symlink()
    assert (tmp_path / BackupKind.EXPORT.file_name("20200101T000000Z")).is_dir()


def test_remove_leftovers_removes_only_partial_files_and_staging_directories(tmp_path: Path) -> None:
    stamp = STAMPS[0]
    _touch(tmp_path, partial_name(BackupKind.DUMP, stamp))
    _touch(tmp_path, partial_name(BackupKind.EXPORT, stamp))
    staging = tmp_path / staging_name(stamp)
    (staging / "tables").mkdir(parents=True)
    (staging / "tables" / "balance.jsonl").write_bytes(b"{}\n")
    _generation(tmp_path, STAMPS[1])
    kept = ["other.partial", ".hidden.staging", "x.tmp"]
    for name in kept:
        _touch(tmp_path, name)

    removed = remove_leftovers(tmp_path)

    assert removed == sorted([partial_name(BackupKind.DUMP, stamp), partial_name(BackupKind.EXPORT, stamp), staging_name(stamp)])
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([*kept, *(kind.file_name(STAMPS[1]) for kind in BackupKind)])
    assert remove_leftovers(tmp_path / "missing") == []


def test_next_backup_due_is_now_without_backups(tmp_path: Path) -> None:
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    assert next_backup_due(tmp_path, timedelta(days=1), now) <= now
    assert next_backup_due(tmp_path / "missing", timedelta(days=1), now) <= now


def test_next_backup_due_follows_the_newest_complete_generation(tmp_path: Path) -> None:
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    written = datetime(2026, 11, 5, 3, 0, 7, tzinfo=UTC)
    _generation(tmp_path, "20261104T030000Z", written - timedelta(days=1))
    _generation(tmp_path, "20261105T030000Z", written)
    # A newer generation with only a dump does not count, and neither do partial files or foreign names.
    _generation(tmp_path, "20261105T110000Z", now, kinds=(BackupKind.DUMP,))
    _touch(tmp_path, partial_name(BackupKind.EXPORT, "20261105T110000Z"), now)
    _touch(tmp_path, "rapidwire-export-latest.tar.gz", now)
    assert next_backup_due(tmp_path, timedelta(hours=6), now) == written + timedelta(hours=6)


def test_next_backup_due_uses_the_later_file_of_a_generation(tmp_path: Path) -> None:
    dumped = datetime(2026, 11, 5, 3, 0, 1, tzinfo=UTC)
    exported = datetime(2026, 11, 5, 3, 4, 2, 500000, tzinfo=UTC)
    _touch(tmp_path, BackupKind.DUMP.file_name("20261105T030000Z"), dumped)
    _touch(tmp_path, BackupKind.EXPORT.file_name("20261105T030000Z"), exported)
    assert next_backup_due(tmp_path, timedelta(days=1), datetime(2026, 11, 5, 4, tzinfo=UTC)) == exported + timedelta(days=1)
