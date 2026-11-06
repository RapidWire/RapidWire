# コントラクト言語

コントラクトは Python の静的型付きの部分集合で書き、作成者の手元で `rwc compile` ([cli.md](cli.md) を参照) により RWC1 バイナリ ([binary-format.md](binary-format.md) を参照) へコンパイルする。
サーバーが受け取って実行するのはバイナリだけである。
すべての式はコンパイル時に型が決まっており、コンパイラは部分集合の外にあるものを、最初の誤りの行と列を示して拒否する。
言語が Python の部分集合なので、コントラクトは正しい Python モジュールでもあり、エディタや型検査器は `rapidwire_contract.pyi` スタブを使ってそのまま扱える。

## 最初のコントラクト

```python
from rapidwire_contract import *

PRICE = 100_000  # 100.000 RAPID in minimal units


@public
def buy() -> str:
    if paid.currency != currency("RAPID") or paid.amount < PRICE:
        revert("Send at least 100 RAPID")
    if paid.amount > PRICE:
        transfer(sender, paid.currency, paid.amount - PRICE)
    storage["sold"] = storage.get("sold", 0) + 1
    return "OK"
```

## モジュールの構成

ソースファイルは、次のものを任意の順で含む。

- 任意のモジュール docstring と、その他の単独の文字列リテラル。これらは無視される。
- 最大 1 つの `from rapidwire_contract import *`。コンパイラが受け付ける唯一の import である (スタブの名前をエディタに知らせるためのもので、コンパイラにとっては何も変えない)。
- モジュール定数: `NAME = expression` または `NAME: type = expression`。式は、コンパイラが計算できる int、str または bool の定数式 (リテラル、他の定数、int の `+ - * // %`、str の `+`、比較、`and`、`or`、`not`) である。注釈は値の型と一致しなければならない。
- 関数定義。

コントラクトは少なくとも 1 つの関数を定義し、そのうち少なくとも 1 つは `@public` でなければならない。
モジュール定数はコードに畳み込まれ、関数の中で代入することはできない。

## 型

| 型 | 値 |
| --- | --- |
| `int` | 絶対値が 2**256 未満の整数 |
| `str` | 最大 4,096 文字 (コードポイント) のテキスト |
| `bool` | `True` と `False`。`int` とは区別される |
| `Account` | 勘定 ID。同じ ID の `Account` とだけ等しい |
| `Currency` | 通貨 ID。同じ ID の `Currency` とだけ等しい |
| `list[T]` | 型 T の要素が最大 1,024 個 |
| `dict[K, V]` | エントリが最大 1,024 個。K は `int`、`str`、`Account` または `Currency` で、int のキーは -2**63 から 2**63-1 の範囲になければならない |
| `Value` | 型が実行時にしか分からない値 |
| `Paid`、`CurrencyInfo`、`ClaimInfo`、`Quote`、`CallResult` | 組み込み機能が返す読み取り専用のレコード |

- 浮動小数点数はなく、金額は最小単位の int である。
- `int`、`str`、`bool`、`Account`、`Currency` がスカラー型である。`Account` は同じ ID の `Currency` や `int` と等しくなることはなく、両者は dict の別のキーになる。
- dict で符号付き 64 ビットの範囲外の int キーを使うと `RUNTIME` 失敗になる。
- `list` と `dict` には要素の型が必要である (`list[int]`、`dict[str, int]`)。型の入れ子は最大 8 段である。
- `None` は関数の戻り値の型 (何も返さない関数) としてだけ使える。
- レコードは変数に保持し、内部関数に渡すことはできるが、ストレージへの保存、コントラクト呼び出しの引数への使用、公開関数からの返却はできない。
- 空の list または dict のリテラルには、注釈 (`items: list[int] = []`)、引数、戻り値の型、またはもう一方のオペランドから期待される型が必要である。

### Value

`Value` は `call()`、`CallResult.value`、既定値なしの `storage.get(key)` と `read_storage(account, key)` から得られる。
使う前に絞り込まなければならない。
絞り込みは `int(v)`、`str(v)`、`bool(v)`、または注釈付き代入 `x: list[int] = v` で行い、実行時に値を検査して、型が異なれば `RUNTIME` で失敗する。

