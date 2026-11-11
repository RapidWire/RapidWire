// Shared module of the RapidWire web explorer: API access, number and time formatting, a DOM builder that only ever writes text, links and account labels, badges, tables, transfer histories and feeds, paging, page states and the search.
// Pages import what they need from "/assets/app.js"; lookups are cached only in the open page's memory and nothing here stores data in the browser.

// Every execution type value of the ledger, in the server's order.
export const EXECUTION_TYPES = Object.freeze([
  "transfer",
  "transfer_from",
  "approve",
  "mint",
  "burn",
  "currency_create",
  "currency_renounce",
  "rate_change_request",
  "rate_change_apply",
  "currency_delete_request",
  "currency_delete_finalize",
  "delegation_enable",
  "delegation_disable",
  "stake_deposit",
  "stake_withdraw",
  "interest",
  "claim_create",
  "claim_pay",
  "claim_cancel",
  "notification_allow",
  "notification_deny",
  "discord_permission_allow",
  "discord_permission_deny",
  "token_issue",
  "token_revoke",
  "pool_create",
  "liquidity_add",
  "liquidity_remove",
  "swap",
  "contract_call",
  "contract_update",
  "contract_lock",
  "contract_pause",
  "contract_resume",
  "contract_delete",
  "backup_list",
  "backup_download",
]);

// Japanese display names of the execution types; a type missing here is shown as its value.
export const EXECUTION_TYPE_LABELS = Object.freeze({
  transfer: "送金",
  transfer_from: "代理送金",
  approve: "許可額の設定",
  mint: "発行",
  burn: "焼却",
  currency_create: "通貨の作成",
  currency_renounce: "発行権の放棄",
  rate_change_request: "金利変更の申請",
  rate_change_apply: "金利変更の適用",
  currency_delete_request: "通貨削除の申請",
  currency_delete_finalize: "通貨の削除",
  delegation_enable: "委任の有効化",
  delegation_disable: "委任の無効化",
  stake_deposit: "ステーク預け入れ",
  stake_withdraw: "ステーク引き出し",
  interest: "利息",
  claim_create: "請求の作成",
  claim_pay: "請求の支払い",
  claim_cancel: "請求の取り消し",
  notification_allow: "通知の許可",
  notification_deny: "通知の拒否",
  discord_permission_allow: "Discord操作の許可",
  discord_permission_deny: "Discord操作の取り消し",
  token_issue: "APIトークンの発行",
  token_revoke: "APIトークンの失効",
  pool_create: "プールの作成",
  liquidity_add: "流動性の追加",
  liquidity_remove: "流動性の引き出し",
  swap: "スワップ",
  contract_call: "コントラクト呼び出し",
  contract_update: "コントラクトの登録",
  contract_lock: "コントラクトのロック",
  contract_pause: "コントラクトの一時停止",
  contract_resume: "コントラクトの再開",
  contract_delete: "コントラクトの削除",
  backup_list: "バックアップ一覧",
  backup_download: "バックアップ取得",
});

export const EXECUTION_STATUSES = Object.freeze(["success", "failed", "reverted"]);

const STATUS_LABELS = Object.freeze({ success: "成功", failed: "失敗", reverted: "リバート" });

const TRANSFER_KIND_BADGES = Object.freeze({
  principal: ["元本 / principal", "badge--muted"],
  interest: ["利息 / interest", "badge--kind-interest"],
  gas: ["ガス / gas", "badge--kind-gas"],
});

const CONTRACT_STATUS_BADGES = Object.freeze({
  active: ["badge--success", "稼働中 / active"],
  paused: ["badge--reverted", "一時停止 / paused"],
  deleted: ["badge--failed", "削除済み / deleted"],
});

// The stake custody system account; other negative IDs are issuance and pool custody accounts.
export const STAKE_CUSTODY_ACCOUNT_ID = "-1";
export const STAKE_CUSTODY_LABEL = "ステーク預かり / Stake custody";

// Display names of the account kinds of GET /v1/accounts/{account_id}.
export const ACCOUNT_KIND_LABELS = Object.freeze({
  user: "ユーザー / user",
  issuance: "発行アカウント / issuance",
  stake_custody: "ステーク預かり / stake custody",
  pool_custody: "プールのカストディ / pool custody",
});

export const HISTORY_SORT_FIELDS = Object.freeze(["transfer_id", "timestamp", "amount"]);
export const HISTORY_SORT_ORDERS = Object.freeze(["desc", "asc"]);

// Only the first MAX_WINDOW items of a list can be paged through.
export const MAX_WINDOW = 10000;

