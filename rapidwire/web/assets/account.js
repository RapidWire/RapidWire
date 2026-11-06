// Account page: the account's name or, for a system account, what it belongs to, its contract badge and transfer statistics, its balances, stakes, liquidity positions and claims in tabs, its paged transfer history with in and out colouring, and the paged executions it started.
import {
  ACCOUNT_KIND_LABELS,
  accountFlow,
  accountLabel,
  accountLink,
  amountView,
  api,
  apiOrNull,
  contractStatusBadge,
  countText,
  currencyLink,
  executionColumns,
  executionRef,
  formatTime,
  getParam,
  groupInteger,
  h,
  historyColumns,
  isId,
  kvList,
  loadCurrencyIndex,
  loadPagedTable,
  lookupAccount,
  lookupCurrency,
  lookupPool,
  lookupUsername,
  mount,
  pageUrl,
  poolLink,
  ppmPercent,
  readHistorySort,
  renderError,
  renderLoading,
  setUpHistoryForm,
  systemAccountLabel,
  withParams,
} from "/assets/app.js";
import { feeTierText, pairView } from "/assets/dex.js";

const HOLDINGS_PER_PAGE = 15;
const HISTORY_PER_PAGE = 20;
const EXECUTIONS_PER_PAGE = 20;
const TABS = ["balances", "stakes", "pools", "claims"];
const CLAIM_STATUSES = ["pending", "paid", "canceled"];
const CLAIM_ROLES = ["claimant", "payer"];

function claimStatusBadge(status) {
  switch (status) {
    case "pending":
      return h("span", { class: "badge badge--warn" }, "未払い / pending");
    case "paid":
      return h("span", { class: "badge badge--success" }, "支払い済み / paid");
    case "canceled":
      return h("span", { class: "badge badge--muted" }, "取り消し / canceled");
    default:
      return h("span", { class: "badge badge--muted" }, String(status));
  }
}

function setCount(name, page) {
  document.getElementById(`count-${name}`).textContent = `(${countText(page)})`;
}

function setUpTabs(initial) {
  const tabs = TABS.map((name) => document.getElementById(`tab-${name}`));
  const select = (name, { focus = false, record = true } = {}) => {
    for (const tab of tabs) {
      const selected = tab.id === `tab-${name}`;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) {
        tab.focus();
      }
    }
    if (record) {
      window.history.replaceState(null, "", withParams({ tab: name === TABS[0] ? null : name }));
    }
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(TABS[index]));
    tab.addEventListener("keydown", (event) => {
      const moves = { ArrowRight: (index + 1) % TABS.length, ArrowLeft: (index - 1 + TABS.length) % TABS.length, Home: 0, End: TABS.length - 1 };
      if (Object.hasOwn(moves, event.key)) {
        event.preventDefault();
        select(TABS[moves[event.key]], { focus: true });
      }
    });
  });
  select(initial, { record: false });
}

// The head of a positive ID: its Discord username once it arrives, and a note when the ledger has no account row for it yet, which is not an error because a Discord user ID is valid before its first ledger operation.
function renderUserHead(id) {
  const title = document.getElementById("account-title");
  mount("account-kind", "アカウント ", h("span", { class: "id" }, id));
  document.title = `${id} - Account - RapidWire`;
  mount(title, h("span", { class: "id" }, id));
  lookupUsername(id).then((name) => {
    if (name !== null) {
      mount(title, name);
      document.title = `${name} (${id}) - Account - RapidWire`;
    }
  });
  lookupAccount(id).then(
    (account) => {
      if (account === null) {
        mount(
          "account-note",
          h(
            "div",
            { class: "banner", role: "note", id: "account-unrecorded" },
            h("p", { class: "banner-title" }, "台帳にまだ記録がないアカウントです"),
            h("p", null, "このIDのアカウントは台帳にまだ記録されていません。アカウントは最初の送金や受け取りなどの取引で記録されるため、それまでは残高と移動はありません。"),
          ),
        );
      }
    },
    () => null,
  );
}

