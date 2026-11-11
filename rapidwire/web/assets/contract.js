// Contract page for ?id=: the contract's state, public functions, paged storage (?spage=), versions (?vpage=) and calls (?cpage=, every contract_call execution of the contract); binaries are never requested.
import {
  accountLink,
  api,
  bytesText,
  contractStatusBadge,
  currencyLink,
  dataTable,
  executionColumns,
  executionLink,
  formatTime,
  getParam,
  h,
  kvList,
  mount,
  pageParam,
  pager,
  renderEmpty,
  renderError,
  renderLoading,
  renderPageError,
  withParams,
} from "/assets/app.js";

const VERSIONS_PER_PAGE = 15;
const STORAGE_PER_PAGE = 20;
const CALLS_PER_PAGE = 20;

// A typed contract value as nodes: scalars as text, accounts and currencies as links, lists and dicts as nested lists; every string is written as a text node.
function typedValue(value) {
  if (value === null || typeof value !== "object" || typeof value.type !== "string") {
    return h("span", { class: "mono" }, JSON.stringify(value));
  }
  const raw = value.value;
  switch (value.type) {
    case "int":
      return h("span", { class: "mono", title: "int" }, String(raw));
    case "str":
      return h("span", { class: "mono", title: "str" }, JSON.stringify(String(raw)));
    case "bool":
      return h("span", { class: "mono", title: "bool" }, raw === true ? "true" : "false");
    case "Account":
      return h("span", { title: "Account" }, accountLink(String(raw)));
    case "Currency":
      return h("span", { title: "Currency" }, currencyLink(String(raw)));
    case "list":
      if (!Array.isArray(raw) || raw.length === 0) {
        return h("span", { class: "mono", title: "list" }, "[]");
      }
      return h("span", null, h("span", { class: "sub" }, `list (${raw.length})`), h("ol", { start: "0" }, raw.map((item) => h("li", null, typedValue(item)))));
    case "dict":
      if (!Array.isArray(raw) || raw.length === 0) {
        return h("span", { class: "mono", title: "dict" }, "{}");
      }
      return h("span", null, h("span", { class: "sub" }, `dict (${raw.length})`), h("ul", null, raw.map((entry) => h("li", null, typedValue(entry && entry.key), h("span", { class: "flow-arrow" }, " : "), typedValue(entry && entry.value)))));
    default:
      return h("span", { class: "mono", title: value.type }, JSON.stringify(raw));
  }
}

function lockView(contract) {
  if (contract.locked) {
    return [h("span", { class: "badge badge--reverted" }, "ロック中 / locked"), h("span", { class: "sub" }, "期限: ", formatTime(contract.locked_until), " まで更新、一時停止、再開、削除はできません")];
  }
  if (contract.locked_until) {
    return [h("span", { class: "badge badge--muted" }, "ロックなし"), h("span", { class: "sub" }, "以前の期限: ", formatTime(contract.locked_until))];
  }
  return h("span", { class: "badge badge--muted" }, "ロックなし");
}

function renderOverview(contract) {
  mount(
    "contract-overview",
    kvList([
      ["所有アカウント", accountLink(contract.account_id, { withName: true })],
      ["状態", contractStatusBadge(contract.status)],
      ["現在の版", h("span", { class: "id" }, `v${contract.current_version}`)],
      ["SHA-256 (現在の版)", h("span", { class: "mono" }, contract.sha256)],
      ["サイズ (現在の版)", bytesText(contract.size)],
      ["登録された版の数", String(contract.version_count)],
      ["ロック", lockView(contract)],
      ["ストレージ", `${contract.storage_keys.toLocaleString("en-US")} キー / ${bytesText(contract.storage_bytes)}`],
      ["作成日時", formatTime(contract.created_at)],
      ["作成の実行", executionLink(contract.created_execution_id)],
      ["更新日時", formatTime(contract.updated_at)],
      ["更新の実行", executionLink(contract.updated_execution_id)],
      ["削除日時", formatTime(contract.deleted_at)],
    ]),
  );
}

const FUNCTION_COLUMNS = [
  { label: "名前", render: (fn) => h("span", { class: "mono" }, fn.name) },
  { label: "シグネチャ", render: (fn) => h("code", null, fn.signature) },
  { label: "引数", render: (fn) => (fn.params.length === 0 ? h("span", { class: "sub" }, "なし") : h("span", { class: "mono" }, fn.params.join(", "))) },
  { label: "戻り値", render: (fn) => h("span", { class: "mono" }, fn.returns) },
];

function renderFunctions(contract) {
  const container = document.getElementById("contract-functions");
  if (contract.functions.length === 0) {
    renderEmpty(container, contract.status === "deleted" ? "削除されたコントラクトの関数は呼び出せません。" : "公開関数はありません。");
    return;
  }
  mount(container, dataTable({ caption: "公開関数", columns: FUNCTION_COLUMNS, rows: contract.functions }));
}