const INT64_MAX = 9223372036854775807n;
const ID_PATTERN = /^-?[1-9][0-9]{0,18}$/;
const AMOUNT_PATTERN = /^(-?)([0-9]+)(?:\.([0-9]{1,3}))?$/;
const MINIMAL_UNITS_PATTERN = /^(-?)([0-9]+)$/;
const SYMBOL_PATTERN = /^[A-Z]+(?:-[A-Z]+){0,2}$/;
const SYMBOL_MAX_LENGTH = 8;
const API_PATH_PATTERN = /^\/v1\/[^?#]*$/;
// A same-origin path starts with one slash that is not followed by a slash or a backslash, which browsers would read as the start of another host.
const SAFE_URL_PATTERN = /^(?:\/(?![/\\])|\?|#|https:\/\/)/;
const SAME_ORIGIN_PATH_PATTERN = /^\/(?![/\\])/;

// An error answered by the API, carrying the HTTP status (0 when the server could not be reached) and the code and message of the error envelope.
export class ApiError extends Error {
  constructor(status, code, message, envelope = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.envelope = envelope;
  }
}

function buildUrl(path, query) {
  if (!API_PATH_PATTERN.test(path)) {
    throw new TypeError("API paths start with the API base path and carry no query string");
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") {
      params.append(key, String(value));
    }
  }
  const search = params.toString();
  return search === "" ? path : `${path}?${search}`;
}

// Fetch an API path on this origin and return the Response when its status is 2xx; otherwise throw ApiError from the error envelope (aborts propagate as AbortError).
export async function apiFetch(path, { query, headers, signal, method = "GET" } = {}) {
  let response;
  try {
    response = await fetch(buildUrl(path, query), { method, headers: { Accept: "application/json", ...headers }, signal, credentials: "same-origin", cache: "no-store", redirect: "error" });
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw error;
    }
    throw new ApiError(0, "network_error", "The server could not be reached.");
  }
  if (!response.ok) {
    let envelope = null;
    try {
      envelope = await response.json();
    } catch {
      envelope = null;
    }
    const detail = envelope && typeof envelope.error === "object" && envelope.error !== null ? envelope.error : {};
    const code = typeof detail.code === "string" ? detail.code : "http_error";
    const message = typeof detail.message === "string" ? detail.message : `The server answered with status ${response.status}.`;
    throw new ApiError(response.status, code, message, envelope);
  }
  return response;
}

// GET an API path and return its JSON body; IDs and amounts arrive as strings and stay strings.
export async function api(path, options = {}) {
  const response = await apiFetch(path, options);
  return response.json();
}

// True when value is a decimal account ID string in the signed 64-bit range; positive restricts it to IDs above zero.
export function isId(value, { positive = false } = {}) {
  if (typeof value !== "string" || !ID_PATTERN.test(value)) {
    return false;
  }
  if (positive && value.startsWith("-")) {
    return false;
  }
  const big = BigInt(value);
  return big <= INT64_MAX && big >= -INT64_MAX - 1n;
}

// The message shown for a page number that is malformed or lies beyond the first MAX_WINDOW items.
export const PAGE_NUMBER_ERROR = `ページ番号が正しくありません。一覧は先頭から${MAX_WINDOW.toLocaleString("en-US")}件目までをページで表示できます。`;

// Read a query parameter of the page URL; kind "id" accepts any account ID, "positiveId" a positive ID, "page" an integer from 1 to MAX_WINDOW and "string" any non-empty value; a missing or invalid value gives null.
export function getParam(name, kind = "string") {
  const raw = new URLSearchParams(window.location.search).get(name);
  if (raw === null || raw === "") {
    return null;
  }
  switch (kind) {
    case "id":
      return isId(raw) ? raw : null;
    case "positiveId":
      return isId(raw, { positive: true }) ? raw : null;
    case "page":
      return /^[1-9][0-9]{0,4}$/.test(raw) && Number(raw) <= MAX_WINDOW ? Number(raw) : null;
    case "string":
      return raw;
    default:
      throw new TypeError(`unknown parameter kind ${kind}`);
  }
}

// The page number of a paged list from its query parameter: 1 when absent, null when present but invalid or when a page of perPage items would reach past the first MAX_WINDOW items, which the API refuses.
export function pageParam(name, perPage) {
  if (new URLSearchParams(window.location.search).get(name) === null) {
    return 1;
  }
  const page = getParam(name, "page");
  return page !== null && page <= Math.floor(MAX_WINDOW / perPage) ? page : null;
}

function groupThousands(digits) {
  return digits.replace(/\B(?=(?:[0-9]{3})+$)/g, ",");
}

// Group the digits of an integer string such as a share count, a gas count or a rate in ppm ("0001234567" -> "1,234,567"); null and undefined give "-" and any other input is returned as text.
export function groupInteger(value) {
  if (value === null || value === undefined) {
    return "-";
  }
  const match = /^(-?)([0-9]+)$/.exec(String(value));
  return match === null ? String(value) : `${match[1]}${groupThousands(match[2].replace(/^0+(?=[0-9])/, ""))}`;
}

// An hourly rate in parts per million as a percentage with 4 decimals ("1500" -> "0.1500"), by moving the decimal point of the string.
export function ppmPercent(ppm) {
  if (!/^[0-9]+$/.test(String(ppm))) {
    return String(ppm);
  }
  const digits = String(ppm).padStart(5, "0");
  return `${groupInteger(digits.slice(0, -4))}.${digits.slice(-4)}`;
}

// The total of a Page object with thousands separators, followed by "+" when it is a lower bound.
export function countText(page) {
  return `${page.total.toLocaleString("en-US")}${page.total_is_lower_bound ? "+" : ""}`;
}

