"""`kind: connector` の contributes 詳細スキーマと合成バリデーション土台(CON-01)。

`manifest.py` は `kind` と `contributes` のキー対応までを強制する(L1)。本モジュールは
`kind: connector` の `contributes["connector"]` ペイロード——**L2 MCP コネクタ**(Slack 等の
SaaS を JetUse から呼び出すための正規化された MCP 接続宣言)——を pydantic で構造検証する
(spec 出典: docs/enhance/202607-demo-platform-plan.md §6 D9 / §10 `tool`=`connector`(L2 MCP) /
specs/16-platform.md §12)。

設計方針:
  - コネクタは「DB 認証情報を持たずにテナントデータ/外部 SaaS へ到達する唯一の正規経路」(plan §4-3)
    の L2 を担う。manifest は **接続方法(transport)＋公開操作(actions)＋必要な認証方式(auth)** を
    宣言するが、**認証の実値(トークン/シークレット)は一切持たない**。manifest が保持するのは
    ホストが install 時に Vault へ束ねる秘密の **参照名(secret_ref)** のみ(CLAUDE.md「認証実値を
    コミットしない」)。実シークレットは CON-02/03 の install 時に Vault(OCID)へ束ねる。
  - 本モジュールは **定義の妥当性** と **必要権限スコープの宣言抽出**(合成バリデーションの土台)に
    責務を限定する。実際の MCP 呼び出し(Responses API type:"mcp")・Slack 実装は CON-02、
    合成への組込(sample-app × AI 部品 × connector)＋ E2E は CON-03。
  - endpoint 検証は **オフライン・決定的**(DNS 解決しない)。https・公開ホスト literal までを
    構文検証し、実行時の完全な SSRF ガード(DNS 解決を伴う公開判定)は invoke 時(CON-03)。

`contributes["connector"]` は JSON 配布物であり、検証済み manifest が正準 JSON 化できる範囲
(manifest._assert_json_value)に収まる。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any, Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .manifest import PlatformScope, PluginManifest, register_contributes_validator

# --- 語彙(仕様の正本) ----------------------------------------------------

#: コネクタの接続方式。
#:   mcp     = 外部 HTTPS MCP サーバー(Responses API type:"mcp" で到達。endpoint 必須)。
#:   builtin = コア同梱でインプロセス実行されるコネクタ(Slack コア = CON-02。endpoint 禁止)。
ConnectorTransport = Literal["mcp", "builtin"]
CONNECTOR_TRANSPORTS = frozenset(get_args(ConnectorTransport))

#: 認証方式。none = 認証不要 / api_token = 単一トークン / oauth2 = OAuth2(provider スコープを伴う)。
#: いずれの値でも manifest は実シークレットを持たない(secret_ref = 参照名のみ)。
ConnectorAuthKind = Literal["none", "api_token", "oauth2"]
CONNECTOR_AUTH_KINDS = frozenset(get_args(ConnectorAuthKind))

#: 識別子(provider・action 名・secret_ref)の長さ上限と件数上限。肥大定義による浪費を防ぐ。
MAX_KEY_LEN = 64
MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 1000
MAX_ENDPOINT_LEN = 2048
MAX_ACTIONS = 100
MAX_AUTH_SCOPES = 50
MAX_PROVIDER_SCOPE_LEN = 128

#: provider / action 名 / secret_ref の形式。小文字 snake と数字・ハイフン(端は英数)。
_PROVIDER_RE = r"^[a-z][a-z0-9_-]*[a-z0-9]$|^[a-z]$"
_ACTION_RE = r"^[a-z][a-z0-9_]*$"
#: secret_ref は「参照名」であり実値ではない。名前らしい識別子に限定し、誤って値を入れにくくする。
_SECRET_REF_RE = r"^[a-z][a-z0-9_-]*[a-z0-9]$|^[a-z]$"


class ConnectorError(ValueError):
    """connector 定義が仕様に適合しないときに送出する。"""


def _validate_mcp_endpoint(endpoint: str) -> None:
    """MCP エンドポイント URL をオフライン・決定的に構文検証する(DNS 解決しない)。

    要求: https スキーム・ホスト名あり・明白な private/loopback literal の拒否、および
    **認証値を埋め込めない形**(userinfo / query / fragment 禁止)。コネクタは「認証実値を持たない」
    契約のため、`https://token@host/` や `?token=...` のような URL に秘密を紛れ込ませる経路を塞ぐ。
    完全な SSRF ガード(DNS 解決を伴う公開ホスト判定)は invoke 時(CON-03)。
    ここで弾くのは「定義時点で確実に不正」なものに限り、誤検知で正当な公開 FQDN を落とさない。
    """
    p = urlparse(endpoint)
    if p.scheme != "https":
        raise ValueError("connector.endpoint は https でなければならない")
    # userinfo(user:password@)に認証実値を埋め込む経路を塞ぐ(認証は auth.secretRef 経由のみ)。
    if p.username or p.password:
        raise ValueError("connector.endpoint に userinfo(認証情報)を含められない")
    # query/fragment に ?token=... 等で秘密を紛れ込ませる経路を塞ぐ(MCP 基底 URL に不要)。
    if p.query or p.fragment:
        raise ValueError("connector.endpoint に query/fragment は含められない")
    # port を明示参照して不正(非数値/範囲外)を弾く(urlparse は .port 参照時に ValueError を出す)。
    try:
        _ = p.port
    except ValueError as e:
        raise ValueError(f"connector.endpoint のポートが不正: {e}") from e
    host = p.hostname
    if not host:
        raise ValueError("connector.endpoint にホスト名が無い")
    if host.lower() == "localhost":
        raise ValueError("connector.endpoint に localhost は使えない")
    # IP literal のときのみ「公開ユニキャスト以外」を拒否する(FQDN は解決せず通す)。
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 標準形でない → 非正規 IPv4 表記(10進 2130706433 / 短縮 127.1 / 16進 0x7f000001)を
        # socket.inet_aton で正規 IPv4 に展開して判定する(ipaddress はこれらを解さず FQDN 扱いに
        # なるため、内部宛をすり抜ける。これらは connect 時に IPv4 として解決される)。
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            return  # 真の FQDN。定義時点ではこれ以上判定しない(invoke 時に解決して検証)。
    # IPv4-mapped IPv6(例 ::ffff:127.0.0.1)は mapped 側 IPv4 で判定する(IPv6 形のままだと
    # is_global/is_loopback が期待どおり立たず内部宛をすり抜けるため)。
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # is_global=False(private/loopback/link-local/reserved/unspecified/CGNAT 100.64/10 等)と
    # multicast を一括で拒否し、公開到達可能なユニキャストのみ許可する。
    if ip.is_multicast or not ip.is_global:
        raise ValueError(f"connector.endpoint は公開ユニキャスト IP のみ可: {host}")


# --- サブモデル -----------------------------------------------------------


class ConnectorAuth(BaseModel):
    """コネクタが要求する認証方式の宣言。**実シークレットは持たない**(secret_ref = 参照名のみ)。"""

    model_config = ConfigDict(extra="forbid")

    kind: ConnectorAuthKind
    #: 外部 SaaS 側のスコープ(例 Slack の `chat:write`)。Platform スコープではない自由文字列。
    #: oauth2 のときのみ意味を持つ(他の kind では空でなければならない)。
    scopes: list[str] = Field(default_factory=list, max_length=MAX_AUTH_SCOPES)
    #: ホストがインストール時に Vault へ束ねる秘密の **参照名**。値ではない。kind!=none のとき必須。
    secret_ref: str | None = Field(default=None, alias="secretRef")

    @model_validator(mode="after")
    def _check_auth(self) -> ConnectorAuth:
        if self.kind == "none":
            if self.secret_ref is not None:
                raise ValueError("auth.kind=none のとき secretRef は持てない")
            if self.scopes:
                raise ValueError("auth.kind=none のとき scopes は空でなければならない")
            return self
        # api_token / oauth2 は秘密の参照名が必須(実値ではなく名前)。
        if not self.secret_ref or not self.secret_ref.strip():
            raise ValueError(f"auth.kind={self.kind} のとき secretRef(参照名)は必須")
        if not re.fullmatch(_SECRET_REF_RE, self.secret_ref) or len(self.secret_ref) > MAX_KEY_LEN:
            raise ValueError(
                "auth.secretRef は参照名(小文字英数とハイフン/アンダースコア・"
                f"{MAX_KEY_LEN}文字以内)でなければならない。実シークレット値は不可"
            )
        if self.kind == "api_token" and self.scopes:
            raise ValueError("auth.kind=api_token のとき scopes は空でなければならない")
        # provider スコープの形式・重複を点検(空白のみ・長すぎる文字列を弾く)。
        seen: set[str] = set()
        for sc in self.scopes:
            if not sc.strip():
                raise ValueError("auth.scopes に空の要素がある")
            if len(sc) > MAX_PROVIDER_SCOPE_LEN:
                raise ValueError(f"auth.scopes の要素が長すぎる: {sc!r}")
            if sc in seen:
                raise ValueError(f"auth.scopes が重複している: {sc!r}")
            seen.add(sc)
        return self


class ConnectorAction(BaseModel):
    """コネクタが公開する正規化操作(L2 MCP のツールに対応)。

    `permissions` はこの操作がホストの Platform API から要求するスコープ(manifest.permissions の
    部分集合でなければならない。整合は `validate_connector_composition` が判定)。SaaS だけを叩く
    純粋なブリッジ操作は空でよい(プラットフォームデータに触れる操作のみスコープを要求する)。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=_ACTION_RE)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    permissions: list[PlatformScope] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dup_permissions(self) -> ConnectorAction:
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError(f"action '{self.name}': permissions に重複がある")
        return self


