"""RAGファイル管理ルート(RAG-01)。"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from jetuse_core import rag
from jetuse_core.auth import AuthContext, require_user

logger = logging.getLogger("jetuse.service")
router = APIRouter()


@router.get("/api/rag/files")
async def list_rag_files(user: Annotated[AuthContext, Depends(require_user)]):
    files = await asyncio.to_thread(rag.list_files, user.subject)
    files = await asyncio.to_thread(rag.refresh_statuses, user.subject, files)
    files = await asyncio.to_thread(rag.attach_backend_status, user.subject, files)
    return {"files": files}


@router.post("/api/rag/files")
async def upload_rag_file(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)]
):
    import pathlib

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
    return await asyncio.to_thread(rag.add_file, user.subject, name, content)


@router.delete("/api/rag/files/{file_id}")
async def delete_rag_file(
    file_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    if not await asyncio.to_thread(rag.delete_file, user.subject, file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": True}
