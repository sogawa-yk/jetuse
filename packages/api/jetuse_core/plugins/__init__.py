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
from .registry_client import RegistryClient, RegistryError

# store(インストール記録 / PLG-02)・installer(取込 / PLG-03)は DB 接続に依存するため、
# manifest/registry-only 利用者に DB 依存を持ち込まないよう __init__ では re-export しない。
# 利用側は `from jetuse_core.plugins.installer import install, uninstall` で明示 import する。
# registry_client は httpx を遅延 import するため(モジュール import で副作用なし)再公開する。

__all__ = [
    "PLATFORM_SCOPES",
    "PLUGIN_KINDS",
    "SCHEMA_VERSION",
    "ManifestError",
    "PluginManifest",
    "RegistryClient",
    "RegistryError",
    "canonical_signing_payload",
    "manifest_json_schema",
    "validate_manifest",
    "verify_signature",
]
