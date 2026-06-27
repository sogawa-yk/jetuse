"""RegistryService のドメインテスト(publish/署名検証/index/読取)。

実 Object Storage は使わず InMemoryObjectStore で検証する(PLG-04 受け入れ条件)。
"""

from __future__ import annotations

import base64
import json

import pytest
from helpers import (
    PUBLIC_KEY_ID,
    PUBLISHER,
    TOKEN,
    base_manifest,
    public_key_b64,
    sign_manifest,
)

from jetuse_registry.errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryForbiddenError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryValidationError,
)
from jetuse_registry.index import INDEX_OBJECT_NAME, RegistryIndex

# --- 公開鍵登録 ---------------------------------------------------------------


def test_register_public_key_persists_in_index(service, store, private_key):
    out = service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    assert out["publisher"] == PUBLISHER
    assert out["publicKeyId"] == PUBLIC_KEY_ID
    assert out["publicKeyLength"] == 32
    index = RegistryIndex.from_bytes(store.get(INDEX_OBJECT_NAME))
    assert index.get_public_key(PUBLISHER, PUBLIC_KEY_ID) is not None


def test_register_public_key_rejects_unknown_token(service, private_key):
    with pytest.raises(RegistryAuthError):
        service.register_public_key("bogus", PUBLIC_KEY_ID, public_key_b64(private_key))


def test_register_public_key_rejects_bad_key_length(service):
    short = base64.b64encode(b"\x01" * 16).decode("ascii")
    with pytest.raises(RegistryValidationError):
        service.register_public_key(TOKEN, PUBLIC_KEY_ID, short)


def test_register_public_key_rejects_non_base64(service):
    with pytest.raises(RegistryValidationError):
        service.register_public_key(TOKEN, PUBLIC_KEY_ID, "not base64!!!")


def test_register_public_key_idempotent_same_key(service, private_key):
    # 同一 (publisher, publicKeyId, key) の再登録は冪等(成功)。
    pk = public_key_b64(private_key)
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, pk)
    out = service.register_public_key(TOKEN, PUBLIC_KEY_ID, pk)
    assert out["publicKeyId"] == PUBLIC_KEY_ID


def test_get_publisher_keys_returns_registered_keys(service, private_key):
    # 取込側が署名検証に使う公開鍵を取得できる(無認証の read 経路)。
    pk = public_key_b64(private_key)
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, pk)
    keys = service.get_publisher_keys(PUBLISHER)
    assert keys == [{"publicKeyId": PUBLIC_KEY_ID, "publicKey": pk}]
    # 未知 publisher は空リスト。
    assert service.get_publisher_keys("nobody") == []


def test_register_public_key_rejects_key_change(service, private_key):
    # 同一 publicKeyId で別の鍵に差し替えは不変違反として拒否(過去 publish の検証可能性保護)。
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    other = Ed25519PrivateKey.generate()
    with pytest.raises(RegistryConflictError, match="差し替え"):
        service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(other))


# --- publish 成功 → index 更新 → 読取 -----------------------------------------


def test_publish_then_index_then_list_get_download(registered_service, store, private_key):
    """受け入れ条件の中核: publish→index更新→list/get/download が一貫する。"""
    manifest = sign_manifest(private_key, base_manifest())
    entry = registered_service.publish(TOKEN, manifest)
    assert entry["id"] == "acme/faq-summarizer"
    assert entry["version"] == "1.0.0"
    assert entry["publisher"] == PUBLISHER
    assert len(entry["sha256"]) == 64
    # 成果物パスは content-addressed(sha 入り)。
    assert entry["objectPath"] == f"plugins/acme/faq-summarizer/1.0.0/{entry['sha256']}.json"

    # index.json が更新されている。
    index = RegistryIndex.from_bytes(store.get(INDEX_OBJECT_NAME))
    assert index.find("acme/faq-summarizer", "1.0.0") is not None

    # list に出る。
    listed = registered_service.list_plugins()
    assert any(p["id"] == "acme/faq-summarizer" for p in listed)

    # get で manifest 全文が取れる。
    got = registered_service.get("acme/faq-summarizer")
    assert got["manifest"]["id"] == "acme/faq-summarizer"
    assert got["manifest"]["signature"]["algorithm"] == "ed25519"
    assert got["entry"]["version"] == "1.0.0"

    # download で成果物バイト列が取れ、sha256 が index と一致する。
    data, dl_entry = registered_service.download("acme/faq-summarizer", "1.0.0")
    import hashlib

    assert hashlib.sha256(data).hexdigest() == dl_entry.sha256
    assert json.loads(data.decode("utf-8"))["id"] == "acme/faq-summarizer"


def test_get_returns_latest_version_by_semver(registered_service, private_key):
    for v in ["1.0.0", "1.2.0", "1.10.0", "2.0.0-rc.1"]:
        registered_service.publish(TOKEN, sign_manifest(private_key, base_manifest(version=v)))
    got = registered_service.get("acme/faq-summarizer")
    # semver: 1.10.0 > 1.2.0 (数値比較)。2.0.0-rc.1 は major=2 で 1.x 系より上(prerelease は
    # 同じ 2.0.0 正式版より低いだけ) → 最新は 2.0.0-rc.1。
    assert got["entry"]["version"] == "2.0.0-rc.1"


def test_get_latest_prefers_release_over_prerelease(registered_service, private_key):
    for v in ["2.0.0-rc.1", "2.0.0"]:
        registered_service.publish(TOKEN, sign_manifest(private_key, base_manifest(version=v)))
    got = registered_service.get("acme/faq-summarizer")
    assert got["entry"]["version"] == "2.0.0"


