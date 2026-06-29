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

import hmac
import re
from collections.abc import Callable
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
        # shape と実 exchange（exchange_sso_token）で token type を一致させる（BE06-003）:
        # 連携先へ渡すのは身元 id_token なので requested=id_token、subject は JetUse セッションの
        # access token（Web の Bearer）なので subject=access_token。
        "requested_token_type": ID_TOKEN_TYPE,
        "subject_token_type": ACCESS_TOKEN_TYPE,
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


# --- 公開 API: 実 token-exchange 配線（BE-06） -----------------------------
#
# build_sso_handoff は「要求の shape」を **参照名のまま** 決定的に組み立てる（IdP へ通信しない）。
# 本節はその先＝**実 RFC 8693 token-exchange を実行する配線**を提供する。連携層（connector_runtime）
# と同じ「実シークレットを持たない／差し替え可能な継ぎ目で実値を解決する」契約を踏襲する:
#   - client_secret は `secret_resolver`（secretRef→実 client_secret。install 時 Vault 束ね＝
#     人間ゲート）。
#   - subject_token（JetUse の実 id_token）は呼び出し側がランタイムで与える（保存しない）。
#   - 実 IdP への HTTP は差し替え可能な `token_exchange_caller` 経由。**既定は fail-closed**（実通信
#     しない）。テスト/mock E2E は caller を注入。実 IdP 接続・実 client_secret 投入は人間ゲート。
# 戻り値・例外には **入力シークレット（client_secret / subject_token）を出さない**（最終防壁で
# redact）。

#: secretRef（参照名）→ 実 client_secret の解決関数。install 時の Vault 束ね（人間ゲート）に
#: 差し替えられる継ぎ目。**実値はここで初めて現れ、戻り値・例外・ログには出さない**。
SsoSecretResolver = Callable[[str], str]

#: token-exchange の実呼び出し。(token_endpoint, request_body) -> token レスポンス dict。
#: request_body は実 client_secret / 実 subject_token を含む（IdP へ送る本物の要求）。
#: 既定は fail-closed（実 IdP へ実通信しない）。テスト/E2E は mock を注入する。
TokenExchangeCaller = Callable[[str, dict[str, Any]], dict[str, Any]]

#: id_token の検証関数。(id_token, issuer, audience) -> **検証済み claims dict**。署名（JWKS）・
#: issuer・audience・exp 等を fail-closed で確認し、検証済みクレームを返す継ぎ目。返した claims の
#: nonce/sub を exchange_sso_token が「現在のトランザクション/認証利用者」へ束ねる（BE06-BLK-001）。
#: 検証不能（署名/iss/aud/exp 不正）は例外送出（呼出側 fail-closed）。**実 JWKS 取得は実 IdP 通信＝
#: 人間ゲート**のため既定は None。承認後に JWKS ベース verifier を注入する（BE06-004）。
IdTokenVerifier = Callable[[str, str, str], dict[str, Any]]

#: token-exchange レスポンスから取り出す発行トークンのキー候補（OIDC/OAuth 慣行）。
_ISSUED_TOKEN_KEYS = ("id_token", "access_token", "token")
#: redact 置換文字列（connector_runtime と同じ意匠）。
_REDACTED = "***redacted***"


def _redact_values(obj: Any, secrets: tuple[str, ...]) -> Any:
    """obj 内に出現する `secrets` の各文字列を再帰的に伏字へ置換する（str/dict/list/tuple 走査）。

    IdP/caller がエラー文や応答に client_secret / subject_token を echo しても、戻り値・例外文字列に
    入力シークレットが残らないための最終防壁（connector_runtime._redact_secret と同契約）。
    """
    live = tuple(s for s in secrets if s)
    if not live:
        return obj
    if isinstance(obj, str):
        out = obj
        for s in live:
            out = out.replace(s, _REDACTED)
        return out
    if isinstance(obj, dict):
        return {_redact_values(k, live): _redact_values(v, live) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_values(v, live) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_values(v, live) for v in obj)
    return obj


