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
import oracledb
from fastapi import APIRouter, Body, Depends, HTTPException
from jetuse_shared.sqlguard import SqlRejectedError, assert_tables_allowed, sanitize_sql
from openai import APIError
from pydantic import BaseModel, Field, field_validator

from jetuse_core import audit, guardrails, materialize, moderation, nl2sql
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.models import MODELS
from jetuse_core.plugins import ai_runtime
from jetuse_core.plugins.sample_app import SampleAppDefinition, SampleAppError
from jetuse_core.plugins.sample_app_registry import (
    get_sample_app,
    list_sample_apps,
    resolve_app,
)
from jetuse_core.settings import get_settings

from ..schemas import ExecuteSqlRequest

logger = logging.getLogger("jetuse.service")
router = APIRouter()


class SlotInvokeRequest(BaseModel):
    """aiSlot 実行のリクエスト。`input` が処理対象本文(質問/問い合わせ等)。"""

    input: str = Field(min_length=1, max_length=ai_runtime.MAX_INPUT_CHARS)
    categories: list[str] | None = Field(default=None, max_length=ai_runtime.MAX_CATEGORIES)
    top_k: int | None = Field(default=None, ge=1, le=ai_runtime.MAX_TOP_K)
    model: str | None = Field(default=None, max_length=64)
    # chart capability(SBA-B)用: 実行結果の列名・行データを渡す(上限付き)。
    columns: list[str] | None = Field(default=None, max_length=ai_runtime.MAX_CHART_COLUMNS)
    rows: list[list[str]] | None = Field(default=None, max_length=ai_runtime.MAX_CHART_ROWS)

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

    @field_validator("columns", "rows")
    @classmethod
    def _bound_chart_payload(cls, v: list | None) -> list | None:
        """chart の列名/行(各セル)を行幅・セル長で制限する(行数は max_length が担う)。

        巨大セル・極端に横長の行で prompt とメモリを膨らませられないよう、入力段で 422 にする。
        """
        if v is None:
            return None
        for row in v:
            cells = row if isinstance(row, list) else [row]
            if len(cells) > ai_runtime.MAX_CHART_COLUMNS:
                raise ValueError(
                    f"1行のセル数は {ai_runtime.MAX_CHART_COLUMNS} 以内"
                )
            for cell in cells:
                if isinstance(cell, str) and len(cell) > ai_runtime.MAX_CHART_CELL_CHARS:
                    raise ValueError(
                        f"セルは {ai_runtime.MAX_CHART_CELL_CHARS} 文字以内"
                    )
        return v


def _resolve_full(app_id: str) -> dict[str, Any]:
    app = get_sample_app(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="sample-app not found")
    return app


def _runtime_for(
    app_id: str,
) -> tuple[SampleAppDefinition, list[dict[str, Any]], str | None]:
    """app_id から (検証済み定義, 知識コーパス, nl2sql 照会先スキーマ) を返す。

    解決はコア同梱レジストリ(resolve_app)に一本化する。SBA-A は FAQ コーパスを根拠にする
    (nl2sql なし)。SBA-B は datasets を文脈に NL2SQL を生成のみ(schema=None)。SBA-C は売上集計が
    専用スキーマ(JETUSE_SBA04)を実行照会する。
    """
    resolved = resolve_app(app_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="sample-app not found")
    return resolved.definition, resolved.corpus, resolved.nl2sql_schema


@router.get("/api/sample-apps")
async def list_sample_apps_route(user: Annotated[AuthContext, Depends(require_user)]):
    """コア同梱 sample-app の一覧を返す(home カード/実行導線用)。"""
    return {"sample_apps": list_sample_apps()}


