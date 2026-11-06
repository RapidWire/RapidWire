# RapidWire の運用

このガイドは、Docker Compose で RapidWire をサーバーに配備して運用し、そのバックアップを管理して自分のマシンで検証する運営者に向けたものである。
HTTP API は [api.md](api.md)、Discord Bot は [bot.md](bot.md)、バックアップのファイルとそのエクスポート形式は [backups.md](backups.md) で説明する。

## サービス

1 つのコンテナイメージがすべての役割を担い、与えたコマンドが役割を選ぶ。
`compose.yaml` は次のサービスを定義する:

| サービス | コマンド | 役割 |
| --- | --- | --- |
| `mysql` | `mysql:8.4` イメージ | データベース。コマンド引数で設定され (utf8mb4 と `utf8mb4_0900_bin` 照合順序、UTC、READ-COMMITTED)、`mysql-data` ボリュームに保存される |
| `migrate` | `rapidwire migrate` | Alembic のマイグレーションを最新のリビジョンまで適用して終了する。`build` セクションを持つ唯一のサービスであり、共有の `rapidwire:local` イメージをビルドする |
| `api` | `rapidwire api` | HTTP API と Web エクスプローラー。`cloudflared` を通じてのみ到達できる。運営者にバックアップを提供するため `backups` ボリュームを読み取り専用でマウントする |
| `bot` | `rapidwire bot` | Discord Bot (gateway 接続、スラッシュコマンド、API トークンのメンション、ギルド一覧) |
| `worker` | `rapidwire worker` | outbox の dispatcher、ステークの利息計上、定期バックアップ。`backups` ボリュームを読み書き可能でマウントする |
| `backup` | `rapidwire backup` | 要求に応じてバックアップを 1 回取り、`backups` ボリュームを読み書き可能でマウントする。`tools` プロファイルに属するため、`docker compose up` がこれを起動することはない |
| `cloudflared` | `cloudflare/cloudflared` イメージ | API を公開する Cloudflare Tunnel |

```mermaid
flowchart LR
  USERS[インターネット] --> CF[Cloudflare]
  CF --> CFD[cloudflared]
  CFD --> API[api]
  DISCORD[Discord] <--> BOT[bot]
  WRK[worker] -->|REST| DISCORD
  MIG[migrate] --> DB[(mysql)]
  API --> DB
  BOT --> DB
  WRK --> DB
  BK[backup 要求時のみ] --> DB
  DB --- MYSQLDATA[(ボリューム mysql-data)]
  WRK --> BACKUPS[(ボリューム backups)]
  BK --> BACKUPS
  BACKUPS -->|読み取り専用| API
  OP[運営者] -->|SSH ポートフォワーディング| DB
```

`rapidwire` コマンドには次のサブコマンドがある:

| コマンド | 内容 |
| --- | --- |
| `rapidwire migrate` | データベースのマイグレーションを最新のリビジョンまで適用する |
| `rapidwire api` | SIGTERM または SIGINT を受けるまで HTTP API と Web エクスプローラーを実行する |
| `rapidwire bot` | SIGTERM または SIGINT を受けるまで Discord Bot を実行する。`rapidwire bot --list-commands` は接続せずにコマンドツリーを表示する |
| `rapidwire worker` | SIGTERM または SIGINT を受けるまで worker のジョブを実行する |
| `rapidwire backup` | いまバックアップを 1 回取る。`rapidwire backup verify` と `rapidwire backup restore` は運営者のマシンでダウンロードしたバックアップを検証する |
| `rapidwire verify-invariants` | 台帳の不変条件を確認してレポートを表示する |

`rapidwire backup verify` と `rapidwire backup restore` を除くすべてのコマンドは、起動時にすべての設定を読み込んで検証する。
`rapidwire bot --list-commands` は、使わないデータベースのパスワードに仮の値を与える。
設定が欠けているか不正であれば、終了コード 78 でコマンドを停止し、標準エラーにプレーンテキストの `Invalid configuration:` メッセージを出す。
このメッセージは変数名を示すが、その値は決して示さない。
その他の終了コードは、成功が 0、失敗が 1、使い方の誤りが 2、不変条件違反またはバックアップの不一致が 4 である。

## 配備

### サーバー

サーバーには Compose プラグイン付きの Docker Engine が必要であり、`compose.yaml` が長い形式の `env_file` 構文を使うためバージョン 2.24 以降、アーキテクチャは amd64 とする: イメージは MySQL 8.4 クライアントを MySQL のリポジトリからインストールし、そのリポジトリは amd64 のパッケージしか公開していない。
サーバーは SSH 以外のポートを開けない。
`cloudflared` は Cloudflare へ外向きに接続し、API はそのトンネルを通じてのみ公開され、MySQL はホストの `127.0.0.1` にのみ公開される。

### 初回の起動