const BYTE_UNITS = [
  ["TiB", 1024n ** 4n],
  ["GiB", 1024n ** 3n],
  ["MiB", 1024n ** 2n],
  ["KiB", 1024n],
];

// An exact byte count, "1,572,864 bytes"; anything but a safe integer is returned as text.
export function bytesText(size) {
  return Number.isSafeInteger(size) ? `${size.toLocaleString("en-US")} bytes` : String(size);
}

// A byte count with a binary unit to one decimal, computed with BigInt, followed by the exact count: "1.5 MiB (1,572,864 bytes)".
export function formatBytes(size) {
  if (!Number.isSafeInteger(size) || size < 0) {
    return String(size);
  }
  const bytes = BigInt(size);
  for (const [unit, scale] of BYTE_UNITS) {
    if (bytes >= scale) {
      const tenths = (bytes * 10n) / scale;
      return `${tenths / 10n}.${tenths % 10n} ${unit} (${bytesText(size)})`;
    }
  }
  return bytesText(size);
}

// Format an API amount string ("-1234567.5" or "-1234567.500") with thousands separators and always exactly 3 decimals ("-1,234,567.500"), using string operations only; any other input is returned unchanged.
export function formatAmount(value) {
  const match = typeof value === "string" ? AMOUNT_PATTERN.exec(value) : null;
  if (match === null) {
    return String(value);
  }
  const integer = match[2].replace(/^0+(?=[0-9])/, "");
  const fraction = (match[3] || "").padEnd(3, "0");
  const negative = match[1] === "-" && (integer !== "0" || fraction !== "000");
  return `${negative ? "-" : ""}${groupThousands(integer)}.${fraction}`;
}

// Convert a minimal-unit integer string from a raw execution input or output (1 unit = 1000 minimal units) to the formatAmount form ("-12500" -> "-12.500"); any other input is returned unchanged.
export function formatMinimalUnits(value) {
  const match = typeof value === "string" ? MINIMAL_UNITS_PATTERN.exec(value) : null;
  if (match === null) {
    return String(value);
  }
  const digits = match[2].replace(/^0+/, "").padStart(4, "0");
  return formatAmount(`${match[1]}${digits.slice(0, -3)}.${digits.slice(-3)}`);
}

const JST_FORMAT = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

// The text of an ISO 8601 time in JST, "2026-11-05 21:04:05 JST"; null gives "-" and an unparsable value is returned unchanged.
export function formatTimeText(iso) {
  if (iso === null || iso === undefined) {
    return "-";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return String(iso);
  }
  const parts = Object.fromEntries(JST_FORMAT.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} JST`;
}

// A <time> element showing an ISO 8601 time in JST, with the original UTC string as its datetime and title; null gives a "-" text node.
export function formatTime(iso) {
  if (iso === null || iso === undefined) {
    return document.createTextNode("-");
  }
  return h("time", { datetime: String(iso), title: String(iso) }, formatTimeText(iso));
}

function appendChildren(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) {
      continue;
    }
    if (Array.isArray(child)) {
      appendChildren(parent, child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
}

// Build an element: attrs maps attribute names to values (null, undefined and false skip one, true sets it empty), functions under "on<Event>" names become listeners, href/src/action must be same-origin paths, fragments, queries or https URLs, and children (strings, numbers, nodes, nested arrays) are appended with strings as text nodes.
export function h(tag, attrs, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (typeof value === "function") {
      if (!/^on[A-Z]/.test(name)) {
        throw new TypeError(`listener attribute ${name} must be named on<Event>`);
      }
      element.addEventListener(name.slice(2).toLowerCase(), value);
      continue;
    }
    const text = value === true ? "" : String(value);
    if ((name === "href" || name === "src" || name === "action") && !SAFE_URL_PATTERN.test(text)) {
      throw new TypeError(`unsafe URL in ${name}`);
    }
    element.setAttribute(name, text);
  }
  appendChildren(element, children);
  return element;
}

// Replace the children of a container (an element or its id) with the given nodes or strings and return the container.
export function mount(container, ...children) {
  const target = typeof container === "string" ? document.getElementById(container) : container;
  target.replaceChildren();
  appendChildren(target, children);
  return target;
}

// The page URL of an object kind with its ID, such as pageUrl("account", "123") -> "/account.html?id=123".
export function pageUrl(kind, id) {
  return `/${kind}.html?id=${encodeURIComponent(id)}`;
}

// The label of an account ID known without a request: the stake custody label for -1, "System #<id>" for other negative IDs until systemAccountLabel() resolves them, else the ID itself.
export function accountLabel(id) {
  const text = String(id);
  if (text === STAKE_CUSTODY_ACCOUNT_ID) {
    return STAKE_CUSTODY_LABEL;
  }
  if (text.startsWith("-")) {
    return `System #${text}`;
  }
  return text;
}

// The label of an issuance account from its currency's symbol.
export function issuanceLabel(symbol) {
  return `${symbol} 発行`;
}

// The label of a pool custody account from the pool, with the pair's symbols when the pool object carries them.
export function poolCustodyLabel(poolId, pool = null) {
  const pair = pool && pool.symbol_a && pool.symbol_b ? ` (${pool.symbol_a}/${pool.symbol_b})` : "";
  return `Pool #${poolId} カストディ${pair}`;
}

