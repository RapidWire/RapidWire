// Explorer page: the search chooser for ?q=, the filtered and paged list of executions, and the latest transfers.
import {
  EXECUTION_STATUSES,
  EXECUTION_TYPES,
  api,
  dataTable,
  executionColumns,
  executionTypeLabel,
  getParam,
  h,
  isId,
  mount,
  pageParam,
  pager,
  renderEmpty,
  renderError,
  renderLoading,
  renderPageError,
  runSearch,
  transferFeedItem,
  withParams,
} from "/assets/app.js";

const PER_PAGE = 20;
const FEED_SIZE = 10;

// The filters of the URL, or {error} naming the first invalid one.
function readFilters() {
  const params = new URLSearchParams(window.location.search);
  const executionType = params.get("execution_type") || "";
  const status = params.get("status") || "";
  const initiator = (params.get("initiator_id") || "").trim();
  const page = pageParam("page", PER_PAGE);
  const filters = { executionType, status, initiator, page };
  if (executionType !== "" && !EXECUTION_TYPES.includes(executionType)) {
    return { ...filters, error: "種類の指定が正しくありません。" };
  }
  if (status !== "" && !EXECUTION_STATUSES.includes(status)) {
    return { ...filters, error: "状態の指定が正しくありません。" };
  }
  if (initiator !== "" && !isId(initiator)) {
    return { ...filters, error: "実行者のアカウントIDは数字で入力してください。" };
  }
  if (page === null) {
    return { ...filters, page: 1, pageError: true };
  }
  return filters;
}

function setUpFilters(filters) {
  const typeSelect = document.getElementById("filter-type");
  for (const type of EXECUTION_TYPES) {
    typeSelect.appendChild(h("option", { value: type, selected: type === filters.executionType }, `${executionTypeLabel(type)} (${type})`));
  }
  document.getElementById("filter-status").value = EXECUTION_STATUSES.includes(filters.status) ? filters.status : "";
  document.getElementById("filter-initiator").value = filters.initiator;
  const form = document.getElementById("filters");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const initiator = String(data.get("initiator_id") || "").trim();
    window.location.assign(withParams({ execution_type: data.get("execution_type"), status: data.get("status"), initiator_id: initiator, page: null, q: null }));
  });
}

const EXECUTION_COLUMNS = executionColumns();

async function loadExecutions(filters) {
  const container = document.getElementById("executions");
  const pagerHost = document.getElementById("executions-pager");
  const summary = document.getElementById("executions-summary");
  if (filters.error) {
    renderError(container, filters.error);
    return;
  }
  if (filters.pageError) {
    renderPageError(container, pagerHost, withParams({ page: null, q: null }) + "#executions-title", "実行一覧のページ");
    return;
  }
  renderLoading(container);
  let page;
  try {
    page = await api("/v1/executions", { query: { execution_type: filters.executionType, status: filters.status, initiator_id: filters.initiator, page: filters.page, per_page: PER_PAGE } });
  } catch (error) {
    renderError(container, error);
    return;
  }
  summary.textContent = page.total_is_lower_bound ? `${page.total.toLocaleString("en-US")}件以上` : `${page.total.toLocaleString("en-US")}件`;
  if (page.items.length === 0) {
    renderEmpty(container, page.total === 0 ? "条件に一致する実行はありません。" : "このページに実行はありません。");
  } else {
    mount(container, dataTable({ caption: "最新の実行", columns: EXECUTION_COLUMNS, rows: page.items }));
  }
  mount(pagerHost, pager(page, (target) => withParams({ page: target, q: null }) + "#executions-title", { label: "実行一覧のページ" }));
}

async function loadTransfers() {
  const container = document.getElementById("transfers");
  renderLoading(container);
  try {
    const page = await api("/v1/history", { query: { sort_by: "transfer_id", sort_order: "desc", per_page: FEED_SIZE } });
    if (page.items.length === 0) {
      renderEmpty(container, "まだ移動はありません。");
      return;
    }
    mount(container, h("ul", { class: "feed", "aria-label": "最新の移動" }, page.items.map(transferFeedItem)));
  } catch (error) {
    renderError(container, error);
  }
}

function startSearch() {
  const query = getParam("q");
  if (query === null) {
    return;
  }
  document.getElementById("site-search").value = query;
  document.getElementById("search-panel").hidden = false;
  runSearch(document.getElementById("search-results"), query);
}

const filters = readFilters();
setUpFilters(filters);
startSearch();
loadExecutions(filters);
loadTransfers();