### レコード

| レコード | フィールド |
| --- | --- |
| `Paid` | `currency: Currency`, `amount: int` |
| `CurrencyInfo` | `currency: Currency`, `name: str`, `symbol: str`, `issuer: Account`, `supply: int`, `hourly_rate_ppm: int`, `minting_renounced: bool`, `delegation_enabled: bool`, `delete_requested: bool`, `created_at: int` |
| `ClaimInfo` | `claim_id: int`, `claimant: Account`, `payer: Account`, `currency: Currency`, `amount: int`, `description: str`, `status: str`, `created_at: int` |
| `Quote` | `from_currency: Currency`, `to_currency: Currency`, `amount_in: int`, `amount_out: int`, `route_count: int`, `truncated: bool` |
| `CallResult` | `ok: bool`, `value: Value`, `error: str`, `message: str` |

## 関数

```python
def name(param: type, ...) -> type:
    ...
```

- すべての引数と戻り値に型注釈が必要である。引数は既定値を取らず、`*args`、`**kwargs`、キーワード専用引数、位置専用引数には対応しない。
- 関数の引数は最大 16 個、局所変数はコンパイラの一時変数を含めて最大 256 個である。
- デコレータは `@public` だけであり、関数を外部から呼び出せるようにする。公開関数の引数はスカラーでなければならず、公開関数は `int`、`str` または `bool` を返さなければならない。
- `@public` のない関数は内部関数である。`None`、list、dict、レコードを含め、任意の型を受け取り、返すことができる。
- 値を返す関数は、すべての経路で return しなければならない。末尾に到達しうる関数はコンパイラが拒否する。
- 関数名は最大 64 文字の ASCII 識別子である。
- 呼び出しは位置引数だけを取る。
- 関数は互いに、また自分自身を呼び出せる。再帰はコントラクトの実行あたり 64 フレームまでで、その次の呼び出しは `RUNTIME` で失敗する。

## 名前

- 変数は Python と同じく関数に局所的である。関数内のどこかで代入される名前はその関数の局所変数であり、最初の代入より前に読むとコンパイルエラーになる (コンパイラが見通せない経路では `RUNTIME` 失敗になる)。
- 局所変数の型は最初の代入または注釈で決まり、別の型の値を代入するとコンパイルエラーになる。
- 内包表記の変数は内包表記に局所的である。
- `__` で始まる名前はどこでも使えない。
- 組み込み機能の名前、型名、`public`、`call`、`try_call`、`revert`、`len`、`int`、`str`、`bool`、`range`、`list`、`dict`、`storage`、`discord` は束縛し直せない。モジュール定数と関数は、これらの名前とも互いとも同じ名前を持てない。

## 文

- 代入: `x = value`、連鎖代入 `x = y = value`、注釈付き代入 `x: type = value`、または `x: type` (値なしの宣言)。
- 要素への代入: `items[i] = v`、`d[key] = v`、`storage[key] = value`。
- 累算代入: int に対する `+=`、`-=`、`*=`、`//=`、`%=`、str と list に対する `+=` (list はその場で拡張される) を、変数または要素 (`totals["games"] += 1`) に使える。`storage[key] += ...` と list の要素への `+=` には対応しない。
- `if`、`elif`、`else`。
- `while condition:` (`while True:` を含む)。`else` 節はない。
- `for x in range(stop)`、`range(start, stop)`、`range(start, stop, step)` (step は 0 以外)。`for x in a_list`、`for ch in a_str`、`for key in a_dict` (キーの複製を反復する)。`else` 節はない。
- `break`、`continue`、`return`、`pass`。
- `assert condition` と `assert condition, message`: message、または `"assertion failed"` で取り消す。
- `revert(message)`: 実行を `REVERTED` と message で失敗させる。文としてだけ書ける。
- `del storage[key]`、`del d[key]`、`del items[i]`。
- 式文は呼び出しでなければならない。

## 式

