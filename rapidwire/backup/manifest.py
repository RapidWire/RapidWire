"""The manifest of an export archive: format and versions, when the snapshot was taken, every table file with its column encodings, row count, size and SHA-256, and the invariant values and verification result of the same snapshot."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from rapidwire.backup.encoding import ColumnSpec, Encoding, TableSpec
from rapidwire.backup.totals import InvariantTotals

__all__ = ["FORMAT", "FORMAT_VERSION", "MANIFEST_NAME", "Manifest", "ManifestError", "TableEntry"]

FORMAT: Final = "rapidwire-export"
FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
_TABLE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """The manifest cannot be read as a manifest of this format version."""


@dataclass(frozen=True)
class TableEntry:
    spec: TableSpec
    rows: int
    size: int
    sha256: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "file": self.spec.file,
            "columns": [{"name": item.name, "encoding": item.encoding.value} for item in self.spec.columns],
            "primary_key": list(self.spec.primary_key),
            "rows": self.rows,
            "bytes": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TableEntry:
        name = data["name"]
        if not isinstance(name, str) or _TABLE_NAME_RE.fullmatch(name) is None:
            raise ManifestError(f"invalid table name {name!r}")
        columns = tuple(ColumnSpec(name=str(item["name"]), encoding=Encoding(item["encoding"])) for item in data["columns"])
        names = [item.name for item in columns]
        primary_key = tuple(str(item) for item in data["primary_key"])
        if not columns or len(set(names)) != len(names) or not primary_key or not set(primary_key) <= set(names):
            raise ManifestError(f"invalid columns or primary key for table {name}")
        spec = TableSpec(name=name, columns=columns, primary_key=primary_key)
        if data["file"] != spec.file:
            raise ManifestError(f"table {name} names file {data['file']!r}, expected {spec.file!r}")
        rows, size, digest = data["rows"], data["bytes"], data["sha256"]
        if not (isinstance(rows, int) and isinstance(size, int) and rows >= 0 and size >= 0 and isinstance(digest, str) and _SHA256_RE.fullmatch(digest)):
            raise ManifestError(f"invalid counts or checksum for table {name}")
        return cls(spec=spec, rows=rows, size=size, sha256=digest)


@dataclass(frozen=True)
class Manifest:
    app_version: str
    schema_revision: str
    created_at: datetime
    tables: tuple[TableEntry, ...]
    totals: InvariantTotals
    invariants_ok: bool
    invariant_violations: int

    def to_bytes(self) -> bytes:
        document = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app_version": self.app_version,
            "schema_revision": self.schema_revision,
            "created_at": self.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "tables": [entry.to_json() for entry in self.tables],
            "invariants": {**self.totals.to_json(), "verification": {"ok": self.invariants_ok, "violations": self.invariant_violations}},
        }
        return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True).encode("ascii") + b"\n"

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        try:
            document = json.loads(data)
            if document.get("format") != FORMAT:
                raise ManifestError("not a RapidWire export manifest")
            if document.get("format_version") != FORMAT_VERSION:
                raise ManifestError(f"unsupported export format version {document.get('format_version')!r}")
            tables = tuple(TableEntry.from_json(item) for item in document["tables"])
            names = [entry.spec.name for entry in tables]
            if len(set(names)) != len(names):
                raise ManifestError("a table is listed twice")
            invariants = document["invariants"]
            verification = invariants["verification"]
            if not isinstance(verification["ok"], bool) or not isinstance(verification["violations"], int):
                raise ManifestError("invalid invariant verification result")
            created_at = datetime.strptime(document["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            return cls(
                app_version=str(document["app_version"]),
                schema_revision=str(document["schema_revision"]),
                created_at=created_at,
                tables=tables,
                totals=InvariantTotals.from_json(invariants),
                invariants_ok=verification["ok"],
                invariant_violations=verification["violations"],
            )
        except ManifestError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ManifestError(f"malformed manifest: {exc}") from exc

    def entry(self, name: str) -> TableEntry | None:
        return next((entry for entry in self.tables if entry.spec.name == name), None)
