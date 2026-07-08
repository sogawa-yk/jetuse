"""ビルダー・ヒアリング / デモ設計 API(SP3-01・SP3-02 / specs/19 §2.4・§3.1)。

Internal 専用面(AUTH_REQUIRED=true 前提の配備で提供)。すべて require_user。
所有者強制は builder_sessions リポジトリの WHERE 句 — 0 行 = 404 の存在秘匿(demos と同形)。
messages は LLM 1 呼び出しの同期 JSON(SSE 化は SP3-05 の UX 要件が求めたら residual)。
"""

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from jetuse_core import builder_design, builder_hearing, builder_sessions
from jetuse_core import conversations as conv_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.capabilities import demo_plan_vocabulary

from ..schemas import BuilderMessageIn
from .capabilities import build_catalog

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
_INSUFFICIENT_DETAIL = ("要求サマリが設計に足りません(missing: {missing})。"
                        "ヒアリングで必須項目を埋めてください(specs/19 §3.1)")


def _transcript_bytes(transcript: list[dict]) -> int:
    return len(json.dumps(transcript, ensure_ascii=False).encode())


def _get_or_404(owner: str, sid: str) -> dict:
    s = builder_sessions.get_session(owner, sid)
    if not s:
        raise HTTPException(status_code=404, detail="session not found")
    return s


def _session_out(s: dict) -> dict:
    """SessionOut(specs/19 §2.4)の形に落とす。sufficient は内部判定列で応答に出さない。"""
    return {k: v for k, v in s.items() if k != "sufficient"}


def _log_llm_usage(subject: str, model: str, usage: dict) -> None:
    """LLM 使用を owner に紐づけて記録(specs/19 §8.3)。ベストエフォート。"""
    try:
        conv_repo.log_usage(
            subject, None, model,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )
    except Exception:
        logger.exception("builder usage log failed")


@router.post("")
def create_session(user: User):
    """Body なし。status='hearing'・transcript=[] で INSERT(specs/19 §2.4)。"""
    return _session_out(builder_sessions.create_session(user.subject))


@router.get("/{sid}")
def get_session(sid: str, user: User):
    return _session_out(_get_or_404(user.subject, sid))


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
    # 並行 messages / 生成開始が割り込んだら 0 行 → 409(後勝ち消失を構造的に防ぐ)。
    # turn.sufficient は決定的再検査後の最終判定 — design ゲートが参照する永続値(F002)
    saved = await asyncio.to_thread(
        builder_sessions.save_hearing_turn, user.subject, sid,
        final_transcript, requirements, turn.sufficient, len(transcript),
    )
    if not saved:
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL)

    _log_llm_usage(user.subject, builder_hearing.HEARING_MODEL, usage)

    return {
        "reply": turn.reply,
        "requirements": requirements,
        "sufficient": turn.sufficient,
        "missing": turn.missing,
    }


@router.post("/{sid}/design")
async def design(sid: str, request: Request, user: User):
    """要求サマリ + 能力カタログ(語彙フィルタ済み) → 検証済みデモプラン(specs/19 §3.1)。

    sufficient 前提条件(≠なら 409)は二段で判定する(SP3-01 residual M001 の確定):
    (1) 永続化済み requirements への決定的再検査(§2.3「サーバ側の決定的再検査を最終判定と
    する」— 必須欠落での design を構造的に禁止)、かつ (2) 直近ヒアリングの最終 sufficient
    判定の永続値(§2.3「必須が埋まっていても LLM が false なら LLM に従う」を design ゲート
    でも効かせる — review-1 F002)。designed 後の再実行はプラン上書き(demo_id が付くまで)。
    """
    session = _get_or_404(user.subject, sid)
    if session["demo_id"] is not None:
        raise HTTPException(status_code=409, detail=_READONLY_DETAIL)
    req = builder_hearing.Requirements.model_validate(session["requirements"] or {})
    missing = builder_hearing.missing_required(req)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=_INSUFFICIENT_DETAIL.format(missing=", ".join(missing)),
        )
    if not session["sufficient"]:
        raise HTTPException(
            status_code=409,
            detail=("直近のヒアリング判定が sufficient=false です(追加確認が残っています)。"
                    "ヒアリングを続けてください(specs/19 §2.3・§3.1)"),
        )
    expected_len = len(session["transcript"])  # save_plan の楽観ロック基準(F003)

    # 語彙とカタログはリクエスト時にカタログ登録簿から導出(§3.4 — ハードコードしない)
    vocabulary = demo_plan_vocabulary()
    catalog = [c for c in build_catalog(request.app.openapi().get("paths", {}))
               if c["capability"] in vocabulary]
    try:
        plan, usage = await asyncio.to_thread(
            builder_design.run_design, session["requirements"], catalog, vocabulary
        )
    except builder_design.DesignError as e:
        # 消費したトークンはエラー経路でも記録する(SP3-01 review-2 M002 の轍を踏まない)
        _log_llm_usage(user.subject, builder_design.DESIGN_MODEL, e.usage)
        raise HTTPException(
            status_code=422,
            detail=(f"デモプランが検証に合格しませんでした"
                    f"(再生成 {builder_design.MAX_REGENERATIONS} 回実施)。{e}"),
        ) from e
    except builder_design.DesignUpstreamError as e:
        _log_llm_usage(user.subject, builder_design.DESIGN_MODEL, e.usage)  # F004
        raise HTTPException(
            status_code=502, detail="デモ設計の LLM 呼び出しに失敗しました"
        ) from e
    _log_llm_usage(user.subject, builder_design.DESIGN_MODEL, usage)

    saved = await asyncio.to_thread(
        builder_sessions.save_plan, user.subject, sid, plan, expected_len
    )
    if not saved:
        # 設計中に生成開始(demo_id)or 並行 messages(transcript 前進)が割り込んだ(F003)
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL)
    return _session_out(_get_or_404(user.subject, sid))
