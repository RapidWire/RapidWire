"""The JSON encoding of contract values used by every contract endpoint: an object {"type", "value"} where type is the contract language's name of the value's type, for call arguments, call results and storage values alike.

Scalars: int as a decimal string, str as a string, bool as true or false, Account and Currency as their ID in a decimal string; this is exactly the form the execution record stores call arguments in.
Containers: list holds a JSON array of typed values, and dict a JSON array of {"key", "value"} entries whose key and value are typed values, so keys of every key type survive and storage dicts keep their canonical key order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, WithJsonSchema
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.values import ID_MAX, ID_MIN, Account, Currency, Key, Value, is_key, scalar_problem

__all__ = [
    "TYPED_VALUE_JSON_SCHEMA",
    "TYPE_NAMES",
    "TypedValueError",
    "TypedValueIn",
    "TypedValueOut",
    "decode_typed",
    "encode_typed",
]

TYPE_NAMES: Final = ("int", "str", "bool", "Account", "Currency", "list", "dict")

_INT_RE: Final = re.compile(r"(0|-?[1-9][0-9]*)")

TYPED_VALUE_JSON_SCHEMA: Final[JsonSchemaValue] = {
    "type": "object",
    "required": ["type", "value"],
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": list(TYPE_NAMES)},
        "value": {
            "description": "A decimal string for int, Account and Currency; a string for str; a boolean for bool; an array of typed values for list; an array of {key, value} typed pairs for dict."
        },
    },
    "description": 'A contract value, such as {"type": "int", "value": "5"} or {"type": "Account", "value": "123"}.',
    "examples": [{"type": "int", "value": "5"}],
}


class TypedValueError(ValueError):
    """A JSON typed value that does not follow the encoding; the message names where inside the value the problem is and never contains the input."""


def _int_text(text: object, where: str, minimum: int | None, maximum: int | None, limits: Limits) -> int:
    if not isinstance(text, str):
        raise TypedValueError(f"{where}: the value must be a decimal integer string.")
    if len(text) > len(str(limits.int_bound)) + 1 or _INT_RE.fullmatch(text) is None:
        raise TypedValueError(f"{where}: the value must be a decimal integer string without plus sign, spaces or leading zeros, within the contract int limit.")
    number = int(text)
    if minimum is not None and maximum is not None:
        if not minimum <= number <= maximum:
            raise TypedValueError(f"{where}: the ID must be between {minimum} and {maximum}.")
    elif not limits.int_ok(number):
        raise TypedValueError(f"{where}: the int is outside the contract int limit.")
    return number


def _fields(obj: object, names: tuple[str, str], where: str) -> tuple[Any, Any]:
    if not isinstance(obj, dict) or set(obj) != set(names):
        raise TypedValueError(f'{where}: expected an object with exactly the fields "{names[0]}" and "{names[1]}".')
    return obj[names[0]], obj[names[1]]


def _decode(obj: object, where: str, depth: int, limits: Limits) -> Value:
    type_name, raw = _fields(obj, ("type", "value"), where)
    if type_name == "int":
        return _int_text(raw, where, None, None, limits)
    if type_name == "Account":
        return Account(_int_text(raw, where, ID_MIN, ID_MAX, limits))
    if type_name == "Currency":
        return Currency(_int_text(raw, where, ID_MIN, ID_MAX, limits))
    if type_name == "bool":
        if not isinstance(raw, bool):
            raise TypedValueError(f"{where}: a bool value must be true or false.")
        return raw
    if type_name == "str":
        if not isinstance(raw, str) or scalar_problem(raw, limits) is not None:
            raise TypedValueError(f"{where}: a str value must be valid Unicode of at most {limits.str_max_length} characters.")
        return raw
    if type_name in ("list", "dict"):
        if depth > limits.storage_value_max_depth:
            raise TypedValueError(f"{where}: lists and dicts nest at most {limits.storage_value_max_depth} deep.")
        if not isinstance(raw, list) or len(raw) > limits.collection_max_length:
            raise TypedValueError(f"{where}: a {type_name} value must be an array of at most {limits.collection_max_length} items.")
        if type_name == "list":
            return [_decode(item, f"{where}[{index}]", depth + 1, limits) for index, item in enumerate(raw)]
        entries: dict[Key, Value] = {}
        for index, entry in enumerate(raw):
            raw_key, raw_value = _fields(entry, ("key", "value"), f"{where}[{index}]")
            key = _decode(raw_key, f"{where}[{index}].key", depth + 1, limits)
            if not is_key(key):
                raise TypedValueError(f"{where}[{index}].key: dict keys are int (64-bit), str, Account or Currency values.")
            if key in entries:
                raise TypedValueError(f"{where}[{index}].key: the key appears twice.")
            entries[key] = _decode(raw_value, f"{where}[{index}].value", depth + 1, limits)  # type: ignore[index]
        return entries
    raise TypedValueError(f"{where}: the type must be one of {', '.join(TYPE_NAMES)}.")


def decode_typed(obj: object, *, limits: Limits = DEFAULT_LIMITS) -> Value:
    """The contract value of a JSON typed value; raises TypedValueError for anything the encoding does not produce or that breaks a value limit."""
    return _decode(obj, "value", 1, limits)


def encode_typed(value: Value) -> dict[str, Any]:
    """The JSON typed value of a contract value; raises TypeError for a record, which no argument, result or storage value can be."""
    t = type(value)
    if t is bool:
        return {"type": "bool", "value": value}
    if t is int:
        return {"type": "int", "value": str(value)}
    if t is str:
        return {"type": "str", "value": value}
    if isinstance(value, Account):
        return {"type": "Account", "value": str(value.id)}
    if isinstance(value, Currency):
        return {"type": "Currency", "value": str(value.id)}
    if isinstance(value, list):
        return {"type": "list", "value": [encode_typed(item) for item in value]}
    if isinstance(value, dict):
        return {"type": "dict", "value": [{"key": encode_typed(key), "value": encode_typed(item)} for key, item in value.items()]}
    raise TypeError(f"a {t.__name__} has no JSON typed value")


@dataclass(frozen=True)
class _TypedValueInput:
    """Annotated[Value, _TypedValueInput()] metadata: a request field holding one JSON typed value, validated into the contract value."""

    def validate(self, obj: Any) -> Value:
        try:
            return decode_typed(obj)
        except TypedValueError as error:
            raise PydanticCustomError("typed_value", str(error).replace("{", "{{").replace("}", "}}")) from None

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_plain_validator_function(self.validate, serialization=core_schema.plain_serializer_function_ser_schema(encode_typed, when_used="always"))

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return dict(TYPED_VALUE_JSON_SCHEMA)


TypedValueIn = Annotated[Value, _TypedValueInput()]
TypedValueOut = Annotated[dict[str, Any], WithJsonSchema(TYPED_VALUE_JSON_SCHEMA)]
