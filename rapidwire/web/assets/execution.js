// Execution page: one execution with its error, contract call summary, transfers, nested executions, Discord deliveries and the raw input and output.
import {
  accountFlow,
  accountLink,
  amountView,
  api,
  contractLink,
  currencyLink,
  dataTable,
  executionLink,
  executionTypeLabel,
  formatMinimalUnits,
  formatTime,
  getParam,
  groupInteger,
  h,
  isId,
  kindBadge,
  kvList,
  loadCurrencyIndex,
  mount,
  renderEmpty,
  renderError,
  renderLoading,
  statusBadge,
  transferLink,
} from "/assets/app.js";

// Execution types whose input and output the API withholds.
const PRIVATE_TYPES = Object.freeze(["notification_allow", "notification_deny", "discord_permission_allow", "discord_permission_deny", "token_issue", "token_revoke"]);

const FAILURE_KIND_TEXT = Object.freeze({
  REVERTED: "revert() または assert の失敗",
  RUNTIME: "実行時エラー",
  OUT_OF_GAS: "ガス切れ",
  CALL_FAILED: "呼び出しを開始できませんでした",
  MEMORY_LIMIT: "メモリ上限",
  WALL_CLOCK_LIMIT: "実行時間の上限",
  HOST_TIME_LIMIT: "ホスト処理時間の上限",
  SETTLEMENT: "ガス代を支払えませんでした",
});

const DELIVERY_KIND_TEXT = Object.freeze({ discord_send: "メッセージ送信", discord_add_role: "ロール付与", discord_remove_role: "ロール削除" });

const DELIVERY_REASON_TEXT = Object.freeze({
  permission_revoked: "コントラクトのDiscord操作の許可が取り消されていました",
  channel_not_in_guild: "チャンネルが通貨のサーバーにありません",
  channel_unavailable: "Discordがチャンネルを表示できませんでした",
  message_rejected: "Discordがメッセージを拒否しました",
  role_change_rejected: "Discordがロールの変更を拒否しました",
  retries_exhausted: "再試行の上限に達しました",
  malformed_payload: "配送内容が不正でした",
  internal_error: "送信前に内部エラーが発生しました",
});

const INTEGER = /^-?[0-9]+$/;

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value) {
  return value === null || value === undefined ? "-" : String(value);
}

function section(id, title, ...content) {
  return h("section", { class: "panel", "aria-labelledby": id }, h("div", { class: "panel-head" }, h("h2", { id }, title)), content);
}

// Every currency, deleted ones included, as a map from ID to symbol; empty when the list cannot be read, so links fall back to IDs.
async function loadSymbols() {
  const index = await loadCurrencyIndex();
  return new Map([...index].map(([id, item]) => [id, item.symbol]));
}

// The contract an execution concerns: the API's contract_id (the called contract of a call, the contract of a management operation), else null.
function contractOf(execution) {
  const id = execution.contract_id;
  return typeof id === "string" && isId(id, { positive: true }) ? id : null;
}

function renderHead(execution) {
  document.title = `Execution #${execution.execution_id} - RapidWire`;
  mount("execution-kind", "実行 / Execution ", h("span", { class: "id" }, `#${execution.execution_id}`));
  mount("execution-title", executionTypeLabel(execution.type), " ", h("span", { class: "unit mono" }, execution.type));
  mount("execution-badges", statusBadge(execution.status), PRIVATE_TYPES.includes(execution.type) ? h("span", { class: "badge badge--muted" }, "非公開 / private") : null);
}

function banners(execution) {
  const items = [];
  if (PRIVATE_TYPES.includes(execution.type)) {
    items.push(
      h(
        "div",
        { class: "banner", role: "note" },
        h("p", { class: "banner-title" }, "非公開の実行"),
        h("p", null, "通知の許可、コントラクトのDiscord操作の許可、APIトークンに関する実行は、入力と出力を公開しません。入力は空のオブジェクト、出力は null として表示されます。"),
      ),
    );
  }
  return items;
}

