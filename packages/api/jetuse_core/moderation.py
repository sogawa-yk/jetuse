"""入力モデレーション(SEC-02)。

OCI実機調査(2026-06-13)の結果:
- 互換APIに /moderations は無い(Path doesn't map)
- cohere(safety_mode持ち)はchat completions自体が「Unsupported OpenAI operation」
→ 高速モデル(llama-3.3-70b、TTFT 0.07s)による自己判定ガードを採用。
  MODERATION_ENABLED=true で有効(既定false。devは無効)。
"""

import json
import logging

from .chat import complete_once

logger = logging.getLogger("jetuse.moderation")

CATEGORIES = (
    "violence(暴力) / sexual(性的) / hate(差別) / illegal(犯罪助長) / pii(個人情報の不正取得)"
)

_PROMPT = (
    "あなたはコンテンツモデレーターです。次のユーザー入力が業務チャットとして"
    f"不適切か判定してください。不適切カテゴリ: {CATEGORIES}。\n"
    '出力はJSONのみ: {{"flag": true/false, "category": "カテゴリ名 or none"}}\n'
    "ユーザー入力:\n{c}"
)


def check_input(text: str) -> tuple[bool, str]:
    """(flagged, category)を返す。判定失敗時は通す(可用性優先・ログのみ)"""
    try:
        raw = complete_once("llama-3.3-70b", [
            {"role": "user", "content": _PROMPT.format(c=text[:4000])}
        ], max_chars=200)
        m = raw[raw.find("{"): raw.rfind("}") + 1]
        d = json.loads(m)
        return bool(d.get("flag")), str(d.get("category") or "none")
    except Exception:
        logger.exception("moderation check failed (pass-through)")
        return False, "error"
