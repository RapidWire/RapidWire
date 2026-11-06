# rapidwire-client

`rapidwire_client` は RapidWire の `/v1` HTTP API の Python クライアントであり、すべてのエンドポイントの定義を共有する非同期クライアントと同期クライアントを備える。
Python 3.12 以降を必要とし、[httpx](https://www.python-httpx.org/) だけに依存する。
API そのものは [document/api.md](../document/api.md) で説明している。

## インストール

リポジトリのクローンから:

```
pip install ./client
```

`pip install -e ./client` は開発用に editable モードでインストールする。
リポジトリ自身の開発環境の中では pytest と mypy が `client/` から直接インポートするので、そこでは別途のインストールは不要である。

## クライアント

`AsyncRapidWireClient` が非同期クライアント、`RapidWireClient` が同期クライアントであり、両者は同じメソッドを持ち、非同期クライアントではそれらがコルーチン関数である。

```python
from decimal import Decimal

from rapidwire_client import AsyncRapidWireClient, RapidWireClient

async with AsyncRapidWireClient("https://rapidwire.example.com", token) as client:
    sent = await client.transfer(recipient_id=123, currency_id=456, amount=Decimal("12.5"))

with RapidWireClient("https://rapidwire.example.com") as client:
    currency = client.get_currency_by_symbol("RAPID")
```

- コンストラクタは `(base_url, token=None, *, transport=None, timeout=30.0)` である。`base_url` は `/v1` を含まないサーバーのルート、`transport` は httpx のトランスポート (たとえばテストでの `httpx.MockTransport`)、`timeout` は秒数または `httpx.Timeout` である。
- どちらのクライアントも終了時に接続を閉じるコンテキストマネージャであり、コンテキストマネージャを使わない場合は、非同期クライアントでは `aclose()` を、同期クライアントでは `close()` を呼ぶ。
- 結果は、ID が `int`、金額が `Decimal`、時刻がタイムゾーン付きの `datetime` である凍結された dataclass である。
- 履歴検索の `start` と `end` やコントラクトのロックの `until` などのリクエストの時刻は、タイムゾーン付きの `datetime` 値でなければならず、タイムゾーンなしの値は `ValueError` を送出する。
- `client_routes(RapidWireClient)` はクライアントが扱う `(method, route)` の組を返し、各メソッドの docstring がその動作を述べている。

## 認証

読み取りは公開されており、トークンは不要である。
書き込み、コントラクトのドライラン、呼び出し元の非公開の一覧 (トークン、通知、サーバーの Discord 権限)、運営者向けのバックアップのエンドポイントには API トークンが必要であり、トークンは Discord Bot を `API` 付きでメンションすると発行される ([document/bot.md](../document/bot.md#api-トークンのメンション))。

- トークンは `token` として渡し、空白を含まない空でない文字列でなければならず、そうでなければコンストラクタが `ValueError` を送出する。
- クライアントは `Authorization: Bearer <token>` をそれを使うエンドポイントにだけ送り、公開の読み取りには決して送らない。
- トークンがクライアントの `repr` や `str` に現れることはない。
- トークンがない、未知である、または無効化されている場合、それらのエンドポイントは `AuthenticationError` を送出する。

## 冪等キー

すべての書き込みは省略可能なキーワード引数 `idempotency_key` を受け取り、`Idempotency-Key` ヘッダーとして送る。
勘定がすでに使ったキーで書き込みを繰り返すと、再実行せずに最初の結果が返るので、タイムアウト後の再試行が安全になる。

```python
from rapidwire_client import new_idempotency_key

key = new_idempotency_key()
result = client.transfer(123, 456, Decimal("12.5"), idempotency_key=key)
again = client.transfer(123, 456, Decimal("12.5"), idempotency_key=key)
assert again.replayed and again.transfer_id == result.transfer_id
```

- `new_idempotency_key()` はランダムなキーを返す。
- キーは先頭と末尾に空白を持たない 1 から 255 文字の印字可能な ASCII 文字であり、それ以外の値はリクエストを送る前に `ValueError` を送出する。
- すべての書き込みの結果は `execution_id` と `replayed` を持ち、`replayed` はサーバーが同じキーの以前のリクエストから応答したときに `True` である。記録済みの失敗が返された場合は最初と同じエラーを送出し、その例外には `replayed` が設定される。
- 異なる引数にキーを再利用すると、コード `idempotency_conflict` の `ValidationError` を送出する。
- `dry_run_contract()` とバックアップのエンドポイントはキーを受け取らない。

## ページ送り

ページ単位の一覧は `items` (タプル)、`page`、`per_page`、`total`、`total_is_lower_bound` を持つ `Page` を返し、キーワード引数 `page` と `per_page` を受け取る。
`has_next` は、到達可能な範囲の中で次のページがさらに項目を持ちうるかを示す: API は一覧の最初の 10,000 項目だけを提供し、`total` は `total_is_lower_bound` が真でない限り正確であり、データベースで数える一覧は 10,000 を超える項目が一致したときにそれを真にする。

`paginate(method, *args, per_page=None, **kwargs)` はページ単位の一覧のすべての項目を順に返し、`has_next` が偽になるまでページを次々に要求する:

```python
for balance in client.paginate(client.list_balances, 123):
    print(balance.symbol, balance.amount)

async for entry in async_client.paginate(async_client.get_account_history, 123, currency_id=456):
    print(entry.transfer_id, entry.amount)
```

`list_backups()` は `BackupList` を返し、これは一覧取得の `execution_id` を持つ `BackupInfo` 項目の `Page` なので、`paginate()` でも使える。

## フィルタと実行の出自

- `list_claims(account_id, status=..., role=...)` は、勘定が行った請求には `role="claimant"`、勘定に対して行われた請求には `role="payer"` を受け取り、`role` を指定しなければ両方を一覧にする。
- `list_executions(contract_id=...)` は、コントラクトの呼び出し (入れ子の呼び出しを含む) をその管理実行とともに一覧にし、呼び出しだけを得るには `execution_type="contract_call"` と組み合わせる。
- `get_user_stats()` は勘定の正確な `total_transfers` を返す。
- すべての実行と移動の項目は `origin` と `label_basis` (`legacy` は旧版から取り込んだ実行を示す) に加え、親の実行の `parent_origin` と `parent_label_basis` を持ち、実行はさらに `contract_id` (呼び出しが呼び出した、または管理実行が対象としたコントラクト) を持つ。
- 請求、プール、コントラクト、コントラクトのバージョンは、それぞれが結び付く各実行の出自とラベルの根拠を持つ: 請求では `created_execution_origin` と `resolved_execution_origin`、プールでは `created_execution_origin` と `dissolved_execution_origin`、コントラクトでは `created_execution_origin` と `updated_execution_origin`、バージョンでは `created_execution_origin` であり、それぞれに対応する `*_label_basis` がある。

## 金額

金額は通貨単位の `Decimal` または `str` で渡し、小数部がちょうど 3 桁の `Decimal` として返るので、金額が浮動小数点を経由することは決してない。

- リクエストの金額は、負でない `Decimal` または `"12.5"` のような `str` であり、小数部は 3 桁まで、最大 10^38 - 1 最小単位である。
- `float` または `int` は `TypeError` を送出する: `int` は通貨単位と最小単位のどちらとも取れるためである。
- 小数部が 3 桁を超える値、負の値、不正な形式の文字列は、丸めずに `ValueError` を送出する。
- `from_units(12500)` は最小単位を `Decimal("12.500")` に変換し、`to_units(Decimal("12.5"))` は `12500` に戻し、どちらもあらゆる大きさで正確である。
- `amount_string()` は金額の正規のリクエスト文字列を与え、`parse_amount()` はレスポンスの金額文字列を解析する。
- 実行の生の `input` と `output` のオブジェクトは保存されたとおりに返り、金額を含むすべての整数は最小単位の 10 進数文字列である。
- プールのシェアは API の生のシェア数による素の `int` 値である。

```python
from rapidwire_client import from_units, to_units

quote = client.quote_swap(1, 2, Decimal("10"))
min_out = from_units(to_units(quote.amount_out) * 99 // 100)
client.swap(1, 2, Decimal("10"), min_out)
```

## コントラクトの値

コントラクトの呼び出しの引数、結果、ストレージの値は Python の値である:

| コントラクトの型 | Python の値 |
| --- | --- |
| `int` | `int` |
| `str` | `str` |
| `bool` | `bool` |
| `Account` | `Account(id)` |
| `Currency` | `Currency(id)` |
| `list` | コントラクトの値の `list` |
| `dict` | キーが `int`、`str`、`Account`、`Currency` のいずれかである `dict` |

```python
from rapidwire_client import Account, Attachment, Currency

result = client.call_contract(789, "echo", [5, "text", True, Account(123), Currency(456)], attach=Attachment(currency_id=456, amount=Decimal("1")), gas_limit=1000)
print(result.value, result.gas_used, result.gas_cost, result.random)
entry = client.get_storage(789, "counter")
```

- `Account` と `Currency` は `int` の ID を包むので、素の整数とは区別される。
- `encode_value()` は値を API の型付き JSON 形式 `{"type", "value"}` に変換し、`encode_args()` は引数のリストを変換し、`decode_value()` は元に戻す。クライアントは引数、結果、ストレージの値にこれらを自ら適用する。
- `float`、`None`、その他の型は、エンコード時に `TypeError` を送出し、他の型の dict キーも同様である。
- `dry_run_contract()` は `idempotency_key` を除き `call_contract()` と同じ引数を受け取り、結果またはエラー、ガス、引いた値、移動、キューに入った Discord の操作を持つ `DryRunResult` を返す。

## エラー

| 例外 | 送出される場合 |
| --- | --- |
| `RapidWireError` | クライアント自身が送出するすべての例外の基底 |
| `RapidWireAPIError` | API が応答したエラーであり、以下のクラスの基底 |
| `AuthenticationError` | 401: トークンがない、形式が不正、未知、または無効化されている |
| `PermissionDeniedError` | 403: 呼び出し元はその操作を行えない |
| `NotFoundError` | 404: 指定した項目が存在しない |
| `ConflictError` | 409: 正しい形式のリクエストを現在の状態が拒否する (残高不足やスリッページなど) |
| `ValidationError` | 422: リクエストが不正であるか、コントラクトの実行が失敗した |
| `ServerError` | 5xx: サーバーが失敗したか、Discord が利用できない |
| `ResponseFormatError` | API の形式に従わないレスポンス |
| `ChecksumMismatchError` | バイト列が通知された SHA-256 またはサイズと一致しないダウンロード |

`RapidWireAPIError` は `status_code`、`code` (API の安定した snake_case のエラーコード)、`message`、`execution_id` (記録された失敗した実行、または `None`)、`kind` と `reason` (失敗したコントラクトの実行のもの)、`child` (失敗した入れ子の実行)、`details` (バリデーションエラーの `ErrorDetail(loc, message, type)` のタプル)、`replayed`、`retry_after` (`Retry-After` ヘッダーからの秒数) を持つ。
それ以外のステータスは基底の `RapidWireAPIError` を送出し、API のエラーエンベロープを持たないエラーレスポンスにはコード `http_error` が付く。

```python
from rapidwire_client import ConflictError

try:
    client.transfer(123, 456, Decimal("1000000"))
except ConflictError as error:
    print(error.code, error.message, error.execution_id)
```

## チェックサムで検証するダウンロード

バックアップとコントラクトのバイナリのダウンロードは、サーバーが送る `X-Checksum-SHA256` ヘッダーとバイト列を照合する。

- `download_backup(name, destination)` はバックアップをパスまたはバイナリのファイルオブジェクトへストリーミングし、`name`、`size`、`sha256`、`execution_id` (`backup_download` 実行)、`content_type` に加え、パスを指定した場合は `path` を持つ `BackupDownload` を返す。
- パスの書き込み先は `<path>.partial` として書かれ、SHA-256 と `Content-Length` が一致した後にだけそのパスへ改名される。不一致やその他の失敗の場合は部分ファイルが削除され、`ChecksumMismatchError` (またはその他のエラー) が送出される。
- ファイルオブジェクトはバイト列を届いた順に受け取るので、`ChecksumMismatchError` の後はその内容を破棄しなければならない。
- `stream_backup(name)` はコンテキストマネージャ (非同期クライアントでは `async with`) であり、そのストリームはバイト列を順に返し、一致しない場合は最後のチャンクの後に `ChecksumMismatchError` を送出する。その `result` は、ストリームを最後まで読んだ時点で検証済みの `BackupDownload` になる。
- `download_contract_binary(account_id, version)` は、同じ方法でダイジェストを検査した後、`data`、`sha256`、`filename` を持つ `ContractBinary` を返す。
- 有効な `X-Checksum-SHA256` ヘッダーのないダウンロードは `ResponseFormatError` を送出する。

```python
with RapidWireClient("https://rapidwire.example.com", token=TOKEN) as client:
    for item in client.paginate(client.list_backups):
        client.download_backup(item.name, item.name)
```

バックアップの一覧取得とダウンロードには運営者の勘定のトークンが必要である ([document/operations.md](../document/operations.md#バックアップのダウンロード))。

## メソッド

API のエンドポイントそれぞれに 1 つのメソッドがあり、"トークン" 列は API トークンを送るメソッドを示す。

| メソッド | エンドポイント | トークン |
| --- | --- | --- |
| `get_version` | `GET /v1/version` | なし |
| `get_config` | `GET /v1/config` | なし |
| `get_account` | `GET /v1/accounts/{account_id}` | なし |
| `get_username` | `GET /v1/users/{account_id}/name` | なし |
| `get_user_stats` | `GET /v1/users/{account_id}/stats` | なし |
| `list_balances` | `GET /v1/accounts/{account_id}/balances` | なし |
| `get_balance` | `GET /v1/accounts/{account_id}/balances/{currency_id}` | なし |
| `list_stakes` | `GET /v1/accounts/{account_id}/stakes` | なし |
| `get_stake` | `GET /v1/accounts/{account_id}/stakes/{currency_id}` | なし |
| `get_account_history` | `GET /v1/accounts/{account_id}/history` | なし |
| `list_claims` | `GET /v1/accounts/{account_id}/claims` | なし |
| `list_allowance_history` | `GET /v1/accounts/{account_id}/allowance-history` | なし |
| `get_claim` | `GET /v1/claims/{claim_id}` | なし |
| `get_allowance` | `GET /v1/allowances/{owner_id}/{spender_id}/{currency_id}` | なし |
| `list_currencies` | `GET /v1/currencies` | なし |
| `get_currency_by_symbol` | `GET /v1/currencies/symbol/{symbol}` | なし |
| `get_currency` | `GET /v1/currencies/{currency_id}` | なし |
| `search_history` | `GET /v1/history` | なし |
| `list_executions` | `GET /v1/executions` | なし |
| `get_execution` | `GET /v1/executions/{execution_id}` | なし |
| `get_transfer` | `GET /v1/transfers/{transfer_id}` | なし |
| `transfer` | `POST /v1/transfers` | あり |
| `transfer_from` | `POST /v1/transfers/from` | あり |
| `approve` | `POST /v1/allowances` | あり |
| `create_claim` | `POST /v1/claims` | あり |
| `pay_claim` | `POST /v1/claims/{claim_id}/pay` | あり |
| `cancel_claim` | `POST /v1/claims/{claim_id}/cancel` | あり |
| `deposit_stake` | `POST /v1/stakes/deposit` | あり |
| `withdraw_stake` | `POST /v1/stakes/withdraw` | あり |
| `list_tokens` | `GET /v1/tokens` | あり |
| `revoke_tokens` | `POST /v1/tokens/revoke` | あり |
| `create_currency` | `POST /v1/currencies` | あり |
| `mint` | `POST /v1/currencies/{currency_id}/mint` | あり |
| `burn` | `POST /v1/currencies/{currency_id}/burn` | あり |
| `renounce_minting` | `POST /v1/currencies/{currency_id}/renounce` | あり |
| `request_rate_change` | `POST /v1/currencies/{currency_id}/rate-change` | あり |
| `apply_rate_change` | `POST /v1/currencies/{currency_id}/rate-change/apply` | あり |
| `request_currency_deletion` | `POST /v1/currencies/{currency_id}/delete` | あり |
| `finalize_currency_deletion` | `POST /v1/currencies/{currency_id}/delete/finalize` | あり |
| `enable_delegation` | `POST /v1/currencies/{currency_id}/delegation/enable` | あり |
| `disable_delegation` | `POST /v1/currencies/{currency_id}/delegation/disable` | あり |
| `list_notifications` | `GET /v1/notifications` | あり |
| `allow_notifications` | `POST /v1/notifications/allow` | あり |
| `deny_notifications` | `POST /v1/notifications/deny` | あり |
| `list_discord_permissions` | `GET /v1/guilds/{guild_id}/discord-permissions` | あり |
| `allow_discord` | `POST /v1/guilds/{guild_id}/discord-permissions/allow` | あり |
| `deny_discord` | `POST /v1/guilds/{guild_id}/discord-permissions/deny` | あり |
| `list_pools` | `GET /v1/pools` | なし |
| `get_pair_pools` | `GET /v1/pools/pair/{currency_x}/{currency_y}` | なし |
| `get_pool` | `GET /v1/pools/{pool_id}` | なし |
| `list_positions` | `GET /v1/accounts/{account_id}/pools` | なし |
| `quote_swap` | `POST /v1/swap/quote` | なし |
| `swap` | `POST /v1/swap` | あり |
| `create_pool` | `POST /v1/pools` | あり |
| `add_liquidity` | `POST /v1/pools/liquidity/add` | あり |
| `remove_liquidity` | `POST /v1/pools/liquidity/remove` | あり |
| `get_contract` | `GET /v1/contracts/{account_id}` | なし |
| `get_contract_functions` | `GET /v1/contracts/{account_id}/functions` | なし |
| `list_contract_versions` | `GET /v1/contracts/{account_id}/versions` | なし |
| `get_contract_version` | `GET /v1/contracts/{account_id}/versions/{version}` | なし |
| `download_contract_binary` | `GET /v1/contracts/{account_id}/versions/{version}/binary` | なし |
| `list_storage` | `GET /v1/contracts/{account_id}/storage` | なし |
| `get_storage` | `GET /v1/contracts/{account_id}/storage/{key:path}` | なし |
| `register_contract` | `POST /v1/contracts/{account_id}/versions` | あり |
| `lock_contract` | `POST /v1/contracts/{account_id}/lock` | あり |
| `pause_contract` | `POST /v1/contracts/{account_id}/pause` | あり |
| `resume_contract` | `POST /v1/contracts/{account_id}/resume` | あり |
| `delete_contract` | `POST /v1/contracts/{account_id}/delete` | あり |
| `call_contract` | `POST /v1/contracts/{account_id}/call` | あり |
| `dry_run_contract` | `POST /v1/contracts/{account_id}/dry-run` | あり |
| `list_backups` | `GET /v1/admin/backups` | あり |
| `download_backup`, `stream_backup` | `GET /v1/admin/backups/{name:path}` | あり |