- リテラル: int (`_` 区切りを使える)、str、`True`、`False`、list リテラル `[a, b]`、dict リテラル `{k: v}`。
- int の算術: `+`、`-`、`*`、`//` (切り捨て除算)、`%` (除数の符号を持つ)、単項の `-` と `+`。0 による除算と int の上限を超える結果は `RUNTIME` 失敗になる。
- `+` は 2 つの str、または同じ型の 2 つの list を連結する。
- 比較: `==` と `!=` は同じ型の 2 つのスカラーを比較する。`<`、`<=`、`>`、`>=` は 2 つの int または 2 つの str を比較する。比較は連鎖できる (`0 < x <= 10`)。
- `in` と `not in`: str の中の部分文字列、その型の list の中のスカラー、dict の中のキー。
- `and`、`or`、`not` は bool を取り、短絡評価する。`if`、`while`、`assert` の条件は bool でなければならない (真偽値への暗黙の変換はないので、`len(items) > 0` と書く)。
- 条件式: `a if condition else b`。両方の分岐は同じ型である。
- 添字: `text[i]` (1 文字の str)、`items[i]`、`d[key]`。負の添字は末尾から数える。範囲外の添字や存在しないキーは `RUNTIME` 失敗になる。
- スライス: `text[a:b]` と `items[a:b]`。どちらの境界も省略できる。ステップはない。
- f-string: `f"{x} paid {amount}"`。整形できるのはスカラーだけである (`Account` または `Currency` は ID として整形される)。`!r` のような変換と書式指定には対応しない。
- list 内包表記 `[expr for x in iterable if condition]` と dict 内包表記 `{k: v for x in iterable}`。`for` 節と `if` 節はいくつでも書ける。
- レコードのフィールド: `paid.amount`、`info.symbol`、`r.ok`。

### 変換

