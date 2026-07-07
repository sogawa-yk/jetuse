"""NL2SQL・DBチャート・データセット(CSV)ルート(SQL-02/03, ENH-01/02)。

*_response 関数は user 単位/デモスコープ(SP2-03 / specs/18 §4.3)で共有する本体。
owner キーは user 単位 = user_owner_key(user.subject)、デモスコープ = DemoContext.namespace。
"""

import asyncio
import functools
import json
import logging
import pathlib
from collections.abc import Callable
from typing import Annotated

import oracledb
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from jetuse_core import audit, datasets, demo_lease, nl2sql, vpd
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.owner_keys import OwnerKeyPreflightError, owner_key_gate, user_owner_key

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

# demo 経路がハンドラ内で再送出する fail-closed 例外(main.py のハンドラが 404/503 に写像)
_PASSTHROUGH = (
    vpd.DatasetsSecurityError,
    OwnerKeyPreflightError,
    demo_lease.DemoGoneError,
    demo_lease.LeaseUnavailableError,
    demo_lease.LeaseTimeoutError,
)


def datasets_nl2sql_preflight(owner_key: str, demo_id: str | None = None) -> None:
    """datasets NL2SQL の SSE 開始前の **fast** fail-closed 検査(404/503 へ写像)。

    VPD 完全性・owner-key・demo 状態/リース可否だけを見る。**遅い profile 再構築/warmup は
    含めない** — それは keepalive を送れる SSE ワーカー(datasets_generator)で行う
    (review-4 M001 — cold cache でも最初の keepalive を送れるよう warmup を stream 前に置かない。
    review-3 M002 — fail-closed 例外は 200 SSE でなく既定の 404/503 ハンドラへ)。
    """
    vpd.integrity_gate()   # VPD 完全性欠落 → DatasetsSecurityError(503)
    owner_key_gate()       # 未分類の予約接頭辞行 → OwnerKeyPreflightError(503)
    if demo_id is not None:
        # nowait(timeout_s=0): demo 行なし/deleting=404・**競合中は即 503**。既定 300s を待って
        # stream 開始をブロックしない(review-5 M001 — gateway timeout 回避)。実 build は worker が
        # 既定 timeout(keepalive 下)でリースを取り直す。
        with demo_lease.mutation(demo_id, timeout_s=0):
            pass


def datasets_generator(
    owner_key: str, model: str | None, demo_id: str | None = None
) -> Callable[[str], str]:
    """datasets ターゲット NL2SQL の SSE 本体。遅い profile 再構築/warmup を keepalive しながら
    実行し、生成本体(GENERATE)はリース外で流す(specs/18 §3.2.1)。fast な fail-closed 検査は
    datasets_nl2sql_preflight で stream 開始前に済ませる(cold path の keepalive 確保 — M001)。
    """
    def generator(q: str) -> str:
        if demo_id is None:
            prof = datasets.ensure_profile(owner_key, model)
        else:
            with demo_lease.mutation(demo_id) as lease:  # profile lazy-gen はリース下
                prof = datasets.ensure_profile(owner_key, model, lease=lease)
        return nl2sql.generate_sql_select_ai(q, profile_name=prof)  # GENERATE はリース外

    return generator


