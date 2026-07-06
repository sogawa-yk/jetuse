"""デモスコープ能力ルート(SP1-03)と Demo CRUD(SP2-01 / specs/18 §2)。

能力ルートは require_demo で DemoContext を得て、その箱(`demo_<id>` 名前空間)だけを
操作する。ハンドラ本体は user 単位ルートと共有(chat.stream_chat_response / rag.*_response)。
CRUD は usecases のルート流儀({"demos": [...]} / mine)と同語彙。DELETE は公開しない
(後始末を持たない行削除は SP1-03 の RAG 箱を孤児化する — 後始末込みで SP2-02。specs/18 §2.1)。
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from jetuse_core import demos, nl2sql
from jetuse_core.auth import AuthContext, require_user

from ..demo_context import DemoContext, require_demo, require_demo_owner
from ..schemas import ChatRequest, DemoCreate, DemoPatch
from . import chat as chat_routes
from . import rag as rag_routes

router = APIRouter(prefix="/api/demos/{demo_id}")
crud_router = APIRouter()  # collection ルート(/api/demos)を含むため prefix なし

# 閲覧・実行(公開デモは非所有者も可) / 書き込み(所有者のみ — REV-002)
Ctx = Annotated[DemoContext, Depends(require_demo)]
OwnerCtx = Annotated[DemoContext, Depends(require_demo_owner)]
User = Annotated[AuthContext, Depends(require_user)]

MAX_CONFIG_BYTES = 1_048_576  # 直列化後 1MB(specs/18 §2.2 — 信頼境界の入力上限)
_DBCHAT_MODELS = {m["key"] for m in nl2sql.SELECT_AI_MODELS}


def _validate_config(config: dict) -> None:
    """config の共通検証(POST/PATCH 同一契約 — specs/18 §2.2)。

    原則不透明だが、`config.dbchat.model` のみ SP2-03 が解釈する正規キーとして形状検証する。
    省略/欠落は既定モデル(検証なし)。他キーは検証せず保存・返却のみ。
    """
    try:
        # NaN/Infinity は json.loads が受理してしまうが正規 JSON ではない(IS JSON 違反を
        # 503/500 で漏らさず 422 へ — review-1 M002)。保存側(demos.py)と同じ直列化契約。
        serialized = json.dumps(config, ensure_ascii=False, allow_nan=False)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail="config must be valid JSON (NaN/Infinity not allowed)"
        ) from e
    if len(serialized.encode()) > MAX_CONFIG_BYTES:
        raise HTTPException(status_code=422, detail="config exceeds 1MB limit")
    if "dbchat" not in config:
        return
    dbchat = config["dbchat"]
    if not isinstance(dbchat, dict):
        raise HTTPException(status_code=422, detail="config.dbchat must be a JSON object")
    if "model" in dbchat and (
        not isinstance(dbchat["model"], str) or dbchat["model"] not in _DBCHAT_MODELS
    ):
        raise HTTPException(
            status_code=422,
            detail=f"config.dbchat.model must be one of: {sorted(_DBCHAT_MODELS)}",
        )


def _demo_out(demo: dict[str, Any], subject: str) -> dict[str, Any]:
    """DemoOut(specs/18 §2.2)。owner_sub は返さず、編集可否は mine で示す。"""
    out = {k: demo[k] for k in ("id", "name", "description", "visibility", "status",
                                "config", "created_at", "updated_at")}
    return {**out, "mine": demo["owner_sub"] == subject}


def _refetch_authorized(demo_id: str, subject: str) -> dict[str, Any]:
    """require_demo 通過後の再取得に認可条件を再適用する(TOCTOU — review-1 B002)。

    認可判定と応答用取得の間に行が変わりうる(public→private + config 更新 / ready→deleting)。
    再取得行にも require_demo と同一の条件を課し、外れていれば存在秘匿の 404。
    """
    d = demos.get_demo(demo_id)
    if (
        not d
        or d["status"] == "deleting"
        or (d["owner_sub"] != subject and d["visibility"] != "public")
    ):
        raise HTTPException(status_code=404, detail="demo not found")
    return d


@crud_router.get("/api/demos")
def list_demos(user: User):
    """自分の所有のみ(updated_at DESC)。公開デモの横断一覧は SP4(specs/18 §2.1)。"""
    return {"demos": [_demo_out(d, user.subject) for d in demos.list_demos(user.subject)]}


@crud_router.post("/api/demos")
def create_demo(req: DemoCreate, user: User):
    """INSERT のみ・即 status='ready'(箱は lazy — specs/18 §3.1)。"""
    _validate_config(req.config)
    d = demos.create_demo(
        user.subject, req.name, req.description, req.visibility, req.config
    )
    return _demo_out(d, user.subject)


@crud_router.get("/api/demos/{demo_id}")
def get_demo(ctx: Ctx, user: User):
    return _demo_out(_refetch_authorized(ctx.demo_id, user.subject), user.subject)


@crud_router.patch("/api/demos/{demo_id}")
def update_demo(req: DemoPatch, ctx: OwnerCtx, user: User):
    """部分更新(specs/18 §2.2 の null 意味論)。status は変更不可(スキーマ非包含)。"""
    fields = req.model_dump(exclude_unset=True)
    for k in ("name", "visibility", "config"):
        if k in fields and fields[k] is None:  # DB 上 NOT NULL — Oracle エラーを 500 で漏らさない
            raise HTTPException(status_code=422, detail=f"{k} cannot be null")
    if fields.get("config") is not None:
        _validate_config(fields["config"])
    if not fields:  # 空 PATCH は 200 で現状を返す(updated_at も変えない)
        d = _refetch_authorized(ctx.demo_id, user.subject)
    else:
        d = demos.update_demo(user.subject, ctx.demo_id, fields)
    if not d:
        raise HTTPException(status_code=404, detail="demo not found")
    return _demo_out(d, user.subject)


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
