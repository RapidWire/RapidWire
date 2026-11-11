// Operator page: lists and downloads backups with an API token held only in this module's memory, checks each download's SHA-256 in the browser, and saves it only when every digest agrees.
import { ApiError, api, apiFetch, dataTable, describeError, executionLink, formatBytes, formatTime, h, isId, mount, renderEmpty, renderLoading } from "/assets/app.js";

const BACKUPS_PER_PAGE = 15;
const TOKEN_PATTERN = /^[\x21-\x7e]{1,200}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
// How long an object URL stays valid after its download was started.
const REVOKE_DELAY_MS = 10000;

let token = null;
// The listing request and the download in progress each have their own controller, so a new listing never cancels a download.
let listController = null;
let downloadController = null;
// True while a download runs; the download buttons and the pager are disabled meanwhile.
let busy = false;
// Incremented whenever the token is forgotten or replaced, so work started under an earlier token is dropped when it finishes.
let session = 0;

function authHeaders() {
  return { Authorization: `Bearer ${token}` };
}

function setState(text) {
  document.getElementById("token-state").textContent = text;
}

function showIdle(text = "トークンを入力すると、バックアップの一覧を表示します。") {
  document.getElementById("backups-summary").textContent = "";
  mount("backups-pager");
  renderEmpty("backups", text);
}

// Cancel the listing request and the download that are still running.
function abortAll() {
  for (const running of [listController, downloadController]) {
    if (running !== null) {
      running.abort();
    }
  }
  listController = null;
  downloadController = null;
}

// Drop the token and everything it fetched, and cancel requests still running.
function forget(reason = "トークンを消去しました。") {
  abortAll();
  session += 1;
  token = null;
  busy = false;
  document.getElementById("token-forget").hidden = true;
  setState(reason);
  showIdle();
}

// Show an API failure: 401 and 403 also drop the token, which cannot list backups.
function showFailure(container, error) {
  let title;
  let detail;
  if (error instanceof ApiError && error.status === 401) {
    forget("トークンが無効か失効しているため消去しました。 / The token is invalid or revoked.");
    title = "認証できません / Unauthorized (401)";
    detail = "トークンが正しくないか、失効しています。Botにメンションして新しいトークンを発行してください。";
  } else if (error instanceof ApiError && error.status === 403) {
    forget("運営者のトークンではないため消去しました。 / The token's account is not an operator.");
    title = "運営者ではありません / Forbidden (403)";
    detail = "このトークンのアカウントは運営者として登録されていません。";
  } else {
    [title, detail] = describeError(error);
  }
  const box = h("div", { class: "state state--error", role: "alert" }, h("p", { class: "state-title" }, title), detail ? h("p", null, detail) : null);
  mount(container, box);
}

