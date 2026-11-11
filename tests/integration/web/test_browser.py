"""Headless Chromium over the served web explorer: every page at phone and desktop widths in light and dark, the largest amounts on narrow screens, the explorer search and filters, escaping of stored text, and the operator page's token handling and verified download."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from rapidwire.db.types import AMOUNT_MAX
from tests.integration.web.conftest import Site
from tests.integration.web.seed import CLAIM_MARKUP, HUGE_AMOUNT, INJECTED_IDS, STORAGE_MARKUP, Seed

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, ConsoleMessage, Frame, Page, Request, Response, Route

VIEWPORTS = ((360, 800), (1280, 900))
SCHEMES = ("light", "dark")
# Pages checked at once in one browser context.
CONCURRENT_PAGES = 4
# How long the network must stay quiet before a page counts as settled.
QUIET_SECONDS = 0.25
SETTLE_TIMEOUT_SECONDS = 15.0
# An amount as the pages format it: thousands separators and exactly 3 decimals.
AMOUNT_TEXT = re.compile(r"(?<![0-9.,])[0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{3}(?![0-9])")
USERNAME_PATH = re.compile(r"^/v1/users/[0-9]+/name$")
# The account page asks whether an account has a contract and expects 404 for accounts without one.
CONTRACT_PROBE_PATH = re.compile(r"^/v1/contracts/-?[0-9]+$")
FAILED_RESOURCE = "Failed to load resource"
UNKNOWN_ID = "987654321987"
UNRECORDED_ACCOUNT = "555555555555"
# The not-found page, which shows only the requested path and makes no API request, used as a blank page for scripts run with page.evaluate().
BLANK_PAGE = "/no-such-page.html"
# Elements whose content must fit inside them; td cells are checked only while their table is shown as stacked cards.
FIT_CHECK = """() => {
  const problems = [];
  const describe = (el) => `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${[...el.classList].join(".")} "${el.textContent.trim().slice(0, 40)}"`;
  for (const el of document.querySelectorAll("#main td, #main .kv dd, #main .feed li, #main .table-wrap")) {
    const table = el.closest("table") || el.querySelector("table");
    const cards = table !== null && table.classList.contains("data-table--cards") && getComputedStyle(table).display === "block";
    if ((el.tagName === "TD" || el.classList.contains("table-wrap")) && !cards) {
      continue;
    }
    if (el.scrollWidth > el.clientWidth) {
      problems.push(`${describe(el)} overflows: scrollWidth ${el.scrollWidth} > clientWidth ${el.clientWidth}`);
    }
  }
  return problems;
}"""


def amount_text(minimal_units: int) -> str:
    """A non-negative minimal-unit amount as the pages show it."""
    return f"{minimal_units // 1000:,}.{minimal_units % 1000:03d}"


HUGE_TEXT = amount_text(HUGE_AMOUNT)
SUPPLY_TEXT = amount_text(AMOUNT_MAX)


@dataclass(frozen=True)
class PageCase:
    """One page visit: the expected document status, whether the main content ends in an error state, texts and selectors that must be present (at least one selector only page scripts render), and API paths allowed to answer 404."""

    name: str
    path: str
    selectors: tuple[str, ...]
    status: int = 200
    error_state: bool = False
    texts: tuple[str, ...] = ()
    allow_404: tuple[re.Pattern[str], ...] = ()
    amount: bool = False


@dataclass
class Visit:
    """What one page did: same-origin responses as (path, status, is the document), failed requests, console errors and uncaught exceptions."""

    origin: str
    responses: list[tuple[str, int, bool]] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    console_errors: list[tuple[str, str]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    in_flight: set[Request] = field(default_factory=set)
    last_activity: float = field(default_factory=time.monotonic)

    def api_paths(self) -> list[str]:
        return [path for path, _status, _document in self.responses if path.startswith("/v1/")]


def _exact(path: str) -> re.Pattern[str]:
    return re.compile(f"^{re.escape(path)}$")


def page_cases(seed: Seed) -> list[PageCase]:
    gold_amount = "1,234.567"
    return [
        PageCase("index", "/", selectors=("#live-stats:not([hidden])",)),
        PageCase("explorer", "/explorer.html", texts=(HUGE_TEXT,), selectors=("#executions .badge--success", "#transfers li"), amount=True),
        PageCase(
            "account",
            f"/account.html?id={seed.bob}",
            texts=(HUGE_TEXT,),
            selectors=("#executions tbody tr", '#executions-more a[href^="/explorer.html?initiator_id="]'),
            amount=True,
        ),
        PageCase(
            "account-claims",
            f"/account.html?id={seed.bob}&tab=claims",
            texts=(CLAIM_MARKUP,),
            selectors=("#claims tbody tr", '#claims a[href^="/execution.html?id="]'),
        ),
        PageCase("account-claims-claimant", f"/account.html?id={seed.bob}&tab=claims&cr=claimant", selectors=("#claims .state--empty", "#claims-role option[value=claimant]:checked")),
        PageCase("account-claims-payer", f"/account.html?id={seed.bob}&tab=claims&cr=payer&cs=pending", texts=(CLAIM_MARKUP,), selectors=("#claims tbody tr",)),
        PageCase("account-system", "/account.html?id=-1", selectors=("#system-details .kv",), amount=True),
        PageCase("account-issuance-huge", f"/account.html?id={seed.huge_issuance}", texts=(SUPPLY_TEXT,), selectors=("#system-details .kv", "#balances tbody tr"), amount=True),
        PageCase("account-contract", f"/account.html?id={seed.contract}", selectors=('#account-badges a.badge[href^="/contract.html"]',)),
        PageCase(
            "account-unrecorded", f"/account.html?id={UNRECORDED_ACCOUNT}", selectors=("#account-unrecorded", "#executions .state--empty"), allow_404=(_exact(f"/v1/accounts/{UNRECORDED_ACCOUNT}"),)
        ),
        PageCase("currency", f"/currency.html?id={seed.gold}", texts=(seed.gold_symbol,), selectors=("#details .kv", "#history tbody tr"), amount=True),
        PageCase("currency-symbol", f"/currency.html?symbol={seed.gold_symbol}", texts=(seed.gold_symbol,), selectors=("#details .kv",), amount=True),
        PageCase("currency-huge", f"/currency.html?id={seed.huge}", texts=(SUPPLY_TEXT, HUGE_TEXT), selectors=("#details .kv", "#history tbody tr"), amount=True),
        PageCase("transfer", f"/transfer.html?id={seed.transfer_id}", texts=(gold_amount,), selectors=(".badge--success", '#execution-details a[href^="/execution.html?id="]')),
        PageCase("transfer-huge", f"/transfer.html?id={seed.huge_transfer}", texts=(HUGE_TEXT,), selectors=("#transfer-details .kv",)),
        PageCase("execution", f"/execution.html?id={seed.transfer_execution}", texts=(gold_amount,), selectors=("#execution-badges .badge--success",)),
        PageCase("execution-failed", f"/execution.html?id={seed.failed_transfer_execution}", selectors=("#execution-badges .badge--failed",)),
        PageCase("execution-swap", f"/execution.html?id={seed.swap_execution}", selectors=("#execution-badges .badge--success",), amount=True),
        PageCase("execution-stake", f"/execution.html?id={seed.stake_execution}", selectors=("#execution-badges .badge--success",), amount=True),
        PageCase("execution-claim", f"/execution.html?id={seed.claim_execution}", texts=(CLAIM_MARKUP,), selectors=("#execution-badges .badge--success",)),
        PageCase("execution-call", f"/execution.html?id={seed.contract_call_execution}", texts=(STORAGE_MARKUP,), selectors=("#execution-badges .badge--success",)),
        PageCase("execution-call-reverted", f"/execution.html?id={seed.contract_failed_execution}", texts=("always fails",), selectors=("#execution-badges .badge--reverted",)),
        PageCase("pools", "/pools.html", selectors=("#pools tbody tr",), amount=True),
        PageCase("pools-pair", f"/pools.html?x={seed.gold_symbol}&y=SILV", selectors=("#pair-result tbody tr",), amount=True),
        PageCase("pool", f"/pool.html?id={seed.pool_high}", selectors=("#pool-overview .kv", '#pool-overview a[href^="/execution.html?id="]'), amount=True),
        PageCase("pool-low", f"/pool.html?id={seed.pool_low}", selectors=("#pool-overview .kv",), amount=True),
        PageCase(
            "contract",
            f"/contract.html?id={seed.contract}",
            texts=(STORAGE_MARKUP,),
            selectors=(
                "#contract-overview .kv",
                '#contract-overview a[href^="/execution.html?id="]',
                '#contract-versions a[href^="/execution.html?id="]',
                "#contract-versions .badge--muted",
                f'#contract-calls a[href="/execution.html?id={seed.contract_call_execution}"]',
                f'#contract-calls a[href="/execution.html?id={seed.contract_failed_execution}"]',
            ),
        ),
        PageCase("operator", "/operator.html", selectors=("#backups .state--empty",)),
        PageCase("execution-invalid-id", "/execution.html?id=abc", selectors=("#execution-error .state--error",), error_state=True),
        PageCase("account-invalid-id", "/account.html?id=12x", selectors=("#account-error .state--error",), error_state=True),
        PageCase("execution-unknown", f"/execution.html?id={UNKNOWN_ID}", selectors=("#execution-error .state--error",), error_state=True, allow_404=(_exact(f"/v1/executions/{UNKNOWN_ID}"),)),
        PageCase("not-found", "/no-such-page.html", selectors=("#missing-path code",), status=404, texts=("/no-such-page.html",)),
    ]


def _attach(page: Page, visit: Visit) -> None:
    def on_request(request: Request) -> None:
        visit.in_flight.add(request)
        visit.last_activity = time.monotonic()

    def on_done(request: Request) -> None:
        visit.in_flight.discard(request)
        visit.last_activity = time.monotonic()

    def on_navigated(frame: Frame) -> None:
        # Requests of the previous document end without an event when the main frame navigates.
        if frame == page.main_frame:
            visit.in_flight = {request for request in visit.in_flight if request.is_navigation_request()}
            visit.last_activity = time.monotonic()

    def on_failed(request: Request) -> None:
        on_done(request)
        visit.failed_requests.append(f"{request.url} ({request.failure})")

    def on_response(response: Response) -> None:
        parts = urlsplit(response.url)
        if f"{parts.scheme}://{parts.netloc}" == visit.origin:
            visit.responses.append((parts.path, response.status, response.request.resource_type == "document"))

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            visit.console_errors.append((message.text, str(message.location.get("url", ""))))

    page.on("request", on_request)
    page.on("framenavigated", on_navigated)
    page.on("requestfinished", on_done)
    page.on("requestfailed", on_failed)
    page.on("response", on_response)
    page.on("console", on_console)
    page.on("pageerror", lambda error: visit.page_errors.append(str(error)))


async def _settle(page: Page, visit: Visit) -> None:
    """Wait until no request has been running for QUIET_SECONDS and no loading state is left in the main content."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        quiet = not visit.in_flight and time.monotonic() - visit.last_activity >= QUIET_SECONDS
        if quiet and await page.evaluate("() => document.querySelector('#main .state--loading') === null"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{page.url} did not settle within {SETTLE_TIMEOUT_SECONDS:.0f}s (requests in flight: {sorted(request.url for request in visit.in_flight)})")


def _allowed(path: str, status: int, document: bool, allow_404: Iterable[re.Pattern[str]], document_404: bool) -> bool:
    if status == 503 and USERNAME_PATH.match(path):
        return True
    if status != 404:
        return False
    if not path.startswith("/v1/"):
        return document and document_404
    return bool(CONTRACT_PROBE_PATH.match(path)) or any(pattern.match(path) for pattern in allow_404)


def visit_problems(visit: Visit, *, allow_404: Iterable[re.Pattern[str]] = (), allow_status: Iterable[tuple[str, int]] = (), document_404: bool = False) -> list[str]:
    """Problems of a settled visit: page errors, same-origin responses of 400 or more that the case does not expect, failed requests, and console errors other than the browser's report of an allowed status."""
    allow_404 = tuple(allow_404)
    explicit = set(allow_status)
    problems = [f"page error: {error}" for error in visit.page_errors]
    allowed_paths: set[str] = set()
    for path, status, document in visit.responses:
        if status < 400:
            continue
        if (path, status) in explicit or _allowed(path, status, document, allow_404, document_404):
            allowed_paths.add(path)
            continue
        problems.append(f"{'document' if document else 'request'} {path} answered {status}")
    problems += [f"request failed: {url}" for url in visit.failed_requests]
    for text, url in visit.console_errors:
        if text.startswith(FAILED_RESOURCE) and urlsplit(url).path in allowed_paths:
            continue
        problems.append(f"console error: {text} at {url}")
    return problems


async def layout_problems(page: Page) -> list[str]:
    """Horizontal overflow of the page, content wider than its cell, list item, value or stacked table, and injected elements."""
    problems = []
    scroll_width, inner_width = await page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
    if scroll_width > inner_width:
        problems.append(f"horizontal overflow: scrollWidth {scroll_width} > innerWidth {inner_width}")
    problems += await page.evaluate(FIT_CHECK)
    injected = await page.evaluate("(ids) => ids.filter((id) => document.getElementById(id) !== null).concat(window.injected === undefined ? [] : ['window.injected'])", list(INJECTED_IDS))
    if injected:
        problems.append(f"injected markup became elements: {injected}")
    return problems


@asynccontextmanager
async def opened(context: BrowserContext, url: str) -> AsyncIterator[tuple[Page, Visit, int]]:
    """Open url in a new page of the context, yield it with its visit record and document status once settled, and close it afterwards."""
    page = await context.new_page()
    parts = urlsplit(url)
    visit = Visit(origin=f"{parts.scheme}://{parts.netloc}")
    _attach(page, visit)
    try:
        response = await page.goto(url, wait_until="load")
        assert response is not None
        await _settle(page, visit)
        yield page, visit, response.status
    finally:
        await page.close()


async def evaluate_case(page: Page, visit: Visit, status: int, case: PageCase) -> list[str]:
    """Problems of a settled page against its case: request and console problems, layout, the main content state, and the expected texts, elements and amounts."""
    problems = visit_problems(visit, allow_404=case.allow_404, document_404=case.status == 404)
    if status != case.status:
        problems.append(f"document status {status}, expected {case.status}")
    problems += await layout_problems(page)
    # A failed request shows an alert; the error section of a failed execution is a note.
    alerts = page.locator("#main .state--error[role=alert]")
    errors = await alerts.count()
    if case.error_state and errors == 0:
        problems.append("expected an error state in the main content")
    if not case.error_state and errors > 0:
        problems.append(f"unexpected error state: {await alerts.first.inner_text()}")
    main_text = await page.locator("main").inner_text()
    problems += [f"missing text {text!r}" for text in case.texts if text not in main_text]
    for selector in case.selectors:
        if await page.locator(selector).count() == 0:
            problems.append(f"missing element {selector}")
    if case.amount and AMOUNT_TEXT.search(main_text) is None:
        problems.append("no amount with 3 decimals")
    return problems


async def check_case(context: BrowserContext, site: Site, case: PageCase) -> list[str]:
    async with opened(context, site.base_url + case.path) as (page, visit, status):
        problems = await evaluate_case(page, visit, status, case)
    return [f"{case.name} ({case.path}): {problem}" for problem in problems]


async def test_every_case_needs_a_rendered_element(site: Site) -> None:
    assert all(case.selectors for case in page_cases(site.seed))


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("viewport", VIEWPORTS, ids=[f"{width}x{height}" for width, height in VIEWPORTS])
async def test_every_page_renders(browser: Browser, site: Site, viewport: tuple[int, int], scheme: str) -> None:
    width, height = viewport
    context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark" if scheme == "dark" else "light")
    try:
        limit = asyncio.Semaphore(CONCURRENT_PAGES)

        async def run(case: PageCase) -> list[str]:
            async with limit:
                return await check_case(context, site, case)

        results = await asyncio.gather(*(run(case) for case in page_cases(site.seed)))
    finally:
        await context.close()
    problems = [problem for result in results for problem in result]
    assert problems == [], "\n".join(problems)
    assert site.discord_requests == []


async def test_the_fit_check_detects_an_overflowing_card(browser: Browser, site: Site) -> None:
    context = await browser.new_context(viewport={"width": 360, "height": 800})
    try:
        async with opened(context, site.base_url + f"/currency.html?id={site.seed.huge}") as (page, _visit, _status):
            assert await layout_problems(page) == []
            # The policy refuses style elements, so the old one-line amounts are restored through the CSSOM.
            await page.evaluate("() => document.querySelectorAll('.amount').forEach((el) => el.style.setProperty('white-space', 'nowrap', 'important'))")
            assert any("overflows" in problem for problem in await layout_problems(page))
    finally:
        await context.close()


async def test_dark_mode_changes_the_background(browser: Browser, site: Site) -> None:
    backgrounds: dict[str, dict[str, str]] = {}
    for scheme in SCHEMES:
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark" if scheme == "dark" else "light")
        try:
            for path in ("/", f"/execution.html?id={site.seed.transfer_execution}", "/no-such-page.html"):
                async with opened(context, site.base_url + path) as (page, _visit, _status):
                    backgrounds.setdefault(path, {})[scheme] = await page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        finally:
            await context.close()
    for path, colours in backgrounds.items():
        assert colours["light"] != colours["dark"], (path, colours)


async def test_explorer_search_and_filters(browser: Browser, site: Site) -> None:
    seed = site.seed
    lookups = (re.compile(r"^/v1/(executions|transfers|currencies|accounts)/-?[0-9]+$"),)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        async with opened(context, site.base_url + "/explorer.html") as (page, visit, _status):
            await page.fill("#site-search", str(seed.swap_execution))
            await page.press("#site-search", "Enter")
            target = f"/execution.html?id={seed.swap_execution}"
            await page.wait_for_url(re.compile(rf"(\?q={seed.swap_execution}$)|({re.escape(target)}$)"))
            if "?q=" in page.url:
                link = page.locator(f'#search-results a[href="{target}"]')
                await link.wait_for()
                await link.click()
            await page.wait_for_url(re.compile(re.escape(target) + "$"))
            await page.locator("#execution-badges .badge--success").wait_for()

            await page.goto(site.base_url + "/explorer.html?q=gold")
            await page.wait_for_url(re.compile(rf"/currency\.html\?id={seed.gold}$"))
            await page.locator("#currency-title", has_text=seed.gold_symbol).wait_for()

            await page.goto(site.base_url + f"/explorer.html?q={seed.huge}")
            await page.wait_for_url(re.compile(rf"/currency\.html\?id={seed.huge}$"))
            await page.locator("#details .kv").wait_for()

            await page.goto(site.base_url + f"/explorer.html?q={seed.bob}")
            await page.wait_for_url(re.compile(rf"/account\.html\?id={seed.bob}$"))
            await page.locator("#executions tbody tr").first.wait_for()

            await page.goto(site.base_url + f"/explorer.html?q={UNKNOWN_ID}")
            await page.locator("#search-results .state--empty", has_text=UNKNOWN_ID).wait_for()
            await _settle(page, visit)
            assert page.url.endswith(f"/explorer.html?q={UNKNOWN_ID}")
            assert await page.locator("#search-results a").count() == 0

            await page.goto(site.base_url + "/explorer.html?q=-424242")
            await page.locator("#search-results .state--empty", has_text="-424242").wait_for()

            await page.goto(site.base_url + "/explorer.html")
            await page.locator("#executions tbody tr").first.wait_for()
            unfiltered = await page.locator("#executions tbody tr").count()
            await page.select_option("#filter-type", "contract_call")
            await page.locator('#filters [type="submit"]').click()
            await page.wait_for_url(re.compile(r"execution_type=contract_call"))
            await _settle(page, visit)
            rows = page.locator("#executions tbody tr")
            assert await rows.count() == 2
            assert unfiltered > 2
            for text in await rows.all_inner_texts():
                assert "contract_call" in text
            await page.locator("#executions .badge--reverted").wait_for()
            assert visit_problems(visit, allow_404=lookups) == []
    finally:
        await context.close()


async def test_pages_beyond_the_window_are_refused_without_a_request(browser: Browser, site: Site) -> None:
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        async with opened(context, site.base_url + "/explorer.html?page=501&status=success") as (page, visit, _status):
            assert "/v1/executions" not in visit.api_paths()
            await page.locator("#executions .state--error[role=alert]").wait_for()
            first = page.locator('#executions-pager a[href="/explorer.html?status=success#executions-title"]')
            assert await first.count() == 1
            assert visit_problems(visit) == []
        async with opened(context, site.base_url + f"/explorer.html?page=400&q={UNKNOWN_ID}") as (page, visit, _status):
            await page.locator("#executions .state--empty").wait_for()
            await page.locator("#search-results .state--empty").wait_for()
            total = (await (await page.request.get(site.base_url + "/v1/executions?per_page=1")).json())["total"]
            last = max(1, -(-total // 20))
            previous = page.locator('#executions-pager a[rel="prev"]')
            assert await previous.get_attribute("href") == f"/explorer.html?page={last}#executions-title"
            assert visit_problems(visit, allow_404=(re.compile(rf"^/v1/(executions|transfers|currencies|accounts)/{UNKNOWN_ID}$"),)) == []
    finally:
        await context.close()


SAME_PAGE_URLS = """async () => {
  const app = await import("/assets/app.js");
  const start = window.location.pathname + window.location.search;
  const results = {};
  window.history.pushState(null, "", "/explorer.html?status=success");
  results.canonical = [app.withParams({ page: 2 }), app.withParams({ status: null })];
  window.history.pushState(null, "", "/.//evil.example/explorer.html?status=success");
  results.pathname = window.location.pathname;
  results.doubleSlash = [app.withParams({ page: 2 }), app.withParams({ status: null })];
  results.resolved = results.doubleSlash.map((url) => new URL(url, window.location.href).origin === window.location.origin);
  window.history.replaceState(null, "", app.withParams({ page: 3 }));
  results.replaced = window.location.pathname + window.location.search;
  window.history.replaceState(null, "", start);
  results.refused = ["//evil.example/", "/\\\\evil.example/", "javascript:alert(1)"].filter((href) => {
    try {
      app.h("a", { href });
      return false;
    } catch {
      return true;
    }
  });
  return results;
}"""

LOOKUP_RETRIES = """async (ids) => {
  const app = await import("/assets/app.js");
  const outcome = async (promise) => promise.then((value) => (value === null ? "null" : "found"), (error) => `error ${error.status}`);
  const [first, concurrent] = await Promise.all([outcome(app.lookupAccount(ids.flaky)), outcome(app.lookupAccount(ids.flaky))]);
  const second = await outcome(app.lookupAccount(ids.flaky));
  const third = await outcome(app.lookupAccount(ids.flaky));
  const missing = [await outcome(app.lookupAccount(ids.missing)), await outcome(app.lookupAccount(ids.missing))];
  return { first, concurrent, second, third, missing };
}"""

CURRENCY_INDEX_RETRIES = """async () => {
  const app = await import("/assets/app.js");
  const sizes = [];
  for (let attempt = 0; attempt < 4; attempt += 1) {
    sizes.push((await app.loadCurrencyIndex()).size);
  }
  return sizes;
}"""


async def test_same_page_urls_stay_on_the_origin(browser: Browser, site: Site) -> None:
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await context.new_page()
        await page.goto(site.base_url + BLANK_PAGE)
        results = await page.evaluate(SAME_PAGE_URLS)
        assert results["canonical"] == ["/explorer.html?status=success&page=2", "/explorer.html"]
        assert results["pathname"] == "//evil.example/explorer.html"
        assert results["doubleSlash"] == ["?status=success&page=2", "?"]
        assert results["resolved"] == [True, True]
        assert results["replaced"] == "//evil.example/explorer.html?status=success&page=3"
        assert results["refused"] == ["//evil.example/", "/\\evil.example/", "javascript:alert(1)"]
    finally:
        await context.close()


async def test_failed_lookups_are_retried_and_404_is_kept(browser: Browser, site: Site) -> None:
    flaky, missing = "-777001", "-777002"
    calls: dict[str, int] = {flaky: 0, missing: 0}

    async def answer(route: Route) -> None:
        account_id = urlsplit(route.request.url).path.rsplit("/", 1)[1]
        calls[account_id] += 1
        if account_id == flaky and calls[account_id] == 1:
            await route.fulfill(status=500, json={"error": {"code": "internal_error", "message": "boom"}})
        elif account_id == flaky:
            await route.fulfill(status=200, json={"account_id": flaky, "kind": "stake_custody", "currency_id": None, "pool_id": None, "created_at": "2026-01-01T00:00:00Z"})
        else:
            await route.fulfill(status=404, json={"error": {"code": "account_not_found", "message": "missing"}})

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await context.new_page()
        await page.goto(site.base_url + BLANK_PAGE)
        await page.route(re.compile(rf"/v1/accounts/({flaky}|{missing})$"), answer)
        results = await page.evaluate(LOOKUP_RETRIES, {"flaky": flaky, "missing": missing})
        assert results == {"first": "error 500", "concurrent": "error 500", "second": "found", "third": "found", "missing": ["null", "null"]}
        assert calls == {flaky: 2, missing: 1}
    finally:
        await context.close()


async def test_a_failed_currency_index_is_empty_and_retried(browser: Browser, site: Site) -> None:
    answers: list[tuple[int, object]] = [
        (200, {"unexpected": []}),
        (500, {"error": {"code": "internal_error", "message": "boom"}}),
        (200, {"items": [{"currency_id": "1", "symbol": "RWC"}], "next_cursor": None}),
    ]
    calls = 0

    async def answer(route: Route) -> None:
        nonlocal calls
        calls += 1
        status, body = answers[min(calls, len(answers)) - 1]
        await route.fulfill(status=status, json=body)

    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        await page.goto(site.base_url + BLANK_PAGE)
        await page.route(re.compile(r"/v1/currencies\?"), answer)
        assert await page.evaluate(CURRENCY_INDEX_RETRIES) == [0, 0, 1, 1]
        assert calls == 3
        assert page_errors == []
    finally:
        await context.close()


async def test_operator_page_lists_and_verifies_a_download(browser: Browser, site: Site, tmp_path: Path) -> None:
    seed = site.seed
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    request_urls: list[str] = []
    context.on("request", lambda request: request_urls.append(request.url))
    try:
        async with opened(context, site.base_url + "/operator.html") as (page, visit, _status):
            await page.fill("#token-input", seed.operator_token)
            await page.locator('#token-form [type="submit"]').click()
            row = page.locator("#backups tbody tr", has_text=seed.backup_name)
            await row.wait_for()
            assert await page.input_value("#token-input") == ""
            assert seed.backup_sha256 in await row.inner_text()
            async with page.expect_download() as download_info:
                await row.locator("button[data-download]").click()
            download = await download_info.value
            assert download.suggested_filename == seed.backup_name
            saved = tmp_path / seed.backup_name
            await download.save_as(saved)
            assert hashlib.sha256(saved.read_bytes()).hexdigest() == seed.backup_sha256
            assert saved.stat().st_size == seed.backup_size
            await row.locator(".badge--success[role=status]").wait_for()
            await _settle(page, visit)

            stored = await page.evaluate("async () => [localStorage.length, sessionStorage.length, document.cookie, window.name, (await caches.keys()).length, (await indexedDB.databases()).length]")
            assert stored == [0, 0, "", "", 0, 0]
            assert seed.operator_token not in page.url
            assert all(seed.operator_token not in url for url in request_urls)
            assert seed.operator_token not in await page.content()
            assert visit_problems(visit) == []
            assert "/v1/admin/backups" in visit.api_paths()
            assert f"/v1/admin/backups/{seed.backup_name}" in visit.api_paths()

        async with opened(context, site.base_url + "/operator.html") as (page, visit, _status):
            await page.fill("#token-input", seed.other_token)
            await page.locator('#token-form [type="submit"]').click()
            await page.locator("#backups .state--error", has_text="403").wait_for()
            await _settle(page, visit)
            assert visit_problems(visit, allow_status=[("/v1/admin/backups", 403)]) == []
            assert seed.other_token not in await page.content()
            assert await page.locator("#backups tbody tr").count() == 0
    finally:
        await context.close()


async def test_operator_pager_waits_for_a_running_download(browser: Browser, site: Site) -> None:
    seed = site.seed
    # Older files fill the first page after the seed backup, so the list has a second page of two.
    extra = [site.backup_dir / f"older-{index:02d}.gz" for index in range(16)]
    for index, path in enumerate(extra):
        path.write_bytes(b"older backup %d" % index)
        stamp = 1_750_000_000 + index
        os.utime(path, (stamp, stamp))
    context = await browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    release = asyncio.Event()

    async def hold(route: Route) -> None:
        await release.wait()
        await route.continue_()

    try:
        async with opened(context, site.base_url + "/operator.html") as (page, visit, _status):
            await page.route(re.compile(r"/v1/admin/backups/"), hold)
            await page.fill("#token-input", seed.operator_token)
            await page.locator('#token-form [type="submit"]').click()
            row = page.locator("#backups tbody tr", has_text=seed.backup_name)
            await row.wait_for()
            assert await page.locator("#backups tbody tr").count() == 15
            assert "1 / 2" in await page.locator("#backups-pager .pager-status").inner_text()
            following = page.locator("#backups-pager button[data-page='2']")
            assert await following.is_enabled()

            async with page.expect_download() as download_info:
                await row.locator("button[data-download]").click()
                await page.wait_for_function("() => document.querySelector(\"#backups-pager button[data-page='2']\").disabled")
                await page.evaluate("() => document.querySelector(\"#backups-pager button[data-page='2']\").click()")
                assert "1 / 2" in await page.locator("#backups-pager .pager-status").inner_text()
                assert await row.locator("progress").count() == 1
                release.set()
            assert (await download_info.value).suggested_filename == seed.backup_name
            await row.locator(".badge--success[role=status]").wait_for()
            assert await following.is_enabled()

            await following.click()
            await page.locator("#backups-pager .pager-status", has_text="2 / 2").wait_for()
            assert await page.locator("#backups tbody tr").count() == 2
            assert await page.locator("#backups tbody tr", has_text=seed.backup_name).count() == 0
            await _settle(page, visit)
            assert visit_problems(visit) == []
            assert [path for path in visit.api_paths() if path == "/v1/admin/backups"] == ["/v1/admin/backups"] * 2
    finally:
        release.set()
        await context.close()
        for path in extra:
            path.unlink(missing_ok=True)
