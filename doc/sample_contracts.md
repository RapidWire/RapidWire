# RapidWire コントラクトサンプル

このドキュメントは、RapidWire Discordボット用のコントラクトのサンプル集です。これらの例は、スマートコントラクトシステムの様々な機能を示しています。

## コントラクトの仕組み

コントラクトが設定されたユーザーがトランザクションを受信すると、サンドボックス化されたVM環境でコントラクトコードが実行されます。コントラクトはJSON形式の命令セット（スクリプト）として記述されます。

### システム変数

VMは実行時に以下のシステム変数を自動的に設定します。

- `_tx_source`: 送信者のユーザーID。
- `_tx_dest`: 受信者（あなた）のユーザーID。
- `_tx_input`: 送信者によってトランザクションに添付されたデータ。

### 利用可能な操作（Opcode）

JSONオブジェクトのリストとしてスクリプトを記述します。各オブジェクトは `op`（操作名）、`args`（引数リスト）、`out`（結果の格納先変数名、任意）を持ちます。

**計算・論理演算:**
- `add`, `sub`, `mul`, `div`, `mod`: 四則演算と剰余。
- `concat`: 文字列結合。
- `eq`: 等価比較（1または0を返す）。
- `gt`: 大なり比較（1または0を返す）。

**フロー制御:**
- `if`: 条件分岐。`args[0]`が真（非ゼロ）なら`then`ブロック、偽なら`else`ブロックを実行します。
- `exit`: 実行を終了します。
- `cancel`: トランザクションをキャンセルします（メッセージを指定可能）。

**アクション:**
- `transfer`: 送金 (`[to, amount, cur]`)。
- `reply`: 送信者への返信メッセージを設定 (`[message]`)。
- `store_get`: 変数の取得 (`[key]`)。
- `store_set`: 変数の設定 (`[key, val]`)。
- `store_get_other`: 他者の変数取得 (`[user_id, key]`)。
- `exec`: 他のコントラクトの実行 (`[dest, input]`)。
- `getitem`: リストや辞書、タプルからの要素取得 (`[obj, index]`)。

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

このコントラクトは、特定の条件に基づいてトランザクションを拒否する方法を示します。この例では、`input_data`に「fee」が含まれるトランザクションを拒否します。

**コード (JSON):**
```json
[
  {
    "op": "eq",
    "args": ["_tx_input", "fee"],
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
- `eq`で`_tx_input`が「fee」と等しいかチェックし、結果を`_is_fee`に格納します。
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
    "args": ["123456789012345678", "1", "1"]
  },
  {
    "op": "reply",
    "args": ["ありがとうございます！1コインがチップとして転送されました。"]
  }
]
```

**説明:**
- `transfer`オペレーションで指定したユーザーIDに1コイン（通貨ID=1）のチップを送金します。

---

### 4. 状態管理コントラクト（カウンター）

`store_get`と`store_set`を使用して、永続的な状態（カウンター）を管理します。

**コード (JSON):**
```json
[
  {
    "op": "store_get",
    "args": ["tx_count"],
    "out": "_count"
  },
  {
    "op": "add",
    "args": ["_count", "1"],
    "out": "_new_count"
  },
  {
    "op": "store_set",
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
- `store_get`で現在のカウントを取得します（未設定時は0）。
- カウントを1増やし、`store_set`で保存します。
- 更新されたカウントを含むメッセージを返信します。

---

### 5. コントラクト間通信

`exec`を使用して、他のコントラクトを実行します。

#### パートA：ロガーコントラクト (JSON)

```json
[
  {
    "op": "store_get",
    "args": ["logged_tx_count"],
    "out": "_count"
  },
  {
    "op": "add",
    "args": ["_count", "1"],
    "out": "_new_count"
  },
  {
    "op": "store_set",
    "args": ["logged_tx_count", "_new_count"]
  },
  {
    "op": "concat",
    "args": ["Logged transaction #", "_new_count"],
    "out": "_msg_1"
  },
  {
    "op": "concat",
    "args": [". Source: ", "_tx_source"],
    "out": "_msg_2"
  },
  {
    "op": "concat",
    "args": ["_msg_1", "_msg_2"],
    "out": "_msg"
  },
  {
    "op": "reply",
    "args": ["_msg"]
  }
]
```

#### パートB：ディスパッチャーコントラクト (JSON)

```json
[
  {
    "op": "concat",
    "args": ["forwarded_from:", "_tx_source"],
    "out": "_input_data"
  },
  {
    "op": "exec",
    "args": ["987654321098765432", "_input_data"],
    "out": "_exec_result"
  },
  {
    "op": "getitem",
    "args": ["_exec_result", "1"],
    "out": "_logger_message"
  },
  {
    "op": "concat",
    "args": ["Successfully forwarded to logger. Logger says: '", "_logger_message"],
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
- `exec`の戻り値から`getitem`を使用してメッセージ（インデックス1）を取り出します。
