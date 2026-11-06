# ガスのコスト表

すべての命令と組み込み機能には、以下に挙げるガスの基本コストがある。
オペランドの大きさに応じて処理量が増える操作には、加算分が加わる。
ガスは操作の実行前に課金されるので、支払えない操作は何の効果も持たない。

## ガス代

呼び出しは、ガス代の通貨 (RAPID) で `gas used * gas price` を支払う。
ガス代は焼却されるので、RAPID の供給量はその分だけ減る。
呼び出しの開始前に、呼び出し元は `gas limit * gas price` (RAPID で支払う添付がある場合はその額も加えた額) を保有していなければならない。
終了後に徴収されるのは、実際に使ったガスの分だけである。
ガス上限を使い切った呼び出しは失敗し、上限の全額を支払う。
コードが始まる前に失敗した呼び出し (コントラクトがない、停止中のコントラクト、不明な関数、引数の誤り、存在しないガス代の通貨または添付の通貨、支払えないガス上限または添付) は何も支払わない。
コントラクトが呼び出し元の RAPID を使った (例えば `transfer_from` で) ためにガス代を支払えなくなった場合、呼び出しは種類 `SETTLEMENT` で失敗する。
すべてが取り消され、ガス代は復元された残高から徴収される。
最上位の呼び出し元は、失敗した入れ子の呼び出しが使ったガスも含め、入れ子の呼び出しの分も支払う。
コントラクトの登録、更新、ロック、停止、削除にガスはかからない。

## ガスの設定

| 設定 | 既定値 | 意味 |
| --- | --- | --- |
| `RAPIDWIRE_GAS_PRICE` | 1 | ガス 1 あたりの RAPID の最小単位 (0.001 RAPID) |
| `RAPIDWIRE_DEFAULT_GAS` | 128 | ガス上限を指定しない呼び出しのガス上限 |
| `RAPIDWIRE_MAX_GAS` | 200000 | 呼び出しが指定できる最大のガス上限 |

