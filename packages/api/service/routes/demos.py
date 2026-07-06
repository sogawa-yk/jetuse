"""デモスコープ能力ルート(SP1-03 / specs/17 §5)。chat + rag の縦切り。

全ルートが require_demo で DemoContext を得て、その箱(`demo_<id>` 名前空間)だけを
操作する。ハンドラ本体は user 単位ルートと共有(chat.stream_chat_response / rag.*_response)。
残能力(dbchat/agents/voice/minutes/translate/docunderstand)は SP2 以降で同型追随。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from jetuse_core.auth import AuthContext, require_user

from ..demo_context import DemoContext, require_demo, require_demo_owner
from ..schemas import ChatRequest
from . import chat as chat_routes
from . import rag as rag_routes

router = APIRouter(prefix="/api/demos/{demo_id}")

# 閲覧・実行(公開デモは非所有者も可) / 書き込み(所有者のみ — REV-002)
Ctx = Annotated[DemoContext, Depends(require_demo)]
OwnerCtx = Annotated[DemoContext, Depends(require_demo_owner)]


@router.post("/chat")
async def demo_chat(  # noqa: ANN202
    req: ChatRequest,
    ctx: Ctx,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """デモの箱の中でチャット。RAG文書は demo_<id> 名前空間に閉じる。"""
    if req.conversation_id:
        # 会話の demo_id 紐付け(specs/17 §5)は SP2。user 会話の箱への持ち込みを拒否(REV-004)
        raise HTTPException(
            status_code=422,
            detail="conversation_id is not supported for demo-scoped chat yet (SP2)",
        )
    return await chat_routes.stream_chat_response(req, user, ctx.namespace)


@router.get("/rag/files")
async def demo_list_rag_files(ctx: Ctx):
    return await rag_routes.list_files_response(ctx.namespace)


@router.post("/rag/files")
async def demo_upload_rag_file(file: UploadFile, ctx: OwnerCtx):
    return await rag_routes.upload_file_response(ctx.namespace, file)


@router.delete("/rag/files/{file_id}")
async def demo_delete_rag_file(file_id: str, ctx: OwnerCtx):
    return await rag_routes.delete_file_response(ctx.namespace, file_id)