// Run a lookup once per key and page load: concurrent callers share one promise and later callers its answer, while a rejected lookup is dropped from the cache so the next use asks again.
function once(cache, key, load) {
  let pending = cache.get(key);
  if (pending === undefined) {
    pending = load();
    cache.set(key, pending);
    pending.catch(() => {
      if (cache.get(key) === pending) {
        cache.delete(key);
      }
    });
  }
  return pending;
}

const usernames = new Map();
const accounts = new Map();
const currencies = new Map();
const pools = new Map();
const systemLabels = new Map();
let currencyIndex = null;

// The Discord username of a positive account ID, kept in memory for the page load once found or answered 404; null when it cannot be found now (404, 503 or any other failure), and a failure other than 404 is asked again on the next use.
export function lookupUsername(id) {
  const text = String(id);
  if (!isId(text, { positive: true })) {
    return Promise.resolve(null);
  }
  return once(usernames, text, () => apiOrNull(`/v1/users/${text}/name`).then((body) => (body !== null && typeof body.username === "string" ? body.username : null))).catch(() => null);
}

// The account object of GET /v1/accounts/{account_id}, read once per page load; resolves to null when the account does not exist and rejects with the ApiError of any other failure, which the next use retries.
export function lookupAccount(id) {
  const text = String(id);
  return once(accounts, text, () => apiOrNull(`/v1/accounts/${text}`));
}

// A currency of GET /v1/currencies/{currency_id}, read once per page load; resolves to null when it does not exist and rejects with the ApiError of any other failure, which the next use retries.
export function lookupCurrency(id) {
  const text = String(id);
  return once(currencies, text, () => apiOrNull(`/v1/currencies/${text}`));
}

// A pool of GET /v1/pools/{pool_id}, read once per page load; resolves to null when it does not exist and rejects with the ApiError of any other failure, which the next use retries.
export function lookupPool(id) {
  const text = String(id);
  return once(pools, text, () => apiOrNull(`/v1/pools/${text}`));
}

// Every currency, deleted ones included, keyed by ID and read once per page load; an empty map when the list cannot be read or parsed, so symbols fall back to IDs, and the next use reads the list again.
export function loadCurrencyIndex() {
  if (currencyIndex === null) {
    const loading = api("/v1/currencies", { query: { include_deleted: "true" } })
      .then((body) => new Map(body.items.map((item) => [item.currency_id, item])))
      .catch(() => {
        if (currencyIndex === loading) {
          currencyIndex = null;
        }
        return new Map();
      });
    currencyIndex = loading;
  }
  return currencyIndex;
}

// Record a currency the page has read, so the labels of it and its issuance account need no further request.
export function rememberCurrency(currency) {
  currencies.set(currency.currency_id, Promise.resolve(currency));
  systemLabels.set(currency.issuance_account_id, Promise.resolve(issuanceLabel(currency.symbol)));
}

// Record a pool the page has read, so the label of its custody account needs no further request.
export function rememberPool(pool) {
  pools.set(pool.pool_id, Promise.resolve(pool));
  systemLabels.set(pool.custody_account_id, Promise.resolve(poolCustodyLabel(pool.pool_id, pool)));
}

async function currencySymbol(id) {
  if (currencyIndex !== null) {
    const item = (await currencyIndex).get(id);
    if (item) {
      return item.symbol;
    }
  }
  const currency = await lookupCurrency(id);
  return currency === null ? null : currency.symbol;
}

// A system account label built while a lookup it needs failed, shown for now but not kept, so the next use resolves it again.
class PartialLabel {
  constructor(label) {
    this.label = label;
  }
}

async function resolveSystemLabel(id) {
  const account = await lookupAccount(id);
  if (account === null) {
    return null;
  }
  switch (account.kind) {
    case "stake_custody":
      return STAKE_CUSTODY_LABEL;
    case "issuance": {
      let symbol;
      try {
        symbol = await currencySymbol(account.currency_id);
      } catch {
        throw new PartialLabel(issuanceLabel(`#${account.currency_id}`));
      }
      return issuanceLabel(symbol === null ? `#${account.currency_id}` : symbol);
    }
    case "pool_custody": {
      let pool;
      try {
        pool = await lookupPool(account.pool_id);
      } catch {
        throw new PartialLabel(poolCustodyLabel(account.pool_id));
      }
      return poolCustodyLabel(account.pool_id, pool);
    }
    default:
      return null;
  }
}

// The label of a system account: the stake custody label for -1, issuanceLabel() of the currency's symbol for an issuance account and poolCustodyLabel() with the pair for a pool custody account, each resolved once per page load; null for a positive ID and when the account is unknown or cannot be read now, and a label resolved while a lookup failed is resolved again on the next use.
export function systemAccountLabel(id) {
  const text = String(id);
  if (text === STAKE_CUSTODY_ACCOUNT_ID) {
    return Promise.resolve(STAKE_CUSTODY_LABEL);
  }
  if (!isId(text) || !text.startsWith("-")) {
    return Promise.resolve(null);
  }
  return once(systemLabels, text, () => resolveSystemLabel(text)).catch((error) => (error instanceof PartialLabel ? error.label : null));
}

