// Transfer page: every field of one transfer entry, with the execution that made it.
import { accountLink, amountView, api, currencyLink, executionRef, executionTypeLabel, formatTime, getParam, h, kindBadge, kvList, mount, originDetail, renderError, renderLoading, statusBadge } from "/assets/app.js";

const KIND_NOTES = Object.freeze({
  principal: "実行が指定した額そのものの移動です。",
  interest: "ステークに付いた利息の移動です。",
  gas: "コントラクト呼び出しのガス代として焼却された額です。",
});

function render(entry) {
  document.title = `Transfer #${entry.transfer_id} - RapidWire`;
  mount("transfer-title", "移動 ", h("span", { class: "id" }, `#${entry.transfer_id}`));
  mount(
    "transfer-details",
    kvList([
      ["移動ID", h("span", { class: "id" }, entry.transfer_id)],
      ["日時", formatTime(entry.created_at)],
      ["送金元", accountLink(entry.from_account, { withName: true })],
      ["送金先", accountLink(entry.to_account, { withName: true })],
      ["金額", [amountView(entry.amount, null), " ", currencyLink(entry.currency_id, entry.currency_symbol)]],
      ["通貨", [currencyLink(entry.currency_id, entry.currency_symbol), h("span", { class: "sub id" }, entry.currency_id)]],
      ["区分", [kindBadge(entry.kind), KIND_NOTES[entry.kind] ? h("span", { class: "sub" }, KIND_NOTES[entry.kind]) : null]],
    ]),
  );
  mount(
    "execution-details",
    kvList([
      ["実行", executionRef(entry.execution_id, entry.origin, entry.label_basis)],
      ["種類", [executionTypeLabel(entry.execution_type), h("span", { class: "sub mono" }, entry.execution_type)]],
      ["状態", statusBadge(entry.execution_status)],
      ["由来", originDetail(entry.origin, entry.label_basis)],
      ["実行者", accountLink(entry.initiator_id, { withName: true })],
      ["親の実行", entry.parent_execution_id ? executionRef(entry.parent_execution_id, entry.parent_origin, entry.parent_label_basis) : "なし"],
      ["コントラクト関数", entry.contract_function ? h("span", { class: "mono" }, entry.contract_function) : "-"],
    ]),
  );
}

async function start() {
  const raw = new URLSearchParams(window.location.search).get("id");
  const id = getParam("id", "positiveId");
  if (id === null) {
    renderError("transfer-error", raw === null || raw === "" ? "移動IDが指定されていません。URLに ?id=<移動ID> を付けてください。" : "移動IDが正しくありません。IDは正の数字で指定してください。");
    return;
  }
  document.title = `Transfer #${id} - RapidWire`;
  mount("transfer-title", "移動 ", h("span", { class: "id" }, `#${id}`));
  const errorHost = document.getElementById("transfer-error");
  renderLoading(errorHost);
  try {
    render(await api(`/v1/transfers/${id}`));
  } catch (error) {
    renderError(errorHost, error);
    return;
  }
  mount(errorHost);
  document.getElementById("transfer-body").hidden = false;
}

start();
