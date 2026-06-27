"""MKT-02 μService 拡張の単体テスト(評価/DL 数/版ライフサイクル/DB 検索/後方互換)。

ADB を起こさずに service の意味づけを検証するため、`InMemoryRegistryBackend`(`AdbBackend` と同契約・
可視性規則)を `RegistryService` の backend に注入する。実 ADB 検証は E2E が担う。
list/get/download/publish と ed25519 署名検証が ADB でも従来契約どおり動くことも確認する。
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from helpers import (
    PUBLIC_KEY_ID,
    PUBLISHER,
    TOKEN,
    base_manifest,
    public_key_b64,
    sign_manifest,
)

from jetuse_registry.errors import (
    RegistryForbiddenError,
    RegistryGoneError,
    RegistryNotFoundError,
    RegistryUnsupportedError,
    RegistryValidationError,
)
from jetuse_registry.memory_backend import InMemoryRegistryBackend
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore

PLUGIN_ID = "acme/faq-summarizer"


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def authenticator() -> StaticTokenAuthenticator:
    # 2 発行者: acme(所有者) と他社 rival(なりすまし/権限テスト用)。
    return StaticTokenAuthenticator.from_token_map(
        {TOKEN: PUBLISHER, "rival-token": "rival-corp"}
    )


@pytest.fixture
def svc(authenticator) -> RegistryService:
    """ADB バックエンド相当(全機能)の service。"""
    return RegistryService(authenticator=authenticator, backend=InMemoryRegistryBackend())


@pytest.fixture
def published(svc, private_key) -> RegistryService:
    """鍵登録＋1 版 publish 済みの service。"""
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    return svc


# --- 後方互換: list/get/download/publish が ADB バックエンドでも同契約 ---

def test_publish_list_get_download_roundtrip(published):
    plugins = published.list_plugins()
    assert len(plugins) == 1
    assert plugins[0]["id"] == PLUGIN_ID
    got = published.get(PLUGIN_ID)
    assert got["entry"]["version"] == "1.0.0"
    assert got["manifest"]["id"] == PLUGIN_ID
    data, entry = published.download(PLUGIN_ID, "1.0.0")
    assert entry.sha256 == got["entry"]["sha256"]
    assert b"faq-summarizer" in data


def test_unsigned_publish_rejected(svc, private_key):
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    with pytest.raises(RegistryValidationError):
        svc.publish(TOKEN, base_manifest(version="1.0.0"))  # 署名なし


def test_tampered_manifest_rejected(svc, private_key):
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    signed = sign_manifest(private_key, base_manifest(version="1.0.0"))
    signed["name"] = "改ざん後の名前"  # 署名対象を改変 → 検証失敗
    with pytest.raises(RegistryValidationError):
        svc.publish(TOKEN, signed)


def test_republish_same_version_conflict(published, private_key):
    from jetuse_registry.errors import RegistryConflictError

    with pytest.raises(RegistryConflictError):
        published.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))


# --- DL 数 ---

def test_download_increments_count(published):
    _, e1 = published.download(PLUGIN_ID, "1.0.0")
    assert e1.download_count == 1
    _, e2 = published.download(PLUGIN_ID, "1.0.0")
    assert e2.download_count == 2
    # list にも反映される。
    assert published.list_plugins()[0]["downloadCount"] == 2


# --- 評価 ---

def test_rating_aggregate_and_upsert(published):
    published.rate_plugin(TOKEN, PLUGIN_ID, 4, "便利")
    summary = published.rate_plugin("rival-token", PLUGIN_ID, 2, "微妙")
    assert summary["count"] == 2
    assert summary["average"] == 3.0
    # 同 rater は upsert(1 件のまま、平均更新)。
    summary = published.rate_plugin(TOKEN, PLUGIN_ID, 5)
    assert summary["count"] == 2
    assert summary["average"] == 3.5


def test_rating_invalid_score(published):
    for bad in (0, 6, -1):
        with pytest.raises(RegistryValidationError):
            published.rate_plugin(TOKEN, PLUGIN_ID, bad)


def test_rating_unknown_plugin(published):
    with pytest.raises(RegistryNotFoundError):
        published.rate_plugin(TOKEN, "acme/does-not-exist", 5)


def test_get_ratings_empty(published):
    summary = published.get_ratings(PLUGIN_ID)
    assert summary["count"] == 0
    assert summary["average"] is None


def test_get_ratings_unknown_plugin_404(published):
    # F-002: 存在しない plugin は 404(無評価の既存 plugin と区別)。
    with pytest.raises(RegistryNotFoundError):
        published.get_ratings("acme/does-not-exist")


def test_rating_comment_too_long(published):
    from jetuse_registry.backend import MAX_COMMENT_LEN

    with pytest.raises(RegistryValidationError):
        published.rate_plugin(TOKEN, PLUGIN_ID, 5, "あ" * (MAX_COMMENT_LEN + 1))


# --- F-001: 保存層カラム幅を超える入力は publish 時に 422 ---

def test_publish_oversized_fields_rejected(svc, private_key):
    # ADB バックエンド相当(InMemory)は列幅超過を 422 に倒す。
    from jetuse_registry.backend import MAX_DESCRIPTION_LEN, MAX_NAME_LEN

    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    long_name = base_manifest(version="1.0.0", name="N" * (MAX_NAME_LEN + 1))
    with pytest.raises(RegistryValidationError):
        svc.publish(TOKEN, sign_manifest(private_key, long_name))
    long_desc = base_manifest(version="1.0.0", description="D" * (MAX_DESCRIPTION_LEN + 1))
    with pytest.raises(RegistryValidationError):
        svc.publish(TOKEN, sign_manifest(private_key, long_desc))


def test_publish_multibyte_name_byte_limit(svc, private_key):
    # F-001: 文字数は上限以内でも UTF-8 バイト数が列幅超過なら 422(byte セマンティクス)。
    from jetuse_registry.backend import MAX_NAME_LEN

    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    # 「あ」=3 bytes。文字数 = MAX_NAME_LEN/2(上限以内)だが byte 数は約 1.5×上限。
    chars = MAX_NAME_LEN // 2
    assert chars <= MAX_NAME_LEN  # 文字数では通る
    mb_name = base_manifest(version="1.0.0", name="あ" * chars)
    with pytest.raises(RegistryValidationError):
        svc.publish(TOKEN, sign_manifest(private_key, mb_name))


def test_oversized_publisher_rejected(private_key):
    # F-002(review-3): 認証発行者名が VARCHAR2(255) を超えると 422(500 ではない)。
    from jetuse_registry.backend import MAX_PUBLISHER_LEN

    long_pub = "p" * (MAX_PUBLISHER_LEN + 1)
    auth = StaticTokenAuthenticator.from_token_map({"long-pub-token": long_pub})
    svc = RegistryService(authenticator=auth, backend=InMemoryRegistryBackend())
    with pytest.raises(RegistryValidationError):
        svc.register_public_key("long-pub-token", PUBLIC_KEY_ID, public_key_b64(private_key))


def test_legacy_backend_accepts_long_fields(authenticator, private_key):
    # F-002(後方互換): レガシー index.json backend は ADB 列幅を課さず長い name も従来どおり受理。
    from jetuse_registry.backend import MAX_NAME_LEN

    legacy = RegistryService(InMemoryObjectStore(), authenticator)
    legacy.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    long_name = base_manifest(version="1.0.0", name="N" * (MAX_NAME_LEN + 50))
    entry = legacy.publish(TOKEN, sign_manifest(private_key, long_name))
    assert entry["version"] == "1.0.0"
    assert len(legacy.get(PLUGIN_ID)["entry"]["name"]) == MAX_NAME_LEN + 50


# --- 版ライフサイクル ---

def test_latest_prefers_active_over_deprecated(svc, private_key):
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="2.0.0")))
    # 2.0.0 を deprecated にすると latest は 1.0.0 へ戻る。
    svc.set_lifecycle(TOKEN, PLUGIN_ID, "2.0.0", "deprecated")
    assert svc.get(PLUGIN_ID)["entry"]["version"] == "1.0.0"
    # 全て deprecated のときは deprecated の中の最新へフォールバック。
    svc.set_lifecycle(TOKEN, PLUGIN_ID, "1.0.0", "deprecated")
    assert svc.get(PLUGIN_ID)["entry"]["version"] == "2.0.0"


def test_yanked_excluded_and_gone(svc, private_key):
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="2.0.0")))
    svc.set_lifecycle(TOKEN, PLUGIN_ID, "2.0.0", "yanked")
    # list/search から消える(既定)。
    versions = {p["version"] for p in svc.list_plugins()}
    assert versions == {"1.0.0"}
    assert {p["version"] for p in svc.search()} == {"1.0.0"}
    # include_yanked=True なら見える(監査用)。
    assert "2.0.0" in {p["version"] for p in svc.list_plugins(include_yanked=True)}
    # latest は yanked を選ばない。
    assert svc.get(PLUGIN_ID)["entry"]["version"] == "1.0.0"
    # 明示取得は 410。
    with pytest.raises(RegistryGoneError):
        svc.get(PLUGIN_ID, "2.0.0")
    with pytest.raises(RegistryGoneError):
        svc.download(PLUGIN_ID, "2.0.0")


def test_all_yanked_latest_not_found(published):
    published.set_lifecycle(TOKEN, PLUGIN_ID, "1.0.0", "yanked")
    with pytest.raises(RegistryNotFoundError):
        published.get(PLUGIN_ID)


def test_lifecycle_requires_owner(published):
    with pytest.raises(RegistryForbiddenError):
        published.set_lifecycle("rival-token", PLUGIN_ID, "1.0.0", "deprecated")


def test_lifecycle_invalid_state(published):
    with pytest.raises(RegistryValidationError):
        published.set_lifecycle(TOKEN, PLUGIN_ID, "1.0.0", "archived")


def test_lifecycle_unknown_version(published):
    with pytest.raises(RegistryNotFoundError):
        published.set_lifecycle(TOKEN, PLUGIN_ID, "9.9.9", "deprecated")


# --- DB 検索(意味づけ) ---

def test_search_by_kind_and_query(svc, private_key):
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    svc.publish(
        TOKEN,
        sign_manifest(
            private_key,
            base_manifest(
                plugin_id="acme/sales-agent", version="1.0.0", kind="agent",
                name="営業エージェント", description="営業支援", tags=["sales"],
            ),
        ),
    )
    assert {p["id"] for p in svc.search(kind="agent")} == {"acme/sales-agent"}
    assert {p["id"] for p in svc.search("faq")} == {PLUGIN_ID}
    assert {p["id"] for p in svc.search(tag="sales")} == {"acme/sales-agent"}


# --- レガシー index バックエンドは拡張未対応 ---

def test_index_backend_rejects_extensions(authenticator, private_key):
    legacy = RegistryService(InMemoryObjectStore(), authenticator)
    legacy.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    legacy.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    with pytest.raises(RegistryUnsupportedError):
        legacy.rate_plugin(TOKEN, PLUGIN_ID, 5)
    with pytest.raises(RegistryUnsupportedError):
        legacy.get_ratings(PLUGIN_ID)
    with pytest.raises(RegistryUnsupportedError):
        legacy.set_lifecycle(TOKEN, PLUGIN_ID, "1.0.0", "deprecated")
    # download は no-op カウント(後方互換: 0 据置・例外なし)。
    _, entry = legacy.download(PLUGIN_ID, "1.0.0")
    assert entry.download_count == 0
