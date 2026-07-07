"""デモスコープ能力ルート(SP1-03)と Demo CRUD(SP2-01 / specs/18 §2)。

能力ルートは require_demo で DemoContext を得て、その箱(`demo_<id>` 名前空間)だけを
操作する。ハンドラ本体は user 単位ルートと共有(chat.stream_chat_response / rag.*_response)。
CRUD は usecases のルート流儀({"demos": [...]} / mine)と同語彙。DELETE は公開しない
(後始末を持たない行削除は SP1-03 の RAG 箱を孤児化する — 後始末込みで SP2-02。specs/18 §2.1)。
"""

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from jetuse_core import conversations as conv_repo
from jetuse_core import datasets, demo_cleanup, demo_lease, demos, nl2sql
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.models import MODELS
from jetuse_core.owner_keys import owner_key_gate, user_owner_key

from ..demo_context import DemoContext, require_demo, require_demo_owner
from ..schemas import (
    ChatRequest,
    ConversationCreate,
    DemoCreate,
    DemoPatch,
    ExecuteSqlRequest,
    GenerateDatasetRequest,
    Nl2SqlRequest,
)
from . import chat as chat_routes
from . import dbchat as dbchat_routes
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


@crud_router.delete("/api/demos/{demo_id}")
async def delete_demo(demo_id: str, user: User):
    """後始末込みの同期 DELETE(specs/18 §2.1・§3.2 — SP2-02 で初公開)。

    require_demo を経由しない: 所有者の DELETE は status='deleting' の残骸にも受理する
    必要がある(後始末途中失敗の再実行 = 収束)。存在秘匿は他ルートと同一の 404。
    失敗はその段階を detail に含む 503(再 DELETE で収束)。
    """
    try:
        return await asyncio.to_thread(demo_cleanup.delete_demo_box, demo_id, user.subject)
    except demo_cleanup.DemoNotFoundError:
        raise HTTPException(status_code=404, detail="demo not found") from None
    except demo_cleanup.CleanupError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/chat")
