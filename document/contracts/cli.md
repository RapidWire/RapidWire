# rwc: コントラクト開発ツール

`rwc` は、コントラクトのソースのコンパイル、バイナリに対するサーバーの登録時の検査、逆アセンブル、インメモリのモック台帳でのコントラクトの実行、エディタ用スタブの書き出しを行う。
`rapidwire` パッケージとともに `rwc` コマンドとしてインストールされる。

## コマンド

| コマンド | 目的 |
| --- | --- |
| `rwc compile SOURCE [-o OUTPUT] [--max-size N]` | ソースを RWC1 バイナリにコンパイルする |
| `rwc verify BINARY [--max-size N]` | 登録時の検査を行う |
| `rwc disasm BINARY [--max-size N]` | ヘッダー、関数表、命令の一覧を出力する |
| `rwc run PROGRAM --function NAME [options]` | モック台帳で公開関数を実行する |
| `rwc stub [DIRECTORY]` | `rapidwire_contract.pyi` を書き出す |

`--max-size` の既定値は、サーバーの登録時のサイズ上限である 8,000 バイトである。

### compile

```
rwc compile role_vending.py -o role_vending.rwc
```

バイナリを書き出し (既定ではソースの隣に `.rwc` 拡張子で)、そのパス、サイズ、SHA-256 を出力する。
コンパイルエラーは `FILE:LINE:COLUMN: message` の形で出力され、続いてソースの行とその列の下にキャレットが示される。
このときバイナリは書き出されない。
`--max-size` より大きなバイナリも書き出され、stderr に警告が出る。

### verify

```
rwc verify role_vending.rwc
```

