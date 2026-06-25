"""レジストリテストの署名ヘルパ・定数(test_* から import する)。

pytest の prepend import モードで tests ディレクトリが sys.path に載るため、
`from helpers import ...` で参照できる(api の tests と同じく __init__.py を置かない素のモジュール)。
署名は PLG-01 の `canonical_signing_payload` を対象に ed25519 で生成し、本物の検証経路を通す。
"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jetuse_core.plugins.manifest import (
    SCHEMA_VERSION,
    canonical_signing_payload,
    validate_manifest,
)

PUBLISHER = "acme-corp"
TOKEN = "test-token-acme"
PUBLIC_KEY_ID = "acme-key-1"


def base_manifest(
    *,
    plugin_id: str = "acme/faq-summarizer",
    version: str = "1.0.0",
    publisher: str = PUBLISHER,
    kind: str = "usecase",
    name: str = "FAQ要約",
    description: str = "FAQ を要約するユースケース",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """署名前の manifest dict(配布表現 camelCase)。contributes は kind に合わせる。"""
    if kind == "agent":
        contributes = {"agent": {"instructions": "あなたは営業支援エージェント", "tools": []}}
    else:
        contributes = {
            "usecase": {
                "fields": [{"name": "text", "type": "textarea"}],
                "template": "要約して: {{text}}",
            }
        }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": plugin_id,
        "version": version,
        "kind": kind,
        "name": name,
        "description": description,
        "publisher": publisher,
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:rag.search"],
        "tags": tags if tags is not None else ["faq", "summarize"],
        "contributes": contributes,
    }


def sign_manifest(
    private_key: Ed25519PrivateKey,
    manifest_data: dict[str, Any],
    *,
    public_key_id: str = PUBLIC_KEY_ID,
) -> dict[str, Any]:
    """manifest dict に有効な ed25519 署名を付けて返す。

    署名対象は「signature を除いた正準バイト列」。未署名 manifest を validate して payload を得て
    署名し、signature を載せる(署名の有無で payload は不変=PLG-01 の契約)。
    """
    unsigned = validate_manifest(manifest_data)
    payload = canonical_signing_payload(unsigned)
    sig = private_key.sign(payload)
    signed = dict(manifest_data)
    signed["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": public_key_id,
        "value": base64.b64encode(sig).decode("ascii"),
    }
    return signed


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii")