// A link to an account page: a system account is labelled by what it is once systemAccountLabel() resolves, and with withName true a user account's Discord username is shown before the ID once it arrives; the ID stays visible or in the tooltip.
export function accountLink(id, { withName = false } = {}) {
  if (id === null || id === undefined) {
    return document.createTextNode("-");
  }
  const text = String(id);
  const system = text.startsWith("-");
  const link = h("a", { href: pageUrl("account", text), class: system ? "account account--system" : "account id", title: system ? `Account ${text}` : null }, accountLabel(text));
  if (system) {
    systemAccountLabel(text).then((label) => {
      if (label !== null) {
        link.replaceChildren(label);
      }
    });
  } else if (withName) {
    lookupUsername(text).then((name) => {
      if (name !== null) {
        link.classList.remove("id");
        link.replaceChildren(document.createTextNode(name), h("span", { class: "id sub" }, text));
      }
    });
  }
  return link;
}

// Two account links joined by an arrow, read as "from to to" by assistive technology.
export function accountFlow(from, to, { withName = false } = {}) {
  return h("span", { class: "flow" }, accountLink(from, { withName }), h("span", { class: "flow-arrow", "aria-hidden": "true" }, "->"), h("span", { class: "visually-hidden" }, "to"), accountLink(to, { withName }));
}

// A link to a currency page labelled with its symbol, or with its ID when the symbol is unknown.
export function currencyLink(id, symbol = null) {
  if (id === null || id === undefined) {
    return document.createTextNode("-");
  }
  return h("a", { href: pageUrl("currency", id), class: symbol ? "currency" : "currency id", title: symbol ? `Currency ${id}` : null }, symbol || `#${id}`);
}

// A link to an execution page labelled "#<id>".
export function executionLink(id) {
  return id === null || id === undefined ? document.createTextNode("-") : h("a", { href: pageUrl("execution", id), class: "id" }, `#${id}`);
}

// A link to a transfer page labelled "#<id>".
export function transferLink(id) {
  return id === null || id === undefined ? document.createTextNode("-") : h("a", { href: pageUrl("transfer", id), class: "id" }, `#${id}`);
}

// A link to a pool page labelled "Pool #<id>".
export function poolLink(id) {
  return id === null || id === undefined ? document.createTextNode("-") : h("a", { href: pageUrl("pool", id), class: "id" }, `Pool #${id}`);
}

// A link to the contract page of an account, labelled with the account ID.
export function contractLink(id) {
  return id === null || id === undefined ? document.createTextNode("-") : h("a", { href: pageUrl("contract", id), class: "id" }, `Contract ${id}`);
}

// An amount in API amount format with its currency symbol; sign "auto" colours negatives red, "in" and "out" force a green "+" or a red "-" prefix for history rows.
export function amountView(amount, symbol = null, { sign = "none" } = {}) {
  let text = formatAmount(amount);
  let tone = null;
  if (sign === "in") {
    text = `+${text.replace(/^-/, "")}`;
    tone = "amount--pos";
  } else if (sign === "out") {
    text = `-${text.replace(/^-/, "")}`;
    tone = "amount--neg";
  } else if (sign === "auto" && text.startsWith("-")) {
    tone = "amount--neg";
  }
  return h("span", { class: tone ? `amount ${tone}` : "amount" }, text, symbol ? h("span", { class: "unit" }, symbol) : null);
}

// The display name of an execution type.
export function executionTypeLabel(type) {
  return EXECUTION_TYPE_LABELS[type] || String(type);
}

// A badge for an execution status: success green, failed red, reverted orange.
export function statusBadge(status) {
  const known = EXECUTION_STATUSES.includes(status);
  return h("span", { class: known ? `badge badge--${status}` : "badge badge--muted" }, known ? `${STATUS_LABELS[status]} / ${status}` : String(status));
}

// A badge for a transfer kind: principal stays quiet, interest and gas are marked.
export function kindBadge(kind) {
  const [label, tone] = TRANSFER_KIND_BADGES[kind] || [String(kind), "badge--muted"];
  return h("span", { class: `badge ${tone}` }, label);
}

// A badge for a contract status: active green, paused orange, deleted red.
export function contractStatusBadge(status) {
  const [tone, label] = CONTRACT_STATUS_BADGES[status] || ["badge--muted", String(status)];
  return h("span", { class: `badge ${tone}` }, label);
}

// A key-value list (dl.kv) from [label, value] pairs, where a value is a node, a string or an array of them.
export function kvList(pairs) {
  return h(
    "dl",
    { class: "kv" },
    pairs.map(([label, value]) => [h("dt", null, label), h("dd", null, value === null || value === undefined ? "-" : value)]),
  );
}

