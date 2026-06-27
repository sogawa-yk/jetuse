"""公開フロー(PLG-05 / D7 / MKT-01): 既存 UC/Agent/sample-app/connector 定義を manifest 化し、
発行者鍵で署名して中央レジストリ(PLG-04)の publish API へ送る。

MKT-01 でマーケット流通を **L2 kind(sample-app / connector)** に拡張した。usecase/agent と同じ
署名・版固定・出所追跡の枠組みをそのまま使い、kind 固有の写像
(`manifest_from_sample_app` / `manifest_from_connector`)を追加する。L2 kind は contributes が
要求する Platform スコープ(aiSlots / actions の permissions)を **manifest.permissions に導出宣言**
する(取込時の合成バリデーションが宣言整合 undeclared_permissions=空 を要求するため)。

設計(§6 D7「発行者ID＋ed25519署名付き直接公開」):
  builder(web) → /api/{usecases|agents}/{id}/publish(route) → ここ:
    1. 既存定義(get_usecase / get_agent の戻り)を **配布表現の未署名 manifest** に写像する
       (`manifest_from_usecase` / `manifest_from_agent`)。
    2. 発行者の ed25519 秘密鍵で **canonical_signing_payload を対象に署名**する(`sign_manifest`)。
       署名対象・正準化は PLG-01(manifest.py)を再利用し、別実装 publisher と同一バイト列を保つ。
    3. レジストリの publish API(`POST /registry/plugins`)へ署名済み manifest を送る
       (`RegistryPublishClient`)。事前に発行者公開鍵を冪等登録する(D7 の検証鍵)。

manifest の `contributes[kind]` は **取込側(installer._ingest_contributes)がそのまま定義として
読める形**にする(usecase=fields/template/model、agent=instructions/model/enabled_tools/framework)。
表示メタ(name/description/icon/tags)は manifest トップレベルに置く(取込時に既定として注入される)。

秘密(署名鍵・トークン)はリポジトリにコミットしない。すべて `.env`/Vault で注入し、未設定なら
`PublisherConfigError`(route 側で 503)に倒す。plugin id の namespace/name セグメントは PLG-01 の
`ID_PATTERN`(`[a-z0-9-]`、端はハイフン不可)に収める(検証を通らない id を作らない)。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .manifest import (
    ID_RE,
    MAX_ID_LEN,
    SCHEMA_VERSION,
    SIGNATURE_ALGORITHM,
    ManifestError,
    canonical_signing_payload,
    validate_manifest,
)

#: ホスト JetUse の最低バージョン既定(manifest.jetuse.minVersion)。設定で上書き可。
DEFAULT_MIN_VERSION = "0.3.0"

#: ed25519 秘密シードのバイト長(raw private key = 32 バイト)。
_ED25519_SEED_LEN = 32

#: id セグメント(namespace / name)の最大長。id 全体 <= MAX_ID_LEN に収める保険でセグメントも制限。
_MAX_SEGMENT_LEN = 100


class PublisherConfigError(RuntimeError):
    """発行者設定(鍵・トークン・URL 等)が未設定/不正で publish できないときに送出する。"""


class PublishError(RuntimeError):
    """レジストリ publish が失敗したときに送出する。`status` に元の HTTP ステータスを保持する。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- 発行者設定 -----------------------------------------------------------


