# HTTP API リファレンス

RapidWireは、外部アプリケーションとの連携用にRESTful APIを提供しています。
デフォルトではポート `8000` で動作します。

## 認証
多くのエンドポイントは公開されていますが、送金などの操作を伴うエンドポイントや個人情報に関わるエンドポイントには認証が必要です。
リクエストヘッダーに `API-Key` を含める必要があります。

```
API-Key: YOUR_API_KEY
```
*(注: 現在の実装では、APIキーの発行メカニズムはBot管理者を通じて行う必要があります)*

## エンドポイント一覧

### Info & Config

#### `GET /version`
APIサーバーのバージョン情報を取得します。

#### `GET /config`
現在のRapidWireの経済設定（手数料、制限など）を取得します。

---

### User & Account

#### `GET /user/{user_id}/name`
Discordユーザー名を取得します。

#### `GET /user/{user_id}/stats`
ユーザーの統計情報（総取引回数など）を取得します。

#### `GET /balance/{user_id}`
ユーザーの全通貨の残高を取得します。

#### `GET /balance/{user_id}/{currency_id}`
特定の通貨の残高を取得します。

#### `GET /account/history`
自分（APIキー所有者）の取引履歴を取得します。

---

### Currency & Transfer

#### `GET /currency/{currency_id}`
ID指定で通貨情報を取得します。

#### `GET /currency/symbol/{symbol}`
シンボル指定で通貨情報を取得します。

#### `POST /currency/transfer`
通貨を送金します。
- **body**:
  ```json
  {
    "destination_id": 123456789,
    "currency_id": 1,
    "amount": 100
  }
  ```

#### `POST /currency/transfer_from`
承認された額の範囲内で、他人のウォレットから送金します。

#### `POST /currency/approve`
第三者への送金許可額（Allowance）を設定します。

#### `GET /currency/allowance/{owner_id}/{spender_id}/{currency_id}`
許可額を確認します。

#### `GET /transfers/search`
条件を指定してトランザクションを検索します。
- クエリパラメータ: `source_id`, `dest_id`, `min_amount`, `start_timestamp` など。

#### `GET /transfer/{transfer_id}`
特定のトランザクション詳細を取得します。

---

### Staking

#### `GET /stakes/{user_id}`
ユーザーのステーキング状況を取得します。

---

### DEX (Liquidity Pool & Swap)

#### `GET /pools`
全流動性プールの一覧を取得します。

#### `GET /pools/{currency_a_id}/{currency_b_id}`
特定の通貨ペアのプール情報を取得します。

#### `POST /pools/add_liquidity`
流動性を提供します。

#### `POST /pools/remove_liquidity`
流動性を削除します。

#### `POST /swap`
通貨をスワップ（交換）します。
- **body**:
  ```json
  {
    "currency_from_id": 1,
    "currency_to_id": 2,
    "amount": 100
  }
  ```

#### `POST /swap/rate`
スワップのレート（見積もり）を計算します。実際の取引は行われません。

#### `GET /swap/route/{currency_from_id}/{currency_to_id}`
最適なスワップルート（経路）を取得します。

---

### Smart Contract

#### `GET /script/{user_id}`
ユーザーのコントラクトコードを取得します。

#### `POST /contract/update`
自分のコントラクトコードを更新します。

#### `POST /contract/execute`
コントラクトを実行します。

#### `GET /contract/variables/{user_id}`
コントラクトの全ストレージ変数を取得します。

#### `GET /contract/variable/{user_id}/{key}`
特定のストレージ変数を取得します。

#### `GET /contract/history/{user_id}`
コントラクトの実行履歴を取得します。

#### `GET /executions/{execution_id}`
特定の実行結果詳細を取得します。

---

### Claims (請求)

#### `POST /claims/create`
請求を作成します。

#### `GET /claims`
自分の請求一覧を取得します。

#### `POST /claims/{claim_id}/pay`
請求を支払います。

#### `POST /claims/{claim_id}/cancel`
請求をキャンセルします。
