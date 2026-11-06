# RapidWire HTTP API

## 概要

API は RapidWire の台帳、DEX、コントラクトを HTTP で提供し、同じプロセスから静的な Web エクスプローラーも配信する。
API のすべてのエンドポイントはベースパス `/v1` で始まる。
OpenAPI の文書は `/openapi.json` で、対話的なドキュメントは `/docs` で公開され、それ以外のすべてのパスは Web エクスプローラー ([web.md](web.md)) に属する。
リクエストとレスポンスの本文は、エンドポイントが別に定めない限り JSON (`Content-Type: application/json`) である。

リクエストの本文の大きさの上限は、有効なコントラクト呼び出しのうち最大のものに等しい: 全体を `\uXXXX` のエスケープの対で書いた 4,096 文字の `str` の引数 16 個と、同じくエスケープした 64 文字の関数名に、他のフィールドの余地を加えたものである。
`GET /v1/config` はこの上限をバイト単位で `max_body_bytes` として報告する。
コントラクトの登録には独自の上限があり、その節で述べる。
これより大きな本文にはステータス 413、コード `payload_too_large`、`Connection: close` で応答する: 何かを読む前に `Content-Length` がそう示している場合はその時点で、そうでなければ受信したバイト数が上限を超えた時点で応答するため、大きすぎる本文が解析されることはなく、そのトークンが確認されることもない。

## 認証

残高、移動、実行などの台帳データの読み取りは公開されており、トークンを必要としない。
書き込み、コントラクトのドライラン、呼び出し元の非公開の一覧、運営者用のエンドポイントには API トークンが必要である。
API トークンは Discord の Bot がメンションされたときに発行するもので、クライアントは `Authorization: Bearer <token>` として送る。
トークンの権限は 1 段階だけである: その勘定が行えるすべての書き込みを、常にその勘定として行える。
新しいトークンを発行するとその勘定の以前のトークンは取り消され、持ち主はいつでもトークンを取り消せる。
トークンが無い、形式が不正、未知、または取り消し済みの場合は、ステータス 401、エラーコード `unauthorized`、ヘッダー `WWW-Authenticate: Bearer` で応答する。
書き込みは自身のデータベーストランザクションの中でトークンの行を保持したままトークンを再度確認するため、書き込みが記録される前にコミットされた取り消しは書き込みを 401 で失敗させて何も記録させず、後から始まった取り消しは書き込みの完了を待つ。
書き込みエンドポイントが実行するすべてのデータベーストランザクションは、デッドロック後の各再試行も含め、この確認を行う。
トークンを使うとその `last_used_at` が最大で 1 分に 1 回更新される。
書き込みがトークンの行を保持している間はこの更新を省くため、更新がリクエストを遅らせたり失敗させたりすることはない。

## 冪等性

すべての書き込みは、任意の `Idempotency-Key` ヘッダーを受け付ける。
値は先頭と末尾に空白のない、表示可能な ASCII 文字 1 から 255 文字であり、それ以外の値は 422 の検証エラーとなる。
キーは書き込みを行う勘定ごとの範囲を持つため、2 つの勘定が同じキーを使っても互いに影響しない。
その勘定が既に使ったキーで書き込みを繰り返すと、操作を再実行せずに最初の結果を返す: 同じステータスと同じ JSON 本文に、レスポンスヘッダー `Idempotency-Replayed: true` を加える。
最初のリクエストが失敗し、その失敗が実行として記録された場合、繰り返しは `execution_id` を含む同じエラーの封筒を `Idempotency-Replayed: true` とともに返す。
別のリクエスト (別の操作または別の引数) にキーを再利用すると、ステータス 422、コード `idempotency_conflict` で失敗する。
キーのないリクエストは送るたびに実行される。
繰り返しも最初に認証されるため、現時点で有効なトークンが必要である: `POST /v1/tokens/revoke` の繰り返しを、そのリクエストが取り消したトークンで送ると 401 が返り、同じ勘定のより新しいトークンで送ると最初の結果が返る。

## 数値の形式

通貨の金額は、最小の金額が 0.001 であるため、通貨単位で小数 3 桁までの JSON 文字列である。
リクエストの金額は `^(0|[1-9][0-9]*)(\.[0-9]{1,3})?$` に一致しなければならない: 符号、指数、空白、桁区切りを含まず、小数点の前の単一の `0` を除いて先頭のゼロを含まない (`"0.5"` は有効であり、`"00.5"`、`"01"`、`".5"`、`"1."` は無効である)。
金額のフィールドに JSON の数値を入れたものは拒否され、小数 4 桁以上の文字列も同様であるため、金額が丸められることはない。
金額のフィールドは、別に定めない限り正の値である。
一部 (承認、最小出力) はゼロも受け付けるが、負の値を受け付けるものはない。
最大の金額は 10^38 - 1 最小単位である。
レスポンスの金額は常に小数 3 桁ちょうどの正規の文字列であり、`"12.500"` や `"-3.000"` のようになる (発行勘定は負の残高を持つ)。

ID (勘定、通貨、実行、移動、請求、プール、トークン、ロール、チャンネル) と、JavaScript が正確に表せる範囲を超えうるその他の整数 (プールのシェア、百万分率の利率、ガス) は、リクエストでもレスポンスでも `^(0|-?[1-9][0-9]*)$` に一致する 10 進数字の JSON 文字列である。
このようなフィールドに JSON の数値を入れたものは拒否される。
システム勘定は負の ID を持つ。
小さな上限のあるカウンタ (`page`、`per_page`、`total`、コントラクトのバージョン番号、`fee_bps`、バイト単位のサイズ) は JSON の数値である。

実行の生の `input` と `output` オブジェクトは保存されたとおりに返され、その中では金額を含むすべての整数が最小単位の 10 進文字列である (12.5 は `"12500"`)。

## エラー

