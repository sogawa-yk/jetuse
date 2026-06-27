"""CentralRegistryClient(PLG-04 形状)+ マーケット API の統合テスト。

実 `packages/registry`(jetuse_registry)の RegistryService に InMemoryObjectStore で publish させ、
PLG-04 が生成した本物の `index.json`(objectPath / sha256 / 入れ子 publisherKeys)を
CentralRegistryClient で読む。さらにマーケット API ルート経由で list/detail/install まで通す。
署名は本物の ed25519 で行い、検証経路を素通りさせない。
"""

from __future__ import annotations

import base64
import contextlib

import pytest
import test_plugin_install as tpi  # FakeDB / FakeConn(インメモリ ADB)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore

import service.routes.marketplace as mp
from jetuse_core import agents, usecases
from jetuse_core.plugins import connector_store, scaffold, store
from jetuse_core.plugins.central_registry import CentralRegistryClient
from jetuse_core.plugins.manifest import (
    SCHEMA_VERSION,
    canonical_signing_payload,
    validate_manifest,
)
from jetuse_core.plugins.registry_client import RegistryError
from service.main import app

PUBLISHER = "acme-corp"
TOKEN = "tok-acme"
KEY_ID = "acme-key-1"

client = TestClient(app)


def _manifest(version="1.2.0", plugin_id="acme/faq"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": plugin_id,
        "version": version,
        "kind": "usecase",
        "name": "FAQ要約",
        "description": "FAQを要約する",
        "publisher": PUBLISHER,
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:rag.search"],
        "tags": ["faq"],
        "contributes": {
            "usecase": {
                "fields": [{"name": "text", "type": "textarea"}],
                "template": "要約して: {{text}}",
            }
        },
    }


def _sign(private_key, manifest_dict):
    unsigned = validate_manifest(manifest_dict)
    payload = canonical_signing_payload(unsigned)
    sig = private_key.sign(payload)
    signed = dict(manifest_dict)
    signed["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": KEY_ID,
        "value": base64.b64encode(sig).decode("ascii"),
    }
    return signed


def build_plg04_registry(manifests):
    """実 RegistryService に publish させ、PLG-04 形状の index を持つ store を返す。"""
    objstore = InMemoryObjectStore()
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})
    svc = RegistryService(objstore, auth)
    private_key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
    svc.register_public_key(TOKEN, KEY_ID, pub_b64)
    for md in manifests:
        svc.publish(TOKEN, _sign(private_key, md))
    return objstore, private_key


def client_for(objstore) -> CentralRegistryClient:
    # transport は Object Storage の名前→バイト列。実 PLG-04 index/成果物をそのまま読む。
    return CentralRegistryClient(base_url="mem://reg/", transport=lambda name: objstore.get(name))


# --- クライアント契約(PLG-04 形状を読む) ---------------------------------


def test_client_list_get_download_against_real_plg04_index():
    objstore, _ = build_plg04_registry([_manifest("1.0.0"), _manifest("1.2.0")])
    c = client_for(objstore)

    entries = c.list()
    assert {e["id"] for e in entries} == {"acme/faq"}
    assert {e["version"] for e in entries} == {"1.0.0", "1.2.0"}
    assert all("objectPath" in e and "sha256" in e for e in entries)

    # version 未指定は semver 最新を解決。
    assert c.get("acme/faq")["version"] == "1.2.0"

    # download は sha256 検証 + 構文検証を通った manifest を返す。
    m = c.download("acme/faq", "1.0.0")
    assert m.id == "acme/faq" and m.version == "1.0.0"
    assert m.permissions == ["platform:rag.search"]


def test_client_public_key_lookup_nested():
    objstore, pk = build_plg04_registry([_manifest("1.0.0")])
    c = client_for(objstore)
    raw = c.public_key(KEY_ID)
    assert raw == pk.public_key().public_bytes_raw()
    with pytest.raises(RegistryError):
        c.public_key("no-such-key")


def test_client_download_rejects_sha_mismatch():
    objstore, _ = build_plg04_registry([_manifest("1.0.0")])

    # 成果物だけ改ざんしたトランスポート(index の sha256 と食い違う)。
    def tampered(name):
        raw = objstore.get(name)
        if name.endswith(".json") and name != "index.json":
            return raw + b" "  # 1 バイト付加で内容が変わり sha256 不一致になる
        return raw

    c = CentralRegistryClient(base_url="mem://reg/", transport=tampered)
    with pytest.raises(RegistryError):
        c.download("acme/faq", "1.0.0")


