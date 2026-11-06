"""Page numbers of the bot's paged lists: page counts, the out-of-range check with the earlier bot's message, and page titles."""

from __future__ import annotations

from typing import Final

from rapidwire.bot.embeds import ITEMS_PER_PAGE
from rapidwire.bot.errors import UserInputError

__all__ = ["ITEMS_PER_PAGE", "PAGE_TITLE_FORMAT", "check_page", "page_count", "page_title"]

# The page position appended to a paged list's title.
PAGE_TITLE_FORMAT: Final = "{title} (ページ {page}/{pages})"


def page_count(total: int, per_page: int = ITEMS_PER_PAGE) -> int:
    """The number of pages holding total items, 0 when there are none."""
    return -(-max(total, 0) // per_page)


def check_page(page: int, total: int, *, per_page: int = ITEMS_PER_PAGE) -> int:
    """Return the page count for total items, raising UserInputError that states the page count when page is below 1 or beyond the last page; with no items every page from 1 is accepted, so the caller shows its empty message."""
    pages = page_count(total, per_page)
    if page < 1 or (pages > 0 and page > pages):
        raise UserInputError(f"指定されたページ ({page}) は存在しません。全 {pages} ページです。")
    return pages


def page_title(title: str, page: int, pages: int) -> str:
    """title followed by the page position, formatted by PAGE_TITLE_FORMAT."""
    return PAGE_TITLE_FORMAT.format(title=title, page=page, pages=pages)