def _denied_token_exchange_caller(
    token_endpoint: str, body: dict[str, Any]
) -> dict[str, Any]:
    """既定 TokenExchangeCaller。実 IdP へは通信しない（SSO 実設定は人間ゲート）。

    テスト/mock E2E は caller を注入する。実 IdP 接続・実 client_secret 投入・実 id_token 発行は
    人間ゲート（Identity Domain＝テナンシ変更・Vault 束ね）。
    """
    raise SsoHandoffError(
        "token_exchange_caller が未設定。実 OIDC token-exchange は caller の注入が必要"
        "（実 IdP 接続・実 client_secret 投入・実トークン発行は人間ゲート＝SSO 実設定）"
    )


#: 実 token-exchange の HTTP timeout（connect, read）秒。Gateway/SSE 上限内に収める保守的値。
HTTP_TOKEN_EXCHANGE_TIMEOUT = (5, 15)


def http_token_exchange_caller(
    token_endpoint: str, body: dict[str, Any]
) -> dict[str, Any]:
    """本番 TokenExchangeCaller。OIDC token endpoint へ RFC 8693 token-exchange を実 HTTP で投げる。

    `body` は実 client_secret / 実 subject_token を含む application/x-www-form-urlencoded 要求。
    timeout を明示し、HTTP/OAuth エラー（非 2xx・OAuth error・不正 JSON）を `SsoHandoffError`
    へ正規化する（fail-closed）。**実 IdP 到達は人間ゲート**（SSO 実設定）であり、本関数を実 IdP に
    向けて呼ぶ前提（実 client_secret 投入・実 subject_token）が満たされるのは承認後。HTTP は httpx
    （本体の直接依存。BE06-REV-007）。secret は呼び出し側 exchange_sso_token が戻り値・例外から
    redact する。
    """
    import httpx

    try:
        resp = httpx.post(
            token_endpoint,
            data=body,
            headers={"Accept": "application/json"},
            timeout=HTTP_TOKEN_EXCHANGE_TIMEOUT,
            # redirect を無効化する（実 secret/subject_token を載せた要求が 3xx で別ホストへ
            # 再送されるのを防ぐ。SEC-001）。token endpoint は最終到達先のみ許す。
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        # 例外文に secret が載らないよう型名のみを出す（呼び出し側でも redact するが二重防御）。
        raise SsoHandoffError(
            f"token endpoint への接続に失敗: {type(e).__name__}"
        ) from None
    try:
        data = resp.json()
    except ValueError:
        raise SsoHandoffError(
            f"token-exchange 応答が JSON でない（status={resp.status_code}）"
        ) from None
    if not (200 <= resp.status_code < 300) or (isinstance(data, dict) and data.get("error")):
        # 2xx 以外は拒否する（redirect 無効化時に JSON を伴う 3xx を成功と誤認しない。BE06-R006）。
        # OAuth エラーは error/error_description を返す（実トークンは含まない想定）。
        err = data.get("error") if isinstance(data, dict) else None
        raise SsoHandoffError(
            f"token-exchange が拒否された（status={resp.status_code} error={err}）"
        )
    if not isinstance(data, dict):
        raise SsoHandoffError("token-exchange 応答は JSON オブジェクトでなければならない")
    return data


def jwks_id_token_verifier(jwks_url: str) -> IdTokenVerifier:
    """JWKS から発行 id_token の **署名/iss/aud/exp** を検証する verifier を作る（BE06-R002）。

    実 JWKS（実 IdP）への通信を伴うため `jwks_url` は実 IdP 設定（人間ゲート）で与える。検証は
    fail-closed: 署名不正・iss/aud 不一致・期限切れは PyJWT が例外送出し、呼び出し側
    （exchange_sso_token）が fail-closed。検証成功時は **検証済み claims（nonce/sub 含む）**を返す
    （exchange_sso_token がトランザクション/利用者束ねに使う。BE06-BLK-001）。
    """
    import jwt
    from jwt import PyJWKClient

    client = PyJWKClient(jwks_url, cache_keys=True)

    def _verify(token: str, issuer: str, audience: str) -> dict[str, Any]:
        key = client.get_signing_key_from_jwt(token)
        # exp/iat/iss/aud/sub の **存在を必須**にする（BE06-REV-002）。PyJWT の verify_exp は exp が
        # 在るときだけ検査するため、require で欠落自体を拒否する（exp 無しトークンを通さない）。
        # nonce も **存在必須**（exchange_sso_token がトランザクション束ねに使う。BE06-BLK-001）。
        claims: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=audience or None,
            issuer=issuer or None,
            options={
                "verify_aud": bool(audience),
                "require": ["exp", "iat", "iss", "aud", "sub", "nonce"],
            },
        )
        return claims

    return _verify