def nl2sql_sse_response(
    generator: Callable[[str], str], question: str, subject: str, audit_meta: str
) -> StreamingResponse:
    """NL2SQL 生成の SSE 本体(user/デモスコープ共有)。実測30秒前後で keepalive 必須。"""

    async def gen():
        yield KEEPALIVE_FRAME
        task = asyncio.create_task(asyncio.to_thread(generator, question))
        try:
            while True:
                try:
                    sql = await asyncio.wait_for(
                        asyncio.shield(task), timeout=KEEPALIVE_SECONDS
                    )
                    break
                except TimeoutError:
                    yield KEEPALIVE_FRAME
            log_with(logger, logging.INFO, "nl2sql generated", user=subject)
            yield f"data: {json.dumps({'sql': sql}, ensure_ascii=False)}\n\n"
            await asyncio.to_thread(audit.log_event, subject, "nl2sql", audit_meta)
        except _PASSTHROUGH as e:
            # preflight 通過後(stream 開始後)に fail-closed 条件が競合発生した場合(worker が
            # リースを取り直す瞬間の DELETE 競合 = DemoGone/リース不可 等)。HTTP 404/503 は既に
            # 送れないので **明示的な SSE エラー契約**で通知する(review-6 M001 — 汎用「生成失敗」に
            # 埋もれさせない。code はクライアントが再試行/中断を判断できる機械可読値)。
            code = ("demo_gone" if isinstance(e, demo_lease.DemoGoneError)
                    else "temporarily_unavailable")
            logger.warning("nl2sql fail-closed after stream start: %s (%s)",
                           type(e).__name__, code)
            err = {"error": "デモが利用できません(競合により中断しました)", "code": code}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("nl2sql generation failed")
            err = {"error": f"SQL生成に失敗しました: {str(e)[:200]}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# --- NL2SQL(SQL-02): 生成はSSE(実測30秒前後でkeepalive必須) ---

@router.post("/api/chat/nl2sql")
async def nl2sql_generate(
    req: Nl2SqlRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    if req.target == "datasets":
        # ENH-01: 本人のCSVデータセット。本人専用Select AIプロファイルで生成。
        # feedback 20260620 #3: 選択モデルでプロファイルを(必要なら)整える(#2: 準備待ちも内包)。
        # fast な fail-closed 検査は SSE 開始前(M002)。遅い warmup は worker 内(M001)。
        owner_key = user_owner_key(user.subject)
        await asyncio.to_thread(datasets_nl2sql_preflight, owner_key)
        generator = datasets_generator(owner_key, req.model)
    elif req.backend == "select_ai":
        def generator(q: str) -> str:
            return nl2sql.generate_sql_select_ai(q, model=req.model)
    else:
        generator = nl2sql.generate_sql

    return nl2sql_sse_response(generator, req.question, user.subject, req.backend)


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
    return await asyncio.to_thread(nl2sql.get_schema_info)


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
        return await asyncio.to_thread(
            functools.partial(nl2sql.preview_table, table,
                              owner_key=user_owner_key(user.subject)))
    except nl2sql.SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# --- 構造化データ(CSV)アップロード→DBチャット対象化(ENH-01) ---
# *_response は user/デモスコープ共有の本体(specs/18 §4.3)。demo_id 指定時は
# demo 単位の排他リースを操作の開始から完了まで保持する(specs/18 §3.2.1)。


async def list_datasets_response(owner_key: str) -> dict:
    return {"datasets": await asyncio.to_thread(datasets.list_datasets, owner_key)}


async def create_dataset_response(
    owner_key: str, file: UploadFile,
    model: str | None = None, demo_id: str | None = None,
) -> dict:
    name = pathlib.Path(file.filename or "dataset.csv").name
    if pathlib.Path(name).suffix.lower() != ".csv":
        raise HTTPException(status_code=422, detail="CSVファイルのみ対応です")
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(status_code=413, detail="ファイルが大きすぎます(最大5MB)")
    if not content:
        raise HTTPException(status_code=422, detail="空のファイルです")
    display = pathlib.Path(name).stem

    def work():
        if demo_id is None:
            return datasets.create_dataset(owner_key, display, content, model=model)
        with demo_lease.mutation(demo_id) as lease:
            return datasets.create_dataset(
                owner_key, display, content, model=model, lease=lease)

    try:
        return await asyncio.to_thread(work)
    except _PASSTHROUGH:
        raise  # main.py のハンドラで 404/503(fail-closed)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("dataset create failed")
        raise HTTPException(status_code=502, detail=f"取り込みに失敗: {str(e)[:200]}") from e


async def generate_dataset_response(
    owner_key: str, req: GenerateDatasetRequest,
    model: str | None = None, demo_id: str | None = None,
) -> dict:
    def work():
        if demo_id is None:
            return datasets.generate_dataset(
                owner_key, req.description, req.display_name, req.rows, model)
        with demo_lease.mutation(demo_id) as lease:
            return datasets.generate_dataset(
                owner_key, req.description, req.display_name, req.rows, model,
                lease=lease)

    try:
        return await asyncio.to_thread(work)
    except _PASSTHROUGH:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("dataset generate failed")
        raise HTTPException(status_code=502, detail=f"生成に失敗: {str(e)[:200]}") from e


async def preview_dataset_response(owner_key: str, ds_id: str) -> dict:
    try:
        return await asyncio.to_thread(datasets.preview, owner_key, ds_id)
    except _PASSTHROUGH:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def delete_dataset_response(
    owner_key: str, ds_id: str, demo_id: str | None = None
) -> dict:
    def work():
        if demo_id is None:
            return datasets.delete_dataset(owner_key, ds_id)
        with demo_lease.mutation(demo_id) as lease:
            return datasets.delete_dataset(owner_key, ds_id, lease=lease)

    try:
        deleted = await asyncio.to_thread(work)
    except datasets.DatasetDeleteError as e:
        # DROP 先行の失敗は登録簿行を残して 503(再試行で収束 — specs/18 §3.2 手順 2)
        raise HTTPException(status_code=503, detail=str(e)[:300]) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="dataset not found")
    return {"deleted": True}


async def execute_sql_response(sql: str, owner_key: str | None, subject: str) -> dict:
    """SQL 実行本体(user/デモスコープ共有)。層2ゲートの越境拒否は 403(specs/18 §4.3)。"""
    try:
        result = await asyncio.to_thread(nl2sql.execute_readonly, sql, owner_key)
    except nl2sql.SqlBoundaryError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except nl2sql.SqlRejectedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except oracledb.DatabaseError as e:
        msg = str(e).splitlines()[0][:300]
        if "DPY-" in msg:  # 接続系はDB停止扱い(503ハンドラへ)
            raise
        raise HTTPException(status_code=400, detail=f"SQL実行エラー: {msg}") from e
    log_with(logger, logging.INFO, "dbchat executed",
             user=subject, rows=result["row_count"])
    return result


@router.get("/api/db/datasets")
async def list_datasets(user: Annotated[AuthContext, Depends(require_user)]):
    return await list_datasets_response(user_owner_key(user.subject))


@router.post("/api/db/datasets")
async def create_dataset(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)]
):
    return await create_dataset_response(user_owner_key(user.subject), file)