1. サーバーでリポジトリをクローンし、そのディレクトリに移動する。
2. 3 つの設定ファイルをそれぞれの例から作成し、秘密情報を記入する。これらは認証情報を含み、git と Docker のビルドからは無視され、運営者だけが読めるようにすべきである (`chmod 600`)。
3. イメージをビルドし、サービスを起動する。

```
git clone https://github.com/RapidWire/RapidWire.git
cd RapidWire
cp .env.example .env
cp .env.mysql.example .env.mysql
cp .env.cloudflared.example .env.cloudflared
chmod 600 .env .env.mysql .env.cloudflared
docker compose build
docker compose up -d
```

| ファイル | 読むもの | 設定が必須のもの |
| --- | --- | --- |
| `.env` | `migrate`、`api`、`bot`、`worker`、`backup` と compose 自身 | `RAPIDWIRE_DB_PASSWORD` (8 文字以上)、`RAPIDWIRE_DISCORD_TOKEN` (Bot のトークン)、`RAPIDWIRE_OPERATOR_ACCOUNT_IDS` (運営者の Discord ユーザー ID) |
| `.env.mysql` | `mysql` のみ | `MYSQL_ROOT_PASSWORD` |
| `.env.cloudflared` | `cloudflared` のみ | `TUNNEL_TOKEN` (Cloudflare Tunnel のトークン) |

- Compose は、`mysql-data` ボリュームの初回の初期化時にデータベースとそのユーザーを作成するため、`.env` から `RAPIDWIRE_DB_NAME`、`RAPIDWIRE_DB_USER`、`RAPIDWIRE_DB_PASSWORD` も読み、`RAPIDWIRE_DB_PASSWORD` がなければ起動を拒否する。
- `MYSQL_ROOT_PASSWORD` とデータベースユーザーのパスワードは、その初回の初期化時にのみ反映される。後でファイル上で変更しても MySQL 内の値は変わらない。
- MySQL の root アカウントは `mysql` コンテナ内のソケット経由でのみログインでき、ネットワーク経由では決してログインできない。
- Cloudflare のダッシュボードでは、トンネルの公開ホスト名の向き先を `http://api:8000` (`RAPIDWIRE_API_PORT` で待ち受ける `api` サービス) とする。
- `api` サービスはポートを公開せず、compose のネットワーク上でのみ到達できる。そのため、どの接続元から来た `X-Forwarded-For` と `X-Forwarded-Proto` ヘッダーも信頼し、アクセスログには `cloudflared` が転送する訪問者のアドレスが記録される。

`docker compose up -d` は `mysql` を起動して healthy になるまで待ち、`migrate` を完了まで実行してから初めて `api`、`bot`、`worker` を起動し、`cloudflared` は `api` の後に起動する。
`migrate` が失敗すると `api`、`bot`、`worker` は起動しない。
`docker compose ps -a` がその終了コードを、`docker compose logs migrate` がそのログを示す。
`mysql`、`api`、`bot`、`worker`、`cloudflared` は自動的に再起動する (`restart: unless-stopped`)。
`migrate` と `backup` は 1 回だけ実行される。

`RAPIDWIRE_DISCORD_TOKEN` がないと、`bot` と `worker` はただちに終了コード 78 で停止し、標準エラーにトレースバックなしで `Invalid configuration: RAPIDWIRE_DISCORD_TOKEN is required for this command but is not set` の 1 行を出す。
その後 Docker の再起動ポリシーが間隔を伸ばしながらこれらを再起動し続け、`docker compose ps -a` はそれを `Restarting (78)` と表示するが、`mysql`、`migrate`、`api` は影響を受けない。
`.env` にトークンを設定した後、`docker compose up -d bot worker` が新しい環境で両方を作り直す。

`bot` と `worker` はちょうど 1 つずつ動かす。
worker のジョブはデータベースのロックで保護されているため、2 つ目の worker があってもジョブが二重に実行されることはないが、この配備はどちらのサービスもスケールさせない。

### ホストのディレクトリを使わない

ホストのディレクトリをマウントするサービスはない。
コードはビルド時にイメージへコピーされ、設定と秘密情報は `env_file` を通じて環境変数としてコンテナに渡り、データベースとバックアップは名前付きボリューム `mysql-data` と `backups` に置かれる。
Compose はボリューム名にプロジェクト名を前置するため、ホスト上では `rapidwire_mysql-data` と `rapidwire_backups` になる。

### 更新

```
git pull
docker compose build
docker compose up -d
```

`docker compose up -d` はイメージまたは構成が変わったコンテナを作り直し、`api`、`bot`、`worker` の起動前に `migrate` を再び実行するため、新しいマイグレーションは更新の一部として適用される。
Bot は起動のたびにスラッシュコマンドを Discord と同期する。

設定はプロセスの起動時に読まれる。
`.env` を編集した後は、それを読むコンテナを作り直す。
例えば `docker compose up -d --force-recreate api bot worker` を使う。
`docker compose restart` では古い環境のままになる。

