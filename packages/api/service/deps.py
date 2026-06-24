"""route 横断のヘルパー(P1c §5)。create_app() のクロージャから service 層へ移設。"""

from fastapi import HTTPException

from jetuse_core.auth import AuthContext
from jetuse_core.settings import get_settings


def is_admin(user: AuthContext) -> bool:
    """ADMIN_USERS(カンマ区切り)に sub または email claim が含まれれば管理者"""
    admins = {a.strip() for a in get_settings().admin_users.split(",") if a.strip()}
    if not admins:
        return False
    email = (user.claims or {}).get("email")
    return user.subject in admins or (bool(email) and email in admins)


def require_speech() -> None:
    if not get_settings().speech_bucket:
        raise HTTPException(
            status_code=503,
            detail="議事録機能は未設定です(SPEECH_BUCKET)。docs/setup/iam.md「VOICE-01」参照",
        )
