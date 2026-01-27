# RapidWire コントラクト SDK ドキュメント

RapidWireでは、Pythonを使用してスマートコントラクトを開発できます。
開発には `RapidWire/sdk.py` を使用してコードを記述し、`RapidWire/compiler.py` を使用してコンパイルします。

## 開発の準備

コントラクトコードを作成するファイル（例: `my_contract.py`）の先頭で、SDKをインポートします。
これにより、開発環境（IDE）での型ヒントや自動補完が有効になります。

```python
from RapidWire.sdk import *

def main():
    # ここにロジックを記述
    ...
```

## コントラクトの構造

すべてのコントラクトロジックは `main` 関数内に記述する必要があります。
コンパイラは `main` 関数内のコードのみを処理し、それ以外のグローバルスコープのコードは無視されます。

## コンパイルと実行

作成したPythonファイルは、以下のコマンドでRapidWire VMが理解できる命令セット（JSON）にコンパイルします。

```bash
python3 RapidWire/compiler.py my_contract.py
```

生成されたJSONファイルの内容をコントラクトとしてデプロイします。

## システム変数

`main` 関数内では、以下のグローバル変数にアクセスできます。

| 変数名 | 型 | 説明 |
| :--- | :--- | :--- |
| `sender` | `int` | コントラクトを呼び出したユーザーのID |
| `self_id` | `int` | コントラクト自身の所有者ID |
| `input_data` | `str` | 実行時に渡された入力データ文字列 |
| `storage` | `Dict[str, str]` | 永続的な文字列ストレージ（キーと値は文字列）。整数を代入した場合は自動的に文字列に変換されます。 |

### ストレージの使用例

```python
# データの保存
storage['last_msg'] = input_data
storage['count'] = int(storage['count']) + 1

# データの取得
msg = storage['last_msg']
```

## データ構造

SDKでは以下のクラスが定義されており、関数の戻り値として使用されます。
これらのオブジェクトの属性にはドット記法（例: `tx.amount`）でアクセスできます。

### `Currency`
通貨情報を表します。

- `currency_id` (`int`): 通貨ID
- `name` (`str`): 通貨名
- `symbol` (`str`): シンボル
- `issuer_id` (`int`): 発行者ID
- `supply` (`int`): 総供給量
- `minting_renounced` (`bool`): 新規発行権限が放棄されているか
- `hourly_interest_rate` (`int`): 時次金利
- `new_hourly_interest_rate` (`int`): 変更予定の時次金利（未設定の場合はNone）
- `rate_change_requested_at` (`int`): 金利変更リクエストのタイムスタンプ
- `delete_requested_at` (`int`): 削除リクエストのタイムスタンプ

### `Transaction`
トランザクション情報を表します。

- `transfer_id` (`int`): 送金ID
- `execution_id` (`int`): 関連するコントラクト実行ID（存在する場合）
- `source_id` (`int`): 送信元ID
- `dest_id` (`int`): 送信先ID
- `currency_id` (`int`): 通貨ID
- `amount` (`int`): 金額
- `timestamp` (`int`): タイムスタンプ

### `Claim`
請求情報を表します。

- `claim_id` (`int`): 請求ID
- `claimant_id` (`int`): 請求作成者ID
- `payer_id` (`int`): 請求先ID
- `currency_id` (`int`): 通貨ID
- `amount` (`int`): 金額
- `status` (`str`): ステータス
- `created_at` (`int`): 作成日時
- `description` (`str`): 説明

## 関数リファレンス

### 基本操作

- `output(msg: str) -> None`: 送信者にメッセージを返信します。
- `cancel(reason: str) -> None`: 処理をキャンセルし、変更をロールバックします。
- `exit() -> None`: 処理を正常終了します。

### 通貨・送金・取引

- `transfer(to: int, amount: int, currency: int) -> None`: 通貨を送金します。
- `get_balance(user: int, currency: int) -> int`: ユーザーの残高を取得します。
- `get_currency(currency_id: int) -> Currency`: 通貨情報を取得します。
- `approve(spender: int, amount: int, currency: int) -> None`: 第三者による送金を許可します。
- `transfer_from(sender: int, recipient: int, amount: int, currency: int) -> Transaction`: 許可された範囲で代理送金を行います。
- `get_allowance(owner: int, spender: int, currency: int) -> int`: 指定したユーザーがスペンダーに対して許可している送金可能額を取得します。
- `get_transaction(tx_id: int) -> Transaction`: トランザクション情報を取得します。
- `swap(from_currency_id: int, to_currency_id: int, amount: int) -> Transaction`: 通貨を交換します。
- `add_liquidity(currency_a_id: int, currency_b_id: int, amount_a: int, amount_b: int) -> int`: 流動性を提供します。
- `remove_liquidity(currency_a_id: int, currency_b_id: int, shares: int) -> list[int]`: 流動性を削除します。

### 請求

- `create_claim(payer: int, amount: int, currency: int, desc: str = None) -> Claim`: 請求を作成します。
- `pay_claim(claim_id: int) -> Transaction`: 請求を支払います。
- `cancel_claim(claim_id: int) -> Claim`: 請求をキャンセルします。

### その他

- `sha256(val: str) -> str`: 文字列のSHA-256ハッシュを計算します。
- `random(min_val: int, max_val: int) -> int`: 範囲内のランダムな整数を生成します。
- `concat(*args: str) -> str`: 文字列を結合します（複数引数可）。
- `length(val: Any) -> int`: オブジェクトの長さ（文字数など）を返します。Python標準の `len()` も使用できます。
- `split(val: str, separator: str) -> list[str]`: 文字列を指定した区切り文字で分割します。
- `to_str(val: int) -> str`: 整数を文字列に変換します。
- `to_int(val: str) -> int`: 文字列を整数に変換します。
- `now() -> int`: 現在のUnixタイムスタンプを取得します。
- `execute(destination_id: int, input_data: str = None) -> str`: 他のコントラクトを実行します。

### スライス

文字列などのシーケンス型に対して、Python標準のスライス構文を使用できます。

- `val[start:end]`: 部分文字列の取得
- `val[start:end:step]`: ステップ指定による取得

例:
```python
s = "Hello World"
sub = s[0:5] # "Hello"
rev = s[::-1] # "dlroW olleH"
```

### Discord連携 (要権限)

- `discord_send(guild_id: int, channel_id: int, message: str) -> int`: Discordチャンネルにメッセージを送信します。
- `discord_role_add(user_id: int, guild_id: int, role_id: int) -> int`: ユーザーにロールを付与します。

- `has_role(user_id: int, guild_id: int, role_id: int) -> bool`: ユーザーが指定されたギルドで特定のロールを持っているかを確認します。

*注意: この関数を使用するには、コントラクト所有者がそのギルドに対するDiscord操作権限を持っている必要があります。*

## 制限事項

RapidWireのコントラクトはPythonのサブセットで動作します。

- **構文制限**: `if`、`while`、代入、式評価がサポートされています。
- **関数定義不可**: `main` 以外の関数定義はサポートされていません。
- **インポート不可**: `RapidWire.sdk` 以外のモジュールをインポートしても、コンパイル後のコードには影響しませんが、VM上では利用できません。