// A data table inside a .table-wrap: columns are {label, render(row) -> node|string|array, className?} and each cell's content is wrapped in one div.cell; with cards true (the default) rows become stacked cards on narrow screens, keeping their table semantics through explicit ARIA roles, and caption names the table for assistive technology (visually hidden unless showCaption).
export function dataTable({ caption, columns, rows, cards = true, showCaption = false }) {
  const head = h("thead", { role: "rowgroup" }, h("tr", { role: "row" }, columns.map((column) => h("th", { scope: "col", role: "columnheader", class: column.className || null }, column.label))));
  const body = h(
    "tbody",
    { role: "rowgroup" },
    rows.map((row) => h("tr", { role: "row" }, columns.map((column) => h("td", { role: "cell", "data-label": column.label, class: column.className || null }, h("div", { class: "cell" }, column.render(row)))))),
  );
  const table = h("table", { class: cards ? "data-table data-table--cards" : "data-table", role: "table" }, h("caption", { class: showCaption ? null : "visually-hidden" }, caption), head, body);
  return h("div", { class: "table-wrap" }, table);
}

// The number of pages a Page object can reach, limited to the first MAX_WINDOW items.
export function pageCount(page) {
  const reachable = Math.min(page.total, MAX_WINDOW);
  return Math.max(1, Math.min(Math.ceil(reachable / page.per_page), Math.floor(MAX_WINDOW / page.per_page)));
}

// A pager for a Page object {items, page, per_page, total, total_is_lower_bound}; hrefFor(n) gives the URL of page n so every page is a link, label names the list, and on a page past the end "previous" leads to the last page.
export function pager(page, hrefFor, { label = "ページ" } = {}) {
  const last = pageCount(page);
  const totalText = page.total_is_lower_bound ? `${page.total.toLocaleString("en-US")}+ 件` : `${page.total.toLocaleString("en-US")} 件`;
  const status = `${page.page} / ${last}${page.total_is_lower_bound ? "+" : ""} ページ (${totalText})`;
  const step = (target, text, rel) => (target >= 1 && target <= last && target !== page.page ? h("a", { class: "btn", href: hrefFor(target), rel }, text) : h("span", { class: "btn", "aria-disabled": "true" }, text));
  return h("nav", { class: "pager", "aria-label": label }, step(Math.min(page.page - 1, last), "前へ", "prev"), h("span", { class: "pager-status", "aria-live": "polite" }, status), step(page.page + 1, "次へ", "next"));
}

// A URL of the current page with the given query parameters replaced (null or "" removes one): the page's path and query, or only "?" and the query, relative to the current document, when the path is not a plain same-origin path.
export function withParams(changes) {
  const params = new URLSearchParams(window.location.search);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === undefined || value === "") {
      params.delete(key);
    } else {
      params.set(key, String(value));
    }
  }
  const search = params.toString();
  const path = SAME_ORIGIN_PATH_PATTERN.test(window.location.pathname) ? window.location.pathname : "";
  return search === "" ? path || "?" : `${path}?${search}`;
}

// Show a loading state in a container.
export function renderLoading(container, text = "読み込み中...") {
  return mount(container, h("div", { class: "state state--loading", role: "status" }, text));
}

// Show an empty state in a container.
export function renderEmpty(container, text = "データがありません。") {
  return mount(container, h("div", { class: "state state--empty" }, text));
}

// The title and explanation shown for an error: 404 is "not found", 503 discord_unavailable asks to retry later, a network failure names the connection; other errors show the API's message.
export function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return ["見つかりません / Not found", error.message];
    }
    if (error.status === 503 && error.code === "discord_unavailable") {
      return ["Discordに接続できません", "Discordから情報を取得できないため、しばらくしてから再度お試しください。"];
    }
    if (error.status === 0) {
      return ["サーバーに接続できません", "ネットワークを確認して、再度お試しください。"];
    }
    if (error.status === 422) {
      return ["入力が正しくありません", error.message];
    }
    return [`エラー (${error.status} ${error.code})`, error.message];
  }
  if (typeof error === "string") {
    return ["入力が正しくありません", error];
  }
  return ["エラーが発生しました", "ページを再読み込みしてください。"];
}

// Show an error state in a container for an ApiError, a validation message string or any other error.
export function renderError(container, error) {
  const [title, detail] = describeError(error);
  return mount(container, h("div", { class: "state state--error", role: "alert" }, h("p", { class: "state-title" }, title), detail ? h("p", null, detail) : null));
}

// Show PAGE_NUMBER_ERROR in a list's container and a link to its first page (firstHref) in its pager container, without asking the API.
export function renderPageError(container, pagerContainer, firstHref, label) {
  renderError(container, PAGE_NUMBER_ERROR);
  mount(pagerContainer, h("nav", { class: "pager", "aria-label": label }, h("a", { class: "btn", href: firstHref }, "最初のページへ")));
}

