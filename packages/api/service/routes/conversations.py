"""会話・タイトル生成ルート(CHAT-02/05/09)。"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import conversations as conv_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.chat import complete_once
from jetuse_core.logging import log_with
from jetuse_core.models import MODELS
from jetuse_core.owner_keys import owner_key_gate, user_owner_key

from ..schemas import ConversationCreate

logger = logging.getLogger("jetuse.service")
router = APIRouter()


def _owner(user: AuthContext) -> str:
    """owner キー導出の前に符号化導入 preflight を通す(予約接頭辞行が未分類なら 503 —
    escaped key で即検索して legacy demo_/sub_ 利用者の履歴を空表示・別キー新規作成する
    のを防ぐ。M004。process 内キャッシュ後は no-op)。"""
    owner_key_gate()
    return user_owner_key(user.subject)


def _delete_oci_conversation(*args, **kwargs):
    # tests が `service.main.delete_oci_conversation` を monkeypatch するため
    # 呼び出し時に service.main 経由で解決する(lazy import で循環回避)。
    from .. import main as svc_main
    return svc_main.delete_oci_conversation(*args, **kwargs)


@router.get("/api/conversations")
def list_conversations(user: Annotated[AuthContext, Depends(require_user)]):
    return {"conversations": conv_repo.list_conversations(_owner(user))}


@router.post("/api/conversations")
def create_conversation(
    req: ConversationCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")
    return conv_repo.create_conversation(_owner(user), req.model, req.title)


@router.get("/api/conversations/{cid}")
def get_conversation(cid: str, user: Annotated[AuthContext, Depends(require_user)]):
    conv = conv_repo.get_conversation(_owner(user), cid)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, user: Annotated[AuthContext, Depends(require_user)]):
    # OCI Conversation(短期メモリ)はADB削除成功後にベストエフォート削除(CHAT-09)。
    # 履歴の正はADBでありOCI側はretentionでも消えるため、失敗してもAPIは成功を返す
    conv = conv_repo.get_conversation(_owner(user), cid)
    if not conv_repo.delete_conversation(_owner(user), cid):
        raise HTTPException(status_code=404, detail="conversation not found")
    oci_conv = (conv or {}).get("oci_conversation_id")
    if oci_conv:
        try:
            _delete_oci_conversation(oci_conv)
            log_with(logger, logging.INFO, "oci conversation deleted",
                     conversation_id=cid, oci_conversation_id=oci_conv)
        except Exception:
            logger.exception("oci conversation delete failed (ignored)")
    return {"deleted": True}


@router.post("/api/conversations/{cid}/title")
def generate_title(cid: str, user: Annotated[AuthContext, Depends(require_user)]):
    """初回応答後にllama(高速)で短いタイトルを生成して更新(CHAT-05)。"""
    conv = conv_repo.get_conversation(_owner(user), cid)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    if not conv["messages"]:
        raise HTTPException(status_code=400, detail="no messages")
    digest = "\n".join(
        f"{m['role']}: {m['content'][:300]}" for m in conv["messages"][:4]
    )
    try:
        title = complete_once(
            "llama-3.3-70b",
            [
                {
                    "role": "user",
                    "content": "次の会話を表す15文字以内の日本語タイトルを1つだけ、"
                    f"記号や引用符なしで出力してください。\n\n{digest}",
                }
            ],
            max_chars=40,
        ).strip().strip('「」"')
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"title generation failed: {e}") from e
    if not title:
        raise HTTPException(status_code=502, detail="empty title")
    conv_repo.update_title(_owner(user), cid, title)
    return {"title": title}
