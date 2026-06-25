"""index.json の楽観的並行制御(read-modify-write 競合)のテスト。

`InMemoryObjectStore` を継承した「割り込み store」で、publish が index を読んでから条件付き
put する間に別の書き手が index を更新する状況を決定的に再現し、(1) 更新消失が起きないこと、
(2) 同一版の重複が並行下でも 409 で弾かれることを確認する。
"""

from __future__ import annotations

import pytest
from helpers import PUBLIC_KEY_ID, PUBLISHER, TOKEN, base_manifest, public_key_b64, sign_manifest

from jetuse_registry.errors import RegistryConflictError
from jetuse_registry.index import INDEX_OBJECT_NAME, IndexEntry, RegistryIndex
from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore


class RacyStore(InMemoryObjectStore):
    """最初の index 条件付き put の直前に1回だけ「競合する書き手」を割り込ませる store。

    これにより if_match が外れ、サービス側の楽観ロックリトライ経路を決定的に通す。
    """

    def __init__(self, racer):
        super().__init__()
        self._racer = racer
        self._fired = False

    def put(self, name, data, **kwargs):
        if name == INDEX_OBJECT_NAME and kwargs.get("if_match") and not self._fired:
            self._fired = True
            self._racer(self)  # 競合書込で etag を進める → この後の if_match put は失敗する。
        return super().put(name, data, **kwargs)


def _make_service(store):
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})
    return RegistryService(store, auth)


def _inject_entry(store, plugin_id, version):
    """index に別エントリを直接追記して etag を進める(競合 publish の模擬)。"""
    raw, _ = store.get_with_etag(INDEX_OBJECT_NAME)
    index = RegistryIndex.from_bytes(raw)
    index.upsert_entry(
        IndexEntry(
            id=plugin_id,
            version=version,
            kind="usecase",
            name="competing",
            publisher=PUBLISHER,
            objectPath=f"plugins/{plugin_id}/{version}/manifest.json",
            sha256="0" * 64,
            publicKeyId=PUBLIC_KEY_ID,
            publishedAt="2026-01-01T00:00:00+00:00",
        )
    )
    super(RacyStore, store).put(
        INDEX_OBJECT_NAME, index.to_bytes(), content_type="application/json"
    )


def test_concurrent_publish_does_not_lose_updates(private_key):
    # 競合する別プラグインの publish が割り込んでも、リトライ後に両方が index に残る。
    store = RacyStore(racer=lambda s: _inject_entry(s, "other/plugin", "1.0.0"))
    svc = _make_service(store)
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))

    entry = svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    assert entry["id"] == "acme/faq-summarizer"

    index = RegistryIndex.from_bytes(store.get_with_etag(INDEX_OBJECT_NAME)[0])
    ids = {e.id for e in index.plugins}
    # 競合エントリ(other/plugin)と自分のエントリの両方が残る = 更新消失なし。
    assert ids == {"other/plugin", "acme/faq-summarizer"}


def test_concurrent_duplicate_version_is_rejected(private_key):
    # 競合書き手が先に同一版を publish した場合、リトライ後の検証で 409 に倒れる。
    store = RacyStore(racer=lambda s: _inject_entry(s, "acme/faq-summarizer", "1.0.0"))
    svc = _make_service(store)
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))

    with pytest.raises(RegistryConflictError):
        svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))


def _artifact_and_path(private_key, **mf):
    """署名済み manifest の保存バイト列と content-addressed パスを返す。"""
    import hashlib

    from jetuse_core.plugins.manifest import validate_manifest

    from jetuse_registry.service import _manifest_bytes

    signed = sign_manifest(private_key, base_manifest(**mf))
    artifact = _manifest_bytes(validate_manifest(signed))
    digest = hashlib.sha256(artifact).hexdigest()
    pid = mf.get("plugin_id", "acme/faq-summarizer")
    ver = mf.get("version", "1.0.0")
    return signed, artifact, f"plugins/{pid}/{ver}/{digest}.json"


def test_orphan_artifact_does_not_poison_later_publish(service, private_key):
    # 失敗した publish が content A の成果物だけ残した(index 未確定)状況。content-addressed なので
    # 同一 version の修正版(別内容 B)も別パスに書かれ、orphan A に汚染されず成功する。
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    _, artifact_a, path_a = _artifact_and_path(private_key, version="1.0.0", name="A版")
    service._store.put(path_a, artifact_a)  # orphan(index にエントリ無し)

    signed_b, _, path_b = _artifact_and_path(private_key, version="1.0.0", name="B版")
    entry = service.publish(TOKEN, signed_b)
    assert entry["version"] == "1.0.0"
    assert entry["objectPath"] == path_b != path_a
    _, dl = service.download("acme/faq-summarizer", "1.0.0")
    assert dl.object_path == path_b  # download は B を返す(orphan A に汚染されない)。


def test_version_immutability_blocks_different_content_republish(service, private_key):
    # 確定済みの version を別内容で再 publish しようとすると index 一意で 409(版は不変)。
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    signed_a, _, path_a = _artifact_and_path(private_key, version="1.0.0", name="A版")
    service.publish(TOKEN, signed_a)
    signed_b, _, _ = _artifact_and_path(private_key, version="1.0.0", name="B版")
    with pytest.raises(RegistryConflictError):
        service.publish(TOKEN, signed_b)
    assert service._store.exists(path_a)  # 確定済み A の成果物は不変。


class AlwaysConflictStore(InMemoryObjectStore):
    """index.json への条件付き put が常に衝突する store(リトライ上限超過を再現)。"""

    def put(self, name, data, **kwargs):
        if name == INDEX_OBJECT_NAME and (kwargs.get("if_match") or kwargs.get("if_none_match")):
            from jetuse_registry.storage import PreconditionFailed

            raise PreconditionFailed(name)
        return super().put(name, data, **kwargs)


class EmptyEtagStore(InMemoryObjectStore):
    """既存オブジェクトの etag を空で返す store(SDK 応答異常/ヘッダ欠落を模す)。"""

    def get_with_etag(self, name):
        data, _ = super().get_with_etag(name)
        return data, ""


def test_empty_etag_on_existing_index_raises_storage_error(private_key):
    # 既存 index なのに etag が取れない場合、黙ってリトライ消尽させず即 RegistryStorageError。
    from jetuse_registry.errors import RegistryStorageError

    store = EmptyEtagStore()
    svc = _make_service(store)
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))  # index 新規作成
    with pytest.raises(RegistryStorageError):  # 既存 index を読む 2 回目の更新で発火
        svc.publish(TOKEN, sign_manifest(private_key, base_manifest()))


def test_register_key_retry_exhaustion_raises_registry_error(private_key):
    # 並行衝突が解消せずリトライ上限を超えると基底 RegistryError(HTTP 層で 503)。
    from jetuse_registry.errors import RegistryError

    svc = _make_service(AlwaysConflictStore())
    with pytest.raises(RegistryError):
        svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))


def test_idempotent_republish_same_content_when_artifact_exists(service, private_key):
    # 自分のリトライ等で同一内容の成果物が既に在っても、publish は成功する(冪等)。
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    signed, artifact, path = _artifact_and_path(private_key, version="1.0.0")
    service._store.put(path, artifact, content_type="application/json")  # 同一内容が先在

    entry = service.publish(TOKEN, signed)
    assert entry["version"] == "1.0.0"
    assert entry["objectPath"] == path
    assert service._store.get(path) == artifact
