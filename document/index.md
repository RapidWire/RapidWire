# RapidWire ドキュメント

RapidWire は Discord を基盤とする経済圏であり、サーバーごとの通貨、公開台帳、ステーク、複数経路の DEX、スマートコントラクトを備え、Discord Bot、HTTP API、Web エクスプローラーを通じて使う。
[README](../README.md) はアーキテクチャ、クイックスタート、開発環境の準備を示し、以下の文書は読者別にまとめている。

## すべての読者

- [concepts.md](concepts.md): 台帳、通貨とその管理、送金、許可額、請求、ステーク、DEX、コントラクト。

## Discord ユーザー

- [walkthrough.md](walkthrough.md): サーバーの通貨の作成からスワップとコントラクトの呼び出しまでを、各スラッシュコマンドと対応する API とともに順に案内する。
- [bot.md](bot.md): すべてのスラッシュコマンドとそのパラメータと応答、請求の通知、スワップの確認、API トークンのメンション。
- [web.md](web.md): Web エクスプローラーのページ、URL の体系と検索、勘定と実行の表示方法。

## API とクライアントの開発者

- [api.md](api.md): `/v1` HTTP API の認証、冪等性、数値の形式、エラー、ページ送り、すべてのエンドポイント。
- [../client/README.md](../client/README.md): Python クライアント `rapidwire_client` (同期と非同期) のページ送り、金額、コントラクトの値、エラー、検証付きダウンロード。

## コントラクトの作成者

- [contracts/README.md](contracts/README.md): コントラクトとは何か、ソースから登録までの流れ、サンプル。
- [contracts/language.md](contracts/language.md): コントラクト言語の型、文、式、コントラクトの呼び出し、失敗、制限。
- [contracts/builtins.md](contracts/builtins.md): すべての組み込み機能のシグネチャ、動作、失敗、ガス。
- [contracts/costs.md](contracts/costs.md): ガスの価格と、すべての命令と組み込み機能のコスト、サイズによる割増。
- [contracts/cli.md](contracts/cli.md): `rwc` ツール、`rwc run` の状態ファイル、エディタの設定。
- [contracts/binary-format.md](contracts/binary-format.md): RWC1 バイナリの配置と、登録時にサーバーが行う検査。

### サンプル

各サンプルは完全なコントラクトであり、その状態ファイルが `rwc run` の模擬台帳の初期値になる。

- [contracts/samples/role_vending.py](contracts/samples/role_vending.py) と [role_vending.state.json](contracts/samples/role_vending.state.json): Discord のロールを RAPID で販売し、払い過ぎた分を返金する。
- [contracts/samples/lottery.py](contracts/samples/lottery.py) と [lottery.state.json](contracts/samples/lottery.state.json): `random()` を使い、コントラクトの資金から払い出すサイコロの賭け。
- [contracts/samples/members_only.py](contracts/samples/members_only.py) と [members_only.state.json](contracts/samples/members_only.state.json): ある通貨またはロールの保有者向けの日次報酬とクラブへの投稿。
- [contracts/samples/exchange_broker.py](contracts/samples/exchange_broker.py) と [exchange_broker.state.json](contracts/samples/exchange_broker.state.json): `min_out` 付きの DEX スワップを代替手段とする固定レートの両替窓口。

## 運営者

- [operations.md](operations.md): Docker Compose による配備、Discord の準備、設定、マイグレーション、worker のジョブ、バックアップ、復元の検証、ログ。
- [backups.md](backups.md): バックアップのジョブ、ダンプとエクスポートのファイル、エクスポートの形式、検証と復元の検査。
- [web.md](web.md#運営者用ページ): バックアップを一覧にしてダウンロードする運営者ページ。