function overview(execution, contractId) {
  const children = Array.isArray(execution.child_execution_ids) ? execution.child_execution_ids : [];
  const transfers = Array.isArray(execution.transfers) ? execution.transfers : [];
  return section(
    "overview-title",
    "概要",
    kvList([
      ["実行ID", h("span", { class: "id" }, execution.execution_id)],
      ["種類", [executionTypeLabel(execution.type), h("span", { class: "sub mono" }, execution.type)]],
      ["状態", statusBadge(execution.status)],
      ["実行者", accountLink(execution.initiator_id, { withName: true })],
      ["日時", formatTime(execution.created_at)],
      ["親の実行", execution.parent_execution_id ? executionLink(execution.parent_execution_id) : "なし"],
      ["冪等キー", execution.has_idempotency_key ? ["あり", h("span", { class: "sub" }, "キーそのものは公開されません。")] : "なし"],
      ["コントラクト", contractId ? contractLink(contractId) : "-"],
      ["コントラクト関数", execution.contract_function ? h("span", { class: "mono" }, execution.contract_function) : "-"],
      ["移動", `${transfers.length} 件`],
      ["子実行", children.length === 0 ? "なし" : h("span", { class: "cluster" }, children.map((child) => executionLink(child)))],
    ]),
  );
}

function errorSection(execution) {
  const output = isObject(execution.output) ? execution.output : null;
  if (execution.status === "success" || output === null || !isObject(output.error)) {
    return null;
  }
  const error = output.error;
  const child = isObject(error.child) ? error.child : null;
  const location = isObject(output.location) ? output.location : null;
  const pairs = [
    ["コード", h("span", { class: "mono" }, text(error.code))],
    ["メッセージ", text(error.message)],
  ];
  if (typeof output.kind === "string") {
    pairs.push(["失敗の種類", [h("span", { class: "mono" }, output.kind), FAILURE_KIND_TEXT[output.kind] ? h("span", { class: "sub" }, FAILURE_KIND_TEXT[output.kind]) : null]]);
  }
  if (typeof output.reason === "string") {
    pairs.push(["理由", h("span", { class: "mono" }, output.reason)]);
  }
  if (output.host_code !== undefined && output.host_code !== null) {
    pairs.push(["組み込み関数のエラーコード", h("span", { class: "mono" }, text(output.host_code))]);
  }
  if (location !== null) {
    pairs.push(["失敗した位置", h("span", { class: "mono" }, `${text(location.function)} @ ${text(location.offset)}`)]);
  }
  if (child !== null) {
    pairs.push(["原因となった子実行", [h("span", null, executionTypeLabel(child.type), " ", h("span", { class: "mono" }, `(${text(child.type)})`)), child.contract_function ? h("span", { class: "sub mono" }, child.contract_function) : null, h("span", { class: "sub mono" }, `${text(child.code)}: ${text(child.message)}`)]]);
  }
  return section("error-title", execution.status === "reverted" ? "リバートの内容" : "エラーの内容", h("div", { class: "state state--error", role: "note" }, h("p", { class: "state-title" }, text(error.message)), h("p", { class: "mono" }, text(error.code))), kvList(pairs));
}

function typedValue(value, symbols) {
  if (!isObject(value)) {
    return h("span", { class: "mono" }, JSON.stringify(value));
  }
  const tag = h("span", { class: "type-tag" }, text(value.type));
  if (value.type === "Account" && typeof value.value === "string") {
    return [tag, accountLink(value.value, { withName: true })];
  }
  if (value.type === "Currency" && typeof value.value === "string") {
    return [tag, currencyLink(value.value, symbols.get(value.value) || null)];
  }
  if (value.type === "str") {
    return [tag, h("span", { class: "mono" }, JSON.stringify(value.value))];
  }
  return [tag, h("span", { class: "mono" }, typeof value.value === "string" ? value.value : JSON.stringify(value.value))];
}

function resultValue(output) {
  if (!Object.hasOwn(output, "value")) {
    return null;
  }
  const shown = output.value_type === "str" ? JSON.stringify(output.value) : text(output.value);
  return [h("span", { class: "type-tag" }, text(output.value_type)), " ", h("span", { class: "mono" }, shown)];
}

