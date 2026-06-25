"""レジストリ index(`index.json`)の構造とシリアライズ。

`index.json` は list/search/get の正本(中央レジストリ MVP = Object Storage + index)。各版ごとに
1 エントリを持ち、成果物(manifest 全文)の Object Storage 上のパスとダイジェストを指す。
発行者公開鍵もここに登録する(specs/16-platform.md §6 / comparison §2:
「Object Storage + index.json + 発行者公開鍵」)。

publish 時に service 層がこのモデルを読み・更新し、Object Storage の `index.json` を書き戻す。
別実装(PLG-03 取込クライアント)が同じ JSON を読めるよう、camelCase の安定したスキーマで直列化する。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: index 自体のスキーマ版。後方非互換変更で繰り上げる(manifest の schemaVersion とは別系統)。
INDEX_SCHEMA_VERSION = "1"

#: Object Storage 上の index オブジェクト名。
INDEX_OBJECT_NAME = "index.json"


class PublisherKey(BaseModel):
    """登録済みの発行者公開鍵(ed25519, base64 raw 32 バイト)。"""

    # alias(publicKeyId/publicKey)・フィールド名のどちらでも構築できるようにする
    # (service 層は alias キーワードで生成する。populate_by_name で両対応にし曖昧さを排除)。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: manifest.signature.publicKeyId と突き合わせる識別子。
    public_key_id: str = Field(alias="publicKeyId", min_length=1)
    #: 32 バイト raw ed25519 公開鍵を base64 した文字列。
    public_key: str = Field(alias="publicKey", min_length=1)


class IndexEntry(BaseModel):
    """1 つのプラグイン版を表す index エントリ(検索・取得の単位)。"""

    # service 層は objectPath/publicKeyId/publishedAt を alias キーワードで生成する。
    # populate_by_name で alias・フィールド名の両対応にし、構築の曖昧さを排除する。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    version: str
    kind: str
    name: str
    description: str = ""
    publisher: str
    tags: list[str] = Field(default_factory=list)
    #: 成果物(manifest 全文 JSON)の Object Storage オブジェクト名。download が読む。
    object_path: str = Field(alias="objectPath")
    #: 成果物バイト列の sha256(16 進)。取込側(PLG-03)が完全性を検証できる。
    sha256: str
    #: 署名に使われた公開鍵 ID(index.json の publisherKeys と対応)。
    public_key_id: str = Field(alias="publicKeyId")
    #: 公開時刻(ISO8601, UTC)。
    published_at: str = Field(alias="publishedAt")


class RegistryIndex(BaseModel):
    """`index.json` 全体。発行者公開鍵とプラグイン版エントリを保持する。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=INDEX_SCHEMA_VERSION, alias="schemaVersion")
    #: publisher_id -> {publicKeyId -> PublisherKey}。
    publisher_keys: dict[str, dict[str, PublisherKey]] = Field(
        default_factory=dict, alias="publisherKeys"
    )
    plugins: list[IndexEntry] = Field(default_factory=list)

    # --- シリアライズ ---

    @classmethod
    def empty(cls) -> RegistryIndex:
        """新規(空)の index を返す。"""
        return cls()

    @classmethod
    def from_bytes(cls, raw: bytes) -> RegistryIndex:
        """Object Storage から読んだ JSON バイト列を index へ復元する。"""
        return cls.model_validate(json.loads(raw.decode("utf-8")))

    def to_bytes(self) -> bytes:
        """Object Storage へ書く JSON バイト列に直列化する(camelCase, 安定整形)。"""
        data = self.model_dump(by_alias=True)
        return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

    # --- 参照ヘルパ(検索の正本) ---

    def find(self, plugin_id: str, version: str) -> IndexEntry | None:
        for e in self.plugins:
            if e.id == plugin_id and e.version == version:
                return e
        return None

    def versions(self, plugin_id: str) -> list[IndexEntry]:
        """指定 id の全版を返す(順序は登録順)。"""
        return [e for e in self.plugins if e.id == plugin_id]

    def get_public_key(self, publisher: str, public_key_id: str) -> PublisherKey | None:
        return self.publisher_keys.get(publisher, {}).get(public_key_id)

    def register_key(self, publisher: str, key: PublisherKey) -> None:
        self.publisher_keys.setdefault(publisher, {})[key.public_key_id] = key

    def upsert_entry(self, entry: IndexEntry) -> None:
        """版エントリを追加する。同一(id, version)があれば置換する。"""
        for i, e in enumerate(self.plugins):
            if e.id == entry.id and e.version == entry.version:
                self.plugins[i] = entry
                return
        self.plugins.append(entry)

    def public_summary(self) -> list[dict[str, Any]]:
        """list/search API が返す公開メタデータ(発行者鍵は含めない)。"""
        return [e.model_dump(by_alias=True) for e in self.plugins]