def _resolve_sso_secret(
    secret_resolver: SsoSecretResolver, ref: str, ref_label: str
) -> Any:
    """secretRef / clientIdRef を実値へ解決する（保護境界）。

    resolver の例外（未知 ref・Vault 権限拒否・一時障害）の文言・連鎖に実 secret や Vault 内部情報が
    混入し得るため、**型に依らず**（resolver が SsoHandoffError を投げても）固定文言の
    `SsoHandoffError` へ **連鎖なし（from None）** で正規化し、参照名（非機密）だけを出す
    （M-003 / BE06-005）。実値（戻り値）はここでは検査せず、呼び出し側が空判定する。
    """
    err: SsoHandoffError
    try:
        return secret_resolver(ref)
    except Exception:
        # resolver 由来の例外は型を問わず固定文言へ正規化（元例外の args/連鎖を一切引き継がない）。
        err = SsoHandoffError(
            f"secret_resolver が {ref_label} '{ref}' を解決できなかった（fail-closed）"
        )
    # **except の外**で連鎖を断ってから raise する（except 内 raise は元例外を __context__
    # に再設定し secret を含む元例外が残るため。connector_runtime._call_transport と同じ）。
    err.__cause__ = None
    err.__context__ = None
    raise err


def _resolve_token_endpoint(sso: OidcSsoBridge) -> str:
    """token endpoint を決定する。明示 tokenEndpoint があればそれ、無ければ fail-closed。

    OIDC discovery（issuer + /.well-known/openid-configuration）からの解決は **実 IdP への通信**を
    伴うため自走では行わない（人間ゲート）。tokenEndpoint 未指定で実 exchange を要求したら、
    どこへ POST すべきか決められないので fail-closed にする（誤った固定パスを生成しない）。
    """
    if sso.token_endpoint is not None:
        return sso.token_endpoint
    raise SsoHandoffError(
        "実 token-exchange には sso.tokenEndpoint が必要（discovery 解決は実 IdP 通信＝"
        "人間ゲート）。shape のみが必要なら build_sso_handoff を使う"
    )


