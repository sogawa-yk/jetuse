"""PLG-05 公開フロー(export→署名→publish)の単体テスト。

manifest 化(usecase/agent)・id スラッグ化・ed25519 署名(PLG-01 の検証経路を本物で通す)・
HTTP publish クライアント(in-process の PLG-04 レジストリ app を transport で叩く)を検証する。
DB には触れない(定義 dict を直接渡す)。実バケットは使わない(InMemoryObjectStore)。
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from jetuse_registry.app import create_app
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore

from jetuse_core.plugins.manifest import validate_manifest, verify_signature
from jetuse_core.plugins.publisher import (
    PublisherConfig,
    PublisherConfigError,
    PublishError,
    RegistryPublishClient,
    build_plugin_id,
    build_signed_manifest,
    manifest_from_agent,
    manifest_from_usecase,
    publish_definition,
    sign_manifest,
    slugify_segment,
)

PUBLISHER = "plg05-e2e"
TOKEN = "unit-token"
PUBLIC_KEY_ID = "plg05-key-1"


# --- フィクスチャ ----------------------------------------------------------


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    # 決定的にしたいわけではないので生成で十分(署名往復を本物で通す)。
    return Ed25519PrivateKey.generate()


@pytest.fixture
def signing_key_b64(private_key) -> str:
    return base64.b64encode(private_key.private_bytes_raw()).decode("ascii")


@pytest.fixture
def config(signing_key_b64) -> PublisherConfig:
    return PublisherConfig(
        publisher=PUBLISHER,
        public_key_id=PUBLIC_KEY_ID,
        signing_key_b64=signing_key_b64,
        token=TOKEN,
        registry_url="http://reg.test",
        namespace="plg05-e2e",
    )


@pytest.fixture
def registry_client(config):
    """in-process の PLG-04 レジストリ app を transport にした publish クライアント。"""
    store = InMemoryObjectStore()
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})
    app = create_app(RegistryService(store, auth))
    tc = TestClient(app)
    base = config.registry_url

    def transport(method, url, body, headers):
        path = url[len(base):] if url.startswith(base) else url
        resp = tc.request(method, path, json=body, headers=headers)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text

    return RegistryPublishClient(base, TOKEN, transport=transport), tc


# --- 定義サンプル ----------------------------------------------------------

UC_DEF = {
    "id": "uc-123",
    "name": "FAQ 要約",
    "description": "FAQ を要約する",
    "icon": "📄",
    "tags": ["faq", "summary"],
    "model": "gpt-oss-120b",
    "visibility": "private",
    "fields": [{"name": "text", "label": "本文", "type": "textarea", "required": True}],
    "template": "次を要約: {{text}}",
}

AGENT_DEF = {
    "id": "ag-456",
    "name": "営業支援エージェント",
    "description": "案件を支援",
    "icon": "🤖",
    "tags": ["sales"],
    "instructions": "あなたは営業支援担当です",
    "model": "gpt-oss-120b",
    "enabled_tools": ["web_search"],
    "framework": "openai_agents",
    "auto_tools": True,
}


# --- slugify / id ----------------------------------------------------------


def test_slugify_basic():
    assert slugify_segment("FAQ Summary", "fb") == "faq-summary"
    assert slugify_segment("  Hello, World!  ", "fb") == "hello-world"


def test_slugify_japanese_only_falls_back():
    # 英数字が残らない名前はフォールバックを使う。
    assert slugify_segment("営業支援", "agent-ag456") == "agent-ag456"


def test_build_plugin_id_uses_name_then_fallback():
    assert build_plugin_id("plg05-e2e", "FAQ 要約 Bot", entity_id="uc-1", kind="usecase") == (
        "plg05-e2e/faq-bot"
    )
    # 日本語のみ → kind+entity 先頭8 のフォールバック。
    pid = build_plugin_id("plg05-e2e", "営業支援", entity_id="ag-456789ab", kind="agent")
    assert pid == "plg05-e2e/agent-ag456789"


# --- manifest 化 -----------------------------------------------------------


def test_manifest_from_usecase_is_valid_and_roundtrips():
    m = manifest_from_usecase(
        UC_DEF, version="1.2.0", publisher=PUBLISHER, namespace="plg05-e2e",
        public_key_id=PUBLIC_KEY_ID,
    )
    manifest = validate_manifest(m)  # PLG-01 の検証を通る。
    assert manifest.kind == "usecase"
    assert manifest.id == "plg05-e2e/faq"
    assert manifest.version == "1.2.0"
    assert manifest.publisher == PUBLISHER
    # contributes.usecase は取込側がそのまま定義として読める(fields/template/model)。
    uc = manifest.contributes["usecase"]
    assert uc["template"] == "次を要約: {{text}}"
    assert uc["fields"][0]["name"] == "text"
    assert uc["model"] == "gpt-oss-120b"
    # 表示メタはトップレベル。
    assert manifest.name == "FAQ 要約"
    assert manifest.icon == "📄"
    assert set(manifest.tags) == {"faq", "summary"}


def test_manifest_from_agent_is_valid():
    m = manifest_from_agent(
        AGENT_DEF, version="0.1.0", publisher=PUBLISHER, namespace="plg05-e2e",
        public_key_id=PUBLIC_KEY_ID,
    )
    manifest = validate_manifest(m)
    assert manifest.kind == "agent"
    ag = manifest.contributes["agent"]
    assert ag["instructions"] == "あなたは営業支援担当です"
    assert ag["model"] == "gpt-oss-120b"
    assert ag["enabled_tools"] == ["web_search"]
    assert ag["framework"] == "openai_agents"


def test_invalid_version_rejected_at_manifest():
    from jetuse_core.plugins.manifest import ManifestError

    m = manifest_from_usecase(
        UC_DEF, version="not-semver", publisher=PUBLISHER, namespace="plg05-e2e",
        public_key_id=PUBLIC_KEY_ID,
    )
    with pytest.raises(ManifestError):
        validate_manifest(m)


# --- 署名 ------------------------------------------------------------------


def test_sign_manifest_roundtrips_with_verify(private_key):
    m = manifest_from_usecase(
        UC_DEF, version="1.0.0", publisher=PUBLISHER, namespace="plg05-e2e",
        public_key_id=PUBLIC_KEY_ID,
    )
    signed = sign_manifest(m, private_key, PUBLIC_KEY_ID)
    manifest = validate_manifest(signed)
    pub = private_key.public_key().public_bytes_raw()
    assert verify_signature(manifest, pub) is True
    # 別鍵では検証失敗(fail-closed)。
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    assert verify_signature(manifest, other) is False


def test_build_signed_manifest_uses_config(config, private_key):
    signed = build_signed_manifest(
        config, kind="usecase", definition=UC_DEF, version="2.0.0", entity_id="uc-123"
    )
    manifest = validate_manifest(signed)
    assert manifest.publisher == PUBLISHER
    assert manifest.signature.public_key_id == PUBLIC_KEY_ID
    pub = private_key.public_key().public_bytes_raw()
    assert verify_signature(manifest, pub) is True


# --- 設定検証 --------------------------------------------------------------


def test_require_complete_lists_missing():
    cfg = PublisherConfig(
        publisher="", public_key_id="", signing_key_b64="", token="", registry_url=""
    )
    with pytest.raises(PublisherConfigError) as ei:
        cfg.require_complete()
    msg = str(ei.value)
    assert "registry_publisher_id" in msg and "registry_publish_url" in msg


def test_bad_signing_key_rejected():
    cfg = PublisherConfig(
        publisher=PUBLISHER, public_key_id=PUBLIC_KEY_ID, signing_key_b64="not-base64!",
        token=TOKEN, registry_url="http://reg",
    )
    with pytest.raises(PublisherConfigError):
        cfg.private_key()


def test_from_settings_reads_fields():
    class S:
        registry_publisher_id = "pubX"
        registry_public_key_id = "kX"
        registry_signing_key = "sX"
        registry_publisher_token = "tX"
        registry_publish_url = "http://r"
        registry_namespace = ""
        registry_min_version = ""

    cfg = PublisherConfig.from_settings(S())
    assert cfg.publisher == "pubX"
    assert cfg.id_namespace == "pubX"  # namespace 空 → publisher
    assert cfg.min_version == "0.3.0"  # 空 → 既定


# --- publish クライアント(in-process レジストリ) ---------------------------


def test_publish_definition_end_to_end_then_list(config, registry_client):
    client, _tc = registry_client
    result = publish_definition(
        kind="usecase", definition=UC_DEF, version="1.0.0", entity_id="uc-123",
        config=config, client=client,
    )
    assert result["id"] == "plg05-e2e/faq"
    assert result["version"] == "1.0.0"
    # publish 後、レジストリの list に出現する(受け入れ条件: builder→公開→list 出現)。
    plugins = client.list_plugins()
    ids = {(p["id"], p["version"]) for p in plugins}
    assert ("plg05-e2e/faq", "1.0.0") in ids


def test_publish_agent_appears_in_list(config, registry_client):
    client, _tc = registry_client
    result = publish_definition(
        kind="agent", definition=AGENT_DEF, version="0.1.0", entity_id="ag-456",
        config=config, client=client,
    )
    plugins = client.list_plugins()
    assert (result["id"], "0.1.0") in {(p["id"], p["version"]) for p in plugins}


def test_duplicate_version_conflicts(config, registry_client):
    client, _tc = registry_client
    publish_definition(
        kind="usecase", definition=UC_DEF, version="1.0.0", entity_id="uc-123",
        config=config, client=client,
    )
    with pytest.raises(PublishError) as ei:
        publish_definition(
            kind="usecase", definition=UC_DEF, version="1.0.0", entity_id="uc-123",
            config=config, client=client,
        )
    assert ei.value.status == 409  # 版は不変。


def test_wrong_token_is_unauthorized(config, registry_client):
    _client, tc = registry_client
    bad = RegistryPublishClient(
        config.registry_url, "wrong-token",
        transport=lambda m, u, b, h: _proxy(tc, config.registry_url, m, u, b, h),
    )
    with pytest.raises(PublishError) as ei:
        publish_definition(
            kind="usecase", definition=UC_DEF, version="1.0.0", entity_id="uc-123",
            config=config, client=bad,
        )
    assert ei.value.status == 401


def _proxy(tc, base, method, url, body, headers):
    path = url[len(base):] if url.startswith(base) else url
    resp = tc.request(method, path, json=body, headers=headers)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text
