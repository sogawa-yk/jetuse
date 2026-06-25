"""FastAPI 層の統合テスト(HTTP 経由で publish→index→list/get/download)。

TestClient で実 HTTP リクエストを通し、InMemoryObjectStore を保存層に使う。
ドメイン例外→HTTP ステータスの写像も検証する。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import (
    PUBLIC_KEY_ID,
    TOKEN,
    base_manifest,
    public_key_b64,
    sign_manifest,
)

from jetuse_registry.app import create_app
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore


@pytest.fixture
def client(private_key):
    store = InMemoryObjectStore()
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: "acme-corp"})
    app = create_app(RegistryService(store, auth))
    c = TestClient(app)
    # 公開鍵を登録(発行者認証つき)。
    r = c.post(
        "/registry/publishers/keys",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"publicKeyId": PUBLIC_KEY_ID, "publicKey": public_key_b64(private_key)},
    )
    assert r.status_code == 201, r.text
    return c


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_full_http_publish_index_list_get_download(client, private_key):
    manifest = sign_manifest(private_key, base_manifest())
    r = client.post("/registry/plugins", headers=_auth(), json=manifest)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "acme/faq-summarizer"

    # list
    r = client.get("/registry/plugins")
    assert r.status_code == 200
    assert any(p["id"] == "acme/faq-summarizer" for p in r.json()["plugins"])

    # search
    r = client.get("/registry/plugins/search", params={"q": "faq"})
    assert r.status_code == 200
    assert {p["id"] for p in r.json()["plugins"]} == {"acme/faq-summarizer"}

    # get (namespace/name)
    r = client.get("/registry/plugins/acme/faq-summarizer")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["id"] == "acme/faq-summarizer"
    assert body["entry"]["version"] == "1.0.0"

    # download
    r = client.get("/registry/plugins/acme/faq-summarizer/download")
    assert r.status_code == 200
    assert r.headers["X-Plugin-Id"] == "acme/faq-summarizer"
    assert r.headers["X-Plugin-Version"] == "1.0.0"
    assert r.json()["id"] == "acme/faq-summarizer"


def test_http_publish_requires_auth(client, private_key):
    manifest = sign_manifest(private_key, base_manifest())
    # 認証ヘッダ無し → 401。
    r = client.post("/registry/plugins", json=manifest)
    assert r.status_code == 401
    # 未知トークン → 401。
    r = client.post(
        "/registry/plugins",
        headers={"Authorization": "Bearer bogus"},
        json=manifest,
    )
    assert r.status_code == 401


def test_http_publish_rejects_unsigned(client):
    r = client.post("/registry/plugins", headers=_auth(), json=base_manifest())
    assert r.status_code == 422
    assert "無署名" in r.json()["detail"]


def test_http_publish_publisher_mismatch_is_403(client, private_key):
    manifest = sign_manifest(private_key, base_manifest(publisher="evil-corp"))
    r = client.post("/registry/plugins", headers=_auth(), json=manifest)
    assert r.status_code == 403


def test_http_publish_duplicate_is_409(client, private_key):
    manifest = sign_manifest(private_key, base_manifest())
    assert client.post("/registry/plugins", headers=_auth(), json=manifest).status_code == 201
    r = client.post("/registry/plugins", headers=_auth(), json=manifest)
    assert r.status_code == 409


def test_http_get_missing_is_404(client):
    r = client.get("/registry/plugins/acme/nope")
    assert r.status_code == 404


def test_http_two_segment_path_serves_full_plugin_id(client, private_key):
    # PLG-01 の id は厳密に namespace/name(各セグメントは小文字英数＋ハイフン)。ハイフン・数字を
    # 含む id でも 2 セグメントパスで get/download できる(>2 階層の有効 id は存在しない)。
    pid = "my-org-7/faq-bot-v2"
    manifest = sign_manifest(private_key, base_manifest(plugin_id=pid))
    assert client.post("/registry/plugins", headers=_auth(), json=manifest).status_code == 201
    r = client.get(f"/registry/plugins/{pid}")
    assert r.status_code == 200
    assert r.json()["manifest"]["id"] == pid
    r = client.get(f"/registry/plugins/{pid}/download")
    assert r.status_code == 200
    assert r.headers["X-Plugin-Id"] == pid


def test_http_get_publisher_keys(client, private_key):
    # 取込側が署名検証用の公開鍵を無認証で取得できる(publisher は query param)。
    r = client.get("/registry/publishers/keys", params={"publisher": "acme-corp"})
    assert r.status_code == 200
    body = r.json()
    assert body["publisher"] == "acme-corp"
    assert body["keys"] == [
        {"publicKeyId": PUBLIC_KEY_ID, "publicKey": public_key_b64(private_key)}
    ]


def test_http_register_key_change_is_409(client):
    # 同一 publicKeyId で別の鍵に差し替えは 409。
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = public_key_b64(Ed25519PrivateKey.generate())
    r = client.post(
        "/registry/publishers/keys",
        headers=_auth(),
        json={"publicKeyId": PUBLIC_KEY_ID, "publicKey": other},
    )
    assert r.status_code == 409


def test_http_malformed_bearer_is_401(client):
    r = client.post(
        "/registry/plugins",
        headers={"Authorization": "Token abc"},
        json=base_manifest(),
    )
    assert r.status_code == 401
