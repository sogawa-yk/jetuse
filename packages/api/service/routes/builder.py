"""ビルダー・ヒアリング / デモ設計 API(SP3-01・SP3-02 / specs/19 §2.4・§3.1)。

Internal 専用面(AUTH_REQUIRED=true 前提の配備で提供)。すべて require_user。
所有者強制は builder_sessions リポジトリの WHERE 句 — 0 行 = 404 の存在秘匿(demos と同形)。
messages は LLM 1 呼び出しの同期 JSON(SSE 化は SP3-05 の UX 要件が求めたら residual)。
"""

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from jetuse_core import (
    builder_design,
    builder_generate,
    builder_hearing,
    builder_sessions,
    demos,
)
from jetuse_core import conversations as conv_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.capabilities import demo_plan_vocabulary
from jetuse_core.gen_models import GEN_MODELS

from ..schemas import BuilderGenerateIn, BuilderMessageIn, BuilderPlanPatch
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
_BUSY_DETAIL = "生成中のデモが多すぎます。少し待って再試行してください(specs/19 §4.2 N3)"
_NO_PLAN_DETAIL = "デモプランがありません。先に POST /design を実行してください(specs/19 §4.5)"
_GENERATED_DETAIL = "このセッションは生成済みです。修正は新セッションで(specs/19 §4.5)"
_PROVISIONING_DETAIL = "このデモは生成中です(specs/19 §4.5 の再実行契約)"


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


@router.patch("/{sid}/plan")
async def patch_plan(sid: str, req: BuilderPlanPatch, user: User):
    """プランの title/description のみ直接編集(SP3-05 / specs/19 §7②)。

    反映後に §3.3 の validate_plan で再検証してから保存する。それ以外の修正は
    追加発話 → 再 design のループ(プラン JSON 自由編集の API は作らない — §11)。
    保存は save_plan の楽観ロック(demo_id IS NULL + transcript 長)— 競合は 409。
    """
    session = _get_or_404(user.subject, sid)
    if session["demo_id"] is not None:
        raise HTTPException(status_code=409, detail=_READONLY_DETAIL)
    if session["status"] != "designed" or not session["plan"]:
        raise HTTPException(status_code=409, detail=_NO_PLAN_DETAIL)
    updates = {k: v for k, v in (("title", req.title), ("description", req.description))
               if v is not None}
    if not updates:
        return _session_out(session)  # 空 PATCH = 現状(demos PATCH と同じ流儀)
    try:
        plan = builder_design.validate_plan(
            {**session["plan"], **updates}, demo_plan_vocabulary()
        )
    except builder_design.PlanValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    saved = await asyncio.to_thread(
        builder_sessions.save_plan, user.subject, sid, plan, len(session["transcript"])
    )
    if not saved:
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL)
    return _session_out(_get_or_404(user.subject, sid))


@router.post("/{sid}/generate", status_code=202)
async def generate(sid: str, background: BackgroundTasks, user: User,
                   req: BuilderGenerateIn | None = None):
    """designed セッション → Demo(provisioning)を作り生成を開始(specs/19 §4.5)。202 {demo_id}。

    生成本体(③b 生成→③c 検査→公開)は BackgroundTask で回す(N3≤2・§4.2)。状態は Demo.status。
    - demo_id 未設定: status='designed' + plan 必須(≠なら 409)。start(N3・作成・attach)→ run 予約。
    - demo_id 済 + failed: 再実行(restart → run 予約)。
    - demo_id 済 + provisioning / ready: 409(§4.5 の再実行契約)。deleting / 行なし: 404。
    - body は任意 {"model": <生成レジストリ key>}(SP3-06 / §4.5)。未知キーは副作用前に 422。
      省略 = 設定既定。使用モデルは N6(config.frontend.generator.model)に記録される。
    """
    model_key = req.model if req else None
    if model_key is not None and model_key not in GEN_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"未知の生成モデルです。選択可能: {', '.join(GEN_MODELS)}")
    session = _get_or_404(user.subject, sid)
    demo_id = session["demo_id"]

    if demo_id is not None:
        demo = await asyncio.to_thread(demos.get_demo, demo_id)
        status = demo["status"] if demo else None
        if status is None or status == "deleting":
            raise HTTPException(status_code=404, detail="demo not found")
        if status == "provisioning":
            raise HTTPException(status_code=409, detail=_PROVISIONING_DETAIL)  # §4.5
        if status == "ready":
            raise HTTPException(status_code=409, detail=_GENERATED_DETAIL)
        demo_id = await _restart_or_raise(demo_id)   # failed → 再実行
    else:
        if session["status"] != "designed" or not session["plan"]:
            raise HTTPException(status_code=409, detail=_NO_PLAN_DETAIL)
        demo_id = await _start_or_raise(user.subject, session)

    background.add_task(builder_generate.run, demo_id, model_key)
    return {"demo_id": demo_id}


async def _start_or_raise(owner: str, session: dict) -> str:
    try:
        return await asyncio.to_thread(builder_generate.start, owner, session)
    except builder_generate.GenerationBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e  # N3 超過 = 409(§4.2)
    except builder_generate.GenerationConflictError as e:
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL) from e


async def _restart_or_raise(demo_id: str) -> str:
    try:
        return await asyncio.to_thread(builder_generate.restart, demo_id)
    except builder_generate.GenerationBusyError as e:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL) from e  # N3 超過 = 409(§4.2)
    except builder_generate.GenerationConflictError as e:
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL) from e
