"""The Idempotency-Key request header of write endpoints and the Idempotency-Replayed response header of answers served from an earlier execution under the same key."""

from __future__ import annotations

from typing import Annotated, Final, Protocol

from fastapi import Header, Response

from rapidwire.db.tables import IDEMPOTENCY_KEY_MAX

__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX",
    "IDEMPOTENCY_KEY_PATTERN",
    "REPLAYED_HEADER",
    "IdempotencyKeyHeader",
    "Replayable",
    "mark_replayed",
]

IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotency-Replayed"
# Printable ASCII without leading or trailing spaces.
IDEMPOTENCY_KEY_PATTERN: Final = r"^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$"

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_KEY_HEADER,
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=f"Optional key of 1 to {IDEMPOTENCY_KEY_MAX} printable ASCII characters; repeating a write with the same key returns the first result instead of running it again.",
    ),
]


class Replayable(Protocol):
    @property
    def replayed(self) -> bool: ...


def mark_replayed[T: Replayable](response: Response, result: T) -> T:
    """Set Idempotency-Replayed: true on response when result was answered from an earlier execution, and return result."""
    if result.replayed:
        response.headers[REPLAYED_HEADER] = "true"
    return result