def exchange_sso_token(
    definition: ExternalAppDefinition,
    subject: dict[str, Any],
    *,
    state: str,
    nonce: str,
    subject_token: str,
    secret_resolver: SsoSecretResolver,
    token_exchange_caller: TokenExchangeCaller | None = None,
    subject_token_ref: str = "jetuse-session-id-token",
    subject_token_type: str = ACCESS_TOKEN_TYPE,
    id_token_verifier: IdTokenVerifier | None = None,
    expected_subject: str | None = None,
) -> dict[str, Any]:
    """OIDC SSO ブリッジの **実 RFC 8693 token-exchange** を実行し、発行トークン入り結果を返す。

    build_sso_handoff が組み立てた「参照名のままの要求 shape」を実値で具体化して実行する:
      1. build_sso_handoff で shape ＋ claimMapping 適用済みクレームを得る（検証を再利用）。
      2. `secret_resolver(secretRef)` で **実 client_secret** を解決する（Vault 束ね＝人間ゲートの
         継ぎ目）。
      3. `subject_token`（JetUse の実トークン。呼び出し側がランタイムで与える・保存しない）
         と実 client_secret を要求本体へ埋め `token_exchange_caller(token_endpoint, body)` で実行。
         `subject_token_type` は subject_token の実種別（既定 access_token。Web の Bearer は access
         token のため。AUTH-001）。`requested_token_type` は **id_token**（身元を渡す。M-001）。
      4. レスポンスから **id_token** を取り出す（issued_token_type を厳格検証。SSO-001）。
      5. verifier が返す **検証済み claims** を現在の Tx/利用者へ束ねる（BE06-BLK-001）:
         claims.nonce が要求 `nonce` と一致し claims.sub が `expected_subject` と一致することを
         **handoff 成果物を返す前に強制**（別利用者の有効 id_token・リプレイを排除）。

    **トランザクション/本人束ね（BE06-BLK-001）**: issued_token_type の自己申告・署名検証だけでは、
    同一 issuer/client の **別利用者**・**別 Tx（リプレイ）** の有効 id_token を受理し得る。
    これを塞ぐため、(a) **nonce 完全一致**（要求 nonce ↔ id_token の nonce claim。定数時間比較）と
    (b) **sub 対応検証**（id_token の sub ↔ `expected_subject`。対応 sub は IdP ごとに
    定義する mapping＝人間ゲートなので **呼出側が注入**。未指定は fail-closed）を必須に
    する。これは実 IdP の有無に依らない **認証境界の必須検証**であり mock で検証できる。

    **シークレット非漏洩契約**: 戻り値・例外に **入力 client_secret / subject_token を出さない**
    （最終防壁 `_redact_values` で走査・redact）。発行された連携用トークン（IdP の応答）は SSO
    ハンドオフの成果物そのものなので戻り値に載せる（`contains_secret_values=True`）。

    既定 caller は fail-closed（実 IdP へ通信しない）。実 IdP 接続・client_secret 投入・id_token
    発行・実 sub mapping は人間ゲート（SSO 実設定＝Identity Domain・Vault）。
    """
    sso = definition.sso
    if sso is None:
        raise SsoHandoffError(
            f"external-app '{definition.app}' は sso を宣言していない（SSO ブリッジ不能）"
        )
    if not callable(secret_resolver):
        raise SsoHandoffError(
            "secret_resolver は呼び出し可能でなければならない（secretRef→実 secret）"
        )
    if not isinstance(subject_token, str) or not subject_token.strip():
        raise SsoHandoffError("subject_token（JetUse セッションの実 id_token）は必須の文字列")
    # **id_token 検証関数を常に必須**にする（caller の種類に依存しない。BE06-REV-003）。caller を
    # 関数同一性で判定するとラッパー/partial/別 HTTP 実装で検証を素通りできるため、公開 API の
    # fail-closed 契約として verifier 未注入は常に交換前に拒否する。issued_token_type の自己申告だけ
    # では侵害/設定ミスの token endpoint が任意文字列を通す穴を塞げない。JWKS 取得は実 IdP 通信＝
    # 人間ゲートのため、テストは True を返す明示 verifier を注入して交換機構を確認する。
    if id_token_verifier is None:
        raise SsoHandoffError(
            "token-exchange には id_token 検証関数（署名/iss/aud/exp）が必須（fail-closed）"
        )

    # 1. shape ＋ mapped_claims（build_sso_handoff の fail-closed 検証をそのまま通す）。
    handoff = build_sso_handoff(
        definition, subject, state=state, nonce=nonce, subject_token_ref=subject_token_ref
    )
    token_endpoint = _resolve_token_endpoint(sso)

    # 2. 実 client_secret / client_id を解決（実値はここで初めて現れる）。**解決は保護境界内**で
    #    行う: resolver の例外（未知 ref・Vault 権限拒否・一時障害）の文言・連鎖には実 secret や
    #    Vault 内部情報が混入し得るため、from None で連鎖を断ち参照名（非機密）だけを出す（M-003）。
    client_secret = _resolve_sso_secret(secret_resolver, sso.secret_ref, "secretRef")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise SsoHandoffError(
            f"secret_resolver が secretRef '{sso.secret_ref}' を解決できなかった（fail-closed）"
        )
    # client_id は機密ではない（OIDC 公開識別子）が、解決経路は同じ secret_resolver を使う。
    client_id = _resolve_sso_secret(secret_resolver, sso.client_id_ref, "clientIdRef")
    if not isinstance(client_id, str) or not client_id.strip():
        raise SsoHandoffError(
            f"secret_resolver が clientIdRef '{sso.client_id_ref}' を解決不能（fail-closed）"
        )

    # 3. 実値を埋めた token-exchange 要求本体。SSO は **id_token**（身元）を連携先へ渡すため
    #    requested_token_type は id_token を要求する（受け入れ条件: id_token を取得。M-001）。
    #    subject_token_type は subject_token の実種別（既定 access_token。AUTH-001）。
    #    **nonce を要求に含める**（IdP が id_token の nonce claim に束ねる。BE06-REV-002）。
    request_body = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "audience": sso.audience,
        "scope": " ".join(sso.scopes),
        "requested_token_type": ID_TOKEN_TYPE,
        "subject_token_type": subject_token_type,
        "subject_token": subject_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "nonce": nonce,
    }

    caller = token_exchange_caller or _denied_token_exchange_caller
    secrets = (client_secret, subject_token)
    err: SsoHandoffError | None = None
    try:
        raw = caller(token_endpoint, request_body)
    except SsoHandoffError as e:
        # 既知の fail-closed は型を保ちつつ args を redact する。
        e.args = tuple(_redact_values(a, secrets) for a in e.args)
        err = e
    except Exception as e:  # noqa: BLE001 - caller の任意例外を redact して正規化する
        err = SsoHandoffError(
            f"token-exchange の実行に失敗: {_redact_values(str(e), secrets)}"
        )
    if err is not None:
        # **except の外**で連鎖を断つ（except 内 raise は元例外を __context__ に再設定し、secret を
        # 含む元例外が連鎖経由で残るため。connector_runtime._call_transport と同じ）。
        err.__cause__ = None
        err.__context__ = None
        raise err

    if not isinstance(raw, dict):
        raise SsoHandoffError("token-exchange 応答は dict でなければならない")
    # 入力シークレットが応答に echo されても残さない（client_id は機密でないので redact 対象外）。
    response = _redact_values(raw, secrets)
    # SSO ハンドオフは **id_token**（身元）を渡す契約（M-001）。issued_token_type を **厳格**に
    # 検証する（SSO-001）: issued_token_type が存在し ID_TOKEN_TYPE と完全一致のときだけ受理する。
    # 種別が無い／access_token 等の不一致は、id_token フィールドの有無に関わらず fail-closed
    # （IdP が要求を無視して access token を返す不整合を通さない）。発行 id_token は RFC 8693 では
    # `access_token` フィールドに載るため、種別一致時はそこ（または明示 id_token）から取り出す。
    issued_token_type = response.get("issued_token_type")
    if issued_token_type != ID_TOKEN_TYPE:
        raise SsoHandoffError(
            "token-exchange 応答の issued_token_type が id_token でない（fail-closed。"
            "SSO は身元トークンの引き渡しが要）"
        )
    cand = response.get("id_token") or response.get("access_token")
    issued = cand if isinstance(cand, str) and cand else None
    if not issued:
        raise SsoHandoffError("token-exchange 応答に発行 id_token が無い（fail-closed）")

    # 発行 id_token の **暗号学的検証**（署名/iss/aud/exp）。issued_token_type だけでは設定
    # 不良・侵害 token endpoint が任意文字列を返す経路を塞げない（BE06-REV-002）。verifier は
    # 上で必須化済み（未注入は交換前に拒否。BE06-REV-003）。ここでは fail-closed で検証する。
    # **id_token の aud は RP の client_id**（OIDC）であり token-exchange の audience（リソース
    # URL=sso.audience）ではない（BE06-BLK-002）。解決済み client_id を期待 audience として渡す。
    # verifier は **検証済み claims（nonce/sub 含む）** を返す契約（BE06-BLK-001）。
    try:
        claims = id_token_verifier(issued, sso.issuer, client_id)
    except Exception:
        raise SsoHandoffError(
            "発行 id_token の検証に失敗（fail-closed）"
        ) from None
    if not isinstance(claims, dict):
        # bool 等を返す旧 verifier は契約違反（nonce/sub 束ねができない）→ fail-closed。
        raise SsoHandoffError(
            "id_token_verifier は検証済み claims（dict）を返さなければならない（fail-closed）"
        )

    # **トランザクション束ね（BE06-BLK-001）**: 要求に載せた nonce と id_token の nonce claim が完全
    # 一致することを強制する（別トランザクション/リプレイの有効 id_token を排除）。欠落・型不一致・
    # 値不一致はいずれも fail-closed（定数時間比較で nonce のタイミング差も出さない）。
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not token_nonce:
        raise SsoHandoffError(
            "発行 id_token に nonce claim が無い（トランザクション束ね不能。fail-closed）"
        )
    if not hmac.compare_digest(token_nonce, nonce):
        raise SsoHandoffError(
            "発行 id_token の nonce が要求と一致しない（リプレイ/別トランザクション。fail-closed）"
        )

    # **本人束ね（BE06-BLK-001）**: id_token の sub が「現在の認証利用者に対応する sub」（呼出側が
    # IdP 別 mapping で注入する `expected_subject`）と一致を強制する（同一 issuer/client 向けの
    # 別利用者の有効 id_token を排除）。expected_subject は実 IdP の sub mapping＝人間ゲートのため
    # 呼出側が与える（未指定は fail-closed）。mock E2E はテストが対応 sub を注入して機構を検証する。
    token_sub = claims.get("sub")
    if not isinstance(token_sub, str) or not token_sub:
        raise SsoHandoffError(
            "発行 id_token に sub claim が無い（本人束ね不能。fail-closed）"
        )
    if not isinstance(expected_subject, str) or not expected_subject.strip():
        raise SsoHandoffError(
            "expected_subject（認証利用者に対応する id_token の sub。IdP 別 mapping＝人間ゲート）"
            "が未指定（本人束ね不能。fail-closed）"
        )
    if not hmac.compare_digest(token_sub, expected_subject):
        raise SsoHandoffError(
            "発行 id_token の sub が現在の認証利用者と一致しない（別利用者トークン。fail-closed）"
        )

    # 戻り値は **明示的な許可リスト**に縮小する（SSO-002）。token_response 全体（access_token /
    # refresh_token 等）は返さない。発行 id_token はハンドオフの成果物として返すが、
    # refresh_token 等は載せない。
    return {
        "app": definition.app,
        "mode": sso.mode,
        "embed": definition.embed,
        "url": definition.url,
        "state": state,
        "nonce": nonce,
        "token_endpoint": token_endpoint,
        "audience": sso.audience,
        "scope": request_body["scope"],
        "mapped_claims": handoff["mapped_claims"],
        # IdP が発行した連携用 id_token（SSO ハンドオフの成果物）。入力シークレットは含まない。
        "issued_token": issued,
        "issued_token_type": issued_token_type,
        # 検証・束ね済みの本人識別子（id_token の sub＝expected_subject 一致済み。BE06-BLK-001）。
        # handoff store の subject はこれを使う（mapped_claims と id_token 本人の食い違いを防ぐ）。
        "issued_subject": token_sub,
        # 発行トークン（成果物）を載せる以上、参照名のみの shape とは異なり実トークンを含む。
        "contains_secret_values": True,
    }