async def demo_chat(  # noqa: ANN202
    req: ChatRequest,
    ctx: Ctx,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """デモの箱の中でチャット。RAG文書は demo_<id> 名前空間に閉じる。

    conversation_id は demo 会話(POST .../conversations で作成)のみ受理(specs/18 §4.2 —
    検証と demo_id IS NULL/一致の強制は stream_chat_response)。SSE は会話を自動作成せず、
    OCI Conversation も作らない(継続 = クライアントが messages に全履歴を再送)。
    """
    # Select AI RAG は profile/index を lazy 生成しうる。SSE 本体はリースを跨がないが、
    # 作成区間だけは demo 単位リース下で行い、解体中の箱を復活させ孤児化するのを防ぐ
    # (specs/18 §3.2.1)。作成 → 解放 → ストリームの順(生成本体はリース外)。
    if req.rag and req.rag_backend == "select_ai":
        from jetuse_core import rag_select_ai

        def _provision():
            with demo_lease.mutation(ctx.demo_id) as lease:  # deleting は 404
                rag_select_ai.ensure_profile(ctx.namespace, lease=lease)

        try:
            await asyncio.to_thread(_provision)
        except demo_lease.DemoGoneError:
            raise HTTPException(status_code=404, detail="demo not found") from None
    return await chat_routes.stream_chat_response(
        req, user, ctx.namespace, demo_id=ctx.demo_id)


@router.post("/conversations")
async def demo_create_conversation(req: ConversationCreate, ctx: Ctx, user: User):
    """デモ会話の作成(specs/18 §4.2)。require_demo — 公開デモで chat を実行できる者は
    会話も持てる。行は owner_sub = owner_key(user.subject)・demo_id = ctx.demo_id。
    レスポンスは既存 POST /api/conversations と同形。OCI Conversation は作らない。
    demo 会話 INSERT は排他リース下(specs/18 §3.2.1 — 削除の列挙後に行を作らせない)。
    一覧・履歴取得・個別削除はデモ SPA の要件が確定する SP3 で追加する。
    """
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")
    await asyncio.to_thread(owner_key_gate)  # 未分類の予約接頭辞行が残る間は 503

    def _create():
        with demo_lease.mutation(ctx.demo_id):  # 行なし/deleting は 404(DemoGoneError)
            return conv_repo.create_conversation(
                user_owner_key(user.subject), req.model, req.title,
                demo_id=ctx.demo_id,
            )

    return await asyncio.to_thread(_create)


# --- dbchat 縦切り(SP2-03 / specs/18 §4.3): デモの DB 箱 = datasets ターゲット固定 ---


def _demo_dbchat_model(demo_id: str) -> str | None:
    """demo config の正規キー config.dbchat.model(specs/18 §2.2)。省略時は既定モデル。

    demo nl2sql のモデルは config 固定(specs/18 §4.3): リクエストの model 入力は無視する
    (非所有者のモデル指定で共有プロファイルの再構築・warmup を起こさせない)。
    変更は owner の PATCH のみ(検証は _validate_config — POST/PATCH 同一契約)。
    """
    d = demos.get_demo(demo_id)
    dbchat_cfg = ((d or {}).get("config") or {}).get("dbchat")
    return dbchat_cfg.get("model") if isinstance(dbchat_cfg, dict) else None


@router.post("/dbchat/nl2sql")
async def demo_nl2sql(req: Nl2SqlRequest, ctx: Ctx, user: User):
    """箱の datasets への NL2SQL 生成(SSE)。target/backend/model は demo では固定
    (datasets + demo 専用 Select AI プロファイル + config.dbchat.model)。"""
    # fast な fail-closed 検査(demo status・リース可否・VPD/owner-key)は SSE 開始前に実行し
    # 404/503 へ写像(review-3 M002)。遅い profile 再構築/warmup は SSE ワーカーで keepalive
    # しながら行う(review-4 M001)。SSE 本体の GENERATE はリース外(specs/18 §3.2.1)。
    await asyncio.to_thread(
        dbchat_routes.datasets_nl2sql_preflight, ctx.namespace, ctx.demo_id)
    generator = dbchat_routes.datasets_generator(
        ctx.namespace, _demo_dbchat_model(ctx.demo_id), ctx.demo_id)
    return dbchat_routes.nl2sql_sse_response(
        generator, req.question, user.subject, "select_ai")


@router.post("/dbchat/execute")
async def demo_dbchat_execute(req: ExecuteSqlRequest, ctx: Ctx, user: User):
    """箱の datasets への読取専用 SQL 実行。owner キー = ctx.namespace(VPD 層1)+
    層2ゲート(越境は 403 — specs/18 §4.3)。"""
    await asyncio.to_thread(owner_key_gate)
    return await dbchat_routes.execute_sql_response(
        req.sql, ctx.namespace, user.subject)


@router.get("/dbchat/schema")
async def demo_dbchat_schema(ctx: Ctx):
    """箱の datasets(登録簿 owner_sub=namespace)から表・列を返す(specs/18 §4.3)。"""
    return await asyncio.to_thread(datasets.schema_info, ctx.namespace)


@router.get("/db/datasets")
async def demo_list_datasets(ctx: Ctx):
    return await dbchat_routes.list_datasets_response(ctx.namespace)


@router.post("/db/datasets")
async def demo_create_dataset(file: UploadFile, ctx: OwnerCtx):
    return await dbchat_routes.create_dataset_response(
        ctx.namespace, file,
        model=_demo_dbchat_model(ctx.demo_id), demo_id=ctx.demo_id)


@router.post("/db/datasets/generate")
async def demo_generate_dataset(req: GenerateDatasetRequest, ctx: OwnerCtx):
    # モデルは config 固定(リクエストの model は無視 — specs/18 §4.3)
    return await dbchat_routes.generate_dataset_response(
        ctx.namespace, req,
        model=_demo_dbchat_model(ctx.demo_id), demo_id=ctx.demo_id)


@router.get("/db/datasets/{ds_id}/preview")
async def demo_dataset_preview(ds_id: str, ctx: Ctx):
    return await dbchat_routes.preview_dataset_response(ctx.namespace, ds_id)


@router.delete("/db/datasets/{ds_id}")
async def demo_delete_dataset(ds_id: str, ctx: OwnerCtx):
    return await dbchat_routes.delete_dataset_response(
        ctx.namespace, ds_id, demo_id=ctx.demo_id)


@router.get("/rag/files")
async def demo_list_rag_files(ctx: Ctx):
    return await rag_routes.list_files_response(ctx.namespace)


@router.post("/rag/files")
async def demo_upload_rag_file(file: UploadFile, ctx: OwnerCtx):
    # demo_id 指定で demo 単位の排他リースを保持(specs/18 §3.2.1)
    return await rag_routes.upload_file_response(ctx.namespace, file, demo_id=ctx.demo_id)


@router.delete("/rag/files/{file_id}")
async def demo_delete_rag_file(file_id: str, ctx: OwnerCtx):
    return await rag_routes.delete_file_response(ctx.namespace, file_id,
                                                 demo_id=ctx.demo_id)
