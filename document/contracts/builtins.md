# 組み込み機能

組み込み機能は、コントラクトが台帳、ストレージ、Discord を読み書きする唯一の手段である。
どの組み込み機能も、実行中のコントラクトの勘定 (`self`) として動作する。
移動は `self` から行われ、承認は `self` が与え、スワップは `self` の残高を使う。
Mint と通貨の設定 (利率、Mint 放棄、削除、管理の委任) には、コントラクトの持ち主が誰であってもコントラクトからは手が届かない。

## 失敗

台帳または Discord の規則に反した組み込み機能は、実行中のコントラクトを、メッセージが `NAME failed: CODE: MESSAGE` の `RUNTIME` 失敗で失敗させる。
実行の記録は `CODE` を `host_code` として保持する。
この失敗は、それを起こしたコントラクトの中では受け止められない。
事前に確認して避ける (例えば `transfer` の前に `balance(self, c) >= amount` を確かめる)。
`try_call` を使った呼び出し元のコントラクトは、これを呼び出される側の `RUNTIME` 失敗として受け取る。
組み込み機能の中のデータベースの処理は実時間の上限に数えない。
最上位の呼び出し全体のデータベース時間は別に制限されており ([language.md](language.md#上限) を参照)、その上限を過ぎた組み込み機能は、組み込み機能自体も失敗していた場合でも、呼び出しを `HOST_TIME_LIMIT` で失敗させる。
誤った型の引数、上限 ([language.md](language.md#上限) を参照) を超える値、または 1 から 2**63-1 の範囲外の Discord ID も `RUNTIME` 失敗であり、組み込み機能の実行前に検出される。
以下に挙げるコードは台帳のエラーコードである。

## シグネチャ

シグネチャは組み込み機能のレジストリが定めるとおりに書く。
`[...]` は省略可能な引数を表し、大文字 1 文字は型変数である。
ID はバイナリ中の `CALL_BUILTIN` 命令のオペランドであり、ガスは [costs.md](costs.md) の基本コストである (注記がある場合はサイズによる加算分が上乗せされる)。

| ID | シグネチャ | ガス |
| --- | --- | --- |
| 0 | `sender() -> Account` | 0 |
| 1 | `self() -> Account` | 0 |
| 2 | `paid() -> Paid` | 0 |
| 3 | `now() -> int` | 1 |
| 4 | `balance(account: Account, currency: Currency) -> int` | 1 |
| 5 | `currency(symbol: str) -> Currency` | 1 |
| 6 | `currency_info(currency: Currency) -> CurrencyInfo` | 1 |
| 7 | `transfer(to: Account, currency: Currency, amount: int) -> None` | 10 |
| 8 | `transfer_from(owner: Account, to: Account, currency: Currency, amount: int) -> None` | 10 |
| 9 | `approve(spender: Account, currency: Currency, amount: int) -> None` | 3 |
| 10 | `allowance(owner: Account, spender: Account, currency: Currency) -> int` | 1 |
| 11 | `create_claim(payer: Account, currency: Currency, amount: int, description: str) -> int` | 3 |
| 12 | `pay_claim(claim_id: int) -> None` | 5 |
| 13 | `cancel_claim(claim_id: int) -> None` | 2 |
| 14 | `claim_info(claim_id: int) -> ClaimInfo` | 2 |
| 15 | `swap(from_currency: Currency, to_currency: Currency, amount_in: int, min_out: int) -> int` | 20 |
| 16 | `add_liquidity(currency_x: Currency, currency_y: Currency, fee_bps: int, amount_x: int, amount_y: int, amount_x_min: int, amount_y_min: int, min_shares: int) -> int` | 15 |
| 17 | `remove_liquidity(currency_x: Currency, currency_y: Currency, fee_bps: int, shares: int, amount_x_min: int, amount_y_min: int) -> list[int]` | 15 |
| 18 | `quote(from_currency: Currency, to_currency: Currency, amount_in: int) -> Quote` | 20 |
| 19 | `stake(currency: Currency, amount: int) -> None` | 10 |
| 20 | `unstake(currency: Currency, amount: int) -> None` | 10 |
| 21 | `staked(account: Account, currency: Currency) -> int` | 1 |
| 22 | `storage.get(key: str[, default: Value]) -> Value` | 1 |
| 23 | `storage.set(key: str, value: Value) -> None` | 3 |
| 24 | `storage.delete(key: str) -> None` | 3 |
| 25 | `read_storage(account: Account, key: str[, default: Value]) -> Value` | 1 |
| 26 | `discord.send(guild: int, channel: int, message: str) -> None` | 5 |
| 27 | `discord.add_role(guild: int, user: Account, role: int) -> None` | 10 |
| 28 | `discord.remove_role(guild: int, user: Account, role: int) -> None` | 10 |
| 29 | `discord.has_role(guild: int, user: Account, role: int) -> bool` | 1 |
| 30 | `sha256(text: str) -> str` | 5 |
| 31 | `random(low: int, high: int) -> int` | 2 |
| 32 | `min(a: int, b: int) -> int` | 1 |
| 33 | `max(a: int, b: int) -> int` | 1 |
| 34 | `abs(a: int) -> int` | 1 |
| 35 | `Account(id: int) -> Account` | 1 |
| 36 | `Currency(id: int) -> Currency` | 1 |
| 37 | `str.split(text: str, sep: str) -> list[str]` | 1 |
| 38 | `str.find(text: str, sub: str) -> int` | 1 |
| 39 | `str.startswith(text: str, prefix: str) -> bool` | 1 |
| 40 | `str.endswith(text: str, suffix: str) -> bool` | 1 |
| 41 | `str.replace(text: str, old: str, new: str) -> str` | 1 |
| 42 | `str.upper(text: str) -> str` | 1 |
| 43 | `str.lower(text: str) -> str` | 1 |
| 44 | `str.strip(text: str) -> str` | 1 |
| 45 | `str.join(sep: str, items: list[str]) -> str` | 1 |
| 46 | `dict.get(d: dict[K,V], key: K, default: V) -> V` | 1 |
| 47 | `dict.keys(d: dict[K,V]) -> list[K]` | 1 |
| 48 | `dict.values(d: dict[K,V]) -> list[V]` | 1 |

## 実行の文脈

- `sender` (括弧なし): この実行を呼び出した勘定。最上位の呼び出しでは利用者、入れ子の呼び出しでは呼び出し元のコントラクトである。
- `self` (括弧なし): 実行中のコントラクトが登録されている勘定。そのウォレットがコントラクトの資金を保持する。
- `paid` (括弧なし): 添付した額を `currency` と `amount` を持つ `Paid` レコードとして表したもの。添付がなければ `paid.currency` はどの通貨も指さない `Currency(0)` で、`paid.amount` は 0 である。コードが始まる時点で、添付した額はすでに `self` の残高に入っている。
- `now()`: 最上位の呼び出しが始まった時刻の Unix 秒。どの入れ子の呼び出しでも同じであり、実行中に進まない。

## 残高と通貨

- `balance(account, currency)`: 任意の勘定の残高。保有していなければ 0 である。不明な通貨には `currency_not_found` で失敗する。
- `currency(symbol)`: そのシンボルの通貨。`currency_not_found` で失敗する。
- `currency_info(currency)`: `currency`、`name`、`symbol`、`issuer` (作成した勘定)、`supply`、`hourly_rate_ppm`、`minting_renounced`、`delegation_enabled`、`delete_requested`、`created_at` (Unix 秒) を持つ `CurrencyInfo` レコード。`currency_not_found` で失敗する。

## 送金と許可額

- `transfer(to, currency, amount)`: amount を `self` から `to` へ移す。1 未満の金額には `invalid_amount`、受取人の残高が最大の金額を超える場合は `amount_overflow`、`self` への移動には `invalid_argument`、さらに `currency_not_found` と `insufficient_balance` で失敗する。
- `transfer_from(owner, to, currency, amount)`: `owner` が `self` に与えた許可額の範囲内で、amount を `owner` から `to` へ移し、許可額をその分減らす。`transfer` と同様に失敗するほか、`owner` が `self` であるか `to` と等しい場合は `invalid_argument`、さらに `insufficient_allowance` で失敗する。
- `approve(spender, currency, amount)`: `self` が `spender` に与える許可額を設定 (上書き) する。0 で許可額を取り除く。負の金額には `invalid_amount`、`spender` が `self` の場合は `invalid_argument`、さらに `currency_not_found` で失敗する。
- `allowance(owner, spender, currency)`: 現在の許可額。なければ 0 である。

## 請求

- `create_claim(payer, currency, amount, description)`: `self` から `payer` への未払いの請求を作成し、その ID を返す。`invalid_amount`、`invalid_argument` (`self` への請求、または台帳が許すより長い説明)、`currency_not_found` で失敗する。
- `pay_claim(claim_id)`: 支払者である `self` が未払いの請求を支払う。`claim_not_found`、`permission_denied` (支払者でない)、`invalid_state` (未払いでない)、`currency_not_found`、`insufficient_balance` で失敗する。
- `cancel_claim(claim_id)`: 支払者または請求者である `self` が未払いの請求を取り消す。`claim_not_found`、`permission_denied`、`invalid_state` で失敗する。
- `claim_info(claim_id)`: `claim_id`、`claimant`、`payer`、`currency`、`amount`、`description`、`status` (`pending`、`paid` または `canceled`)、`created_at` を持つ `ClaimInfo` レコード。`claim_not_found` で失敗する。

## DEX

- `swap(from_currency, to_currency, amount_in, min_out)`: `self` の from_currency の amount_in を、最良の経路または経路の分割に沿ってプールを通じてスワップし、受け取った額を返す。min_out (0 以上) は必須であり、払い出しがそれより少なくなるスワップは `slippage_exceeded` で失敗する。その他の失敗: `invalid_amount`、`invalid_argument` (同じ通貨を 2 回指定)、`currency_not_found`、`insufficient_balance`、`no_route`、`amount_overflow`。コントラクトの経路探索は、経路の展開を最大 500 回まで行い、金額を最大 10 個の小口に分ける。これは利用者のスワップより小さな予算である。ガス: 20 に探索の `route_search_gas` を加えたもの。何も動く前に課金され、その後スワップが失敗しても返らない。
- `quote(from_currency, to_currency, amount_in)`: スワップを行わずに、現在の準備金でのスワップの結果を `Quote` レコードとして返す。レコードは `from_currency`、`to_currency`、`amount_in`、`amount_out`、`route_count`、`truncated` (経路探索が予算で止まったかどうか) を持つ。残高とスリッページの検査を除き、`swap` と同様に失敗する。ガス: 20 に `route_search_gas` を加えたもの。探索が経路を見つけなかった場合も同じである。
- `add_liquidity(currency_x, currency_y, fee_bps, amount_x, amount_y, amount_x_min, amount_y_min, min_shares)`: その手数料の段階 (100、50 または 10 ベーシスポイント) を持つ通貨ペアの既存のプールに、プールの価格で希望額以下を預け入れ、Mint されたシェアを返す。通貨はどちらの順に指定してもよい。`pool_not_found`、`invalid_argument` (同じ通貨、不明な手数料の段階、1 未満の min_shares)、`invalid_amount`、`insufficient_liquidity` (シェアを Mint するには少なすぎる預け入れ)、`slippage_exceeded` (最小値を下回る)、`insufficient_balance`、`amount_overflow` で失敗する。
- `remove_liquidity(currency_x, currency_y, fee_bps, shares, amount_x_min, amount_y_min)`: `self` のシェアを焼却し、`[amount_x, amount_y]` を通貨を指定した順で返す。`pool_not_found`、`invalid_amount`、`insufficient_shares`、`insufficient_liquidity` (一方の通貨の払い出しが 0 になる)、`slippage_exceeded` で失敗する。

## ステーク

- `stake(currency, amount)`: `self` の残高から amount をステークする。`invalid_amount`、`currency_not_found`、`insufficient_balance` で失敗する。
- `unstake(currency, amount)`: `self` のステークから引き出す。`invalid_amount`、`currency_not_found`、`insufficient_stake` で失敗する。
- `staked(account, currency)`: `now()` までに計上された利息を含む、任意の勘定のステーク。なければ 0 である。

## ストレージ

各コントラクトは独自のストレージを持ち、キー (1 から 64 文字の str) を、保存可能な任意の型の値に対応付ける。
保存可能な型は、スカラー、および保存可能な値の list と dict (int の dict キーは符号付き 64 ビットの範囲内) で、入れ子は最大 16 段である。
レコードは保存できない。
符号化した値は最大 4,096 バイトであり、コントラクトが保存するキーは最大 2,000 個で、キーのバイト数と値のバイト数の合計は最大 262,144 バイトである。
ストレージは公開されている。
誰でも読むことができ、他のコントラクトは `read_storage` で読む。
保存した dict は、挿入順ではなく、符号化したキーの正規の順で読み戻される。

- `storage[key] = value` または `storage.set(key, value)`: value を保存する。キーの数またはバイトの合計が上限を超える場合は `contract_storage_limit` で失敗する。不正なキー、保存できない値、サイズまたは深さの上限を超える値は `RUNTIME` 失敗である。ガス: `storage_write_gas` ([costs.md](costs.md#サイズによる加算) を参照)。
- `storage.get(key, default)` と `storage.get(key)`: 保存された値、またはキーがなければ default。結果は default の型を持ち、別の型の保存値は `RUNTIME` 失敗になる。default がなければ結果は `Value` であり、存在しないキーは `RUNTIME` 失敗になる。ガス: `storage_read_gas`。
- `del storage[key]` または `storage.delete(key)`: キーを取り除く。存在しないキーを取り除いても何も起きない。
- `read_storage(account, key, default)` と `read_storage(account, key)`: 他のコントラクトのストレージを `storage.get` の 2 つの形式と同様に読む。そのコントラクトはロックしない。

## Discord

コントラクトがメッセージの送信、ロールの変更、ロールの読み取りを行えるのは、管理者が `/discord-permission allow` で許可した Discord サーバーの中だけである。
それ以外では、どの `discord` 組み込み機能も `permission_denied` で失敗する。
メッセージとロールの変更は呼び出しの間には実行されない。
実行とともにキューに入り、呼び出しのコミット後に行われる。
失敗した呼び出しは何もキューに入れない。
ギルド、チャンネル、ロールの ID と利用者の勘定 ID は 1 から 2**63-1 の範囲になければならない。

- `discord.send(guild, channel, message)`: 1 から 2,000 文字のメッセージをキューに入れる (それ以外は `invalid_argument`)。
- `discord.add_role(guild, user, role)` と `discord.remove_role(guild, user, role)`: 利用者の勘定に対するロールの変更をキューに入れる (利用者でない勘定には `invalid_argument`)。
- `discord.has_role(guild, user, role)`: メンバーがそのロールを持っているかどうか。Bot から見たサーバーの状態に基づいて即座に答える。user は利用者の勘定でなければならず (それ以外は `invalid_argument`)、許可されたサーバーでは、不明なサーバー、メンバー、ロール、または時間内に答えが得られない場合は `False` になる。

### 配送

`worker` は、キューに入ったメッセージとロールの変更を呼び出しのコミット後にキューの順に行う。
ただし、再試行やレート制限を待っている操作は、後の操作に追い越されることがある。
`worker` はそれぞれの結果を実行とともに記録し、`GET /v1/executions/{execution_id}` がそれを `deliveries` の下に示す (状態、試行回数、投稿されたメッセージの ID またはエラーであり、メッセージの本文は示さない)。
配送の失敗が、呼び出しの結果、残高、移動、ストレージを変えることはなく、何も返金されない。

- 配送のたびに、`worker` はコントラクトがまだそのサーバーで許可されているかを確認し直す。呼び出しの後に許可が取り消されていると、配送は `permission_revoked` で失敗する。
- `discord.send` は、指定したサーバーのチャンネルにだけ投稿する (それ以外は `channel_not_in_guild`)。メッセージは利用者にメンションでき、その利用者には通知が届くが、`@everyone`、`@here`、ロールへのメンションは通知にならない。
- ロールの変更は、コントラクトの勘定と実行を示す監査ログの理由付きで行われる。
- ネットワークエラー、タイムアウト、Discord のサーバーエラーは、待ち時間を延ばしながら数分間再試行され、それでも駄目なら配送は `retries_exhausted` で失敗する。レート制限は配送を遅らせるだけである。Bot トークンが拒否されると、運営者がトークンを直すまで、どの配送も失敗させずにすべての配送を止める。Discord によるその他の拒否 (アクセス権の欠如、不明なチャンネル、メンバー、ロール、不正なメッセージ) は、Discord のエラーコードで即座に配送を失敗させる。
- 配送は少なくとも 1 回である。Discord がリクエストを受け付けた後、`worker` がそれを記録する前に停止するか、データベースへの接続を失うと、リクエストは再送される。Discord がリクエストをすでに受け付けた後でタイムアウトやサーバーエラーを返した場合も再送になる。Discord は最初のメッセージから数分以内に届いた重複メッセージを捨てるので、メッセージが 2 回投稿されるのは、再送がそれより遅れた場合だけである。重複したロールの変更はそれ以上の効果を持たない。

## ハッシュと乱数

- `sha256(text)`: text の UTF-8 バイト列の SHA-256 を、小文字の 16 進数 64 文字で返す。ガス: 5 に `str_gas(len(text))` を加えたもの。
- `random(low, high)`: サーバーの暗号論的に安全な乱数源による、low <= result <= high の一様乱数の int。どの呼び出し元も予測できない。抽選した値はすべて実行とともに記録されるので、結果を後から確かめられる。1 つの実行が抽選できる値は最大 256 個である (入れ子の呼び出しはそれぞれ独自の回数を持つ)。low が high より大きい場合、またはそれ以上の抽選は `RUNTIME` 失敗になる。

## 数値と識別子

- `min(a, b)`、`max(a, b)`、`abs(a)`: int に対して Python と同じ。
- `Account(id)` と `Currency(id)`: その ID の勘定または通貨。ID は 1 から 2**63-1 の範囲になければならない。`int(account)` で ID に戻る。
- `len(x)`、`int(x)`、`str(x)`、`bool(x)`、`range(...)` は言語の構文であり、[language.md](language.md#変換) で説明する。

## 文字列のメソッド

str のメソッドとして呼び出す (例えば `text.split(",")`)。
上のシグネチャでは、レシーバが最初の引数である。

- `text.split(sep)`: sep の出現の間にある部分。sep は空であってはならない。部分は最大 1,024 個である。
- `text.find(sub)`: 最初の出現の位置、または -1。
- `text.startswith(prefix)` と `text.endswith(suffix)`。
- `text.replace(old, new)`: すべての出現を置き換えたもの。結果は str の長さの上限に収まらなければならない。
- `text.upper()`、`text.lower()`、`text.strip()` (両端の空白)。
- `sep.join(items)`: `list[str]` の要素を sep でつないだもの。

いずれも、読む、または生み出す文字に対して `str_gas` を課金し、`split` と `join` は部分に対して `elements_gas` も課金する。

## 辞書のメソッド

- `d.get(key, default)`: key に対応する値、または default。default は dict の値の型を持たなければならない。
- `d.keys()` と `d.values()`: キーと値を挿入順に並べた新しい list。ガス: 1 に `elements_gas(2 * len(d))` を加えたもの。

list はメソッド `append(value)` と `pop()` / `pop(index)` を持つが、これらは組み込み機能ではなく命令である ([language.md](language.md#リストと辞書) を参照)。
