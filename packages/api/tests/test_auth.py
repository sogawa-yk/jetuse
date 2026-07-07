"""auth.verify_token の fail-closed テスト(SP2-04 / specs/18 §5.1 — codex review-2 B006)。

AUTH_REQUIRED=true では issuer / audience / JWKS URL の3点全てが必須(不備は500)、
検証後の sub 欠落/空は401。署名はローカル RS256 鍵で行い、JWKS 取得だけ差し替える
(署名・iss/aud 検証・sub 抽出は本物の経路)。
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from jetuse_core import auth
from jetuse_core.settings import Settings

ISS = "https://idp.example.test/"
AUD = "https://api.example.test/"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(monkeypatch, rsa_key):
    class _FakeJWKSClient:
        def get_signing_key_from_jwt(self, token):
            class _Key:
                key = rsa_key.public_key()

            return _Key()

    monkeypatch.setattr(auth, "_jwks_client", lambda url: _FakeJWKSClient())
    return Settings(
        auth_required=True,
        oidc_issuer=ISS,
        oidc_audience=AUD,
        oidc_jwks_url="https://idp.example.test/admin/v1/SigningCert/jwk",
    )


def make_token(rsa_key, **overrides):
    claims = {
        "sub": "user-a@example.test",
        "iss": ISS,
        "aud": AUD,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, rsa_key, algorithm="RS256")


def test_valid_token_returns_subject(settings, rsa_key):
    ctx = auth.verify_token(make_token(rsa_key), settings)
    assert ctx.subject == "user-a@example.test"
    assert ctx.claims["iss"] == ISS


@pytest.mark.parametrize("missing", ["oidc_issuer", "oidc_audience", "oidc_jwks_url"])
@pytest.mark.parametrize("blank", ["", "   "])  # 空白のみも未設定扱い(review-3 m001)
def test_incomplete_oidc_config_is_500(settings, rsa_key, missing, blank):
    # 設定不備で検証を欠いたまま受理しない(同一IdPの別アプリ用トークン受理を防ぐ)
    broken = settings.model_copy(update={missing: blank})
    with pytest.raises(HTTPException) as ei:
        auth.verify_token(make_token(rsa_key), broken)
    assert ei.value.status_code == 500


def test_wrong_issuer_is_401(settings, rsa_key):
    with pytest.raises(HTTPException) as ei:
        auth.verify_token(make_token(rsa_key, iss="https://evil.example.test/"), settings)
    assert ei.value.status_code == 401


def test_wrong_audience_is_401(settings, rsa_key):
    with pytest.raises(HTTPException) as ei:
        auth.verify_token(make_token(rsa_key, aud="https://other-app.example.test/"), settings)
    assert ei.value.status_code == 401


@pytest.mark.parametrize("sub", [None, "", 123, True, ["a"], {"v": "a"}])
def test_missing_or_non_string_sub_is_401(settings, rsa_key, sub):
    # 欠落/空に加え非文字列 sub も401(123が"123"へ正規化されるのを防ぐ — review-1 m001)
    with pytest.raises(HTTPException) as ei:
        auth.verify_token(make_token(rsa_key, sub=sub), settings)
    assert ei.value.status_code == 401
