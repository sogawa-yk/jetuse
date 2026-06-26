"""ヒアリングフロー API(HBD-01)。セッション CRUD ＋ 回答保存 ＋ 決定的推薦。

`GET  /api/hearing/questions`                     質問スキーマ(Q1..Q6＋Auto)
`POST /api/hearing/sessions`                      セッション作成(任意 input_notes)
`GET  /api/hearing/sessions`                      自分のセッション一覧
`GET  /api/hearing/sessions/{sid}`                セッション(回答＋推薦含む)
`PATCH/DELETE /api/hearing/sessions/{sid}`        更新 / 削除
`PUT  /api/hearing/sessions/{sid}/answers/{qid}`  回答保存(upsert, 手入力は常に source='sa')
`POST /api/hearing/sessions/{sid}/recommend`      回答→推薦を決定ルールで生成・保存
`POST /api/hearing/sessions/{sid}/recommend/confirm`  SA が推薦を確定

推薦は決定ルール(`recommend.recommend`)のみで成立する(GenAI 補助は §6 の境界で別途)。
所有権は repo(SQL)で強制し、他人のセッションへの操作は 404 にする。
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from jetuse_core import hearing as hearing_repo
from jetuse_core import hearing_genai
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.governance import validate_governance
from jetuse_core.hearing_schema import (
    MAX_INPUT_NOTES_CHARS,
    HearingSchemaError,
    question_schema,
)
from jetuse_core.recommend import Recommendation, recommend
from jetuse_core.synth import synthesize

router = APIRouter()


def _recommendation_from_detail(detail: dict[str, Any]) -> Recommendation:
    """保存済み推薦の detail(`rec.model_dump()` ＋付随キー)から Recommendation を復元する。

    `confirmed_at`(保存メタ)や `genai_nearest_sample_app`(/recommend が添える助言)は
    Recommendation のフィールドではないので取り除いてから検証する(extra=forbid)。
    """
    payload = {
        k: v
        for k, v in detail.items()
        if k not in ("confirmed_at", "genai_nearest_sample_app")
    }
    return Recommendation.model_validate(payload)


class SessionCreate(BaseModel):
    input_notes: str | None = Field(default=None, max_length=MAX_INPUT_NOTES_CHARS)


class SessionUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=32)
    # input_notes: 未指定(None)=据え置き / 空文字""=クリアの明示。null での消去は扱わない(F-003)。
    input_notes: str | None = Field(default=None, max_length=MAX_INPUT_NOTES_CHARS)


class AnswerSave(BaseModel):
    # value は質問型に依存する(single=str / multi=list[str])。検証は repo が質問スキーマで行う。
    # source は受け取らない: 公開の手入力保存は常に source='sa' に固定する(監査区分を守る)。
    # 'genai_suggested' は /suggest 内部の保存経路だけが付与する(F-001)。
    value: Any


class SuggestRequest(BaseModel):
    # 省略時はセッションの input_notes を使う。明示指定でその場メモから提案も可能。
    notes: str | None = Field(default=None, max_length=MAX_INPUT_NOTES_CHARS)
    model: str | None = Field(default=None, max_length=64)
    # True なら提案を回答として保存(source=genai_suggested)。False なら提案を返すだけ。
    save: bool = True


@router.get("/api/hearing/questions")
async def get_questions(user: Annotated[AuthContext, Depends(require_user)]):
    return question_schema()


@router.post("/api/hearing/sessions")
async def create_session(
    user: Annotated[AuthContext, Depends(require_user)],
    req: Annotated[SessionCreate, Body(default_factory=SessionCreate)],
):
    return hearing_repo.create_session(user.subject, req.input_notes)


@router.get("/api/hearing/sessions")
async def list_sessions(user: Annotated[AuthContext, Depends(require_user)]):
    return {"sessions": hearing_repo.list_sessions(user.subject)}


@router.get("/api/hearing/sessions/{sid}")
async def get_session(sid: str, user: Annotated[AuthContext, Depends(require_user)]):
    session = hearing_repo.get_session(user.subject, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    return session


@router.patch("/api/hearing/sessions/{sid}")
async def update_session(
    sid: str,
    req: SessionUpdate,
    user: Annotated[AuthContext, Depends(require_user)],
):
    try:
        session = hearing_repo.update_session(
            user.subject, sid, status=req.status, input_notes=req.input_notes
        )
    except HearingSchemaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if session is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    return session


@router.delete("/api/hearing/sessions/{sid}")
async def delete_session(sid: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not hearing_repo.delete_session(user.subject, sid):
        raise HTTPException(status_code=404, detail="hearing session not found")
    return {"deleted": True}


@router.put("/api/hearing/sessions/{sid}/answers/{qid}")
async def save_answer(
    sid: str,
    qid: str,
    req: AnswerSave,
    user: Annotated[AuthContext, Depends(require_user)],
):
    try:
        # 手入力保存は常に source='sa'(repo 既定)。クライアントは source を指定できない。
        saved = hearing_repo.save_answer(user.subject, sid, qid, req.value)
    except HearingSchemaError as e:
        # 未知の質問/選択肢・型不一致は入力エラー(422)。
        raise HTTPException(status_code=422, detail=str(e)) from e
    if saved is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    return saved


@router.post("/api/hearing/sessions/{sid}/suggest")
async def suggest_answers(
    sid: str,
    req: Annotated[SuggestRequest, Body(default_factory=SuggestRequest)],
    user: Annotated[AuthContext, Depends(require_user)],
):
    """ヒアリングメモから各質問のデフォルト回答を GenAI で提案する(§6 ①)。

    `save=True`(既定)なら提案を `source=genai_suggested` で保存する(SA は後で上書き可能)。
    GenAI 不在/失敗でも 200 で空の提案を返す(決定ルールでの推薦は別途成立=フォールバック)。
    """
    session = hearing_repo.get_session(user.subject, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    notes = req.notes if req.notes is not None else (session.get("input_notes") or "")
    model_key = hearing_genai._resolve_model(req.model)
    suggestions = await asyncio.to_thread(
        hearing_genai.suggest_answers_from_notes, notes, model_key=model_key
    )
    # SA が手入力(source='sa')した質問だけは GenAI 提案で上書きしない(手入力を尊重)。
    # 過去の genai_suggested は再提案で更新してよい(既答すべてを一律スキップしない)。
    existing = {a["question_id"] for a in session.get("answers", []) if a.get("source") == "sa"}
    saved: list[str] = []
    skipped: list[str] = []
    if req.save:
        for qid, value in suggestions.items():
            if qid in existing:
                skipped.append(qid)
                continue
            if hearing_repo.save_answer(
                user.subject, sid, qid, value, source="genai_suggested"
            ) is not None:
                saved.append(qid)
    return {
        "suggestions": suggestions,
        "saved": saved,
        "skipped_existing": skipped,
        "genai": "ok" if suggestions else "no_suggestions",
    }


@router.post("/api/hearing/sessions/{sid}/recommend")
async def recommend_session(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """保存済み回答から決定ルールで推薦構成を生成し、保存して返す。

    回答が揃わない/不正なら 422。GenAI 非依存(Q1=other は needs_genai_nearest を立てる)。
    """
    answers = hearing_repo.get_answers(user.subject, sid)
    if answers is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    try:
        rec = recommend(answers)
    except HearingSchemaError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    saved = hearing_repo.save_recommendation(user.subject, sid, rec)
    if saved is None:  # 直前に削除された等のレース
        raise HTTPException(status_code=404, detail="hearing session not found")
    # ② Q1=other で主 SBA が決定ルールで未定のとき、メモから最近傍 SBA を**助言**として添える
    #    (決定ルールの sample_app=None は保持。GenAI 失敗時は None で素通り=フォールバック)。
    if rec.needs_genai_nearest:
        notes = (hearing_repo.get_session(user.subject, sid) or {}).get("input_notes") or ""
        model_key = hearing_genai._resolve_model(None)
        nearest = await asyncio.to_thread(
            hearing_genai.nearest_sample_app, notes, model_key=model_key
        )
        saved = {**saved, "genai_nearest_sample_app": nearest}
    return saved


@router.post("/api/hearing/sessions/{sid}/recommend/confirm")
async def confirm_recommendation(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """SA が提示された推薦を確定する(ブラックボックス化しない: 画面提示→確定の明示)。

    主SBAが未確定(Q1=other で最近傍未反映)の推薦は 409 で確定を拒否する。
    """
    result = hearing_repo.confirm_recommendation(user.subject, sid)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="recommendation not found")
    if result == "unresolved":
        raise HTTPException(
            status_code=409,
            detail="主SBAが未確定です(Q1=その他)。最近傍を反映して再推薦してから確定してください",
        )
    return {"confirmed": True}


@router.post("/api/hearing/sessions/{sid}/preview")
async def preview_composition(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """保存済み推薦から**デモ構成を合成**し、プレビュー定義を返す(HBD-03)。

    実行はしない(宣言定義のレンダリング)。AI 部品は ai_runtime の束縛レジストリから束縛し、
    未束縛/組込点なしは active から外して理由を残す。主SBA を解決できない推薦は ok=False の
    構成を 200 で返す(プレビューで「合成不能」を安全に描画。HBD-04 の前段に渡せる形)。
    """
    session = hearing_repo.get_session(user.subject, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    detail = session.get("recommendation")
    if not detail:
        raise HTTPException(
            status_code=409, detail="推薦がまだありません。先に /recommend を実行してください"
        )
    rec = _recommendation_from_detail(detail)
    composition = synthesize(rec)
    return composition.model_dump()


@router.post("/api/hearing/sessions/{sid}/validate")
async def validate_composition_gate(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """保存済み推薦を合成し、**デプロイ前ゲート**としてガバナンス4制約で検証する(HBD-04)。

    許可組合せ(sample-app × AI部品 × connector)・必要ケイパビリティ束縛・権限スコープ・
    モデル可用性を判定し、違反は機械可読(種別・該当要素・代替提案つき)で返す。外れた構成は
    `ok=False` で弾く(外させない: 各違反に代替提案を添える)。実行はしない(静的検証)。
    """
    session = hearing_repo.get_session(user.subject, sid)
    if session is None:
        raise HTTPException(status_code=404, detail="hearing session not found")
    detail = session.get("recommendation")
    if not detail:
        raise HTTPException(
            status_code=409, detail="推薦がまだありません。先に /recommend を実行してください"
        )
    rec = _recommendation_from_detail(detail)
    composition = synthesize(rec)
    report = validate_governance(composition)
    return {
        "composition": composition.model_dump(),
        "governance": report.model_dump(),
    }
