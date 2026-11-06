"""Conversion of response JSON into the client's dataclasses, driven by their type hints: decimal strings become Decimal, integer strings and numbers int, ISO 8601 times aware datetime, base64 text bytes and typed values contract values."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import functools
import re
import types
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Union, cast, get_args, get_origin, get_type_hints

from rapidwire_client.errors import ResponseFormatError
from rapidwire_client.models import Page
from rapidwire_client.values import ContractValue, decode_value

__all__ = ["parse_as"]

_INT_RE: Final = re.compile(r"0|-?[1-9][0-9]*")
_DECIMAL_RE: Final = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


def parse_as[T](hint: type[T], data: object) -> T:
    """The value of hint parsed from JSON data; raises ResponseFormatError naming the first field that does not fit."""
    return cast(T, _parse(hint, data, "response"))


@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


@functools.cache
def _page_hints(cls: type) -> dict[str, Any]:
    """The type hints of a Page subclass, with Page's own type parameter in scope for the inherited fields."""
    return get_type_hints(cls, localns={param.__name__: param for param in Page.__type_params__})


def _fail(where: str, expected: str) -> ResponseFormatError:
    return ResponseFormatError(f"{where}: expected {expected}")


def _object(data: object, where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _fail(where, "an object")
    return data


def _sequence(item: Any, data: object, where: str) -> tuple[object, ...]:
    if not isinstance(data, list):
        raise _fail(where, "an array")
    return tuple(_parse(item, value, f"{where}[{index}]") for index, value in enumerate(data))


def _int(data: object, where: str) -> int:
    if isinstance(data, int) and not isinstance(data, bool):
        return data
    if isinstance(data, str) and _INT_RE.fullmatch(data) is not None:
        return int(data)
    raise _fail(where, "an integer")


def _bool(data: object, where: str) -> bool:
    if not isinstance(data, bool):
        raise _fail(where, "a boolean")
    return data


def _dataclass(cls: type, data: object, where: str) -> object:
    obj = _object(data, where)
    hints = _hints(cls)
    values: dict[str, object] = {}
    for field in dataclasses.fields(cls):
        if field.name in obj:
            values[field.name] = _parse(hints[field.name], obj[field.name], f"{where}.{field.name}")
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise ResponseFormatError(f"{where}: the field {field.name!r} is missing")
    return cls(**values)


def _scalar(hint: Any, data: object, where: str) -> object:
    if hint is bool:
        return _bool(data, where)
    if hint is int:
        return _int(data, where)
    if hint is str:
        if not isinstance(data, str):
            raise _fail(where, "a string")
        return data
    if hint is Decimal:
        if not isinstance(data, str) or _DECIMAL_RE.fullmatch(data) is None:
            raise _fail(where, "a decimal string")
        return Decimal(data)
    if hint is datetime:
        try:
            parsed = datetime.fromisoformat(data) if isinstance(data, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            raise _fail(where, "an ISO 8601 time with a UTC offset")
        return parsed
    if hint is bytes:
        try:
            if isinstance(data, str):
                return base64.b64decode(data.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error):
            pass
        raise _fail(where, "standard base64 text")
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _dataclass(hint, data, where)
    raise TypeError(f"unsupported type hint {hint!r}")


def _parse(hint: Any, data: object, where: str) -> object:
    if hint is Any:
        return data
    if hint is ContractValue:
        try:
            return decode_value(data)
        except (TypeError, ValueError) as error:
            raise ResponseFormatError(f"{where}: {error}") from None
    origin = get_origin(hint)
    if origin is Union or origin is types.UnionType:
        options = [arg for arg in get_args(hint) if arg is not type(None)]
        if data is None and len(options) < len(get_args(hint)):
            return None
        if len(options) != 1:
            raise TypeError(f"unsupported type hint {hint!r}")
        return _parse(options[0], data, where)
    if origin is tuple:
        return _sequence(get_args(hint)[0], data, where)
    if origin is list:
        return list(_sequence(get_args(hint)[0], data, where))
    if origin is dict:
        key_hint, value_hint = get_args(hint)
        return {_parse(key_hint, key, f"{where} key"): _parse(value_hint, value, f"{where}.{key}") for key, value in _object(data, where).items()}
    if origin is Page:
        (item,) = get_args(hint)
        return _page(Page, item, data, where)
    if isinstance(hint, type) and issubclass(hint, Page):
        return _page(hint, _page_item(hint), data, where)
    return _scalar(hint, data, where)


def _page_item(cls: type) -> Any:
    """The item type T of a dataclass derived from Page[T]."""
    for base in getattr(cls, "__orig_bases__", ()):
        if get_origin(base) is Page:
            (item,) = get_args(base)
            return item
    raise TypeError(f"{cls!r} does not derive from a parameterized Page")


def _page(cls: type, item: Any, data: object, where: str) -> object:
    """A Page, or a subclass of Page[item] whose own fields are parsed from their type hints."""
    obj = _object(data, where)
    values: dict[str, object] = {
        "items": _sequence(item, obj.get("items"), f"{where}.items"),
        "page": _int(obj.get("page"), f"{where}.page"),
        "per_page": _int(obj.get("per_page"), f"{where}.per_page"),
        "total": _int(obj.get("total"), f"{where}.total"),
        "total_is_lower_bound": _bool(obj.get("total_is_lower_bound"), f"{where}.total_is_lower_bound"),
    }
    if cls is not Page:
        hints = _page_hints(cls)
        for field in dataclasses.fields(cls):
            if field.name not in values:
                if field.name not in obj:
                    raise ResponseFormatError(f"{where}: the field {field.name!r} is missing")
                values[field.name] = _parse(hints[field.name], obj[field.name], f"{where}.{field.name}")
    return cls(**values)
