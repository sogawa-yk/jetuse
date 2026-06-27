"""`kind: external-app` の contributes 詳細スキーマと OIDC SSO ブリッジ最小実装（ASSET-01）。

伝ぴょん（denpyon）のような **外部アプリ連携** を JetUse の配布表現へ正規化する。コネクタ
（`kind: connector` / L2 MCP）が「外部 SaaS の API を呼び出す」のに対し、external-app は「外部
アプリの **UI そのもの**（独自フロント）を JetUse に **埋め込む**（iframe / link）＋ **OIDC SSO**
する」オンボード方式を表す（方式比較は docs/verification/ASSET-01.md）。

設計方針（CON-01 connector と同じ「実シークレットを持たない」契約を踏襲する）:
  - `embed`（iframe | link）＋ `url`（外部アプリの HTTPS エンドポイント）。url 検証は
    **オフライン・決定的**（DNS 解決しない。https・公開ホスト・private/loopback 拒否・認証値禁止）。
  - `sso`（任意）= **OIDC SSO ブリッジ**宣言。issuer（IdP）・`clientIdRef`・`secretRef`
    （client_secret の **論理参照名**＝ Vault 束ね対象）・audience・scopes・claimMapping。
    **実 client_secret / 実トークンは一切持たない**（参照名のみ。実値は install 時に Vault 束ね）。
  - `build_sso_handoff` は **SSO ブリッジ最小実装**。決定的・オフライン（IdP へ実通信しない）で
    RFC 8693 token-exchange 要求の **shape** ＋ claimMapping 適用済みクレームを組み立てる。
    実シークレット値・実 subject_token を持たず参照名のみで配管する（fail-closed）。実 IdP 接続・
    実 client_secret 投入・実トークン発行は人間ゲート（SSO 実設定）。

`contributes["external-app"]` は JSON 配布物であり、検証済み manifest が正準 JSON 化できる
（manifest._assert_json_value）。実 SSO 配線・合成への組込は後段。
"""

from __future__ import annotations

import re
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .connector import _validate_mcp_endpoint
from .manifest import _assert_json_value, register_contributes_validator

# --- 語彙（仕様の正本） ----------------------------------------------------

#: 埋め込み方式。iframe = JetUse 画面内に枠で埋め込む / link = 別タブ/別ウィンドウで開く導線。
EmbedMode = Literal["iframe", "link"]
EMBED_MODES = frozenset(get_args(EmbedMode))

#: SSO 方式。現状 OIDC のみ（SAML 等は後段）。
SsoMode = Literal["oidc"]
SSO_MODES = frozenset(get_args(SsoMode))

#: RFC 8693 token-exchange の grant_type / token type（SSO ブリッジが組み立てる要求の shape）。
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

#: 長さ・件数上限（肥大定義による浪費を防ぐ）。
MAX_KEY_LEN = 64
MAX_TITLE_LEN = 200
MAX_URL_LEN = 2048
MAX_SCOPES = 50
MAX_SCOPE_LEN = 128
MAX_CLAIM_MAP = 50
MAX_CLAIM_NAME_LEN = 128

#: 参照名（clientIdRef / secretRef）の形式。小文字英数とハイフン/アンダースコア（端は英数）。
#: 実値（client_secret 等）を誤って入れにくくするため「名前らしい識別子」に限定する。
_REF_RE = r"^[a-z][a-z0-9_-]*[a-z0-9]$|^[a-z]$"
#: OIDC クレーム名（subject / 宛先）。OIDC/JWT のクレーム名に倣い英数・アンダースコア・ハイフン。
_CLAIM_RE = r"^[A-Za-z][A-Za-z0-9_-]*$"

#: claimMapping の **写像元**（subject クレーム名）に使えない資格情報系の名前（小文字比較）。
#: SSO ハンドオフは subject[src] の値を mapped_claims へそのまま載せるため、トークン/シークレットを
#: 運ぶクレーム名を写像元にすると実トークンが戻り値へ漏れ得る（contains_secret_values=False の
#: 不変条件を破る）。定義時点で fail-closed に塞ぐ（ASSET-01-MAJ-001）。身元属性（sub/email/
#: groups 等）の写像のみを許す。
#: 名前の部分一致で資格情報系とみなす語幹（小文字比較）。完全一致の列挙では `session_token` /
#: `jwt` / `sid` 等の未列挙名を取りこぼすため、語幹の部分一致で網を広げる（defense-in-depth）。
#: なお mapped_claims は **呼び出し側が与える身元属性** であり、秘密を入れない責務は呼び出し側にある
#: （contains_secret_values の意味は build_sso_handoff の docstring を参照）。本チェックは多層防御。
_CREDENTIAL_CLAIM_SUBSTRINGS = (
    "token",  # id_token / access_token / refresh_token / session_token / jwt 系の token
    "secret",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "privatekey",
    "api_key",
    "apikey",
    "assertion",
    "credential",
    "bearer",
)
#: 完全一致でのみ弾く短い機微名（部分一致だと正当な名前を巻き込むもの）。
_CREDENTIAL_CLAIM_EXACT = frozenset({"jwt", "sid", "otp", "code", "pin", "cred"})


