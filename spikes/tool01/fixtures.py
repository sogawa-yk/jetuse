"""TOOL-01 の検証用フィクスチャ(**架空**の業務 API の中身)。

顧客データ・顧客名は持ち込まない。ここにあるのは実在しない品番・倉庫だけ。
JetUse 側に業務ロジックは持たせない(渡す口だけを作る)ので、この API の中身は
使い捨ての静的 JSON で足りる。
"""

import json

PREFIX = "jetuse-spike-tool01"

# 架空の在庫 — モデルが学習で知りうる値ではない(だから「使わないと答えられない」)
PART_NUMBER = "JX-7742"
STOCK_QTY = 137
WAREHOUSE = "大阪第2倉庫"
LOT = "LOT-2026-0731"

STOCK_JSON = json.dumps(
    {
        "part_number": PART_NUMBER,
        "stock_qty": STOCK_QTY,
        "warehouse": WAREHOUSE,
        "lot": LOT,
        "as_of": "2026-08-01",
    },
    ensure_ascii=False,
).encode()

# サイズ上限の検証用(応答が MAX_RESPONSE_BYTES を超える架空 API)
BIG_OBJECT_BYTES = 300_000
# 圧縮爆弾の検証用(送られてくる量は小さいが、展開すると上限をはるかに超える)
BOMB_PLAIN_BYTES = 5_000_000

STOCK_TOOL = {
    "name": "lookup_inventory",
    "description": (
        "社内在庫システムに品番を問い合わせ、在庫数・保管倉庫・ロット番号を返す。"
        "品番ごとの在庫を聞かれたら必ずこれを使う(社外の情報源では答えられない)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "part_number": {"type": "string", "description": "品番(例 JX-7742)"}
        },
        "required": ["part_number"],
    },
    "method": "GET",
}

QUESTION = f"品番 {PART_NUMBER} の在庫数・保管倉庫・ロット番号を教えてください。"
