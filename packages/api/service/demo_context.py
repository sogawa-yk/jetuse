"""(user, demo) スコープの継ぎ目(SP1-02 / specs/17 §5)。

所有権検証は信頼境界 — fail-closed。存在しない demo と他人の private demo は
同じ 404 を返す(存在秘匿のため 403 にしない)。

AUTH_REQUIRED=true の生成 SPA 配信/能力呼び出しは Bearer を送れないため、app-session Cookie
(ADR-0023 §3.5)を **Bearer と OR で受ける**複合依存を別に持つ。既存の Bearer 専用依存
(require_demo/require_ready_demo/require_demo_owner)は無変更 = crud メタ面の後方互換。
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jetuse_core import app_session, demos
from jetuse_core.auth import AuthContext, require_user, verify_token
from jetuse_core.settings import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)
APP_COOKIE = "app_session"  # Path=/api/demos/{id}/ で束縛(§3.5)


@dataclass
class DemoContext:
    demo_id: str
    owner_sub: str
    namespace: str  # RAG・会話の名前空間キー。将来の DB スキーマ名の元(specs/17 §5)
    status: str  # specs/18 §2.3(SP2-01)。deleting は require_demo が 404 済み
    subject: str = ""  # 認証された呼び出し主体(owner_key 等に使う)
    auth_kind: str = "bearer"  # bearer | cookie | code(§3.5 — owner mutation は bearer のみ)


def _resolve(demo_id: str, subject: str, auth_kind: str) -> DemoContext:
    """認証済み subject に require_demo の可視性/存在秘匿ルールを適用する。"""
    demo = demos.get_demo(demo_id)
    if (
        not demo
        # 解体中の箱への能力呼び出しが lazy 生成で箱を復活させる事故を封じる(specs/18 §2.3)
        or demo["status"] == "deleting"
        or (demo["owner_sub"] != subject and demo["visibility"] != "public")
    ):
        raise HTTPException(status_code=404, detail="demo not found")
    return DemoContext(
        demo_id=demo_id, owner_sub=demo["owner_sub"], namespace=f"demo_{demo_id}",
        status=demo["status"], subject=subject, auth_kind=auth_kind,
    )


def require_demo(
    demo_id: str, user: Annotated[AuthContext, Depends(require_user)]
) -> DemoContext:
    return _resolve(demo_id, user.subject, "bearer")


def require_ready_demo(
    ctx: Annotated[DemoContext, Depends(require_demo)],
) -> DemoContext:
    """能力ルート・app-session 発行の共通依存(specs/19 §8.1 — SP3-01 で一般化)。

    deleting 404 を「ready 以外 404」へ広げる: provisioning/failed の箱への能力呼び出しが
    lazy 生成と競合する余地を構造的に消す。存在秘匿と同じ 404。demos CRUD メタは対象外
    (所有者は非 ready でも status を見られる — 進行表示・再生成・破棄に必要)。
    """
    if ctx.status != "ready":
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx


def require_demo_owner(
    ctx: Annotated[DemoContext, Depends(require_demo)],
    user: Annotated[AuthContext, Depends(require_user)],
) -> DemoContext:
    """書き込み系は所有者のみ。公開デモの非所有者は閲覧・実行(chat/GET)まで
    (usecases の「公開は取得・実行可、編集・削除は所有者のみ」と同じ規則。SP1-03 REV-002)。"""
    if ctx.owner_sub != user.subject:
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx


# --- app-session(ADR-0023 §3.5): Bearer と Cookie を OR で受ける複合依存 ---


def _auth_subject(
    demo_id: str, request: Request,
    creds: HTTPAuthorizationCredentials | None, settings: Settings,
    *, allow_code: bool,
) -> tuple[str, str]:
    """(subject, auth_kind) を返す。Bearer 優先(あれば従来経路と完全同一)、
    無ければ app-session Cookie、配信のみ一回性コード(?c=)。いずれも無ければ 401(fail-closed)。"""
    # Bearer 提示時(または AUTH オフの dev-user)は require_user 本体をそのまま通す = 後方互換。
    if creds is not None or not settings.auth_required:
        return verify_token(creds.credentials if creds else None, settings).subject, "bearer"
    # AUTH=true かつ Bearer 不在: Cookie セッション → 一回性コード(配信のみ)の順に試す。
    tok = request.cookies.get(APP_COOKIE)
    if tok and (sub := app_session.verify_session(tok, demo_id)):
        return sub, "cookie"
    if allow_code and (code := request.query_params.get("c", "")) \
            and (sub := app_session.verify_code(code, demo_id)):
        return sub, "code"
    raise HTTPException(status_code=401, detail="authentication required",
                        headers={"WWW-Authenticate": "Bearer"})


def _app_ctx(
    demo_id: str, request: Request,
    creds: HTTPAuthorizationCredentials | None, settings: Settings,
    *, allow_code: bool,
) -> DemoContext:
    subject, kind = _auth_subject(demo_id, request, creds, settings, allow_code=allow_code)
    ctx = _resolve(demo_id, subject, kind)
    if ctx.status != "ready":  # 能力/配信は ready のみ(require_ready_demo と同一規則)
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx


def require_app_or_user(
    demo_id: str, request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DemoContext:
    """能力ルート(chat/rag/dbchat・閲覧/実行)の認可: Bearer OR Cookie。ready を毎要求再検査。"""
    return _app_ctx(demo_id, request, creds, settings, allow_code=False)


def require_app_delivery(
    demo_id: str, request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DemoContext:
    """/app/ 配信の認可: Bearer OR Cookie OR 一回性コード(?c=)。コード時は route が Cookie 発行。"""
    return _app_ctx(demo_id, request, creds, settings, allow_code=True)


def require_app_owner(
    ctx: Annotated[DemoContext, Depends(require_app_or_user)],
) -> DemoContext:
    """能力面の owner mutation は Bearer(親)のみ。Cookie/コード(生成 SPA)経由は 403(§3.5)。"""
    if ctx.auth_kind != "bearer":
        raise HTTPException(status_code=403, detail="owner action requires bearer auth")
    if ctx.owner_sub != ctx.subject:
        raise HTTPException(status_code=404, detail="demo not found")
    return ctx
