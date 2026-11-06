from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from rapidwire.api.errors import EXTRA_FIELD_LOC, KEY_LOC, STATUS_BY_ERROR, domain_error_body, install_error_handlers, status_for
from rapidwire.core import errors as core_errors
from rapidwire.core.errors import DomainError
from rapidwire.core.ledger import set_failure_output

LEAKY = "LEAKY-key-31415"


class Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: int


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inner: Inner
    counts: dict[int, int] = {}


def _all_subclasses(cls: type[DomainError]) -> set[type[DomainError]]:
    found: set[type[DomainError]] = set()
    pending = [cls]
    while pending:
        for sub in pending.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                pending.append(sub)
    return found


def _mapped_ancestor(cls: type[DomainError]) -> type[DomainError] | None:
    for ancestor in cls.__mro__:
        if ancestor is not DomainError and ancestor in STATUS_BY_ERROR:
            return ancestor
    return None


def test_every_domain_error_has_an_explicit_or_inherited_status() -> None:
    subclasses = {cls for cls in _all_subclasses(DomainError) if cls.__module__ == core_errors.__name__}
    assert len(subclasses) > 30
    unmapped = sorted(cls.__qualname__ for cls in subclasses if _mapped_ancestor(cls) is None)
    assert unmapped == []


def test_statuses_are_error_statuses() -> None:
    assert set(STATUS_BY_ERROR.values()) <= {401, 403, 404, 409, 422}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (core_errors.CurrencyNotFound(), 404),
        (core_errors.PoolNotFound(), 404),
        (core_errors.ContractNotFound(), 404),
        (core_errors.Unauthorized(), 401),
        (core_errors.PermissionDenied(), 403),
        (core_errors.GuildUnavailable(), 409),
        (core_errors.InsufficientBalance(), 409),
        (core_errors.SlippageExceeded(), 409),
        (core_errors.ContractPaused(), 409),
        (core_errors.PoolAlreadyExists(), 409),
        (core_errors.IdempotencyConflict(), 422),
        (core_errors.InvalidAmount(), 422),
        (core_errors.AmountOverflow(), 422),
        (core_errors.ContractReverted(), 422),
        (core_errors.ContractOutOfGas(), 422),
        (core_errors.error_from_code("some_future_code", "Text."), 422),
    ],
)
def test_status_examples(error: DomainError, status: int) -> None:
    assert status_for(error) == status


def test_body_carries_execution_child_and_contract_kind() -> None:
    error = core_errors.ContractCallFailed("No such function.", execution_id=77)
    error.child = {"type": "contract_call", "contract_function": "f", "code": "contract_call_failed", "message": "No such function."}
    set_failure_output(error, {"kind": "CALL_FAILED", "reason": "NO_FUNCTION", "gas_used": 3})
    assert domain_error_body(error) == {
        "error": {"code": "contract_call_failed", "message": "No such function.", "execution_id": "77", "kind": "CALL_FAILED", "reason": "NO_FUNCTION", "child": error.child}
    }
    assert domain_error_body(core_errors.InvalidArgument("Bad.")) == {"error": {"code": "invalid_argument", "message": "Bad."}}


async def test_validation_locs_never_echo_client_chosen_names() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/strict")
    async def strict(body: Strict) -> None:
        return None

    payload = {"name": "n", LEAKY: 1, "inner": {"amount": 1, LEAKY: 2}, "counts": {LEAKY: 3}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/strict", json=payload)
    assert response.status_code == 422
    assert LEAKY not in response.text
    details = response.json()["error"]["details"]
    assert sorted(tuple(detail["loc"]) for detail in details) == sorted([("body", EXTRA_FIELD_LOC), ("body", "inner", EXTRA_FIELD_LOC), ("body", "counts", KEY_LOC, "[key]")])
    assert {detail["type"] for detail in details} == {"extra_forbidden", "int_parsing"}