// The gas cost in minimal units of the gas currency, gas_used times gas_price, computed with BigInt; null when either is not an integer string.
function gasCost(output) {
  if (typeof output.gas_used !== "string" || typeof output.gas_price !== "string" || !INTEGER.test(output.gas_used) || !INTEGER.test(output.gas_price)) {
    return null;
  }
  return (BigInt(output.gas_used) * BigInt(output.gas_price)).toString();
}

function drawList(values) {
  return h("ol", { class: "value-list" }, values.map((value) => h("li", null, h("span", { class: "mono" }, text(value)))));
}

const DRAW_COLUMNS = [
  { label: "順番", className: "num", render: (row) => text(row.order) },
  { label: "深さ", className: "num", render: (row) => text(row.depth) },
  { label: "コントラクト", render: (row) => (typeof row.contract_id === "string" && isId(row.contract_id, { positive: true }) ? contractLink(row.contract_id) : text(row.contract_id)) },
  { label: "関数", render: (row) => h("span", { class: "mono" }, text(row.function)) },
  { label: "値", render: (row) => (Array.isArray(row.values) && row.values.length > 0 ? drawList(row.values) : "-") },
];

function contractSection(execution, contractId, symbols) {
  if (execution.type !== "contract_call" || !isObject(execution.input) || typeof execution.input.function !== "string") {
    return null;
  }
  const input = execution.input;
  const output = isObject(execution.output) ? execution.output : {};
  const args = Array.isArray(input.args) ? input.args : [];
  const attach = isObject(input.attach) ? input.attach : null;
  const gasCurrency = typeof output.gas_currency_id === "string" ? output.gas_currency_id : null;
  const gasSymbol = gasCurrency ? symbols.get(gasCurrency) || null : null;
  const cost = gasCost(output);
  const pairs = [
    ["コントラクト", contractId ? contractLink(contractId) : text(input.contract_id)],
    ["関数", h("span", { class: "mono" }, input.function)],
    ["引数", args.length === 0 ? "なし" : h("ol", { class: "value-list" }, args.map((arg) => h("li", null, typedValue(arg, symbols))))],
    ["添付", attach ? [amountView(formatMinimalUnits(text(attach.amount))), " ", currencyLink(text(attach.currency_id), symbols.get(attach.currency_id) || null)] : "なし"],
  ];
  if (execution.status === "success") {
    pairs.push(["戻り値", resultValue(output) || "-"]);
  }
  pairs.push(
    ["ガス使用量", output.gas_used !== undefined ? [h("span", { class: "mono" }, `${groupInteger(output.gas_used)} / ${groupInteger(output.gas_limit)}`), h("span", { class: "sub" }, "使用量 / 上限")] : "-"],
    ["ガス価格", output.gas_price !== undefined ? [h("span", { class: "mono" }, groupInteger(output.gas_price)), h("span", { class: "sub" }, "ガス1あたりのガス通貨の最小単位 (0.001)")] : "-"],
    ["ガス通貨", gasCurrency ? currencyLink(gasCurrency, gasSymbol) : "-"],
    ["ガス代", cost !== null ? [amountView(formatMinimalUnits(cost), gasSymbol), h("span", { class: "sub" }, "使用量 x 価格。焼却された額です。")] : "-"],
  );
  const random = Array.isArray(output.random) ? output.random : [];
  const tree = Array.isArray(output.random_tree) ? output.random_tree.filter(isObject) : [];
  pairs.push(["乱数 (random)", random.length === 0 ? "なし" : [drawList(random), h("span", { class: "sub" }, "この実行自身が random() で引いた値です。")]]);
  return section(
    "contract-title",
    "コントラクト呼び出し",
    kvList(pairs),
    tree.length > 0
      ? h(
          "div",
          { class: "panel-section" },
          h("h3", null, "呼び出し全体の乱数 (random_tree)"),
          h("p", { class: "panel-note" }, "失敗した呼び出しでは子実行の記録が取り消されるため、呼び出しの各実行が引いた値を開始順にここへ残しています。"),
          dataTable({ caption: "呼び出し全体の乱数", columns: DRAW_COLUMNS, rows: tree }),
        )
      : null,
  );
}

