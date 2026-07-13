"""RAGファイル管理ルート(RAG-01)。

*_response 関数は user 単位/デモスコープ(SP1-03)で共有する本体。ns はRAG文書の
名前空間キー(user単位= user.subject、デモスコープ= DemoContext.namespace)。
"""

import asyncio
import logging
import pathlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from openai import APIStatusError

from jetuse_core import rag
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.genai import ProjectResolutionError

logger = logging.getLogger("jetuse.service")
router = APIRouter()

# FIX-47: CP/DP 由来の 4xx を 500 のまま漏らさず、原因ヒント付きで表面化する。
# レスポンスbody(OCID等を含みうる)は返さない。詳細はサーバーログと /api/rag/health で追う。
_GENAI_HINT = (
    "OCI GenAI 呼び出しが HTTP {code} で失敗しました。DG matching rule / "
    "IAM policy statements / PROJECT_OCID / リージョンの agentic API 対応を"
    "確認してください(GET /api/rag/health で自己診断できます)"
)


async def _rag_call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except rag.StoreNotReadyError as e:
        raise HTTPException(
            status_code=503, detail="vector store not ready, retry later"
        ) from e
    except ProjectResolutionError as e:
        logger.warning("rag: generative-ai project unresolved: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except APIStatusError as e:
        logger.exception("rag: OCI GenAI call failed (HTTP %s)", e.status_code)
        status = 503 if e.status_code in (401, 403, 404) else 502
        raise HTTPException(
            status_code=status, detail=_GENAI_HINT.format(code=e.status_code)
        ) from e


async def list_files_response(ns: str) -> dict:
    files = await _rag_call(rag.list_files, ns)
    files = await _rag_call(rag.refresh_statuses, ns, files)
    files = await _rag_call(rag.attach_backend_status, ns, files)
    return {"files": files}


async def upload_file_response(ns: str, file: UploadFile) -> dict:
    name = pathlib.Path(file.filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in rag.ALLOWED_EXTENSIONS:
        detail = f"unsupported file type '{ext}'. allowed: pdf/txt/md"
        if ext == ".docx":
            detail += " (docxはVector Store非対応 — SPIKE-03)"
        raise HTTPException(status_code=422, detail=detail)
    content = await file.read()
    if len(content) > rag.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 20MB)")
    if not content:
        raise HTTPException(status_code=422, detail="empty file")
    return await _rag_call(rag.add_file, ns, name, content)


async def delete_file_response(ns: str, file_id: str) -> dict:
    if not await _rag_call(rag.delete_file, ns, file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": True}


@router.get("/api/rag/health")
async def rag_health(user: Annotated[AuthContext, Depends(require_user)]):
    """プリフライト3点検査(FIX-47): project解決 / CP / DP。失敗点をヒント付きで特定する。"""
    try:
        return await asyncio.to_thread(rag.health_check)
    except Exception as e:  # 診断エンドポイントは 500 を漏らさない(REV-001 minor)
        logger.exception("rag health check crashed")
        raise HTTPException(
            status_code=503, detail=f"health check failed: {type(e).__name__}"
        ) from e


@router.get("/api/rag/files")
async def list_rag_files(user: Annotated[AuthContext, Depends(require_user)]):
    return await list_files_response(user.subject)


@router.post("/api/rag/files")
async def upload_rag_file(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)]
):
    return await upload_file_response(user.subject, file)


@router.delete("/api/rag/files/{file_id}")
async def delete_rag_file(
    file_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    return await delete_file_response(user.subject, file_id)
