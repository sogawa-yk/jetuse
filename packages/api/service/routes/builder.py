"""ビルダー・ヒアリング API(SP3-01 / specs/19 §2.4)。

Internal 専用面(AUTH_REQUIRED=true 前提の配備で提供)。すべて require_user。
所有者強制は builder_sessions リポジトリの WHERE 句 — 0 行 = 404 の存在秘匿(demos と同形)。
messages は LLM 1 呼び出しの同期 JSON(SSE 化は SP3-05 の UX 要件が求めたら residual)。
"""

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import builder_hearing, builder_sessions
from jetuse_core import conversations as conv_repo
from jetuse_core.auth import AuthContext, require_user

from ..schemas import BuilderMessageIn

logger = logging.getLogger("jetuse.builder")
router = APIRouter(prefix="/api/builder/sessions")
User = Annotated[AuthContext, Depends(require_user)]

# 信頼境界の入力上限(specs/19 §2.1)。LLM 入力の有界化を兼ねる(呼び出し前に遮断)
MAX_ROUND_TRIPS = 50
MAX_TRANSCRIPT_BYTES = 256 * 1024
_LIMIT_DETAIL = ("このセッションは上限(50 往復 / 256KB)に達しました。"
                 "新しいセッションを開始してください")
_READONLY_DETAIL = "生成開始後のセッションは読み取り専用です(specs/19 §2.1)"
_CONFLICT_DETAIL = ("セッションが並行して更新されました(別リクエストの発話 or 生成開始)。"
                    "セッションを再取得してやり直してください")


def _transcript_bytes(transcript: list[dict]) -> int:
    return len(json.dumps(transcript, ensure_ascii=False).encode())


def _get_or_404(owner: str, sid: str) -> dict:
    s = builder_sessions.get_session(owner, sid)
    if not s:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@router.post("")
def create_session(user: User):
    """Body なし。status='hearing'・transcript=[] で INSERT(specs/19 §2.4)。"""
    return builder_sessions.create_session(user.subject)


@router.get("/{sid}")
def get_session(sid: str, user: User):
    return _get_or_404(user.subject, sid)


@router.post("/{sid}/messages")
async def post_message(sid: str, req: BuilderMessageIn, user: User):
    """NL 発話 → LLM 構造化出力(temperature 0・同期 JSON) → 決定的再検査 → 永続化。"""
    session = _get_or_404(user.subject, sid)
    if session["demo_id"] is not None:
        raise HTTPException(status_code=409, detail=_READONLY_DETAIL)
    transcript = session["transcript"]
    with_user = [*transcript, {"role": "user", "content": req.content}]
    if (
        sum(1 for m in transcript if m.get("role") == "user") >= MAX_ROUND_TRIPS
        or _transcript_bytes(with_user) >= MAX_TRANSCRIPT_BYTES
    ):
        raise HTTPException(status_code=422, detail=_LIMIT_DETAIL)

    try:
        turn, usage = await asyncio.to_thread(
            builder_hearing.run_hearing_turn, with_user
        )
    except builder_hearing.HearingError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # assistant 応答込みの最終 transcript も 256KB 境界を破らない(保存前の再検査 —
    # codex review-1 M003。超過は保存せず 422 = セッション上限)
    final_transcript = [*with_user, {"role": "assistant", "content": turn.reply}]
    if _transcript_bytes(final_transcript) >= MAX_TRANSCRIPT_BYTES:
        raise HTTPException(status_code=422, detail=_LIMIT_DETAIL)

    requirements = turn.requirements.model_dump()
    # 楽観ロック: 読み取り時の transcript 件数を UPDATE の WHERE に含める(review-1 M002)。
    # 並行 messages / 生成開始が割り込んだら 0 行 → 409(後勝ち消失を構造的に防ぐ)
    saved = await asyncio.to_thread(
        builder_sessions.save_hearing_turn, user.subject, sid,
        final_transcript, requirements, len(transcript),
    )
    if not saved:
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL)

    # LLM 使用は実ユーザー(owner)に紐づけて記録(specs/19 §8.3)。ベストエフォート
    try:
        conv_repo.log_usage(
            user.subject, None, builder_hearing.HEARING_MODEL,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )
    except Exception:
        logger.exception("builder hearing usage log failed")

    return {
        "reply": turn.reply,
        "requirements": requirements,
        "sufficient": turn.sufficient,
        "missing": turn.missing,
    }
