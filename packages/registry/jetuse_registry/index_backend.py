"""レガシー保存層: Object Storage + index.json バックエンド(PLG-04 後方互換)。

PLG-04 の `RegistryService` に埋め込まれていた index.json の read-modify-write・楽観的並行制御・
content-addressed 成果物保存を `RegistryBackend` 実装として切り出したもの。外部挙動(list/get/
download/publish の契約)は PLG-04 と同一で、既存テストが無改変で通ることが後方互換の担保。

MKT-02 拡張(評価/ライフサイクル変更)は index.json では扱わず `RegistryUnsupportedError` を送出する
(黙って no-op にせず「このバックエンドでは未対応」を明示)。DL 数の記録は download を壊さないよう
no-op(None)で受ける。ライフサイクルは旧 index.json に無く全エントリ active 扱い(IndexEntry 既定)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TypeVar

from .errors import RegistryStorageError, RegistryUnsupportedError
from .index import INDEX_OBJECT_NAME, IndexEntry, PublisherKey, RegistryIndex
from .storage import IF_NONE_MATCH_ANY, ObjectStore, PreconditionFailed

#: index.json の楽観的更新リトライ上限(並行 publish 衝突時の読み直し回数)。
_MAX_INDEX_RETRIES = 8

_T = TypeVar("_T")


class IndexBackend:
    """Object Storage 上の index.json を正本とする `RegistryBackend` 実装(レガシー MVP)。"""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    # --- index 読み書き ---

    def _load_index(self) -> RegistryIndex:
        try:
            raw, _ = self._store.get_with_etag(INDEX_OBJECT_NAME)
        except KeyError:
            return RegistryIndex.empty()
        return RegistryIndex.from_bytes(raw)

    def _update_index(self, mutate: Callable[[RegistryIndex], _T]) -> _T:
        """index を read-modify-write で更新する(楽観的並行制御＋リトライ)。

        index を etag つきで読み、`mutate` で検証・変更し、`if_match`(既存)/`if_none_match`(新規)で
        条件付き保存する。間に他者が更新していれば `PreconditionFailed` を受けて読み直し再試行する。
        `mutate` が送出するドメイン例外(検証失敗・409 等)はリトライせずそのまま伝播する。
        """
        for _ in range(_MAX_INDEX_RETRIES):
            try:
                raw, etag = self._store.get_with_etag(INDEX_OBJECT_NAME)
                index = RegistryIndex.from_bytes(raw)
                exists = True
            except KeyError:
                index, etag, exists = RegistryIndex.empty(), None, False
            if exists and not etag:
                raise RegistryStorageError(
                    "index.json の etag を取得できず安全に更新できない(保存層/レスポンス形状の異常)"
                )
            result = mutate(index)  # ドメイン検証はここで(失敗はリトライせず伝播)。
            payload = index.to_bytes()
            try:
                if exists:
                    self._store.put(
                        INDEX_OBJECT_NAME, payload,
                        content_type="application/json", if_match=etag,
                    )
                else:
                    self._store.put(
                        INDEX_OBJECT_NAME, payload,
                        content_type="application/json", if_none_match=IF_NONE_MATCH_ANY,
                    )
                return result
            except PreconditionFailed:
                continue  # 他者が先に更新 → 読み直してやり直す。
        from .errors import RegistryError

        raise RegistryError(
            "index.json の更新が並行更新の衝突で確定できなかった(リトライ上限超過)"
        )

    # --- 読取 ---

    def list_entries(self) -> list[IndexEntry]:
        return list(self._load_index().plugins)

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[IndexEntry]:
        ql = q.lower().strip() if q else None
        results: list[IndexEntry] = []
        for e in self._load_index().plugins:
            if kind is not None and e.kind != kind:
                continue
            if tag is not None and tag not in e.tags:
                continue
            if ql:
                haystack = f"{e.id}\n{e.name}\n{e.description}".lower()
                if ql not in haystack:
                    continue
            results.append(e)
        return results

    def versions(self, plugin_id: str) -> list[IndexEntry]:
        return self._load_index().versions(plugin_id)

    def find(self, plugin_id: str, version: str) -> IndexEntry | None:
        return self._load_index().find(plugin_id, version)

    def read_artifact(self, entry: IndexEntry) -> bytes:
        """成果物を読み、index の sha256 と一致するか検証して返す(破損/取り違えの自衛)。"""
        try:
            data = self._store.get(entry.object_path)
        except KeyError as e:
            raise RegistryStorageError(
                f"index に在るが成果物が欠落している(保存層の不整合): {entry.object_path}"
            ) from e
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise RegistryStorageError(
                f"成果物の sha256 が index と不一致(破損/改ざんの疑い): {entry.object_path}"
            )
        return data

    def get_public_key(self, publisher: str, public_key_id: str) -> PublisherKey | None:
        return self._load_index().get_public_key(publisher, public_key_id)

    def get_publisher_keys(self, publisher: str) -> list[PublisherKey]:
        keys = self._load_index().publisher_keys.get(publisher, {})
        return list(keys.values())

    # --- 書込(原子的) ---

    def register_key(self, publisher: str, key: PublisherKey) -> None:
        from .errors import RegistryConflictError
        from .service import _decode_key_or_none

        def _mutate(index: RegistryIndex) -> None:
            # (publisher, publicKeyId) は不変。同一鍵の冪等再登録のみ許可し別鍵への差し替えは 409。
            # 等価判定はデコード後の鍵バイト列で行う(base64 表現ゆれで誤検知しない)。
            existing = index.get_public_key(publisher, key.public_key_id)
            if existing is not None and _decode_key_or_none(
                existing.public_key
            ) != _decode_key_or_none(key.public_key):
                raise RegistryConflictError(
                    f"公開鍵 '{key.public_key_id}' は登録済みで別の鍵に差し替えできない"
                    f"(鍵 ID は不変。新しい鍵は別の publicKeyId で登録すること)"
                )
            index.register_key(publisher, key)

        self._update_index(_mutate)

    def add_version(self, entry: IndexEntry, artifact: bytes) -> None:
        from .errors import RegistryConflictError

        def _mutate(index: RegistryIndex) -> None:
            # 版の不変性(既存版への再 publish を拒否)。並行 publish でも条件付き put で確定する。
            if index.find(entry.id, entry.version) is not None:
                raise RegistryConflictError(
                    f"{entry.id}@{entry.version} は既に publish 済み(版は不変)"
                )
            # 成果物保存(content-addressed: sha 入りパス。内容が違えばパスも違う=衝突しない)。
            try:
                self._store.put(
                    entry.object_path, artifact,
                    content_type="application/json", if_none_match=IF_NONE_MATCH_ANY,
                )
            except PreconditionFailed:
                existing = self._store.get(entry.object_path)
                if existing != artifact:
                    raise RegistryConflictError(
                        f"{entry.id}@{entry.version} の成果物が別内容で既に存在(版は不変)"
                    ) from None
            index.upsert_entry(entry)

        self._update_index(_mutate)

    # --- MKT-02 拡張(レガシー未対応) ---

    def record_download(self, plugin_id: str, version: str) -> int | None:
        # index.json は DL 数を持たない。download を壊さないよう no-op(後方互換)。
        return None

    def set_lifecycle(self, plugin_id: str, version: str, state: str):
        raise RegistryUnsupportedError(
            "版ライフサイクル変更は ADB バックエンドμService 限定(レガシー index.json は未対応)"
        )

    def add_rating(self, plugin_id: str, rater: str, score: int, comment: str) -> None:
        raise RegistryUnsupportedError(
            "評価は ADB バックエンドμService でのみ利用可能(レガシー index.json は未対応)"
        )

    def get_ratings(self, plugin_id: str):
        raise RegistryUnsupportedError(
            "評価は ADB バックエンドμService でのみ利用可能(レガシー index.json は未対応)"
        )
