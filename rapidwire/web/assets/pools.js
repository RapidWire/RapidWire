// Pools page: the paged list of every active pool and, for ?x=&y=, the fee tiers of one currency pair side by side.
import {
  accountLink,
  amountView,
  api,
  apiOrNull,
  classifySearch,
  dataTable,
  groupInteger,
  h,
  isId,
  lookupCurrency,
  mount,
  pageParam,
  pager,
  poolLink,
  rememberPool,
  renderEmpty,
  renderError,
  renderLoading,
  renderPageError,
  withParams,
} from "/assets/app.js";
import { feeTierText, pairCurrency, pairView, priceLine, spotPrices } from "/assets/dex.js";

const PER_PAGE = 20;

const POOL_COLUMNS = [
  { label: "プール", className: "nowrap", render: (pool) => poolLink(pool.pool_id) },
  { label: "ペア", render: (pool) => pairView(pool) },
  { label: "手数料", className: "num", render: (pool) => feeTierText(pool.fee_bps) },
  { label: "リザーブ", className: "num", render: (pool) => [h("span", { class: "sub" }, amountView(pool.reserve_a, pool.symbol_a)), h("span", { class: "sub" }, amountView(pool.reserve_b, pool.symbol_b))] },
  { label: "総シェア", className: "num", render: (pool) => h("span", { class: "mono" }, groupInteger(pool.total_shares)) },
  { label: "保管口座", render: (pool) => accountLink(pool.custody_account_id) },
];

async function loadPools() {
  const container = document.getElementById("pools");
  const pagerHost = document.getElementById("pools-pager");
  const pageNumber = pageParam("page", PER_PAGE);
  if (pageNumber === null) {
    renderPageError(container, pagerHost, withParams({ page: null }) + "#pools-title", "プール一覧のページ");
    return;
  }
  renderLoading(container);
  let page;
  try {
    page = await api("/v1/pools", { query: { page: pageNumber, per_page: PER_PAGE } });
  } catch (error) {
    renderError(container, error);
    return;
  }
  document.getElementById("pools-summary").textContent = `${page.total.toLocaleString("en-US")}${page.total_is_lower_bound ? "件以上" : "件"}`;
  page.items.forEach(rememberPool);
  if (page.items.length === 0) {
    renderEmpty(container, page.total === 0 ? "稼働中のプールはまだありません。" : "このページにプールはありません。");
  } else {
    mount(container, dataTable({ caption: "稼働中のプール", columns: POOL_COLUMNS, rows: page.items }));
  }
  mount(pagerHost, pager(page, (target) => withParams({ page: target }) + "#pools-title", { label: "プール一覧のページ" }));
}

// Resolve one pair input to {id} from a positive currency ID or a symbol, or {error} with a message.
async function resolveCurrency(text) {
  const value = text.trim();
  if (isId(value, { positive: true })) {
    return { id: value };
  }
  const query = classifySearch(value);
  if (query.kind !== "symbol") {
    return { error: `"${value}" は通貨IDでも通貨シンボルでもありません。` };
  }
  const currency = await apiOrNull(`/v1/currencies/symbol/${encodeURIComponent(query.value)}`);
  return currency === null ? { error: `通貨シンボル ${query.value} は見つかりません。` } : { id: currency.currency_id };
}

// The fee tiers the server offers, or null when the configuration cannot be read.
async function feeTiers() {
  try {
    const config = await api("/v1/config");
    const tiers = config && config.swap && Array.isArray(config.swap.fee_tiers_bps) ? config.swap.fee_tiers_bps : null;
    return tiers === null ? null : [...tiers].sort((left, right) => left - right);
  } catch {
    return null;
  }
}

