"""ユーザー情報・管理ルート(SEC-02/OPS-01)。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import audit
from jetuse_core.auth import AuthContext, require_user

from ..deps import is_admin

router = APIRouter()


@router.get("/api/me")
async def me(user: Annotated[AuthContext, Depends(require_user)]):
    """ログインユーザー情報(アカウントメニュー・管理メニュー表示制御用)"""
    claims = user.claims or {}
    return {
        "subject": user.subject,
        "name": claims.get("name") or claims.get("preferred_username") or user.subject,
        "email": claims.get("email"),
        "is_admin": is_admin(user),
    }


@router.get("/api/admin/usage")
async def admin_usage(
    user: Annotated[AuthContext, Depends(require_user)], days: int = 30
):
    """利用状況の集計(SEC-02/OPS-01)。ADMIN_USERS(sub または email)のみ"""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")
    return await asyncio.to_thread(audit.summarize, max(1, min(days, 365)))