@router.get("/api/sample-apps/{app_id}")
async def get_sample_app_route(
    app_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """sample-app の完全定義(screens/datasets/aiSlots と seed)を返す。

    各 aiSlot には、実行時フレームワークでハンドラが束縛されているか(`bound`)を付ける。
    """
    app = _resolve_full(app_id)
    bound = ai_runtime.bound_capabilities()
    # definition は配布表現のまま(再検証可能)に保ち、束縛状況は別フィールドで返す
    # (definition.aiSlots に余分なキーを足すと extra=forbid の再検証が壊れるため)。
    app["slot_bindings"] = {
        slot["key"]: slot.get("capability") in bound
        for slot in app["definition"].get("aiSlots", [])
    }
    # knowledge_dataset はレジストリの完全定義 dict が**アプリ単位の契約**として持つ:
    #   SBA-A = "faqs"(RAGコーパスあり) / SBA-B = None(NL2SQL・コーパス無しを明示) /
    #   SBA-C = キー自体を持たない(RAGコーパス概念が無い)。
    # 一律 setdefault は SBA-C の「キーを付けない」契約を壊すため、dict の値をそのまま返す。
    return app


@router.post("/api/sample-apps/{app_id}/slots/{slot_key}/invoke")
async def invoke_slot(
    app_id: str,
    slot_key: str,
    user: Annotated[AuthContext, Depends(require_user)],
    req: Annotated[SlotInvokeRequest, Body()],
):
    """aiSlot を実行時バインドして実行し、結果を JSON で返す。"""
    definition, corpus, nl2sql_schema = _runtime_for(app_id)
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
    # chart(SBA-B): 結果表の列・行を文脈として渡す(input は元の質問)。
    if req.columns is not None:
        payload["columns"] = req.columns
        payload["question"] = req.input
    if req.rows is not None:
        payload["rows"] = req.rows
    try:
        result = await asyncio.to_thread(
            ai_runtime.invoke_slot,
            definition,
            slot_key,
            payload,
            owner=user.subject,
            corpus=corpus,
            model_key=model_key,
            nl2sql_schema=nl2sql_schema,
        )
    except ai_runtime.UnboundCapabilityError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except ai_runtime.SlotInputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ai_runtime.SlotInferenceError as e:
        # LLM 空応答など推論結果が成立しない場合。成功偽装せず 502 に正規化する。
        logger.warning("sample-app slot inference empty: %s", str(e)[:200])
        raise HTTPException(status_code=502, detail="AI inference failed") from e
    except ai_runtime.SlotBackendUnavailableError as e:
        # DB 接続/可用性障害(一過性)。SampleAppError サブクラスなので 404 の前に捕捉する。
        logger.warning("sample-app slot backend unavailable: %s", str(e)[:200])
        raise HTTPException(status_code=503, detail="backend temporarily unavailable") from e
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


@router.post("/api/sample-apps/{app_id}/dbchat/execute")
async def sample_app_execute(
    app_id: str,
    req: ExecuteSqlRequest,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """sample-app(NL2SQL)の読取専用実行。SQL-02 ガード + 当該 sample-app のテーブル許可リストを
    強制してから execute_readonly に渡す。

    汎用 /api/dbchat/execute は対象スキーマ(SH/データセット)向けで sample-app のテーブル境界を
    知らない。UI で編集された SQL がこの境界を越えないよう、専用経路で sanitize_sql +
    assert_tables_allowed(datasets) を必ず適用する(SELECT 以外・別スキーマ・許可外テーブルを拒否)。

    この execute は NL2SQL 能力(`nl2sql` capability)を束縛した sample-app 専用。SBA-A など
    DB 照会を持たない sample-app では 404 とし、DB 照会経路の到達範囲を最小化する。さらに
    `DUAL` の暗黙許可を切り、業務テーブルを最低1つ参照しない SQL(スカラ/関数呼び出しのみ)も
    拒否して、dataset 境界外への露出を塞ぐ。
    """
    resolved = resolve_app(app_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="sample-app not found")
    if not any(s.capability == "nl2sql" for s in resolved.definition.ai_slots):
        # NL2SQL を持たない sample-app には DB 照会 execute を提供しない(到達範囲の最小化)。
        raise HTTPException(status_code=404, detail="sample-app has no nl2sql capability")
    allowed = {ds.name.upper() for ds in resolved.definition.datasets}
    try:
        cleaned = sanitize_sql(req.sql)
        assert_tables_allowed(cleaned, allowed, allow_dual=False, require_table=True)
    except SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # B1/BE-02: 専用 execute は CURRENT_SCHEMA を **その sample-app の照会スキーマ** へ固定し、
    # テーブル名が当該スキーマの物理表へ確定解決するようにする(synonym 依存・読取ユーザ側の同名
    # オブジェクトに左右されない)。アプリが専用 nl2sql_schema を宣言する場合(SBA-C / JETUSE_SBA04)は
    # それを尊重し、宣言しない自動マテリアライズ対象(SBA-B 等)は `materialize.target_schema()`
    # (= 展開先 adb_user)に固定。これで「展開先」と「読取解決先」を整合させ、起動だけで NL2SQL を
    # 成立させつつ、専用スキーマ運用アプリを ORA-00942 に回帰させない(F-003)。
    current_schema = resolved.nl2sql_schema or materialize.target_schema()
    try:
        result = await asyncio.to_thread(
            nl2sql.execute_readonly, cleaned, current_schema
        )
    except SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except oracledb.DatabaseError as e:
        msg = str(e).splitlines()[0][:300]
        if "DPY-" in msg:  # 接続系は DB 停止扱い(503 ハンドラへ)
            raise
        raise HTTPException(status_code=400, detail=f"SQL実行エラー: {msg}") from e
    log_with(logger, logging.INFO, "sample-app dbchat executed",
             user=user.subject, app=app_id, rows=result["row_count"])
    return result