function systemLede(account) {
  switch (account.kind) {
    case "issuance":
      return "通貨の発行アカウントです。発行と焼却はこのアカウントとの移動として記録され、残高は供給量の符号を反転した値 (負の値) になります。";
    case "pool_custody":
      return "流動性プールのリザーブを保管するアカウントです。プールの準備金はこのアカウントの残高として保管されます。";
    case "stake_custody":
      return "ステークされた通貨を預かるシステムアカウントです。すべてのステークの元本と利息はこのアカウントの残高になります。";
    default:
      return "システムが管理するアカウントです。";
  }
}

async function issuanceRows(account) {
  const currency = await lookupCurrency(account.currency_id).catch(() => null);
  if (currency === null) {
    return [["通貨", currencyLink(account.currency_id)]];
  }
  return [
    ["通貨", [currencyLink(currency.currency_id, currency.symbol), h("span", { class: "sub" }, currency.name), currency.deleted_at ? h("span", { class: "sub" }, h("span", { class: "badge badge--failed" }, "削除済み / deleted")) : null]],
    ["供給量", [amountView(currency.supply, currency.symbol), h("span", { class: "sub" }, "このアカウントの残高は供給量の符号を反転した値です。")]],
  ];
}

async function poolRows(account) {
  const pool = await lookupPool(account.pool_id).catch(() => null);
  if (pool === null) {
    return [["プール", poolLink(account.pool_id)]];
  }
  return [
    ["プール", [poolLink(pool.pool_id), pool.active ? null : h("span", { class: "sub" }, h("span", { class: "badge badge--muted" }, "解消済み / dissolved"))]],
    ["ペア", [pairView(pool), h("span", { class: "sub" }, `手数料ティア ${feeTierText(pool.fee_bps)}`)]],
  ];
}

// The head and the system account panel of a negative ID: the label and lede of its kind, and what it belongs to; a negative ID without an account row says so.
async function renderSystemHead(id) {
  const title = document.getElementById("account-title");
  const panel = document.getElementById("system-panel");
  const details = document.getElementById("system-details");
  mount("account-kind", "システムアカウント ", h("span", { class: "id" }, id));
  mount(title, accountLabel(id));
  document.title = `${accountLabel(id)} - Account - RapidWire`;
  panel.hidden = false;
  renderLoading(details);
  let account;
  try {
    account = await lookupAccount(id);
  } catch (error) {
    renderError(details, error);
    return;
  }
  if (account === null) {
    mount("account-lede", "このIDのシステムアカウントは存在しません。");
    renderError(details, "このIDのシステムアカウントは存在しません。システムアカウントは通貨やプールの作成時に作られます。");
    return;
  }
  mount("account-lede", `${systemLede(account)}残高と取引はすべて公開されています。`);
  mount("account-badges", h("span", { class: "badge badge--flag" }, ACCOUNT_KIND_LABELS[account.kind] || String(account.kind)));
  systemAccountLabel(id).then((label) => {
    if (label !== null) {
      mount(title, label);
      document.title = `${label} (${id}) - Account - RapidWire`;
    }
  });
  const rows = account.kind === "issuance" ? await issuanceRows(account) : account.kind === "pool_custody" ? await poolRows(account) : [];
  mount(
    details,
    kvList([
      ["種類", [h("span", { class: "badge badge--flag" }, ACCOUNT_KIND_LABELS[account.kind] || String(account.kind)), h("span", { class: "sub mono" }, account.kind)]],
      ...rows,
      ["アカウントID", h("span", { class: "id" }, account.account_id)],
      ["作成日時", formatTime(account.created_at)],
    ]),
  );
}

async function loadContractBadge(id) {
  const badges = document.getElementById("account-badges");
  try {
    const contract = await apiOrNull(`/v1/contracts/${id}`);
    if (contract === null) {
      return;
    }
    mount(badges, h("a", { class: "badge badge--flag", href: pageUrl("contract", id) }, "コントラクト"), contractStatusBadge(contract.status));
  } catch {
    mount(badges, h("span", { class: "panel-note" }, "コントラクトの情報を取得できませんでした。"));
  }
}