def _is_credential_claim_name(name: str) -> bool:
    """クレーム名が資格情報系か（写像元として禁止すべきか）を判定する。"""
    low = name.lower()
    if low in _CREDENTIAL_CLAIM_EXACT:
        return True
    return any(sub in low for sub in _CREDENTIAL_CLAIM_SUBSTRINGS)


class ExternalAppError(ValueError):
    """external-app 定義が仕様に適合しないときに送出する。"""


class SsoHandoffError(ExternalAppError):
    """SSO ブリッジのハンドオフ組み立てに失敗したときに送出する（fail-closed）。"""


# --- サブモデル -----------------------------------------------------------


class OidcSsoBridge(BaseModel):
    """OIDC SSO ブリッジ宣言。**実 client_secret / 実トークンを持たない**（参照名のみ）。"""

    model_config = ConfigDict(extra="forbid")

    mode: SsoMode = "oidc"
    #: IdP（OIDC issuer）の HTTPS URL。token / authorize エンドポイントの基底。
    issuer: str = Field(min_length=1, max_length=MAX_URL_LEN)
    #: OIDC client_id の **論理参照名**（install 時に解決。値ではない）。
    client_id_ref: str = Field(alias="clientIdRef", min_length=1, max_length=MAX_KEY_LEN)
    #: client_secret の **論理参照名**（Vault 束ね対象。値ではない）。
    secret_ref: str = Field(alias="secretRef", min_length=1, max_length=MAX_KEY_LEN)
    #: token-exchange の audience（連携先アプリ＝伝ぴょん）。
    audience: str = Field(min_length=1, max_length=MAX_URL_LEN)
    #: token endpoint の明示指定（任意）。OIDC の token endpoint は IdP ごとに異なり issuer から
    #: 機械的に導出できない（Okta=/v1/token, Keycloak=/protocol/openid-connect/token,
    #: Azure=/oauth2/v2.0/token 等）。未指定なら build_sso_handoff は OIDC discovery URL を返し、
    #: token endpoint は discovery から解決する（ASSET-01: 誤った固定パスを生成しない）。
    token_endpoint: str | None = Field(
        default=None, alias="tokenEndpoint", max_length=MAX_URL_LEN
    )
    #: OIDC スコープ（openid を含むべき。重複不可）。
    scopes: list[str] = Field(default_factory=lambda: ["openid"], max_length=MAX_SCOPES)
    #: JetUse subject クレーム名 → 連携先アプリのクレーム名 への写像（SSO で渡す属性）。
    claim_mapping: dict[str, str] = Field(
        default_factory=dict, alias="claimMapping", max_length=MAX_CLAIM_MAP
    )

    @model_validator(mode="after")
    def _check(self) -> OidcSsoBridge:
        # issuer / audience は認証値を埋め込めない公開 HTTPS URL（DNS 解決しないオフライン検証）。
        try:
            _validate_mcp_endpoint(self.issuer)
        except ValueError as e:
            raise ValueError(f"sso.issuer が不正: {e}") from e
        try:
            _validate_mcp_endpoint(self.audience)
        except ValueError as e:
            raise ValueError(f"sso.audience が不正: {e}") from e
        if self.token_endpoint is not None:
            try:
                _validate_mcp_endpoint(self.token_endpoint)
            except ValueError as e:
                raise ValueError(f"sso.tokenEndpoint が不正: {e}") from e
        for ref_name, ref in (("clientIdRef", self.client_id_ref), ("secretRef", self.secret_ref)):
            if not re.fullmatch(_REF_RE, ref):
                raise ValueError(
                    f"sso.{ref_name} は参照名（小文字英数とハイフン/アンダースコア・"
                    f"{MAX_KEY_LEN}文字以内）でなければならない。実シークレット値は不可"
                )
        if not self.scopes:
            raise ValueError("sso.scopes は空にできない（最低 openid）")
        if "openid" not in self.scopes:
            raise ValueError("sso.scopes は openid を含まなければならない（OIDC）")
        seen: set[str] = set()
        for sc in self.scopes:
            if not sc.strip():
                raise ValueError("sso.scopes に空の要素がある")
            # 内部空白を禁止する: build_sso_handoff は scopes を空白結合するため、1要素に空白を
            # 含めると token-exchange 要求で別 scope として展開され、宣言外 scope の混入になる
            # （ASSET-01-MAJOR-002）。OIDC scope token は空白で区切る単一トークンであるべき。
            if sc != sc.strip() or any(c.isspace() for c in sc):
                raise ValueError(f"sso.scopes の要素に空白を含められない: {sc!r}")
            if len(sc) > MAX_SCOPE_LEN:
                raise ValueError(f"sso.scopes の要素が長すぎる: {sc!r}")
            if sc in seen:
                raise ValueError(f"sso.scopes が重複している: {sc!r}")
            seen.add(sc)
        # SSO を宣言する以上、最低1つの身元クレーム写像が必須（空だと mapped_claims={} で身元を
        # 渡せず SSO の契約に反する。ASSET-01-MAJOR-001）。不要なら sso=None に倒す。
        if not self.claim_mapping:
            raise ValueError(
                "sso.claimMapping は最低1つ必要（身元属性を渡さない SSO は無意味。"
                "SSO 不要なら sso=null にする）"
            )
        # claimMapping のキー（subject クレーム）・値（宛先クレーム）の形式を点検する。
        seen_dst: set[str] = set()
        for src, dst in self.claim_mapping.items():
            if not re.fullmatch(_CLAIM_RE, src) or len(src) > MAX_CLAIM_NAME_LEN:
                raise ValueError(f"sso.claimMapping のキー（subject クレーム名）が不正: {src!r}")
            # 資格情報系クレーム名を写像元にできない（実トークンの転写経路を塞ぐ・多層防御）。
            if _is_credential_claim_name(src):
                raise ValueError(
                    f"sso.claimMapping の写像元に資格情報系クレーム名は使えない: {src!r}"
                )
            dst_ok = (
                isinstance(dst, str)
                and re.fullmatch(_CLAIM_RE, dst)
                and len(dst) <= MAX_CLAIM_NAME_LEN
            )
            if not dst_ok:
                raise ValueError(f"sso.claimMapping の値（宛先クレーム名）が不正: {dst!r}")
            # 宛先クレーム名の重複を禁止する（_map_claims は mapped[dst]=val で代入するため、重複は
            # 後勝ちで身元属性を黙って上書きしてしまう。ASSET-01-MAJOR-001）。
            if dst in seen_dst:
                raise ValueError(f"sso.claimMapping の宛先クレーム名が重複している: {dst!r}")
            seen_dst.add(dst)
        return self


