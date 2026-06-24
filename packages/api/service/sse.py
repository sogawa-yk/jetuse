"""SSE フレーミング・キープアライブのユーティリティ(P1c §2)。

service/main.py の各ストリーミングハンドラから分離。生成する文字列は分離前と
バイト等価(`data: ...\\n\\n` / `data: [DONE]\\n\\n`)を厳守する。
"""

import json
from typing import Any

# SSEレスポンスヘッダ(GW/中継のバッファリング抑止)
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
SSE_MEDIA_TYPE = "text/event-stream"

# keepalive: コメント行はGW/中継でのバッファリング・特別扱いの疑いがあるため
# dataフレームで送る(クライアントは未知キーのイベントを無視する — 2026-06-11実験)
KEEPALIVE_FRAME = 'data: {"ka": 1}\n\n'
KEEPALIVE_SECONDS = 15  # ADR-0003: gemini-2.5-proのTTFT 10秒超対策

DONE_FRAME = "data: [DONE]\n\n"


def sse_event(payload: Any) -> str:
    """1イベントをSSEフレームへ。`json.dumps(..., ensure_ascii=False)` を踏襲。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