function pairColumns(symbolA, symbolB) {
  return [
    { label: "手数料ティア", className: "nowrap", render: (row) => [h("strong", null, feeTierText(row.fee_bps)), h("span", { class: "sub mono" }, `fee_bps ${row.fee_bps}`)] },
    { label: "プール", className: "nowrap", render: (row) => (row.pool ? poolLink(row.pool.pool_id) : h("span", { class: "sub" }, "プールなし")) },
    { label: `リザーブ ${symbolA}`, className: "num", render: (row) => (row.pool ? amountView(row.pool.reserve_a, row.pool.symbol_a) : "-") },
    { label: `リザーブ ${symbolB}`, className: "num", render: (row) => (row.pool ? amountView(row.pool.reserve_b, row.pool.symbol_b) : "-") },
    { label: "総シェア", className: "num", render: (row) => (row.pool ? h("span", { class: "mono" }, groupInteger(row.pool.total_shares)) : "-") },
    {
      label: "スポット価格",
      render: (row) => {
        if (!row.pool) {
          return "-";
        }
        const prices = spotPrices(row.pool);
        return [h("span", { class: "sub" }, priceLine(row.pool.symbol_a, row.pool.symbol_b, prices.aInB)), h("span", { class: "sub" }, priceLine(row.pool.symbol_b, row.pool.symbol_a, prices.bInA))];
      },
    },
  ];
}

async function loadPair(rawX, rawY) {
  const container = document.getElementById("pair-result");
  renderLoading(container);
  try {
    const [x, y] = await Promise.all([resolveCurrency(rawX), resolveCurrency(rawY)]);
    const invalid = [x, y].find((side) => side.error);
    if (invalid) {
      renderError(container, invalid.error);
      return;
    }
    if (x.id === y.id) {
      renderError(container, "異なる2つの通貨を指定してください。");
      return;
    }
    const [pair, tiers] = await Promise.all([api(`/v1/pools/pair/${x.id}/${y.id}`), feeTiers()]);
    const [currencyA, currencyB] = await Promise.all([lookupCurrency(pair.currency_a), lookupCurrency(pair.currency_b)]);
    const missing = [[pair.currency_a, currencyA], [pair.currency_b, currencyB]].find(([, currency]) => currency === null);
    if (missing) {
      renderError(container, `通貨 ${missing[0]} は見つかりません。`);
      return;
    }
    const byTier = new Map(pair.pools.map((pool) => [pool.fee_bps, pool]));
    const tierList = [...new Set([...(tiers || []), ...byTier.keys()])].sort((left, right) => left - right);
    const rows = tierList.map((feeBps) => ({ fee_bps: feeBps, pool: byTier.get(feeBps) || null }));
    const heading = h("p", { class: "panel-note" }, "ペア: ", pairCurrency(currencyA.currency_id, currencyA.symbol, Boolean(currencyA.deleted_at)), " / ", pairCurrency(currencyB.currency_id, currencyB.symbol, Boolean(currencyB.deleted_at)), ` - 稼働中のプール ${pair.pools.length} 件`);
    mount(container, heading, dataTable({ caption: `${currencyA.symbol} / ${currencyB.symbol} の手数料ティア`, columns: pairColumns(currencyA.symbol, currencyB.symbol), rows }));
  } catch (error) {
    renderError(container, error);
  }
}

function setUpPairForm() {
  const params = new URLSearchParams(window.location.search);
  const rawX = (params.get("x") || "").trim();
  const rawY = (params.get("y") || "").trim();
  document.getElementById("pair-x").value = rawX;
  document.getElementById("pair-y").value = rawY;
  const form = document.getElementById("pair-form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    window.location.assign(withParams({ x: String(data.get("x") || "").trim(), y: String(data.get("y") || "").trim(), page: null }));
  });
  if (rawX === "" && rawY === "") {
    return;
  }
  if (rawX === "" || rawY === "") {
    renderError(document.getElementById("pair-result"), "通貨を2つとも指定してください。");
    return;
  }
  loadPair(rawX, rawY);
}

setUpPairForm();
loadPools();
