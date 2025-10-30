"""
RapidWire スマートコントラクト 開発用スタブファイル

このファイルは、RapidWireスマートコントラクトを作成する際の開発体験を向上させるためのものです。
IDE（例: VSCode, PyCharm）でこのファイルをプロジェクトに含めることで、
`tx`や`api`オブジェクトの型ヒントが有効になり、オートコンプリートやエラーチェックが機能するようになります。

**使用方法:**
1. このファイルを開発プロジェクトのディレクトリに配置します。
2. コントラクトのコードを記述するPythonファイルで、以下のように型情報をインポートします。

```python
# contract_script.py
from contract_api_stubs import *

# これで、txやapiオブジェクトのメンバーにアクセスする際に
# IDEがヒントを表示してくれるようになります。
print(tx["source"])
transfer(tx["dest"], 12345, tx["currency"], 100)
```

**注意:**
このファイルは純粋に型ヒントとドキュメントを提供するためのものであり、実際のコントラクト実行環境で
インポートして使用することはできません。RapidWireのサンドボックス環境には、これらのオブジェクトが
グローバル変数として自動的に提供されます。
"""
from typing import TypedDict, Optional, List, Dict, Tuple, Any

# --- グローバル変数: トランザクションコンテキスト ---

class TransactionContext(TypedDict):
    """
    受信するトランザクションの詳細を含む辞書（オブジェクト）。
    """
    source: int
    """送信者のユーザーID。"""
    dest: int
    """受信者（あなた）のユーザーID。"""
    currency: int
    """転送される通貨のID。"""
    amount: int
    """転送される通貨の量（整数）。"""
    input_data: Optional[str]
    """送信者によってトランザクションに添付されたデータ。"""
    transaction_id: int
    """このトランザクションの一意なID。"""

tx: TransactionContext = ...
"""
現在のトランザクションコンテキスト。
グローバルスコープで利用可能です。
"""


# --- グローバル変数: APIハンドラ ---

def get_balance(self, user_id: int, currency_id: int) -> int:
    """ユーザーの残高を取得します。"""
    ...

def get_transaction(self, tx_id: int) -> Optional[Dict[str, Any]]:
    """特定のトランザクションを取得します。"""
    ...

def transfer(self, source: int, dest: int, currency: int, amount: int) -> Dict[str, Any]:
    """
    新しい送金を開始します。
    注意: `source`はコントラクトの所有者でなければなりません。
    (例: `api.transfer(source=tx['dest'], ...)` )
    """
    ...

def search_transactions(self, source: Optional[int] = None, dest: Optional[int] = None, currency: Optional[int] = None, page: int = 1) -> List[Dict[str, Any]]:
    """トランザクションを検索します。"""
    ...

def get_currency(self, currency_id: int) -> Optional[Dict[str, Any]]:
    """通貨の詳細を取得します。"""
    ...

def create_claim(self, claimant: int, payer: int, currency: int, amount: int, desc: Optional[str] = None) -> Dict[str, Any]:
    """新しい請求を作成します。"""
    ...

def get_claim(self, claim_id: int) -> Optional[Dict[str, Any]]:
    """特定の請求を取得します。"""
    ...

def pay_claim(self, claim_id: int, payer_id: int) -> Dict[str, Any]:
    """請求に対して支払います。"""
    ...

def cancel_claim(self, claim_id: int, user_id: int) -> Dict[str, Any]:
    """請求をキャンセルします。"""
    ...

def execute_contract(self, destination_id: int, currency_id: int, amount: int, input_data: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    別のコントラクトを実行します。
    このコントラクトのアカウントから`destination_id`へ新しいトランザクションを作成し、
    相手のコントラクトを実行します。
    戻り値: (作成されたトランザクションの辞書, 相手のコントラクトからの返信メッセージ)
    """
    ...

def get_variable(self, user_id: int, key: bytes) -> Optional[bytes]:
    """
    コントラクトに関連付けられた永続的な変数を取得します。
    `user_id` は変数が属するユーザーIDです。通常は自分自身 (`tx['dest']`) を指定します。
    キーと値はバイト文字列として扱われます。
    """
    ...

def set_variable(self, key: bytes, value: bytes) -> None:
    """
    コントラクトに関連付けられた永続的な変数を設定します。
    このコントラクトの所有者 (`tx['dest']`) に変数を保存します。
    キーは8バイト以下、値は16バイト以下でなければなりません。
    """
    ...


# --- グローバル変数: その他 ---

class Cancel(Exception):
    """
    受信するトランザクションをキャンセルするために発生させることができる例外。

    使用例: `raise Cancel("キャンセルの理由")`
    """
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

return_message: Optional[str] = ...
"""
トランザクションの送信者にメッセージを返すために設定できる変数。
この変数に文字列を設定すると、トランザクション完了後に送信者にその内容が送信されます。
"""
