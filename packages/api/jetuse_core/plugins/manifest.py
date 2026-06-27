"""プラグイン manifest 仕様(L1 宣言型サブセット)と検証ロジック(PLG-01)。

配布可能なプラグインの最小単位を pydantic モデルとして定義し、JSON Schema を提供する。
仕様の正本は specs/16-platform.md、設計判断は docs/decisions/ADR-0013。

サポート範囲:
  - `kind` は `usecase` / `agent` / `sample-app`(SBA-01 で追加)。
    tool/hosted-app/bundle は後続タスク。
    sample-app の contributes 詳細スキーマ(screens/datasets/aiSlots)は `sample_app.py` が担う。
  - レジストリ通信・UI は含めない。署名は「フィールドの形式検証」＋「任意の ed25519 検証関数」まで。

manifest は配布時に camelCase JSON として表現される(`schemaVersion`, `jetuse.minVersion` 等)。
pydantic の alias で受理し、`model_dump(by_alias=True)` で同じ表現に戻す。

制約は可能な限り型(`Literal`)と `Field(pattern=...)` に寄せ、`manifest_json_schema()` が返す
JSON Schema にも const/enum/pattern が出るようにする(配布スキーマと実バリデータの乖離を防ぐ)。
型で表せない規則(permissions 重複・contributes と kind の対応)は validator で補う。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Callable
from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# --- 定数(仕様の正本) ----------------------------------------------------

#: manifest スキーマ自体の版。後方非互換な変更で繰り上げる。
SCHEMA_VERSION = "1"

#: サポートする配布種別の型。hosted-app(L3)/bundle は後続。
#: `sample-app`(scaffold テンプレ = §6 D9)は SBA-01 で追加した。詳細スキーマは
#: `jetuse_core.plugins.sample_app`(contributes["sample-app"] の構造検証)が担う。
#: `connector`(L2 MCP = §6 D9 / plan §10 `tool`=`connector`)は CON-01 で追加した。詳細スキーマは
#: `jetuse_core.plugins.connector`(contributes["connector"] の構造検証)が担う。
PluginKind = Literal["usecase", "agent", "sample-app", "connector"]
PLUGIN_KINDS = get_args(PluginKind)

#: Platform API ブローカー(§7)が発行するスコープの語彙。
#: manifest の `permissions` はこの集合の部分集合でなければならない。
PlatformScope = Literal[
    "platform:rag.search",
    "platform:db.query",
    "platform:conversations.read",
    "platform:files.read",
    "platform:files.write",
    "platform:connector.invoke",
]
PLATFORM_SCOPES = frozenset(get_args(PlatformScope))

#: 実 Platform API ルート(PAPI-03)が要求する scope の名前付き定数。各ルートが文字列直書きを
#: 避け、typo を import エラーで前倒し検出するための再エクスポート(語彙の正本は PlatformScope)。
PLATFORM_SCOPE_RAG_SEARCH = "platform:rag.search"
PLATFORM_SCOPE_DB_QUERY = "platform:db.query"
PLATFORM_SCOPE_CONVERSATIONS_READ = "platform:conversations.read"
PLATFORM_SCOPE_FILES_READ = "platform:files.read"
PLATFORM_SCOPE_FILES_WRITE = "platform:files.write"
PLATFORM_SCOPE_CONNECTOR_INVOKE = "platform:connector.invoke"

#: id は `namespace/name`。各セグメントは小文字英数とハイフン(端はハイフン不可)。
_ID_SEGMENT = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
ID_PATTERN = rf"^{_ID_SEGMENT}/{_ID_SEGMENT}$"
ID_RE = re.compile(ID_PATTERN)

#: 長さ上限。検証を通った manifest は必ず永続化できる(`installed_plugins`・取込定義の
#: `source_plugin_id`/`source_version` カラム = ADR-0013/PLG-02)ことを保証するため、
#: id/version に上限を設ける。値は DB カラム幅(VARCHAR2)と一致させる(乖離=保存時桁超過)。
MAX_ID_LEN = 255
MAX_VERSION_LEN = 64

#: semver.org 公式の正準正規表現(MAJOR.MINOR.PATCH[-prerelease][+build])。
#: ASCII 数字のみ([0-9]。`\d` は Python 正規表現で Unicode 数字も拾うため使わない)。
SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
SEMVER_RE = re.compile(SEMVER_PATTERN)

#: 署名アルゴリズム。中央レジストリ(D7)は ed25519 を採用。
SIGNATURE_ALGORITHM = "ed25519"


class ManifestError(ValueError):
    """manifest が仕様に適合しないときに送出する。pydantic の詳細を文字列で保持する。"""


# --- kind 別 contributes 詳細バリデータの登録機構 --------------------------
#
# `validate_manifest()` は L1 として「kind と contributes キーの対応」までを強制する。
# 一方、L2 の kind(connector 等)は `contributes[kind]` の詳細スキーマ違反や認証値混入を
# **公開入口である validate_manifest() の時点で**弾きたい(署名・レジストリ取込・保存の各経路が
# validate_manifest() のみを信頼しても安全であるように)。
#
# manifest.py が個別の詳細モジュール(connector.py 等)を import すると循環するため、依存を反転し、
# **詳細モジュール側が自分の validator をここへ登録**する(connector.py の import 時に register)。
# plugins パッケージ(__init__)は connector を import するため、実利用経路では必ず登録済みになる。
# validator は payload(dict)を受け取り、不正なら ValueError を送出(pydantic が ValidationError 化)。
_CONTRIBUTES_DETAIL_VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {}

# import 順に依存しないための保険: 登録がまだ無い L2 kind は、検証時に当該モジュールを遅延 import し
# 自己登録させる(manifest だけを import した経路でも詳細検証が必ず効く)。
# 遅延 import は「検証時」に起きるため、import 時の循環は発生しない。
_L2_DETAIL_MODULES: dict[str, str] = {"connector": "jetuse_core.plugins.connector"}


def register_contributes_validator(
    kind: str, validator: Callable[[dict[str, Any]], None]
) -> None:
    """kind の `contributes[kind]` 詳細バリデータを登録する(詳細モジュールが import 時に呼ぶ)。"""
    _CONTRIBUTES_DETAIL_VALIDATORS[kind] = validator


def _resolve_detail_validator(kind: str) -> Callable[[dict[str, Any]], None] | None:
    """kind の詳細バリデータを返す。未登録でも既知 L2 kind は遅延 import で確実に解決する。"""
    validator = _CONTRIBUTES_DETAIL_VALIDATORS.get(kind)
    if validator is None and kind in _L2_DETAIL_MODULES:
        import importlib

        importlib.import_module(_L2_DETAIL_MODULES[kind])  # モジュールが自己登録する
        validator = _CONTRIBUTES_DETAIL_VALIDATORS.get(kind)
    return validator


def _assert_json_value(v: Any, path: str) -> None:
    """値が JSON で表現できる範囲(オブジェクト/配列/文字列/数値/真偽/null)か再帰的に検証する。

    manifest は JSON 配布物であり、検証済み manifest は必ず正準 JSON 化できなければならない
    (canonical_signing_payload が落ちない保証)。bytes・任意オブジェクト・非有限数(NaN/Inf)を拒否。
    """
    if v is None or isinstance(v, (str, bool)):  # bool を int 分岐より前に明示的に許可。
        return
    if isinstance(v, int):  # bool は上で処理済み。ここは純粋な整数のみ。
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"{path}: 非有限の数値(NaN/Infinity)は JSON manifest で表現できない")
        return
    if isinstance(v, dict):
        for k, sub in v.items():
            if not isinstance(k, str):
                raise ValueError(f"{path}: オブジェクトのキーは文字列でなければならない")
            _assert_json_value(sub, f"{path}.{k}")
        return
    if isinstance(v, list):
        for i, sub in enumerate(v):
            _assert_json_value(sub, f"{path}[{i}]")
        return
    raise ValueError(f"{path}: JSON で表現できない値 ({type(v).__name__})")


# --- サブモデル -----------------------------------------------------------


class Requires(BaseModel):
    """インストール先に存在を要求する依存(取込時に解決可否を確認するための宣言)。"""

    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    datasources: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class JetUseConstraint(BaseModel):
    """ホストとなる JetUse 本体への制約。"""

    model_config = ConfigDict(extra="forbid")

    min_version: str = Field(alias="minVersion", pattern=SEMVER_PATTERN)


class Signature(BaseModel):
    """発行者の ed25519 署名(D7)。値の正否はレジストリ取得の公開鍵で別途検証する。"""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["ed25519"]
    #: 発行者公開鍵の識別子(レジストリ index.json の鍵と突き合わせる)。
    public_key_id: str = Field(alias="publicKeyId", min_length=1)

    @field_validator("public_key_id")
    @classmethod
    def _key_id_not_blank(cls, v: str) -> str:
        # 鍵 ID 照合に使うため空白のみを弾く(後段 lookup 失敗を前倒しで検出)。
        if not v.strip():
            raise ValueError("signature.publicKeyId は空白のみにできない")
        return v
    #: base64 エンコードした署名バイト列(64 バイトの ed25519 署名 = base64 で 88 文字)。
    value: str = Field(
        json_schema_extra={
            "contentEncoding": "base64",
            "description": "64 バイトの ed25519 署名を base64 した文字列(88 文字)",
        }
    )

    @field_validator("value")
    @classmethod
    def _b64(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"signature.value は base64 でなければならない: {e}") from e
        if len(raw) != 64:
            # ed25519 署名は 64 バイト固定。
            raise ValueError("signature.value は 64 バイトの ed25519 署名でなければならない")
        return v


# --- ルートモデル ---------------------------------------------------------


class PluginManifest(BaseModel):
    """配布可能なプラグインの宣言(L1 サブセット)。"""

    # 配布 manifest は camelCase の alias を正本とする。snake_case 名での受理は無効
    # (populate_by_name を立てない)ことで、JSON Schema の受理範囲と実バリデータを揃える。
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(alias="schemaVersion")
    id: str = Field(pattern=ID_PATTERN, max_length=MAX_ID_LEN)
    version: str = Field(pattern=SEMVER_PATTERN, max_length=MAX_VERSION_LEN)
    kind: PluginKind
    name: str = Field(min_length=1)
    description: str = ""
    publisher: str = Field(min_length=1)
    jetuse: JetUseConstraint
    requires: Requires = Field(default_factory=Requires)
    permissions: list[PlatformScope] = Field(
        default_factory=list, json_schema_extra={"uniqueItems": True}
    )
    #: kind に対応する宣言型ペイロード。キーは kind と一致する 1 つだけを持ち、値は object。
    #: 「キーが kind と一致」は cross-field 制約のため validator が正本(JSON Schema は
    #: maxProperties:1 までを表現)。
    contributes: dict[str, dict[str, Any]] = Field(
        json_schema_extra={"minProperties": 1, "maxProperties": 1}
    )
    icon: str | None = None
    tags: list[str] = Field(default_factory=list)
    license: str | None = None
    signature: Signature | None = None

    # --- 型で表せない規則の補完 ---

    @field_validator("name", "publisher")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # min_length=1 は空文字を弾くが空白のみは通るため、ここで明示的に弾く。
        if not v.strip():
            raise ValueError("空にできない")
        return v

    @field_validator("permissions")
    @classmethod
    def _no_dup_permissions(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("permissions に重複がある")
        return v

    @field_validator("contributes")
    @classmethod
    def _contributes_json_safe(cls, v: dict[str, Any]) -> dict[str, Any]:
        # 検証済み manifest が必ず正準 JSON 化できるよう、payload 内部を JSON value に限定する。
        _assert_json_value(v, "contributes")
        return v

    @model_validator(mode="after")
    def _contributes_matches_kind(self) -> PluginManifest:
        if self.kind not in self.contributes:
            raise ValueError(
                f"contributes は kind '{self.kind}' のキーを持たなければならない"
            )
        extra = set(self.contributes) - {self.kind}
        if extra:
            raise ValueError(
                f"contributes は kind '{self.kind}' のキーのみ持てる。余分: {sorted(extra)}"
            )
        # L2 kind(connector 等)は詳細バリデータで `contributes[kind]` を構造検証する。これにより
        # validate_manifest() 単体で詳細違反・認証値混入を弾く(公開入口の安全性・import 順非依存)。
        detail_validator = _resolve_detail_validator(self.kind)
        if detail_validator is not None:
            detail_validator(self.contributes[self.kind])
        return self


# --- 公開 API -------------------------------------------------------------


def validate_manifest(data: dict[str, Any]) -> PluginManifest:
    """dict から manifest を検証して返す。不正なら ManifestError を送出する。

    pydantic の ValidationError を ManifestError に包んで API を安定させる。
    """
    try:
        return PluginManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(str(e)) from e


def manifest_json_schema() -> dict[str, Any]:
    """配布・ドキュメント用の JSON Schema(camelCase 別名)を返す。

    型・Field の制約(const/enum/pattern)を反映するため、外部ツールがこのスキーマを
    正本として検証しても validate_manifest と概ね一致する(重複・kind 対応は型で表せず
    validator のみが担保する点に注意)。
    """
    return PluginManifest.model_json_schema(by_alias=True)


def canonical_signing_payload(manifest: PluginManifest) -> bytes:
    """署名対象の正準バイト列を返す。

    定義: **検証後の manifest から `signature` のみを除いた全フィールド**を正準 JSON 化する。
    既定値は注入済み(`requires`/`tags`/`permissions` 等)・任意フィールドの未指定は `null` として
    含める(`exclude_none` しない)。これにより「signature を除いた manifest」という仕様(specs/16 §6)
    と実装が一致し、別実装の publisher でも同じバイト列を再現できる。発行側・検証側で同一バイト列を
    得るため、キーをソートし区切りを固定する。
    """
    data = manifest.model_dump(by_alias=True)
    data.pop("signature", None)
    # allow_nan=False: 検証を迂回して構築した manifest に NaN/Infinity が紛れても、正準
    # 化の時点で ValueError に倒す(非 JSON 値で署名ペイロードを作れないようにする)。
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def verify_signature(manifest: PluginManifest, public_key: bytes) -> bool:
    """manifest の ed25519 署名を 32 バイトの公開鍵で検証する。

    セキュリティ境界として **fail-closed**: 署名なし・公開鍵が不正(型/長さ/未対応)・署名値が不正・
    検証失敗・その他あらゆる内部エラーのいずれでも例外を漏らさず False を返す。検証を迂回して
    構築した不正な PluginManifest(model_construct 等)を渡されても契約は崩れない。
    取込時(PLG-03)はここが False の manifest を拒否する。
    """
    try:
        signature = manifest.signature
        if signature is None:
            return False
        # 迂回構築(model_construct)で algorithm/value 属性が欠落していても、参照を try 内に
        # 置くことで AttributeError を漏らさず False に倒す(fail-closed 契約の完全化)。
        if signature.algorithm != SIGNATURE_ALGORITHM:
            # ed25519 以外が紛れても ed25519 として検証しない。
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(public_key)
        sig = base64.b64decode(signature.value, validate=True)
        key.verify(sig, canonical_signing_payload(manifest))
        return True
    except Exception:
        # fail-closed。どの失敗経路でも「検証できなかった=不可」に倒す。
        return False
