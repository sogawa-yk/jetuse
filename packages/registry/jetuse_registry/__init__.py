"""中央プラグインレジストリ Service (PLG-04 / D2 MVP)。

ベンダー運用の共有レジストリ本体。Object Storage を保存層とし、`index.json`(一覧/検索/取得の
正本)＋発行者公開鍵＋プラグイン成果物(manifest)を保持する。読取(list/search/get/download)は
公開、publish は発行者認証＋ed25519 署名検証(PLG-01 再利用)を要求する。

設計判断は docs/decisions/ADR-0013。
仕様は specs/16-platform.md / docs/comparison/marketplace-plugin.md §2。
本タスクは MVP(Object Storage + index)。評価・DL 数・レビュー等の μService 高度化はステージ4。
"""

from .errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryError,
    RegistryForbiddenError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryValidationError,
)
from .index import IndexEntry, RegistryIndex
from .service import RegistryService
from .storage import InMemoryObjectStore, ObjectStore, PreconditionFailed

__all__ = [
    "IndexEntry",
    "InMemoryObjectStore",
    "ObjectStore",
    "PreconditionFailed",
    "RegistryAuthError",
    "RegistryConflictError",
    "RegistryError",
    "RegistryForbiddenError",
    "RegistryIndex",
    "RegistryNotFoundError",
    "RegistryService",
    "RegistryStorageError",
    "RegistryValidationError",
]
