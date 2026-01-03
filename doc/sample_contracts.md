# RapidWire コントラクトサンプル

このドキュメントは、RapidWire Discordボット用のスマートコントラクトのサンプル集です。

コントラクトは、仮想マシン（VM）上で実行されるJSON形式の命令セット（スクリプト）として記述されます。

## システム変数

VMは実行時に以下のシステム変数を自動的に設定します。

- `_sender`: コントラクトを呼び出したユーザーのID。
- `_self`: コントラクトの所有者（あなた）のユーザーID。
- `_input`: 実行時に渡された入力データ（文字列）。

## 利用可能な操作（Opcode）

JSONオブジェクトのリストとしてスクリプトを記述します。各オブジェクトは `op`（操作名）、`args`（引数リスト）、`out`（結果の格納先変数名、任意）を持ちます。

**計算・論理演算:**
- `add`, `sub`, `mul`, `div`, `mod`: 四則演算と剰余。
- `concat`: 文字列結合。
- `eq`: 等価比較（1または0を返す）。
- `gt`: 大なり比較（1または0を返す）。
- `sha256`: SHA-256ハッシュ計算。
- `random`: ランダム整数の生成。
- `length`: 文字列やリストの長さを取得 (`[obj]`)。
- `slice`: スライス (`[obj, start, stop, step]`)。

**フロー制御:**
- `if`: 条件分岐。`args[0]`が真（非ゼロ）なら`then`ブロック、偽なら`else`ブロックを実行します。
- `exit`: 実行を終了します。
- `cancel`: トランザクションをキャンセルし、実行を巻き戻します。

**アクション:**
- `transfer`: 送金 (`[to, amount, cur]`)。
- `output`: 呼び出し元への返信メッセージを設定 (`[message]`)。
- `store_get`: 変数の取得 (`[key]`)。
- `store_set`: 変数の設定 (`[key, val]`)。
- `execute`: 他のコントラクトの実行 (`[dest, input]`)。
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
- `has_role`: ロール所持判定 (`[user_id, guild_id, role_id]`)。

---

## 実践的サンプルコントラクト

### 1. 初回限定ボーナス（Faucet）

ユーザーがこのコントラクトを初めて呼び出したときに、一度だけ通貨をプレゼントします。`store_int_set` を使って受け取り済みかどうかを管理します。

**コード (JSON):**
```json
[
  {
    "op": "concat",
    "args": ["claimed_", "_sender"],
    "out": "_key"
  },
  {
    "op": "store_int_get",
    "args": ["_key"],
    "out": "_has_claimed"
  },
  {
    "op": "if",
    "args": ["_has_claimed"],
    "then": [
      {
        "op": "cancel",
        "args": ["あなたは既にボーナスを受け取っています。"]
      }
    ]
  },
  {
    "op": "transfer",
    "args": ["_sender", "1000", "1"]
  },
  {
    "op": "store_int_set",
    "args": ["_key", "1"]
  },
  {
    "op": "output",
    "args": ["初回ボーナスとして 10.00 コインを送金しました！"]
  }
]
```

**ポイント:**
- ユーザーIDごとのフラグを作成するために `concat` でキーを動的に生成しています。
- 未受け取りの場合のみ送金を行い、その後フラグを立てて再取得を防ぎます。

---

### 2. 数当てゲーム（High & Low）

ユーザーが入力した数字と、コントラクトが生成したランダムな数字が一致すれば賞金を出します。
ここでは簡易的に「0か1か」を当てるゲームとします。

**使い方:** `/execute_contract <bot_user> 0` または `1`

**コード (JSON):**
```json
[
  {
    "op": "random",
    "args": ["0", "1"],
    "out": "_lucky_num"
  },
  {
    "op": "eq",
    "args": ["_input", "_lucky_num"],
    "out": "_is_win"
  },
  {
    "op": "if",
    "args": ["_is_win"],
    "then": [
      {
        "op": "transfer",
        "args": ["_sender", "500", "1"]
      },
      {
        "op": "output",
        "args": ["おめでとうございます！当たりです！5.00コインを獲得しました。"]
      }
    ],
    "else": [
      {
        "op": "concat",
        "args": ["残念、はずれです。正解は ", "_lucky_num"],
        "out": "_msg"
      },
      {
        "op": "concat",
        "args": ["_msg", " でした。"],
        "out": "_full_msg"
      },
      {
        "op": "output",
        "args": ["_full_msg"]
      }
    ]
  }
]
```

**ポイント:**
- `random` で運試し要素を実装しています。
- `_input` でユーザーの選択を受け取ります。

---

### 3. 自動販売機（ロール販売）

ユーザーからの送金を検証し、Discordのロールを付与します。
**重要:** 先に送金を行い、その「転送ID」をコントラクトに入力する必要があります。

**使い方:**
1. `/transfer <contract_owner> 100` を実行し、転送ID（例: `55`）を控える。
2. `/execute_contract <contract_owner> 55` を実行。