async function sha256Hex(blob) {
  if (!globalThis.crypto || !globalThis.crypto.subtle) {
    return null;
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

// Read a response body into a Blob, reporting progress as bytes arrive.
async function readBody(response, onProgress) {
  const type = response.headers.get("Content-Type") || "application/octet-stream";
  if (!response.body || typeof response.body.getReader !== "function") {
    const blob = await response.blob();
    onProgress(blob.size);
    return blob;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    chunks.push(value);
    received += value.byteLength;
    onProgress(received);
  }
  return new Blob(chunks, { type });
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}

// Disable or enable the download buttons and the pager's usable buttons together.
function setButtonsDisabled(disabled) {
  for (const button of document.querySelectorAll("#backups button[data-download], #backups-pager button[data-page]")) {
    button.disabled = disabled;
  }
}

function checkLine(ok, text) {
  return h("li", null, h("span", { class: ok ? "badge badge--success" : "badge badge--failed" }, ok ? "OK" : "NG"), " ", text);
}

async function download(item, statusHost) {
  if (token === null || busy) {
    return;
  }
  busy = true;
  setButtonsDisabled(true);
  const started = session;
  downloadController = new AbortController();
  const signal = downloadController.signal;
  const progress = h("progress", { max: String(Math.max(item.size, 1)), value: "0" });
  const progressText = h("span", { class: "sub" }, "ダウンロード中...");
  mount(statusHost, h("label", { class: "sub" }, progress, " ", progressText));
  try {
    const response = await apiFetch(`/v1/admin/backups/${encodeURIComponent(item.name)}`, { headers: authHeaders(), signal });
    const headerDigest = (response.headers.get("X-Checksum-SHA256") || "").trim().toLowerCase();
    const executionId = response.headers.get("X-Execution-Id");
    const lengthHeader = response.headers.get("Content-Length");
    const total = lengthHeader !== null && /^[0-9]{1,15}$/.test(lengthHeader) ? Number(lengthHeader) : null;
    if (total !== null) {
      progress.max = Math.max(total, 1);
    } else {
      progress.removeAttribute("value");
    }
    const blob = await readBody(response, (received) => {
      if (total !== null) {
        progress.value = received;
      }
      progressText.textContent = total !== null ? `${received.toLocaleString("en-US")} / ${total.toLocaleString("en-US")} bytes` : `${received.toLocaleString("en-US")} bytes`;
    });
    progressText.textContent = "SHA-256を計算中...";
    const computed = await sha256Hex(blob);
    if (session !== started) {
      return;
    }
    const sizeOk = blob.size === item.size && (total === null || total === item.size);
    const headerOk = SHA256_PATTERN.test(headerDigest) && headerDigest === item.sha256;
    const computedOk = computed === null ? null : computed === item.sha256;
    const recorded = isId(executionId) ? h("span", { class: "sub" }, "記録: 実行 ", executionLink(executionId)) : null;
    const checks = h(
      "ul",
      { class: "sub" },
      checkLine(sizeOk, `サイズ ${blob.size.toLocaleString("en-US")} bytes`),
      checkLine(headerOk, "サーバーのSHA-256 (X-Checksum-SHA256) が一覧と一致"),
      computedOk === null ? h("li", null, h("span", { class: "badge badge--reverted" }, "未計算"), " このページはHTTPSでないためブラウザでSHA-256を計算できません") : checkLine(computedOk, "受信したファイルのSHA-256が一覧と一致"),
    );
    if (!sizeOk || !headerOk || computedOk === false) {
      mount(statusHost, h("span", { class: "badge badge--failed", role: "alert" }, "不一致 / Mismatch - 保存しませんでした"), checks, computed ? h("span", { class: "sub mono" }, `受信: ${computed}`) : null, recorded);
      return;
    }
    saveBlob(blob, item.name);
    if (computedOk) {
      mount(statusHost, h("span", { class: "badge badge--success", role: "status" }, "検証済み / Verified - 保存しました"), checks, recorded);
    } else {
      mount(statusHost, h("span", { class: "badge badge--reverted", role: "status" }, "保存しました (未検証 / not verified)"), checks, h("span", { class: "sub" }, "保存したファイルの sha256sum が一覧のSHA-256と一致することを確認してください。"), recorded);
    }
  } catch (error) {
    if (session !== started) {
      return;
    }
    if (error && error.name === "AbortError") {
      mount(statusHost);
      return;
    }
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      showFailure("backups", error);
      return;
    }
    const [title, detail] = describeError(error);
    mount(statusHost, h("div", { class: "state state--error", role: "alert" }, h("p", { class: "state-title" }, title), detail ? h("p", null, detail) : null));
  } finally {
    if (session === started) {
      busy = false;
      downloadController = null;
      setButtonsDisabled(false);
    }
  }
}

function backupColumns() {
  return [
    { label: "ファイル名", render: (item) => h("span", { class: "mono" }, item.name) },
    { label: "作成日時", className: "nowrap", render: (item) => formatTime(item.created_at) },
    { label: "サイズ", className: "num", render: (item) => formatBytes(item.size) },
    { label: "SHA-256", render: (item) => h("span", { class: "mono" }, item.sha256) },
    {
      label: "ダウンロード",
      render: (item) => {
        const status = h("div", { "aria-live": "polite" });
        const button = h("button", { class: "btn", type: "button", "data-download": "", "aria-label": `${item.name} をダウンロード`, onClick: () => download(item, status) }, "ダウンロード");
        return [button, status];
      },
    },
  ];
}

// A pager of buttons for the backup list; the page number stays in this module like the token, so changing pages never navigates, and its usable buttons (data-page) are disabled while a download runs.
function backupPager(body) {
  const last = Math.max(1, Math.ceil(body.total / body.per_page));
  if (body.page === 1 && last === 1) {
    return null;
  }
  const step = (target, text) => {
    if (!(target >= 1 && target <= last && target !== body.page)) {
      return h("button", { class: "btn", type: "button", disabled: true }, text);
    }
    return h("button", { class: "btn", type: "button", "data-page": String(target), disabled: busy, onClick: () => list(target) }, text);
  };
  const status = `${body.page} / ${last} ページ (${body.total.toLocaleString("en-US")} 件)`;
  return h("nav", { class: "pager", "aria-label": "バックアップの一覧のページ" }, step(Math.min(body.page - 1, last), "前へ"), h("span", { class: "pager-status", "aria-live": "polite" }, status), step(body.page + 1, "次へ"));
}

// List one page of backups; while a download runs the listing is left as it is, so the download's row and status stay on screen.
async function list(pageNumber = 1) {
  if (busy) {
    return;
  }
  const container = document.getElementById("backups");
  if (listController !== null) {
    listController.abort();
  }
  listController = new AbortController();
  const signal = listController.signal;
  renderLoading(container);
  mount("backups-pager");
  try {
    const body = await api("/v1/admin/backups", { headers: authHeaders(), signal, query: { page: pageNumber, per_page: BACKUPS_PER_PAGE } });
    const summary = document.getElementById("backups-summary");
    summary.replaceChildren(`${body.total.toLocaleString("en-US")} 件 - この一覧は実行 `, executionLink(body.execution_id), " として記録されました");
    setState("トークンはこのページのメモリにだけ保持しています。");
    mount("backups-pager", backupPager(body));
    if (body.items.length === 0) {
      renderEmpty(container, body.total === 0 ? "バックアップはありません (またはサーバーにバックアップのディレクトリがありません)。" : "このページにバックアップはありません。");
      return;
    }
    mount(container, dataTable({ caption: "バックアップの一覧", columns: backupColumns(), rows: body.items }));
  } catch (error) {
    if (error && error.name === "AbortError") {
      return;
    }
    showFailure(container, error);
  }
}

function setUp() {
  const form = document.getElementById("token-form");
  const input = document.getElementById("token-input");
  const forgetButton = document.getElementById("token-forget");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    input.value = "";
    if (!TOKEN_PATTERN.test(value)) {
      setState("トークンを入力してください (空白を含まない200文字以内)。");
      input.focus();
      return;
    }
    abortAll();
    session += 1;
    busy = false;
    token = value;
    forgetButton.hidden = false;
    list();
  });
  forgetButton.addEventListener("click", () => {
    forget();
    input.focus();
  });
  window.addEventListener("pagehide", () => forget());
  showIdle();
}

setUp();
