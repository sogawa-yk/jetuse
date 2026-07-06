"""RAGファイル管理ルート(RAG-01)。

*_response 関数は user 単位/デモスコープ(SP1-03)で共有する本体。ns はRAG文書の
名前空間キー(user単位= user.subject、デモスコープ= DemoContext.namespace)。
"""

import asyncio
import logging
import pathlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from jetuse_core import rag
from jetuse_core.auth import AuthContext, require_user

logger = logging.getLogger("jetuse.service")
router = APIRouter()


async def list_files_response(ns: str) -> dict:
    files = await asyncio.to_thread(rag.list_files, ns)
    files = await asyncio.to_thread(rag.refresh_statuses, ns, files)
    files = await asyncio.to_thread(rag.attach_backend_status, ns, files)
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
    return await asyncio.to_thread(rag.add_file, ns, name, content)


async def delete_file_response(ns: str, file_id: str) -> dict:
    if not await asyncio.to_thread(rag.delete_file, ns, file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": True}


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