// Run an API lookup that may legitimately be missing: the result, or null on 404.
export async function apiOrNull(path, options = {}) {
  try {
    return await api(path, options);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

// Load one paged list into its host element and pager: the page number comes from the query parameter param, the API path is read with query plus page and per_page, onPage(page) sees each answer, and pager links set param, keep the other parameters, apply extra and end with anchor.
export async function loadPagedTable({ host, pagerHost, path, query = {}, param, perPage, caption, columns, emptyText, extra = {}, anchor = "", onPage = null }) {
  const hostElement = typeof host === "string" ? document.getElementById(host) : host;
  const pagerElement = typeof pagerHost === "string" ? document.getElementById(pagerHost) : pagerHost;
  const number = pageParam(param, perPage);
  if (number === null) {
    renderPageError(hostElement, pagerElement, withParams({ ...extra, [param]: null }) + anchor, `${caption}のページ`);
    return;
  }
  renderLoading(hostElement);
  let page;
  try {
    page = await api(path, { query: { ...query, page: number, per_page: perPage } });
  } catch (error) {
    renderError(hostElement, error);
    mount(pagerElement);
    return;
  }
  if (onPage !== null) {
    onPage(page);
  }
  if (page.items.length === 0) {
    renderEmpty(hostElement, page.total === 0 ? emptyText : "このページには項目がありません。");
  } else {
    mount(hostElement, dataTable({ caption, columns, rows: page.items }));
  }
  if (page.page > 1 || pageCount(page) > 1) {
    mount(pagerElement, pager(page, (target) => withParams({ ...extra, [param]: target }) + anchor, { label: `${caption}のページ` }));
  } else {
    mount(pagerElement);
  }
}

function historyExecutionCell(row) {
  return [
    executionLink(row.execution_id),
    h("span", { class: "sub" }, executionTypeLabel(row.execution_type), " ", h("span", { class: "mono" }, `(${row.execution_type})`)),
    row.contract_function ? h("span", { class: "sub mono" }, row.contract_function) : null,
    row.execution_status !== "success" ? statusBadge(row.execution_status) : null,
  ];
}

// The columns of a transfer history table of TransferEntry rows: the transfer and its time, then the page's own partyColumns (accounts and amount), then the transfer kind and the execution.
export function historyColumns(partyColumns) {
  return [
    { label: "移動", className: "nowrap", render: (row) => transferLink(row.transfer_id) },
    { label: "時刻", className: "nowrap", render: (row) => formatTime(row.created_at) },
    ...partyColumns,
    { label: "区分", render: (row) => kindBadge(row.kind) },
    { label: "実行", render: historyExecutionCell },
  ];
}

// The columns of an execution list of GET /v1/executions rows: the execution with its parent, time, type, status, optionally the initiator, and the contract function.
export function executionColumns({ initiator = true } = {}) {
  return [
    { label: "ID", className: "nowrap", render: (row) => [executionLink(row.execution_id), row.parent_execution_id ? h("span", { class: "sub" }, "親: ", executionLink(row.parent_execution_id)) : null] },
    { label: "時刻", className: "nowrap", render: (row) => formatTime(row.created_at) },
    { label: "種類", render: (row) => [executionTypeLabel(row.type), h("span", { class: "sub mono" }, row.type)] },
    { label: "状態", render: (row) => statusBadge(row.status) },
    initiator ? { label: "実行者", render: (row) => accountLink(row.initiator_id, { withName: true }) } : null,
    { label: "関数", render: (row) => (row.contract_function ? h("span", { class: "mono" }, row.contract_function) : "-") },
  ].filter((column) => column !== null);
}

// The sort of a transfer history from the URL's sort_by and sort_order: {sortBy, sortOrder}, plus error when either is invalid.
export function readHistorySort() {
  const params = new URLSearchParams(window.location.search);
  const sort = { sortBy: params.get("sort_by") || "transfer_id", sortOrder: params.get("sort_order") || "desc" };
  if (!HISTORY_SORT_FIELDS.includes(sort.sortBy) || !HISTORY_SORT_ORDERS.includes(sort.sortOrder)) {
    return { ...sort, error: "並び替えの指定が正しくありません。" };
  }
  return sort;
}

// Set a history filter form's sort_by and sort_order controls from sort and make submitting it open the first page (hp) of the history at #history-title with the chosen sort and the parameters extra(formData) returns; defaults are left out of the URL.
export function setUpHistoryForm(form, sort, extra = () => ({})) {
  form.elements.namedItem("sort_by").value = HISTORY_SORT_FIELDS.includes(sort.sortBy) ? sort.sortBy : "transfer_id";
  form.elements.namedItem("sort_order").value = HISTORY_SORT_ORDERS.includes(sort.sortOrder) ? sort.sortOrder : "desc";
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const sortBy = String(data.get("sort_by"));
    const sortOrder = String(data.get("sort_order"));
    window.location.assign(withParams({ ...extra(data), sort_by: sortBy === "transfer_id" ? null : sortBy, sort_order: sortOrder === "desc" ? null : sortOrder, hp: null }) + "#history-title");
  });
}

// One item of a compact transfer feed (ul.feed): the transfer and its amount, the two accounts, and the time, execution type, execution and contract function.
export function transferFeedItem(entry) {
  return h(
    "li",
    null,
    h("div", { class: "feed-top" }, transferLink(entry.transfer_id), h("span", null, amountView(entry.amount, null), " ", currencyLink(entry.currency_id, entry.currency_symbol))),
    accountFlow(entry.from_account, entry.to_account),
    h(
      "span",
      { class: "sub" },
      formatTime(entry.created_at),
      " ",
      executionTypeLabel(entry.execution_type),
      " ",
      executionLink(entry.execution_id),
      entry.contract_function ? [" ", h("span", { class: "mono" }, `${entry.contract_function}()`)] : null,
    ),
  );
}

