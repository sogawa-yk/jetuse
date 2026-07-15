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

from jetuse_core import demo_lease, rag
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.genai import ProjectResolutionError
from jetuse_core.owner_keys import owner_key_gate, user_owner_key

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
    # read も移行ゲートを通す(未分類の予約接頭辞行が残る間は fail-closed=503)。書き込み経路
    # (add_file/delete_file)は既に owner_key_gate を通す。read だけ素通りだと、旧命名の
    # 予約接頭辞ユーザー資産(owner_sub='demo_<id>')が同 ID の demo 経路から参照され得る
    # (越境。OwnerKeyPreflightError→503。実在 sub のみ環境では no-op — codex review-9 B001)。
    await asyncio.to_thread(owner_key_gate)
    files = await _rag_call(rag.list_files, ns)
    files = await _rag_call(rag.refresh_statuses, ns, files)
    files = await _rag_call(rag.attach_backend_status, ns, files)
    return {"files": files}


async def upload_file_response(ns: str, file: UploadFile,
                               demo_id: str | None = None) -> dict:
    """アップロード本体(user/デモスコープ共有)。demo_id 指定時は demo 単位の排他リースを
    操作の開始から完了まで保持する(specs/18 §3.2.1 — lazy 生成と DELETE の競合防止)。"""
    name = pathlib.Path(file.filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in rag.ALLOWED_EXTENSIONS:
        detail = f"unsupported file type '{ext}'. allowed: pdf/txt/md"
        if ext == ".docx":
            detail += " (docxはVector Store非対応 — SPIKE-03)"
        raise HTTPException(status_code=422, detail=detail)
    if len(name) > rag.MAX_FILENAME_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"filename too long (max {rag.MAX_FILENAME_CHARS} chars)",
        )
    content = await file.read()
    if len(content) > rag.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 20MB)")
    if not content:
        raise HTTPException(status_code=422, detail="empty file")

    def work():
        if demo_id is None:
            return rag.add_file(ns, name, content)
        with demo_lease.mutation(demo_id) as lease:  # 行なし/deleting は 404(2契約)
            return rag.add_file(ns, name, content, lease=lease)

    try:
        # _rag_call が CP/DP 由来 4xx→503/502・project 未解決→503 の変換を担う(FIX-47/PORT-02)
        return await _rag_call(work)
    except rag.BoxLimitExceededError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


async def delete_file_response(ns: str, file_id: str,
                               demo_id: str | None = None) -> dict:
    def work():
        if demo_id is None:
            return rag.delete_file(ns, file_id)
        with demo_lease.mutation(demo_id):
            return rag.delete_file(ns, file_id)

    try:
        deleted = await _rag_call(work)
    except rag.ExternalDeleteError as e:
        # 外部先行削除の失敗は行とカウンタを保持して 503(再試行で収束 — specs/18 §3.2)
        raise HTTPException(status_code=503, detail=str(e)[:300]) from e
    if not deleted:
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


# user 単位ルートも資源キーの導出は owner キーヘルパーを必ず通す(specs/18 §3.2.1 —
# sub='demo_<uuid>' のユーザーが同名 demo の資源キーと衝突するのを防ぐ。実在 sub は no-op)


@router.get("/api/rag/files")
async def list_rag_files(user: Annotated[AuthContext, Depends(require_user)]):
    return await list_files_response(user_owner_key(user.subject))


@router.post("/api/rag/files")
async def upload_rag_file(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)]
):
    return await upload_file_response(user_owner_key(user.subject), file)


@router.delete("/api/rag/files/{file_id}")
async def delete_rag_file(
    file_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    return await delete_file_response(user_owner_key(user.subject), file_id)