# --- ルート定義 -----------------------------------------------------------


class ConnectorDefinition(BaseModel):
    """`contributes["connector"]` のルート。L2 MCP コネクタの接続方法＋操作＋認証方式。"""

    model_config = ConfigDict(extra="forbid")

    #: 接続先 SaaS の識別子(slack / teams / jira ...)。表示文言ではなく安定キー。
    provider: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=_PROVIDER_RE)
    transport: ConnectorTransport
    #: transport=mcp のとき必須(https・公開ホスト literal)。builtin のとき禁止。
    endpoint: str | None = Field(default=None, max_length=MAX_ENDPOINT_LEN)
    auth: ConnectorAuth
    actions: list[ConnectorAction] = Field(min_length=1, max_length=MAX_ACTIONS)
    #: 表示用の説明(任意)。
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _check_transport_and_actions(self) -> ConnectorDefinition:
        if self.transport == "mcp":
            if not self.endpoint:
                raise ValueError("transport=mcp のとき endpoint は必須")
            _validate_mcp_endpoint(self.endpoint)
        elif self.endpoint is not None:
            # builtin はインプロセス実行(コア同梱)。外部エンドポイントを持たない。
            raise ValueError("transport=builtin のとき endpoint は持てない")

        names = [a.name for a in self.actions]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"action 名が重複: {dup}")
        return self


