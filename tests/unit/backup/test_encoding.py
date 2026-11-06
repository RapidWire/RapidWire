from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Column, Float, Integer, MetaData, Table

from rapidwire.backup.encoding import ColumnSpec, Encoding, EncodingError, TableSpec, canonical_line, decode_cell, decode_row, encode_cell, encode_row, table_specs
from rapidwire.backup.manifest import Manifest, ManifestError, TableEntry
from rapidwire.backup.totals import CurrencyTotals, InvariantTotals, PoolTotals, TotalsBuilder
from rapidwire.db.tables import metadata

E_ACUTE = "\U000000e9"
RENT = '\U00005bb6\U00008cc3 \U0001f3e0 "x"\n'


@pytest.mark.parametrize(
    ("encoding", "raw", "encoded", "decoded"),
    [
        (Encoding.BIGINT, -1269970084965912747, "-1269970084965912747", -1269970084965912747),
        (Encoding.DECIMAL, Decimal("99999999999999999999999999999999999999"), "99999999999999999999999999999999999999", 10**38 - 1),
        (Encoding.DECIMAL, Decimal(0), "0", 0),
        (Encoding.INT, 8000, 8000, 8000),
        (Encoding.BOOL, 1, True, True),
        (Encoding.BOOL, 0, False, False),
        (Encoding.STRING, RENT, RENT, RENT),
        (Encoding.DATETIME, datetime(2026, 10, 31, 3, 0, 0, 5), "2026-10-31T03:00:00.000005Z", datetime(2026, 10, 31, 3, 0, 0, 5)),
        (Encoding.HEX, b"\x00\xff\x10", "00ff10", b"\x00\xff\x10"),
        (Encoding.JSON, '{"b": [1, 2.5, null], "a": "\\u00e9"}', [{"a": E_ACUTE, "b": [1, 2.5, None]}], '{"a":"' + E_ACUTE + '","b":[1,2.5,null]}'),
        (Encoding.JSON, "null", [None], "null"),
    ],
)
def test_cells_round_trip(encoding: Encoding, raw: Any, encoded: Any, decoded: Any) -> None:
    assert encode_cell(encoding, raw) == encoded
    assert decode_cell(encoding, encoded) == decoded
    assert encode_cell(encoding, None) is None
    assert decode_cell(encoding, None) is None


def test_sql_null_and_a_json_null_document_stay_distinct() -> None:
    assert encode_cell(Encoding.JSON, None) is None
    assert encode_cell(Encoding.JSON, "null") == [None]
    assert decode_cell(Encoding.JSON, None) is None
    assert decode_cell(Encoding.JSON, [None]) == "null"


def test_aware_datetimes_are_written_in_utc() -> None:
    from datetime import timedelta, timezone

    moment = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    assert encode_cell(Encoding.DATETIME, moment) == "2026-10-31T03:00:00.000000Z"
    assert encode_cell(Encoding.DATETIME, moment.astimezone(UTC)) == "2026-10-31T03:00:00.000000Z"


@pytest.mark.parametrize(
    ("encoding", "value"),
    [
        (Encoding.BIGINT, "1.0"),
        (Encoding.BIGINT, "01"),
        (Encoding.BIGINT, 5),
        (Encoding.DECIMAL, "1e3"),
        (Encoding.INT, "5"),
        (Encoding.INT, True),
        (Encoding.BOOL, 1),
        (Encoding.DATETIME, "2026-10-31T03:00:00Z"),
        (Encoding.DATETIME, "2026-10-31 03:00:00.000000"),
        (Encoding.HEX, "ABCD"),
        (Encoding.HEX, "abc"),
        (Encoding.JSON, {"a": 1}),
        (Encoding.JSON, [1, 2]),
        (Encoding.STRING, 5),
    ],
)
def test_malformed_cells_are_rejected(encoding: Encoding, value: Any) -> None:
    with pytest.raises(EncodingError):
        decode_cell(encoding, value)


@pytest.mark.parametrize(
    ("encoding", "value"),
    [(Encoding.DECIMAL, Decimal("1.5")), (Encoding.BIGINT, True), (Encoding.BOOL, 2), (Encoding.HEX, "00"), (Encoding.DATETIME, "2026")],
)
def test_unexpected_driver_values_are_rejected(encoding: Encoding, value: Any) -> None:
    with pytest.raises(EncodingError):
        encode_cell(encoding, value)


def test_canonical_line_is_sorted_compact_ascii() -> None:
    line = canonical_line({"b": E_ACUTE, "a": [{"z": 1, "y": None}]})
    assert line == b'{"a":[{"y":null,"z":1}],"b":"\\u00e9"}\n'
    assert canonical_line(json.loads(line)) == line


def test_every_schema_table_and_stored_column_has_an_encoding() -> None:
    specs = {spec.name: spec for spec in table_specs(metadata)}
    assert list(specs) == sorted(metadata.tables)
    for table in metadata.tables.values():
        spec = specs[table.name]
        assert spec.column_names == tuple(column.name for column in table.columns if column.computed is None)
        assert spec.primary_key == tuple(column.name for column in table.primary_key.columns)
        assert spec.file == f"tables/{table.name}.jsonl"
    generated = {(table.name, column.name) for table in metadata.tables.values() for column in table.columns if column.computed is not None}
    assert {("currency", "active_symbol"), ("balance", "negative_currency_id"), ("pool", "active")} <= generated
    assert specs["balance"].columns[2] == ColumnSpec("amount", Encoding.DECIMAL)
    assert dict((item.name, item.encoding) for item in specs["api_token"].columns)["token_hash"] is Encoding.HEX
    assert dict((item.name, item.encoding) for item in specs["execution"].columns)["output"] is Encoding.JSON


