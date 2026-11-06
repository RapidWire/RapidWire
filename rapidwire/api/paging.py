"""The paged response model and the page/per_page query parameters of every paged endpoint."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from rapidwire.core import paging as core_paging
from rapidwire.core.paging import MAX_WINDOW

__all__ = ["Page", "PageParams", "page_params"]


class Page[T](BaseModel):
    """One page of a list; total is exact unless total_is_lower_bound, in which case at least total items match and only the first MAX_WINDOW can be paged through."""

    items: list[T]
    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    total: int = Field(ge=0)
    total_is_lower_bound: bool

    @classmethod
    def build[S](cls, page: core_paging.Page[S], convert: Callable[[S], T]) -> Page[T]:
        """The response for a core page, converting each item with convert."""
        return cls(items=[convert(item) for item in page.items], page=page.page, per_page=page.per_page, total=page.total, total_is_lower_bound=page.total_is_lower_bound)


@dataclass(frozen=True)
class PageParams:
    """Validated paging arguments to pass to a core list function as page= and per_page=."""

    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def page_params(default: int, maximum: int) -> Callable[..., Coroutine[Any, Any, PageParams]]:
    """A dependency reading page (from 1) and per_page (1..maximum, default default) from the query string; a page reaching past the first MAX_WINDOW items is a 422 validation error."""
    if not 1 <= default <= maximum <= MAX_WINDOW:
        raise ValueError("page size defaults must satisfy 1 <= default <= maximum <= MAX_WINDOW")

    # The Query objects are parameter defaults because this module's annotations are strings and could not refer to maximum.
    async def dependency(
        page: int = Query(1, ge=1, le=MAX_WINDOW, description="The 1-based page number."),
        per_page: int = Query(default, ge=1, le=maximum, description=f"Items per page, at most {maximum}."),
    ) -> PageParams:
        params = PageParams(page=page, per_page=per_page)
        if params.offset + per_page > MAX_WINDOW:
            error: dict[str, Any] = {"type": "page_window", "loc": ("query", "page"), "msg": f"Only the first {MAX_WINDOW} results can be paged through; narrow the search.", "input": None}
            raise RequestValidationError([error])
        return params

    return dependency