// Classify search text: {kind: "id", value} for a decimal ID, {kind: "symbol", value} for a currency symbol (upper-cased), else {kind: "invalid", message}.
export function classifySearch(text) {
  const value = (text || "").trim();
  if (value === "") {
    return { kind: "invalid", message: "検索語を入力してください。" };
  }
  if (isId(value)) {
    return { kind: "id", value };
  }
  const symbol = value.toUpperCase();
  if (symbol.length <= SYMBOL_MAX_LENGTH && SYMBOL_PATTERN.test(symbol)) {
    return { kind: "symbol", value: symbol };
  }
  return { kind: "invalid", message: "IDは数字、通貨シンボルは8文字以内の英字とハイフンで入力してください。" };
}

// Resolve search text into destinations: {query, destinations: [{kind, label, detail, href}], navigate: href|null, failed: bool, message?}; an ID is looked up as an account and, when positive, as an execution, transfer, currency and contract, only the items that exist become destinations, and navigate is set when exactly one exists and every lookup answered.
export async function resolveSearch(text, { signal } = {}) {
  const query = classifySearch(text);
  if (query.kind === "invalid") {
    return { query, destinations: [], navigate: null, failed: false, message: query.message };
  }
  if (query.kind === "symbol") {
    const currency = await apiOrNull(`/v1/currencies/symbol/${encodeURIComponent(query.value)}`, { signal });
    if (currency === null) {
      return { query, destinations: [], navigate: null, failed: false, message: `通貨シンボル ${query.value} は見つかりません。` };
    }
    const href = pageUrl("currency", currency.currency_id);
    return { query, destinations: [{ kind: "通貨", label: `${currency.symbol} (${currency.name})`, detail: currency.currency_id, href }], navigate: href, failed: false };
  }
  const id = query.value;
  const accountLookup = apiOrNull(`/v1/accounts/${id}`, { signal }).then((found) => found && { kind: "アカウント", label: accountLabel(id), detail: ACCOUNT_KIND_LABELS[found.kind] || found.kind, href: pageUrl("account", id) });
  const lookups = id.startsWith("-")
    ? [accountLookup]
    : [
        apiOrNull(`/v1/executions/${id}`, { signal }).then((found) => found && { kind: "実行", label: `#${id}`, detail: executionTypeLabel(found.type), href: pageUrl("execution", id) }),
        apiOrNull(`/v1/transfers/${id}`, { signal }).then((found) => found && { kind: "移動", label: `#${id}`, detail: `${formatAmount(found.amount)} ${found.currency_symbol}`, href: pageUrl("transfer", id) }),
        apiOrNull(`/v1/currencies/${id}`, { signal }).then((found) => found && { kind: "通貨", label: `${found.symbol} (${found.name})`, detail: id, href: pageUrl("currency", id) }),
        accountLookup,
        apiOrNull(`/v1/contracts/${id}`, { signal }).then((found) => found && { kind: "コントラクト", label: `Contract ${id}`, detail: found.status, href: pageUrl("contract", id) }),
      ];
  const settled = await Promise.allSettled(lookups);
  if (signal && signal.aborted) {
    throw new DOMException("The search was aborted.", "AbortError");
  }
  const destinations = settled.filter((result) => result.status === "fulfilled" && result.value).map((result) => result.value);
  const failed = settled.some((result) => result.status === "rejected");
  const navigate = destinations.length === 1 && !failed ? destinations[0].href : null;
  const message = failed ? "検索に失敗しました。時間をおいて再度お試しください。" : `ID ${id} に一致する実行、移動、通貨、アカウント、コントラクトは見つかりません。`;
  return { query, destinations, navigate, failed, message: destinations.length === 0 ? message : undefined };
}

// Render a search for text into a container: navigate directly when one destination is plausible, else list each match as a link.
export async function runSearch(container, text, { signal } = {}) {
  renderLoading(container, "検索中...");
  let result;
  try {
    result = await resolveSearch(text, { signal });
  } catch (error) {
    if (error && error.name === "AbortError") {
      return null;
    }
    renderError(container, error);
    return null;
  }
  if (result.navigate !== null) {
    window.location.replace(result.navigate);
    return result;
  }
  if (result.destinations.length === 0) {
    mount(container, h("div", { class: "state state--empty", role: "status" }, result.message || "見つかりません。"));
    return result;
  }
  mount(
    container,
    h("p", { class: "panel-note" }, `"${result.query.value}" に一致する項目:`),
    h(
      "ul",
      { class: "choices" },
      result.destinations.map((destination) => h("li", null, h("a", { href: destination.href }, h("span", null, h("span", { class: "choice-kind" }, destination.kind), " ", h("span", { class: "id" }, destination.label)), h("span", { class: "choice-detail" }, destination.detail)))),
    ),
    result.failed ? h("p", { class: "form-error" }, "一部の検索に失敗しました。時間をおいて再度お試しください。") : null,
  );
  return result;
}
