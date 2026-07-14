"""NL2SQL・DBチャート・データセット(CSV)ルート(SQL-02/03, ENH-01/02)。"""

import asyncio
import json
import logging
from typing import Annotated

import oracledb
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from jetuse_core import audit, datasets, nl2sql
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.settings import get_settings

from ..schemas import (
    ChartSuggestRequest,
    ExecuteSqlRequest,
    GenerateDatasetRequest,
    Nl2SqlRequest,
    SeedDatasetsRequest,
)
from ..sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")
router = APIRouter()


# --- NL2SQL(SQL-02): 生成はSSE(実測30秒前後でkeepalive必須) ---

@router.post("/api/chat/nl2sql")
async def nl2sql_generate(
    req: Nl2SqlRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    if req.target == "sample":
        # PORT-02: SHが読めないADBでの ORA-00942 / サイレント空表示を、生成前に検出して防ぐ。
        # 検査自体がDB未接続等で失敗しても、SSE契約を破る生500にせずSSEエラーへ正規化する
        # (レビュー指摘F-003)。
        try:
            sample = await asyncio.to_thread(nl2sql.sh_sample_status)
        except Exception as e:  # noqa: BLE001
            logger.exception("sh_sample_status precheck failed")
            sample = {"available": False, "reason": f"SHサンプル検査に失敗しました: {e}"[:300]}
        if not sample["available"]:
            async def unavailable_gen():
                yield KEEPALIVE_FRAME
                err = {"error": sample["reason"]}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                unavailable_gen(), media_type="text/event-stream", headers=SSE_HEADERS
            )

    effective_backend = req.backend  # 監査ログ用の実効値
    if req.target == "datasets":
        # ENH-01: 本人のCSVデータセット。本人専用Select AIプロファイルで生成。
        # feedback 20260620 #3: 選択モデルでプロファイルを(必要なら)整える(#2: 準備待ちも内包)。
        # ensure_profile は再構築/ウォームアップでブロックしうるためワーカースレッド内で呼ぶ。
        effective_backend = "select_ai"  # datasetsは常にSelect AI経由(監査ログの実効値)

        def generator(q: str) -> str:
            prof = datasets.ensure_profile(user.subject, req.model)
            return nl2sql.generate_sql_select_ai(q, profile_name=prof)
    elif req.backend == "select_ai" or (
        req.target == "sample" and not get_settings().semstore_ocid
    ):
        # PORT-02: semantic store未構成の別テナンシでは既定でselect_ai経路へ切替える
        # (公開ORMスタックはSemanticStoreを作らないため — 既定dbchatが必ず壊れる問題の根治)。
        # web UIは常にbackendを明示送信するため「未指定」と「明示sql_search」をワイヤ上で
        # 区別できず、両方をここで拾う(schemas.Nl2SqlRequestのponytailコメント参照)。
        effective_backend = "select_ai"

        def generator(q: str) -> str:
            return nl2sql.generate_sql_select_ai(q, model=req.model)
    else:
        generator = nl2sql.generate_sql

    async def gen():
        yield KEEPALIVE_FRAME
        task = asyncio.create_task(asyncio.to_thread(generator, req.question))
        try:
            while True:
                try:
                    sql = await asyncio.wait_for(
                        asyncio.shield(task), timeout=KEEPALIVE_SECONDS
                    )
                    break
                except TimeoutError:
                    yield KEEPALIVE_FRAME
            log_with(logger, logging.INFO, "nl2sql generated", user=user.subject)
            yield f"data: {json.dumps({'sql': sql}, ensure_ascii=False)}\n\n"
            await asyncio.to_thread(
                audit.log_event, user.subject, "nl2sql", effective_backend
            )
        except Exception as e:
            logger.exception("nl2sql generation failed")
            err = {"error": f"SQL生成に失敗しました: {str(e)[:200]}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api/dbchat/chart")