- `int(x)`: `int`、`bool` (0 または 1)、`Account` または `Currency` (その ID)、10 進数の `str` (`-?[0-9]+`、それ以外は `RUNTIME`)、またはこれらのいずれかを保持する `Value` に使える。
- `str(x)`: スカラー、またはスカラーを保持する `Value` に使える (`Account` と `Currency` はその ID になる)。
- `bool(x)`: `bool` に使える。`int` では `x != 0`、`str`、list、dict では空でないかどうかになる。`Value` では bool を保持していなければならない。
- `len(x)`: `str`、list、dict に使える。
- `Account(id)` と `Currency(id)` は int から識別子を作る ([builtins.md](builtins.md#数値と識別子) を参照)。

### リストと辞書

- `items.append(v)`、`items.pop()` (最後の要素)、`items.pop(i)`。空の list からの pop と範囲外の添字での pop は `RUNTIME` 失敗になる。
- `d.get(key, default)`、`d.keys()`、`d.values()`。
- list と dict は Python と同じく参照で共有される。受け取った list を変更する内部関数は、呼び出し元の list を変更する。
- list または dict を 1,024 要素を超えて大きくすると `RUNTIME` 失敗になる。

## 対応しない構文

コンパイラは次のものを拒否し、代わりの書き方がある場合はそれをメッセージで示す。

- `from rapidwire_contract import *` 以外の `import`、`class`、入れ子の関数、`lambda`、`async`、`await`、`yield` とジェネレータ。
- `global`、`nonlocal`、`try` / `except` (他のコントラクトの失敗を受け止めるには `try_call` を使う)、`raise` (`revert` を使う)、`with`、`match`。
- 浮動小数点数、複素数、bytes、値としての `None`、タプル (`attach=(currency, amount)` の組を除く)、集合、ジェネレータ式、`:=`、`*` と `**` による展開。
- 演算子 `/` (`//` を使う)、`**`、`@`、`<<`、`>>`、`|`、`^`、`&`、`~`、`is` と `is not` (`==` を使う)。
- スライスのステップとスライスへの代入、アンパック代入 (`a, b = ...`)、属性への代入。
- `@public` 以外のデコレータ、型引数、型エイリアス、文字列による注釈、キーワード引数 (`call` と `try_call` の `attach` と `gas` を除く)。
- `list()` と `dict()` の呼び出し (リテラルで書く)、およびこのドキュメントに挙げていないすべての Python 組み込み関数。
- `__` で始まる名前。

## 他のコントラクトの呼び出し

```python
result = call(target, "function", [arg1, arg2], attach=(currency, amount), gas=5_000)
r = try_call(target, "function", args, attach=(currency, amount), gas=5_000)
```

- target は `Account`、関数名は `str` である。引数リストは省略でき、最大 16 個のスカラー (または `Value`) を並べた list リテラル、またはスカラーの list である。`attach` と `gas` は省略できるキーワードである。
- 呼び出される側は、`sender` を呼び出し元のコントラクト、`paid` を添付した額として実行される。添付した額は、呼び出される側のコードが始まる前に呼び出し元のコントラクトから呼び出される側へ移る。添付する額は正でなければならない (そうでなければ呼び出し元の `RUNTIME` 失敗になる)。
- `call` は呼び出される側の結果を `Value` として返す。呼び出される側のどの失敗も同じ種類で呼び出し元を失敗させ、さらに上位の `try_call` が受け止めない限り、呼び出しの連鎖をさかのぼって同様に失敗させる。
- `try_call` は失敗する代わりに `CallResult` を返す。成功時は `ok` が `True` で、`value` が結果を保持し、`error` と `message` は空である。失敗時は `ok` が `False` で、`error` が失敗の種類、`message` が 256 文字で切った呼び出される側のメッセージであり、`value` を読むと `RUNTIME` 失敗になる。
- 呼び出される側が `try_call` のもとで失敗すると、その側とその側が行った呼び出しのすべてが取り消される。対象は移動、ストレージへの書き込み、キューに入った Discord 操作、そして添付した額であり、添付した額は呼び出し元に戻る。呼び出しの前に呼び出し元が行ったことは残る。
- 失敗した呼び出しはすべて、呼び出し元の子の実行として、使ったガスと抽選した乱数の値とともに、失敗または取り消しとして記録される。
- これには、呼び出される側が始まらなかった `CALL_FAILED` の呼び出しも含まれる。その記録の使用ガスは 0 で、乱数の値はない。
- 呼び出しは、呼び出される側を調べる前に、次の理由でこの順に検査され、最初に当てはまったものを理由として `CALL_FAILED` で拒否される: 16 個を超える引数、スカラーでない引数、深さの上限、再入、どのコントラクトも持ちえない関数名。したがって `try_call(self, "bad name")` は再入を報告する。
- `try_call` が失敗を受け止めた場合、記録は残る。失敗が呼び出し元も失敗させる場合、記録は呼び出し元の効果とともに取り消され、呼び出し元自身の記録が、エラーの `child` で失敗した呼び出しを示す。
- 入れ子の呼び出しの記録は、引数を呼び出し引数の型付き形式で保存する。ただし、長さまたはスカラーでない値を理由に拒否された引数リストは空のリストとして保存する。どのコントラクトも持ちえない関数名 (ASCII 識別子でない、または 64 文字より長い) は、`'not-a-name'` のように引用符で囲みエスケープした形式で、64 文字で切り、切った場合は `...` で終わるように保存する。そのため、実在の関数名のように見えることはない。
- `gas=N` (正の値) は、呼び出される側を最大 N ガス、または残りがそれより少なければ残りまでに制限する。N を使い切った呼び出される側は `OUT_OF_GAS` で失敗し、これは `try_call` で受け止められる。`gas` がなければ呼び出される側は残りのガスをすべて使うことができ、使い切ると呼び出し元も続行できない。
- 呼び出される側が使ったガスは決して戻らず、最上位の呼び出し元がすべてを支払う。
- 現在の呼び出しの連鎖上ですでに実行中のコントラクトは再び呼び出せず (再入)、1 つの連鎖が持つコントラクトの実行は最上位の実行を含めて最大 6 個である。どちらも `CALL_FAILED` になる。

### 失敗の種類

| `error` | 原因 |
| --- | --- |
| `REVERTED` | 呼び出される側が `revert` を呼んだ、または `assert` が失敗した |
| `RUNTIME` | 0 による除算、型の不一致、添字やキーの欠如、値の上限の超過、深すぎる再帰、または組み込み機能の失敗 (例えば残高不足) |
| `OUT_OF_GAS` | 呼び出される側が、その呼び出しに与えられた `gas` の上限を使い切った |
| `CALL_FAILED` | 呼び出しを行えなかった: コントラクトがない、停止中のコントラクト、その名前の公開関数がない、引数の数または型の誤り、存在しない通貨または呼び出し元が支払えない通貨での添付、深さの上限、または再入 |

`try_call` は、最上位のガス上限の到達、メモリの上限、実時間の上限、ホスト時間の上限を決して受け止めない。
これらはサーバーの障害と同じく、最上位の呼び出し全体を失敗させる。

## 失敗と取り消し

- 実行は、`revert`、失敗した `assert`、あらゆる `RUNTIME` の障害、失敗した組み込み機能、または上限によって失敗する。
- 最上位の呼び出しが失敗すると、そのすべての効果 (移動、ストレージへの書き込み、キューに入った Discord 操作、添付した額) が取り消され、ガス代だけが徴収される。[costs.md](costs.md#ガス代) を参照。
- 実行の記録は、失敗の種類、メッセージ (最大 256 文字)、失敗した命令の関数とバイトオフセット、使用ガス、実行が抽選した乱数の値、および行ったすべての入れ子の呼び出しの抽選を保持する。これには、自身の記録が実行とともに巻き戻された呼び出しも含まれる。

## 上限

| 上限 | 値 |
| --- | --- |
| int | 絶対値が 2**256 未満 |
| str | 4,096 文字 |
| list と dict | 1,024 個の要素またはエントリ |
| 最上位の呼び出しあたりのメモリ | メモリ計測の対象となる値で 2 MiB (入れ子の呼び出しを含む) |
| 関数あたりのオペランドスタック | 128 個の値 (バイナリの登録時に検査する) |
| 関数あたりの引数 | 16 |
| 関数あたりの局所変数 | 256 |
| コントラクトの実行あたりの内部呼び出しフレーム | 64 |
| 呼び出しの連鎖上のコントラクトの実行 | 6 (最上位の実行を含む) |
| 最上位の呼び出しあたりの実時間 | 2 秒 (入れ子の呼び出しを含み、データベースの処理と、その間に他のリクエストが動いた時間は数えない) |
| 最上位の呼び出しあたりのデータベース時間 | 10 秒 (入れ子の呼び出しを含む) |
| 1 つのプロセスのイベントループ (API、Bot または `worker`) が同時に実行する最上位の呼び出し | 4 |
| 1 つのプロセスのイベントループが専用の枠で同時に実行するドライラン | 2 |
| 失敗メッセージと `CallResult.message` | 256 文字 |
| ストレージのキー | 1 から 64 文字 |
| ストレージの値 | 符号化後 4,096 バイト、入れ子は最大 16 段 |
| コントラクトあたりのストレージ | 2,000 キーかつ 262,144 バイト |
| コントラクトの実行あたりの `random()` の抽選 | 256 |
| バイナリのサイズ | 8,000 バイト |
| 呼び出しあたりのガス | `RAPIDWIRE_MAX_GAS`、既定 200,000、最大 500,000 |

実行中のコントラクトは、同じイベントループ上でプロセスが他のリクエストを処理する機会を定期的に与え、その時間は実時間に含めない。
そのため、同じプロセスの他の処理が呼び出しの実時間を使い果たすことはない。
コントラクトの実行中にマシンの CPU が他のプロセスに費やした時間は、引き続き数えられる。
ドライランには専用の枠があるので、ドライランが呼び出しを待たせることはない。
それ以上の呼び出しやドライランは、開始前にその種類の空き枠を待ち、その待ち時間はどの上限にも含まれない。

値の上限を超えると、上限を超える値を生み出す操作の時点で `RUNTIME` 失敗になる。
メモリ、実時間、データベース時間の上限を超えると、最上位の呼び出し全体がそれぞれ `MEMORY_LIMIT`、`WALL_CLOCK_LIMIT`、`HOST_TIME_LIMIT` で失敗し、これは `try_call` で受け止められない。
失敗した組み込み機能がデータベース時間の上限も過ぎていた場合は `HOST_TIME_LIMIT` を報告する。
最後にガス代を支払えなくなった最上位の呼び出しは `SETTLEMENT` で失敗する ([costs.md](costs.md#ガス代) を参照)。
