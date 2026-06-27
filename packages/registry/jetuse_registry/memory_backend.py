"""全機能のインメモリ `RegistryBackend`(service ロジック・MKT-02 拡張の単体テスト用)。

ADB を起こさずに評価/DL 数/版ライフサイクル/DB 検索の **意味づけ**を検証するための実装。
`AdbBackend` と同じ契約・同じ可視性規則を Python の dict で再現する(実 ADB 検証は E2E が担う)。
スレッド安全(lock)にして並行 publish/download/rating のテストにも使えるようにする。
"""

from __future__ import annotations

import datetime
import threading

from .backend import (
    Rating,
    RatingSummary,
    check_comment_storage_limit,
    check_entry_storage_limits,
    check_key_storage_limits,
    check_principal_storage_limit,
)
from .errors import (
    RegistryConflictError,
    RegistryNotFoundError,
    RegistryStorageError,
)
from .index import LIFECYCLE_STATES, IndexEntry, PublisherKey


def _decode_key_or_none(b64: str) -> bytes | None:
    import base64
    import binascii

    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


class InMemoryRegistryBackend:
    """`RegistryBackend` の全機能インメモリ実装。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._artifacts: dict[str, bytes] = {}
        self._entries: dict[tuple[str, str], IndexEntry] = {}
        self._keys: dict[str, dict[str, PublisherKey]] = {}
        # plugin_id -> rater -> Rating
        self._ratings: dict[str, dict[str, Rating]] = {}

    # --- 読取 ---

    def list_entries(self) -> list[IndexEntry]:
        with self._lock:
            return list(self._entries.values())

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[IndexEntry]:
        ql = q.lower().strip() if q else None
        results: list[IndexEntry] = []
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
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
        with self._lock:
            return [e for e in self._entries.values() if e.id == plugin_id]

    def find(self, plugin_id: str, version: str) -> IndexEntry | None:
        with self._lock:
            return self._entries.get((plugin_id, version))

    def read_artifact(self, entry: IndexEntry) -> bytes:
        import hashlib

        with self._lock:
            data = self._artifacts.get(entry.object_path)
        if data is None:
            raise RegistryStorageError(
                f"index に在るが成果物が欠落している(保存層の不整合): {entry.object_path}"
            )
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise RegistryStorageError(
                f"成果物の sha256 が index と不一致(破損/改ざんの疑い): {entry.object_path}"
            )
        return data

    def get_public_key(self, publisher: str, public_key_id: str) -> PublisherKey | None:
        with self._lock:
            return self._keys.get(publisher, {}).get(public_key_id)

    def get_publisher_keys(self, publisher: str) -> list[PublisherKey]:
        with self._lock:
            return list(self._keys.get(publisher, {}).values())

    # --- 書込(原子的) ---

    def register_key(self, publisher: str, key: PublisherKey) -> None:
        check_principal_storage_limit("publisher", publisher)
        check_key_storage_limits(key)  # ADB と同じ列幅検証(test double として挙動を揃える)。
        with self._lock:
            existing = self._keys.get(publisher, {}).get(key.public_key_id)
            if existing is not None and _decode_key_or_none(
                existing.public_key
            ) != _decode_key_or_none(key.public_key):
                raise RegistryConflictError(
                    f"公開鍵 '{key.public_key_id}' は登録済みで別の鍵に差し替えできない"
                    f"(鍵 ID は不変。新しい鍵は別の publicKeyId で登録すること)"
                )
            self._keys.setdefault(publisher, {})[key.public_key_id] = key

    def add_version(self, entry: IndexEntry, artifact: bytes) -> None:
        check_entry_storage_limits(entry)  # ADB と同じ列幅検証。
        with self._lock:
            if (entry.id, entry.version) in self._entries:
                raise RegistryConflictError(
                    f"{entry.id}@{entry.version} は既に publish 済み(版は不変)"
                )
            existing = self._artifacts.get(entry.object_path)
            if existing is not None and existing != artifact:
                raise RegistryConflictError(
                    f"{entry.id}@{entry.version} の成果物が別内容で既に存在(版は不変)"
                )
            self._artifacts[entry.object_path] = bytes(artifact)
            self._entries[(entry.id, entry.version)] = entry

    # --- MKT-02 拡張 ---

    def record_download(self, plugin_id: str, version: str) -> int | None:
        with self._lock:
            entry = self._entries.get((plugin_id, version))
            if entry is None:
                return None
            new_count = entry.download_count + 1
            self._entries[(plugin_id, version)] = entry.model_copy(
                update={"download_count": new_count}
            )
            return new_count

    def set_lifecycle(self, plugin_id: str, version: str, state: str) -> IndexEntry:
        if state not in LIFECYCLE_STATES:
            # service 側でも検証するが、バックエンド単体でも不正状態を弾く(防御的)。
            raise RegistryStorageError(f"未知のライフサイクル状態: {state!r}")
        with self._lock:
            entry = self._entries.get((plugin_id, version))
            if entry is None:
                raise RegistryNotFoundError(f"{plugin_id}@{version} は存在しない")
            updated = entry.model_copy(update={"lifecycle": state})
            self._entries[(plugin_id, version)] = updated
            return updated

    def add_rating(self, plugin_id: str, rater: str, score: int, comment: str) -> None:
        check_principal_storage_limit("rater", rater)
        check_comment_storage_limit(comment)  # ADB と同じ列幅検証。
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        with self._lock:
            self._ratings.setdefault(plugin_id, {})[rater] = Rating(
                rater=rater, score=score, comment=comment, created_at=now
            )

    def get_ratings(self, plugin_id: str) -> RatingSummary:
        with self._lock:
            by_rater = dict(self._ratings.get(plugin_id, {}))
        ratings = sorted(by_rater.values(), key=lambda r: r.created_at, reverse=True)
        count = len(ratings)
        average = round(sum(r.score for r in ratings) / count, 2) if count else None
        return RatingSummary(
            plugin_id=plugin_id, count=count, average=average, ratings=ratings
        )