検証エラー、`/v1` の未知のパス、誤ったメソッド、認証の失敗、内部エラーを含め、API のすべてのエラーレスポンスは同じ封筒を持つ。
`/v1`、`/docs`、`/openapi.json` の外の未知のパスには、代わりに Web エクスプローラーが HTML の 404 ページで応答する ([web.md](web.md#ページ)):

```json
{"error": {"code": "insufficient_balance", "message": "The balance is insufficient.", "execution_id": "123"}}
```

- `code` は安定した snake_case のコードであり、`message` はユーザーに表示して問題のない英語のテキストである。
- `execution_id` は、失敗が失敗した (または取り消された) 実行として記録された場合に存在し、その実行は他の実行と同様に読み取れる。
- 失敗したコントラクトの実行は、さらに `kind` (REVERTED、RUNTIME、OUT_OF_GAS、CALL_FAILED、MEMORY_LIMIT、WALL_CLOCK_LIMIT、HOST_TIME_LIMIT または SETTLEMENT) と、CALL_FAILED の場合は `reason` を持つ。
- 入れ子の実行が原因の失敗は、その実行の `type`、`contract_function`、`code`、`message` を持つ `child` を含む。
- リクエストの検証エラーは、コード `validation_error`、ステータス 422 と `details` を持つ。`details` は不正な各フィールドを示す `{"loc", "message", "type"}` の項目のリストである (例 `["body", "amount"]`)。送信された値がそのまま返されることはなく、クライアントが選んだ名前も返されない: 未知のフィールドは `"<extra>"` (type `extra_forbidden`) として、マッピングのキーは `"<key>"` として現れる。

操作に由来しないエラー:

| ステータス | コード | 発生条件 |
| --- | --- | --- |
| 400 | `bad_request` | 本文が、サーバーが値に解析できない JSON である (パーサーが許すより深く入れ子になったものなど) |
| 404 | `not_found` | `/v1` の下の未知のパス (それ以外の未知のパスには Web エクスプローラーの HTML の 404 ページが返る) |
| 405 | `method_not_allowed` | 既知のパスに誤ったメソッドを使った (`Allow` ヘッダー付き) |
| 413 | `payload_too_large` | 本文がエンドポイントの受け付けるより大きい (概要を参照) |
| 422 | `validation_error` | スキーマまたは数値の形式に反するリクエスト、または不正な形式の JSON |
| 500 | `internal_error` | 予期しないサーバーの障害 (メッセージは `Internal server error.` で、それ以上の詳細はない) |
| 503 | `discord_unavailable` | `GET /v1/users/{account_id}/name` で Discord が応答できず、名前もキャッシュされていない (`Retry-After` が付くことがある) |

ドメインエラーはクラスごとにステータスへ対応し、専用の項目を持たないクラスは、一覧にある最も近い祖先のステータスをとる。
規則は次のとおりである: 指定した項目が存在しなければ 404、呼び出し元がその操作を行えなければ 403、有効なトークンがなければ 401、リクエストの形式は正しいが現在の状態がそれを拒むなら 409、リクエスト自体が不正であるか、実行した操作がそれ自身の条件で失敗した (コントラクトの実行) なら 422 である。

| エラークラス | ステータス | コード |
| --- | --- | --- |
| `NotFoundError` | 404 | `not_found`, `currency_not_found`, `claim_not_found`, `account_not_found`, `pool_not_found`, `contract_not_found`, `user_not_found`, `stake_not_found`, `execution_not_found`, `transfer_not_found` |
| `Unauthorized` | 401 | `unauthorized` |
| `PermissionDenied` | 403 | `permission_denied` |
| `GuildUnavailable` | 409 | `guild_unavailable` (Bot がその通貨のサーバーを見られないため、その所有者と管理者が不明である) |
| `InsufficientBalance` | 409 | `insufficient_balance` |
| `InsufficientAllowance` | 409 | `insufficient_allowance` |
| `InsufficientStake` | 409 | `insufficient_stake` |
| `InsufficientShares` | 409 | `insufficient_shares` |
| `InsufficientLiquidity` | 409 | `insufficient_liquidity` |
| `SlippageExceeded` | 409 | `slippage_exceeded` |
| `NoRoute` | 409 | `no_route` |
| `TimelockActive` | 409 | `timelock_active` |
| `RequestExpired` | 409 | `request_expired` |
| `NoPendingRequest` | 409 | `no_pending_request` |
| `InvalidState` | 409 | `invalid_state` |
| `CurrencyRenounced` | 409 | `currency_renounced` |
| `ContractLocked` | 409 | `contract_locked` |
| `ContractPaused` | 409 | `contract_paused` |
| `ContractStorageLimit` | 409 | `contract_storage_limit` |
| `AlreadyExists` | 409 | `already_exists`, `pool_already_exists` |
| `IdempotencyConflict` | 422 | `idempotency_conflict` |
| `InvalidAmount` | 422 | `invalid_amount` |
| `InvalidArgument` | 422 | `invalid_argument` |
| `InvalidContract` | 422 | `invalid_contract` |
| `AmountOverflow` | 422 | `amount_overflow` |
| `RevertError` | 422 | `reverted` |
| `ContractExecutionError` | 422 | `contract_reverted`, `contract_runtime_error`, `contract_out_of_gas`, `contract_call_failed`, `contract_limit_exceeded` |
| `DomainError` | 422 | `domain_error` とその他すべてのコード |

## ページ分割

ページ分割された一覧は 1 つのページオブジェクトで応答する:

```json
{"items": [], "page": 1, "per_page": 15, "total": 0, "total_is_lower_bound": false}
```

- クエリパラメータは `page` (1 から) と `per_page` (1 から一覧の最大値まで) である。一覧ごとに既定値と最大値があり、`GET /v1/config` が `paging.limits` の下で報告する。
- 一覧の先頭 10,000 件にしか到達できないため、`(page - 1) * per_page + per_page` は 10,000 を超えてはならない。これを超えるリクエストは、他の不正なページ分割の値と同様に 422 の検証エラーとなる。
- `total` は `total_is_lower_bound` が `false` の間は正確である。データベースから数える一覧は最大 10,000 件まで数え、それより多く一致する場合は `total` が 10,000、`total_is_lower_bound` が `true` になる。
- バックアップの一覧は代わりにバックアップのディレクトリのファイルを数えるため、その `total` は 10,000 を超えても常に正確であり、その `total_is_lower_bound` は常に `false` である。

## サービス情報

`GET /v1/version` は API の名前とサーバーのバージョンを返す:

```json
{"name": "RapidWire API", "version": "<server version>"}
```

`GET /v1/config` は、クライアントが必要とする上限と設定を、動作中のサーバーの構成から取って返す:

- `amounts`: `decimal_places` (3)、`minimal_unit` (`"0.001"`)、`max_amount` (金額の文字列)。
- `currency`: 通貨の名前とシンボルの最大長、および請求の説明の最大長 (文字数)。
- `staking`: `rate_ppm_max` (1 時間あたりの利率の最大値を百万分率で表した文字列)、`rate_percent_per_hour_max` (同じ値を小数 4 桁の百分率で表したもの、`"100.0000"`)、`rate_change_timelock_seconds`、`delete_timelock_seconds` (これ以降に削除を確定できる)、`delete_expiry_seconds` (このとき要請が失効する)。
- `swap`: `fee_tiers_bps`、`fee_denominator`、`minimum_liquidity` (プールの作成時にロックされるシェア、文字列)、`chunk_count`、`max_routes`。
- `gas`: `currency_id`、`price` (ガス 1 あたりのガス代の通貨の最小単位)、`default_gas`、`max_gas` で、すべて文字列。
- `contract`: `max_size` (バイナリのバイト数)、`max_call_depth`、`storage_max_keys`、`storage_max_bytes`、`storage_key_max_length`、`storage_value_max_bytes`。
- `paging`: `max_window` と `limits` (各一覧のページサイズの `default` と `max`)。
- `idempotency_key_max_length`。
- `max_body_bytes`: その節が別の上限を定めない限り、エンドポイントが受け付けるリクエストの本文の最大の大きさ。

## 勘定と履歴

この節のすべてのエンドポイントは公開の読み取りである: トークンを必要とせず、`Authorization` ヘッダーを無視する。
すべての勘定の残高、ステーク、請求、許可額、移動、実行は公開されている。
システム勘定 (発行、ステーク保管、プール保管) は負の ID を持ち、ユーザー勘定と同様に読み取れる。
パスとクエリの ID は 10 進文字列である。
形式が不正または範囲外の ID (例えば `abc`、`01`、`1.5`、符号付き 64 ビットの範囲外の値) は 422 の検証エラーとなり、通貨、請求、実行、移動の ID は正でなければならない。
レスポンスの時刻は、`Z` の接尾辞を付けた UTC の ISO 8601 文字列である。
旧版から取り込んだ実行は、すべての一覧と照会に新版の実行と並んで現れる。
その `origin` は `legacy` であり、`label_basis` はその種類がどう決まったか (`row_shape`、`recalculation`、`binlog`、`owner_decision` または `state_import`) を示す。
新版の実行の `origin` は `native`、`label_basis` は `null` である。

### 勘定

`GET /v1/accounts/{account_id}` は、その勘定が何であるかを示す `{"account_id", "kind", "currency_id", "pool_id", "created_at"}` を返す。
`kind` は `user`、`issuance` (負の残高が通貨の供給量となる勘定で、`currency_id` が設定される)、`stake_custody` (すべてのステークを保持する勘定 `-1`)、`pool_custody` (DEX のプールの準備金を保持する勘定で、`pool_id` が設定される) のいずれかである。
`currency_id` と `pool_id` は該当しない場合 `null` である。
存在しない勘定は 404 `account_not_found` である。
ユーザー勘定は最初の台帳操作で作られるため、勘定を持たない有効な Discord のユーザー ID も 404 となる。
一方、この節の他の読み取りは、そのような ID に空の一覧とゼロの残高で応答する。

### ユーザー

`GET /v1/users/{account_id}/name` は `{"account_id", "username"}` を返す。
これは Discord の REST API から得た、そのユーザーの Discord のユーザー名 (表示名ではない) である。
`account_id` は正のユーザー ID でなければならず、これは Discord に問い合わせる前に確認される。
API の各プロセスは最近使われた 100 件の名前を、それぞれ取得から 1 日の間キャッシュする。
それより古い名前は再び取得し、そのとき Discord が応答できないか、もうそのユーザーを見つけられない場合は古い名前を返す。
キャッシュされていない名前への同時のリクエストは Discord への 1 つのリクエストを共有し、各プロセスが Discord へ同時に送るリクエストは最大 4 件である。
Discord が知らず、名前もキャッシュされていないユーザーは 404 `user_not_found` である。
この応答は 5 分間記憶され、その間そのユーザーについて Discord に再び問い合わせることはない。
Discord が応答できず (到達できない、エラーを返す、またはサーバーに Bot のトークンがない)、名前もキャッシュされていない場合、応答は 503 `discord_unavailable` で、メッセージは `Discord is unavailable, so the username cannot be looked up now; try again later.` である。
Discord が 429 を返した場合、そのリクエストは再試行しない。
プロセスは Discord が示した待ち時間 (最大 1 時間) の間 Discord への問い合わせをやめ、その間の 503 は秒単位の `Retry-After` ヘッダーを持つ。
Discord が 401 または 403 を返した場合は Bot のトークンが拒否されているため、プロセスは同じ方法で 1 回につき 10 分間 Discord への問い合わせをやめ、照会が再び成功するまでその拒否を一度だけログに記録する。
プロセスの停止時にまだ Discord を待っている照会は、キャッシュされた名前または 503 で応答する。

`GET /v1/users/{account_id}/stats` は任意の勘定の移動の統計 `{"account_id", "total_transfers", "first_transfer_at", "last_transfer_at"}` を返す。
`total_transfers` は、その勘定がどちらか一方の側にある移動の正確な件数を示す JSON の数値であり、移動のない勘定では 2 つの時刻が `null` になる。

### 残高とステーク

`GET /v1/accounts/{account_id}/balances` は、その勘定のゼロでない残高を通貨 ID の昇順に並べたページ分割された一覧 (既定 15 件、1 ページ最大 100 件) であり、各項目は `{"currency_id", "symbol", "name", "amount"}` である。
発行勘定の残高は供給量の符号を反転したものであるため、その金額は負になる。

`GET /v1/accounts/{account_id}/balances/{currency_id}` は 1 つの通貨について `{"account_id", "currency_id", "symbol", "name", "amount", "currency_deleted"}` を返す。
削除された通貨も含み、勘定がその通貨を保有していない場合の金額は `"0.000"` である。
存在しない通貨は 404 `currency_not_found` である。

`GET /v1/accounts/{account_id}/stakes` は、その勘定のステークを通貨 ID の昇順に並べたページ分割された一覧 (既定 15 件、1 ページ最大 100 件) であり、`GET /v1/accounts/{account_id}/stakes/{currency_id}` は 1 つのステークまたは 404 `stake_not_found` を返す。
各ステークは `{"account_id", "currency_id", "symbol", "name", "amount", "last_accrued_at", "hourly_rate_ppm", "interest_halted", "projected_amount", "projected_at"}` である。
`amount` は `last_accrued_at` の最後の計上時点で保存された金額であり、`projected_amount` は `projected_at` (リクエストの時刻) に計上した場合の金額である。
これは何も書き込まずに計算され、計上と同じく通貨の供給量の余裕で頭打ちになる。
`hourly_rate_ppm` は通貨の現在の 1 時間あたりの利率を百万分率で表した文字列である。
`interest_halted` が `true` の場合、その通貨はもう利息を支払わず、`projected_amount` は `amount` に等しい。

### 移動の履歴

`GET /v1/history` はすべての移動を検索し、移動の項目のページ分割された一覧 (既定 10 件、1 ページ最大 20 件) で応答する。
指定したすべての絞り込みが一致しなければならない:

- `transfer_id`: その移動だけ。
- `user_id`: その勘定がどちらか一方の側にある移動。`source_id` と `destination_id`: その勘定からの移動、その勘定への移動。
- `currency_id`: その通貨の移動。
- `start` と `end`: UTC からのオフセット (`Z` または `+09:00`) を持つ ISO 8601 の時刻。`start` はその時刻を含み `end` は含まないため、日付の範囲は最終日の翌日の午前 0 時で終わる。オフセットのない時刻は 422 の検証エラーとなる。
- `min_amount` と `max_amount`: リクエストの形式の金額文字列 (`"1.5"`) で表した、両端を含む境界。負の値、指数、空白、小数 4 桁以上は 422 の検証エラーとなる。
- `execution_type`: その種類 (`transfer`、`mint`、`swap`、`contract_call`、`interest` など) の実行が行った移動。
- `contract_function`: そのコントラクト関数の呼び出しが行った移動。
- `sort_by`: `transfer_id` (既定)、`timestamp` または `amount`、`sort_order`: `desc` (既定) または `asc`。時刻や金額が等しい場合は、同じ向きの移動 ID 順に並ぶ。

旧版の `/transfers/search` の絞り込みはこれらの名前に対応する: `source_id`、`user_id`、`currency_id`、`min_amount`、`max_amount`、`sort_by`、`sort_order` は名前がそのままで、`dest_id` は `destination_id`、`start_timestamp` と `end_timestamp` は `start` と `end`、`limit` は `per_page` になり、`input_data` は `execution_type` と `contract_function` に置き換わる。

`GET /v1/accounts/{account_id}/history` は、その勘定がどちらか一方の側にある移動に固定した同じ検索である。
`currency_id`、`start`、`end`、`min_amount`、`max_amount`、`execution_type`、`contract_function`、`sort_by`、`sort_order` とページ分割のパラメータを受け取る。

移動の項目は `{"transfer_id", "execution_id", "currency_id", "currency_symbol", "from_account", "to_account", "amount", "kind", "created_at", "execution_type", "execution_status", "origin", "label_basis", "initiator_id", "parent_execution_id", "parent_origin", "parent_label_basis", "contract_function"}` である。
`kind` は `principal`、`interest` または `gas` であり、`currency_symbol` は通貨の現在のシンボルである。
`execution_*`、`origin`、`label_basis`、`initiator_id`、`parent_execution_id`、`contract_function` の各フィールドは、その移動を行った実行を表す。
`parent_origin` と `parent_label_basis` はその親の実行の `origin` と `label_basis` であり、親がなければどちらも `null` である。

`GET /v1/transfers/{transfer_id}` は 1 件の移動の項目、または 404 `transfer_not_found` を返す。

### 実行

`GET /v1/executions` は、失敗したものや移動のないものを含めて、実行を新しい順 (実行 ID の降順) に並べたページ分割された一覧 (既定 10 件、1 ページ最大 20 件) を返す。
絞り込み `initiator_id`、`execution_type`、`status` (`success`、`failed` または `reverted`)、`contract_id` はすべて一致しなければならない。
`contract_id` は、`contract_id` がその勘定である実行を選ぶ: 他のコントラクトからの入れ子の呼び出しと失敗した呼び出しを含むそのコントラクトのすべての呼び出しと、その `contract_update`、`contract_lock`、`contract_pause`、`contract_resume`、`contract_delete` の実行である。
`execution_type=contract_call` と組み合わせると呼び出しだけを一覧にする。
実行は `{"execution_id", "type", "origin", "label_basis", "initiator_id", "parent_execution_id", "parent_origin", "parent_label_basis", "contract_id", "status", "input", "output", "has_idempotency_key", "contract_function", "created_at"}` である。
`contract_id` は、`contract_call` では呼び出されたコントラクト (`input.contract_id`)、コントラクトの管理の実行ではそのコントラクトの勘定 (その操作者) であり、それ以外のすべての種類では `null` である。
`parent_origin` と `parent_label_basis` は親の実行を表し、親がなければどちらも `null` である。
`input` と `output` は保存されたオブジェクトであり、その中では金額を含むすべての整数が、他のフィールドの金額の形式ではなく最小単位の 10 進文字列である (`"12500"` は 12.5)。
`output` は、成功した実行ではその結果、失敗した実行ではその `error` オブジェクトである。
通知の許可、Discord 権限、API トークンを変更する実行は非公開である: その `input` は `{}`、`output` は `null` である。
`has_idempotency_key` はリクエストが冪等キーを持っていたかどうかを示す。
キーそのものが返されることはない。

`GET /v1/executions/{execution_id}` は 1 件の実行を、`transfers` (その実行自身の移動を昇順に並べたもので、各項目は `{"transfer_id", "execution_id", "currency_id", "from_account", "to_account", "amount", "kind", "created_at"}`)、`child_execution_ids` (直接の入れ子の実行)、`deliveries`、`children` (直接の入れ子の実行を、それぞれの `transfers`、`child_execution_ids`、`deliveries` とともに含む) とともに返す。
それより深い階層はその ID で読む。
存在しない実行は 404 `execution_not_found` である。

`deliveries` は、実行のコントラクトのコードが `discord.send`、`discord.add_role`、`discord.remove_role` でキューに入れた Discord の操作と、それぞれに対する `worker` の進捗を、キューに入れられた順に一覧にする。
`worker` は実行のコミット後にそれらを行い、配送の失敗が実行、その残高、その移動を変えることはない。
配送は `{"outbox_id", "kind", "guild_id", "channel_id", "user_id", "role_id", "status", "attempts", "created_at", "next_attempt_at", "completed_at", "message_id", "error"}` である:

| フィールド | 意味 |
| --- | --- |
| `kind` | `discord_send`、`discord_add_role` または `discord_remove_role` |
| `guild_id`, `channel_id`, `user_id`, `role_id` | 対象を表す ID 文字列。`discord_send` では `channel_id` が、ロールの変更では `user_id` と `role_id` が設定され、それ以外は `null` である |
| `status` | `worker` が終えるまでは `pending`、その後は `sent` または `failed` |
| `attempts` | Discord へリクエストを送った配送の試行。Discord のレート制限による待機は試行に数えない |
| `next_attempt_at` | `pending` の配送を次に試みる時刻。それ以外は `null` |
| `completed_at` | 配送が送信または失敗した時刻。未完了の間は `null` |
| `message_id` | 送信済みの `discord_send` が投稿したメッセージの ID。それ以外は `null` |
| `error` | 失敗した配送では `{"reason", "http_status", "code", "message"}`、それ以外は `null` |

メッセージの本文が含まれることはない。
`error.reason` は `permission_revoked` (配送前にコントラクトがそのサーバーでの Discord 権限を失った)、`channel_not_in_guild` (チャンネルが別のサーバーに属するか、どのサーバーにも属さない)、`channel_unavailable` (Discord がチャンネルの表示を拒否した)、`message_rejected`、`role_change_rejected`、`retries_exhausted` (Discord またはネットワークがすべての試行で失敗し続けた)、`malformed_payload` または `internal_error` (`worker` がリクエストを送る前に失敗し、再試行しても同じ結果になるため、配送は直ちに失敗する) である。
`http_status`、`code` (Discord の JSON エラーコードを文字列にしたもの)、`message` (Discord のエラーメッセージ) は、失敗を決めた Discord の応答、`retries_exhausted` の場合は最後の試行に対する Discord の応答であり、Discord が何も返さなかった場合 (例えばネットワークエラーの後) は `null` である。
請求の通知は請求の実行の配送ではなく、一覧に載ることはない。
載せると支払者の通知の設定が明らかになるからである。

### 通貨

`GET /v1/currencies` は、すべての有効な通貨をシンボル、次に ID の順に並べた `{"items": [...]}` を返し、各項目は `{"currency_id", "symbol", "name", "supply", "deleted"}` である。
`include_deleted=true` を付けると削除された通貨も一覧にする。

`GET /v1/currencies/{currency_id}` は削除されたものを含めて 1 つの通貨を返し、`GET /v1/currencies/symbol/{symbol}` はそのシンボルを持つ有効な通貨を返す。
シンボルは大文字に変換してから照合する。
どちらも該当がなければ 404 `currency_not_found` であり、通貨のシンボルの最大長より長いシンボルは 422 の検証エラーとなる。
通貨は次のとおりである:

- `currency_id` (Discord のサーバー ID)、`name`、`symbol`、`issuance_account_id`、`created_by`、`created_at`。
- `supply` (金額)。
- `hourly_rate_ppm` (1 時間あたりのステークの利率を百万分率で表した文字列) と、まだ適用されていない利率変更の予約についての `pending_rate_ppm`、`rate_change_requested_at`、`rate_change_apply_after` (予約がなければすべて `null`)。
- `minting_renounced`、`delegation_enabled`、`interest_halted`。
- 失効していない削除の要請についての `delete_requested_at`、`delete_finalize_from`、`delete_finalize_until` (要請がなければ、要請が失効した後も含めてすべて `null`) と `deleted_at`。

### 請求と許可額

`GET /v1/accounts/{account_id}/claims` は、その勘定が請求者または支払者である請求を新しい順に並べたページ分割された一覧 (既定 15 件、1 ページ最大 100 件) である。
`status` (`pending`、`paid` または `canceled`) で絞り込み、`role` で勘定が行った請求 (`claimant`) またはその勘定に対して行われた請求 (`payer`) だけに限る。
`GET /v1/claims/{claim_id}` は 1 件の請求、または 404 `claim_not_found` を返す。
請求は `{"claim_id", "claimant_id", "payer_id", "currency_id", "amount", "description", "status", "created_at", "updated_at", "created_execution_id", "created_execution_origin", "created_execution_label_basis", "resolved_execution_id", "resolved_execution_origin", "resolved_execution_label_basis"}` である。
`resolved_execution_id` はその請求を支払ったか取り消した実行であり、未払いの間は `null` である。
`*_origin` と `*_label_basis` のフィールドは関連する実行の `origin` と `label_basis` であるため、旧版から取り込んだ請求は作成の実行について `legacy` を示す。
解決の実行についてのフィールドは、請求が未払いの間は `null` である。

`GET /v1/allowances/{owner_id}/{spender_id}/{currency_id}` は、使用者が持ち主の残高からまだ移動できる額を `{"owner_id", "spender_id", "currency_id", "symbol", "amount"}` として返す。
許可額が存在しない場合の金額は `"0.000"` であり、存在しない通貨は 404 `currency_not_found` である。

`GET /v1/accounts/{account_id}/allowance-history` は、その勘定が与えた許可額の変更を新しい順に並べたページ分割された一覧 (既定 15 件、1 ページ最大 100 件) であり、`spender_id` と `currency_id` で絞り込む。
変更は `{"allowance_history_id", "execution_id", "owner_id", "spender_id", "currency_id", "old_amount", "new_amount", "created_at"}` であり、承認、`transfer_from`、通貨の削除によって生じる。

## 台帳への書き込み

この節のすべてのエンドポイントはトークンを必要とし、すべての書き込みはトークンの勘定として実行される: 呼び出し元が送り手、持ち主、使用者、請求者、支払者、ステークする者、または管理者であり、リクエストの本文で別の操作者を指定することはできない。
書き込みは `Idempotency-Key` を受け付け、記録された実行の `execution_id` を持つ JSON オブジェクトとともにステータス 200 で応答する。
リクエストの本文の未知のフィールドは 422 の検証エラーとなるため、綴りを誤ったフィールドや余分なフィールド (`sender_id` など) が黙って無視されることはない。
数値の形式に反する本文 (JSON の数値、浮動小数点数、負の金額、小数 4 桁以上、指数、空白、空文字列、正の値のフィールドのゼロ) は 422 の検証エラーとなり、何も記録しない。
操作自体が決める失敗 (残高、権限、請求や通貨の状態) は失敗した実行として記録され、そのステータスと失敗した `execution_id` で応答する。
同じ `Idempotency-Key` でそのリクエストを繰り返すと、同じ封筒を `Idempotency-Replayed: true` とともに返す。
時刻は、`Z` の接尾辞を付けた UTC の ISO 8601 文字列である。

### 送金と許可額

`POST /v1/transfers` は本文 `{"recipient_id", "currency_id", "amount"}` で呼び出し元の資金を移動し、`{"execution_id", "transfer_id", "sender_id", "recipient_id", "currency_id", "amount"}` で応答する。
受取人は別のユーザー勘定でなければならず、自分自身への送金は 422 `invalid_argument` である。

`POST /v1/transfers/from` は、送り元が呼び出し元に与えた許可額を本文 `{"source_id", "recipient_id", "currency_id", "amount"}` で使い、`amount` を `source_id` から `recipient_id` へ移動する。
`{"execution_id", "transfer_id", "spender_id", "source_id", "recipient_id", "currency_id", "amount", "allowance_remaining"}` で応答し、`spender_id` は呼び出し元、`allowance_remaining` はその後に残る許可額である。
`amount` より小さい許可額は 409 `insufficient_allowance` であり、許可額は変わらない。
呼び出し元が送り元である場合、または送り元が受取人である場合は 422 `invalid_argument` である。

`POST /v1/allowances` は、`spender_id` が呼び出し元の残高から移動できる額を本文 `{"spender_id", "currency_id", "amount"}` で設定する。
この金額は現在の許可額に加算されるのではなく置き換わり、`"0"` も受け付けて許可額を削除する。
`{"execution_id", "owner_id", "spender_id", "currency_id", "old_amount", "new_amount"}` で応答する。
自分自身への承認は 422 `invalid_argument` である。

3 つとも、存在しない通貨または削除された通貨では 404 `currency_not_found` であり、2 つの送金は支払う側の残高が足りなければ 409 `insufficient_balance` である。
これらの失敗は記録される。

### 請求

`POST /v1/claims` は本文 `{"payer_id", "currency_id", "amount", "description"}` で `payer_id` に呼び出し元への支払いを求める。
`description` は 50 文字までの任意のテキストであり、空または空白だけのテキストは `null` として保存される。
それより長い説明は何も記録しない 422 の検証エラーである。
自分自身への請求は記録される 422 `invalid_argument` であり、存在しない通貨は記録される 404 `currency_not_found` である。
`{"execution_id", "claim_id", "claimant_id", "payer_id", "currency_id", "amount", "description"}` で応答する。
支払者が呼び出し元からの通知を許可している場合、Bot は支払者にその請求についての DM を送る。
請求が失効することはない。

`POST /v1/claims/{claim_id}/pay` は呼び出し元が支払うべき未払いの請求を支払い、`{"execution_id", "claim_id", "transfer_id", "payer_id", "claimant_id", "currency_id", "amount"}` で応答する。

`POST /v1/claims/{claim_id}/cancel` は未払いの請求をその支払者または請求者として取り消し、`{"execution_id", "claim_id", "canceled_by"}` で応答する。

どちらも本文をとらない。
未知の請求は 404 `claim_not_found`、それを解決できない呼び出し元は 403 `permission_denied`、もう未払いでない請求は 409 `invalid_state` であり、いずれも記録される。
支払いは 409 `insufficient_balance` でも失敗する。
通貨を削除するとその未払いの請求は取り消されるため、通貨が削除された請求の支払いは 409 `invalid_state` である。
正の ID 文字列でない `claim_id` は 422 の検証エラーとなる。

### ステーク

`POST /v1/stakes/deposit` と `POST /v1/stakes/withdraw` は、本文 `{"currency_id", "amount"}` で呼び出し元のウォレットと呼び出し元のステークの間で `amount` を移動する。
どちらもまず既存のステークの利息を現時点まで、別の利息の実行として計上する。
`{"execution_id", "account_id", "currency_id", "amount", "stake_amount", "transfer_id", "interest", "interest_execution_id"}` で応答する。
`stake_amount` はその後のステーク、`interest` は直前に計上された利息、`interest_execution_id` はその利息の実行 (利息が計上されなかった場合は `null`) である。
ウォレットより大きな預け入れは 409 `insufficient_balance`、ステークより大きな引き出しは 409 `insufficient_stake` である。

### API トークン

`GET /v1/tokens` は呼び出し元のトークンを発行順に並べたページ (既定 15 件、1 ページ最大 100 件) を返し、各項目は `{"token_id", "active", "created_at", "revoked_at", "last_used_at"}` である。
クエリパラメータ `include_revoked` (既定 `true`) を `false` にすると有効なトークンだけを一覧にする。
トークンのテキストもそのハッシュも返されることはない。
トークンは Discord の Bot だけが発行する。
`last_used_at` は最大で 1 分に 1 回更新される。

`POST /v1/tokens/revoke` は任意の本文 `{"token_id"}` で呼び出し元のトークンを取り消す: 本文なし、`{}`、または `"token_id": null` では呼び出し元の有効なトークンをすべて取り消し、`token_id` を指定するとそのトークンだけを取り消す。
`{"execution_id", "revoked_token_ids"}` で応答し、何も有効でなかった場合は空になる。
呼び出し元が所有していない `token_id` は 404 `not_found` である。
リクエストを送ったトークンが取り消されると、それ以降そのトークンによるすべてのリクエストは 401 になる。

### 通貨の管理

通貨は Discord のサーバーに属し、その ID はサーバー ID である。
管理の権利は、Bot が現在見ているサーバーの状態に基づいて決まる: サーバーの所有者が通貨を管理し、管理の委任が有効な間は管理者権限を持つすべてのメンバーが同じ管理操作 (Mint、Mint 放棄、利率の変更、削除) を行える。
管理の委任の有効化と無効化、および通貨の作成は所有者だけが行える。
権利のない呼び出し元は 403 `permission_denied` である。
Bot がサーバーを見られない場合、または Bot がデータベースに保持するサーバーの情報を時間内に読めない場合は、その所有者と管理者が不明であるため、管理は 409 `guild_unavailable` で失敗する。
どちらも記録される。
焼却は保有者の操作であり、サーバーがなくても行える。
作成以外のすべての通貨のエンドポイントは、存在しない通貨または削除された通貨では 404 `currency_not_found` であり、正の ID 文字列でない `currency_id` は 422 の検証エラーとなる。

`POST /v1/currencies` は本文 `{"guild_id", "name", "symbol", "supply", "hourly_rate_ppm"}` でサーバーの通貨を作成する。
`supply` (金額、`"0"` も可) は呼び出し元へ Mint され、`hourly_rate_ppm` は 1 時間あたりのステークの利率を百万分率で表したもの (`"0"` から `"1000000"` までの整数文字列で、`"1000000"` が 1 時間あたり 100%、既定 `"0"`) である。
名前は 24 文字以内で、英字で始まり、英字または数字で終わり、連続しないアンダースコアを 5 個まで含められる。
シンボルは大文字に変換したうえで 8 文字以内の英字であり、先頭でも末尾でもない単独のハイフンを 2 個まで含められる。
長さの上限を超える名前やシンボル、および `"0"` から `"1000000"` までの整数文字列でない `hourly_rate_ppm` は何も記録しない 422 の検証エラーであり、それ以外の命名規則への違反は記録される 422 `invalid_argument` である。
そのサーバーの有効な通貨、または他の有効な通貨が使っているシンボルは 409 `already_exists` である。
そのサーバーの削除された通貨はその場で再び作成され、レスポンスは `"revived": true` を持つ。
`{"execution_id", "currency_id", "name", "symbol", "hourly_rate_ppm", "initial_supply", "transfer_id", "revived"}` で応答し、供給量がゼロの場合 `transfer_id` は `null` である。

`POST /v1/currencies/{currency_id}/mint` は本文 `{"amount"}` で `amount` を呼び出し元のウォレットへ Mint し、`POST /v1/currencies/{currency_id}/burn` は同じ本文で呼び出し元自身の残高の `amount` を焼却する。
どちらも `{"execution_id", "currency_id", "account_id", "amount", "transfer_id"}` で応答する。
Mint 放棄後の Mint は 409 `currency_renounced`、最大の供給量を超える Mint は 422 `amount_overflow`、残高より多い焼却は 409 `insufficient_balance` である。

`POST /v1/currencies/{currency_id}/renounce` は Mint と利率の変更を永久に放棄し、予約中の利率変更を破棄して、`{"execution_id", "currency_id"}` で応答する。
2 回目の Mint 放棄は 409 `currency_renounced` である。

`POST /v1/currencies/{currency_id}/rate-change` は本文 `{"hourly_rate_ppm"}` (`"0"` から `"1000000"` までの整数文字列で、それ以外は何も記録しない 422 の検証エラー) で新しい利率を予約し、`{"execution_id", "currency_id", "new_rate_ppm", "requested_at", "apply_after"}` で応答する。
変更は予約の 7 日後から適用でき、失効することはない。
予約できる変更は 1 つだけであり (409 `invalid_state`)、Mint 放棄済みの通貨は 409 `currency_renounced` で拒否する。
どちらも記録される。

`POST /v1/currencies/{currency_id}/rate-change/apply` は、その通貨のすべてのステークを古い利率で現時点まで計上し、その後予約中の利率に切り替える。
`{"execution_id", "currency_id", "old_rate_ppm", "new_rate_ppm", "stakes_accrued", "interest_execution_ids"}` で応答する。
予約中の変更がなければ 409 `no_pending_request`、`apply_after` より前なら 409 `timelock_active` である。

`POST /v1/currencies/{currency_id}/delete` は削除を要請し、`{"execution_id", "currency_id", "requested_at", "finalize_from", "finalize_until"}` で応答する: 削除は要請の 7 日後から 10 日後まで確定できる。
まだ期間内にある要請があれば 409 `invalid_state` であり、失効した要請は新しい要請で置き換わる。

`POST /v1/currencies/{currency_id}/delete/finalize` は 1 つの実行で通貨を削除する: そのプールを解散し、その未払いの請求を取り消し、その許可額を削除し、その通貨のすべての残高とステークを焼却する。
`{"execution_id", "currency_id", "burned_amount", "balances_burned", "stakes_burned", "claims_canceled", "allowances_deleted", "pools_dissolved"}` で応答する。
要請がなければ 409 `no_pending_request`、早すぎれば 409 `timelock_active`、期間を過ぎていれば 409 `request_expired` であり、最後の場合は失効した要請も消去する。

`POST /v1/currencies/{currency_id}/delegation/enable` と `POST /v1/currencies/{currency_id}/delegation/disable` は管理者への管理の委任を有効または無効にし、`{"execution_id", "currency_id", "enabled", "changed"}` で応答する。
`changed` は、管理の委任が既に要求された状態であった場合に `false` である。

Mint 放棄、利率変更の適用、削除、削除の確定、管理の委任のエンドポイントは本文をとらない。

### 請求の通知

Bot は、支払者が請求者からの通知を許可している場合に限り、新しい請求についての DM を送る。
既定では何も許可されていない。
これらの設定は非公開である: その勘定自身のトークンだけが読み取りと変更を行える。

`GET /v1/notifications` は、請求が呼び出し元に通知されるユーザーを古い順に並べたページ (既定 15 件、1 ページ最大 100 件) を返し、各項目は `{"allowed_user_id", "created_at"}` である。

`POST /v1/notifications/allow` と `POST /v1/notifications/deny` は、本文 `{"user_id"}` で `user_id` からの通知を許可または停止する。
`{"execution_id", "account_id", "allowed_user_id", "changed"}` で応答する。
`account_id` は呼び出し元であり、`changed` は設定が既に要求された状態であった場合に `false` である。
自分自身を指定すると 422 `invalid_argument` である。

### コントラクトの Discord 権限

勘定のコントラクトがサーバーでメッセージを送りロールを変更できるのは、そのサーバーがその勘定を許可している間だけである。
サーバーの一覧を読み取り変更できるのは、サーバーの所有者または管理者権限を持つメンバーだけである。
それ以外の者は 403 `permission_denied` であり、Bot が見られないサーバーは 409 `guild_unavailable` である。
正の ID 文字列でない `guild_id` は 422 の検証エラーとなる。

`GET /v1/guilds/{guild_id}/discord-permissions` は許可された勘定を古い順に並べたページ (既定 15 件、1 ページ最大 100 件) を返し、各項目は `{"guild_id", "account_id", "granted_by", "created_at"}` である。
これは何も記録しないため、その失敗は `execution_id` を持たない。

`POST /v1/guilds/{guild_id}/discord-permissions/allow` と `POST /v1/guilds/{guild_id}/discord-permissions/deny` は、本文 `{"account_id"}` で `account_id` を許可または取り下げる。
`{"execution_id", "guild_id", "account_id", "changed"}` で応答し、`changed` は権限が既に要求された状態であった場合に `false` である。
既存の権限は元の `granted_by` を保つ。

## DEX

プールは定積型のプールであり、通貨のペアと手数料の段階で識別される。
各ペアは段階ごとに 1 つの有効なプールを持てる (`fee_bps` は 100、50 または 10 で、それぞれスワップの入力の 1%、0.5%、0.1% であり、プールに残って流動性提供者のものになる)。
プールはそのペアを `currency_a < currency_b` の順で保存する。
リクエストはペアを `currency_x` と `currency_y` としてどちらの順でも指定でき、書き込みのレスポンスは金額を両方の形 (`amount_a`/`amount_b` と `amount_x`/`amount_y`) で報告する。
`fee_bps` は JSON の数値であり、いずれかの段階でなければならない。
それ以外の値、文字列、浮動小数点数、真偽値は 422 の検証エラーとなる。
プールのシェアは 10 進文字列として送受信される整数であり、準備金とその他の金額は金額の形式を使う。
DEX のリクエストの本文の未知のフィールドは 422 の検証エラーとなるため、綴りを誤った最小値が黙って無視されることはない。
読み取りは公開されている。
書き込みはトークンを必要とし、`Idempotency-Key` を受け付け、すべての書き込みのレスポンスは記録された実行の `execution_id` を持つ。

### プールオブジェクト

プールの読み取りは、次のフィールドを持つプールオブジェクトを返す:

- `pool_id`、`currency_a`、`currency_b`、`symbol_a`、`symbol_b` (通貨の現在のシンボル)、`deleted_a`、`deleted_b` (各通貨が現在削除されているか)、`fee_bps`。
- `active`: 通貨の一方が削除されたためにプールが解散されると `false` になる。解散したプールは準備金とシェアがゼロの状態で行を保ち、そのペアと段階は新しい ID で再び作成できる。
- `reserve_a`、`reserve_b` (金額)、`total_shares`、`locked_shares` (作成時に永久にロックされる最小流動性分。解散後は 0)。
- `custody_account_id`: 準備金を保持するシステム勘定 (負の ID)。
- `created_by`、`created_at`、`created_execution_id`、`dissolved_at`、`dissolved_execution_id` (後の 2 つは有効な間 `null`)。
- `created_execution_origin`、`created_execution_label_basis`、`dissolved_execution_origin`、`dissolved_execution_label_basis`: 作成と解散の実行の `origin` と `label_basis` であり、解散についてのものは有効な間 `null` である。

### プールの読み取り

`GET /v1/pools` は有効なプールを `pool_id` の昇順に並べたページを返し、各項目はプールオブジェクトである。

`GET /v1/pools/{pool_id}` は、有効か解散済みかを問わず 1 つのプールオブジェクトを返す。
未知のプールは 404 `pool_not_found` であり、正の ID 文字列でない `pool_id` は 422 の検証エラーとなる。

`GET /v1/pools/pair/{currency_x}/{currency_y}` は `{"currency_a", "currency_b", "pools"}` を返す。
`pools` はそのペアの有効なプールを手数料の段階ごとに 1 つずつ `fee_bps` の昇順で一覧にし、ペアにプールがなければ空である。
通貨はどちらの順でも指定でき、同じ通貨を 2 回指定すると 422 `invalid_argument` である。

`GET /v1/accounts/{account_id}/pools` は、その勘定の流動性ポジションをプール ID の昇順に並べたページを返し、各項目は `{"pool", "account_id", "shares", "share_percent", "withdrawable_a", "withdrawable_b"}` である。
`pool` はプールオブジェクト、`share_percent` は `shares / pool.total_shares` を小数 2 桁の百分率の文字列にしたもの (偶数丸め、`"33.33"`)、`withdrawable_a`/`withdrawable_b` は現在の準備金ですべてのシェアを焼却した場合に支払われる額である。

### スワップの見積もり

`POST /v1/swap/quote` は何も書き込まずにスワップの価格を求め、トークンを必要としない。
本文は `{"from_currency", "to_currency", "amount_in"}` であり、`amount_in` は正である。
サーバーは受け取る合計を最大にするため、入力を小口に分け、1 から 5 個の互いに異なるプールからなる最大 `max_routes` 個の候補の経路にそれらを配分する (`GET /v1/config` が `chunk_count` と `max_routes` を報告する)。
レスポンスは次のとおりである:

- `from_currency`、`to_currency`、`from_symbol`、`to_symbol`、`amount_in`、`amount_out` (すべての経路で受け取る合計)。
- `routes`: 配分であり、経路ごとに 1 項目で、`pool_ids`、`currencies` (送り元から送り先までの経路上の通貨)、`fee_bps` (各プールの段階)、`amount_in`、`amount_out`、`hops` を持つ。各ホップは `pool_id`、`currency_in`、`currency_out`、`fee_bps`、`amount_in`、`amount_out` を持つ。経路の入力の合計は `amount_in`、経路の出力の合計は `amount_out` になる。
- `truncated`: 経路の探索が上限に達したため、より良い経路が存在しうる場合に `true`。
- `symbols`: 見積もりに現れるすべての通貨のシンボルを、通貨 ID の文字列をキーとして持つ。

エラー: どちらかの通貨が存在しないか削除されている場合は 404 `currency_not_found`、通貨をつなぐプールがないか、金額が小さすぎて何も受け取れない場合は 409 `no_route`、同じ通貨を 2 回指定した場合は 422 `invalid_argument` である。

### スワップ

`POST /v1/swap` は本文 `{"from_currency", "to_currency", "amount_in", "min_out"}` で呼び出し元の通貨をスワップする。
`min_out` は必須であり、`"0"` にするとどんな結果も受け入れる。
これを欠いた本文は 422 の検証エラーとなる。
スワップはすべての候補のプールをロックし、ロックした準備金で配分を計算し直すため、その結果は先の見積もりと異なりうる。
合計が `min_out` を下回る場合、スワップは失敗した実行として記録され、409 `slippage_exceeded` とその `execution_id` で応答し、何も移動しない。
見積もりを確認するクライアントは、見積もられた `amount_out` を少し下回る `min_out` を送るべきである。
見積もられた額をそのまま送ると、経路上のいずれかのプールが不利な方向に動いた時点で失敗する。
レスポンスは `execution_id`、`from_currency`、`to_currency`、`amount_in`、`min_out`、`amount_out`、`routes` (実行された配分で、見積もりの経路の形式)、`transfer_ids` (呼び出し元から最初のプールへ、プール間、最後のプールから呼び出し元へのすべての移動) を持つ。
記録される失敗は、優先順に 404 `currency_not_found`、409 `insufficient_balance`、409 `no_route`、409 `slippage_exceeded` である。
同じ通貨を 2 回指定すると 422 `invalid_argument` であり、何も記録しない。
出力がゼロに切り捨てられるための `no_route` の失敗は、実行の出力の `error` の隣に `"zero_output": true` を記録し、これによりプールが通貨をつないでいない場合と区別できる。

### プールの作成と流動性

`POST /v1/pools` はプールを作成する。
誰でも作成できる。
本文は `{"currency_x", "currency_y", "fee_bps", "amount_x", "amount_y"}` である: 作成者は両方の金額を預け入れ、それがプールの価格を決め、作成者は最初の流動性提供者になる。
この預け入れは最小単位で数えた `floor(sqrt(amount_a * amount_b))` のシェアを発行し、そのうち `minimum_liquidity` (`GET /v1/config`) は永久にロックされ、残りが作成者のものになる。
ロックされる最小量以下のシェアしか発行しない預け入れは 409 `insufficient_liquidity` である。
同じペアと段階の有効なプールがあれば 409 `pool_already_exists`、存在しない通貨または削除された通貨は 404 `currency_not_found` である。

`POST /v1/pools/liquidity/add` は、本文 `{"currency_x", "currency_y", "fee_bps", "amount_x", "amount_y", "amount_x_min", "amount_y_min", "min_shares"}` でペアと段階の有効なプールに預け入れる。
`amount_x` と `amount_y` は呼び出し元が預け入れる最大額である。
預け入れは一方をすべて使い、他方をプールの現在の価格が求める分だけ使う。
任意の最小値は価格の変動を防ぐ: `amount_x_min` と `amount_y_min` (金額、既定 `"0"`) は通貨の使用額がそれより少なくなる場合に、`min_shares` (正の整数文字列、既定 `"1"`) は発行するシェアがそれより少なくなる場合に預け入れを失敗させる。
どちらも 409 `slippage_exceeded` である。
シェアを 1 つも発行できないほど小さな預け入れは 409 `insufficient_liquidity`、ペアと段階の有効なプールがなければ 404 `pool_not_found` である。

`POST /v1/pools/liquidity/remove` は本文 `{"currency_x", "currency_y", "fee_bps", "shares", "amount_x_min", "amount_y_min"}` で呼び出し元のシェアを焼却し、各通貨の `floor(shares * reserve / total_shares)` を支払う。
`shares` は正の整数文字列である。
任意の `amount_x_min` と `amount_y_min` (既定 `"0"`) は、支払額がそれより小さくなる場合に引き出しを 409 `slippage_exceeded` で失敗させる。
保有するシェアが少なければ 409 `insufficient_shares`、ある通貨について何も支払われない引き出しは 409 `insufficient_liquidity`、ペアと段階の有効なプールがなければ 404 `pool_not_found` である。

3 つの書き込みはいずれも `execution_id`、`pool_id`、`currency_a`、`currency_b`、`fee_bps`、`currency_x`、`currency_y`、`amount_a`、`amount_b`、`amount_x`、`amount_y` (預け入れた額または支払われた額)、`shares` (発行または焼却したシェア)、`provider_shares` (その後の呼び出し元のシェア)、`total_shares`、`reserve_a`、`reserve_b`、`transfer_ids` で応答する。
プールの作成ではさらに `locked_shares` と `custody_account_id` が加わる。
検証エラー以外のドメインの失敗は記録されて `execution_id` を持ち、形式が不正なペアや段階は何かが記録される前に拒否される。

## コントラクト

コントラクトは 1 つのユーザー勘定に属し、その ID がすべてのパスで `{account_id}` としてコントラクトを指す。
言語、組み込み機能、ガスの費用は `document/contracts/` で説明する。
読み取りは公開されており、トークンを必要としない。
登録、ロック、停止、再開、削除はトークンを必要とし、呼び出し元自身のコントラクトにだけ作用する: トークンの勘定以外の `{account_id}` は 403 `permission_denied` であり、何も記録しない。
呼び出しとドライランはトークンを必要とし、トークンの勘定として実行され、どのコントラクトも対象にできる。
すべての書き込みは `Idempotency-Key` を受け付け、記録された実行の `execution_id` で応答する。
管理操作にガス代はかからない。
コントラクトのリクエストの本文の未知のフィールドは 422 の検証エラーとなるため、綴りを誤った `gas_limit` が黙って既定値に戻ることはない。

### 型付きの値

呼び出しの引数、呼び出しの結果、ストレージの値はコントラクトの値であり、JSON では `{"type", "value"}` として書く。
`type` はコントラクトの言語がその値の型に与える名前である。
同じエンコードをリクエストとレスポンスで使い、スカラーの引数については、実行の記録の `input.args` が保存する形とまったく同じである。
VM がその引数のリストを理由に拒否した入れ子の呼び出しでは、子の実行の `input.args` は `[]` であり、呼び出せない関数名は引用符で囲んでエスケープした形 (Python の `ascii()`) で `contract_function` として保存され、`...` で終わる 64 文字に切り詰められる。

| `type` | `value` | 例 |
| --- | --- | --- |
| `int` | 絶対値が 2^256 未満の 10 進整数の文字列、`^(0\|-?[1-9][0-9]*)$` | `{"type": "int", "value": "-5"}` |
| `str` | 有効な Unicode からなる 4,096 文字以内の文字列 | `{"type": "str", "value": "hi"}` |
| `bool` | `true` または `false` | `{"type": "bool", "value": true}` |
| `Account` | 10 進文字列で表した勘定 ID (符号付き 64 ビット) | `{"type": "Account", "value": "123"}` |
| `Currency` | 10 進文字列で表した通貨 ID (符号付き 64 ビット) | `{"type": "Currency", "value": "1269970084965912747"}` |
| `list` | 型付きの値の配列 (最大 1,024 個) | `{"type": "list", "value": [{"type": "int", "value": "1"}]}` |
| `dict` | `{"key", "value"}` の項目 (それぞれ型付きの値) の配列 (最大 1,024 個)。キーは `int` (符号付き 64 ビット)、`str`、`Account` または `Currency` であり、一意である | `{"type": "dict", "value": [{"key": {"type": "str", "value": "a"}, "value": {"type": "bool", "value": false}}]}` |

- list と dict はそれ自身の要素の型を持たない: 各要素が型付きの値であるため、ストレージの値と同様に list は型を混在させられる。
- list と dict の入れ子は、ストレージの値が持てる深さである最大 16 段までである。
- dict は項目の順序を保つ。ストレージの dict は正規のキーの順序で返され、これはエンコードしたキーのバイト列の順序である (短い `str` のキーでは、短いキーが先で、次にバイト順)。
- 呼び出しの結果は `int`、`str` または `bool` であり、公開関数の引数は `int`、`str`、`bool`、`Account`、`Currency` だけをとるため、list と dict はストレージの値としてのみ現れる。`Paid` や `CallResult` のようなレコードがコントラクトの外に出ることはなく、`null` になる値はない。
- この表に従わない型付きの値 (JSON の数値として送った `int`、先頭のゼロ、未知の型、範囲外の ID、欠けたフィールドや余分なフィールド) は、その引数の位置を示す type `typed_value` の 422 の検証エラーであり、何も記録しない。

### コントラクトの読み取り

`GET /v1/contracts/{account_id}` はコントラクトの状態を返す: `account_id`、`status` (`active`、`paused` または `deleted`)、`current_version`、現在のバージョンのバイナリの `sha256` (小文字の 16 進) と `size` (バイト)、`version_count`、`locked` (更新のロックが現在有効か)、`locked_until`、`storage_keys`、`storage_bytes`、`created_at`、`updated_at`、`deleted_at`、`created_execution_id`、`updated_execution_id`、その 2 つの実行の `origin` と `label_basis` である `created_execution_origin`、`created_execution_label_basis`、`updated_execution_origin`、`updated_execution_label_basis`、そして `functions` である。
`functions` は現在のバージョンの公開関数を名前順に一覧にし、各項目は `{"name", "params", "returns", "signature"}` である。
`params` は引数の型を順に保持し (バイナリは引数名を持たない)、`returns` は結果の型、`signature` は `"echo(int, str) -> str"` のような 1 行である。
削除されたコントラクトでは空である。
削除されたコントラクトは行、バージョン、現在のバージョン番号を保ち、ロックもストレージも持たない。
一度もコントラクトを登録していない勘定は 404 `contract_not_found` であり、正の ID 文字列でない `{account_id}` は 422 の検証エラーとなる。

`GET /v1/contracts/{account_id}/functions` は、呼び出しのフォームを組み立てるために同じ関数の一覧を `{"account_id", "status", "version", "functions"}` として返す。
コントラクトのない勘定は 404 `contract_not_found` である。

`GET /v1/contracts/{account_id}/versions` は登録されたバージョンを新しい順に並べたページ (既定 1 ページ 15 件、最大 100 件) を返し、各項目は `{"version", "sha256", "size", "created_execution_id", "created_execution_origin", "created_execution_label_basis", "created_at"}` である。
`created_execution_id` はそれを登録した `contract_update` の実行であり、その後の 2 つのフィールドはその実行の `origin` と `label_basis` である。
コントラクトのない勘定は空のページになる。

`GET /v1/contracts/{account_id}/versions/{version}` は、削除されたコントラクトのものも含めて 1 つのバージョンを `{"account_id", "version", "sha256", "size", "created_execution_id", "created_execution_origin", "created_execution_label_basis", "created_at", "binary"}` として返す。
`binary` は登録されたとおりのバイナリを、パディング付きの標準の base64 で表したものである。
`{version}` は 1 から始まるバージョン番号であり、存在しないバージョンは 404 `not_found` である。

`GET /v1/contracts/{account_id}/versions/{version}/binary` は同じバイナリを `application/octet-stream` としてダウンロードさせる。
`Content-Disposition: attachment; filename="contract-<account_id>-v<version>.rwc"` を付け、その SHA-256 を 16 進で `X-Checksum-SHA256` に、同じダイジェストを `Repr-Digest: sha-256=:<base64>:` (RFC 9530) に入れる。
このファイルは `rwc verify` と `rwc disasm` で確認と逆アセンブルができる (`document/contracts/cli.md`)。

`GET /v1/contracts/{account_id}/storage` はコントラクトのストレージをキーの順 (UTF-8 のキーのバイナリ順で、既定 1 ページ 15 件、最大 100 件) に並べたページを返し、各項目は `{"key", "value", "size", "updated_at"}` である。
`value` は型付きの値、`size` はそのエンコードした形のバイト数である。
ストレージのないコントラクト、またはコントラクトがない場合は空のページになる。

`GET /v1/contracts/{account_id}/storage/{key:path}` は 1 つの項目を `{"account_id", "key", "value", "size", "updated_at"}` として返す。
キーはパスの残りの部分であるため `/` を含められ、その他の予約文字はパーセントエンコードする。
値のないキーは 404 `not_found`、コントラクトのない勘定は 404 `contract_not_found` である。

### 登録

`POST /v1/contracts/{account_id}/versions` は、本文 `{"binary"}` (コンパイルした RWC1 のバイナリ (`rwc compile`) をパディング付きの標準の base64 で表したもの) で、呼び出し元自身のコントラクトの新しいバージョンを登録する。
サーバーはこれに `document/contracts/binary-format.md` の登録時の検査を行い、変更せずに次のバージョンとして保存する。
最初の登録はコントラクトを `active` として作成し、更新は停止中のコントラクトを停止中のまま保ち、削除後の登録はコントラクトを次のバージョン番号で `active` として復活させる。
レスポンスは `{"execution_id", "account_id", "version", "sha256", "size", "status", "revived"}` であり、`revived` は登録が削除されたコントラクトを復活させた場合に `true` である。
バイナリは `contract.max_size` バイト (`GET /v1/config`) までに制限され、本文は `max_body_bytes` の代わりに、そのようなバイナリの base64 の長さの 2 倍 (すべての `/` を JSON のエスケープ `\/` で書いた場合の余地) に 1,024 バイトを加えた大きさまでに制限される。
これより大きな本文には、概要で述べたとおり、完全に読み終える前かつトークンを確認する前に 413 `payload_too_large` で応答する。
何も記録しないエラー: パディング付きの標準の base64 でない base64 のテキスト、`binary` の欠落、未知のフィールド、JSON でない本文は 422 の検証エラーである。
`contract.max_size` バイトのバイナリより長い base64 のテキスト、および検査に通らないバイナリは 422 `invalid_contract` であり、そのメッセージは検証器のものである。
ロックされたコントラクトは記録される失敗であり、409 `contract_locked` である。
同じ `Idempotency-Key` と同じバイナリで登録を繰り返すと、バージョンを追加せずに最初の結果を返し、同じキーで別のバイナリを送ると 422 `idempotency_conflict` である。

### ロック、停止、再開、削除

`POST /v1/contracts/{account_id}/lock` は、本文 `{"until"}` または `{"hours"}` で、呼び出し元の有効なコントラクトの更新をある時刻まで禁止する:

- `until` は UTC からのオフセットを持つ ISO 8601 の時刻文字列であり、リクエストから最大 876,000 時間 (365 日の年で 100 年) 後までである。
- `hours` は 1 から 876,000 までの JSON の整数である。ロックはそれを記録する実行の時刻からその時間数の後に終わるため、同じ `Idempotency-Key` でリクエストを繰り返すと同じ `locked_until` で再現される。
- 両方を指定するかどちらも指定しない場合、オフセットのない時刻またはそれより先の時刻、文字列でない `until` (数値など)、JSON の整数でない `hours` (`true`、`"2"`、`2.0`) は、何も記録しない 422 の検証エラーとなる。

`{"execution_id", "account_id", "locked_until", "previous_locked_until"}` で応答し、`previous_locked_until` は置き換えたロックであり、有効なロックがなかった場合は `null` である。
ロック中のコントラクトは更新、停止、再開、削除ができず (409 `contract_locked`)、ロックは延長しかできない: 現在の終了時刻より後でない終了時刻は 409 `invalid_state`、過去の終了時刻は 422 `invalid_argument` であり、停止中のコントラクトはロックできない (409 `invalid_state`)。

`POST /v1/contracts/{account_id}/pause` と `POST /v1/contracts/{account_id}/resume` は本文をとらず、`{"execution_id", "account_id", "status"}` で応答する。
停止中のコントラクトは呼び出せず (409 `contract_paused`)、停止中のコントラクトの停止や有効なコントラクトの再開は 409 `invalid_state` である。

`POST /v1/contracts/{account_id}/delete` は本文をとらず、呼び出し元のロックされていないコントラクトを有効か停止中かを問わず削除し、そのストレージを消去する。
`{"execution_id", "account_id", "version", "storage_keys_cleared", "storage_bytes_cleared"}` で応答する。
バージョンは読み取り可能なまま残り、再び登録するとコントラクトは復活する。

この 3 つとロックのいずれでも、存在しないコントラクトまたは削除されたコントラクトは記録される 404 `contract_not_found` である。

### 呼び出し

`POST /v1/contracts/{account_id}/call` は、任意のコントラクトの公開関数を呼び出し元として呼び出す。
本文は次のとおりである:

- `function`: 公開関数の名前で、1 から 64 文字。
- `args`: 引数の順に並べた型付きの値の引数で、最大 16 個。既定は `[]`。
- `attach` (任意): `{"currency_id", "amount"}`。1 つの通貨の正の金額であり、呼び出しの開始時に呼び出し元からコントラクトへ移動し、呼び出しが失敗すると他のすべてとともに返される。コントラクトはこれを `paid` として読み、コントラクト自身の勘定が自分自身の呼び出しに添付することはできない。
- `gas_limit` (任意): 1 から `gas.max_gas` までの 10 進文字列。指定しなければ `gas.default_gas` が適用される (`GET /v1/config`)。

呼び出しを開始するには、呼び出し元がガス代の通貨を `gas_limit` に `gas.price` を掛けた額 (添付がその通貨であればその分を加えた額) だけ保有していなければならない。
呼び出しの後、使用したガスの分が呼び出し元から焼却され、残りが取られることはない。
成功した呼び出しは次の内容で応答する:

- `execution_id`、`contract_id`、`function`。
- `value`: 関数の結果を型付きの値で表したもの。
- `gas_used` と `gas_limit` (10 進文字列)、呼び出しに課されたガス代の通貨と価格である `gas_currency_id` と `gas_price`、焼却されたガス代の通貨の額である `gas_cost` (`gas_used` に `gas_price` を掛けたもの)。これらは記録された実行から得るため、サーバーのガスの設定が変わった後の再現でも同じ値を報告する。
- `random`: 最上位の実行が `random()` で引いた値を 10 進文字列で表したもの。入れ子の呼び出しは自身の値をそれぞれの実行に保持する。

呼び出しがキューに入れた Discord のメッセージとロールの変更は、呼び出しのコミット後に `worker` が送り、レスポンスには含まれない。

記録された後に失敗した呼び出しは、エラーの封筒とその `execution_id` に加え、`kind` と、CALL_FAILED の場合は `reason` で応答する。
実行の `output` は、エラー、使用したガスと上限、呼び出しのすべての実行が引いた値を保持する。
失敗の前に使用したガスは課金されたままである: コントラクトの失敗は使用したガス (最上位の上限を使い切った場合は上限全体) を徴収し、開始しなかった呼び出し (コントラクトがない、停止中のコントラクト、未知の関数、誤った型の引数、支払えないガスまたは添付) は何も課金しない。

| 失敗 | ステータス | コード | `kind` |
| --- | --- | --- | --- |
| `revert()` または失敗した `assert` | 422 | `contract_reverted` | `REVERTED` |
| ゼロ除算、失敗した組み込み機能、その他の実行時の障害 | 422 | `contract_runtime_error` | `RUNTIME` |
| ガス上限を使い切った | 422 | `contract_out_of_gas` | `OUT_OF_GAS` |
| 未知の関数または誤った型の引数 (`reason` `NO_FUNCTION` または `BAD_ARGUMENTS`) | 422 | `contract_call_failed` | `CALL_FAILED` |
| 呼び出し全体のメモリ、実時間、ホスト時間の上限 | 422 | `contract_limit_exceeded` | `MEMORY_LIMIT`、`WALL_CLOCK_LIMIT` または `HOST_TIME_LIMIT` |
| コントラクトがない、または削除されている (`reason` `NO_CONTRACT`) | 404 | `contract_not_found` | `CALL_FAILED` |
| 停止中のコントラクト (`reason` `INACTIVE`) | 409 | `contract_paused` | `CALL_FAILED` |
| 上限に対するガス代の通貨、または添付する通貨が足りない (`reason` `INSUFFICIENT_BALANCE`) | 409 | `insufficient_balance` | `CALL_FAILED` |
| 添付する通貨が存在しない (`reason` `NO_CURRENCY`) | 404 | `currency_not_found` | `CALL_FAILED` |
| コントラクトが呼び出し元のガス代の通貨を取ったため、ガス代をもう支払えない | 409 | `insufficient_balance` | `SETTLEMENT` |

何も記録しないエラーは 422 である: 不正な形式の本文または型付きの値 (`validation_error`)、および `gas.max_gas` を超えるガス上限、コントラクトの上限内のスカラーでない引数、自分自身のコントラクトへの添付 (`invalid_argument`)。
`discord.has_role()` は、Bot から見たサーバーのメンバーに基づいて答え、API のプロセスはそれをギルド一覧 (`guild directory`) から読む。

### ドライラン

`POST /v1/contracts/{account_id}/dry-run` は、ガスの確認、入れ子の呼び出し、ストレージへの書き込みを含めて `POST /v1/contracts/{account_id}/call` とまったく同じように呼び出しを実行し、その後すべてを巻き戻すため、何も記録されず何も移動しない。
トークンを必要とし (有効なトークンがなければ 401 `unauthorized`)、実行の間その勘定の残高を保持するため、トークンの勘定として実行される。
本文は呼び出しの本文であり、`caller_id` やその他の余分なフィールドは 422 の検証エラーとなる。
ドライランは `Idempotency-Key` をとらない: 重複を除くべきものがないため、送ると 422 の検証エラーとなる。
呼び出しが成功するか失敗するかにかかわらず、応答は 200 である:

- `ok`、`value` (型付きの結果。失敗時は `null`)、`error` (成功時は `null`、それ以外は実際の呼び出しが記録するコードを持つ `{"code", "message", "kind", "reason"}`)。
- `gas_used`、`gas_limit`、`gas_currency_id`、`gas_price`、`gas_cost` (呼び出しと同じ)。
- `random` (最上位の実行が引いた値) と `random_tree` (呼び出しのすべての実行が引いた値を開始順に並べたもので、各項目は `{"order", "depth", "contract_id", "function", "values"}`。最上位の実行は `order` 0、`depth` 1)。
- `transfers`: 呼び出しが行うすべての移動で、ガスの焼却を含み、各項目は `{"currency_id", "from_account", "to_account", "amount", "kind"}`。
- `outbox`: キューに入れられる Discord の操作で、各項目は `{"kind", "payload"}`。`payload` は `worker` が受け取る形のままである。

ドライランの乱数値は新たに引かれるため、実際の呼び出しは別の値を引く。
不正な形式のリクエストには呼び出しと同様に応答し、そのエラーの封筒が `execution_id` を持つことはない。

## 運営者用バックアップ

運営者はこれらのエンドポイントを通じてデータベースのバックアップを一覧にし、ダウンロードする。
Web エクスプローラーの運営者用ページ `/operator.html` は、運営者のトークンでこれらを呼び出す ([web.md](web.md#運営者用ページ))。
バックアップには公開されていないデータ (API トークンのハッシュ、通知と Discord 権限の設定、`outbox` の内容) が含まれるため、運営者だけがそれに到達できる。

### アクセス

- どちらのエンドポイントもトークンを必要とする: 有効なトークンがない (無い、形式が不正、未知、または取り消し済み) 場合の応答は 401 `unauthorized` である。
- トークンの勘定は `RAPIDWIRE_OPERATOR_ACCOUNT_IDS` に含まれていなければならない。それ以外の勘定は、指定したファイルが存在するか、名前が有効かにかかわらず、`/v1/admin/backups` の下のすべてのパスで 403 `permission_denied` となり、何も記録されない。
- トークンに運営者用の別の権限はない: 運営者は運営者の勘定の通常のトークンで認証する。
- これらのエンドポイントは `Idempotency-Key` をとらない。送られたキーは無視され、すべての呼び出しが新しい実行として記録される。

### 一覧

`GET /v1/admin/backups` はバックアップを新しい順 (時刻が等しい場合は名前の昇順) に並べたページ分割された一覧 (既定 15 件、1 ページ最大 100 件) であり、標準のページオブジェクトに、一覧を記録する `backup_list` の実行である `execution_id` を加えた `{"items", "page", "per_page", "total", "total_is_lower_bound", "execution_id"}` として返す。
`total` はディレクトリにあるバックアップの正確な数であり、`total_is_lower_bound` は常に `false` である。
応答のためにハッシュを計算するのは、要求されたページのファイルだけである。
各項目は次のとおりである:

| フィールド | 内容 |
| --- | --- |
| `name` | ファイル名。ダウンロードの `{name}` として使う |
| `created_at` | バックアップが完了した時刻。マイクロ秒を含む UTC の ISO 8601 (`"2026-10-31T03:00:00.123456Z"`) で、ファイルの更新時刻である |
| `size` | バイト単位のファイルサイズ (JSON の数値) |
| `sha256` | ファイルの SHA-256 (小文字の 16 進 64 桁) |

バックアップのディレクトリがなければ一覧は空になり、サーバーはディレクトリが再び現れるまで警告を一度だけログに記録する。
したがって空の一覧は、バックアップがないか、ディレクトリがないかのどちらかを意味する。
存在するが読めないディレクトリまたはファイルは 500 `internal_error` である。

### ダウンロード

`GET /v1/admin/backups/{name:path}` は、ファイルのバイト列を次のヘッダーとともにストリーミングする:

- `Content-Type: application/gzip` (名前が `.gz` または `.tgz` で終わる場合)、それ以外は `application/octet-stream`。バイト列は保存されたとおりに送られ、`Content-Encoding` が付くことはない。
- `Content-Length` と `Content-Disposition: attachment; filename="<name>"`。
- 一覧と同じく SHA-256 を 16 進で表した `X-Checksum-SHA256` と、同じダイジェストの `Repr-Digest: sha-256=:<base64>:` (RFC 9530)。
- ダウンロードを記録する `backup_download` の実行を示す `X-Execution-Id`。
- `Cache-Control: no-store` (一覧にも付く) と `X-Content-Type-Options: nosniff`。

このルートはパスの残り全体を `{name}` としてとるため、スラッシュを含むパスは、エンコードされている (`%2F`) か否かにかかわらず他のパスと同様に運営者の確認に達し、その後名前として拒否される。
バックアップの名前でない名前、バックアップのディレクトリの直下にある通常のファイルでないものの名前、存在しないファイルは、いずれもメッセージ `The backup was not found.` の 404 `not_found` であり、何も記録されない。
名前はファイルシステムに触れる前に許可リストと照合され、ファイルはシンボリックリンクをたどらずにバックアップのディレクトリからの相対で開かれ、通常のファイルだけが配信されるため、どのリクエストもディレクトリの外のファイルには到達しない。

### チェックサム

サーバーはすべての SHA-256 を配信するファイルから自分で計算し、バックアップの隣に書かれたチェックサムファイルを読むことはない。
そのようなファイルは、名前が条件を満たせばそれ自体が 1 つのバックアップとして一覧に載る。
ダイジェストは API のアプリケーションごとに、ファイル名、デバイス、inode、サイズ、更新時刻をキーとしてメモリにキャッシュされるため、ファイルのハッシュが再び計算されるのは、ファイルが置き換えられるか変更された場合だけである。
起動時に API は既存のバックアップのハッシュをバックグラウンドのスレッドで計算する。
このスレッドが起動を遅らせることはなく、その失敗はログに記録され、最初の一覧はそれらのダイジェストがキャッシュされた状態で始まる。
まだハッシュの計算中のファイルのダイジェストを必要とするリクエストは、ファイルのハッシュを計算し直さずにその 1 回の計算を待つ。
ダウンロードは開いたファイルの同一性をとり、その同一性に対してキャッシュされたダイジェストを使うか、なければ開いたファイル自体のハッシュを計算する。
そのため、バックアップのファイルがその場で変更されない限り (バックアップのジョブがこれを保証する)、ヘッダーのダイジェストは送るバイト列と一致する。
送信中に縮んだファイルは、サーバーエラーとしてレスポンスを早期に終わらせ、そのディスクリプタは閉じられる。

### バックアップファイル

一覧が示すのは、`RAPIDWIRE_BACKUP_DIR` の直下にある通常のファイルのうち、名前が `^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$` に一致し、`.tmp` または `.partial` で終わらないものだけである。
ドットファイル、それ以外の名前、サブディレクトリ、シンボリックリンク (ディレクトリ内のファイルへのものも含む)、FIFO、その他の特殊ファイルは黙って飛ばされる。
API のコンテナはバックアップのボリュームを読み取り専用でマウントし、そこへ書き込むバックアップのジョブは次の規則に従う:

- 各バックアップはディレクトリの直下にある 1 つのファイルである。例えば、圧縮した `mysqldump` は `rapidwire-dump-20261031T030000Z.sql.gz`、件数、チェックサム、不変条件の値を含む JSON Lines のエクスポートは `rapidwire-export-20261031T030000Z.tar.gz` であり、複数のファイルからなるエクスポートは 1 つのアーカイブにまとめる。
- バックアップは同じディレクトリに `.partial` で終わる名前で書き込まれ (一覧は `.tmp` の名前も飛ばす)、フラッシュと同期の後に最終的な名前へ名前を変えるため、一覧に載るファイルは常に完全である。
- ファイルは名前の変更後に変更されることがない。その更新時刻がバックアップの `created_at` であり、保持期間による削除はファイル単位で古いものから行う。

### 実行の記録

成功した一覧とダウンロードはそれぞれ、運営者を操作者とする 1 つの実行として記録され、公開の実行の読み取りは他の実行と同様にそれを示す:

| 種類 | `input` | `output` |
| --- | --- | --- |
| `backup_list` | リクエストの `{"page", "per_page"}` | `{"count", "total"}`: `count` は応答の `items` にあるバックアップの数、`total` はディレクトリにあるバックアップの数 |
| `backup_download` | `{"name"}` | 配信したファイルの `{"name", "size", "sha256"}` |

ダウンロードはファイルを開き、検証し、ハッシュを計算した後、最初のバイトを送る前に記録されるため、その後クライアントが中断したダウンロードも記録される。
トークンは記録のトランザクションの中で再度確認されるため、ダウンロードが記録される前に取り消されたトークンは 401 となり、何も記録されず、ファイルも受け取らない。
その後の取り消しは、始まったダウンロードを止めない。
拒否されたリクエスト (401、403) と存在しないファイル (404) は何も記録しない。

## Python クライアント

`client/` のパッケージ `rapidwire_client` は、`httpx` だけに依存するこの API のクライアントである。
`pip install ./client` で、開発用には `pip install -e ./client` でインストールする ([client/README.md](../client/README.md) が詳しく説明する)。
`AsyncRapidWireClient(base_url, token=None, *, transport=None, timeout=30.0)` は非同期のクライアントであり、`RapidWireClient` は同じメソッドを持つ同期のクライアントである。
`base_url` は `/v1` を含まないサーバーのルートであり、どちらも接続を閉じるコンテキストマネージャーである。
各エンドポイントに 1 つのメソッドがあり、`client_routes()` はクライアントのクラスが扱うルートを一覧にする。

```python
from decimal import Decimal
from rapidwire_client import Account, AsyncRapidWireClient, ConflictError, from_units, new_idempotency_key

async with AsyncRapidWireClient("https://rapidwire.example", token) as client:
    sent = await client.transfer(recipient_id=123, currency_id=456, amount=Decimal("12.5"), idempotency_key=new_idempotency_key())
    balances = [balance async for balance in client.paginate(client.list_balances, 123)]
    result = await client.call_contract(789, "echo", [5, "text", True, Account(123)])
    await client.download_backup("rapidwire-dump-20261031T030000Z.sql.gz", "dump.sql.gz")
```

- 金額は通貨単位の `Decimal` または `str`、あるいは `from_units()` を通じた最小単位である。`float` や裸の `int` は `TypeError` を送出し、小数 4 桁以上は丸めずに `ValueError` を送出する。
- 結果は、`int` の ID、`Decimal` の金額、タイムゾーン付きの `datetime` の時刻を持つ凍結された dataclass である。ページ分割された一覧は `has_next` を持つ `Page` を返し、`paginate()` は到達可能な範囲内のすべてのページをたどる。`list_backups()` も含み、その `BackupList` は一覧の `execution_id` を持つ `Page` である。
- すべての書き込みは任意の `idempotency_key` をとり、サーバーが同じキーの以前のリクエストから応答した場合は、その結果 (または送出されたエラー) に `replayed` が設定される。
- エラーの封筒は、ステータスに応じた `RapidWireAPIError` のサブクラス (`AuthenticationError`、`PermissionDeniedError`、`NotFoundError`、`ConflictError`、`ValidationError`、`ServerError`) を送出し、それは `code`、`message`、`execution_id`、`kind`、`reason`、`child`、`details` を持つ。
- コントラクトの値は Python の `int`、`str`、`bool`、`list`、`dict` と `Account`、`Currency` である。`encode_value()` と `decode_value()` はそれらを型付きの JSON の形と相互に変換する。
- バックアップのダウンロードは、パス (`.partial` ファイルとして書き込み、SHA-256 が一致した後でのみ名前を変える)、ファイルオブジェクト、または `stream_backup()` へストリーミングする。`X-Checksum-SHA256` や `Content-Length` と異なるバイト列は `ChecksumMismatchError` を送出し、コントラクトのバイナリのダウンロードも同じように確認される。
- トークンはそれを使うエンドポイントにだけ送られ、`repr` に現れることはない。
