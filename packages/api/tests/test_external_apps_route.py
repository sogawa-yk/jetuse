"""external-app 起動導線ルート（/api/external-apps）の単体テスト（ASSET-01 / BE-06）。

設定（denpyon_url/issuer/audience）から一覧と SSO ハンドオフ shape を返すこと、未構成は 503、
SSO 写像元クレーム欠落は 422（fail-closed）、**実トークンを返さない**ことを検証する。実 exchange の
実行は人間ゲートのためルートでは行わない（shape のみ）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import service.routes.external_apps as ea
from jetuse_core.settings import Settings, get_settings
from service.main import app

URL = "https://denpyon.example.com/app"
ISSUER = "https://idp.example.com"
AUDIENCE = "https://denpyon.example.com"


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """external-app instance store の DB 参照を既定で空にする（route の DB 依存を遮断）。
    インストール済み instance を試すテストは個別に list_external_apps を差し替える。"""
    monkeypatch.setattr(
        ea.external_app_store, "list_external_apps", lambda app=None, registered_by=None: []
    )


def _configured_settings() -> Settings:
    return Settings(
        denpyon_url=URL,
        denpyon_issuer=ISSUER,
        denpyon_audience=AUDIENCE,
    )


def _client(settings: Settings) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_list_external_apps_configured():
    c = _client(_configured_settings())
    resp = c.get("/api/external-apps")
    assert resp.status_code == 200
    apps = resp.json()["external_apps"]
    assert len(apps) == 1
    assert apps[0]["app"] == "denpyon"
    assert apps[0]["embed"] == "iframe"
    assert apps[0]["sso"] is True
    # 秘密・参照名を一覧に出さない（embed 情報のみ）。
    assert "secretRef" not in json.dumps(apps)
    assert "clientIdRef" not in json.dumps(apps)


def test_list_external_apps_unconfigured_is_empty():
    c = _client(Settings())  # 未構成
    resp = c.get("/api/external-apps")
    assert resp.status_code == 200
    assert resp.json()["external_apps"] == []


def test_sso_launch_unconfigured_503():
    c = _client(Settings())
    resp = c.post(
        "/api/external-apps/denpyon/sso-launch", json={"state": "s", "nonce": "n"}
    )
    assert resp.status_code == 503


def test_sso_launch_unknown_app_404():
    c = _client(_configured_settings())
    resp = c.post("/api/external-apps/unknown/sso-launch", json={"state": "s", "nonce": "n"})
    assert resp.status_code == 404


def test_sso_launch_missing_claims_422():
    """dev（auth_required=false）は sub のみ。denpyon は email/groups も要求し fail-closed。"""
    c = _client(_configured_settings())
    resp = c.post(
        "/api/external-apps/denpyon/sso-launch", json={"state": "s", "nonce": "n"}
    )
    assert resp.status_code == 422


def test_sso_launch_returns_handoff_shape_without_secrets():
    """全クレームが揃えば handoff shape を返す。実トークン・実シークレットは含まない。"""
    from jetuse_core.auth import AuthContext, require_user

    # 認証済み利用者の身元クレームを与える（IdP 検証済み相当。実トークンではない）。
    # Depends(require_user) と同一の関数オブジェクトをキーに override する。
    app.dependency_overrides[require_user] = lambda: AuthContext(
        subject="u-1",
        claims={"sub": "u-1", "email": "u1@example.com", "groups": ["sales"]},
    )
    c = _client(_configured_settings())
    resp = c.post(
        "/api/external-apps/denpyon/sso-launch", json={"state": "st", "nonce": "no"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["app"] == "denpyon"
    assert body["contains_secret_values"] is False
    assert body["mapped_claims"]["preferred_username"] == "u-1"
    txr = body["token_exchange_request"]
    # 参照名のみ（実値・実トークンを含まない）。
    assert txr["client_secret_ref"] == "denpyon-oidc-client-secret"
    assert "client_secret" not in txr
    assert "subject_token" not in txr  # 実 id_token は含めない（参照名のみ）


# --- マーケット install 済み instance との接続（M-004） / 実 exchange ゲート（B-002） ---

_INSTALLED_DEF = {
    "app": "acme-portal",
    "embed": "link",
    "url": "https://portal.example.com/app",
    "title": "Acme Portal",
    "sso": {
        "mode": "oidc",
        "issuer": ISSUER,
        "clientIdRef": "acme-oidc-client-id",
        "secretRef": "acme-oidc-client-secret",
        "audience": "https://portal.example.com",
        "scopes": ["openid", "email"],
        "claimMapping": {"sub": "preferred_username"},
        "tokenEndpoint": "https://idp.example.com/oauth2/token",
    },
}


def test_list_surfaces_installed_instances(monkeypatch):
    """マーケット install 済みの external-app が一覧に出る（source=installed）。"""
    monkeypatch.setattr(
        ea.external_app_store,
        "list_external_apps",
        lambda app=None, registered_by=None: [{"definition": _INSTALLED_DEF}],
    )
    c = _client(Settings())  # builder 未構成。installed のみ。
    apps = c.get("/api/external-apps").json()["external_apps"]
    assert [a["app"] for a in apps] == ["acme-portal"]
    assert apps[0]["source"] == "installed"


def test_sso_launch_installed_instance():
    """install 済み instance に対しても SSO ハンドオフ shape を返せる。"""
    from jetuse_core.auth import AuthContext, require_user

    ea.external_app_store.list_external_apps = (  # type: ignore[assignment]
        lambda app=None, registered_by=None: [{"definition": _INSTALLED_DEF}]
    )
    app.dependency_overrides[require_user] = lambda: AuthContext(
        subject="u-1", claims={"sub": "u-1"}
    )
    try:
        c = _client(Settings())
        resp = c.post(
            "/api/external-apps/acme-portal/sso-launch", json={"state": "s", "nonce": "n"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["app"] == "acme-portal"
    finally:
        app.dependency_overrides.clear()


def _bypass_auth():
    """require_user を上書きし JWT 検証をバイパス（route の auth_required ゲートを試す）。"""
    from jetuse_core.auth import AuthContext, require_user

    app.dependency_overrides[require_user] = lambda: AuthContext(
        subject="u-1", claims={"sub": "u-1"}
    )


def test_sso_exchange_forbidden_when_auth_not_required():
    """AUTH_REQUIRED=false では実 exchange を禁止（未検証 Bearer を実 IdP へ送らない・SEC-001）。"""
    _bypass_auth()
    s = Settings(
        denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
        denpyon_token_endpoint="https://idp.example.com/oauth2/token",
        external_app_secret_ocids="denpyon-oidc-client-secret=ocid1.x,denpyon-oidc-client-id=ocid1.y",
        auth_required=False,
    )
    c = _client(s)
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "s", "nonce": "n"}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 403


def test_sso_exchange_forbidden_for_installed_only_app(monkeypatch):
    """install 済みだけの app は実 exchange の対象外（任意 endpoint を信頼しない・SEC-001）。"""
    _bypass_auth()
    monkeypatch.setattr(
        ea.external_app_store, "list_external_apps",
        lambda app=None, registered_by=None: [{"definition": _INSTALLED_DEF}],
    )
    s = Settings(auth_required=True)  # builder 未構成（denpyon 無し）
    c = _client(s)
    resp = c.post("/api/external-apps/acme-portal/sso-exchange",
                  json={"state": "s", "nonce": "n"}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 403


def test_sso_exchange_fail_closed_without_token_endpoint():
    """builder 構成＋AUTH_REQUIRED だが tokenEndpoint 未構成だと 503（人間ゲート）。"""
    _bypass_auth()
    s = Settings(denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
                 auth_required=True)
    c = _client(s)
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "s", "nonce": "n"}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_sso_exchange_fail_closed_without_vault():
    """tokenEndpoint はあるが Vault secret 未構成だと 503（人間ゲート）。"""
    _bypass_auth()
    s = Settings(
        denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
        denpyon_token_endpoint="https://idp.example.com/oauth2/token", auth_required=True,
    )
    c = _client(s)
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "s", "nonce": "n"}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_sso_exchange_fail_closed_without_jwks():
    """tokenEndpoint＋Vault 構成済みでも発行 id_token 検証(JWKS)未構成だと 503（BE06-R002）。"""
    _bypass_auth()
    s = Settings(
        denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
        denpyon_token_endpoint="https://idp.example.com/oauth2/token", auth_required=True,
        external_app_secret_ocids="denpyon-oidc-client-secret=ocid1.x,denpyon-oidc-client-id=ocid1.y",
    )
    c = _client(s)
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "s", "nonce": "n"}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_sso_exchange_requires_bearer_subject_token():
    """tokenEndpoint＋Vault＋JWKS 構成済みでも利用者の実トークン（Bearer）が無ければ 401。"""
    _bypass_auth()
    s = Settings(
        denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
        denpyon_token_endpoint="https://idp.example.com/oauth2/token", auth_required=True,
        external_app_secret_ocids="denpyon-oidc-client-secret=ocid1.x,denpyon-oidc-client-id=ocid1.y",
        denpyon_jwks_url="https://idp.example.com/jwks",
    )
    c = _client(s)
    resp = c.post("/api/external-apps/denpyon/sso-exchange", json={"state": "s", "nonce": "n"})
    assert resp.status_code == 401


# --- handoff code 引き渡し（認可コード型 / BE06-SSO-002） -------------------

_FULL_SSO = dict(
    denpyon_url=URL, denpyon_issuer=ISSUER, denpyon_audience=AUDIENCE,
    denpyon_token_endpoint="https://idp.example.com/oauth2/token", auth_required=True,
    external_app_secret_ocids="denpyon-oidc-client-secret=ocid1.x,denpyon-oidc-client-id=ocid1.y",
    denpyon_jwks_url="https://idp.example.com/jwks",
)


def _fake_exchange_result():
    return {
        "app": "denpyon", "mode": "oidc", "embed": "iframe", "url": URL,
        "state": "st", "nonce": "no", "mapped_claims": {"preferred_username": "u-1"},
        "issued_token": "ISSUED-ID-TOKEN", "issued_token_type": "urn:ietf:params:oauth:"
        "token-type:id_token",
        # 検証・束ね済みの本人識別子（BE06-BLK-001。route は handoff store の subject に使う）。
        "issued_subject": "u-1",
        "contains_secret_values": True,
    }


def test_sso_exchange_returns_handoff_code_not_id_token(monkeypatch):
    """sso-exchange は **id_token をブラウザに返さず** handoff code を返す（BE06-SSO-002）。"""
    from jetuse_core.plugins import sso_handoff_store

    sso_handoff_store._clear_for_test()
    _bypass_auth()
    # 実 exchange は mock（実 IdP/Vault は人間ゲート）。verifier 生成も差し替える。
    monkeypatch.setattr(ea, "exchange_sso_token", lambda *a, **k: _fake_exchange_result())
    monkeypatch.setattr(ea, "jwks_id_token_verifier", lambda url: (lambda t, i, a: True))
    c = _client(Settings(**_FULL_SSO))
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "st", "nonce": "no"}, headers={"Authorization": "Bearer realtok"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["handoff_code"] and body["contains_secret_values"] is False
    assert "issued_token" not in body and "id_token" not in body  # ブラウザに id_token を返さない
    assert resp.headers.get("Cache-Control") == "no-store"


def test_sso_exchange_passes_expected_subject(monkeypatch):
    """route は認証利用者の subject を expected_subject として渡す（本人束ね。BE06-BLK-001）。"""
    from jetuse_core.plugins import sso_handoff_store

    sso_handoff_store._clear_for_test()
    _bypass_auth()
    captured = {}

    def _capture(*a, **k):
        captured.update(k)
        return _fake_exchange_result()

    monkeypatch.setattr(ea, "exchange_sso_token", _capture)
    monkeypatch.setattr(ea, "jwks_id_token_verifier", lambda url: (lambda t, i, a: {"sub": "u-1"}))
    c = _client(Settings(**_FULL_SSO))
    resp = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "st", "nonce": "no"}, headers={"Authorization": "Bearer realtok"})
    assert resp.status_code == 200, resp.text
    assert captured["expected_subject"] == "u-1"  # 認証利用者に束ねる


def test_sso_redeem_backchannel_single_use(monkeypatch):
    """バックチャネル sso-redeem は client 認証＋単回使用で id_token を返す（BE06-SSO-002）。"""
    from jetuse_core.plugins import sso_handoff_store

    sso_handoff_store._clear_for_test()
    _bypass_auth()
    monkeypatch.setattr(ea, "exchange_sso_token", lambda *a, **k: _fake_exchange_result())
    monkeypatch.setattr(ea, "jwks_id_token_verifier", lambda url: (lambda t, i, a: True))
    # Vault 解決を mock（実 OCID/権限は人間ゲート）。client_id/secret の実値を返す。
    creds = {"denpyon-oidc-client-id": "real-client-id",
             "denpyon-oidc-client-secret": "real-secret"}
    monkeypatch.setattr(ea, "_vault_sso_resolver", lambda s: (lambda ref: creds[ref]))
    c = _client(Settings(**_FULL_SSO))
    code = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "st", "nonce": "no"},
                  headers={"Authorization": "Bearer realtok"}).json()["handoff_code"]

    redeem_body = {"handoff_code": code, "client_id": "real-client-id",
                   "client_secret": "real-secret"}
    r1 = c.post("/api/external-apps/denpyon/sso-redeem", json=redeem_body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["id_token"] == "ISSUED-ID-TOKEN"
    # claimMapping 適用済みクレームも redeem 応答で外部アプリへ渡る（BE06-MAJ-003）。
    assert r1.json()["mapped_claims"] == {"preferred_username": "u-1"}
    assert r1.headers.get("Cache-Control") == "no-store"
    # 2回目は使用済み → 404（単回使用）。
    r2 = c.post("/api/external-apps/denpyon/sso-redeem", json=redeem_body)
    assert r2.status_code == 404


def test_sso_redeem_rejects_wrong_client_secret(monkeypatch):
    """redeem の client 認証（client_secret 不一致）は 401（id_token を出さない）。"""
    from jetuse_core.plugins import sso_handoff_store

    sso_handoff_store._clear_for_test()
    _bypass_auth()
    monkeypatch.setattr(ea, "exchange_sso_token", lambda *a, **k: _fake_exchange_result())
    monkeypatch.setattr(ea, "jwks_id_token_verifier", lambda url: (lambda t, i, a: True))
    creds = {"denpyon-oidc-client-id": "real-client-id",
             "denpyon-oidc-client-secret": "real-secret"}
    monkeypatch.setattr(ea, "_vault_sso_resolver", lambda s: (lambda ref: creds[ref]))
    c = _client(Settings(**_FULL_SSO))
    code = c.post("/api/external-apps/denpyon/sso-exchange",
                  json={"state": "st", "nonce": "no"},
                  headers={"Authorization": "Bearer realtok"}).json()["handoff_code"]
    r = c.post("/api/external-apps/denpyon/sso-redeem",
               json={"handoff_code": code, "client_id": "real-client-id",
                     "client_secret": "WRONG"})
    assert r.status_code == 401
    # code は消費されず残る？ 認証失敗は redeem 前に弾くので、正しい資格情報なら後で交換できる。
    r_ok = c.post("/api/external-apps/denpyon/sso-redeem",
                  json={"handoff_code": code, "client_id": "real-client-id",
                        "client_secret": "real-secret"})
    assert r_ok.status_code == 200


def test_installed_instance_is_platform_wide_visible(monkeypatch):
    """install 済み instance は platform-wide 可視（運用者 install→別利用者も可。BE06-REV-005）。

    install は署名検証済み・運用者ゲートで (plugin_id, version) 全体一意（connector/usecase と同じ
    platform 一貫モデル）。最初の利用者が install した instance を別利用者も一覧・起動できる。
    """
    from jetuse_core.auth import AuthContext, require_user

    # alice が install した instance（platform-wide。list は所有者で絞らない）。
    store: list[dict] = [{"definition": _INSTALLED_DEF, "registered_by": "alice"}]

    def _fake_list(app=None, registered_by=None):
        return [r for r in store if app is None or r["definition"]["app"] == app]

    monkeypatch.setattr(ea.external_app_store, "list_external_apps", _fake_list)

    # 別利用者 bob からも見える・SSO ハンドオフを取得できる。
    app.dependency_overrides[require_user] = lambda: AuthContext(
        subject="bob", claims={"sub": "bob"}
    )
    c = _client(Settings())
    apps = c.get("/api/external-apps").json()["external_apps"]
    assert [a["app"] for a in apps] == ["acme-portal"]
    r = c.post("/api/external-apps/acme-portal/sso-launch", json={"state": "s", "nonce": "n"})
    assert r.status_code == 200, r.text
