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
    # 空白のみ(env の混入しがちな値)も未設定扱い(review-3 m001)
    issuer = settings.oidc_issuer.strip()
    audience = settings.oidc_audience.strip()
    jwks_url = settings.oidc_jwks_url.strip()
    if not (issuer and audience and jwks_url):
        # 設定不備で検証を欠いたまま受理しない(fail-closed)。issuer/audience/JWKSの3点必須
        # (欠けると同一IdPの別アプリ用トークンを受理しうる — specs/18 §5.1 / codex review-2 B006)
        raise HTTPException(status_code=500, detail="OIDC is not configured")

    try:
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
        )
    except jwt.PyJWTError as e:
        raise _unauthorized(f"invalid token: {type(e).__name__}") from e
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        # sub はデータ分離キー。欠落/空/非文字列(JWT仕様外)のまま受理しない(specs/18 §5.1)
        raise _unauthorized("token has no sub claim")
    return AuthContext(subject=sub, claims=claims)
