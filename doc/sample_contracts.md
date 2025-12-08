# RapidWire コントラクトサンプル

このドキュメントは、RapidWire Discordボット用のコントラクトのサンプル集です。これらの例は、スマートコントラクトシステムの様々な機能を示しています。

コントラクトは、仮想マシン（VM）上で実行されるJSON形式の命令セット（スクリプト）として記述されます。

## システム変数

VMは実行時に以下のシステム変数を自動的に設定します。

- `_sender`: 送信者のユーザーID。
- `_self`: 受信者（あなた）のユーザーID。
- `_input`: 送信者によってトランザクションに添付されたデータ。

## 利用可能な操作（Opcode）

JSONオブジェクトのリストとしてスクリプトを記述します。各オブジェクトは `op`（操作名）、`args`（引数リスト）、`out`（結果の格納先変数名、任意）を持ちます。

**計算・論理演算:**
- `add`, `sub`, `mul`, `div`, `mod`: 四則演算と剰余。
- `concat`: 文字列結合。
- `eq`: 等価比較（1または0を返す）。
- `gt`: 大なり比較（1または0を返す）。
- `hash`: SHA-256ハッシュ計算。
- `random`: ランダム整数の生成。

**フロー制御:**
- `if`: 条件分岐。`args[0]`が真（非ゼロ）なら`then`ブロック、偽なら`else`ブロックを実行します。
- `exit`: 実行を終了します。
- `cancel`: トランザクションをキャンセルします（メッセージを指定可能）。

**アクション:**
- `transfer`: 送金 (`[to, amount, cur]`)。
- `reply`: 送信者への返信メッセージを設定 (`[message]`)。
- `store_str_get`: 文字列変数の取得 (`[key]`)。
- `store_int_get`: 整数変数の取得 (`[key]`)。
- `store_str_set`: 文字列変数の設定 (`[key, val]`)。
- `store_int_set`: 整数変数の設定 (`[key, val]`)。
- `exec`: 他のコントラクトの実行 (`[dest, input]`)。
- `getitem`: リストや辞書、タプルからの要素取得 (`[obj, index]`)。
- `attr`: オブジェクトの属性取得 (`[obj, prop]`)。
- `get_balance`: 残高取得 (`[user, cur]`)。
- `get_currency`: 通貨情報取得 (`[cur_id]`)。
- `get_transaction`: トランザクション情報取得 (`[tx_id]`)。
- `create_claim`: 請求作成 (`[payer, amount, cur, desc]`)。
- `pay_claim`: 請求支払 (`[claim_id]`)。
- `cancel_claim`: 請求キャンセル (`[claim_id]`)。
- `discord_send`: Discordメッセージ送信 (`[guild_id, channel_id, message]`)。
- `discord_role_add`: Discordロール付与 (`[user_id, guild_id, role_id]`)。

---

## サンプルコントラクト

### 1. 自動返信コントラクト

これは、送信者に「ありがとう」というメッセージを送り返す基本的なコントラクトです。

**コード (JSON):**
```json
[
  {
    "op": "reply",
    "args": ["コインありがとうございます！"]
  }
]
```

**説明:**
- `reply`を使って、返信メッセージを設定します。

---

### 2. 条件付き拒否コントラクト

このコントラクトは、特定の条件に基づいてトランザクションを拒否する方法を示します。この例では、`_input`に「fee」が含まれるトランザクションを拒否します。

**コード (JSON):**
```json
[
  {
    "op": "eq",
    "args": ["_input", "fee"],
    "out": "_is_fee"
  },
  {
    "op": "if",
    "args": ["_is_fee"],
    "then": [
      {
        "op": "cancel",
        "args": ["'fee'がinput_dataのトランザクションは受け付けられません。"]
      }
    ]
  },
  {
    "op": "reply",
    "args": ["トランザクションは承認されました。"]
  }
]
```

**説明:**
- `eq`で`_input`が「fee」と等しいかチェックし、結果を`_is_fee`に格納します。
- `if`で`_is_fee`を評価します。真の場合、`cancel`を実行してトランザクションを拒否します。
- 条件が満たされない場合、承認メッセージを設定して終了します。

---

### 3. チップ転送コントラクト

このコントラクトは、固定額のチップを自動的に別のユーザーに転送します。

**コード (JSON):**
```json
[
  {
    "op": "transfer",
    "args": ["123456789012345678", "100", "1"]
  },
  {
    "op": "reply",
    "args": ["ありがとうございます！1.00コインがチップとして転送されました。"]
  }
]
```

**説明:**
- `transfer`オペレーションで指定したユーザーIDに100（小数点以下2桁の場合1.00コイン）を送金します。

---

### 4. 状態管理コントラクト（カウンター）

`store_int_get`と`store_int_set`を使用して、永続的な状態（カウンター）を管理します。

**コード (JSON):**
```json
[
  {
    "op": "store_int_get",
    "args": ["tx_count"],
    "out": "_count"
  },
  {
    "op": "add",
    "args": ["_count", "1"],
    "out": "_new_count"
  },
  {
    "op": "store_int_set",
    "args": ["tx_count", "_new_count"]
  },
  {
    "op": "concat",
    "args": ["これはあなたの", "_new_count"],
    "out": "_msg_1"
  },
  {
    "op": "concat",
    "args": ["_msg_1", "番目のトランザクションです。ありがとうございます！"],
    "out": "_msg"
  },
  {
    "op": "reply",
    "args": ["_msg"]
  }
]
```

**説明:**
- `store_int_get`で現在のカウントを取得します（未設定時は0）。
- カウントを1増やし、`store_int_set`で保存します。
- 更新されたカウントを含むメッセージを返信します。

---

### 5. コントラクト間通信

`exec`を使用して、他のコントラクトを実行します。

#### パートA：ロガーコントラクト (JSON)

このコントラクトは呼び出されると、カウントを増やし、ログメッセージを返します。

```json
[
  {
    "op": "store_int_get",
    "args": ["logged_tx_count"],
    "out": "_count"
  },
  {
    "op": "add",
    "args": ["_count", "1"],
    "out": "_new_count"
  },
  {
    "op": "store_int_set",
    "args": ["logged_tx_count", "_new_count"]
  },
  {
    "op": "concat",
    "args": ["Logged transaction #", "_new_count"],
    "out": "_msg_1"
  },
  {
    "op": "concat",
    "args": [". Source: ", "_sender"],
    "out": "_msg_2"
  },
  {
    "op": "concat",
    "args": ["_msg_1", "_msg_2"],
    "out": "_return_msg"
  },
  {
    "op": "reply",
    "args": ["_return_msg"]
  }
]
```

#### パートB：ディスパッチャーコントラクト (JSON)

```json
[
  {
    "op": "concat",
    "args": ["forwarded_from:", "_sender"],
    "out": "_input_data"
  },
  {
    "op": "exec",
    "args": ["987654321098765432", "_input_data"],
    "out": "_exec_result"
  },
  {
    "op": "concat",
    "args": ["Successfully forwarded to logger. Logger says: '", "_exec_result"],
    "out": "_msg_part"
  },
  {
    "op": "concat",
    "args": ["_msg_part", "'"],
    "out": "_msg"
  },
  {
    "op": "reply",
    "args": ["_msg"]
  }
]
```

**説明:**
- ディスパッチャーは`exec`を使用してロガーコントラクト（ID: `987654321098765432`）を実行します。
- `exec`の戻り値（ロガーの`reply`メッセージ）を受け取ります。