const STORAGE_COLUMNS = [
  { label: "キー", render: (entry) => h("span", { class: "mono" }, entry.key) },
  { label: "値", render: (entry) => typedValue(entry.value) },
  { label: "サイズ", className: "num", render: (entry) => bytesText(entry.size) },
  { label: "更新日時", className: "nowrap", render: (entry) => formatTime(entry.updated_at) },
];

async function loadStorage(id) {
  const container = document.getElementById("contract-storage");
  const pageNumber = pageParam("spage", STORAGE_PER_PAGE);
  if (pageNumber === null) {
    renderPageError(container, "storage-pager", withParams({ spage: null }) + "#storage-title", "ストレージのページ");
    return;
  }
  renderLoading(container);
  try {
    const page = await api(`/v1/contracts/${id}/storage`, { query: { page: pageNumber, per_page: STORAGE_PER_PAGE } });
    document.getElementById("storage-summary").textContent = `${page.total.toLocaleString("en-US")}${page.total_is_lower_bound ? "件以上" : "件"}`;
    if (page.items.length === 0) {
      renderEmpty(container, page.total === 0 ? "ストレージは空です。" : "このページに値はありません。");
    } else {
      mount(container, dataTable({ caption: "ストレージ", columns: STORAGE_COLUMNS, rows: page.items }));
    }
    mount("storage-pager", pager(page, (target) => withParams({ spage: target }) + "#storage-title", { label: "ストレージのページ" }));
  } catch (error) {
    renderError(container, error);
  }
}

function versionColumns(current) {
  return [
    { label: "版", className: "nowrap", render: (version) => [h("span", { class: "id" }, `v${version.version}`), version.version === current ? [" ", h("span", { class: "badge badge--muted" }, "現在")] : null] },
    { label: "SHA-256", render: (version) => h("span", { class: "mono" }, version.sha256) },
    { label: "サイズ", className: "num", render: (version) => bytesText(version.size) },
    { label: "登録の実行", className: "nowrap", render: (version) => executionLink(version.created_execution_id) },
    { label: "登録日時", className: "nowrap", render: (version) => formatTime(version.created_at) },
  ];
}

async function loadVersions(id, current) {
  const container = document.getElementById("contract-versions");
  const pageNumber = pageParam("vpage", VERSIONS_PER_PAGE);
  if (pageNumber === null) {
    renderPageError(container, "versions-pager", withParams({ vpage: null }) + "#versions-title", "版の履歴のページ");
    return;
  }
  renderLoading(container);
  try {
    const page = await api(`/v1/contracts/${id}/versions`, { query: { page: pageNumber, per_page: VERSIONS_PER_PAGE } });
    document.getElementById("versions-summary").textContent = `${page.total.toLocaleString("en-US")}${page.total_is_lower_bound ? "件以上" : "件"}`;
    if (page.items.length === 0) {
      renderEmpty(container, "このページに版はありません。");
    } else {
      mount(container, dataTable({ caption: "版の履歴", columns: versionColumns(current), rows: page.items }));
    }
    mount("versions-pager", pager(page, (target) => withParams({ vpage: target }) + "#versions-title", { label: "版の履歴のページ" }));
  } catch (error) {
    renderError(container, error);
  }
}

async function loadCalls(id) {
  const container = document.getElementById("contract-calls");
  const pageNumber = pageParam("cpage", CALLS_PER_PAGE);
  if (pageNumber === null) {
    renderPageError(container, "calls-pager", withParams({ cpage: null }) + "#calls-title", "呼び出しのページ");
    return;
  }
  renderLoading(container);
  try {
    const page = await api("/v1/executions", { query: { contract_id: id, execution_type: "contract_call", page: pageNumber, per_page: CALLS_PER_PAGE } });
    document.getElementById("calls-summary").textContent = `${page.total.toLocaleString("en-US")}${page.total_is_lower_bound ? "件以上" : "件"}`;
    if (page.items.length === 0) {
      renderEmpty(container, page.total === 0 ? "このコントラクトはまだ呼び出されていません。" : "このページに呼び出しはありません。");
    } else {
      mount(container, dataTable({ caption: "コントラクト呼び出し", columns: executionColumns(), rows: page.items }));
    }
    mount("calls-pager", pager(page, (target) => withParams({ cpage: target }) + "#calls-title", { label: "呼び出しのページ" }));
  } catch (error) {
    renderError(container, error);
  }
}

async function main() {
  const overview = document.getElementById("contract-overview");
  const sections = ["contract-functions", "contract-storage", "contract-versions", "contract-calls"];
  const id = getParam("id", "positiveId");
  if (id === null) {
    renderError(overview, "コントラクトのアカウントIDを ?id= に正の整数で指定してください。");
    sections.forEach((name) => mount(name));
    return;
  }
  document.getElementById("contract-id").textContent = `Contract ${id}`;
  document.title = `Contract ${id} - RapidWire`;
  renderLoading(overview);
  let contract;
  try {
    contract = await api(`/v1/contracts/${id}`);
  } catch (error) {
    renderError(overview, error);
    sections.forEach((name) => mount(name));
    return;
  }
  renderOverview(contract);
  renderFunctions(contract);
  loadStorage(id);
  loadVersions(id, contract.current_version);
  loadCalls(id);
}

main();