async function loadOverview(id) {
  const container = document.getElementById("overview");
  renderLoading(container);
  let stats;
  try {
    stats = await api(`/v1/users/${id}/stats`);
  } catch (error) {
    renderError(container, error);
    return;
  }
  mount(
    container,
    h(
      "dl",
      { class: "stats" },
      h("div", null, h("dt", null, "移動の件数"), h("dd", null, stats.total_transfers.toLocaleString("en-US"))),
      h("div", null, h("dt", null, "最初の移動"), h("dd", null, formatTime(stats.first_transfer_at))),
      h("div", null, h("dt", null, "最後の移動"), h("dd", null, formatTime(stats.last_transfer_at))),
    ),
  );
}

function currencyCell(row) {
  return [currencyLink(row.currency_id, row.symbol), row.name ? h("span", { class: "sub" }, row.name) : null];
}

const BALANCE_COLUMNS = [
  { label: "通貨", render: currencyCell },
  {
    label: "残高",
    className: "num",
    render: (row) => {
      const negative = row.amount.startsWith("-");
      return [amountView(row.amount, row.symbol, { sign: "auto" }), negative ? h("span", { class: "sub" }, "発行残高 (供給量 ", amountView(row.amount.slice(1)), ")") : null];
    },
  },
];

const STAKE_COLUMNS = [
  { label: "通貨", render: currencyCell },
  { label: "元本", className: "num", render: (row) => amountView(row.amount, row.symbol) },
  { label: "見込み額", className: "num", render: (row) => [amountView(row.projected_amount, row.symbol), h("span", { class: "sub" }, formatTime(row.projected_at), " 時点")] },
  { label: "時間利率", className: "num", render: (row) => [`${ppmPercent(row.hourly_rate_ppm)}% / 時`, h("span", { class: "sub" }, `${groupInteger(row.hourly_rate_ppm)} ppm`)] },
  { label: "最後の付利", className: "nowrap", render: (row) => formatTime(row.last_accrued_at) },
  { label: "利息", render: (row) => (row.interest_halted ? h("span", { class: "badge badge--warn" }, "停止 / halted") : h("span", { class: "badge badge--muted" }, "付利中")) },
];

const POSITION_COLUMNS = [
  { label: "プール", className: "nowrap", render: (row) => [poolLink(row.pool.pool_id), row.pool.active ? null : h("span", { class: "sub" }, h("span", { class: "badge badge--muted" }, "解消済み / dissolved"))] },
  { label: "ペア", render: (row) => h("span", { class: "flow" }, currencyLink(row.pool.currency_a, row.pool.symbol_a), h("span", { "aria-hidden": "true" }, "/"), currencyLink(row.pool.currency_b, row.pool.symbol_b)) },
  { label: "手数料", className: "num", render: (row) => feeTierText(row.pool.fee_bps) },
  { label: "シェア", className: "num", render: (row) => [h("span", { class: "mono" }, groupInteger(row.shares)), h("span", { class: "sub" }, `${row.share_percent}%`)] },
  { label: "引き出し可能額", className: "num", render: (row) => [amountView(row.withdrawable_a, row.pool.symbol_a), h("span", { class: "sub" }, amountView(row.withdrawable_b, row.pool.symbol_b))] },
];

function claimColumns(id, currencies) {
  const symbol = (currencyId) => (currencies.has(currencyId) ? currencies.get(currencyId).symbol : null);
  return [
    { label: "請求", className: "nowrap", render: (row) => [h("span", { class: "id" }, `#${row.claim_id}`), h("span", { class: "sub" }, row.claimant_id === id ? "請求した側" : "支払う側")] },
    { label: "請求者 -> 支払者", render: (row) => accountFlow(row.claimant_id, row.payer_id, { withName: true }) },
    { label: "金額", className: "num", render: (row) => [amountView(row.amount), " ", currencyLink(row.currency_id, symbol(row.currency_id))] },
    { label: "内容", render: (row) => (row.description ? row.description : "-") },
    { label: "状態", render: (row) => claimStatusBadge(row.status) },
    { label: "日時", className: "nowrap", render: (row) => [formatTime(row.created_at), h("span", { class: "sub" }, "更新 ", formatTime(row.updated_at))] },
    {
      label: "実行",
      render: (row) => [
        h("span", { class: "sub" }, "作成 ", executionRef(row.created_execution_id, row.created_execution_origin, row.created_execution_label_basis)),
        row.resolved_execution_id ? h("span", { class: "sub" }, "解決 ", executionRef(row.resolved_execution_id, row.resolved_execution_origin, row.resolved_execution_label_basis)) : null,
      ],
    },
  ];
}