# --- 公開 API: 定義検証 ----------------------------------------------------


def _coerce_definition(source: PluginManifest | dict[str, Any]) -> dict[str, Any]:
    """manifest または contributes["connector"] dict から定義 dict を取り出す。"""
    if isinstance(source, PluginManifest):
        if source.kind != "connector":
            raise ConnectorError(
                f"kind が 'connector' でない manifest を検証できない: {source.kind}"
            )
        try:
            return source.contributes["connector"]
        except KeyError as e:  # pragma: no cover - manifest 検証済みなら起きない
            raise ConnectorError("contributes['connector'] が無い") from e
    return source


def validate_connector(source: PluginManifest | dict[str, Any]) -> ConnectorDefinition:
    """connector 定義を検証して返す。不正なら ConnectorError。

    引数は検証済み `PluginManifest`(kind=connector)か、`contributes["connector"]` 相当の dict。
    """
    data = _coerce_definition(source)
    try:
        return ConnectorDefinition.model_validate(data)
    except ValidationError as e:
        raise ConnectorError(str(e)) from e


def connector_json_schema() -> dict[str, Any]:
    """connector 定義(contributes["connector"])の JSON Schema(camelCase 別名)。"""
    return ConnectorDefinition.model_json_schema(by_alias=True)


def _validate_connector_contributes(payload: dict[str, Any]) -> None:
    """`validate_manifest()` の後段で呼ばれる contributes["connector"] 詳細バリデータ。

    不正なら ValueError を送出する(pydantic の after-validator 内で ValidationError 化され、
    最終的に `validate_manifest()` の ManifestError になる)。これにより kind=connector は
    **公開入口 validate_manifest() の時点で**詳細違反・認証値混入(endpoint userinfo 等)を弾く。
    """
    try:
        ConnectorDefinition.model_validate(payload)
    except ValidationError as e:
        raise ValueError(f"contributes['connector'] が不正: {e}") from e


