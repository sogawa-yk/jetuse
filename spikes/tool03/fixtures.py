"""TOOL-03 の検証用フィクスチャ(**架空**の受注 API のボディ形)。

顧客データ・顧客名・案件名は持ち込まない。ここにあるのは実在しない品番・受注番号と、
tasks/TOOL-03.md に記録した**ボディの形**(入れ子オブジェクト / オブジェクトの配列 /
配列の中の配列)だけ。相手(env `TOOL03_ECHO_URL` の公開 https エコー)は受け取った JSON を
そのまま返すので、「入れ子のまま届いたか」は**相手が受け取った本文**で判定できる。

**秘密は送らない**。このタスクの検証に認証は要らないので、Vault の検証用トークンも作らない
(TOOL-02 と違う点。送らないものは漏れない)。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "ops"))
import _adb as adb  # noqa: E402  環境変数 → .env の順で読む(ops と同じ流儀)

# 相手の宛先はコードに焼かない(環境依存値は .env。雛形は .env.example の TOOL03_ECHO_URL)。
# 未設定なら e2e は実行を断る = 承認していない宛先へ送らない
ECHO_URL = adb.env("TOOL03_ECHO_URL").strip()


def echo_url(path: str) -> str:
    """同じエコー先の別パス(GET 用など)。**パスだけ**を差し替える。

    素朴な `ECHO_URL.replace("/post", "/get")` はホスト名の中の `post`
    (`postman-echo.com`)まで書き換えて別ホストへ向く(実際に踏んだ)。
    """
    from urllib.parse import urlparse, urlunparse

    p = urlparse(ECHO_URL)
    return urlunparse(p._replace(path=path))

PART_NUMBER = "JX-7742"

# 1 段の入れ子オブジェクト(実案件の「契約者情報設定」に相当する形)
CONTRACTOR_TOOL = {
    "name": "set_contractor",
    "description": (
        "受注システムに契約者情報を登録する。氏名・電話・住所(郵便番号/都道府県/以降)を"
        "まとめて渡す。契約者を登録・変更するときは必ずこれを使う"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "受注番号(例 ORD-1001)"},
            "contractor": {
                "type": "object",
                "description": "契約者情報",
                "properties": {
                    "name": {"type": "string", "description": "氏名"},
                    "phone": {"type": "string", "description": "電話番号"},
                    "address": {
                        "type": "object",
                        "description": "住所",
                        "properties": {
                            "zip": {"type": "string", "description": "郵便番号"},
                            "prefecture": {"type": "string", "description": "都道府県"},
                            "rest": {"type": "string", "description": "市区町村以降"},
                        },
                        "required": ["zip", "prefecture"],
                    },
                },
                "required": ["name", "address"],
            },
        },
        "required": ["order_id", "contractor"],
    },
    "method": "POST",
}

# オブジェクトの配列 + その中にさらに配列(実案件の「商品設定」「サービス情報設定」に相当)
ITEMS_TOOL = {
    "name": "set_order_items",
    "description": (
        "受注システムに商品明細をまとめて登録する。明細は複数行あり、各行に品番・数量と、"
        "その行に付けるオプション(コードと値)の一覧を持つ。明細を登録するときは必ずこれを使う"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "受注番号(例 ORD-1001)"},
            "items": {
                "type": "array",
                "description": "商品明細の配列",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string", "description": "品番(例 JX-7742)"},
                        "qty": {"type": "integer", "description": "数量"},
                        "options": {
                            "type": "array",
                            "description": "その明細に付けるオプションの配列",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "code": {"type": "string", "description": "オプションコード"},
                                    "value": {"type": "string", "description": "オプション値"},
                                },
                                "required": ["code"],
                            },
                        },
                    },
                    "required": ["sku", "qty"],
                },
            },
        },
        "required": ["order_id", "items"],
    },
    "method": "POST",
}

# 回帰用。TOOL-01 から形を変えていない平坦なスカラーだけのツール(GET)
FLAT_TOOL = {
    "name": "lookup_stock",
    "description": (
        "社内在庫システムに品番を問い合わせ、在庫数を返す。"
        "品番ごとの在庫を聞かれたら必ずこれを使う"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "part_number": {"type": "string", "description": f"品番(例 {PART_NUMBER})"},
        },
        "required": ["part_number"],
    },
    "method": "GET",
}

CONTRACTOR_QUESTION = (
    "受注番号 ORD-1001 の契約者を登録してください。"
    "氏名は山田太郎、電話は 06-1234-5678、住所は 〒530-0001 大阪府大阪市北区梅田1-2-3 です。"
)
ITEMS_QUESTION = (
    "受注番号 ORD-1001 に明細を登録してください。"
    "1行目は品番 JX-7742 を 3 個、オプションは COLOR=赤 と SIZE=L。"
    "2行目は品番 KM-1180 を 1 個、オプションは無し。"
)
