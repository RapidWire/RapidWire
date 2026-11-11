// Pool page for ?id=: every field of the pool, its spot prices, the other fee tiers of its pair and the latest transfers of its custody account.
import {
  accountLink,
  amountView,
  api,
  dataTable,
  executionLink,
  formatTime,
  getParam,
  groupInteger,
  h,
  kvList,
  mount,
  pageUrl,
  poolLink,
  rememberPool,
  renderEmpty,
  renderError,
  renderLoading,
  transferFeedItem,
} from "/assets/app.js";
import { PRICE_DECIMALS, feeTierText, pairCurrency, priceLine, providerShares, spotPrices } from "/assets/dex.js";

const ACTIVITY_SIZE = 10;

function statusBadge(pool) {
  return pool.active ? h("span", { class: "badge badge--success" }, "稼働中 / active") : h("span", { class: "badge badge--failed" }, "解消済み / dissolved");
}

function renderBanner(pool) {
  const host = document.getElementById("pool-banner");
  if (pool.active) {
    mount(host);
    return;
  }
  mount(
    host,
    h(
      "div",
      { class: "state state--error panel", role: "status" },
      h("p", { class: "state-title" }, "このプールは解消されました / This pool was dissolved"),
      h("p", null, "通貨の削除によりプールは解消され、リザーブとシェアは0になっています。同じペアとティアのプールは新しいIDで作り直せます。 ", "解消: ", formatTime(pool.dissolved_at), " (実行 ", executionLink(pool.dissolved_execution_id), ")"),
    ),
  );
}

function shareText(value) {
  return value === null || value === undefined ? "-" : h("span", { class: "mono" }, groupInteger(value));
}

function renderOverview(pool) {
  const provider = providerShares(pool);
  mount(
    "pool-overview",
    kvList([
      ["プールID", h("span", { class: "id" }, pool.pool_id)],
      ["状態", statusBadge(pool)],
      ["通貨 A", pairCurrency(pool.currency_a, pool.symbol_a, pool.deleted_a)],
      ["通貨 B", pairCurrency(pool.currency_b, pool.symbol_b, pool.deleted_b)],
      ["手数料ティア", [feeTierText(pool.fee_bps), h("span", { class: "sub mono" }, `fee_bps ${pool.fee_bps}`)]],
      [`リザーブ ${pool.symbol_a}`, amountView(pool.reserve_a, pool.symbol_a)],
      [`リザーブ ${pool.symbol_b}`, amountView(pool.reserve_b, pool.symbol_b)],
      ["総シェア", shareText(pool.total_shares)],
      ["ロックされたシェア", [shareText(pool.locked_shares), h("span", { class: "sub" }, "作成時に永久にロックされる最小流動性")]],
      ["提供者のシェア", [shareText(provider), h("span", { class: "sub" }, "総シェア - ロックされたシェア")]],
      ["保管口座", accountLink(pool.custody_account_id)],
      ["作成者", accountLink(pool.created_by, { withName: true })],
      ["作成日時", formatTime(pool.created_at)],
      ["作成の実行", executionLink(pool.created_execution_id)],
      ["解消日時", formatTime(pool.dissolved_at)],
      ["解消の実行", executionLink(pool.dissolved_execution_id)],
    ]),
  );
}

function renderPrice(pool) {
  const prices = spotPrices(pool);
  mount(
    "pool-price",
    kvList([
      [`${pool.symbol_a} の価格`, priceLine(pool.symbol_a, pool.symbol_b, prices.aInB)],
      [`${pool.symbol_b} の価格`, priceLine(pool.symbol_b, pool.symbol_a, prices.bInA)],
    ]),
    h("p", { class: "panel-note" }, `小数点以下${PRICE_DECIMALS}桁に四捨五入しています。実際のスワップでは手数料と価格への影響が加わります。`),
  );
}

const TIER_COLUMNS = [
  { label: "手数料ティア", className: "nowrap", render: (pool) => feeTierText(pool.fee_bps) },
  { label: "プール", className: "nowrap", render: (pool) => poolLink(pool.pool_id) },
  { label: "リザーブ", className: "num", render: (pool) => [h("span", { class: "sub" }, amountView(pool.reserve_a, pool.symbol_a)), h("span", { class: "sub" }, amountView(pool.reserve_b, pool.symbol_b))] },
  {
    label: "スポット価格",
    render: (pool) => {
      const prices = spotPrices(pool);
      return [h("span", { class: "sub" }, priceLine(pool.symbol_a, pool.symbol_b, prices.aInB)), h("span", { class: "sub" }, priceLine(pool.symbol_b, pool.symbol_a, prices.bInA))];
    },
  },
];

async function loadTiers(pool) {
  const container = document.getElementById("pool-tiers");
  renderLoading(container);
  try {
    const pair = await api(`/v1/pools/pair/${pool.currency_a}/${pool.currency_b}`);
    const others = pair.pools.filter((other) => other.pool_id !== pool.pool_id);
    if (others.length === 0) {
      renderEmpty(container, "このペアに他の稼働中のティアはありません。");
      return;
    }
    mount(container, dataTable({ caption: "同じペアの他のティア", columns: TIER_COLUMNS, rows: others }));
  } catch (error) {
    renderError(container, error);
  }
}

async function loadActivity(pool) {
  const container = document.getElementById("pool-activity");
  renderLoading(container);
  try {
    const page = await api("/v1/history", { query: { user_id: pool.custody_account_id, sort_by: "transfer_id", sort_order: "desc", per_page: ACTIVITY_SIZE } });
    const more = h("p", { class: "panel-note" }, h("a", { href: pageUrl("account", pool.custody_account_id) }, "保管口座のすべての移動を見る"));
    if (page.items.length === 0) {
      mount(container, h("div", { class: "state state--empty" }, "まだ移動はありません。"), more);
      return;
    }
    mount(container, h("ul", { class: "feed", "aria-label": "プールの最近の移動" }, page.items.map(transferFeedItem)), more);
  } catch (error) {
    renderError(container, error);
  }
}

async function main() {
  const overview = document.getElementById("pool-overview");
  const id = getParam("id", "positiveId");
  if (id === null) {
    renderError(overview, "プールIDを ?id= に正の整数で指定してください。");
    for (const name of ["pool-price", "pool-tiers", "pool-activity"]) {
      mount(name);
    }
    return;
  }
  document.getElementById("pool-id").textContent = `Pool #${id}`;
  renderLoading(overview);
  let pool;
  try {
    pool = await api(`/v1/pools/${id}`);
  } catch (error) {
    renderError(overview, error);
    for (const name of ["pool-price", "pool-tiers", "pool-activity"]) {
      mount(name);
    }
    return;
  }
  rememberPool(pool);
  const title = `${pool.symbol_a} / ${pool.symbol_b} ${feeTierText(pool.fee_bps)}`;
  document.getElementById("pool-title").textContent = title;
  document.title = `${title} (Pool #${pool.pool_id}) - RapidWire`;
  renderBanner(pool);
  renderOverview(pool);
  renderPrice(pool);
  loadTiers(pool);
  loadActivity(pool);
}

main();
