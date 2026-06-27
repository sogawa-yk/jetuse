"""プラグイン基盤(PLG)。配布単位の manifest 仕様と検証ロジックを提供する。

正式仕様は specs/16-platform.md。設計判断は docs/decisions/ADR-0013。
"""

from .connector import (
    CONNECTOR_AUTH_KINDS,
    CONNECTOR_TRANSPORTS,
    ConnectorCompositionError,
    ConnectorCompositionReport,
    ConnectorDefinition,
    ConnectorError,
    connector_json_schema,
    validate_connector,
    validate_connector_composition,
)
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
from .sample_app import (
    SAMPLE_APP_CAPABILITIES,
    CompositionError,
    CompositionReport,
    SampleAppDefinition,
    SampleAppError,
    sample_app_json_schema,
    validate_composition,
    validate_sample_app,
)

# store(インストール記録 / PLG-02)・installer(取込 / PLG-03)・scaffold(sample-app 展開 / SBA-01)・
# connector_store(connector 登録 / CON-01)は DB 接続に依存するため、manifest/registry-only 利用者に
# DB 依存を持ち込まないよう __init__ では re-export しない。利用側は
# `from jetuse_core.plugins.installer import install, uninstall` /
# `from jetuse_core.plugins.scaffold import ...` / `from jetuse_core.plugins.store import ...` /
# `from jetuse_core.plugins.connector_store import register_connector, ...` で明示 import する。
# registry_client(httpx 遅延 import)と sample_app/connector(定義検証・合成バリデーション、
# DB 非依存)はモジュール import で副作用がないため再公開する。

__all__ = [
    "CONNECTOR_AUTH_KINDS",
    "CONNECTOR_TRANSPORTS",
    "PLATFORM_SCOPES",
    "PLUGIN_KINDS",
    "SCHEMA_VERSION",
    "SAMPLE_APP_CAPABILITIES",
    "CompositionError",
    "CompositionReport",
    "ConnectorCompositionError",
    "ConnectorCompositionReport",
    "ConnectorDefinition",
    "ConnectorError",
    "ManifestError",
    "PluginManifest",
    "RegistryClient",
    "RegistryError",
    "SampleAppDefinition",
    "SampleAppError",
    "canonical_signing_payload",
    "connector_json_schema",
    "manifest_json_schema",
    "sample_app_json_schema",
    "validate_composition",
    "validate_connector",
    "validate_connector_composition",
    "validate_manifest",
    "validate_sample_app",
    "verify_signature",
]
