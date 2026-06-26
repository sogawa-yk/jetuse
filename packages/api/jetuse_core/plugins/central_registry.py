"""中央レジストリ(PLG-04 形状)読取クライアント。

PLG-04 の中央レジストリ Service が Object Storage に publish する `index.json` を読み、
マーケットプレイス UI(PLG-06)から list/detail/install できるようにする読取専用クライアント。

PLG-03 の `registry_client.RegistryClient` はモック前提の簡易形状(`plugins[].manifest` パス＋
flat な `publisherKeys`)を読むが、**実運用のレジストリは PLG-04 が生成する形状**:

    index.json = {
      "schemaVersion": "1",
      # publisherKeys は発行者で入れ子: { "<publisher>": { "<keyId>": {publicKeyId, publicKey} } }
      "publisherKeys": { ... },
      "plugins": [ {"id","version","kind","name","description","publisher","tags",
                    "objectPath","sha256","publicKeyId","publishedAt"} ]
    }

を読む(`packages/registry/jetuse_registry/index.py` の `RegistryIndex`/`IndexEntry` と一致)。
成果物(manifest 全文 JSON)は各エントリの `objectPath` にあり、`sha256` で完全性を検証する。

installer.install が必要とするクライアント契約(`list` / `download` / `public_key` / `base_url`)を
満たすため、PLG-03 の installer をそのまま再利用できる。署名検証は installer 側
(`verify_signature`)が `public_key()` の返す鍵で行う(D7・fail-closed)。

レジストリ URL は運用者が設定する信頼済み値(settings.plugin_registry_url)。本クライアントは
PLG-03 のトランザクション境界・SSRF 防御(相対パス強制・リダイレクト不追従)を流用する。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .manifest import ManifestError, PluginManifest, validate_manifest
from .registry_client import (
    DEFAULT_TIMEOUT,
    INDEX_PATH,
    RegistryError,
    Transport,
    _http_transport,
    _semver_key,
)

_ED25519_PUBLIC_KEY_LEN = 32


class CentralRegistryClient:
    """PLG-04 形状の `index.json` を読む読取専用クライアント(list/get/download + 公開鍵)。"""

    def __init__(
        self,
        base_url: str = "",
        *,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if transport is None and not base_url:
            raise RegistryError("base_url か transport のどちらかが必要")
        self.base_url = base_url
        self._fetch: Transport = transport or _http_transport(base_url, timeout)
        self._index: dict[str, Any] | None = None

    # --- index -----------------------------------------------------------

    def refresh(self) -> None:
        self._index = None

    def _load_index(self) -> dict[str, Any]:
        if self._index is not None:
            return self._index
        raw = self._fetch(INDEX_PATH)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise RegistryError(f"index.json が JSON ではない: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
            raise RegistryError("index.json の形式が不正(plugins 配列が無い)")
        self._index = data
        return data

    # --- 公開 API --------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """配布エントリ((id, version) 単位)の一覧を返す(カタログ表示・選択に使う)。"""
        plugins = self._load_index()["plugins"]
        out: list[dict[str, Any]] = []
        for e in plugins:
            if not isinstance(e, dict) or "id" not in e or "version" not in e:
                raise RegistryError(f"index.json の plugins エントリが不正: {e!r}")
            out.append(dict(e))
        return out

    def get(self, plugin_id: str, version: str | None = None) -> dict[str, Any]:
        """id(と任意の version)で配布エントリを 1 件返す。version=None は semver 上の最新。"""
        candidates = [e for e in self.list() if e.get("id") == plugin_id]
        if not candidates:
            raise RegistryError(f"レジストリに存在しない plugin: {plugin_id}")
        if version is not None:
            for e in candidates:
                if e.get("version") == version:
                    return e
            raise RegistryError(f"指定版が存在しない: {plugin_id}@{version}")
        return max(candidates, key=lambda e: _semver_key(str(e.get("version", "0.0.0"))))

    def download(self, plugin_id: str, version: str | None = None) -> PluginManifest:
        """成果物(manifest 全文)を `objectPath` から取得し、sha256 検証 + 構文検証して返す。"""
        entry = self.get(plugin_id, version)
        path = entry.get("objectPath")
        if not path:
            raise RegistryError(f"index エントリに objectPath が無い: {plugin_id}")
        raw = self._fetch(path)
        # 完全性: index の sha256 と一致しない成果物は配布の取り違え/改ざんとして取込前に弾く。
        expected = entry.get("sha256")
        if expected:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                raise RegistryError(
                    f"成果物の sha256 不一致({plugin_id}@{entry.get('version')}): "
                    f"{actual} != {expected}"
                )
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise ManifestError(f"manifest が JSON ではない: {plugin_id}: {e}") from e
        manifest = validate_manifest(data)
        if manifest.id != entry.get("id"):
            raise RegistryError(
                f"manifest の id が index と不一致: {manifest.id} != {entry.get('id')}"
            )
        if manifest.version != entry.get("version"):
            raise RegistryError(
                f"manifest の version が index と不一致: "
                f"{manifest.version} != {entry.get('version')}"
            )
        return manifest

    def public_key(self, public_key_id: str) -> bytes:
        """発行者公開鍵 id から raw ed25519 公開鍵(32 バイト)を返す。

        PLG-04 の publisherKeys は `{publisher: {keyId: {...}}}` と発行者で入れ子。installer は
        publisher を渡さず key_id だけで引くため、全発行者を横断して key_id を探す。複数の発行者が
        同じ key_id に**異なる鍵**を登録していた場合は曖昧として拒否(なりすまし防止・fail-closed)。
        """
        keys = self._load_index().get("publisherKeys", {})
        if not isinstance(keys, dict):
            raise RegistryError("publisherKeys の形式が不正")
        found: bytes | None = None
        for per_publisher in keys.values():
            if not isinstance(per_publisher, dict):
                continue
            entry = per_publisher.get(public_key_id)
            if entry is None:
                continue
            raw = _decode_public_key(public_key_id, entry)
            if found is not None and found != raw:
                raise RegistryError(
                    f"発行者公開鍵 id が複数発行者で異なる鍵に衝突: {public_key_id}"
                )
            found = raw
        if found is None:
            raise RegistryError(f"発行者公開鍵が未登録: {public_key_id}")
        return found


def _decode_public_key(public_key_id: str, entry: Any) -> bytes:
    import base64
    import binascii

    if not isinstance(entry, dict) or "publicKey" not in entry:
        raise RegistryError(f"発行者公開鍵エントリが不正: {public_key_id}")
    try:
        raw = base64.b64decode(entry["publicKey"], validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise RegistryError(f"公開鍵が base64 でない: {public_key_id}: {e}") from e
    if len(raw) != _ED25519_PUBLIC_KEY_LEN:
        raise RegistryError(
            f"公開鍵長が不正(ed25519 は {_ED25519_PUBLIC_KEY_LEN} バイト): {public_key_id}"
        )
    return raw