**コード (JSON):**
```json
[
  {
    "op": "concat",
    "args": ["used_tx_", "_input"],
    "out": "_key_used"
  },
  {
    "op": "store_int_get",
    "args": ["_key_used"],
    "out": "_is_used"
  },
  {
    "op": "if",
    "args": ["_is_used"],
    "then": [
      {
        "op": "cancel",
        "args": ["この転送IDは既に使用されています。"]
      }
    ]
  },
  {
    "op": "get_transaction",
    "args": ["_input"],
    "out": "_tx"
  },
  {
    "op": "if",
    "args": ["_tx"],
    "else": [
      {
        "op": "cancel",
        "args": ["指定された転送が見つかりません。"]
      }
    ]
  },
  {
    "op": "attr",
    "args": ["_tx", "dest_id"],
    "out": "_tx_dest"
  },
  {
    "op": "eq",
    "args": ["_tx_dest", "_self"],
    "out": "_is_dest_ok"
  },
  {
    "op": "if",
    "args": ["_is_dest_ok"],
    "else": [
      {
        "op": "cancel",
        "args": ["この送金の宛先は私ではありません。"]
      }
    ]
  },
  {
    "op": "attr",
    "args": ["_tx", "amount"],
    "out": "_tx_amount"
  },
  {
    "op": "gt",
    "args": ["_tx_amount", "9999"],
    "out": "_is_amount_ok"
  },
  {
    "op": "if",
    "args": ["_is_amount_ok"],
    "else": [
      {
        "op": "cancel",
        "args": ["金額が不足しています。100.00コイン以上必要です。"]
      }
    ]
  },
  {
    "op": "store_int_set",
    "args": ["_key_used", "1"]
  },
  {
    "op": "discord_role_add",
    "args": ["_sender", "123456789012345678", "987654321098765432"]
  },
  {
    "op": "output",
    "args": ["購入ありがとうございます！ロールを付与しました。"]
  }
]
```

**ポイント:**
- `get_transaction` と `attr` を使って、過去の取引の内容（宛先、金額）を厳密にチェックしています。
- 「使用済みフラグ」を立てることで、同じ転送IDの使い回し（リプレイ攻撃）を防いでいます。
- `discord_role_add` で外部への作用（ロール付与）を行っています。※IDはダミーです。

---

### 4. パブリックゲストブック（掲示板）

ユーザーが送信したメッセージを保存し、誰でも読めるようにします。

**使い方:**
- 書き込み: `/execute_contract <user> "Hello!"`
- 読み込み: `/execute_contract <user> read`

**コード (JSON):**
```json
[
  {
    "op": "eq",
    "args": ["_input", "read"],
    "out": "_is_read_mode"
  },
  {
    "op": "if",
    "args": ["_is_read_mode"],
    "then": [
      {
        "op": "store_str_get",
        "args": ["guestbook"],
        "out": "_content"
      },
      {
        "op": "output",
        "args": ["_content"]
      },
      {
        "op": "exit"
      }
    ]
  },
  {
    "op": "store_str_get",
    "args": ["guestbook"],
    "out": "_current"
  },
  {
    "op": "concat",
    "args": ["_current", "\n"],
    "out": "_temp"
  },
  {
    "op": "concat",
    "args": ["_temp", "_sender"],
    "out": "_temp2"
  },
  {
    "op": "concat",
    "args": ["_temp2", ": "],
    "out": "_temp3"
  },
  {
    "op": "concat",
    "args": ["_temp3", "_input"],
    "out": "_new_content"
  },
  {
    "op": "store_str_set",
    "args": ["guestbook", "_new_content"]
  },
  {
    "op": "output",
    "args": ["ゲストブックに書き込みました。"]
  }
]
```

**ポイント:**
- `_input` の内容によって処理を分岐させています（コマンドパターン）。
- `concat` を繰り返して文字列を整形・追記しています。

---

### 5. ロール限定配給 (VIP Bonus)

特定のDiscordロールを持っているユーザーにのみ、特別なボーナスを付与します。

**使い方:** `/execute_contract <bot_user>`

**コード (JSON):**
```json
[
  {
    "op": "has_role",
    "args": ["_sender", "123456789012345678", "987654321098765432"],
    "out": "_is_vip"
  },
  {
    "op": "if",
    "args": ["_is_vip"],
    "then": [
      {
        "op": "transfer",
        "args": ["_sender", "10000", "1"]
      },
      {
        "op": "output",
        "args": ["VIPボーナスとして 100.00 コインを送金しました！いつもありがとうございます。"]
      }
    ],
    "else": [
      {
        "op": "output",
        "args": ["申し訳ありませんが、このボーナスはVIP会員限定です。"]
      }
    ]
  }
]
```

**ポイント:**
- `has_role` を使用して、実行者が特定のギルド（サーバー）で特定のロールを持っているかを判定しています。
- これにより、Discordのコミュニティ活動と連携したトークンエコノミーを構築できます。