function transferColumns(symbols) {
  return [
    { label: "移動", className: "nowrap", render: (row) => transferLink(row.transfer_id) },
    { label: "送金元 -> 送金先", render: (row) => accountFlow(row.from_account, row.to_account, { withName: true }) },
    { label: "金額", className: "num", render: (row) => [amountView(row.amount), " ", currencyLink(row.currency_id, symbols.get(row.currency_id) || null)] },
    { label: "区分", render: (row) => kindBadge(row.kind) },
    { label: "時刻", className: "nowrap", render: (row) => formatTime(row.created_at) },
  ];
}

function transfersSection(execution, symbols) {
  const transfers = Array.isArray(execution.transfers) ? execution.transfers : [];
  const host = h("div", null);
  if (transfers.length === 0) {
    renderEmpty(host, "この実行の移動はありません。");
  } else {
    mount(host, dataTable({ caption: "この実行の移動", columns: transferColumns(symbols), rows: transfers }));
  }
  return section("transfers-title", "移動", h("p", { class: "panel-note" }, "この実行自身の移動です。子実行の移動はそれぞれの実行に記録されます。"), host);
}

const CHILD_COLUMNS = [
  { label: "実行", className: "nowrap", render: (row) => executionLink(row.execution_id) },
  { label: "種類", render: (row) => [executionTypeLabel(row.type), h("span", { class: "sub mono" }, row.type)] },
  { label: "状態", render: (row) => statusBadge(row.status) },
  { label: "関数", render: (row) => (row.contract_function ? h("span", { class: "mono" }, row.contract_function) : "-") },
  { label: "移動", className: "num", render: (row) => `${Array.isArray(row.transfers) ? row.transfers.length : 0} 件` },
  { label: "配送", className: "num", render: (row) => `${Array.isArray(row.deliveries) ? row.deliveries.length : 0} 件` },
  { label: "その子実行", className: "num", render: (row) => `${Array.isArray(row.child_execution_ids) ? row.child_execution_ids.length : 0} 件` },
];

function childrenSection(execution) {
  const children = Array.isArray(execution.children) ? execution.children : [];
  const ids = Array.isArray(execution.child_execution_ids) ? execution.child_execution_ids : [];
  const host = h("div", null);
  if (children.length === 0) {
    renderEmpty(host, "子実行はありません。");
  } else {
    mount(host, dataTable({ caption: "直下の子実行", columns: CHILD_COLUMNS, rows: children }));
  }
  const note = ids.length === 0 ? "この実行の中で開始された実行 (コントラクトからの呼び出しなど) を表示します。" : ["直下の子実行ID (child_execution_ids): ", ids.map((id, index) => [index > 0 ? ", " : null, executionLink(id)]), "。さらに深い実行はそれぞれのページで確認できます。"];
  return section("children-title", "子実行", h("p", { class: "panel-note" }, note), host);
}

function deliveryStatus(status) {
  switch (status) {
    case "sent":
      return h("span", { class: "badge badge--success" }, "送信済み / sent");
    case "failed":
      return h("span", { class: "badge badge--failed" }, "失敗 / failed");
    case "pending":
      return h("span", { class: "badge badge--warn" }, "待機中 / pending");
    default:
      return h("span", { class: "badge badge--muted" }, text(status));
  }
}

function targetLine(label, id) {
  return id === null || id === undefined ? null : h("span", { class: "sub" }, `${label} `, h("span", { class: "id" }, text(id)));
}