呼び出しは 1 から最大値までの任意のガス上限を指定でき、最大値そのものは 500000 まで設定できる。
最大値のもとでは、最も遅い種類の処理にすべてのガスを費やす呼び出しも、実行の実時間の上限 ([language.md](language.md#上限) を参照) のおよそ半分以内に終わる。
データベースを待つ時間は実時間に含まれず、別の上限を持つ。

## 命令のコスト

補助的な命令 (定数の読み込み、局所変数の読み込みと書き込み、スタックの入れ替え、無条件ジャンプ、return、レコードのフィールドの読み取り) のコストは 0 である。
1 つの操作に当たる命令 (算術、比較、条件分岐、ループの 1 ステップ、内部呼び出し、文字列とコレクションの操作、変換) のコストは 1 である。
`CALL_BUILTIN` 自体は無料で、呼び出す組み込み機能のコストを課金する。

| 命令コード | バイト | ガス | 操作 |
| --- | --- | --- | --- |
| `PUSH_INT` | 0x01 | 0 | インラインの int を積む |
| `PUSH_CONST` | 0x02 | 0 | str または int の定数を積む |
| `PUSH_TRUE` | 0x03 | 0 | True を積む |
| `PUSH_FALSE` | 0x04 | 0 | False を積む |
| `POP` | 0x05 | 0 | 先頭の値を捨てる |
| `DUP` | 0x06 | 0 | 先頭の値を複製する |
| `SWAP` | 0x07 | 0 | 先頭の 2 つの値を入れ替える |
| `LOAD` | 0x08 | 0 | 局所変数を積む |
| `STORE` | 0x09 | 0 | 取り出して局所変数に入れる |
| `ADD` | 0x10 | 1 | int a + b |
| `SUB` | 0x11 | 1 | int a - b |
| `MUL` | 0x12 | 1 | int a * b |
| `FLOORDIV` | 0x13 | 1 | int a // b (切り捨て) |
| `MOD` | 0x14 | 1 | int a % b (b の符号) |
| `NEG` | 0x15 | 1 | int -a |
| `EQ` | 0x18 | 1 | 同じ型の 2 つのスカラーの a == b |
| `NE` | 0x19 | 1 | 同じ型の 2 つのスカラーの a != b |
| `LT` | 0x1a | 1 | 2 つの int または 2 つの str の a < b |
| `LE` | 0x1b | 1 | 2 つの int または 2 つの str の a <= b |
| `GT` | 0x1c | 1 | 2 つの int または 2 つの str の a > b |
| `GE` | 0x1d | 1 | 2 つの int または 2 つの str の a >= b |
| `NOT` | 0x1e | 1 | bool の not a |
| `JUMP` | 0x20 | 0 | ジャンプする |
| `JUMP_IF_FALSE` | 0x21 | 1 | bool を取り出し、False ならジャンプする |
| `JUMP_IF_TRUE` | 0x22 | 1 | bool を取り出し、True ならジャンプする |
| `FOR_RANGE` | 0x23 | 1 | range ループの次のステップ |
| `FOR_EACH` | 0x24 | 1 | list または str のループの次の要素 |
| `CALL` | 0x25 | 1 | 内部関数を呼び出す |
| `RET` | 0x26 | 0 | return する |
| `CALL_BUILTIN` | 0x27 | 0 | 組み込み機能を呼び出す (組み込み機能のコストを課金する) |
| `CALL_CONTRACT` | 0x28 | 15 | 他のコントラクトを呼び出す。その失敗は呼び出し元を失敗させる |
| `TRY_CALL` | 0x29 | 15 | 他のコントラクトを呼び出し、CallResult を積む |
| `REVERT` | 0x2a | 0 | str を取り出し、それで取り消す |
| `ASSERT` | 0x2b | 1 | bool を取り出し、False なら str 定数で取り消す |
| `CONCAT` | 0x30 | 1 | str + str または list + list |
| `SLICE` | 0x31 | 1 | 省略可能な start と stop による str または list のスライス |
| `INDEX_GET` | 0x32 | 1 | str[i]、list[i] または dict[k] |
| `INDEX_SET` | 0x33 | 1 | list[i] = v または dict[k] = v |
| `LEN` | 0x34 | 1 | str、list または dict の長さ |
| `CONTAINS` | 0x35 | 1 | str、list または dict の中の needle |
| `BUILD_LIST` | 0x36 | 1 | n 個の値から list を作る |
| `BUILD_DICT` | 0x37 | 1 | n 組のキーと値から dict を作る |
| `APPEND` | 0x38 | 1 | list.append(v) |
| `LIST_POP` | 0x39 | 1 | list.pop() または list.pop(i) |
| `DELETE_ITEM` | 0x3a | 1 | del list[i] または del dict[k] |
| `GET_FIELD` | 0x3b | 0 | レコードのフィールドを読む |
| `CHECK_TYPE` | 0x3c | 1 | 値を型定数に絞り込むか、失敗する |
| `TO_INT` | 0x3d | 1 | int(x) |
| `TO_STR` | 0x3e | 1 | str(x) |

## 組み込み機能のコスト

コストは旧版のコスト表に従う: 読み取りは 1 から 2、ストレージへの書き込みは 3、移動は 10、Discord の送信とロールの変更は 5 から 10、スワップは 20、流動性の変更は 15、`sha256` は 5、`random` は 2 である。
旧版に対応するものがない組み込み機能は、最も近いものと同じコストである。
ストレージの読み取りと書き込み、スワップと見積もり、いくつかの文字列とコレクションの組み込み機能には、次の節の加算分が加わる。

| 組み込み機能 | ID | ガス |
| --- | --- | --- |
| `sender` | 0 | 0 |
| `self` | 1 | 0 |
| `paid` | 2 | 0 |
| `now` | 3 | 1 |
| `balance` | 4 | 1 |
| `currency` | 5 | 1 |
| `currency_info` | 6 | 1 |
| `transfer` | 7 | 10 |
| `transfer_from` | 8 | 10 |
| `approve` | 9 | 3 |
| `allowance` | 10 | 1 |
| `create_claim` | 11 | 3 |
| `pay_claim` | 12 | 5 |
| `cancel_claim` | 13 | 2 |
| `claim_info` | 14 | 2 |
| `swap` | 15 | 20 |
| `add_liquidity` | 16 | 15 |
| `remove_liquidity` | 17 | 15 |
| `quote` | 18 | 20 |
| `stake` | 19 | 10 |
| `unstake` | 20 | 10 |
| `staked` | 21 | 1 |
| `storage.get` | 22 | 1 |
| `storage.set` | 23 | 3 |
| `storage.delete` | 24 | 3 |
| `read_storage` | 25 | 1 |
| `discord.send` | 26 | 5 |
| `discord.add_role` | 27 | 10 |
| `discord.remove_role` | 28 | 10 |
| `discord.has_role` | 29 | 1 |
| `sha256` | 30 | 5 |
| `random` | 31 | 2 |
| `min` | 32 | 1 |
| `max` | 33 | 1 |
| `abs` | 34 | 1 |
| `Account` | 35 | 1 |
| `Currency` | 36 | 1 |
| `str.split` | 37 | 1 |
| `str.find` | 38 | 1 |
| `str.startswith` | 39 | 1 |
| `str.endswith` | 40 | 1 |
| `str.replace` | 41 | 1 |
| `str.upper` | 42 | 1 |
| `str.lower` | 43 | 1 |
| `str.strip` | 44 | 1 |
| `str.join` | 45 | 1 |
| `dict.get` | 46 | 1 |
| `dict.keys` | 47 | 1 |
| `dict.values` | 48 | 1 |

## サイズによる加算

加算分は処理の前にオペランドの大きさから計算され、基本コストに加えて課金される。
`//` は切り捨ての整数除算であり、"始まった区画ごと" は切り上げを意味する。

- `str_gas(n) = n // STR_CHARS_PER_GAS`: 操作が読む、または生み出す n 文字に対して課金する。対象は、2 つの str の `CONCAT` (結果の長さ)、str の `SLICE` (結果の長さ)、str に対する `CONTAINS` (検索対象と needle の合計)、`sha256` (入力)、`str.split` (テキスト)、`str.find` (テキストと部分文字列の合計)、`str.startswith` と `str.endswith` (接頭辞または接尾辞)、`str.replace` (テキスト、さらに結果)、`str.upper`、`str.lower`、`str.strip` (テキスト)、`str.join` (結果) である。
- `elements_gas(n) = n // ELEMENTS_PER_GAS`: 操作が複製または走査する n 個の要素に対して課金する。対象は、`BUILD_LIST` と `BUILD_DICT` (要素またはエントリの数)、list の `CONCAT` と list の `SLICE` (結果の長さに `COPY_ELEMENT_WEIGHT` を掛けたもの)、`dict.keys` と `dict.values` (エントリの数に `COPY_ELEMENT_WEIGHT` を掛けたもの)、位置を指定した `LIST_POP` と list に対する `DELETE_ITEM` (list の長さ)、`str.split` (分割後の部分)、`str.join` (要素)、および `CHECK_TYPE` が調べるスカラー要素である。
- list に対する `CONTAINS` は各要素を 1 回数える。str の needle で検索する list の要素は `1 + len(needle) // NEEDLE_CHARS_PER_ELEMENT` と数え、int の中から bool を、または bool の中から int の 0 か 1 を検索するときは、各要素をさらに `AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT` 回数える。dict に対する `CONTAINS` に加算分はない。
- `CHECK_TYPE` (`Value` の絞り込み) は、最も外側のものの後に調べる入れ子の list、dict、レコード 1 つごとに 1 を追加で支払う。
- `storage.set` のコストは合計で `storage_write_gas(n, v) = 3 + ceil(n / STORAGE_WRITE_BYTES_PER_GAS) + v * STORAGE_WRITE_GAS_PER_VALUE` である。n はキーの UTF-8 バイト数と符号化した値のバイト数の合計、v はその中の値の数 (スカラー、list、dict をそれぞれ 1 と数える) である。符号化の途中で失敗した書き込み (大きすぎる値、または入れ子が深すぎる値) は、失敗するまでに符号化したバイトと値の分を支払う。
- `storage.get` と `read_storage` のコストは、v 個の値を持つ符号化後 n バイトの保存値に対して、合計で `storage_read_gas(n, v) = 1 + n // STORAGE_READ_BYTES_PER_GAS + v * STORAGE_READ_GAS_PER_VALUE` である。存在しないキーは基本の 1 である。
- `swap` と `quote` は、経路探索のために基本の 20 に `route_search_gas(e, c) = e * ROUTE_GAS_PER_EXPANSION + ceil(c * ROUTE_GAS_PER_TWO_CHUNK_HOPS / 2)` を加える。e は探索が行った経路の展開の回数、c は配分の小口ホップの数 (小口の数に候補経路のプールの総数を掛けたもの) である。コントラクトの経路探索は、最大 10 個の小口に対して最大 500 回の展開を行い、この加算分を支払えないスワップは何も動く前に失敗する。加算分は、その後スワップや見積もりが失敗した場合 (例えば `no_route` や `slippage_exceeded`) にも支払われる。
- メモリの計測: 実行が保持する値がメモリの上限に近づくと、VM は使用中のメモリを正確に計測し、たどった r 個の参照に対して `memory_walk_gas(r) = ceil(r / WALK_REFERENCES_PER_GAS)` を課金する。

## コスト 0 の命令の連続

コスト 0 の命令も、多数が連続して実行されると完全に無料ではない。

- 実行されたコスト 0 の命令はそれぞれ、その重みをカウンタに加える。カウンタは、コストのある命令と `RET` のたびに 0 に戻る。
- カウンタが `FREE_RUN_ALLOWANCE` に達するたびに 1 ガスを課金し、カウンタから `FREE_RUN_ALLOWANCE` を差し引く。`free_run_gas(weights)` は、コスト 0 の命令の並びに対してこの規則が課金するガスである。
- `free_instruction_weight(op, builtin)` は、コスト 0 の命令に対して 1 である。ただし、下の `FREE_INSTRUCTION_WEIGHTS` に挙げた命令と、`FREE_BUILTIN_WEIGHTS` に挙げた無料の組み込み機能 (コスト 0 の `CALL_BUILTIN` が読む `sender`、`self`、`paid`) は除く。
- カウンタは関数の呼び出しごとに 0 から始まるので、関数のコストは、呼び出しがその関数から始まる場合も、`CALL` で呼ばれる場合も、他のコントラクトから呼ばれる場合も同じである。
- 登録時の検査はコスト 0 の命令だけでできたループを拒否するので、ループの各反復は少なくとも 1 つのコストのある命令を支払う。`while` ループは反復ごとに条件ジャンプ (1) を支払う。

## 加算の定数

| 定数 | 値 |
| --- | --- |
| `FREE_RUN_ALLOWANCE` | 8 |
| `STR_CHARS_PER_GAS` | 256 |
| `ELEMENTS_PER_GAS` | 64 |
| `STORAGE_WRITE_BYTES_PER_GAS` | 32 |
| `STORAGE_READ_BYTES_PER_GAS` | 128 |
| `STORAGE_WRITE_GAS_PER_VALUE` | 2 |
| `STORAGE_READ_GAS_PER_VALUE` | 1 |
| `ROUTE_GAS_PER_EXPANSION` | 2 |
| `ROUTE_GAS_PER_TWO_CHUNK_HOPS` | 3 |
| `WALK_REFERENCES_PER_GAS` | 4 |
| `COPY_ELEMENT_WEIGHT` | 2 |
| `NEEDLE_CHARS_PER_ELEMENT` | 64 |
| `AMBIGUOUS_CONTAINS_ELEMENT_WEIGHT` | 8 |
| `FREE_INSTRUCTION_WEIGHTS[RET]` | 3 |
| `FREE_INSTRUCTION_WEIGHTS[PUSH_CONST]` | 2 |
| `FREE_BUILTIN_WEIGHTS[sender]` | 2 |
| `FREE_BUILTIN_WEIGHTS[self]` | 2 |
| `FREE_BUILTIN_WEIGHTS[paid]` | 3 |

## 例

周囲のコードにかかるコスト 0 の命令の連続分を除いた、文のコスト:

| コード | ガス |
| --- | --- |
| `x = a + b` | 1 (`ADD`) |
| `if x < 10:` | 2 (`LT` と条件ジャンプ) |
| `transfer(sender, rapid, 5)` | 10 |
| `storage["k"] = 7` | 3 + 1 (4 バイト、始まった 32 バイトの区画ごと) + 2 (値 1 つ) = 6 |
| `sha256(text)` で 1,000 文字 | 5 + 3 = 8 |
| `call(shop, "buy")` | 15 と、呼び出される側自身のガス |

呼び出しの正確なガスを知るには `rwc run` ([cli.md](cli.md) を参照) を使う。
モック台帳はサーバーと全く同じようにガスを計量する。