# import 時に manifest.py のレジストリへ自身の詳細バリデータを登録する(依存反転で循環回避)。
register_contributes_validator("connector", _validate_connector_contributes)


# --- 公開 API: 合成バリデーション土台 --------------------------------------


def required_permissions(definition: ConnectorDefinition) -> set[str]:
    """このコネクタが要求する Platform API スコープの集合(actions の和集合)。"""
    perms: set[str] = set()
    for action in definition.actions:
        perms.update(action.permissions)
    return perms


class ConnectorCompositionReport(BaseModel):
    """コネクタ合成バリデーション結果。`ok` は致命的不整合が無いことを表す。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    provider: str
    transport: str
    actions: list[str]
    required_permissions: list[str]
    #: action が要求するが manifest.permissions で宣言されていないスコープ(致命: 宣言整合違反)。
    undeclared_permissions: list[str]
    #: manifest.permissions のうちどの action からも使われないスコープ(警告)。
    unused_permissions: list[str]
    #: 認証が必要か(kind!=none)。True ならホストは install 時に secret_ref を Vault へ束ねる。
    requires_secret: bool
    #: ホストが束ねるべき秘密の参照名(requires_secret のとき非 None。**実値ではない**)。
    secret_ref: str | None


class ConnectorCompositionError(ConnectorError):
    """コネクタ合成で致命的不整合を検出したときに送出する。`report` に詳細を持つ。"""

    def __init__(self, report: ConnectorCompositionReport):
        self.report = report
        super().__init__(
            "コネクタ合成バリデーション失敗: "
            f"undeclared_permissions={report.undeclared_permissions}"
        )


def validate_connector_composition(
    manifest: PluginManifest,
    *,
    definition: ConnectorDefinition | None = None,
) -> ConnectorCompositionReport:
    """コネクタをホストインスタンスへ合成可能か判定する(土台)。

    - action が要求するスコープが manifest.permissions に宣言されていなければ
      `undeclared_permissions`(宣言整合違反 = 致命)。
    - manifest.permissions のうちどの action も使わないものは `unused_permissions`(警告)。
    - `requires_secret`/`secret_ref`: 認証が必要なら、ホストはインストール時に参照名の秘密を
      Vault へ束ねる必要がある(本タスクでは束ね自体は行わない = CON-02/03)。
    - `ok` は undeclared_permissions が空のとき True。

    本関数は副作用を持たない(DB に触れない)。許可組合せ・テナント境界等の本格的な合成検証は
    ステージ2 HBD-04 / CON-03。
    """
    if manifest.kind != "connector":
        raise ConnectorError(
            f"kind が 'connector' でない manifest は合成できない: {manifest.kind}"
        )
    if definition is None:
        definition = validate_connector(manifest)
    req_perms = required_permissions(definition)
    declared = set(manifest.permissions)
    undeclared = sorted(req_perms - declared)
    unused = sorted(declared - req_perms)
    requires_secret = definition.auth.kind != "none"
    return ConnectorCompositionReport(
        ok=not undeclared,
        provider=definition.provider,
        transport=definition.transport,
        actions=[a.name for a in definition.actions],
        required_permissions=sorted(req_perms),
        undeclared_permissions=undeclared,
        unused_permissions=unused,
        requires_secret=requires_secret,
        secret_ref=definition.auth.secret_ref if requires_secret else None,
    )
