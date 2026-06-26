"""コア同梱 sample-app(SBA-A)とその AI 組込スロット実行ルート(SBA-02)。

`/api/sample-apps`                       一覧(home/実行導線が表示)
`/api/sample-apps/{app_id}`              定義(screens/datasets/aiSlots + seed)
`/api/sample-apps/{app_id}/slots/{key}/invoke`  aiSlot を実行時バインドして実行(JSON 応答)

スロット実行は `ai_runtime.invoke_slot` に委譲する。知識コーパス(FAQ シード)は本ルートが
sample-app 定義から取り出してハンドラへ渡す——これが「業務アプリのデータに AI を組み込む」型。
RAG/分類/要約/返信ドラフトはいずれも非ストリーミングの単発処理のため、ここでは JSON で返す
(チャットの逐次生成とは別系統)。
"""

import asyncio
import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from openai import APIError
from pydantic import BaseModel, Field, field_validator

from jetuse_core import audit, guardrails, moderation
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.models import MODELS
from jetuse_core.plugins import ai_runtime
from jetuse_core.plugins.sample_app import SampleAppDefinition, SampleAppError
from jetuse_core.plugins.sample_app_builtin import (
    SBA_A_KNOWLEDGE_DATASET,
    builtin_sample_apps,
    get_builtin_sample_app,
    knowledge_corpus,
    sba_a_definition,
)
from jetuse_core.settings import get_settings

logger = logging.getLogger("jetuse.service")
router = APIRouter()


class SlotInvokeRequest(BaseModel):
    """aiSlot 実行のリクエスト。`input` が処理対象本文(質問/問い合わせ等)。"""

    input: str = Field(min_length=1, max_length=ai_runtime.MAX_INPUT_CHARS)
    categories: list[str] | None = Field(default=None, max_length=ai_runtime.MAX_CATEGORIES)
    top_k: int | None = Field(default=None, ge=1, le=ai_runtime.MAX_TOP_K)
    model: str | None = Field(default=None, max_length=64)

    @field_validator("input")
    @classmethod
    def _input_not_blank(cls, v: str) -> str:
        """空白のみの input はモデル検証で 422 にする(後段の外部ガードを無効入力で起動しない)。"""
        if not v.strip():
            raise ValueError("input は空白のみにできない")
        return v

    @field_validator("categories")
    @classmethod
    def _clean_categories(cls, v: list[str] | None) -> list[str] | None:
        """空白のみを除外し、1ラベルの長さ上限を検証する(件数上限は max_length が担う)。"""
        if v is None:
            return None
        cleaned = [c.strip() for c in v if c and c.strip()]
        for c in cleaned:
            if len(c) > ai_runtime.MAX_CATEGORY_LABEL:
                raise ValueError(
                    f"category ラベルは {ai_runtime.MAX_CATEGORY_LABEL} 文字以内"
                )
        return cleaned or None


def _resolve_app(app_id: str) -> dict[str, Any]:
    app = get_builtin_sample_app(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="sample-app not found")
    return app


def _definition_and_corpus(app_id: str) -> tuple[SampleAppDefinition, list[dict[str, Any]]]:
    """app_id から検証済み定義と知識コーパス(FAQ シード)を返す。"""
    # 現状コア同梱は SBA-A のみ。将来 scaffold 済みインスタンス対応時はここで分岐する。
    _resolve_app(app_id)
    definition = sba_a_definition()
    corpus = knowledge_corpus(definition)
    return definition, corpus


@router.get("/api/sample-apps")
async def list_sample_apps(user: Annotated[AuthContext, Depends(require_user)]):
    """コア同梱 sample-app の一覧を返す(home カード/実行導線用)。"""
    return {"sample_apps": builtin_sample_apps()}


