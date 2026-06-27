"""中央プラグインレジストリ Service (PLG-04 / D2 MVP)。

ベンダー運用の共有レジストリ本体。Object Storage を保存層とし、`index.json`(一覧/検索/取得の
正本)＋発行者公開鍵＋プラグイン成果物(manifest)を保持する。読取(list/search/get/download)は
公開、publish は発行者認証＋ed25519 署名検証(PLG-01 再利用)を要求する。

設計判断は docs/decisions/ADR-0013。
仕様は specs/16-platform.md / docs/comparison/marketplace-plugin.md §2。
MKT-02: 保存層を差し替え可能な `RegistryBackend` に整理し、ADB バックエンドμService
(評価・DL 数・版ライフサイクル・DB 検索)へ昇格した。既定は従来の Object Storage + index.json
(`IndexBackend`)で後方互換。
"""

from .adb_backend import AdbBackend
from .backend import Rating, RatingSummary, RegistryBackend
from .errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryError,
    RegistryForbiddenError,
    RegistryGoneError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryUnsupportedError,
    RegistryValidationError,
)
from .index import IndexEntry, RegistryIndex
from .index_backend import IndexBackend
from .memory_backend import InMemoryRegistryBackend
from .service import RegistryService
from .storage import InMemoryObjectStore, ObjectStore, PreconditionFailed

__all__ = [
    "AdbBackend",
    "IndexBackend",
    "IndexEntry",
    "InMemoryObjectStore",
    "InMemoryRegistryBackend",
    "ObjectStore",
    "PreconditionFailed",
    "Rating",
    "RatingSummary",
    "RegistryAuthError",
    "RegistryBackend",
    "RegistryConflictError",
    "RegistryError",
    "RegistryForbiddenError",
    "RegistryGoneError",
    "RegistryIndex",
    "RegistryNotFoundError",
    "RegistryService",
    "RegistryStorageError",
    "RegistryUnsupportedError",
    "RegistryValidationError",
]
