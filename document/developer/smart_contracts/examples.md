# コントラクト・サンプル集

実用的なスマートコントラクトの例を紹介します。これらをコピーして、自分のサーバーに合わせて調整して使用してください。

## 1. 自動ロール販売機

特定の金額を送金してこのコントラクトを実行すると、Discordのロールを自動的に付与します。

```python
from RapidWire.sdk import *

# 設定
PRICE = 100               # 価格
CURRENCY_ID = 1           # 支払いに使う通貨ID
ROLE_ID = 9876543210      # 付与するロールID
GUILD_ID = 1234567890     # サーバーID

def main():
    # 1. すでにロールを持っているか確認
    if has_role(sender, GUILD_ID, ROLE_ID):
        output("あなたは既にこのロールを持っています。")
        exit()

    # 2. 支払いを確認する
    # コントラクトを実行する前に、ユーザーは approve するか、
    # あるいは transfer_from を使ってここで引き落とすロジックにする必要があります。
    # ここでは「transfer_from」を使って、実行者の財布から引き落とします。

    # まず、Allowance（許可額）があるかチェック（UXのため）
    allowance = get_allowance(sender, self_id, CURRENCY_ID)
    if allowance < PRICE:
        output(concat("エラー: ロールを購入するには、まず /approve で ", to_str(PRICE), " 以上の使用を許可してください。"))
        exit()

    # 送金実行 (ユーザー -> コントラクト所有者)
    transfer_from(sender, self_id, PRICE, CURRENCY_ID)

    # 3. ロールを付与
    success = discord_role_add(sender, GUILD_ID, ROLE_ID)

    if success:
        output("購入ありがとうございます！ロールを付与しました。")
    else:
        # ロール付与に失敗した場合、お金を返してキャンセルする
        cancel("ロールの付与に失敗しました。管理者に連絡してください。")
```

## 2. コイン投げギャンブル

50%の確率で賭け金が2倍になって返ってきます。

```python
from RapidWire.sdk import *

# 設定
CURRENCY_ID = 1
MIN_BET = 10
MAX_BET = 1000

def main():
    # 入力データから賭け金を読み取る（数値であることを期待）
    bet_amount = to_int(input_data)

    if bet_amount < MIN_BET:
        output(concat("賭け金が少なすぎます。最小: ", to_str(MIN_BET)))
        exit()

    if bet_amount > MAX_BET:
        output(concat("賭け金が多すぎます。最大: ", to_str(MAX_BET)))
        exit()

    # 胴元（コントラクト所有者）の残高チェック
    # 勝った場合に払えるだけのお金があるか？
    my_balance = get_balance(self_id, CURRENCY_ID)
    if my_balance < bet_amount:
        output("現在、賞金の準備ができていません。")
        exit()

    # ユーザーから賭け金を集める
    transfer_from(sender, self_id, bet_amount, CURRENCY_ID)

    # 1〜100の乱数を生成
    rand = random(1, 100)

    # 50より大きければ勝ち
    if rand > 50:
        # 賞金（賭け金の2倍）を送金
        payout = bet_amount * 2
        transfer(sender, payout, CURRENCY_ID)
        output(concat("おめでとうございます！勝ちました！ ", to_str(payout), " を獲得しました。"))
    else:
        output("残念...負けです。また挑戦してください。")
```

## 3. 会員制メッセージボード

特定の通貨（会員権トークン）を持っている人だけがメッセージを書き込めるボードです。

```python
from RapidWire.sdk import *

# 会員権トークンのID
TOKEN_ID = 2

def main():
    # トークンを持っているか確認
    balance = get_balance(sender, TOKEN_ID)

    if balance <= 0:
        output("このボードに書き込むには、会員権トークンが必要です。")
        exit()

    # 入力がなければ、最新のメッセージを表示して終わる
    if length(input_data) == 0:
        last_msg = storage['last_message']
        last_user = storage['last_user']
        output(concat("最新の書き込み (by ", last_user, "): ", last_msg))
        exit()

    # 入力があれば、新しいメッセージとして保存
    storage['last_message'] = input_data
    # senderはIDなので文字列に変換して保存
    storage['last_user'] = to_str(sender)

    output("メッセージを書き込みました！")
```