# --- ルート定義 -----------------------------------------------------------


class ExternalAppDefinition(BaseModel):
    """`contributes["external-app"]` のルート。埋め込み方式＋（任意で）OIDC SSO ブリッジ。"""

    model_config = ConfigDict(extra="forbid")

    #: 連携先アプリの安定キー（denpyon 等）。表示文言ではない。
    app: str = Field(
        min_length=1, max_length=MAX_KEY_LEN, pattern=r"^[a-z][a-z0-9_-]*[a-z0-9]$|^[a-z]$"
    )
    embed: EmbedMode
    #: 外部アプリの HTTPS エンドポイント（埋め込み先）。https・公開ホスト・認証値埋め込み禁止。
    url: str = Field(min_length=1, max_length=MAX_URL_LEN)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    #: OIDC SSO ブリッジ（任意）。None なら埋め込みのみ（SSO なし＝匿名/別途認証）。
    sso: OidcSsoBridge | None = None
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _check_url(self) -> ExternalAppDefinition:
        # url はオフライン・決定的に検証（https・公開ホスト literal・IP/loopback/localhost 拒否・
        # 認証値埋め込み禁止）。FQDN は DNS 解決しない（決定性。connector endpoint と同）。
        # ただし **url はサーバ側で fetch せず利用者ブラウザが iframe/link の src として読む**
        # （client-side embed）ため、connector invoke 時 SSRF ガード（DNS 解決を伴う公開判定）相当の
        # 後段は無い。内部 FQDN 埋め込みの抑止はネットワーク境界・iframe CSP/sandbox・IdP の
        # redirect_uri 許可リストで担保（ASSET-01-MAJOR-001）。
        try:
            _validate_mcp_endpoint(self.url)
        except ValueError as e:
            raise ValueError(f"external-app.url が不正: {e}") from e
        return self