const DELIVERY_COLUMNS = [
  { label: "種類", render: (row) => [DELIVERY_KIND_TEXT[row.kind] || text(row.kind), h("span", { class: "sub mono" }, text(row.kind)), h("span", { class: "sub" }, "outbox ", h("span", { class: "id" }, text(row.outbox_id)))] },
  { label: "対象", render: (row) => [targetLine("サーバー", row.guild_id), targetLine("チャンネル", row.channel_id), targetLine("ユーザー", row.user_id), targetLine("ロール", row.role_id)] },
  { label: "状態", render: (row) => [deliveryStatus(row.status), h("span", { class: "sub" }, `試行 ${text(row.attempts)} 回`)] },
  {
    label: "日時",
    className: "nowrap",
    render: (row) => [h("span", null, "予約 ", formatTime(row.created_at)), row.next_attempt_at ? h("span", { class: "sub" }, "次の試行 ", formatTime(row.next_attempt_at)) : null, row.completed_at ? h("span", { class: "sub" }, "完了 ", formatTime(row.completed_at)) : null],
  },
  { label: "メッセージID", render: (row) => (row.message_id ? h("span", { class: "id" }, row.message_id) : "-") },
  {
    label: "エラー",
    render: (row) => {
      if (!isObject(row.error)) {
        return "-";
      }
      const error = row.error;
      return [
        h("span", { class: "mono" }, text(error.reason)),
        DELIVERY_REASON_TEXT[error.reason] ? h("span", { class: "sub" }, DELIVERY_REASON_TEXT[error.reason]) : null,
        error.http_status !== null && error.http_status !== undefined ? h("span", { class: "sub" }, `HTTP ${text(error.http_status)}`, error.code ? ` / code ${text(error.code)}` : null) : null,
        error.message ? h("span", { class: "sub" }, text(error.message)) : null,
      ];
    },
  },
];

function deliveriesSection(execution) {
  const deliveries = Array.isArray(execution.deliveries) ? execution.deliveries : [];
  const host = h("div", null);
  if (deliveries.length === 0) {
    renderEmpty(host, "この実行が予約したDiscordへの配送はありません。");
  } else {
    mount(host, dataTable({ caption: "Discordへの配送", columns: DELIVERY_COLUMNS, rows: deliveries }));
  }
  return section("deliveries-title", "Discordへの配送", h("p", { class: "panel-note" }, "コントラクトが discord.send、discord.add_role、discord.remove_role で予約し、実行の確定後にworkerが行う操作です。配送の失敗は実行や残高を変えません。メッセージの本文は公開されません。"), host);
}

function rawSection(execution) {
  const block = (title, id, value) => [h("h3", { id }, title), h("pre", { class: "code-block", "aria-labelledby": id }, JSON.stringify(value, null, 2) ?? "null")];
  return section(
    "raw-title",
    "入力と出力",
    h("p", { class: "panel-note" }, "記録されたままの内容です。金額を含むすべての整数は最小単位の10進文字列で、1単位 = 1000最小単位です (\"12500\" は 12.500)。"),
    h("div", { class: "panel-section" }, block("入力 (input)", "raw-input-title", execution.input)),
    h("div", { class: "panel-section" }, block("出力 (output)", "raw-output-title", execution.output)),
  );
}

async function start() {
  const raw = new URLSearchParams(window.location.search).get("id");
  const id = getParam("id", "positiveId");
  const errorHost = document.getElementById("execution-error");
  if (id === null) {
    renderError(errorHost, raw === null || raw === "" ? "実行IDが指定されていません。URLに ?id=<実行ID> を付けてください。" : "実行IDが正しくありません。IDは正の数字で指定してください。");
    return;
  }
  document.title = `Execution #${id} - RapidWire`;
  mount("execution-title", "実行 ", h("span", { class: "id" }, `#${id}`));
  renderLoading(errorHost);
  let execution;
  let symbols;
  try {
    [execution, symbols] = await Promise.all([api(`/v1/executions/${id}`), loadSymbols()]);
  } catch (error) {
    renderError(errorHost, error);
    return;
  }
  mount(errorHost);
  const contractId = contractOf(execution);
  renderHead(execution);
  mount("execution-body", banners(execution), overview(execution, contractId), errorSection(execution), contractSection(execution, contractId, symbols), transfersSection(execution, symbols), childrenSection(execution), deliveriesSection(execution), rawSection(execution));
}

start();
