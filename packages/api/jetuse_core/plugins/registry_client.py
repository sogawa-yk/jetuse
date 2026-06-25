"""中央レジストリ クライアント(PLG-03 / D2・D7)。

ベンダー運用の中央レジストリ(Object Storage + `index.json` + 発行者公開鍵)から、
配布されたプラグイン manifest を取得する読取専用クライアント。スナップショット取込
(installer.py / D6)はここで取得した manifest を版固定で ADB へ書き込む。

レジストリの配布レイアウト(本 MVP が前提とする形):

    <base>/index.json
    <base>/<manifest path...>      # index の各エントリが指す manifest JSON

`index.json` の形(schemaVersion="1"):

    {
      "schemaVersion": "1",
      "plugins": [
        {"id": "acme/faq", "version": "1.2.0", "kind": "usecase",
         "name": "FAQ要約", "publisher": "acme-corp",
         "manifest": "plugins/acme/faq/1.2.0/manifest.json"}
      ],
      "publisherKeys": {"acme-key-1": "<base64 of 32-byte ed25519 public key>"}
    }

- `plugins`: (id, version) ごとの配布エントリ一覧。同一 id の複数版を許す。
- `publisherKeys`: `publicKeyId` -> raw ed25519 公開鍵(32 バイト)の base64。manifest の
  `signature.publicKeyId` をこの表で引き、取込時に ed25519 署名を検証する(D7)。

通信は HTTP(S)。テスト容易性のため `transport`(path -> bytes の callable)を注入できる。
未注入なら httpx で `base_url` 配下を GET する。レジストリ URL は運用者が設定する信頼済みの
値(settings.plugin_registry_url / .env)であり、ユーザー入力ごとの SSRF 面ではない。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from .manifest import ManifestError, PluginManifest, validate_manifest

#: index.json のパス(base_url からの相対)。
INDEX_PATH = "index.json"

#: ネットワーク往復の上限(秒)。レジストリ停止時に無限待ちしない(db.py と同じ思想)。
DEFAULT_TIMEOUT = 10.0

#: ed25519 raw 公開鍵のバイト長。
_ED25519_PUBLIC_KEY_LEN = 32

Transport = Callable[[str], bytes]


class RegistryError(Exception):
    """レジストリ取得・解釈に失敗したときに送出する(通信エラー・不正 index・未知 id 等)。"""


def _require_relative(path: str) -> str:
    """レジストリ取得パスが base URL 配下の相対パスであることを強制する。

    index.json 由来の `manifest` 等のパスを信頼済み base URL に urljoin する前に、絶対 URL
    (`https://evil/...`)・スキーム相対(`//evil/...`)・ホスト絶対パス(`/...`)・親ディレクトリ
    遡行(`..`)を拒否する。これらを許すと、誤配布/改ざんされた index がレジストリ配下の外へ
    取得先を差し替えられ、「中央レジストリ配下からのみ取得する」前提が崩れる(Codex F-001)。
    """
    if not isinstance(path, str) or not path:
        raise RegistryError("レジストリ取得パスが不正(空)")
    split = urlsplit(path)
    if split.scheme or split.netloc:
        raise RegistryError(f"レジストリ取得パスに scheme/host を含められない: {path!r}")
    # "//host/..." は urlsplit で netloc が空のことがあるため明示的に弾く。
    if path.startswith("/") or path.startswith("\\"):
        raise RegistryError(f"レジストリ取得パスは相対でなければならない: {path!r}")
    if any(seg == ".." for seg in path.replace("\\", "/").split("/")):
        raise RegistryError(f"レジストリ取得パスに親ディレクトリ遡行は使えない: {path!r}")
    return path


def _http_transport(base_url: str, timeout: float) -> Transport:
    """httpx を使った既定トランスポート。base_url 配下の相対パスを GET して bytes を返す。"""

    def fetch(path: str) -> bytes:
        import httpx

        # 取得先が base URL 配下から外れないよう、相対パスのみ許可する(防御的・二重化)。
        url = urljoin(_with_slash(base_url), _require_relative(path))
        try:
            # follow_redirects=False: 3xx を追従すると Location で base URL 外へ転送され、
            # _require_relative(入力パス検証)を迂回して取得先が差し替わる(Codex F-001)。
            # リダイレクトは「レジストリ配下からのみ取得」前提を破るのでエラーに倒す。
            resp = httpx.get(url, timeout=timeout, follow_redirects=False)
            if resp.is_redirect:
                raise RegistryError(
                    f"レジストリがリダイレクトを返した(base URL 外への転送は不可): {path}"
                )
            resp.raise_for_status()
        except httpx.HTTPError as e:  # 接続不可・タイムアウト・4xx/5xx を一括りに正規化。
            raise RegistryError(f"レジストリ取得に失敗: {path}: {e}") from e
        return resp.content

    return fetch


def _with_slash(base_url: str) -> str:
    # urljoin はベース末尾が "/" でないと最後のパスセグメントを置換してしまう。
    return base_url if base_url.endswith("/") else base_url + "/"


class RegistryClient:
    """中央レジストリの読取専用クライアント(list / get / download + 公開鍵取得)。

    index.json は 1 クライアント内でキャッシュする(取込の一連の操作で複数回引くため)。
    最新状態が要るときは `refresh()` で破棄する。
    """

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

    # --- index ----------------------------------------------------------

    def refresh(self) -> None:
        """キャッシュした index を破棄する(次アクセスで再取得)。"""
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

    # --- 公開 API -------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """レジストリが公開する配布エントリ((id, version) 単位)の一覧を返す。

        エントリは index.json の `plugins` 要素のコピー(id/version/kind/name/publisher/
        manifest パス等)。取込前のカタログ表示・選択に使う。
        """
        plugins = self._load_index()["plugins"]
        out: list[dict[str, Any]] = []
        for e in plugins:
            if not isinstance(e, dict) or "id" not in e or "version" not in e:
                raise RegistryError(f"index.json の plugins エントリが不正: {e!r}")
            out.append(dict(e))
        return out

    def get(self, plugin_id: str, version: str | None = None) -> dict[str, Any]:
        """id(と任意の version)で配布エントリ(メタデータ)を 1 件返す。

        version=None のときは semver 上の最新版を解決する。該当が無ければ RegistryError。
        manifest 本体ではなく index 上のメタデータを返す(本体は download)。
        """
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
        """配布 manifest 本体を取得し、検証済み PluginManifest として返す。

        index の該当エントリの `manifest` パス(無ければ規約パス)を取得し、
        `validate_manifest` で構文検証する。検証失敗は ManifestError(呼び出し側の
        取込はこの時点で止まる)。署名の真正性検証は installer 側(verify_signature)。
        """
        entry = self.get(plugin_id, version)
        path = entry.get("manifest")
        if not path:
            raise RegistryError(f"index エントリに manifest パスが無い: {plugin_id}")
        # index 由来のパスは信頼済み base URL 配下の相対パスに限る(取得先の差し替え防止)。
        raw = self._fetch(_require_relative(path))
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise ManifestError(f"manifest が JSON ではない: {plugin_id}: {e}") from e
        manifest = validate_manifest(data)
        # index と manifest 本体の (id, version) 不一致は配布の取り違え。取込前に弾く。
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

        index.json の `publisherKeys` を引く。未登録・不正 base64・長さ不正は RegistryError。
        取込側はこの鍵で署名を検証する(検証失敗は取込拒否)。
        """
        keys = self._load_index().get("publisherKeys", {})
        if not isinstance(keys, dict) or public_key_id not in keys:
            raise RegistryError(f"発行者公開鍵が未登録: {public_key_id}")
        try:
            raw = base64.b64decode(keys[public_key_id], validate=True)
        except (binascii.Error, ValueError, TypeError) as e:
            raise RegistryError(f"公開鍵が base64 でない: {public_key_id}: {e}") from e
        if len(raw) != _ED25519_PUBLIC_KEY_LEN:
            raise RegistryError(
                f"公開鍵長が不正(ed25519 は {_ED25519_PUBLIC_KEY_LEN} バイト): {public_key_id}"
            )
        return raw


def _semver_key(version: str) -> tuple:
    """version 文字列を比較可能なキーに変換する(最新版解決用の簡易版)。

    MAJOR.MINOR.PATCH の数値比較のみ。prerelease/build は semver 規則の優先順位を厳密に
    実装せず「prerelease は同じ数値版より小さい」までを表現する(MVP の最新版選択に十分)。
    解釈できない要素は 0 として安全側に倒す。
    """
    core = version.split("+", 1)[0]
    main, _, pre = core.partition("-")
    parts = main.split(".")
    nums = []
    for i in range(3):
        try:
            nums.append(int(parts[i]) if i < len(parts) else 0)
        except ValueError:
            nums.append(0)
    # prerelease あり(pre 非空)は同じ数値版のリリースより小さい(=1 が大きい)。
    return (nums[0], nums[1], nums[2], 0 if pre else 1)