# --- 公開 API: 定義検証 ----------------------------------------------------


def _coerce_definition(source: Any) -> dict[str, Any]:
    """manifest または contributes["external-app"] dict から定義 dict を取り出す。"""
    from .manifest import PluginManifest

    if isinstance(source, PluginManifest):
        if source.kind != "external-app":
            raise ExternalAppError(
                f"kind が 'external-app' でない manifest を検証できない: {source.kind}"
            )
        try:
            return source.contributes["external-app"]
        except KeyError as e:  # pragma: no cover - manifest 検証済みなら起きない
            raise ExternalAppError("contributes['external-app'] が無い") from e
    return source


def validate_external_app(source: Any) -> ExternalAppDefinition:
    """external-app 定義を検証して返す。不正なら ExternalAppError。"""
    data = _coerce_definition(source)
    try:
        return ExternalAppDefinition.model_validate(data)
    except ValidationError as e:
        raise ExternalAppError(str(e)) from e


def external_app_json_schema() -> dict[str, Any]:
    """external-app 定義（contributes["external-app"]）の JSON Schema（camelCase 別名）。"""
    return ExternalAppDefinition.model_json_schema(by_alias=True)


def _validate_external_app_contributes(payload: dict[str, Any]) -> None:
    """`validate_manifest()` の後段で呼ばれる contributes["external-app"] 詳細バリデータ。"""
    try:
        ExternalAppDefinition.model_validate(payload)
    except ValidationError as e:
        raise ValueError(f"contributes['external-app'] が不正: {e}") from e


# import 時に manifest.py のレジストリへ自身の詳細バリデータを登録する（依存反転で循環回避）。
register_contributes_validator("external-app", _validate_external_app_contributes)


# --- 公開 API: OIDC SSO ブリッジ最小実装 -----------------------------------


def _map_claims(mapping: dict[str, str], subject: dict[str, Any]) -> dict[str, Any]:
    """claimMapping（subject クレーム名 → 宛先クレーム名）を subject に適用する。

    写像元クレームが subject に無い／空のときは **fail-closed**（`SsoHandoffError`）。SSO は
    「正しい身元を確実に渡す」ことが要なので、欠落を黙って通さない。
    """
    mapped: dict[str, Any] = {}
    for src, dst in mapping.items():
        if src not in subject:
            raise SsoHandoffError(f"subject に必要なクレーム '{src}' が無い（SSO 写像不能）")
        val = subject[src]
        # 空判定は str だけでなく list/dict/tuple の空コレクションも fail-closed にする
        # （例: groups=[] を roles に写像しても身元を渡せていない。ASSET-01-MIN-001）。
        is_empty = (
            val is None
            or (isinstance(val, str) and not val.strip())
            or (isinstance(val, (list, dict, tuple, set)) and len(val) == 0)
        )
        if is_empty:
            raise SsoHandoffError(f"subject のクレーム '{src}' が空（SSO 写像不能）")
        # 値は JSON 化可能でなければならない（後段のレスポンス/証跡 JSON 化で落とさず、ここで
        # 一貫して fail-closed にする。set/bytes/非有限 float 等を拒否。ASSET-01-MINOR-001）。
        try:
            _assert_json_value(val, f"subject['{src}']")
        except ValueError as e:
            raise SsoHandoffError(
                f"subject のクレーム '{src}' が JSON 化できない値（SSO 写像不能）: {e}"
            ) from e
        mapped[dst] = val
    return mapped


