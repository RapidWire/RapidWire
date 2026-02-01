# スマートコントラクト開発を始める

RapidWireでは、Pythonのサブセットを使用してスマートコントラクトを記述できます。
このガイドでは、開発環境のセットアップから、最初のコントラクトのデプロイまでを解説します。

## 開発環境の準備

コントラクトはプレーンテキスト（`.py`ファイル）で記述します。
IDE（VS Codeなど）を使用すると、シンタックスハイライトや補完が効くため便利です。

### SDKの導入 (推奨)

開発時に型ヒントや自動補完を利用するために、RapidWireリポジトリに含まれる `sdk.py` をインポートすることをお勧めします。

```python
# ファイルの先頭に記述
from RapidWire.sdk import *
```
※ `RapidWire` フォルダがパスの通った場所にあるか、あるいは開発用フォルダにコピーしてください。

## Hello World

最も単純なコントラクトを作成してみましょう。
このコントラクトは、誰かが送金してきたら、その人に感謝のメッセージを返信します。

```python
from RapidWire.sdk import *

def main():
    # 入力データ（メッセージ）があれば、それをログに保存する
    if input_data:
        storage['last_message'] = input_data

    # 実行者にメッセージを返す
    output("Thank you for executing my contract!")
```

このコードを `hello.py` として保存します。

## デプロイ（設定）方法

Discord上で `/contract set` コマンドを使用します。

1.  Discordのチャット欄に `/contract set` と入力します。
2.  `script` オプションに、先ほど作成した `hello.py` をドラッグ＆ドロップします。
3.  Enterキーを押して実行します。

成功すると、あなたのアカウントにコントラクトが設定されます。

## 実行テスト

他のユーザー（または自分自身）から、このコントラクトを実行してみましょう。

```
/execute_contract user:@YourName input_data:Hello!
```

Botから "Thank you for executing my contract!" という返信が来れば成功です。

## 注意事項

- **`main()` 関数**: コントラクトの処理はすべて `main()` 関数の中に記述してください。それ以外の場所（グローバルスコープ）に書かれた処理は、デプロイ時に無視されます。
- **ステートレス性**: `main()` 関数は毎回ゼロから実行されます。前回の実行結果を保持したい場合は、`storage` 変数を使ってデータを永続化する必要があります。
- **Pythonの制限**: すべてのPython機能が使えるわけではありません（例: `import`（SDK以外）、`try-except`、クラス定義などは使えません）。詳細は [SDKリファレンス](./sdk.md) を参照してください。
