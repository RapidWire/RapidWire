# 導入とセットアップ

RapidWireをご自身のサーバー環境に構築するための手順を説明します。
RapidWireは「Discord Botプロセス」と「API/Webサーバープロセス」の2つのコンポーネントで構成されています。

## 前提条件

以下のソフトウェアがインストールされている必要があります。

- **Python 3.9 以上**: [公式サイト](https://www.python.org/downloads/)からダウンロードしてください。
- **MySQL 8.0 以上**: データベースサーバー。
- **Git**: リポジトリのクローンに使用します。

## 1. インストール

まず、GitHubリポジトリからソースコードをクローン（ダウンロード）し、ディレクトリに移動します。

```bash
git clone https://github.com/your-username/RapidWire.git
cd RapidWire
```

次に、Pythonの依存ライブラリをインストールします。仮想環境（venv）の使用を推奨します。

```bash
# 仮想環境の作成（任意）
python -m venv venv
# Linux/Macの場合
source venv/bin/activate
# Windowsの場合
.\venv\Scripts\activate

# 依存パッケージのインストール
pip install -r requirements.txt
```

## 2. 設定ファイルの作成

`config.py.example` をコピーして `config.py` を作成し、環境に合わせて編集します。

```bash
cp config.py.example config.py
```

`config.py` をテキストエディタで開き、以下の項目を設定してください。

### データベース設定
ご自身のMySQLサーバーの接続情報を入力します。

```python
class MySQL:
    host: str = "localhost"
    port: int = 3306
    user: str = "root"      # MySQLのユーザー名
    password: str = "password"  # MySQLのパスワード
    database: str = "rapid_wire" # 使用するデータベース名
```

### Discord設定
Discord Developer Portalで取得したBotトークンと、管理者ユーザーのIDを設定します。

```python
class Discord:
    token: str = "ここにTOKENを貼り付け"
    admins: list[int] = [123456789012345678] # 管理者のDiscord User ID
```

### 経済パラメータ設定（高度な設定）
`RapidWireConfig` クラス内の値を変更することで、経済システムの挙動をカスタマイズできます。

- `Contract.max_cost`: コントラクトの最大実行コスト。
- `Staking.rate_change_timelock`: 金利変更に必要な待機秒数（デフォルトは7日）。
- `Swap.fee`: DEXの手数料率（30 = 0.3%）。
- `Gas.price`: コントラクト実行時の基本手数料。

## 3. データベースの初期化

MySQLにログインし、使用するデータベースを作成した後、初期スキーマを適用します。

```bash
# データベースの作成（まだ存在しない場合）
mysql -u root -p -e "CREATE DATABASE rapid_wire CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# スキーマのインポート
mysql -u root -p rapid_wire < rapid-wire.sql
```

## 4. 起動

RapidWireを動作させるには、2つのプロセスを起動する必要があります。バックグラウンドで実行することをお勧めします。

### Discord Botの起動
メインのBot機能を担当します。

```bash
python main.py
```

### API/Webサーバーの起動
WebエクスプローラーとREST APIを提供します。

```bash
python server.py
```

デフォルトでは、 `http://localhost:8000` でWebサーバーが立ち上がります。
ブラウザでアクセスすると、RapidWire Network Explorerが表示されます。

## 5. 動作確認

DiscordサーバーでBotがオンラインになっていることを確認し、`/balance` コマンドなどを実行して応答があればセットアップ完了です。

次のステップ: [ユーザーガイド - コマンド一覧](../user_guide/commands.md)