def build_sso_handoff(
    definition: ExternalAppDefinition,
    subject: dict[str, Any],
    *,
    state: str,
    nonce: str,
    subject_token_ref: str = "jetuse-session-id-token",
) -> dict[str, Any]:
    """OIDC SSO ブリッジのハンドオフ要求を組み立てる（決定的・オフライン・実シークレット非保持）。

    JetUse は既に利用者を OIDC 認証済み（id_token は **参照名** `subject_token_ref` で参照する。
    本関数は実トークンを受け取らない＝漏らさない）。本関数はその身元を連携先アプリ（伝ぴょん）へ
    引き渡すための **RFC 8693 token-exchange 要求の shape** と、claimMapping を適用した受け渡し
    クレームを組み立てて返す。

    手順:
      1. `definition.sso` が無ければ fail-closed（SSO 未宣言のアプリはブリッジできない）。
      2. claimMapping を subject に適用（欠落クレームは fail-closed）。
      3. token-exchange 要求 shape（grant_type / audience / scope / client/secret は **参照名**・
         subject_token は **参照名**）と front-channel 受け渡し情報（embed/url/state/nonce）を返す。

    **secret 非保持契約の正確な意味**: `contains_secret_values=False` は「**JetUse が構築する部分**
    （token_exchange_request の client_id/client_secret/subject_token は参照名のみ）に実シークレット
    値・実トークンを **注入しない**」ことを表す。一方 `mapped_claims` は **呼び出し側が与える
    subject の身元属性**（SSO の本質＝認証済み利用者の身元を連携先へ渡す）であり、ここに秘密を
    入れない責務は呼び出し側にある。多層防御として、claimMapping の写像元に資格情報系クレーム名
    （token/secret/jwt/sid 等。`_is_credential_claim_name`）を使うことは定義検証で禁止する。

    実 IdP への実呼び出し・実 client_secret 投入・実 id_token 発行は人間ゲート（SSO 実設定）。
    """
    sso = definition.sso
    if sso is None:
        raise SsoHandoffError(
            f"external-app '{definition.app}' は sso を宣言していない（SSO ブリッジ不能）"
        )
    # 公開 API として、不正入力は型に依らず一貫して SsoHandoffError で fail-closed にする
    # （非文字列で AttributeError/TypeError に化けさせない。ASSET-01-MINOR-001）。
    if not isinstance(state, str) or not state.strip():
        raise SsoHandoffError("state は必須の文字列（CSRF 対策。呼び出し側が一意値を与える）")
    if not isinstance(nonce, str) or not nonce.strip():
        raise SsoHandoffError("nonce は必須の文字列（リプレイ対策。呼び出し側が一意値を与える）")
    if not isinstance(subject_token_ref, str):
        raise SsoHandoffError("subject_token_ref は文字列（参照名）でなければならない")
    if not isinstance(subject, dict):
        raise SsoHandoffError("subject は dict（認証済み利用者のクレーム）でなければならない")
    # subject_token_ref も **参照名**（client_id_ref / secret_ref と同形式）に限定する。呼び出し側が
    # 誤って実 id_token / JWT を渡しても、ここで fail-closed にして戻り値へ実トークンが載るのを防ぐ
    # （`contains_secret_values=False` の不変条件を守る。ASSET-MAJ-001）。JWT は '.' や大文字を
    # 含むため _REF_RE（小文字英数とハイフン/アンダースコアのみ）に弾かれる。
    if not re.fullmatch(_REF_RE, subject_token_ref) or len(subject_token_ref) > MAX_KEY_LEN:
        raise SsoHandoffError(
            "subject_token_ref は参照名（小文字英数とハイフン/アンダースコア・"
            f"{MAX_KEY_LEN}文字以内）でなければならない。実トークン値は不可"
        )

    mapped_claims = _map_claims(sso.claim_mapping, subject)

    # token-exchange 要求の shape。client_id / client_secret / subject_token は **参照名** で表し、
    # 実値は持たない（install 時に Vault 解決＝人間ゲート）。token endpoint は IdP ごとに異なり
    # issuer から機械的に導出できないため固定パスを生成しない（誤った /oauth2/token 生成を排除）。
    # tokenEndpoint 明示時はそれを使い、無ければ OIDC discovery URL を返して解決を委ねる。
    discovery_url = sso.issuer.rstrip("/") + "/.well-known/openid-configuration"
    token_exchange_request: dict[str, Any] = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "audience": sso.audience,
        "scope": " ".join(sso.scopes),
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "subject_token_type": ID_TOKEN_TYPE,
        # 実トークン/実シークレットではなく **参照名** のみを載せる。
        "subject_token_ref": subject_token_ref,
        "client_id_ref": sso.client_id_ref,
        "client_secret_ref": sso.secret_ref,
        # token endpoint は discovery から解決する（明示指定があれば下で上書き）。
        "discovery_url": discovery_url,
    }
    if sso.token_endpoint is not None:
        token_exchange_request["token_endpoint"] = sso.token_endpoint

    return {
        "app": definition.app,
        "mode": sso.mode,
        "embed": definition.embed,
        "url": definition.url,
        # front-channel の CSRF/リプレイ対策値（呼び出し側が一意に与える＝決定性は呼び出し側責務）。
        "state": state,
        "nonce": nonce,
        "token_exchange_request": token_exchange_request,
        # claimMapping を適用した受け渡しクレーム（利用者の身元属性。シークレットではない）。
        "mapped_claims": mapped_claims,
        # 実値を持たないことを示す不変条件（証跡で機械検査できるようにする）。
        "contains_secret_values": False,
    }
