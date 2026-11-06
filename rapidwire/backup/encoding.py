"""The canonical row encoding of the export: one JSON object per line with sorted keys and no whitespace, one key per stored column (generated columns excluded), rows in primary key order.

Cell encodings: BIGINT and DECIMAL(38,0) as decimal strings, other integers as JSON numbers, booleans as true/false, text as strings, DATETIME(6) as ISO 8601 UTC with microseconds and a trailing Z, binary as lowercase hex, and a JSON column as a one-element array holding the document, so that SQL NULL (null) stays distinct from a JSON null document ([null]).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, BigInteger, Boolean, Column, Integer, LargeBinary, MetaData, Select, String, column, select, table
from sqlalchemy.types import BINARY, VARBINARY

from rapidwire.db.types import Amount, UtcDateTime

__all__ = [
    "TABLES_DIRECTORY",
    "ColumnSpec",
    "Encoding",
    "EncodingError",
    "TableSpec",
    "canonical_line",
    "decode_cell",
    "decode_row",
    "encode_cell",
    "encode_row",
    "encoding_of",
    "raw_table",
    "row_select",
    "table_specs",
]

TABLES_DIRECTORY: Final = "tables"
_DATETIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
_DATETIME_RE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
_INTEGER_RE: Final = re.compile(r"^-?(0|[1-9][0-9]*)$")
_HEX_RE: Final = re.compile(r"^([0-9a-f]{2})*$")


class EncodingError(ValueError):
    """A value does not fit the encoding of its column."""


class Encoding(StrEnum):
    BIGINT = "bigint"
    DECIMAL = "decimal"
    INT = "int"
    BOOL = "bool"
    STRING = "string"
    DATETIME = "datetime"
    HEX = "hex"
    JSON = "json"


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    encoding: Encoding


@dataclass(frozen=True)
class TableSpec:
    """An exported table: its stored columns in table order and its primary key columns."""

    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]

    @property
    def file(self) -> str:
        return f"{TABLES_DIRECTORY}/{self.name}.jsonl"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.columns)


def encoding_of(item: Column[Any]) -> Encoding:
    """The encoding of a schema column; a type without one raises EncodingError, so a new column type has to be given an encoding before it can be exported."""
    kind = item.type
    if isinstance(kind, Amount):
        return Encoding.DECIMAL
    if isinstance(kind, UtcDateTime):
        return Encoding.DATETIME
    if isinstance(kind, Boolean):
        return Encoding.BOOL
    if isinstance(kind, BigInteger):
        return Encoding.BIGINT
    if isinstance(kind, Integer):
        return Encoding.INT
    if isinstance(kind, JSON):
        return Encoding.JSON
    if isinstance(kind, LargeBinary | BINARY | VARBINARY):
        return Encoding.HEX
    if isinstance(kind, String):
        return Encoding.STRING
    raise EncodingError(f"column {item.table.name}.{item.name} has type {kind!r}, which has no export encoding")


def table_specs(metadata: MetaData) -> list[TableSpec]:
    """Every table on metadata in name order, without generated columns."""
    specs: list[TableSpec] = []
    for item in sorted(metadata.tables.values(), key=lambda entry: entry.name):
        columns = tuple(ColumnSpec(name=col.name, encoding=encoding_of(col)) for col in item.columns if col.computed is None)
        primary_key = tuple(col.name for col in item.primary_key.columns)
        if not primary_key:
            raise EncodingError(f"table {item.name} has no primary key, so its rows have no canonical order")
        specs.append(TableSpec(name=item.name, columns=columns, primary_key=primary_key))
    return specs


def raw_table(spec: TableSpec) -> Any:
    """A lightweight table construct of spec whose columns carry no SQL type, so values pass between the driver and the codec unconverted."""
    return table(spec.name, *(column(name) for name in spec.column_names))


def row_select(spec: TableSpec) -> Select[Any]:
    target = raw_table(spec)
    return select(*(target.c[name] for name in spec.column_names)).order_by(*(target.c[name] for name in spec.primary_key))


def _require(condition: bool, encoding: Encoding, value: object) -> None:
    if not condition:
        raise EncodingError(f"{type(value).__name__} value does not fit encoding {encoding}")


def encode_cell(encoding: Encoding, value: Any) -> Any:
    """The JSON value of a cell as the MySQL driver returned it."""
    if value is None:
        return None
    match encoding:
        case Encoding.BIGINT:
            _require(isinstance(value, int) and not isinstance(value, bool), encoding, value)
            return str(value)
        case Encoding.DECIMAL:
            _require(isinstance(value, int | Decimal) and not isinstance(value, bool), encoding, value)
            number = Decimal(value)
            _require(number == number.to_integral_value(), encoding, value)
            return str(int(number))
        case Encoding.INT:
            _require(isinstance(value, int) and not isinstance(value, bool), encoding, value)
            return int(value)
        case Encoding.BOOL:
            _require(isinstance(value, int) and value in (0, 1), encoding, value)
            return bool(value)
        case Encoding.STRING:
            _require(isinstance(value, str), encoding, value)
            return value
        case Encoding.DATETIME:
            _require(isinstance(value, datetime), encoding, value)
            moment = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
            return moment.strftime(_DATETIME_FORMAT)
        case Encoding.HEX:
            _require(isinstance(value, bytes | bytearray), encoding, value)
            return bytes(value).hex()
        case Encoding.JSON:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            _require(isinstance(text, str), encoding, value)
            return [json.loads(text)]


def decode_cell(encoding: Encoding, value: Any) -> Any:
    """The Python value of an encoded cell as it is bound into an insert: ints, bools, str, naive UTC datetimes, bytes, and JSON documents as their text."""
    if value is None:
        return None
    match encoding:
        case Encoding.BIGINT | Encoding.DECIMAL:
            _require(isinstance(value, str) and _INTEGER_RE.fullmatch(value) is not None, encoding, value)
            return int(value)
        case Encoding.INT:
            _require(isinstance(value, int) and not isinstance(value, bool), encoding, value)
            return value
        case Encoding.BOOL:
            _require(isinstance(value, bool), encoding, value)
            return value
        case Encoding.STRING:
            _require(isinstance(value, str), encoding, value)
            return value
        case Encoding.DATETIME:
            _require(isinstance(value, str) and _DATETIME_RE.fullmatch(value) is not None, encoding, value)
            return datetime.strptime(value, _DATETIME_FORMAT)
        case Encoding.HEX:
            _require(isinstance(value, str) and _HEX_RE.fullmatch(value) is not None, encoding, value)
            return bytes.fromhex(value)
        case Encoding.JSON:
            _require(isinstance(value, list) and len(value) == 1, encoding, value)
            return json.dumps(value[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode_row(spec: TableSpec, row: Mapping[str, Any]) -> dict[str, Any]:
    """The decoded cells of an encoded row, which must have exactly the columns of spec."""
    if set(row) != set(spec.column_names):
        raise EncodingError(f"row of {spec.name} has columns {sorted(row)}, expected {sorted(spec.column_names)}")
    return {item.name: decode_cell(item.encoding, row[item.name]) for item in spec.columns}


def encode_row(spec: TableSpec, values: Iterable[Any]) -> dict[str, Any]:
    return {item.name: encode_cell(item.encoding, value) for item, value in zip(spec.columns, values, strict=True)}


def canonical_line(row: Mapping[str, Any]) -> bytes:
    """The canonical JSON Lines encoding of an encoded row, newline included."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