@router.post("/api/db/datasets/generate")
async def generate_dataset(
    req: GenerateDatasetRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    """AIでサンプルデータ(CSV)を生成しデータセット化(feedback 20260618-3)"""
    return await generate_dataset_response(
        user_owner_key(user.subject), req, model=req.model)


@router.post("/api/db/datasets/seed")
async def seed_datasets(
    req: SeedDatasetsRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    """既定のサンプルデータセットを本人スキーマへ一括投入(feedback 20260620 #12)"""
    try:
        return await asyncio.to_thread(
            datasets.seed_samples, user_owner_key(user.subject), req.model)
    except (vpd.DatasetsSecurityError, OwnerKeyPreflightError):
        raise
    except Exception as e:
        logger.exception("dataset seed failed")
        raise HTTPException(status_code=502, detail=f"投入に失敗: {str(e)[:200]}") from e


@router.get("/api/db/datasets/{ds_id}/preview")
async def dataset_preview(
    ds_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    return await preview_dataset_response(user_owner_key(user.subject), ds_id)


@router.delete("/api/db/datasets/{ds_id}")
async def delete_dataset(
    ds_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    return await delete_dataset_response(user_owner_key(user.subject), ds_id)


@router.post("/api/dbchat/execute")
async def dbchat_execute(
    req: ExecuteSqlRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    # owner コンテキスト付き実行(specs/18 §4.3 — VPD 層1 の呼び出し元契約)
    return await execute_sql_response(req.sql, user_owner_key(user.subject), user.subject)
