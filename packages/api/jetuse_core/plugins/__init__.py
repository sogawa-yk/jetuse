"""プラグイン基盤(PLG)。配布単位の manifest 仕様と検証ロジックを提供する。

正式仕様は specs/16-platform.md。設計判断は docs/decisions/ADR-0013。
"""

from .manifest import (
    PLATFORM_SCOPES,
    PLUGIN_KINDS,
    SCHEMA_VERSION,
    ManifestError,
    PluginManifest,
    canonical_signing_payload,
    manifest_json_schema,
    validate_manifest,
    verify_signature,
)

# store(インストール記録 / PLG-02)は DB 接続に依存するため、manifest-only 利用者に
# DB 依存を持ち込まないよう __init__ では re-export しない。利用側は
# `from jetuse_core.plugins.store import ...` で明示 import する。

__all__ = [
    "PLATFORM_SCOPES",
    "PLUGIN_KINDS",
    "SCHEMA_VERSION",
    "ManifestError",
    "PluginManifest",
    "canonical_signing_payload",
    "manifest_json_schema",
    "validate_manifest",
    "verify_signature",
]
