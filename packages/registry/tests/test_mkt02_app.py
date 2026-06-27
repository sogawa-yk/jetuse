"""MKT-02 の HTTP 層テスト(新規ルート: ratings / lifecycle、410/501/ヘッダ写像)。

ADB を起こさず `InMemoryRegistryBackend` を注入した `create_app` を `TestClient` で叩く。
ドメイン例外→HTTP ステータスの写像(401/403/404/410/422/501)と DL 数ヘッダを検証する。
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from helpers import PUBLIC_KEY_ID, PUBLISHER, TOKEN, base_manifest, public_key_b64, sign_manifest

from jetuse_registry.app import create_app
from jetuse_registry.memory_backend import InMemoryRegistryBackend
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore

PLUGIN_ID = "acme/faq-summarizer"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def client(private_key) -> TestClient:
    auth = StaticTokenAuthenticator.from_token_map(
        {TOKEN: PUBLISHER, "rival-token": "rival-corp"}
    )
    svc = RegistryService(authenticator=auth, backend=InMemoryRegistryBackend())
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    return TestClient(create_app(svc))


def test_download_count_header(client):
    r = client.get(f"/registry/plugins/{PLUGIN_ID}/download", params={"version": "1.0.0"})
    assert r.status_code == 200
    assert r.headers["X-Plugin-Download-Count"] == "1"
    r2 = client.get(f"/registry/plugins/{PLUGIN_ID}/download", params={"version": "1.0.0"})
    assert r2.headers["X-Plugin-Download-Count"] == "2"


def test_rating_post_and_get(client):
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": 5, "comment": "good"}, headers=AUTH
    )
    assert r.status_code == 201
    body = r.json()
    assert body["count"] == 1 and body["average"] == 5.0
    g = client.get(f"/registry/plugins/{PLUGIN_ID}/ratings")
    assert g.status_code == 200
    assert g.json()["count"] == 1


def test_get_ratings_unknown_plugin_404(client):
    r = client.get("/registry/plugins/acme/nope/ratings")
    assert r.status_code == 404


def test_rating_requires_auth(client):
    r = client.post(f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": 5})
    assert r.status_code == 401


def test_rating_invalid_score_422(client):
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": 9}, headers=AUTH
    )
    assert r.status_code == 422


def test_rating_bool_score_rejected_422(client):
    # F-003(review-3): JSON の true が 1 に強制されて星1評価にならないこと(StrictInt で 422)。
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": True}, headers=AUTH
    )
    assert r.status_code == 422
    # 文字列の score も拒否。
    r2 = client.post(
        f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": "5"}, headers=AUTH
    )
    assert r2.status_code == 422


def test_lifecycle_yank_then_410(client):
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/lifecycle",
        json={"version": "1.0.0", "state": "yanked"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["lifecycle"] == "yanked"
    # list から消える。
    assert client.get("/registry/plugins").json()["plugins"] == []
    # 明示取得は 410。
    get_r = client.get(f"/registry/plugins/{PLUGIN_ID}", params={"version": "1.0.0"})
    assert get_r.status_code == 410
    dl = client.get(f"/registry/plugins/{PLUGIN_ID}/download", params={"version": "1.0.0"})
    assert dl.status_code == 410


def test_lifecycle_not_owner_403(client):
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/lifecycle",
        json={"version": "1.0.0", "state": "deprecated"},
        headers={"Authorization": "Bearer rival-token"},
    )
    assert r.status_code == 403


def test_index_backend_extension_501():
    """レガシー index バックエンドの app では拡張ルートが 501。"""
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})
    pk = Ed25519PrivateKey.generate()
    svc = RegistryService(InMemoryObjectStore(), auth)
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(pk))
    svc.publish(TOKEN, sign_manifest(pk, base_manifest(version="1.0.0")))
    client = TestClient(create_app(svc))
    r = client.post(
        f"/registry/plugins/{PLUGIN_ID}/ratings", json={"score": 5}, headers=AUTH
    )
    assert r.status_code == 501
    g = client.get(f"/registry/plugins/{PLUGIN_ID}/ratings")
    assert g.status_code == 501