### MySQL への接続

MySQL はサーバーの `127.0.0.1` の `MYSQL_HOST_PORT` (既定値 3306、compose の変数展開のため `.env` で設定する) にのみ公開されるので、別のマシンからは SSH ポートフォワーディング経由でのみ到達できる:

```
ssh -N -L 13306:127.0.0.1:3306 operator@server.example.com
mysql --host=127.0.0.1 --port=13306 --user=rapidwire --password rapidwire
```

2 つ目の `3306` が `MYSQL_HOST_PORT` であり、転送された接続は `.env` のアプリケーションユーザーとしてログインする。
root アカウントにはコンテナ内からのみ、`docker compose exec mysql mysql --user=root --password` で到達できる。

## Discord の準備

- Discord の開発者ポータルでアプリケーションとその Bot を作成し、Bot のトークンを `RAPIDWIRE_DISCORD_TOKEN` に設定する。`bot` と `worker` のコマンドはこれがないと起動を拒否する。
- Bot は gateway intent として `guilds`、`members`、`guild_messages`、`dm_messages` を使う。
- `members` は特権 intent である: 開発者ポータルで Bot の Server Members Intent を有効にすること。そうしないと gateway が接続を拒否する。
- `message_content` は不要であり、無効のままでよい。
- Bot は `bot` と `applications.commands` のスコープで招待する。

worker は同じトークンを gateway 接続なしで Discord REST API に使い、請求の通知を DM で送り、コントラクトが送るメッセージを投稿し、コントラクトが変更するロールを付与および削除する。
サーバー内で Bot には次の権限が必要である:

| 用途 | 権限 |
| --- | --- |
| コントラクトのメッセージ (`discord.send`) | 対象チャンネルでの View Channel と Send Messages |
| コントラクトによるロールの変更 (`discord.add_role`、`discord.remove_role`) | Manage Roles。Bot の最上位のロールが、付与または削除するすべてのロールより上にあること |
| チャンネル ID 付きの API トークンのメンション | トークンの送り先のチャンネルと、Bot が返信するメンションのチャンネルでの View Channel、Send Messages、Read Message History |

