"""Contract values and their JSON typed form {"type", "value"}: int, str and bool map to the Python types, Account and Currency wrap an ID, list to list and dict to dict, so call arguments are built with encode_value() and results and storage read back with decode_value()."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["Account", "ContractKey", "ContractValue", "Currency", "decode_value", "encode_args", "encode_value"]

_INT_RE: Final = re.compile(r"0|-?[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Account:
    """A contract Account value: the account ID."""

    id: int

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise TypeError("an Account ID must be an int")


@dataclass(frozen=True, slots=True)
class Currency:
    """A contract Currency value: the currency ID."""

    id: int

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise TypeError("a Currency ID must be an int")


type ContractKey = int | str | Account | Currency
type ContractValue = int | str | bool | Account | Currency | list[ContractValue] | dict[ContractKey, ContractValue]


def _is_key(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, str | Account | Currency)


def encode_value(value: ContractValue) -> dict[str, Any]:
    """The JSON typed value of a contract value; raises TypeError for anything else, such as a float, None or a dict key that cannot be one."""
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, Account):
        return {"type": "Account", "value": str(value.id)}
    if isinstance(value, Currency):
        return {"type": "Currency", "value": str(value.id)}
    if isinstance(value, list):
        return {"type": "list", "value": [encode_value(item) for item in value]}
    if isinstance(value, dict):
        entries = []
        for key, item in value.items():
            if not _is_key(key):
                raise TypeError(f"a {type(key).__name__} cannot be a contract dict key")
            entries.append({"key": encode_value(key), "value": encode_value(item)})
        return {"type": "dict", "value": entries}
    raise TypeError(f"a {type(value).__name__} has no contract typed value")


def encode_args(values: Iterable[ContractValue]) -> list[dict[str, Any]]:
    """The typed values of call arguments, in order."""
    return [encode_value(value) for value in values]


def _fields(obj: object, names: tuple[str, str], where: str) -> tuple[Any, Any]:
    if not isinstance(obj, dict) or set(obj) != set(names):
        raise ValueError(f'{where}: expected an object with exactly the fields "{names[0]}" and "{names[1]}"')
    return obj[names[0]], obj[names[1]]


def _int(raw: object, where: str) -> int:
    if not isinstance(raw, str) or _INT_RE.fullmatch(raw) is None:
        raise ValueError(f"{where}: expected a decimal integer string")
    return int(raw)


def _decode(obj: object, where: str) -> ContractValue:
    kind, raw = _fields(obj, ("type", "value"), where)
    if kind == "int":
        return _int(raw, where)
    if kind == "Account":
        return Account(_int(raw, where))
    if kind == "Currency":
        return Currency(_int(raw, where))
    if kind == "bool":
        if not isinstance(raw, bool):
            raise ValueError(f"{where}: a bool value must be true or false")
        return raw
    if kind == "str":
        if not isinstance(raw, str):
            raise ValueError(f"{where}: a str value must be a string")
        return raw
    if kind in ("list", "dict"):
        if not isinstance(raw, list):
            raise ValueError(f"{where}: a {kind} value must be an array")
        if kind == "list":
            return [_decode(item, f"{where}[{index}]") for index, item in enumerate(raw)]
        entries: dict[ContractKey, ContractValue] = {}
        for index, entry in enumerate(raw):
            raw_key, raw_value = _fields(entry, ("key", "value"), f"{where}[{index}]")
            key = _decode(raw_key, f"{where}[{index}].key")
            if not isinstance(key, int | str | Account | Currency) or isinstance(key, bool):
                raise ValueError(f"{where}[{index}].key: dict keys are int, str, Account or Currency values")
            if key in entries:
                raise ValueError(f"{where}[{index}].key: the key appears twice")
            entries[key] = _decode(raw_value, f"{where}[{index}].value")
        return entries
    raise ValueError(f"{where}: unknown type {kind!r}")


def decode_value(obj: object) -> ContractValue:
    """The contract value of a JSON typed value; raises ValueError for an object that does not follow the encoding."""
    return _decode(obj, "value")