def test_a_column_type_without_an_encoding_is_refused() -> None:
    other = MetaData()
    Table("odd", other, Column("odd_id", Integer, primary_key=True), Column("ratio", Float))
    with pytest.raises(EncodingError, match=r"odd\.ratio"):
        table_specs(other)


def test_decode_row_requires_exactly_the_columns() -> None:
    spec = TableSpec(name="t", columns=(ColumnSpec("a", Encoding.BIGINT), ColumnSpec("b", Encoding.STRING)), primary_key=("a",))
    assert decode_row(spec, encode_row(spec, [7, "x"])) == {"a": 7, "b": "x"}
    with pytest.raises(EncodingError, match="columns"):
        decode_row(spec, {"a": "7"})
    with pytest.raises(EncodingError, match="columns"):
        decode_row(spec, {"a": "7", "b": "x", "c": None})


def _manifest() -> Manifest:
    spec = TableSpec(name="balance", columns=(ColumnSpec("account_id", Encoding.BIGINT), ColumnSpec("amount", Encoding.DECIMAL)), primary_key=("account_id",))
    totals = InvariantTotals(currencies={5: CurrencyTotals(supply=10, balance_sum=0, stake_total=3)}, pools={2: PoolTotals(1, 2, 3000, 1000, 2000)})
    return Manifest(
        app_version="2.0.0",
        schema_revision="0005",
        created_at=datetime(2026, 10, 31, 3, 0, 0, 120000, tzinfo=UTC),
        tables=(TableEntry(spec=spec, rows=2, size=40, sha256="a" * 64),),
        totals=totals,
        invariants_ok=True,
        invariant_violations=0,
    )


def test_manifest_round_trips_with_ids_and_amounts_as_strings() -> None:
    manifest = _manifest()
    data = manifest.to_bytes()
    assert Manifest.from_bytes(data) == manifest
    document = json.loads(data)
    assert document["format"] == "rapidwire-export"
    assert document["format_version"] == 1
    assert document["created_at"] == "2026-10-31T03:00:00.120000Z"
    assert document["invariants"]["currencies"] == [{"currency_id": "5", "supply": "10", "balance_sum": "0", "stake_total": "3"}]
    assert document["invariants"]["verification"] == {"ok": True, "violations": 0}
    assert document["tables"][0]["columns"] == [{"name": "account_id", "encoding": "bigint"}, {"name": "amount", "encoding": "decimal"}]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(format="other"),
        lambda doc: doc.update(format_version=2),
        lambda doc: doc["tables"][0].update(file="../balance.jsonl"),
        lambda doc: doc["tables"][0].update(name="../x"),
        lambda doc: doc["tables"][0].update(sha256="xyz"),
        lambda doc: doc["tables"][0].update(rows=-1),
        lambda doc: doc["tables"][0]["columns"][0].update(encoding="float"),
        lambda doc: doc["tables"].append(dict(doc["tables"][0])),
        lambda doc: doc["invariants"]["currencies"][0].update(supply="ten"),
        lambda doc: doc["invariants"]["verification"].update(ok="yes"),
        lambda doc: doc.pop("schema_revision"),
    ],
)
def test_malformed_manifests_are_refused(mutate: Any) -> None:
    document = json.loads(_manifest().to_bytes())
    mutate(document)
    with pytest.raises(ManifestError):
        Manifest.from_bytes(json.dumps(document).encode())
    with pytest.raises(ManifestError):
        Manifest.from_bytes(b"not json")


def test_totals_builder_derives_supply_from_the_issuance_account() -> None:
    builder = TotalsBuilder()
    builder.add("balance", {"account_id": -5, "currency_id": 7, "amount": -100})
    builder.add("balance", {"account_id": 10, "currency_id": 7, "amount": 60})
    builder.add("balance", {"account_id": -1, "currency_id": 7, "amount": 40})
    builder.add("currency", {"currency_id": 7, "issuance_account_id": -5})
    builder.add("currency", {"currency_id": 8, "issuance_account_id": -6})
    builder.add("stake", {"account_id": 10, "currency_id": 7, "amount": 40})
    builder.add("pool", {"pool_id": 3, "reserve_a": 5, "reserve_b": 6, "total_shares": 1500, "locked_shares": 1000})
    builder.add("pool_share", {"pool_id": 3, "account_id": 10, "shares": 500})
    builder.add("transfer", {"anything": 1})
    totals = builder.build()
    assert totals.currencies == {7: CurrencyTotals(supply=100, balance_sum=0, stake_total=40), 8: CurrencyTotals(supply=0, balance_sum=0, stake_total=0)}
    assert totals.pools == {3: PoolTotals(5, 6, 1500, 1000, 500)}
    assert builder.unbalanced() == {}
    builder.add("balance", {"account_id": 11, "currency_id": 7, "amount": 1})
    assert builder.unbalanced() == {7: 1}
    differences = totals.differences(builder.build())
    assert differences == ["currency 7: balance_sum is 1, manifest says 0"]
