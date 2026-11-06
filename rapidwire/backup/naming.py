"""Names of the files a backup run writes: one generation is one run with one UTC timestamp, published as a compressed dump and an export archive, each first written under a name the API does not list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

__all__ = [
    "PARTIAL_SUFFIX",
    "STAGING_SUFFIX",
    "STAMP_FORMAT",
    "BackupKind",
    "BackupName",
    "format_stamp",
    "is_leftover_name",
    "parse_backup_name",
    "parse_stamp",
    "partial_name",
    "staging_name",
]

STAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"
PARTIAL_SUFFIX: Final = ".partial"
STAGING_SUFFIX: Final = ".staging"
_STAMP: Final = r"(?P<stamp>[0-9]{8}T[0-9]{6}Z)"


class BackupKind(StrEnum):
    DUMP = "dump"
    EXPORT = "export"

    @property
    def suffix(self) -> str:
        return ".sql.gz" if self is BackupKind.DUMP else ".tar.gz"

    def file_name(self, stamp: str) -> str:
        return f"rapidwire-{self.value}-{stamp}{self.suffix}"


_FINAL_RE: Final = re.compile(rf"^rapidwire-(?P<kind>dump|export)-{_STAMP}(?P<suffix>\.sql\.gz|\.tar\.gz)$")
_PARTIAL_RE: Final = re.compile(rf"^rapidwire-(?:dump-{_STAMP}\.sql\.gz|export-[0-9]{{8}}T[0-9]{{6}}Z\.tar\.gz)\.partial$")
_STAGING_RE: Final = re.compile(rf"^\.rapidwire-export-{_STAMP}\.staging$")


@dataclass(frozen=True, order=True)
class BackupName:
    """A published backup file name: its generation's timestamp and its kind."""

    stamp: str
    kind: BackupKind

    @property
    def name(self) -> str:
        return self.kind.file_name(self.stamp)


def format_stamp(moment: datetime) -> str:
    """The generation timestamp of a run started at moment, in UTC to the second."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a backup timestamp needs a timezone-aware datetime")
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)


def parse_backup_name(name: str) -> BackupName | None:
    """The generation and kind of a published backup file name, None for any other name."""
    match = _FINAL_RE.fullmatch(name)
    if match is None:
        return None
    kind = BackupKind(match["kind"])
    if match["suffix"] != kind.suffix:
        return None
    try:
        parse_stamp(match["stamp"])
    except ValueError:
        return None
    return BackupName(stamp=match["stamp"], kind=kind)


def partial_name(kind: BackupKind, stamp: str) -> str:
    """The name a backup file is written under before it is published."""
    return kind.file_name(stamp) + PARTIAL_SUFFIX


def staging_name(stamp: str) -> str:
    """The hidden directory holding an export's table files until they are packed."""
    return f".rapidwire-export-{stamp}{STAGING_SUFFIX}"


def is_leftover_name(name: str) -> bool:
    """Whether name is a partial file or staging directory that only an interrupted backup run leaves behind."""
    return _PARTIAL_RE.fullmatch(name) is not None or _STAGING_RE.fullmatch(name) is not None
