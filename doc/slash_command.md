# RapidWire スラッシュコマンド一覧

このドキュメントでは、RapidWire Discordボットで利用可能なすべてのスラッシュコマンドについて説明します。

---

## 一般コマンド

### `/balance [user]`
指定したユーザー（または自分自身）の全通貨の残高を表示します。
- **user (任意)**: 残高を表示したいユーザーを指定します。

### `/transfer <user> <amount> [symbol] [input_data]`
指定したユーザーに通貨を送金します。
- **user**: 通貨を送金する相手のユーザー。
- **amount**: 送金する通貨の量。
- **symbol (任意)**: 送金する通貨のシンボル。指定しない場合は、コマンドが実行されたサーバーのデフォルト通貨が使用されます。
- **input_data (任意)**: 受信者のコントラクトに渡すことができる追加データ。

### `/history [transaction_id] [user] [source] [destination] [currency_symbol] [start_date] [end_date] [min_amount] [max_amount] [input_data] [page]`
あなた、または指定した条件での取引履歴を表示します。
- **transaction_id (任意)**: 特定の取引IDを指定すると、その取引の詳細を表示します。
- **user (任意)**: このユーザーが送金、または受信した取引を検索します。
- **source (任意)**: 指定した送金元の取引を検索します。
- **destination (任意)**: 指定した送金先の取引を検索します。
- **currency_symbol (任意)**: 指定した通貨での取引を検索します。
- **start_date (任意)**: `YYYY-MM-DD`形式で指定した日付以降の取引を検索します。
- **end_date (任意)**: `YYYY-MM-DD`形式で指定した日付以前の取引を検索します。
- **min_amount (任意)**: 指定した最小金額以上の取引を検索します。
- **max_amount (任意)**: 指定した最大金額以下の取引を検索します。
- **input_data (任意)**: 指定したInput Dataを含む取引を検索します。
- **page (任意)**: 履歴が複数ページにわたる場合にページ番号を指定します。

---

## 通貨管理コマンド (`/currency ...`)

### `/currency create <name> <symbol> <supply> [daily_interest_rate]`
現在のサーバーに新しい通貨を作成します。
- **name**: 通貨の正式名称（例: "MyCoin"）。
- **symbol**: 通貨のシンボル（例: "MYC"）。
- **supply**: 初期供給量。
- **daily_interest_rate (任意)**: ステーキングの日利（パーセント）。デフォルトは0です。

### `/currency info [symbol]`
指定した通貨（または現在のサーバーの通貨）の詳細情報を表示します。
- **symbol (任意)**: 情報を表示したい通貨のシンボル。

### `/currency mint <amount>`
**[管理者権限が必要]** 現在のサーバーの通貨を追加発行します。
- **amount**: 追加発行する量。

### `/currency burn <amount>`
**[管理者権限が必要]** あなたが保有する現在のサーバーの通貨の一部を焼却（永久に削除）します。
- **amount**: 焼却する量。

### `/currency renounce`
**[管理者権限が必要]** 現在のサーバーの通貨の**Mint機能**と**利率変更機能**を永久に放棄します。この操作は取り消せません。

### `/currency delete`
**[管理者権限が必要]** 現在のサーバーの通貨を削除します。誤操作防止のため、2段階の確認が必要です。
1. 最初にコマンドを実行すると、削除要請が記録されます。
2. 7日後から10日後までの間に再度コマンドを実行すると、通貨が完全に削除されます。

### `/currency request-interest-change <rate>`
**[管理者権限が必要]** ステーキングの日利変更を予約します。変更はタイムロック期間後に適用可能になります。
- **rate**: 新しい日利（パーセント）。

### `/currency apply-interest-change`
**[管理者権限が必要]** 予約されている利率変更を適用します。

---

## ステーキングコマンド (`/stake ...`)

### `/stake deposit <amount> [symbol]`
通貨を預け入れ、ステーキングを開始します。
- **amount**: 預け入れる量。
- **symbol (任意)**: ステーキングする通貨のシンボル。

### `/stake withdraw <amount> [symbol]`
ステーキングした通貨の一部または全部を引き出します。
- **amount**: 引き出す量。
- **symbol (任意)**: 引き出す通貨のシンボル。

### `/stake info`
あなたが現在行っているステーキングの状況を表示します。

---

## コントラクトコマンド (`/contract ...`)

### `/contract set <script> [max_cost]`
あなたのアカウントにスマートコントラクトを設定します。
- **script**: Pythonで書かれたコントラクトコードのファイル。
- **max_cost (任意)**: このコントラクトの実行を許可する最大のコスト。0を指定すると無制限になります。

### `/contract get`
現在あなたのアカウントに設定されているコントラクトのコードを取得します。

---

## 請求コマンド (`/claim ...`)

### `/claim create <user> <amount> [description]`
他のユーザーに通貨の支払いを請求します。
- **user**: 請求先のユーザー。
- **amount**: 請求する金額。
- **description (任意)**: 請求の内容に関する説明。

### `/claim list`
あなたが関与している（請求した、または請求された）すべての請求を一覧表示します。

### `/claim pay <claim_id>`
あなた宛ての請求を支払います。
- **claim_id**: 支払う請求のID。`/claim list`で確認できます。

### `/claim cancel <claim_id>`
あなたが作成した、またはあなた宛ての未払いの請求をキャンセルします。
- **claim_id**: キャンセルする請求のID。

---

## 流動性プールコマンド (`/lp ...`)

### `/lp create <symbol_a> <amount_a> <symbol_b> <amount_b>`
2つの通貨ペアで新しい流動性プールを作成します。
- **symbol_a**: 通貨Aのシンボル。
- **amount_a**: プールに提供する通貨Aの量。
- **symbol_b**: 通貨Bのシンボル。
- **amount_b**: プールに提供する通貨Bの量。

### `/lp add <symbol_a> <amount_a> <symbol_b> <amount_b>`
既存の流動性プールに流動性を追加します。
- **symbol_a**: 通貨Aのシンボル。
- **amount_a**: 追加する通貨Aの量。
- **symbol_b**: 通貨Bのシンボル。
- **amount_b**: 追加する通貨Bの量。

### `/lp remove <symbol_a> <symbol_b> <shares>`
流動性プールからあなたのシェアの一部または全部を引き出し、通貨を受け取ります。
- **symbol_a**: 通貨Aのシンボル。
- **symbol_b**: 通貨Bのシンボル。
- **shares**: 引き出すシェアの量。

### `/lp info <symbol_a> <symbol_b>`
指定した通貨ペアの流動性プールの情報を表示します。
- **symbol_a**: 通貨Aのシンボル。
- **symbol_b**: 通貨Bのシンボル。

---

## スワップコマンド

### `/swap <from_symbol> <to_symbol> <amount>`
流動性プールを利用して、ある通貨を別の通貨に交換（スワップ）します。
- **from_symbol**: 交換元の通貨のシンボル。
- **to_symbol**: 交換先の通貨のシンボル。
- **amount**: 交換する量。
