# RapidWire コントラクトサンプル

このドキュメントは、RapidWire Discordボット用のコントラクトのサンプル集です。これらの例は、スマートコントラクトシステムの様々な機能を示しています。

## コントラクトの仕組み

コントラクトが設定されたユーザーがトランザクションを受信すると、サンドボックス化された環境でコントラクトコードが実行されます。コントラクトは主に2つのオブジェクトにアクセスできます。

- `tx`: 受信するトランザクションの詳細を含むオブジェクトです。
    - `source`: 送信者のユーザーID。
    - `dest`: 受信者（あなた）のユーザーID。
    - `currency`: 転送される通貨のID。
    - `amount`: 転送される通貨の量（整数）。
    - `input_data`: 送信者によってトランザクションに添付されたデータ。
    - `transaction_id`: このトランザクションの一意なID。
- `api`: RapidWireシステムと対話するためのメソッドを提供するオブジェクトです。
    - `get_balance(user_id, currency_id)`: ユーザーの残高を取得します。
    - `get_transaction(tx_id)`: 特定のトランザクションを取得します。
    - `transfer(source, dest, currency, amount)`: 新しい送金を開始します。**注意:** `source`はコントラクトの所有者でなければなりません。
    - `search_transactions(...)`: トランザクションを検索します。
    - `get_currency(currency_id)`: 通貨の詳細を取得します。
    - `create_claim(...)`, `get_claim(...)`, `pay_claim(...)`, `cancel_claim(...)`: 請求を管理します。
    - `execute_contract(destination_id, currency_id, amount, input_data)`: 別のコントラクトを実行します。
    - `get_variable(user_id, key)`: コントラクトに関連付けられた永続的な変数を取得します。
    - `set_variable(key, value)`: コントラクトに関連付けられた永続的な変数を設定します。
- `Cancel`: 受信するトランザクションをキャンセルするために発生させることができる例外です (`raise Cancel("キャンセルの理由")`)。
- `return_message`: トランザクションの送信者にメッセージを返すために設定できる変数です。

---

## サンプルコントラクト

### 1. 自動返信コントラクト

これは、送信者に「ありがとう」というメッセージを送り返す基本的なコントラクトです。

**コード:**
```python
# トランザクションの送信元に送信される返信メッセージを設定します。
return_message = f"{tx.amount} コインありがとうございます！"
```

**説明:**
- このスクリプトは`tx`オブジェクトにアクセスして、受信トランザクションから`amount`を取得します。
- その後、`return_message`変数を設定します。この変数の内容は、送金が正常に処理された後、トランザクションを開始したユーザーに送信されます。

---

### 2. 条件付き拒否コントラクト

このコントラクトは、特定の条件に基づいてトランザクションを拒否する方法を示します。この例では、`input_data`に「fee」が含まれるトランザクションを拒否します。

**コード:**
```python
# input_dataが'fee'かどうかを確認します
if tx.input_data == 'fee':
    # もしそうなら、メッセージ付きでトランザクションをキャンセルします。
    raise Cancel("'fee'がinput_dataのトランザクションは受け付けられません。")

return_message = "トランザクションは承認されました。"
```

**説明:**
- スクリプトは`tx`オブジェクトの`input_data`フィールドをチェックします。
- `input_data`が文字列「fee」と一致する場合、スクリプトは`Cancel`例外を発生させます。
- `Cancel`を発生させると、トランザクションは即座に停止し、提供されたメッセージがキャンセルの理由として送信者に送り返されます。
- 条件が満たされない場合、トランザクションは続行され、確認メッセージが設定されます。

---

### 3. チップ転送コントラクト

このコントラクトは、受け取った資金の一部を自動的に別のユーザーに転送します。これは「開発者税」や貯金口座などに使用できます。

**コード:**
```python
# チップを転送する相手のユーザーID。
# 重要: 123456789012345678を実際のDiscordユーザーIDに置き換えてください。
FORWARD_TO_USER_ID = 123456789012345678

# 転送する金額の割合（例: 10%）
TIP_PERCENTAGE = 0.10

# 転送する金額を計算します
tip_amount = int(tx.amount * TIP_PERCENTAGE)

# 金額が非常に小さい場合でも、最低1単位は転送されるようにします
if tip_amount == 0 and tx.amount > 0:
    tip_amount = 1

# 送信するチップがある場合、それを転送します。
if tip_amount > 0:
    api.transfer(
        source=tx.dest,  # sourceは常にコントラクトの所有者です
        dest=FORWARD_TO_USER_ID,
        currency=tx.currency,
        amount=tip_amount
    )
    return_message = f"ありがとうございます！{tip_amount}がチップとして転送されました。"
else:
    return_message = "トランザクションありがとうございます！"
```

**説明:**
- **設定**: `FORWARD_TO_USER_ID`を、資金を送りたい実際のDiscordユーザーIDに設定する必要があります。`TIP_PERCENTAGE`は必要に応じて調整できます。
- **計算**: スクリプトは、受信した`tx.amount`の`TIP_PERCENTAGE`に基づいて転送する金額を計算します。元の金額がゼロより大きい場合、少なくとも通貨の1単位が送信されることを保証します。
- **API呼び出し**: `api.transfer()`メソッドを使用して、計算された`tip_amount`を送信します。
    - `source`: これは**必ず**`tx.dest`（コントラクトを所有するアカウントのユーザーID）でなければなりません。コントラクトは自身のアカウントからのみ送金を開始できます。
    - `dest`: 資金を転送する相手のユーザーID。
    - `currency`: 受信トランザクションと同じ通貨。
    - `amount`: 計算されたチップの金額。