def test_download_specific_version(registered_service, private_key):
    registered_service.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    registered_service.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.1.0")))
    data, entry = registered_service.download("acme/faq-summarizer", "1.0.0")
    assert entry.version == "1.0.0"
    assert json.loads(data.decode("utf-8"))["version"] == "1.0.0"


# --- 無署名・不正署名・認可の拒否 ---------------------------------------------


def test_publish_rejects_unsigned(registered_service):
    """無署名 publish の拒否(受け入れ条件)。"""
    with pytest.raises(RegistryValidationError, match="無署名"):
        registered_service.publish(TOKEN, base_manifest())


def test_publish_rejects_unknown_token(registered_service, private_key):
    manifest = sign_manifest(private_key, base_manifest())
    with pytest.raises(RegistryAuthError):
        registered_service.publish("bogus-token", manifest)


def test_publish_rejects_publisher_mismatch(registered_service, private_key):
    # 認証発行者(acme-corp)と異なる publisher の manifest はなりすましとして拒否(認可失敗=403)。
    manifest = sign_manifest(private_key, base_manifest(publisher="evil-corp"))
    with pytest.raises(RegistryForbiddenError, match="一致しない"):
        registered_service.publish(TOKEN, manifest)


def test_publish_rejects_unregistered_key(service, private_key):
    # 公開鍵未登録のまま publish → 鍵 lookup 失敗で拒否。
    manifest = sign_manifest(private_key, base_manifest())
    with pytest.raises(RegistryValidationError, match="未登録"):
        service.publish(TOKEN, manifest)


def test_publish_rejects_tampered_manifest(registered_service, private_key):
    # 署名後に内容を改ざん → 署名検証 False で拒否。
    manifest = sign_manifest(private_key, base_manifest())
    manifest["name"] = "改ざんされた名前"
    with pytest.raises(RegistryValidationError, match="署名検証"):
        registered_service.publish(TOKEN, manifest)


def test_publish_rejects_wrong_key(service, private_key):
    # 別の鍵で登録して、署名は private_key → 検証失敗。
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = Ed25519PrivateKey.generate()
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(other))
    manifest = sign_manifest(private_key, base_manifest())
    with pytest.raises(RegistryValidationError, match="署名検証"):
        service.publish(TOKEN, manifest)


def test_publish_rejects_invalid_manifest(registered_service, private_key):
    bad = base_manifest()
    bad["version"] = "not-semver"
    # 検証で落ちるので署名は付けない(validate_manifest が先に弾く)。
    with pytest.raises(RegistryValidationError, match="manifest が不正"):
        registered_service.publish(TOKEN, bad)


def test_publish_rejects_duplicate_version(registered_service, private_key):
    manifest = sign_manifest(private_key, base_manifest(version="1.0.0"))
    registered_service.publish(TOKEN, manifest)
    dup = sign_manifest(private_key, base_manifest(version="1.0.0"))
    with pytest.raises(RegistryConflictError):
        registered_service.publish(TOKEN, dup)


# --- search / get の境界 ------------------------------------------------------


def test_search_filters_by_query_kind_tag(registered_service, private_key):
    registered_service.publish(
        TOKEN, sign_manifest(private_key, base_manifest(plugin_id="acme/faq", version="1.0.0"))
    )
    registered_service.publish(
        TOKEN,
        sign_manifest(
            private_key,
            base_manifest(
                plugin_id="acme/sales-agent",
                version="1.0.0",
                kind="agent",
                name="営業エージェント",
                description="案件管理",
                tags=["sales"],
            ),
        ),
    )
    by_q = registered_service.search("faq")
    assert {p["id"] for p in by_q} == {"acme/faq"}
    by_tag = registered_service.search(tag="faq")
    assert {p["id"] for p in by_tag} == {"acme/faq"}
    by_kind = registered_service.search(kind="agent")
    assert {p["id"] for p in by_kind} == {"acme/sales-agent"}
    # 検索語は description にもかかる(案件管理)。
    by_desc = registered_service.search("案件")
    assert {p["id"] for p in by_desc} == {"acme/sales-agent"}


def test_get_missing_raises_not_found(registered_service):
    with pytest.raises(RegistryNotFoundError):
        registered_service.get("acme/does-not-exist")


def test_download_missing_version_raises_not_found(registered_service, private_key):
    registered_service.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    with pytest.raises(RegistryNotFoundError):
        registered_service.download("acme/faq-summarizer", "9.9.9")


def test_missing_artifact_for_indexed_version_is_storage_error(registered_service, private_key):
    # index に在るのに成果物だけ消えた(保存層破損/手動削除)場合は 404 ではなく内部不整合(500相当)。
    entry = registered_service.publish(
        TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0"))
    )
    # 成果物だけ削除(index は残る)
    del registered_service._backend._store._objects[entry["objectPath"]]
    with pytest.raises(RegistryStorageError):
        registered_service.get("acme/faq-summarizer", "1.0.0")
    with pytest.raises(RegistryStorageError):
        registered_service.download("acme/faq-summarizer", "1.0.0")


def test_corrupted_artifact_sha_mismatch_is_storage_error(registered_service, private_key):
    # 保存層の成果物が index の sha256 と食い違う(破損/改ざん)場合、取込側に渡さず拒否する。
    entry = registered_service.publish(
        TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0"))
    )
    # 同じパスに別バイト列を上書き(sha が index と不一致になる)。
    registered_service._backend._store.put(entry["objectPath"], b'{"tampered":true}')
    with pytest.raises(RegistryStorageError, match="sha256"):
        registered_service.get("acme/faq-summarizer", "1.0.0")
    with pytest.raises(RegistryStorageError, match="sha256"):
        registered_service.download("acme/faq-summarizer", "1.0.0")
