"""レジストリ保存層の抽象(`RegistryBackend`)と μService の値オブジェクト(MKT-02)。

PLG-04 は Object Storage + index.json を直接 service 層に埋め込んでいた。MKT-02 では保存層を
**差し替え可能なバックエンド**へ整理し、ADB へ昇格できるようにする。service 層(認証・manifest 検証・
ed25519 署名検証・認可)はバックエンド非依存に保ち、永続化の詳細だけをバックエンドへ委譲する。

実装は 3 種:
  - `IndexBackend`     … 従来の Object Storage + index.json(レガシー後方互換。拡張操作は未対応)。
  - `InMemoryRegistryBackend` … 全機能のインメモリ実装(service ロジック・拡張操作の単体テスト用)。
  - `AdbBackend`       … ADB(jetuse_core.db)に版/鍵/評価/DL 数/ライフサイクルを持つ μService 本体。

バックエンドは「永続化と原子性」のみを担う。署名検証や publisher 一致判定など**意味づけは service**
が持つ(同じ判断を 3 実装に重複させない)。`add_version`/`register_key` は原子的(競合は例外で表す)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import RegistryValidationError
from .index import IndexEntry, PublisherKey

#: 評価スコアの下限・上限(1〜5 の整数)。
MIN_RATING = 1
MAX_RATING = 5

# ADB の固定長 VARCHAR2 カラム幅(migration 022。既定 NLS=BYTE 意味論)と揃えた上限。
# **ADB を保存層とする backend 限定**でこれを検証し超過を RegistryValidationError(=422)に倒す
# (長大値が DB の ORA-12899=500 になるのを防ぐ)。レガシー index.json(IndexBackend)はカラム幅を
# 持たないため**検証しない=PLG-04 の後方互換を狭めない**。byte 長で見る(マルチバイトでも列幅安全)。
MAX_NAME_LEN = 1000
MAX_DESCRIPTION_LEN = 4000
MAX_PUBLISHER_LEN = 255
MAX_PUBLIC_KEY_ID_LEN = 255
MAX_PUBLIC_KEY_LEN = 512
MAX_OBJECT_PATH_LEN = 1000
#: tags は JSON 配列文字列として 1 カラムに保存するため、直列化後の長さで上限を見る。
MAX_TAGS_SERIALIZED_LEN = 4000
MAX_COMMENT_LEN = 2000


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _check_byte_len(label: str, value: str, limit: int) -> None:
    """UTF-8 バイト長が ADB カラム幅を超えていれば RegistryValidationError(=422)。"""
    n = _utf8_len(value)
    if n > limit:
        raise RegistryValidationError(f"{label} が長すぎる({n} > {limit} bytes)")


def check_entry_storage_limits(entry: IndexEntry) -> None:
    """ADB の固定長カラムに収まるか版エントリを検証する(ADB/InMemory backend が書込前に呼ぶ)。"""
    _check_byte_len("name", entry.name, MAX_NAME_LEN)
    _check_byte_len("description", entry.description, MAX_DESCRIPTION_LEN)
    _check_byte_len("publisher", entry.publisher, MAX_PUBLISHER_LEN)
    _check_byte_len("publicKeyId", entry.public_key_id, MAX_PUBLIC_KEY_ID_LEN)
    _check_byte_len("objectPath", entry.object_path, MAX_OBJECT_PATH_LEN)
    _check_byte_len(
        "tags", json.dumps(list(entry.tags), ensure_ascii=False), MAX_TAGS_SERIALIZED_LEN
    )


def check_key_storage_limits(key: PublisherKey) -> None:
    _check_byte_len("publicKeyId", key.public_key_id, MAX_PUBLIC_KEY_ID_LEN)
    _check_byte_len("publicKey", key.public_key, MAX_PUBLIC_KEY_LEN)


def check_comment_storage_limit(comment: str) -> None:
    _check_byte_len("comment", comment, MAX_COMMENT_LEN)


def check_principal_storage_limit(label: str, value: str) -> None:
    """publisher / rater など VARCHAR2(255) 主体識別子の byte 長を検証する。"""
    _check_byte_len(label, value, MAX_PUBLISHER_LEN)


@dataclass(frozen=True)
class Rating:
    """1 件の評価(rater 単位)。score は 1〜5、comment は任意。"""

    rater: str
    score: int
    comment: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class RatingSummary:
    """プラグイン(id 単位)の評価集計。average は count==0 のとき None。"""

    plugin_id: str
    count: int
    average: float | None
    ratings: list[Rating] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "pluginId": self.plugin_id,
            "count": self.count,
            "average": self.average,
            "ratings": [
                {
                    "rater": r.rater,
                    "score": r.score,
                    "comment": r.comment,
                    "createdAt": r.created_at,
                }
                for r in self.ratings
            ],
        }


@runtime_checkable
class RegistryBackend(Protocol):
    """レジストリの永続化バックエンド。service 層はこの契約にのみ依存する。

    読取(list/search/versions/find/read_artifact/公開鍵取得)と、原子的な書込
    (register_key/add_version)を提供する。MKT-02 拡張(record_download/set_lifecycle/
    add_rating/get_ratings)はレガシー実装では `RegistryUnsupportedError` を送出してよい。
    """

    # --- 読取 ---
    def list_entries(self) -> list[IndexEntry]:
        """全プラグイン版エントリを返す(ライフサイクル絞り込みは service が行う)。"""
        ...

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[IndexEntry]:
        """q(id/name/description 部分一致)・kind・tag で絞り込んだエントリを返す。"""
        ...

    def versions(self, plugin_id: str) -> list[IndexEntry]:
        """指定 id の全版エントリを返す。"""
        ...

    def find(self, plugin_id: str, version: str) -> IndexEntry | None:
        """(id, version) のエントリを返す。無ければ None。"""
        ...

    def read_artifact(self, entry: IndexEntry) -> bytes:
        """エントリの成果物(manifest 全文 JSON)のバイト列を返す。欠落は RegistryStorageError。"""
        ...

    def get_public_key(self, publisher: str, public_key_id: str) -> PublisherKey | None:
        """発行者の登録済み公開鍵を返す。無ければ None。"""
        ...

    def get_publisher_keys(self, publisher: str) -> list[PublisherKey]:
        """発行者の全登録公開鍵を返す。"""
        ...

    # --- 書込(原子的) ---
    def register_key(self, publisher: str, key: PublisherKey) -> None:
        """公開鍵を登録する。冪等(同一鍵の再登録は無害)・差し替えは RegistryConflictError。"""
        ...

    def add_version(self, entry: IndexEntry, artifact: bytes) -> None:
        """版エントリ＋成果物を原子的に追加する。(id, version) 既存なら RegistryConflictError。"""
        ...

    # --- MKT-02 拡張(レガシーは RegistryUnsupportedError 可) ---
    def record_download(self, plugin_id: str, version: str) -> int | None:
        """ダウンロードを 1 件記録し新カウントを返す。未対応バックエンドは None を返す。"""
        ...

    def set_lifecycle(self, plugin_id: str, version: str, state: str) -> IndexEntry:
        """版ライフサイクル状態を設定し、更新後エントリを返す。"""
        ...

    def add_rating(self, plugin_id: str, rater: str, score: int, comment: str) -> None:
        """評価を登録/更新する(1 rater 1 件＝upsert)。"""
        ...

    def get_ratings(self, plugin_id: str) -> RatingSummary:
        """プラグインの評価集計を返す。"""
        ...
