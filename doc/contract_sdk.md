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
| `storage_str` | `Dict[str, str]` | 永続的な文字列ストレージ（キーと値は文字列） |
| `storage_int` | `Dict[str, int]` | 永続的な整数ストレージ（キーは文字列、値は整数） |

### ストレージの使用例

```python
# データの保存
storage_str['last_msg'] = input_data
storage_int['count'] = storage_int['count'] + 1

# データの取得
msg = storage_str['last_msg']
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
- `daily_interest_rate` (`int`): 日次金利
- 他

### `Transaction`
トランザクション情報を表します。

- `transfer_id` (`int`): 送金ID
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

### 通貨・送金

- `transfer(to: int, amount: int, currency: int) -> None`: 通貨を送金します。
- `get_balance(user: int, currency: int) -> int`: ユーザーの残高を取得します。
- `get_currency(currency_id: int) -> Currency`: 通貨情報を取得します。
- `approve(spender: int, amount: int, currency: int) -> None`: 第三者による送金を許可します。
- `transfer_from(sender: int, recipient: int, amount: int, currency: int) -> Transaction`: 許可された範囲で代理送金を行います。
- `get_transaction(tx_id: int) -> Transaction`: トランザクション情報を取得します。

### 請求

- `create_claim(payer: int, amount: int, currency: int, desc: str = None) -> Claim`: 請求を作成します。
- `pay_claim(claim_id: int) -> Transaction`: 請求を支払います。
- `cancel_claim(claim_id: int) -> Claim`: 請求をキャンセルします。

### その他

- `sha256(val: str) -> str`: 文字列のSHA-256ハッシュを計算します。
- `random(min_val: int, max_val: int) -> int`: 範囲内のランダムな整数を生成します。
- `concat(a: str, b: str) -> str`: 文字列を結合します。
- `length(val: Any) -> int`: オブジェクトの長さ（文字数など）を返します。Python標準の `len()` も使用できます。
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

## 制限事項

RapidWireのコントラクトはPythonのサブセットで動作します。

- **ループ不可**: `for` や `while` などのループ構文はサポートされていません。
- **構文制限**: `if`、代入、式評価のみがサポートされています。
- **関数定義不可**: `main` 以外の関数定義はサポートされていません。
- **インポート不可**: `RapidWire.sdk` 以外のモジュールをインポートしても、コンパイル後のコードには影響しませんが、VM上では利用できません。