@dataclass(frozen=True)
class PublisherConfig:
    """発行者としてレジストリへ publish するための資格情報(env / .env 由来)。

    すべて秘匿前提(鍵・トークン)で、リポジトリにコミットしない。`namespace` は plugin id の
    名前空間で、空なら `publisher` を使う(慣習: id = `<publisher>/<name-slug>`)。
    """

    publisher: str
    public_key_id: str
    signing_key_b64: str
    token: str
    registry_url: str
    namespace: str = ""
    min_version: str = DEFAULT_MIN_VERSION

    @classmethod
    def from_settings(cls, settings: Any) -> PublisherConfig:
        """jetuse_core.settings.Settings から発行者設定を読む(欠落値は空文字のまま)。"""
        return cls(
            publisher=getattr(settings, "registry_publisher_id", "") or "",
            public_key_id=getattr(settings, "registry_public_key_id", "") or "",
            signing_key_b64=getattr(settings, "registry_signing_key", "") or "",
            token=getattr(settings, "registry_publisher_token", "") or "",
            registry_url=getattr(settings, "registry_publish_url", "") or "",
            namespace=getattr(settings, "registry_namespace", "") or "",
            min_version=getattr(settings, "registry_min_version", "")
            or DEFAULT_MIN_VERSION,
        )

    @property
    def id_namespace(self) -> str:
        return self.namespace or self.publisher

    def require_complete(self) -> None:
        """publish に必要な設定が揃っているか検証する。欠落は PublisherConfigError。"""
        missing = [
            name
            for name, val in (
                ("registry_publisher_id", self.publisher),
                ("registry_public_key_id", self.public_key_id),
                ("registry_signing_key", self.signing_key_b64),
                ("registry_publisher_token", self.token),
                ("registry_publish_url", self.registry_url),
            )
            if not val.strip()
        ]
        if missing:
            raise PublisherConfigError(
                "発行者設定が未設定のため公開できません(.env で設定してください): "
                + ", ".join(missing)
            )
        # namespace は id セグメントに使うため、PLG-01 の規則に収まるか前倒しで検証する。
        if not ID_RE.match(f"{self.id_namespace}/x"):
            raise PublisherConfigError(
                f"namespace '{self.id_namespace}' は plugin id の名前空間に使えません"
                "([a-z0-9-]、端はハイフン不可)"
            )

    def private_key(self):
        """署名用 ed25519 秘密鍵を返す。不正な鍵は PublisherConfigError。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        try:
            seed = base64.b64decode(self.signing_key_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise PublisherConfigError(f"署名鍵が base64 ではありません: {e}") from e
        if len(seed) != _ED25519_SEED_LEN:
            raise PublisherConfigError(
                f"署名鍵は {_ED25519_SEED_LEN} バイトの ed25519 秘密シード(base64)が必要です"
            )
        return Ed25519PrivateKey.from_private_bytes(seed)

    def public_key_b64(self) -> str:
        """署名鍵に対応する raw 公開鍵(32 バイト)の base64。レジストリ鍵登録に使う。"""
        return base64.b64encode(
            self.private_key().public_key().public_bytes_raw()
        ).decode("ascii")


# --- id / manifest 構築 ---------------------------------------------------


def slugify_segment(text: str, fallback: str) -> str:
    """文字列を plugin id のセグメント(`[a-z0-9-]`、端はハイフン不可)に正規化する。

    英数字以外はハイフンに畳み、両端のハイフンを除く。英数字が残らなければ `fallback` を使う。
    日本語のみの名前(畳むと空)でも必ず有効なセグメントを返す。
    """
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:_MAX_SEGMENT_LEN].strip("-")
    if not s:
        s = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")[:_MAX_SEGMENT_LEN]
    return s


def build_plugin_id(namespace: str, name: str, *, entity_id: str, kind: str) -> str:
    """`<namespace>/<name-slug>` 形式の plugin id を作る。

    name から作ったスラッグが空になる場合は `<kind>-<entity_id 先頭8>` に倒して一意性を保つ。
    同一エンティティの再公開(版上げ)で id が安定するよう、name/entity_id から決定的に導く。
    """
    fallback = f"{kind}-{re.sub(r'[^a-z0-9]', '', entity_id.lower())[:8] or '0'}"
    name_seg = slugify_segment(name, fallback)
    plugin_id = f"{namespace}/{name_seg}"
    if len(plugin_id) > MAX_ID_LEN:  # 念のため(セグメント長で実質起きないが防御的)。
        name_seg = name_seg[: MAX_ID_LEN - len(namespace) - 1].strip("-") or fallback
        plugin_id = f"{namespace}/{name_seg}"
    return plugin_id


def _clean_meta(definition: dict[str, Any]) -> dict[str, Any]:
    """manifest トップレベルの表示メタ(name/description/icon/tags)を定義から取り出す。"""
    name = str(definition.get("name") or "").strip()
    description = str(definition.get("description") or "")
    icon = definition.get("icon")
    tags = [str(t) for t in (definition.get("tags") or []) if str(t).strip()]
    return {"name": name, "description": description, "icon": icon, "tags": tags}


def manifest_from_usecase(
    definition: dict[str, Any],
    *,
    version: str,
    publisher: str,
    namespace: str,
    public_key_id: str,
    entity_id: str = "",
    min_version: str = DEFAULT_MIN_VERSION,
) -> dict[str, Any]:
    """UC 定義(get_usecase の戻り)を未署名の配布 manifest(camelCase dict)に写像する。

    contributes.usecase は **取込側がそのまま UC 定義として読める形**(fields/template/model)。
    name/description/icon/tags は manifest トップレベルに置く(取込時に payload へ既定注入される)。
    """
    meta = _clean_meta(definition)
    contributes: dict[str, Any] = {
        "fields": definition.get("fields") or [],
        "template": definition.get("template") or "",
    }
    if definition.get("model"):
        contributes["model"] = definition["model"]
    return _assemble_manifest(
        kind="usecase",
        contributes=contributes,
        meta=meta,
        version=version,
        publisher=publisher,
        namespace=namespace,
        public_key_id=public_key_id,
        entity_id=entity_id or str(definition.get("id") or ""),
        min_version=min_version,
    )


def manifest_from_agent(
    definition: dict[str, Any],
    *,
    version: str,
    publisher: str,
    namespace: str,
    public_key_id: str,
    entity_id: str = "",
    min_version: str = DEFAULT_MIN_VERSION,
) -> dict[str, Any]:
    """Agent 定義(get_agent の戻り)を未署名の配布 manifest(camelCase dict)に写像する。

    contributes.agent は取込側が定義として読める形(instructions/model/enabled_tools/framework…)。
    """
    meta = _clean_meta(definition)
    contributes: dict[str, Any] = {
        "instructions": definition.get("instructions") or "",
        "model": definition.get("model") or "",
        "enabled_tools": list(definition.get("enabled_tools") or []),
        "framework": definition.get("framework") or "openai_agents",
    }
    if definition.get("auto_tools") is not None:
        contributes["auto_tools"] = bool(definition.get("auto_tools"))
    return _assemble_manifest(
        kind="agent",
        contributes=contributes,
        meta=meta,
        version=version,
        publisher=publisher,
        namespace=namespace,
        public_key_id=public_key_id,
        entity_id=entity_id or str(definition.get("id") or ""),
        min_version=min_version,
    )


#: manifest トップレベル(配布メタ・署名・依存)に属するキー。L2 kind(sample-app/connector)を
#: publish する際、入力 definition から **contributes ペイロードに混ぜてはいけない** キー集合。
#: これ以外のキーを contributes[kind] に流し込む(camelCase/snake_case 両方を弾く)。
_MANIFEST_LEVEL_KEYS = frozenset(
    {
        "schemaVersion", "schema_version", "id", "version", "kind", "name",
        "description", "publisher", "jetuse", "requires", "permissions",
        "icon", "tags", "license", "signature",
    }
)


def _contributes_payload(definition: dict[str, Any]) -> dict[str, Any]:
    """definition dict から contributes ペイロード(manifest メタ以外の全キー)を取り出す。

    sample-app/connector の definition は「manifest メタ(name 等)＋ kind 固有ペイロード
    (screens/datasets/aiSlots, provider/transport/actions/auth 等)」を平坦に持つ。メタは
    `_clean_meta`/`permissions` で別途扱い、それ以外をそのまま contributes[kind] とする。
    """
    return {k: v for k, v in definition.items() if k not in _MANIFEST_LEVEL_KEYS}


def _explicit_permissions(definition: dict[str, Any]) -> set[str]:
    """definition が明示宣言する permissions(任意。必須スコープに上乗せできる)。"""
    return {str(p) for p in (definition.get("permissions") or []) if str(p).strip()}


def manifest_from_sample_app(
    definition: dict[str, Any],
    *,
    version: str,
    publisher: str,
    namespace: str,
    public_key_id: str,
    entity_id: str = "",
    min_version: str = DEFAULT_MIN_VERSION,
) -> dict[str, Any]:
    """sample-app 定義を未署名の配布 manifest(camelCase dict)に写像する。

    contributes["sample-app"] は取込側(installer→scaffold)がそのまま展開できる形
    (screens/datasets/aiSlots/summary)。`permissions` は aiSlots が要求する Platform スコープを
    導出して宣言する(取込時の合成バリデーションが宣言整合を要求するため、最小権限を漏れなく宣言)。
    definition が明示する permissions があれば和集合にする。
    """
    from . import sample_app

    meta = _clean_meta(definition)
    payload = _contributes_payload(definition)
    try:
        sa_def = sample_app.validate_sample_app(payload)
    except sample_app.SampleAppError as e:
        raise ManifestError(f"sample-app 定義が不正で manifest 化できません: {e}") from e
    perms = sample_app.required_permissions(sa_def) | _explicit_permissions(definition)
    return _assemble_manifest(
        kind="sample-app",
        contributes=payload,
        meta=meta,
        version=version,
        publisher=publisher,
        namespace=namespace,
        public_key_id=public_key_id,
        entity_id=entity_id or str(definition.get("id") or ""),
        min_version=min_version,
        permissions=sorted(perms),
    )


def manifest_from_connector(
    definition: dict[str, Any],
    *,
    version: str,
    publisher: str,
    namespace: str,
    public_key_id: str,
    entity_id: str = "",
    min_version: str = DEFAULT_MIN_VERSION,
) -> dict[str, Any]:
    """connector 定義を未署名の配布 manifest(camelCase dict)に写像する。

    contributes["connector"] は取込側(installer→connector_store)がそのまま登録できる形
    (provider/transport/endpoint/auth/actions)。`permissions` は actions が要求する Platform
    スコープを導出して宣言する(最小権限の宣言整合)。実シークレット値は **含めない**
    (definition には secret_ref = 参照名のみ。CON-01 の合成バリデータが実値混入を弾く)。
    """
    from . import connector

    meta = _clean_meta(definition)
    payload = _contributes_payload(definition)
    try:
        conn_def = connector.validate_connector(payload)
    except connector.ConnectorError as e:
        raise ManifestError(f"connector 定義が不正で manifest 化できません: {e}") from e
    perms = connector.required_permissions(conn_def) | _explicit_permissions(definition)
    return _assemble_manifest(
        kind="connector",
        contributes=payload,
        meta=meta,
        version=version,
        publisher=publisher,
        namespace=namespace,
        public_key_id=public_key_id,
        entity_id=entity_id or str(definition.get("id") or ""),
        min_version=min_version,
        permissions=sorted(perms),
    )


def _assemble_manifest(
    *,
    kind: str,
    contributes: dict[str, Any],
    meta: dict[str, Any],
    version: str,
    publisher: str,
    namespace: str,
    public_key_id: str,  # noqa: ARG001 (署名は sign_manifest で付与。引数は対称性のため受ける)
    entity_id: str,
    min_version: str,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    plugin_id = build_plugin_id(namespace, meta["name"], entity_id=entity_id, kind=kind)
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "id": plugin_id,
        "version": version,
        "kind": kind,
        "name": meta["name"],
        "description": meta["description"],
        "publisher": publisher,
        "jetuse": {"minVersion": min_version},
        # permissions は kind により異なる。usecase/agent は宣言不要(空)。L2 kind
        # (sample-app/connector)は contributes が要求するスコープを宣言する(取込時の合成
        # バリデーションが undeclared_permissions=空 を要求するため、ここで漏れなく宣言する)。
        "permissions": list(permissions or []),
        "tags": meta["tags"],
        "contributes": {kind: contributes},
    }
    if meta["icon"]:
        manifest["icon"] = meta["icon"]
    return manifest


def sign_manifest(unsigned: dict[str, Any], private_key, public_key_id: str) -> dict[str, Any]:
    """未署名 manifest dict に ed25519 署名を付けて返す(配布表現を保ったまま)。

    署名対象は PLG-01 の `canonical_signing_payload`(signature を除いた正準バイト列)。発行側・
    検証側(別実装含む)で同一バイト列を再現できる。manifest が不正なら ManifestError。
    """
    manifest = validate_manifest(unsigned)  # 署名前に構文検証(不正なら早期に倒す)。
    payload = canonical_signing_payload(manifest)
    signature = private_key.sign(payload)
    signed = dict(unsigned)
    signed["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "publicKeyId": public_key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def build_signed_manifest(
    config: PublisherConfig,
    *,
    kind: str,
    definition: dict[str, Any],
    version: str,
    entity_id: str = "",
) -> dict[str, Any]:
    """設定 + 定義から署名済み manifest を組み立てる(route / E2E 共通の入口)。"""
    config.require_complete()
    try:
        unsigned = _build_unsigned_manifest(
            config, kind=kind, definition=definition, version=version, entity_id=entity_id
        )
        return sign_manifest(unsigned, config.private_key(), config.public_key_id)
    except ManifestError as e:
        raise PublishError(f"定義から有効な manifest を作れませんでした: {e}") from e


def _build_unsigned_manifest(
    config: PublisherConfig,
    *,
    kind: str,
    definition: dict[str, Any],
    version: str,
    entity_id: str,
) -> dict[str, Any]:
    """kind に応じて未署名 manifest を組み立てる(build_signed_manifest の内部分岐)。"""
    if kind == "usecase":
        return manifest_from_usecase(
            definition,
            version=version,
            publisher=config.publisher,
            namespace=config.id_namespace,
            public_key_id=config.public_key_id,
            entity_id=entity_id,
            min_version=config.min_version,
        )
    elif kind == "agent":
        return manifest_from_agent(
            definition,
            version=version,
            publisher=config.publisher,
            namespace=config.id_namespace,
            public_key_id=config.public_key_id,
            entity_id=entity_id,
            min_version=config.min_version,
        )
    elif kind == "sample-app":
        return manifest_from_sample_app(
            definition,
            version=version,
            publisher=config.publisher,
            namespace=config.id_namespace,
            public_key_id=config.public_key_id,
            entity_id=entity_id,
            min_version=config.min_version,
        )
    elif kind == "connector":
        return manifest_from_connector(
            definition,
            version=version,
            publisher=config.publisher,
            namespace=config.id_namespace,
            public_key_id=config.public_key_id,
            entity_id=entity_id,
            min_version=config.min_version,
        )
    else:
        raise PublishError(f"公開に未対応の kind: {kind}")


# --- レジストリ publish HTTP クライアント ----------------------------------

#: transport: (method, url, json_body, headers) -> (status_code, parsed_json|text)
Transport = Callable[[str, str, dict[str, Any], dict[str, str]], "tuple[int, Any]"]

#: 通信往復の上限(秒)。レジストリ停止時に無限待ちしない。
DEFAULT_TIMEOUT = 15.0


def _http_transport(timeout: float) -> Transport:
    def post(method: str, url: str, body: dict[str, Any], headers: dict[str, str]):
        import httpx

        try:
            resp = httpx.request(method, url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            raise PublishError(f"レジストリへの接続に失敗しました: {e}") from e
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
        return resp.status_code, parsed

    return post


class RegistryPublishClient:
    """中央レジストリの publish/鍵登録エンドポイントを叩く発行者クライアント。

    `transport` を注入できる(テストは in-process の registry app を呼ぶ)。未注入なら httpx。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not base_url:
            raise PublisherConfigError("レジストリ publish URL が未設定です")
        self._base = base_url.rstrip("/")
        self._token = token
        self._post: Transport = transport or _http_transport(timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def register_public_key(self, public_key_id: str, public_key_b64: str) -> dict[str, Any]:
        """発行者公開鍵を登録する(冪等: 同一鍵の再登録は成功、別鍵への差し替えは 409)。"""
        status, body = self._post(
            "POST",
            f"{self._base}/registry/publishers/keys",
            {"publicKeyId": public_key_id, "publicKey": public_key_b64},
            self._headers(),
        )
        if status not in (200, 201):
            raise PublishError(
                f"公開鍵の登録に失敗しました(HTTP {status}): {_detail(body)}", status=status
            )
        return body if isinstance(body, dict) else {}

    def publish(self, signed_manifest: dict[str, Any]) -> dict[str, Any]:
        """署名済み manifest を publish する。成功で登録エントリ(dict)を返す。"""
        status, body = self._post(
            "POST", f"{self._base}/registry/plugins", signed_manifest, self._headers()
        )
        if status not in (200, 201):
            raise PublishError(
                f"公開に失敗しました(HTTP {status}): {_detail(body)}", status=status
            )
        return body if isinstance(body, dict) else {}

    def list_plugins(self) -> list[dict[str, Any]]:
        """登録済みプラグイン一覧(検証・E2E 用)。"""
        status, body = self._post(
            "GET", f"{self._base}/registry/plugins", {}, self._headers()
        )
        if status != 200:
            raise PublishError(f"一覧取得に失敗しました(HTTP {status})", status=status)
        return body.get("plugins", []) if isinstance(body, dict) else []


def _detail(body: Any) -> str:
    """エラー応答の本文から detail を取り出す(HTTPException の {detail: ...} を優先)。"""
    if isinstance(body, dict) and "detail" in body:
        d = body["detail"]
        return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False)


def publish_definition(
    *,
    kind: str,
    definition: dict[str, Any],
    version: str,
    entity_id: str = "",
    config: PublisherConfig,
    client: RegistryPublishClient | None = None,
    register_key: bool = True,
) -> dict[str, Any]:
    """定義を export→署名→publish する一連の流れ(route / E2E から呼ぶ)。

    `register_key=True` なら publish 前に発行者公開鍵を冪等登録する(初回 publish でも成立)。
    返り値はレジストリ登録エントリ + 署名済み manifest の id/version。
    """
    signed = build_signed_manifest(
        config, kind=kind, definition=definition, version=version, entity_id=entity_id
    )
    client = client or RegistryPublishClient(config.registry_url, config.token)
    if register_key:
        client.register_public_key(config.public_key_id, config.public_key_b64())
    entry = client.publish(signed)
    return {
        "id": signed["id"],
        "version": signed["version"],
        "kind": signed["kind"],
        "publisher": signed["publisher"],
        "entry": entry,
    }
