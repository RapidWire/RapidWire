"""JSON number formats of the API: currency amounts are decimal strings in currency units with up to DECIMALS fractional digits, and IDs and other unbounded integers are decimal integer strings, both parsed exactly without floats.

Request fields use the input types (PositiveAmount, NonNegativeAmount, IdStr, PositiveIdStr, IntStr, NonNegativeIntStr or Annotated[int, IntString(minimum, maximum)]), which accept only JSON strings; response fields use the output types (Amount, IntOut), which take Python ints and serialize them as strings.
The parsers and formatters themselves live in rapidwire.core.amounts, shared with the Discord bot, and are re-exported here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from rapidwire.core.amounts import (
    AMOUNT_OUT_PATTERN,
    AMOUNT_PATTERN,
    DECIMALS,
    INT_PATTERN,
    MINIMAL_UNIT,
    UNITS_PER_CURRENCY_UNIT,
    NumberFormatError,
    format_amount,
    parse_amount,
    parse_int,
)
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN

__all__ = [
    "AMOUNT_OUT_PATTERN",
    "AMOUNT_PATTERN",
    "BIGINT_MIN",
    "DECIMALS",
    "INT_PATTERN",
    "MINIMAL_UNIT",
    "UNITS_PER_CURRENCY_UNIT",
    "Amount",
    "AmountString",
    "IdStr",
    "IntOut",
    "IntStr",
    "IntString",
    "NonNegativeAmount",
    "NonNegativeIntStr",
    "NumberFormatError",
    "PositiveAmount",
    "PositiveIdStr",
    "format_amount",
    "parse_amount",
    "parse_int",
]

BIGINT_MIN: Final = -(2**63)


def _pydantic_error(error: NumberFormatError) -> PydanticCustomError:
    # The message is used as a template, so braces are doubled.
    return PydanticCustomError(error.kind, error.message.replace("{", "{{").replace("}", "}}"))


def _string_schema(validate: Callable[[Any], int], serialize: Callable[[int], str]) -> CoreSchema:
    return core_schema.no_info_plain_validator_function(validate, serialization=core_schema.plain_serializer_function_ser_schema(serialize, return_schema=core_schema.str_schema(), when_used="always"))


@dataclass(frozen=True)
class AmountString:
    """Annotated[int, AmountString(...)] metadata: a request amount sent as a currency-unit string, validated to minimal units and serialized back canonically."""

    allow_zero: bool
    description: str

    def validate(self, value: Any) -> int:
        try:
            return parse_amount(value, allow_zero=self.allow_zero)
        except NumberFormatError as error:
            raise _pydantic_error(error) from None

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(self.validate, format_amount)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "pattern": AMOUNT_PATTERN, "description": self.description, "examples": ["12.5"]}


@dataclass(frozen=True)
class IntString:
    """Annotated[int, IntString(minimum, maximum)] metadata: a request integer sent as a decimal string within minimum..maximum; JSON numbers are rejected."""

    minimum: int
    maximum: int
    description: str | None = None

    def validate(self, value: Any) -> int:
        try:
            return parse_int(value, self.minimum, self.maximum)
        except NumberFormatError as error:
            raise _pydantic_error(error) from None

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(self.validate, str)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        text = self.description or f"An integer from {self.minimum} to {self.maximum}, as a decimal string."
        return {"type": "string", "pattern": INT_PATTERN, "description": text, "examples": ["123"]}


@dataclass(frozen=True)
class _AmountOut:
    def validate(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PydanticCustomError("amount_out_type", "An amount must be an integer number of minimal units.")
        if not AMOUNT_MIN <= value <= AMOUNT_MAX:
            raise PydanticCustomError("amount_range", "The amount is outside the supported range.")
        return value

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(self.validate, format_amount)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "pattern": AMOUNT_OUT_PATTERN, "description": f"An amount in currency units with exactly {DECIMALS} decimal places, as a string.", "examples": ["12.500"]}


@dataclass(frozen=True)
class _IntOut:
    def validate(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PydanticCustomError("int_out_type", "The value must be an integer.")
        return value

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return _string_schema(self.validate, str)

    def __get_pydantic_json_schema__(self, schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {"type": "string", "pattern": INT_PATTERN, "description": "An integer as a decimal string.", "examples": ["1269970084965912747"]}


PositiveAmount = Annotated[int, AmountString(allow_zero=False, description=f"A positive amount in currency units with at most {DECIMALS} decimal places, as a string.")]
NonNegativeAmount = Annotated[int, AmountString(allow_zero=True, description=f"An amount of zero or more in currency units with at most {DECIMALS} decimal places, as a string.")]
IdStr = Annotated[int, IntString(BIGINT_MIN, BIGINT_MAX, "A 64-bit ID as a decimal string; system accounts have negative IDs.")]
PositiveIdStr = Annotated[int, IntString(1, BIGINT_MAX, "A positive 64-bit ID as a decimal string.")]
IntStr = Annotated[int, IntString(AMOUNT_MIN, AMOUNT_MAX)]
NonNegativeIntStr = Annotated[int, IntString(0, AMOUNT_MAX)]
Amount = Annotated[int, _AmountOut()]
IntOut = Annotated[int, _IntOut()]