// The direction of a transfer seen from the account: "out", "in" or "self".
function direction(row, id) {
  if (row.from_account === id && row.to_account === id) {
    return "self";
  }
  return row.from_account === id ? "out" : "in";
}

function historyPartyColumns(id) {
  return [
    {
      label: "相手",
      render: (row) => {
        const way = direction(row, id);
        if (way === "self") {
          return [h("span", { class: "sub" }, "自分自身への移動"), accountLink(id)];
        }
        return [h("span", { class: "sub" }, way === "out" ? "送金先" : "送金元"), accountLink(way === "out" ? row.to_account : row.from_account, { withName: true })];
      },
    },
    {
      label: "金額",
      className: "num",
      render: (row) => {
        const way = direction(row, id);
        return [amountView(row.amount, null, { sign: way === "self" ? "none" : way }), " ", currencyLink(row.currency_id, row.currency_symbol)];
      },
    },
  ];
}

// The currency filter of the URL: {currencyId}, plus error when it is not a positive ID.
function readCurrencyFilter() {
  const currencyId = new URLSearchParams(window.location.search).get("currency_id") || "";
  return currencyId !== "" && !isId(currencyId, { positive: true }) ? { currencyId, error: "通貨IDの指定が正しくありません。" } : { currencyId };
}

function setUpCurrencySelect(currencyId, currencies) {
  const select = document.getElementById("history-currency");
  const items = [...currencies.values()].sort((a, b) => (a.symbol < b.symbol ? -1 : a.symbol > b.symbol ? 1 : 0));
  for (const item of items) {
    select.appendChild(h("option", { value: item.currency_id, selected: item.currency_id === currencyId }, item.deleted ? `${item.symbol} (削除済み)` : item.symbol));
  }
  if (currencyId !== "" && !currencies.has(currencyId)) {
    select.appendChild(h("option", { value: currencyId, selected: true }, `#${currencyId}`));
  }
}

function setUpClaimsFilter(status, role) {
  document.getElementById("claims-status").value = CLAIM_STATUSES.includes(status) ? status : "";
  document.getElementById("claims-role").value = CLAIM_ROLES.includes(role) ? role : "";
  const form = document.getElementById("claims-filter");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    window.location.assign(withParams({ cs: data.get("cs") || null, cr: data.get("cr") || null, cp: null, tab: "claims" }) + "#holdings-title");
  });
}

function claimsEmptyText(status, role) {
  if (status === "" && role === "") {
    return "請求はありません。";
  }
  return "条件に合う請求はありません。";
}

