"""OIDC JWT検証(IAM Identity Domain — INFRA-02でissuer/JWKS確定)。

AUTH_REQUIRED=false(既定)の間は認証不要で dev-user を返す暫定動作。
署名検証の緩和(オプション無効化等)は行わない方針。
"""

from dataclasses import dataclass, field
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .settings import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)
_jwks_clients: dict[str, PyJWKClient] = {}


@dataclass
class AuthContext:
    subject: str
    claims: dict[str, Any] = field(default_factory=dict)


def _jwks_client(url: str) -> PyJWKClient:
    if url not in _jwks_clients:
        _jwks_clients[url] = PyJWKClient(url, cache_keys=True)
    return _jwks_clients[url]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthContext:
    return verify_token(creds.credentials if creds else None, settings)


def verify_token(token: str | None, settings: Settings) -> AuthContext:
    """Bearerトークン検証の本体。FastAPI(require_user)とFnルーター(ARCH-02)で共用"""
    if not settings.auth_required:
        return AuthContext(subject="dev-user")

    if token is None:
        raise _unauthorized("missing bearer token")
    if not settings.oidc_jwks_url:
        # 設定不備で認証を素通りさせない
        raise HTTPException(status_code=500, detail="OIDC is not configured")

    try:
        key = _jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=settings.oidc_audience or None,
            issuer=settings.oidc_issuer or None,
            options={"verify_aud": bool(settings.oidc_audience)},
        )
    except jwt.PyJWTError as e:
        raise _unauthorized(f"invalid token: {type(e).__name__}") from e
    return AuthContext(subject=str(claims.get("sub", "")), claims=claims)
