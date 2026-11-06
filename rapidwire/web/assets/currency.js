// Currency page: every field of one currency, found by ?id= or ?symbol=, with its paged and sortable transfer history.
import {
  accountFlow,
  accountLink,
  amountView,
  api,
  classifySearch,
  countText,
  formatTime,
  getParam,
  groupInteger,
  h,
  historyColumns,
  kvList,
  loadPagedTable,
  mount,
  ppmPercent,
  readHistorySort,
  rememberCurrency,
  renderError,
  setUpHistoryForm,
  withParams,
} from "/assets/app.js";

const HISTORY_PER_PAGE = 20;

function rateView(ppm) {
  return [`${ppmPercent(ppm)}% / 時`, h("span", { class: "sub" }, `${groupInteger(ppm)} ppm (100万分率)`)];
}

function flag(on, onText, offText, tone = "badge--flag") {
  return on ? h("span", { class: `badge ${tone}` }, onText) : h("span", { class: "badge badge--muted" }, offText);
}

// Find the currency of the URL by ?id= or, without one, by ?symbol=; resolves to the currency or rejects with an ApiError or a validation message.
async function findCurrency() {
  const params = new URLSearchParams(window.location.search);
  const rawId = params.get("id");
  if (rawId !== null && rawId !== "") {
    const id = getParam("id", "positiveId");
    if (id === null) {
      throw "通貨IDが正しくありません。IDは正の数字で指定してください。";
    }
    return api(`/v1/currencies/${id}`);
  }
  const rawSymbol = params.get("symbol");
  if (rawSymbol === null || rawSymbol === "") {
    throw "通貨が指定されていません。URLに ?id=<通貨ID> または ?symbol=<シンボル> を付けてください。";
  }
  const query = classifySearch(rawSymbol);
  if (query.kind !== "symbol") {
    throw "通貨シンボルが正しくありません。シンボルは8文字以内の英字とハイフンです。";
  }
  const currency = await api(`/v1/currencies/symbol/${encodeURIComponent(query.value)}`);
  window.history.replaceState(null, "", withParams({ symbol: null, id: currency.currency_id }));
  return currency;
}

function renderHead(currency) {
  document.title = `${currency.symbol} - Currency - RapidWire`;
  mount("currency-id", "通貨ID ", h("span", { class: "id" }, currency.currency_id));
  mount("currency-title", currency.name, " ", h("span", { class: "unit" }, currency.symbol));
  mount(
    "currency-badges",
    currency.deleted_at ? h("span", { class: "badge badge--failed" }, "削除済み / deleted") : null,
    currency.minting_renounced ? h("span", { class: "badge badge--warn" }, "発行権放棄済み / renounced") : null,
    currency.delegation_enabled ? h("span", { class: "badge badge--flag" }, "委任有効") : null,
    currency.interest_halted ? h("span", { class: "badge badge--warn" }, "利息停止中") : null,
    currency.delete_requested_at && !currency.deleted_at ? h("span", { class: "badge badge--warn" }, "削除申請中") : null,
  );
  if (currency.deleted_at) {
    mount("currency-banner", h("div", { class: "banner banner--danger", role: "note" }, h("p", { class: "banner-title" }, "この通貨は削除されています"), h("p", null, formatTime(currency.deleted_at), " に削除されました。削除前の移動履歴は引き続き閲覧できます。")));
  }
}

function renderDetails(currency) {
  const pending = currency.pending_rate_ppm !== null && currency.pending_rate_ppm !== undefined;
  const deleteRequested = Boolean(currency.delete_requested_at);
  mount(
    "details",
    kvList([
      ["名前", currency.name],
      ["シンボル", h("span", { class: "mono" }, currency.symbol)],
      ["通貨ID", [h("span", { class: "id" }, currency.currency_id), h("span", { class: "sub" }, "通貨を作成したDiscordサーバーのIDです。")]],
      ["発行アカウント", [accountLink(currency.issuance_account_id), h("span", { class: "sub id" }, currency.issuance_account_id), h("span", { class: "sub" }, "発行と焼却はこのシステムアカウントとの移動として記録されます。")]],
      ["供給量", [amountView(currency.supply, currency.symbol), h("span", { class: "sub" }, "発行アカウントの残高の符号を反転した値です。")]],
      ["作成者", accountLink(currency.created_by, { withName: true })],
      ["作成日時", formatTime(currency.created_at)],
      ["ステーク時間利率", rateView(currency.hourly_rate_ppm)],
      [
        "金利変更の予定",
        pending ? [rateView(currency.pending_rate_ppm), h("span", { class: "sub" }, "申請 ", formatTime(currency.rate_change_requested_at)), h("span", { class: "sub" }, "適用可能 ", formatTime(currency.rate_change_apply_after), " 以降")] : "なし",
      ],
      ["発行権", currency.minting_renounced ? [flag(true, "放棄済み / renounced", "", "badge--warn"), h("span", { class: "sub" }, "これ以上発行できません。")] : flag(false, "", "保持 / not renounced")],
      ["委任", flag(currency.delegation_enabled, "有効 / enabled", "無効 / disabled")],
      ["利息", currency.interest_halted ? [flag(true, "停止 / halted", "", "badge--warn"), h("span", { class: "sub" }, "ステークに利息は付きません。")] : flag(false, "", "付利中 / accruing")],
      [
        "削除申請",
        deleteRequested ? [h("span", null, "申請 ", formatTime(currency.delete_requested_at)), h("span", { class: "sub" }, "確定できる期間 ", formatTime(currency.delete_finalize_from), " から ", formatTime(currency.delete_finalize_until), " まで")] : "なし",
      ],
      ["削除日時", currency.deleted_at ? formatTime(currency.deleted_at) : "-"],
    ]),
  );
}

const HISTORY_COLUMNS = historyColumns([
  { label: "送金元 -> 送金先", render: (row) => accountFlow(row.from_account, row.to_account, { withName: true }) },
  { label: "金額", className: "num", render: (row) => amountView(row.amount, row.currency_symbol) },
]);

function loadHistory(currency) {
  const sort = readHistorySort();
  setUpHistoryForm(document.getElementById("history-filter"), sort);
  if (sort.error) {
    renderError("history", sort.error);
    return;
  }
  loadPagedTable({
    host: "history",
    pagerHost: "history-pager",
    path: "/v1/history",
    query: { currency_id: currency.currency_id, sort_by: sort.sortBy, sort_order: sort.sortOrder },
    param: "hp",
    perPage: HISTORY_PER_PAGE,
    caption: `${currency.symbol} の移動履歴`,
    columns: HISTORY_COLUMNS,
    emptyText: "この通貨の移動はまだありません。",
    anchor: "#history-title",
    onPage: (page) => {
      document.getElementById("history-summary").textContent = `${countText(page)} 件`;
    },
  });
}

async function start() {
  let currency;
  try {
    currency = await findCurrency();
  } catch (error) {
    renderError("currency-error", error);
    return;
  }
  rememberCurrency(currency);
  renderHead(currency);
  renderDetails(currency);
  document.getElementById("currency-body").hidden = false;
  loadHistory(currency);
}

start();
