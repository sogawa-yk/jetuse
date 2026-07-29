"""SPIKE-M1 の架空チャンクセット（10 件）。

顧客データは一切持ち込まない（tasks/SPIKE-M1.md 禁止事項）。実在しない
「サンプル在庫連携 API」の仕様書を模した完全な創作である。

版フィルタの検証が成立するよう、旧版 3 件（current_version=False）は
現行版と**意味的に近い near-duplicate** にしてある。フィルタ無しなら
旧版が上位に返り、フィルタ有りなら 1 件も返らない、という対照が取れる。
"""

import hashlib
from typing import Any

FILE_NAME = "サンプル在庫連携API仕様書.xlsx"

# (chunk_id, kind, version, current, sheet, cells, text)
_RAW: list[tuple[str, str, str, bool, str, str, str]] = [
    ("c01", "spec", "2.0", True, "API一覧", "B12:F12",
     "在庫照会API GET /v2/inventory は店舗コードと商品コードを指定して在庫数を返す。"
     "レスポンスは1リクエストあたり最大1000件まで返却する。"),
    ("c02", "spec", "2.0", True, "API一覧", "B13:F13",
     "在庫更新API POST /v2/inventory は差分数量を受け取り在庫数を更新する。"
     "冪等性キー Idempotency-Key ヘッダを必須とする。"),
    ("c03", "spec", "2.0", True, "API一覧", "B14:F14",
     "出荷予定照会API GET /v2/shipments は出荷予定日と数量の一覧を返す。"
     "期間指定は最大31日である。"),
    ("c04", "spec", "2.0", True, "API一覧", "B15:F15",
     "エラーコードは4xx系を業務エラー、5xx系をシステムエラーとし、"
     "本文に code と message と traceId を含める。"),
    ("c05", "constraint", "2.0", True, "制約", "C5:E5",
     "レート制限は1分あたり600リクエストである。超過時は429を返し"
     "Retry-Afterヘッダに待機秒数を設定する。"),
    ("c06", "constraint", "2.0", True, "制約", "C6:E6",
     "認証方式はOAuth2 client_credentialsとする。アクセストークンの有効期限は3600秒である。"),
    ("c07", "constraint", "2.0", True, "制約", "C7:E7",
     "同時接続数の上限は50セッションである。上限超過時は503を返す。"),
    # --- 旧版（current_version=False）。現行版と近い内容だが値が違う ---
    ("c08", "spec", "1.0", False, "API一覧", "B12:F12",
     "在庫照会API GET /v1/inventory は店舗コードと商品コードを指定して在庫数を返す。"
     "レスポンスは1リクエストあたり最大200件まで返却する。"),
    ("c09", "constraint", "1.0", False, "制約", "C5:E5",
     "レート制限は1分あたり120リクエストである。超過時は429を返す。"),
    ("c10", "constraint", "1.0", False, "制約", "C6:E6",
     "認証方式はAPIキーをX-Api-Keyヘッダに設定する方式とする。キーの有効期限は無期限である。"),
]


def chunks() -> list[dict[str, Any]]:
    """架空チャンク 10 件を返す。sha256 は本文から決定的に算出する。"""
    out = []
    for cid, kind, version, current, sheet, cells, text in _RAW:
        out.append({
            "chunk_id": cid,
            "text": text,
            "file": FILE_NAME,
            "version": version,
            "sheet": sheet,
            "cells": cells,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "kind": kind,
            "current_version": current,
        })
    return out


# 3 方式で同一に使う検索クエリ（旧版が近傍に来るよう意図的に選んである）
QUERY = "在庫照会APIのレート制限と1回の最大取得件数を教えて"