# --- マーケット API → 実 PLG-04 → installer まで通す ------------------------


@pytest.fixture
def fake_db(monkeypatch):
    db = tpi.FakeDB()

    @contextlib.contextmanager
    def fake_connect():
        yield tpi.FakeConn(db)

    monkeypatch.setattr(store, "connect", fake_connect)
    monkeypatch.setattr(usecases, "connect", fake_connect)
    monkeypatch.setattr(agents, "connect", fake_connect)
    # MKT-01: uninstall は全 kind の取込先を出所キーで掃除するため、scaffold / connector_store の
    # DB 接続も同じインメモリ ADB に向ける(L2 kind 表が無くても 0 件で安全に通る)。
    monkeypatch.setattr(scaffold, "connect", fake_connect)
    monkeypatch.setattr(connector_store, "connect", fake_connect)
    return db


def test_marketplace_route_install_uninstall_via_plg04(fake_db, monkeypatch):
    objstore, _ = build_plg04_registry([_manifest("1.2.0")])
    monkeypatch.setattr(mp, "build_client", lambda settings: client_for(objstore))

    # 一覧(PLG-04 形状から合成)。
    body = client.get("/api/marketplace/plugins").json()
    assert body["plugins"][0]["id"] == "acme/faq"
    assert body["plugins"][0]["installed"] is False

    # 詳細(成果物 manifest 全文: permissions / 署名)。
    detail = client.get("/api/marketplace/plugins/acme/faq").json()
    assert detail["permissions"] == ["platform:rag.search"]
    assert detail["signed"] is True

    # install(実 ed25519 検証 + sha256 検証 + 取込)。
    res = client.post("/api/marketplace/install", json={"plugin_id": "acme/faq"})
    assert res.status_code == 200, res.json()
    assert res.json()["kind"] == "usecase"
    assert len(fake_db.usecases) == 1  # ADB に取込定義が出現

    # uninstall(取込者 = dev-user)。
    res = client.post(
        "/api/marketplace/uninstall", json={"plugin_id": "acme/faq", "version": "1.2.0"}
    )
    assert res.status_code == 200
    assert fake_db.usecases == [] and fake_db.installed == []


def test_marketplace_route_install_uninstall_sample_app_via_plg04(fake_db, monkeypatch):
    # MKT-01: sample-app がマーケット API 経由で install→uninstall できる(installable=True)。
    md = tpi._sample_app_manifest()
    objstore, _ = build_plg04_registry([md])
    monkeypatch.setattr(mp, "build_client", lambda settings: client_for(objstore))

    catalog = client.get("/api/marketplace/plugins").json()["plugins"]
    card = next(c for c in catalog if c["id"] == md["id"])
    assert card["kind"] == "sample-app" and card["installable"] is True

    res = client.post("/api/marketplace/install", json={"plugin_id": md["id"]})
    assert res.status_code == 200, res.json()
    assert res.json()["kind"] == "sample-app"
    assert len(fake_db.sample_app_instances) == 1
    assert fake_db.sample_app_instances[0]["plugin_id"] == md["id"]

    res = client.post(
        "/api/marketplace/uninstall",
        json={"plugin_id": md["id"], "version": md["version"]},
    )
    assert res.status_code == 200
    assert fake_db.sample_app_instances == [] and fake_db.installed == []


def test_marketplace_route_install_uninstall_connector_via_plg04(fake_db, monkeypatch):
    # MKT-01: connector がマーケット API 経由で install→uninstall できる(installable=True)。
    md = tpi._connector_manifest()
    objstore, _ = build_plg04_registry([md])
    monkeypatch.setattr(mp, "build_client", lambda settings: client_for(objstore))

    catalog = client.get("/api/marketplace/plugins").json()["plugins"]
    card = next(c for c in catalog if c["id"] == md["id"])
    assert card["kind"] == "connector" and card["installable"] is True

    res = client.post("/api/marketplace/install", json={"plugin_id": md["id"]})
    assert res.status_code == 200, res.json()
    assert res.json()["kind"] == "connector"
    assert len(fake_db.connector_instances) == 1
    assert fake_db.connector_instances[0]["provider"] == "slackish"

    res = client.post(
        "/api/marketplace/uninstall",
        json={"plugin_id": md["id"], "version": md["version"]},
    )
    assert res.status_code == 200
    assert fake_db.connector_instances == [] and fake_db.installed == []