サーバーが登録時に行うのと全く同じ検査 ([binary-format.md](binary-format.md#登録時の検査) を参照) を行い、`OK sha256 HASH size BYTES` を出力する。
失敗した場合は stderr に `error: REASON` を出力する。

### disasm

```
rwc disasm role_vending.rwc
```

バイナリを検査してから、ヘッダーのフィールド、関数表 (各関数の公開範囲、シグネチャ、局所変数、スタックの最大の深さ、コードの範囲)、および定数プールと各関数の命令のアセンブラ形式の一覧を出力する。
サーバーから取得したコントラクトを呼び出す前に、中身を調べるために使う。

### run

```
rwc run role_vending.py --state role_vending.state.json --function buy --pay RAPID:120000
```

ソースまたはバイナリの公開関数を、サーバーと同じ VM、ガスの計量、上限、組み込み機能の規則を持つモック台帳で実行し、JSON レポートを出力する。

| オプション | 既定値 | 意味 |
| --- | --- | --- |
| `--function NAME` | 必須 | 呼び出す公開関数 |
| `--arg TYPE:VALUE` | なし | 引数。TYPE は `int`、`str`、`bool`、`account`、`currency` (ID またはシンボル) のいずれか。順に繰り返して指定する |
| `--sender ID` | 2000 | 呼び出す勘定 |
| `--self ID` | 1000 | コントラクトが登録されている勘定 |
| `--pay CURRENCY:AMOUNT` | なし | ID またはシンボルで指定した通貨の正の AMOUNT を添付する |
| `--gas N` | 128 | ガス上限 (サーバーの最大値 200,000 以下) |
| `--state FILE` | なし | 台帳の初期状態を与える JSON ファイル |
| `--allow-discord GUILD` | なし | GUILD でのコントラクトの Discord 操作を許可する。複数あれば繰り返す |
| `--now SECONDS` | 状態の `now`、なければ現在時刻 | Unix 秒での実行時刻 |
| `--seed N` | なし | 再現可能な実行のために `random()` のシードを与える |
| `--max-size N` | 8000 | バイナリのサイズ上限 |

`--state` がなければ、台帳には通貨 `RAPID` (ID 1269970084965912747) があり、呼び出し元がその 1,000,000,000 を保有する。
サーバーと同じく、コントラクトを許可していないギルドでは、どの `discord` 組み込み機能も `permission_denied` で失敗する。
許可は `--allow-discord` または状態の `discord_permissions` で与える。
ガスはサーバーと同じく扱われる。
呼び出しの開始前に、呼び出し元はガス代の通貨でガス上限分のコスト (その通貨で支払う添付があればその額も加える) を保有していなければならず、使ったガスのガス代は、呼び出しの成否にかかわらず呼び出し元から焼却される。
サーバーと同じく、台帳にないガス代の通貨または添付の通貨は呼び出しを `CALL_FAILED` と理由 `NO_CURRENCY` で失敗させ、最後に呼び出し元が支払えなくなったガス代は `SETTLEMENT` で失敗させ、組み込み機能への不正な引数はサーバーと同じメッセージで失敗させる。
ステークには利息が計上されない。

レポートは、`ok`、`result`、`error` と `message` (失敗の種類と本文)、`failed_function` と `failed_offset` (失敗した場所)、`gas_used`、`gas_limit`、`gas_fee`、`random_values`、`outbox` (キューに入った Discord 操作)、`transfers` (すべての移動で、プール保管勘定とステーク保管勘定は負の勘定 ID で示す)、`calls` (サーバーがその実行について記録するコントラクトの実行を、終了した順に並べたもの)、および最終的な `balances` と `storage` を持つ。
失敗した実行は、ガス代を除いて台帳を元のままにするので、その `outbox` は空であり、`transfers` は `gas` の移動だけを持つ。
`calls` はサーバーの記録に従う。
失敗した入れ子の呼び出しは自身のエントリを残すが、それが行った呼び出しのエントリは除かれる。
失敗が `try_call` で受け止められない場合は、失敗した呼び出し元のエントリが、失敗した入れ子の呼び出し自身のエントリにも取って代わるので、失敗した実行は最上位のエントリだけを報告する。

### stub

```
rwc stub contracts/
```

`rapidwire_contract.pyi` をそのディレクトリ (既定では現在のディレクトリ) に書き出す。

## 状態ファイル

すべてのキーが省略可能な JSON オブジェクトである。

| キー | 内容 |
| --- | --- |
| `now` | Unix 秒での実行時刻 |
| `gas_currency` | ガス代を支払う通貨の ID (既定は 1269970084965912747 で、サーバーのガス代の通貨) |
| `currencies` | `[{"id", "symbol", "name"?, "issuer"?, "hourly_rate_ppm"?, "created_at"?}]` |
| `balances` | `[{"account", "currency", "amount"}]` |
| `stakes` | `[{"account", "currency", "amount"}]` |
| `allowances` | `[{"owner", "spender", "currency", "amount"}]` |
| `pools` | `[{"currency_x", "currency_y", "fee_bps", "reserve_x", "reserve_y", "shares"?}]`。shares は流動性提供者の勘定をシェアに対応付ける (省略すると、最初の預け入れで Mint されるシェアを勘定 1 が保有する) |
| `claims` | `[{"claimant", "payer", "currency", "amount", "description"?, "status"?, "created_at"?}]` |
| `roles` | `[[guild, user, role]]`。`discord.has_role` から見えるロール |
| `discord_permissions` | `[[guild, contract]]`。コントラクトがメッセージの送信とロールの変更を行えるギルド |
| `contracts` | `[{"account", "source" or "binary", "active"?}]`。入れ子の呼び出しに使う他のコントラクトで、パスは状態ファイルからの相対パス |
| `storage` | `{account: {key: value}}` |

通貨は ID またはシンボルで指定する。
値には JSON のスカラー、配列、str キーのオブジェクトを使い、さらに `{"$account": id}`、`{"$currency": id}`、および他の型のキーを持つ dict 用の `{"$dict": [[key, value], ...]}` を使う。
レポートも同じ形式を使う。
不明なキーはエラーである。
samples ディレクトリには、サンプルごとの状態ファイルがある ([README.md](README.md#サンプル) を参照)。

## 終了コード

| コード | 意味 |
| --- | --- |
| 0 | 成功。`run` では呼び出しが成功した |
| 1 | 失敗: コンパイルエラー、検査に通らないバイナリ、読めないファイルまたは不正な状態、または `run` での呼び出しの失敗 |
| 2 | 使えないコマンドラインの値 (例えば最大値を超える `--gas`) |

## エディタの設定

すべてのコントラクトを `from rapidwire_contract import *` で始め、ソースのディレクトリで `rwc stub` を実行する。
スタブは言語のすべての組み込み機能、レコード、型を宣言するので、エディタの補完と型検査器は追加の設定なしで動作する。
例:

```
mypy --strict role_vending.py
```

スタブはコンパイラのすべての規則 (例えば条件が bool でなければならないことや、`Value` を絞り込まなければならないこと) を表現できないので、最終的な判断は `rwc compile` が下す。