async function loadHoldingsAndHistory(id) {
  const currencies = await loadCurrencyIndex();
  const holdings = { perPage: HOLDINGS_PER_PAGE, anchor: "#holdings-title" };
  loadPagedTable({ ...holdings, host: "balances", pagerHost: "balances-pager", path: `/v1/accounts/${id}/balances`, param: "bp", caption: "残高", columns: BALANCE_COLUMNS, emptyText: "残高はありません。", extra: { tab: null }, onPage: (page) => setCount("balances", page) });
  loadPagedTable({ ...holdings, host: "stakes", pagerHost: "stakes-pager", path: `/v1/accounts/${id}/stakes`, param: "sp", caption: "ステーク", columns: STAKE_COLUMNS, emptyText: "ステークはありません。", extra: { tab: "stakes" }, onPage: (page) => setCount("stakes", page) });
  loadPagedTable({ ...holdings, host: "positions", pagerHost: "positions-pager", path: `/v1/accounts/${id}/pools`, param: "lp", caption: "流動性ポジション", columns: POSITION_COLUMNS, emptyText: "流動性ポジションはありません。", extra: { tab: "pools" }, onPage: (page) => setCount("pools", page) });

  const params = new URLSearchParams(window.location.search);
  const claimStatus = params.get("cs") || "";
  const claimRole = params.get("cr") || "";
  setUpClaimsFilter(claimStatus, claimRole);
  if (claimStatus !== "" && !CLAIM_STATUSES.includes(claimStatus)) {
    renderError("claims", "請求の状態の指定が正しくありません。");
  } else if (claimRole !== "" && !CLAIM_ROLES.includes(claimRole)) {
    renderError("claims", "請求の立場の指定が正しくありません。");
  } else {
    loadPagedTable({
      ...holdings,
      host: "claims",
      pagerHost: "claims-pager",
      path: `/v1/accounts/${id}/claims`,
      query: { status: claimStatus, role: claimRole },
      param: "cp",
      caption: "請求",
      columns: claimColumns(id, currencies),
      emptyText: claimsEmptyText(claimStatus, claimRole),
      extra: { tab: "claims" },
      onPage: (page) => setCount("claims", page),
    });
  }

  const filter = readCurrencyFilter();
  const sort = readHistorySort();
  setUpCurrencySelect(filter.currencyId, currencies);
  setUpHistoryForm(document.getElementById("history-filter"), sort, (data) => ({ currency_id: data.get("currency_id") }));
  if (filter.error || sort.error) {
    renderError("history", filter.error || sort.error);
    return;
  }
  loadPagedTable({
    host: "history",
    pagerHost: "history-pager",
    path: `/v1/accounts/${id}/history`,
    anchor: "#history-title",
    query: { currency_id: filter.currencyId, sort_by: sort.sortBy, sort_order: sort.sortOrder },
    param: "hp",
    perPage: HISTORY_PER_PAGE,
    caption: "移動履歴",
    columns: historyColumns(historyPartyColumns(id)),
    emptyText: filter.currencyId === "" ? "このアカウントの移動はまだありません。" : "この通貨の移動はありません。",
    onPage: (page) => {
      document.getElementById("history-summary").textContent = `${countText(page)} 件 / 出金は赤の「-」、入金は緑の「+」`;
    },
  });
}

function loadExecutions(id) {
  mount("executions-more", h("a", { href: `/explorer.html?initiator_id=${encodeURIComponent(id)}` }, "Explorerで絞り込む"));
  loadPagedTable({
    host: "executions",
    pagerHost: "executions-pager",
    path: "/v1/executions",
    anchor: "#executions-title",
    query: { initiator_id: id },
    param: "ep",
    perPage: EXECUTIONS_PER_PAGE,
    caption: "このアカウントが開始した実行",
    columns: executionColumns({ initiator: false }),
    emptyText: "このアカウントが開始した実行はまだありません。",
    onPage: (page) => {
      document.getElementById("executions-summary").textContent = `${countText(page)} 件`;
    },
  });
}

function start() {
  const raw = new URLSearchParams(window.location.search).get("id");
  const id = getParam("id", "id");
  if (id === null) {
    document.title = "Account - RapidWire";
    renderError("account-error", raw === null || raw === "" ? "アカウントIDが指定されていません。URLに ?id=<アカウントID> を付けてください。" : "アカウントIDが正しくありません。IDは数字で指定してください。");
    return;
  }
  const tab = new URLSearchParams(window.location.search).get("tab");
  setUpTabs(TABS.includes(tab) ? tab : TABS[0]);
  document.getElementById("account-body").hidden = false;
  if (id.startsWith("-")) {
    renderSystemHead(id);
  } else {
    renderUserHead(id);
    loadContractBadge(id);
  }
  loadOverview(id);
  loadHoldingsAndHistory(id);
  loadExecutions(id);
}

start();