@router.get("/api/sample-apps/{app_id}")
async def get_sample_app(
    app_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """sample-app の完全定義(screens/datasets/aiSlots と seed)を返す。

    各 aiSlot には、実行時フレームワークでハンドラが束縛されているか(`bound`)を付ける。
    """
    app = _resolve_app(app_id)
    bound = ai_runtime.bound_capabilities()
    # definition は配布表現のまま(再検証可能)に保ち、束縛状況は別フィールドで返す
    # (definition.aiSlots に余分なキーを足すと extra=forbid の再検証が壊れるため)。
    app["slot_bindings"] = {
        slot["key"]: slot.get("capability") in bound
        for slot in app["definition"].get("aiSlots", [])
    }
    app["knowledge_dataset"] = SBA_A_KNOWLEDGE_DATASET
    return app


@router.post("/api/sample-apps/{app_id}/slots/{slot_key}/invoke")
async def invoke_slot(
    app_id: str,
    slot_key: str,
    user: Annotated[AuthContext, Depends(require_user)],
    req: Annotated[SlotInvokeRequest, Body()],
):
    """aiSlot を実行時バインドして実行し、結果を JSON で返す。"""
    definition, corpus = _definition_and_corpus(app_id)
    settings = get_settings()
    # Web UI は model を送らない → 既定実行経路。project_ocid 不要なモデルを既定にして、
    # 追加設定なしでデモが動くようにする(settings.sample_app_model)。
    model_key = req.model or settings.sample_app_model
    if model_key not in MODELS:
        raise HTTPException(status_code=422, detail="unknown model")

    # slot の存在・束縛をガード(外部呼び出し)より前に正規化する。未知 slot は 404、
    # 未束縛 capability は 501 にして、guards ON 環境でも無効 slot で外部ガードを起動しない。
    try:
        ai_runtime.bind_slot(definition, slot_key)
    except ai_runtime.UnboundCapabilityError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except SampleAppError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # 入力ガード(SEC-02 モデレーション / GAP-01 プロンプトインジェクション検知)。
    # 既定 OFF のフラグで有効化され、chat/usecase と同じガード経路をこの AI 実行面にも通す。
    # ブロックは監査記録のうえ 400 を返す(フラグ OFF の既定デモ動作は不変)。
    # input だけでなく categories も利用者入力で classify プロンプトに挿入される。ガードは
    # 内部で text を切り詰める(moderation 4000 / guardrails 8000 字)ため、連結すると長い input が
    # categories を判定窓の外へ押し出す。各ユーザー入力片を**個別に**判定して取りこぼしを防ぐ。
    guard_texts = [req.input]
    if req.categories:
        guard_texts.append("\n".join(req.categories))
    if settings.moderation_enabled:
        for text in guard_texts:
            flagged, category = await asyncio.to_thread(moderation.check_input, text)
            if flagged:
                await asyncio.to_thread(
                    audit.log_event, user.subject, "sample_app_moderation_block",
                    model=model_key, status="blocked", meta=category,
                )
                raise HTTPException(
                    status_code=400, detail="入力内容が利用ポリシーに抵触するため処理できません"
                )
    if settings.prompt_injection_guard_enabled:
        for text in guard_texts:
            pi_flagged, pi_score = await asyncio.to_thread(
                guardrails.check_prompt_injection, text
            )
            if pi_flagged:
                await asyncio.to_thread(
                    audit.log_event, user.subject, "sample_app_prompt_injection_block",
                    model=model_key, status="blocked", meta=f"score={pi_score}",
                )
                raise HTTPException(
                    status_code=400,
                    detail="プロンプトインジェクションの可能性があるため処理を中断しました",
                )

    payload: dict[str, Any] = {"input": req.input}
    if req.categories is not None:
        payload["categories"] = req.categories
    if req.top_k is not None:
        payload["top_k"] = req.top_k
    try:
        result = await asyncio.to_thread(
            ai_runtime.invoke_slot,
            definition,
            slot_key,
            payload,
            owner=user.subject,
            corpus=corpus,
            model_key=model_key,
        )
    except ai_runtime.UnboundCapabilityError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except ai_runtime.SlotInputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ai_runtime.SlotInferenceError as e:
        # LLM 空応答など推論結果が成立しない場合。成功偽装せず 502 に正規化する。
        logger.warning("sample-app slot inference empty: %s", str(e)[:200])
        raise HTTPException(status_code=502, detail="AI inference failed") from e
    except SampleAppError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (APIError, httpx.HTTPError) as e:
        # 外部推論サービス(OCI GenAI)のタイムアウト・429・接続/一時障害は通常運用で起きる。
        # openai の APIError は接続/タイムアウト/ステータス系(APIConnectionError/APITimeoutError/
        # APIStatusError)を包含し、httpx.HTTPError は transport 層を覆う。これらに限定して 502 に
        # 正規化する。ローカル実装バグ(TypeError 等)は握りつぶさず 500 のまま表面化させる。
        # (DB 障害は global handler が 503)
        # メッセージ空の例外(httpx.ReadError() 等)でも IndexError にならないようガードする。
        first_line = (str(e).splitlines() or [""])[0]
        logger.warning("sample-app slot inference failed: %s", first_line[:200] or type(e).__name__)
        raise HTTPException(status_code=502, detail="AI inference failed") from e
    # 監査記録(SEC-02/OPS-02)。chat/usecase と同様に成功実行も記録する(fail-soft)。
    await asyncio.to_thread(
        audit.log_event, user.subject, "sample_app_slot",
        model=model_key, status="ok", meta=slot_key,
    )
    return result
