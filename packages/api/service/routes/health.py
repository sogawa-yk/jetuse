"""機能別 readiness ルート(PORT-02)。Issue #47 報告者の自己診断用。"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import health
from jetuse_core.auth import AuthContext, require_user

logger = logging.getLogger("jetuse.service")
router = APIRouter()


@router.get("/api/health")
async def capability_health(user: Annotated[AuthContext, Depends(require_user)]):
    try:
        # 同期のOCI/DB呼び出しを含むため /api/rag/health と同様にワーカースレッドで実行し
        # イベントループをブロックしない(レビュー指摘F-002)。
        return await asyncio.to_thread(health.capability_health)
    except Exception as e:  # 診断エンドポイントは500を漏らさない
        logger.exception("capability health check crashed")
        raise HTTPException(
            status_code=503, detail=f"health check failed: {type(e).__name__}"
        ) from e