async def dbchat_chart(
    req: ChartSuggestRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    """結果表に適したチャートのLLM提案(SQL-03)"""
    try:
        return await asyncio.to_thread(
            nl2sql.suggest_chart, req.question, req.columns, req.rows
        )
    except Exception as e:
        logger.exception("chart suggest failed")
        raise HTTPException(status_code=502, detail=f"チャート提案に失敗: {e}") from e


@router.get("/api/dbchat/schema")
async def dbchat_schema(user: Annotated[AuthContext, Depends(require_user)]):
    """対象スキーマの一覧(UIの「質問できるデータ」表示用 — SQL-02b)"""
    info = await asyncio.to_thread(nl2sql.get_schema_info)
    sample = await asyncio.to_thread(nl2sql.sh_sample_status)
    return {
        **info,
        "sample_available": sample["available"],
        **({"sample_unavailable_reason": sample["reason"]} if not sample["available"] else {}),
    }


@router.get("/api/dbchat/select-ai-models")
async def dbchat_select_ai_models(user: Annotated[AuthContext, Depends(require_user)]):
    """Select AIで選択可能なモデル一覧(feedback 20260620 #3)。UIのドロップダウン用。"""
    return {"models": nl2sql.SELECT_AI_MODELS, "default": nl2sql.DEFAULT_SELECT_AI_MODEL}


@router.get("/api/dbchat/preview")
async def dbchat_preview(
    table: str, user: Annotated[AuthContext, Depends(require_user)]
):
    """テーブルの中身(サンプル行)を参照(ENH-02。read-only・既知テーブル検証)"""
    try:
        return await asyncio.to_thread(nl2sql.preview_table, table)
    except nl2sql.SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# --- 構造化データ(CSV)アップロード→DBチャット対象化(ENH-01) ---

@router.get("/api/db/datasets")
async def list_datasets(user: Annotated[AuthContext, Depends(require_user)]):
    return {"datasets": await asyncio.to_thread(datasets.list_datasets, user.subject)}


@router.post("/api/db/datasets")
async def create_dataset(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)]
):
    import pathlib

    name = pathlib.Path(file.filename or "dataset.csv").name
    if pathlib.Path(name).suffix.lower() != ".csv":
        raise HTTPException(status_code=422, detail="CSVファイルのみ対応です")
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(status_code=413, detail="ファイルが大きすぎます(最大5MB)")
    if not content:
        raise HTTPException(status_code=422, detail="空のファイルです")
    display = pathlib.Path(name).stem
    try:
        return await asyncio.to_thread(
            datasets.create_dataset, user.subject, display, content
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("dataset create failed")
        raise HTTPException(status_code=502, detail=f"取り込みに失敗: {str(e)[:200]}") from e


@router.post("/api/db/datasets/generate")
async def generate_dataset(
    req: GenerateDatasetRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    """AIでサンプルデータ(CSV)を生成しデータセット化(feedback 20260618-3)"""
    try:
        return await asyncio.to_thread(
            datasets.generate_dataset,
            user.subject, req.description, req.display_name, req.rows, req.model,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("dataset generate failed")
        raise HTTPException(status_code=502, detail=f"生成に失敗: {str(e)[:200]}") from e


@router.post("/api/db/datasets/seed")
async def seed_datasets(
    req: SeedDatasetsRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    """既定のサンプルデータセットを本人スキーマへ一括投入(feedback 20260620 #12)"""
    try:
        return await asyncio.to_thread(datasets.seed_samples, user.subject, req.model)
    except Exception as e:
        logger.exception("dataset seed failed")
        raise HTTPException(status_code=502, detail=f"投入に失敗: {str(e)[:200]}") from e


@router.get("/api/db/datasets/{ds_id}/preview")
async def dataset_preview(
    ds_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        return await asyncio.to_thread(datasets.preview, user.subject, ds_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/api/db/datasets/{ds_id}")
async def delete_dataset(
    ds_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    if not await asyncio.to_thread(datasets.delete_dataset, user.subject, ds_id):
        raise HTTPException(status_code=404, detail="dataset not found")
    return {"deleted": True}


@router.post("/api/dbchat/execute")
async def dbchat_execute(
    req: ExecuteSqlRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        result = await asyncio.to_thread(nl2sql.execute_readonly, req.sql)
    except nl2sql.SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except oracledb.DatabaseError as e:
        msg = str(e).splitlines()[0][:300]
        if "DPY-" in msg:  # 接続系はDB停止扱い(503ハンドラへ)
            raise
        raise HTTPException(status_code=400, detail=f"SQL実行エラー: {msg}") from e
    log_with(logger, logging.INFO, "dbchat executed",
             user=user.subject, rows=result["row_count"])
    return result
