# RapidWire コントラクト SDK ドキュメント

RapidWireでは、Pythonを使用してスマートコントラクトを作成できます。コントラクトはサンドボックス化された環境で実行され、特定の関数や変数にアクセスできます。

## 概要

コントラクトは、`/execute_contract` コマンドで明示的に呼び出されたとき、またはAPIを通じて実行リクエストが行われたときに実行されます。
`RapidWire/sdk.py` で定義されている関数や変数を利用して、ロジックを記述します。

## システム変数

コントラクト実行時に、以下の変数が自動的に設定されます。

- **`sender`** (`int`): コントラクトの呼び出し元のユーザーID。
- **`self_id`** (`int`): コントラクト所有者（あなた）のユーザーID。
- **`input_data`** (`str`): 実行時に渡された入力データ。
- **`storage_str`** (`Dict[str, str]`): 文字列を保存できる永続ストレージ。辞書としてアクセスします。
- **`storage_int`** (`Dict[str, int]`): 整数を保存できる永続ストレージ。辞書としてアクセスします。

## 利用可能な関数

### 基本操作

- **`reply(msg: str) -> None`**
  - 送信者にメッセージを返信します。

- **`cancel(reason: str) -> None`**
  - トランザクションをキャンセルし、理由を送信者に通知します。この関数を呼び出すと、送金などの操作はロールバックされます。

- **`exit() -> None`**
  - コントラクトの実行を正常に終了します。

### 通貨・送金操作

- **`transfer(to: int, amount: int, currency: int) -> None`**
  - 指定したユーザーに通貨を送金します。
  - `to`: 送金先のユーザーID。
  - `amount`: 金額（最小単位の整数値）。
  - `currency`: 通貨ID。

- **`get_balance(user: int, currency: int) -> int`**
  - 指定したユーザーの残高を取得します。

- **`get_currency(currency_id: int) -> Any`**
  - 通貨情報を取得します。

- **`approve(spender: int, amount: int, currency: int) -> None`**
  - 指定したユーザー（spender）に対して、あなたの口座から引き出す許可を与えます。

- **`transfer_from(sender: int, recipient: int, amount: int, currency: int) -> Any`**
  - `approve`で許可された範囲内で、他人の口座から送金を行います。

- **`get_transaction(tx_id: int) -> Any`**
  - 指定したIDのトランザクション情報を取得します。

### 請求操作

- **`create_claim(payer: int, amount: int, currency: int, desc: str = None) -> Any`**
  - 指定したユーザーに対して請求を作成します。

- **`pay_claim(claim_id: int) -> Any`**
  - 指定した請求を支払います。

- **`cancel_claim(claim_id: int) -> Any`**
  - 指定した請求をキャンセルします。

### コントラクト間連携

- **`execute_contract(destination_id: int, input_data: str = None) -> str`**
  - 他のユーザーのコントラクトを実行します。実行結果（標準出力など）が文字列として返されます。

### ユーティリティ

- **`sha256(val: str) -> str`**
  - 文字列のSHA-256ハッシュを計算します。

- **`random(min_val: int, max_val: int) -> int`**
  - 指定した範囲のランダムな整数を生成します。

- **`concat(a: str, b: str) -> str`**
  - 2つの文字列を結合します。

### Discord連携 (要権限)

これらの関数を使用するには、サーバー管理者によってDiscord操作権限が付与されている必要があります。

- **`discord_send(guild_id: int, channel_id: int, message: str) -> int`**
  - 指定したDiscordチャンネルにメッセージを送信します。

- **`discord_role_add(user_id: int, guild_id: int, role_id: int) -> int`**
  - 指定したユーザーにDiscordロールを付与します。

## 注意事項

- コントラクトはPythonのサブセットで動作します。`import`文やファイル操作などの危険な操作は制限されています。
- ループや再帰には制限があり、計算コスト（ガスのようなもの）が上限を超えると実行が強制終了されます。
- `amount`などの金額は、小数点以下の桁数を考慮した整数値（例: 1.00コインで小数点以下2桁の場合、`100`）で扱われます。