Bot が権限を持たない操作は Discord が拒否し、その配送は [outbox の dispatcher](#outbox-の-dispatcher) で述べるとおり失敗する。
請求の通知が支払者に届くのは、支払者が Bot とサーバーを共有し、Bot からの DM を受け付けている場合に限られる。

## 設定

設定は `.env` から読まれる環境変数である。
[.env.example](../.env.example) にすべての設定とその制約が載っている。
未知の `RAPIDWIRE_` の名前は起動時に拒否される。
運営者がよく変更するのは次の設定である:

| 設定 | 既定値 | 意味 |
| --- | --- | --- |
| `RAPIDWIRE_DB_PASSWORD` | 必須 | アプリケーションのデータベースユーザーのパスワード |
| `RAPIDWIRE_DB_NAME` | `rapidwire` | データベース名。ASCII 英字、数字、`_` からなる 1 から 64 文字で、数字だけではない |
| `RAPIDWIRE_DB_USER` | `rapidwire` | データベースユーザー |
| `RAPIDWIRE_DISCORD_TOKEN` | 必須 (`bot` と `worker`) | Discord Bot のトークン |
| `RAPIDWIRE_OPERATOR_ACCOUNT_IDS` | 空 (誰もいない) | バックアップの一覧表示とダウンロードを許可する Discord ユーザー ID のカンマ区切り |
| `RAPIDWIRE_OUTBOX_POLL_INTERVAL_SECONDS` | `2.0` | worker が outbox をポーリングする間隔の秒数 |
| `RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS` | `1800` | ステークの利息計上を実行する間隔の秒数 |
| `RAPIDWIRE_STAKE_ACCRUAL_STALENESS_SECONDS` | `10800` | この時間以上計上されていないステークは次の実行で計上される。間隔以上の値とする |
| `RAPIDWIRE_GUILD_DIRECTORY_STALENESS_SECONDS` | `120` | Bot の最後のギルド一覧のハートビートがこの経過時間を超えると、API と worker はギルドの情報を不明として扱う |
| `RAPIDWIRE_BACKUP_INTERVAL_SECONDS` | `86400` | 最新の完全なバックアップから次の定期バックアップの期限までの秒数。60 以上とする |
| `RAPIDWIRE_BACKUP_RETENTION` | `14` | 保持するバックアップの世代数 |
| `RAPIDWIRE_BACKUP_DIR` | `/var/lib/rapidwire/backups` | コンテナ内でバックアップを書き込む場所。compose はそこに `backups` ボリュームをマウントする |
| `RAPIDWIRE_LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` または `CRITICAL` |

## マイグレーション

`migrate` サービスは `rapidwire migrate` を実行し、Alembic のマイグレーションを最新のリビジョンまで適用する。
これはアプリケーションのサービスが起動する前に `docker compose up -d` のたびに実行され、最新のデータベースに適用しても何も変わらない。
任意のときに実行するには `docker compose run --rm migrate` を使う。

## worker のジョブ

worker は 1 つのプロセスで 3 つのジョブを実行し、それぞれ独自のループで動く。
ジョブの間隔は前回の実行の終了から測り、失敗した実行はログに記録されて次の間隔で再試行され、ほかのジョブには影響しない。

| ジョブ | 間隔 | データベースのロック |
| --- | --- | --- |
| `outbox_dispatcher` | `RAPIDWIRE_OUTBOX_POLL_INTERVAL_SECONDS` | `outbox_dispatcher` (60 秒のリース) |
| `stake_accrual` | `RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS` | `stake_accrual` (5 分のリース) |
| `backup_check` | 60 秒 | なし (期限が来たバックアップは、1 分ごとに更新する 5 分の `backup` リースを取る) |

ロックは `job_lock` テーブルの行であり、データベースの時計に基づいてリースされ、保持している間は更新される。
worker が強制終了されたために解放されなかったロックはリースの終わりに失効するため、再起動した worker はジョブが再び動くまでその時間だけ待つことがある。

### outbox の dispatcher

Discord を必要とする台帳の操作は、自身のトランザクション内で outbox の行を書き込み、dispatcher はコミットされた行を作成順に配送する。
ただし、再試行やレート制限を待っている行は後の行に追い越されることがある:

- `claim_created`: 請求とそのボタン (`bot` サービスが処理する) を載せた、支払者への DM。請求がもう未払いでない場合、または支払者が請求者からの通知をもう受け付けていない場合はスキップされる。
- `discord_send`: コントラクトのメッセージ。コントラクトがまだそのサーバーで Discord の権限を持っていることと、チャンネルがそのサーバーに属していることを確認してから投稿する。
- `discord_add_role` と `discord_remove_role`: コントラクトによるロールの変更。同じ権限の確認を行う。

各行は最終的に `sent`、`failed`、`skipped` のいずれかになり、失敗がそれを登録した実行、その残高、その移動を変えることは決してない。

- 配送は少なくとも 1 回である: Discord がリクエストを受け付けた後、結果がコミットされる前に worker が停止するかデータベース接続を失った場合や、Discord がすでに受け付けていたリクエストに対してタイムアウトまたはサーバーエラーが返った場合、その行は再送される。メッセージは outbox ID から導いた nonce を持つため、Discord は数分の nonce のウィンドウ内の再送を破棄するが、それ以降の再送は 2 通目のメッセージを投稿し、繰り返されたロールの変更はそれ以上の効果を持たない。
- 試行とは、Discord に少なくとも 1 つのリクエストを送った行の 1 回の処理であり、行の `attempts` はそれだけを数える。
- ネットワークエラー、タイムアウト、Discord のサーバーエラーは再試行される: n 回目の再試行は min(900, 5 x 2^(n-1)) 秒の半分から全部までのランダムな時間を待ち、8 回目の試行が失敗すると行は `failed` になり理由 `retries_exhausted` となる。その結果には、Discord の最後の応答があればそれ (ステータス、コード、メッセージ) が入り、RapidWire 自身による失敗の説明は `last_error` に残る。
- worker 内の予期しないエラーは、その試行がすでにリクエストを送っていた場合は同じように再試行される。リクエストの前に起きたエラーは再試行のたびに繰り返されるため、その行はただちに理由 `internal_error` で失敗する。
- Discord のレート制限は試行を消費せずに待つ。グローバルなレート制限は、それが終わるまで dispatcher を停止させる。
- その他の 4xx の応答は、その段階の理由 (例えば `message_rejected` や `role_change_rejected`) と Discord のステータス、コード、メッセージとともに、行をただちに `failed` にする。
- 401 の応答は Discord が Bot のトークンを拒否したことを意味する: dispatcher は 1 回あたり 60 秒停止し、そのたびにエラーをログに出し、`.env` のトークンが直されて worker が作り直されると再開する。

コントラクトの操作の結果は `GET /v1/executions/{execution_id}` の `deliveries` に現れる ([api.md](api.md#実行) を参照)。
請求の通知はそこには載らず、その結果は worker のログと、`outbox` テーブルの `status`、`attempts`、`last_error`、`result` 列にある。

### ステークの利息計上

このジョブは `RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS` ごとに、`RAPIDWIRE_STAKE_ACCRUAL_STALENESS_SECONDS` 以上計上されていないすべてのステークに利息を計上し、各ステークをそれぞれのトランザクションで処理して、利息を `interest` の実行として記録する。
利息が 1 最小単位に満たないステークは、計上時刻と繰越額の端数だけが進み、実行は記録されない。
計上の際に通貨の供給量がすでに上限に達していたステークも利息を得ない: その計上時刻は進み、繰越額の端数は捨てられ、実行は記録されず、その通貨の利息は永久に停止する。
ログはこの両方を `without_interest` として数える。
預け入れ、引き出し、利率の変更は、それらが触れるステークを自ら計上するので、このジョブは最近誰も触れていないステークを受け持つ。
計上に失敗したステークは、その ID とともにログに記録され、次の実行で再試行される。

### 定期バックアップ

worker は 60 秒ごとにバックアップの期限が来たかを確認する: 期限は、最新の完全な世代のファイルが書き込まれてから `RAPIDWIRE_BACKUP_INTERVAL_SECONDS` 後であり、完全な世代がなければただちにである。
期限が来たバックアップは `backup` のリースのもとで実行され、ほかのプロセス (要求に応じた `docker compose run --rm backup` など) がそれを保持している間は、その確認はスキップされる。
失敗した実行は期限を動かさないので、定期バックアップが失敗したか完了しなかった後、worker は再試行まで 15 分待つ。

### worker の停止

SIGTERM または SIGINT を受けると、worker は新しい実行の開始をやめ、実行中のジョブに終了まで 10 秒を与え、その後残りをキャンセルしてロックを解放するためにさらに 10 秒を与えてから、データベース接続を閉じて終了コード 0 で終了する。
dispatcher は配送中の行の後で停止し、キャンセルされたバックアップは途中のファイルを削除する。
期限が動いていないため、そのバックアップは次の起動後に再び実行される。
`worker` サービスの `stop_grace_period: 30s` は両方の待ち時間の合計より長いので、Docker はこの終了処理の間にこれを強制終了しない。
`bot` サービスも自身の終了処理のために同じ猶予期間を持つ。

## バックアップ

各バックアップの実行は、`/var/lib/rapidwire/backups` にマウントされた `backups` ボリュームに 2 つのファイルからなる 1 世代を書き込む:

| ファイル | 内容 |
| --- | --- |
| `rapidwire-dump-20261105T030000Z.sql.gz` | データベースの `mysqldump --single-transaction --hex-blob` を gzip 圧縮したもの |
| `rapidwire-export-20261105T030000Z.tar.gz` | アプリケーション形式のエクスポート。テーブルごとに 1 つの JSON Lines ファイルと、行数、SHA-256 チェックサム、台帳の不変条件の値を記録したマニフェストからなる |

タイムスタンプは実行開始時の UTC であり、1 世代の両方のファイルが同じタイムスタンプを持つ。
ファイルの書き込み方法、dump の正確なオプション、エクスポート形式は [backups.md](backups.md) で説明する。

- スケジュール: [定期バックアップ](#定期バックアップ) で述べるとおり、バックアップの期限は最新の完全な世代が書き込まれてから `RAPIDWIRE_BACKUP_INTERVAL_SECONDS` (既定値 1 日) 後である。失敗した実行は 15 分ごとに再試行されるので、間隔は一定の周期ではない。
- 保持処理はすべての実行の後に動き、今回の実行の世代を常に残し、完全な世代 (両方のファイルが書き込まれたもの) だけを数える: 成功した実行の後は、今回の世代を含む最新の `RAPIDWIRE_BACKUP_RETENTION` 個の完全な世代を残してそれより古いものを削除し、失敗した実行は完全な世代を 1 つも削除しない。
- 失敗した実行が残す不完全な世代のうち、残すのは最新のものだけであり、それもどの完全な世代よりも新しい間に限られる。そのため、ボリュームが持つのは最大で `RAPIDWIRE_BACKUP_RETENTION` 個の完全な世代と 1 つの不完全な世代である。
- 要求に応じた実行: `docker compose run --rm backup` はいまバックアップを取る。両方のファイルを書き込めたときは 0、書き込めたがスナップショットが台帳の不変条件に違反するときは 4、実行が失敗した、リースを失った、または別のバックアップが実行中のときは 1 で終了する。両方のファイルを書き込んだ場合、その世代は保持処理で数えられ、次の定期バックアップの期限はそこから 1 間隔後になる。両方のファイルを書き込まなかった実行は期限を変えない。
- バックアップはサーバーの `backups` ボリュームにとどまる。RapidWire がそれをほかの場所へコピーすることはないので、サーバー外のコピーは運営者がダウンロードしたときにのみ存在する。
- ボリュームには、両方の種類の保持する世代を収める容量が必要である。

`docker compose exec api ls -l /var/lib/rapidwire/backups` はサーバー上のファイルを一覧表示する。

## バックアップのダウンロード

運営者は Web エクスプローラーの運営者ページからバックアップをダウンロードする。
このページは [api.md](api.md#運営者用バックアップ) で説明する HTTP API を使い、`curl` と Python クライアントは同じ API を直接呼ぶ:

1. 運営者の Discord ユーザー ID を `.env` の `RAPIDWIRE_OPERATOR_ACCOUNT_IDS` に追加し、`docker compose up -d --force-recreate api` で API のコンテナを作り直す。
2. [bot.md](bot.md#api-トークンのメンション) で説明するとおり、Bot に `API` とメンションして API トークンを取得する。トークンは運営者自身の勘定に属し、新しいトークンを発行すると前のトークンは無効になる。
3. エクスプローラーのサイトで `/operator.html` を開き、トークンを入力して送信すると、バックアップが新しい順に 1 ページ 15 件で一覧表示され、ほかのページへのボタンが付く。
4. ファイルのダウンロードボタンを選ぶ: ページは、ファイルのサイズとブラウザで計算した SHA-256 が一覧と `X-Checksum-SHA256` ヘッダーに一致した場合にのみファイルを保存し、そうでなければどの確認が失敗したかを表示する。

ページはトークンを自身のメモリにのみ保持し、忘れるボタン、ページから離れたとき、401 または 403 の応答のときにそれを忘れる。
そのトークンの扱いと確認は [web.md](web.md#運営者用ページ) で説明する。
ブラウザが SHA-256 を計算するのは HTTPS 上 (または `localhost` 上) に限られる。
それ以外では、ページはファイルを未検証として保存し、その `sha256sum` を一覧と照合する必要がある。

ブラウザを使わない場合は、`GET /v1/admin/backups` でバックアップを一覧表示し、`GET /v1/admin/backups/{name}` で 1 つをダウンロードする:

```
curl -fsS -H "Authorization: Bearer $TOKEN" https://rapidwire.example.com/v1/admin/backups
curl -fsS -O -D headers.txt -H "Authorization: Bearer $TOKEN" https://rapidwire.example.com/v1/admin/backups/rapidwire-export-20261105T030000Z.tar.gz
sha256sum rapidwire-export-20261105T030000Z.tar.gz
```

- 一覧はページ分けされ、新しい順で、既定では 1 ページ 15 件、最大 100 件 (`?page=2&per_page=100`) であり、その `total` はディレクトリ内のすべてのバックアップを数える。
- 一覧は各ファイルの名前、完了時刻、サイズ、SHA-256 を示す。ダウンロードは同じダイジェストを `X-Checksum-SHA256` と `Repr-Digest` に載せ、ダウンロードしたファイルの `sha256sum` はそれと等しくなければならない。
- 運営者でない勘定は、すべてのバックアップのパスで 403 を受ける。
- 成功した一覧表示とダウンロードはすべて、運営者の勘定の公開の実行として記録される。

`client/` の Python クライアントはダイジェストを自ら確認する: `download_backup(name, path)` は `.partial` ファイルに書き込み、サイズと SHA-256 が一致した後でのみそれを `path` に改名し、そうでなければ `ChecksumMismatchError` を送出する。

```python
from rapidwire_client import RapidWireClient

with RapidWireClient("https://rapidwire.example.com", token=TOKEN) as client:
    for item in client.paginate(client.list_backups):
        client.download_backup(item.name, item.name)
```

## 復元による検証

バックアップは運営者自身のマシンで検証する。
ダウンロードした世代が使えることを確かめたいときにいつでも、それをローカルの MySQL コンテナに復元して結果を確認する。
サーバー上のものは何も関与しない。

### ツールのインストール

クローンから Python 3.12 でパッケージをインストールするか、クローンからイメージをビルドする。
エクスポートの復元はインストールされたバージョンが知らないスキーマのリビジョンを拒否するため、クローンはサーバーが動かしているコミットでなければならない。
サーバーのクローンで `git rev-parse HEAD` を実行するとそのコミットが表示される。

```
git clone https://github.com/RapidWire/RapidWire.git
cd RapidWire
git checkout <commit printed by git rev-parse HEAD on the server>
python3.12 -m venv .venv
.venv/bin/pip install -c constraints.txt .
. .venv/bin/activate
```

- `rapidwire backup verify` と `rapidwire backup restore` は RapidWire の設定を読まないので、`.env` は不要である。
- dump の復元は `mysql` コマンドラインクライアントを実行する。これは MySQL 8.4 のクライアントであり、`PATH` 上になければならない。ファイルの検証とエクスポートの復元にはクライアントは不要である。
- イメージにはすでにクライアントが入っている: `docker build -t rapidwire:local .` でビルドする (イメージは amd64 専用なので、arm64 のマシンでは `--platform linux/amd64` を付ける)。

### ファイルの確認

`rapidwire backup verify FILE...` は、ダウンロードしたファイルをデータベースなしで確認する: エクスポートはそのマニフェスト (行数、サイズ、SHA-256 チェックサム、行のエンコーディング、再計算した不変条件の値) と照合し、dump は mysqldump のヘッダーで始まりその完了マーカーで終わる完全な gzip ストリームであるかを確認する。

```
rapidwire backup verify rapidwire-dump-20261105T030000Z.sql.gz rapidwire-export-20261105T030000Z.tar.gz
```

ファイルごとに `OK`、`MISMATCH`、`UNREADABLE` のいずれかを表示し、すべてのファイルが合格すれば 0、いずれかのファイルに不一致があれば 4、それ以外で読めないファイルがあれば 1 で終了する。

### ローカルの MySQL への復元

ローカルのサーバーのパスワードを決め、それをシェルに 1 回だけ読み込ませて、コマンドラインに決して現れないようにする。
docker には変数名だけを渡し、値はシェルの環境から取らせる。
`127.0.0.1` にバインドしたローカルの MySQL 8.4 コンテナを起動し、TCP 接続を受け付けるまで待ち、復元するファイルごとに空のデータベースを 1 つ作成する:

```
read -rs MYSQL_PWD
export MYSQL_PWD
export MYSQL_ROOT_PASSWORD="$MYSQL_PWD"
docker network create rw-restore
docker run -d --rm --name rw-restore-mysql --network rw-restore -p 127.0.0.1:3307:3306 -e MYSQL_ROOT_PASSWORD mysql:8.4
until docker exec -e MYSQL_PWD rw-restore-mysql mysqladmin ping --host=127.0.0.1 --user=root --silent; do sleep 1; done
docker exec -e MYSQL_PWD rw-restore-mysql mysql --user=root --execute="CREATE DATABASE rw_dump; CREATE DATABASE rw_export"
```

インストールしたパッケージで復元する場合は、エクスポートした `MYSQL_PWD` からパスワードが読まれる:

```
rapidwire backup restore --target-url mysql://root@127.0.0.1:3307/rw_dump rapidwire-dump-20261105T030000Z.sql.gz
rapidwire backup restore --target-url mysql://root@127.0.0.1:3307/rw_export rapidwire-export-20261105T030000Z.tar.gz
```

またはイメージで復元する。
イメージは共有のネットワーク上で MySQL コンテナに名前で到達し、カレントディレクトリからファイルを読む (イメージは特権のないユーザーで動くので、ファイルはほかのユーザーから読める必要がある):

```
docker run --rm --network rw-restore -v "$PWD:/backups:ro" -e MYSQL_PWD rapidwire:local backup restore --target-url mysql://root@rw-restore-mysql:3306/rw_dump /backups/rapidwire-dump-20261105T030000Z.sql.gz
docker run --rm --network rw-restore -v "$PWD:/backups:ro" -e MYSQL_PWD rapidwire:local backup restore --target-url mysql://root@rw-restore-mysql:3306/rw_export /backups/rapidwire-export-20261105T030000Z.tar.gz
```

- 復元先は、テーブルもストアドルーチンも持たない既存のデータベースでなければならず、その名前は ASCII 英字、数字、`_` からなる 1 から 64 文字で、数字だけではない。コマンドはそれ以外を拒否する。
- パスワードは、URL にあればそこから (`mysql://user:password@host:port/db`)、なければ `MYSQL_PWD` から取る。パスワードが表示されたりログに記録されたりすることは決してない。
- dump は、先頭のバイトが mysqldump のヘッダーであることを確認した後で `mysql` クライアントに流し込まれ、完了マーカーのないものは不一致として報告される。エクスポートは、それが取られたときのスキーマのリビジョンに読み込まれて再びエクスポートされ、すべてのテーブルの行数、サイズ、SHA-256 がマニフェストと等しくなければならない。
- どちらの種類も、その後に復元したデータベースに対して台帳の不変条件の検証器を実行する。これは `rapidwire verify-invariants` と同じ確認である。
- コマンドは、復元が検証に通れば 0、チェックサムまたは不変条件の値の不一致、または不変条件違反があれば 4、復元そのものが失敗すれば 1 で終了する。

復元の詳細は [backups.md](backups.md#検証のための復元) で説明する。

終わったらローカルのコンテナとネットワークを削除する。
コンテナは `--rm` 付きで起動したので、停止するとそのデータも削除される:

```
docker stop rw-restore-mysql
docker network rm rw-restore
unset MYSQL_PWD MYSQL_ROOT_PASSWORD
```

## サーバーのボリュームを削除しない

データベースとすべてのバックアップは名前付きボリューム `mysql-data` と `backups` にしか存在しないので、これらを削除するとすべてのデータが回復不能に失われる。

- サーバー上では `docker compose down -v`、`docker compose down --volumes`、`docker volume rm`、`docker volume prune` を決して実行せず、`docker system prune --volumes` も決して実行しない。
- サービスを止めるには `docker compose stop`、または `-v` なしの `docker compose down` を使う。後者はコンテナとネットワークを削除するがボリュームは残し、`docker compose up -d` で同じデータのまま再び起動できる。
- 1 つのサービスを止めるには `docker compose stop worker` (または別のサービス名) を使う。

## ログ

RapidWire のすべてのプロセスは、ログを JSON Lines として標準出力に書く。
1 レコードが 1 つのオブジェクトであり、`ts` (UTC、ミリ秒)、`level`、`logger`、`msg`、レコードの追加フィールド、エラーの場合は `exc_info` を持つ:

```
{"ts": "2026-11-05T03:00:12.345+00:00", "level": "INFO", "logger": "rapidwire.backup", "msg": "backup finished", "generation": "20261105T030000Z", "files": ["rapidwire-dump-20261105T030000Z.sql.gz", "rapidwire-export-20261105T030000Z.tar.gz"], "deleted": [], "invariant_violations": 0}
```

- ログは `docker compose logs` で読む。例えば `docker compose logs -f worker` や `docker compose logs --since=24h --no-log-prefix api` を使い、JSON は `jq` などのツールで絞り込む。
- 起動時の構成エラーと、`rapidwire verify-invariants`、`rapidwire backup verify`、`rapidwire backup restore` のレポートはプレーンテキストである。これらのレポートを出すコマンドは、ログの行を標準エラーに書く。
- ログの行は端末のエスケープシーケンスを含まない: 色やその他の ANSI エスケープコードはすべての文字列から取り除かれ、uvicorn の `color_message` フィールド (`msg` の色付きの複製) は含まれない。
- 秘密情報がログに現れることはない: データベースのパスワードと Discord のトークン (およびプロセスの環境にある場合の `MYSQL_ROOT_PASSWORD` と `TUNNEL_TOKEN`) は、現れるところすべてで `***` に置き換えられ、名前に `token`、`password`、`secret` などの語を含む追加フィールドも同様であり、発行した API トークンは決してログに記録されない。
- Docker は各コンテナのログを json-file 形式で `max-size` 20m、`max-file` 5 として保持するので、コンテナが多くログを出すほど古い行から捨てられる。更新のときのようにコンテナを作り直すと、それ以前のログは破棄される。

RapidWire には監視やアラートの仕組みはなく、問題はこれらのログを読んで見つける。
探す価値のあるメッセージ:

| メッセージ | レベル | 意味 |
| --- | --- | --- |
| `starting worker` / `worker stopped` | INFO | worker が起動または停止した |
| `job failed; it runs again at its next tick` | ERROR | worker のジョブの実行が例外を送出した。`job` フィールドがジョブ名を、`exc_info` が原因を示す |
| `job lock lost` | ERROR | worker がジョブのロックを更新できず、その実行を停止した |
| `outbox delivery will be retried` | WARNING | 配送が再試行可能なエラーで失敗した |
| `outbox delivery failed` | WARNING | Discord が配送を拒否した。行は `failed` になる |
| `outbox delivery failed after its last attempt` | WARNING | 配送が理由 `retries_exhausted` で失敗した |
| `outbox row has a malformed payload` | ERROR | outbox の行を読めなかった。行は `failed` になる |
| `Discord refused the bot token with status 401; the dispatcher pauses` | ERROR | Bot のトークンが誤っているか、リセットされた |
| `stake accrual finished` | INFO (一部が失敗したときは WARNING) | 利息を入金したステーク (`credited`)、利息なしで進めたステーク (`without_interest`)、失敗したステーク (`failed`) の数 |
| `backup finished` | INFO | バックアップが両方のファイルを書き込んだ |
| `backup failed` | ERROR | バックアップの実行が失敗した。`errors` が理由を示す |
| `scheduled backup did not complete; it is tried again later` | WARNING | worker は定期バックアップを後で再試行する |
| `the exported snapshot violates the ledger invariants` | ERROR | エクスポートが稼働中のデータベースに不変条件違反を見つけた |
| `removed files of an interrupted backup run` | WARNING | 以前の実行の途中のファイルが削除された |
| `invariants verified` | INFO | `rapidwire verify-invariants` が終了した。`violations` が違反の数である |
| `command failed` | ERROR | コマンドが予期しないエラーで停止した |

## 不変条件の確認

`rapidwire verify-invariants` は台帳全体を読んでその不変条件を確認する: すべての通貨の残高の合計が 0 であること、ステーク、許可額、プール、コントラクトが整合していること、失敗した実行がガスだけを動かしたこと、すべての実行の `contract_id` がその種類と入力に一致することである。
サーバー上で、稼働中の worker のコンテナ内で実行する:

```
docker compose exec worker rapidwire verify-invariants
```

通貨ごと、プールごとに 1 行、要約の 1 行、違反ごとに 1 行を表示し、台帳に問題がなければ 0、違反が見つかれば 4、確認そのものが失敗すれば 1 で終了する。
`rapidwire verify-invariants --help` がすべての確認を一覧表示する。
