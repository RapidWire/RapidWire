# SDK リファレンス

コントラクト内で使用できるシステム変数、関数、および制限事項の一覧です。

## システム変数

これらは `main()` 関数内で自動的に利用可能な変数です。

| 変数名 | 型 | 説明 |
| :--- | :--- | :--- |
| `sender` | `int` | コントラクトを実行したユーザーのDiscord ID（または呼び出し元のコントラクト所有者ID）。 |
| `self_id` | `int` | このコントラクトの所有者（自分）のDiscord ID。 |
| `input_data` | `str` | 実行時に渡された引数データ。指定がない場合は空文字またはNone。 |
| `storage` | `Dict[str, str]` | 永続ストレージ。キーと値はすべて文字列として保存されます。 |

## データ操作・ユーティリティ

### `output(message: str) -> None`
実行結果としてメッセージを出力します。これはDiscord上でユーザーへの返信として表示されます。

### `sha256(val: str) -> str`
文字列のSHA-256ハッシュ値を計算して返します。

### `random(min_val: int, max_val: int) -> int`
指定された範囲（`min` 以上 `max` 以下）のランダムな整数を返します。

### `concat(*args) -> str`
複数の文字列（または数値）を結合して1つの文字列にします。
`+` 演算子でも結合可能ですが、`concat` は型変換を自動で行います。

### `length(obj) -> int`
文字列やリストの長さを返します。Python標準の `len()` も使用可能です。

### `split(val: str, separator: str) -> List[str]`
文字列を区切り文字で分割し、リストとして返します。

### `to_str(val) -> str`
値を文字列に変換します。

### `to_int(val) -> int`
値を整数に変換します。

### `now() -> int`
現在のUnixタイムスタンプ（秒）を返します。

## 経済・取引操作

これらの操作は実際に資産を動かします。

### `transfer(to: int, amount: int, currency: int) -> None`
**自分のウォレット**から指定した相手へ送金します。
残高不足の場合はエラーとなり、トランザクション全体がロールバックされます。

### `get_balance(user: int, currency: int) -> int`
指定したユーザーの指定した通貨の残高を取得します。

### `approve(spender: int, amount: int, currency: int) -> None`
指定した相手（spender）に対して、自分のウォレットから引き出し可能な額（Allowance）を設定します。

### `transfer_from(sender: int, recipient: int, amount: int, currency: int) -> Transaction`
`approve` で許可されている範囲内で、第三者のウォレットから送金を行います。

### `get_allowance(owner: int, spender: int, currency: int) -> int`
現在の許可額（Allowance）を取得します。

### `swap(from_currency_id: int, to_currency_id: int, amount: int) -> Transaction`
DEXを使用して通貨を交換します。

### `create_claim(payer: int, amount: int, currency: int, desc: str) -> Claim`
指定した相手に対して請求書を作成します。

## Discord連携

これらの関数は、コントラクト所有者が対象のDiscordサーバーで適切な権限を持ち、かつ管理者が許可している場合のみ動作します。

### `discord_send(guild_id: int, channel_id: int, message: str) -> int`
指定したチャンネルにメッセージを送信します。成功すると1、失敗すると0を返します。

### `discord_role_add(user_id: int, guild_id: int, role_id: int) -> int`
指定したユーザーにロールを付与します。

### `has_role(user_id: int, guild_id: int, role_id: int) -> bool`
ユーザーが特定のロールを持っているか確認します。

## 制御フロー

### `exit() -> None`
コントラクトの実行を正常終了します。これ以降の行は実行されません。

### `cancel(reason: str) -> None`
実行を**異常終了**させます。
それまでに行われた変更（送金やストレージの書き込みなど）は**すべて取り消されます（ロールバック）**。
`reason` はエラーメッセージとしてユーザーに表示されます。

## 構文の制限

RapidWireのVMはPythonの完全な実装ではありません。以下の制限があります。

- **関数定義**: `def` は `main` 以外使用できません。
- **クラス定義**: `class` は使用できません。
- **インポート**: `import` は無視されます（機能しません）。
- **ループ**: `while` は使用できますが、`for` は部分的なサポートまたは非サポートの場合があります（基本的には `while` を推奨）。
- **例外処理**: `try-except` は使用できません。