- **返信メッセージ**: 元の送信者に、彼らのトランザクションの一部が転送されたことを知らせるメッセージが設定されます。

---

### 4. 状態管理コントラクト（カウンター）

このコントラクトは、`api.set_variable`と`api.get_variable`を使用して、永続的な状態を保存する方法を示します。この例では、コントラクトがトランザクションを受信するたびにカウンターをインクリメントします。

**コード:**
```python
# 'tx_count'というキーで保存されている現在のカウントを取得しようとします。
# b''はバイト文字列リテラルです。変数のキーと値はバイト文字列として保存する必要があります。
raw_count = api.get_variable(user_id=tx.dest, key=b'tx_count')

# 変数がまだ設定されていない場合、カウンターを0に初期化します。
if raw_count is None:
    count = 0
else:
    # 保存された値はバイトなので、整数に変換します。
    count = int.from_bytes(raw_count, 'big')

# カウンターをインクリメントします。
count += 1

# 更新されたカウントをバイトに変換して永続ストレージに保存します。
api.set_variable(key=b'tx_count', value=count.to_bytes(8, 'big'))

# これまでに受信したトランザクションの総数を送信者に報告します。
return_message = f"これはあなたの{count}番目のトランザクションです。ありがとうございます！"
```

**説明:**
- **状態の取得**: `api.get_variable(user_id=tx.dest, key=b'tx_count')`を呼び出して、キー`b'tx_count'`に格納されている値を取得します。`user_id`は、変数を所有するユーザーを指定します。この場合はコントラクトの所有者です。
- **初期化**: `raw_count`が`None`の場合（つまり、変数がまだ存在しない場合）、カウンターを`0`に設定します。
- **デコード**: 値が存在する場合、`int.from_bytes()`を使用してバイト文字列を整数に変換します。
- **状態の更新**: カウンターをインクリメントします。
- **状態の保存**: `count.to_bytes()`を使用してカウンターをバイト文字列に変換し、`api.set_variable()`で保存します。これにより、次のトランザクションで値を取得できるようになります。
- **キーと値**: `get_variable`と`set_variable`のキーと値は、バイト文字列（例：`b'my_key'`）である必要があります。

---

### 5. コントラクト間通信

この例は、`api.execute_contract`を使用して、あるコントラクトが別のコントラクトを呼び出す方法を示します。ここでは、「ディスパッチャー」コントラクトが受信した資金の一部を「ロガー」コントラクトに転送します。

#### パートA：ロガーコントラクト

まず、呼び出されるコントラクトを設定する必要があります。この単純なロガーコントラクトは、受信したトランザクションの数を記録します。これは、上記の「状態管理コントラクト」と同様に機能します。

**`LOGGER_USER_ID` に設定するコード:**
```python
# 'logged_tx_count' というキーで保存されている現在のカウントを取得します。
raw_count = api.get_variable(user_id=tx.dest, key=b'logged_tx_count')

count = int.from_bytes(raw_count, 'big') if raw_count else 0
count += 1

# 更新されたカウントを保存します。
api.set_variable(key=b'logged_tx_count', value=count.to_bytes(8, 'big'))

# ログに記録されたことを元のディスパッチャーに（間接的に）通知します。
return_message = f"Logged transaction #{count}. Source: {tx.source}"
```

#### パートB：ディスパッチャーコントラクト

このコントラクトは、トランザクションを受信し、ロガーコントラクトを呼び出し、資金の一部を転送します。

**コード:**
```python
# ロガーコントラクトが設定されているユーザーID。
# 重要: 987654321098765432をロガーコントラクトを持つ実際のDiscordユーザーIDに置き換えてください。
LOGGER_USER_ID = 987654321098765432

# ロガーに転送する金額
FORWARD_AMOUNT = 10

# 元のトランザクションの金額が転送するのに十分であることを確認します
if tx.amount > FORWARD_AMOUNT:
    # 別のコントラクトを実行します。
    # これにより、このコントラクト（tx.dest）からLOGGER_USER_IDに新しいトランザクションが作成されます。
    new_tx, logger_message = api.execute_contract(
        destination_id=LOGGER_USER_ID,
        currency_id=tx.currency,
        amount=FORWARD_AMOUNT,
        input_data=f"forwarded_from:{tx.source}"
    )

    # ロガーコントラクトからの返信メッセージを元の送信者に渡します。
    return_message = f"Successfully forwarded {FORWARD_AMOUNT} to logger. Logger says: '{logger_message}'"
else:
    return_message = "Amount too small to forward."
```

**説明:**
- **セットアップ**: まず、「ロガー」として機能する2番目のアカウントにパートAのコントラクトを設定する必要があります。次に、そのアカウントのユーザーIDをパートBのコントラクトの `LOGGER_USER_ID` 変数に入力します。
- **`execute_contract`の呼び出し**: ディスパッチャーコントラクトは `api.execute_contract` を呼び出します。これにより、現在のコントラクトの所有者 (`tx.dest`) から `LOGGER_USER_ID` への新しい内部トランザクションが生成されます。
- **データフロー**: `input_data` は、元の送信者に関する情報をロガーコントラクトに渡すために使用されます。
- **戻り値**: `execute_contract` は、作成されたトランザクションの辞書と、呼び出されたコントラクトからの `return_message` の2つの値を返します。これにより、呼び出し元のコントラクトは、呼び出されたコントラクトの実行結果に基づいて行動できます。