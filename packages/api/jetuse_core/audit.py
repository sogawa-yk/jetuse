"""監査ログ(SEC-02): 誰が・どの機能・どのモデル・トークン数をADBに記録。

ベストエフォート(失敗してもサービスを止めない)。集計はOPS-01のダッシュボードが使う。
"""

import logging
import uuid
from typing import Any

from .db import connect
from .obs import post_metric

logger = logging.getLogger("jetuse.audit")

FEATURES = (
    "chat", "agent", "rag", "nl2sql", "dbchat", "usecase", "minutes",
    "tts", "stt", "video", "voicechat", "moderation_block", "prompt_injection_block",
)


def log_event(
    owner: str,
    feature: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    status: str = "ok",
    meta: str | None = None,
) -> None:
    # OCI Monitoringカスタムメトリクス(OPS-02)。失敗してもADB記録は続行
    dims = {"feature": feature, "model": model or "-", "status": status}
    post_metric("calls", 1, dims)
    if input_tokens or output_tokens:
        post_metric("tokens", float((input_tokens or 0) + (output_tokens or 0)), dims)
    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO audit_log(id, owner_sub, feature, model,
                                      input_tokens, output_tokens, status, meta)
                VALUES (:id, :o, :f, :m, :ti, :to_, :s, :meta)
                """,
                id=str(uuid.uuid4()), o=owner[:255], f=feature[:40],
                m=(model or "")[:64] or None, ti=input_tokens, to_=output_tokens,
                s=status[:20], meta=(meta or "")[:1000] or None,
            )
            conn.commit()
    except Exception:
        logger.exception("audit log failed (ignored)")


def summarize(days: int = 30) -> dict[str, Any]:
    """管理ダッシュボード用の集計(OPS-01)"""
    with connect() as conn:
        cur = conn.cursor()
        out: dict[str, Any] = {"days": days}
        cur.execute(
            """
            SELECT feature, COUNT(*), NVL(SUM(input_tokens),0), NVL(SUM(output_tokens),0)
            FROM audit_log WHERE created_at > SYSTIMESTAMP - :d
            GROUP BY feature ORDER BY 2 DESC
            """, d=days,
        )
        out["by_feature"] = [
            {"feature": r[0], "calls": r[1], "input_tokens": r[2], "output_tokens": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT NVL(model,'-'), COUNT(*), NVL(SUM(input_tokens),0), NVL(SUM(output_tokens),0)
            FROM audit_log WHERE created_at > SYSTIMESTAMP - :d
            GROUP BY model ORDER BY 4 DESC
            """, d=days,
        )
        out["by_model"] = [
            {"model": r[0], "calls": r[1], "input_tokens": r[2], "output_tokens": r[3]}
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT owner_sub, COUNT(*), NVL(SUM(input_tokens),0) + NVL(SUM(output_tokens),0)
            FROM audit_log WHERE created_at > SYSTIMESTAMP - :d
            GROUP BY owner_sub ORDER BY 3 DESC FETCH FIRST 50 ROWS ONLY
            """, d=days,
        )
        out["by_user"] = [
            {"user": r[0], "calls": r[1], "total_tokens": r[2]} for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT TO_CHAR(TRUNC(created_at), 'YYYY-MM-DD'), COUNT(*),
                   NVL(SUM(input_tokens),0) + NVL(SUM(output_tokens),0)
            FROM audit_log WHERE created_at > SYSTIMESTAMP - :d
            GROUP BY TRUNC(created_at) ORDER BY 1
            """, d=days,
        )
        out["by_day"] = [
            {"day": r[0], "calls": r[1], "total_tokens": r[2]} for r in cur.fetchall()
        ]
        return out
