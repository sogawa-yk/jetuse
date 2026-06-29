"""外部アプリ連携（external-app）の起動導線ルート（ASSET-01 / BE-06）。

`kind: external-app`（specs/16-platform.md §14）の「UI 埋め込み＋OIDC SSO」をフロントから起動する
ための薄い API 層。配布表現の生成は builder（`denpyon_external_app.py`）、SSO ハンドオフ組み立ては
in-process ブリッジ（`build_sso_handoff` / 実 exchange は `exchange_sso_token`）に閉じ、
本ルートは次の 2 点のみを担う:

  1. 構成済み external-app の一覧（埋め込み先 url/embed/title・SSO 宣言の有無）を返す。
  2. 認証済み利用者の身元から **OIDC SSO ハンドオフの shape** を組み立てて返す。

**人間ゲート（越えない）**: 実 token-exchange の実行（実 IdP 接続・client_secret 投入・id_token
発行）は SSO 実設定（Identity Domain＝テナンシ変更）・Vault を要するため自走では行わない。本ルートは
**決定的・オフラインの handoff shape**（`build_sso_handoff`）だけを返す（実トークンを持たない）。
実 exchange 配線（`exchange_sso_token`）はテスト/mock E2E で caller を注入して検証する。

未構成（denpyon_url/issuer/audience 未設定）は 503（運用者が .env を設定するまで機能無効）。
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jetuse_core.auth import AuthContext, require_user
from jetuse_core.plugins import external_app_store, sso_handoff_store
from jetuse_core.plugins.denpyon_external_app import (
    DENPYON_APP,
    denpyon_external_app_definition,
)
from jetuse_core.plugins.external_app import (
    ACCESS_TOKEN_TYPE,
    ExternalAppDefinition,
    ExternalAppError,
    SsoHandoffError,
    build_sso_handoff,
    exchange_sso_token,
    http_token_exchange_caller,
    jwks_id_token_verifier,
    validate_external_app,
)
from jetuse_core.settings import Settings, get_settings

router = APIRouter()


# --- builder レジストリ（app キー → 構成済み定義の組み立て） -----------------


def _denpyon_definition(settings: Settings) -> ExternalAppDefinition | None:
    """設定から伝ぴょんの external-app 定義を組み立てる。未構成なら None。

    url/issuer/audience のいずれかが空なら未構成（503 に倒す）。tokenEndpoint は実 exchange 用に
    渡す（未設定なら shape のみ）。実 client_secret/client_id は持たない（builder は参照名のみ）。
    """
    if not (settings.denpyon_url and settings.denpyon_issuer and settings.denpyon_audience):
        return None
    return denpyon_external_app_definition(
        url=settings.denpyon_url,
        issuer=settings.denpyon_issuer,
        audience=settings.denpyon_audience,
        token_endpoint=settings.denpyon_token_endpoint or None,
    )


#: app キー → 構成済み定義ビルダ。新しい external-app 資産はここに足す。
_BUILDERS = {DENPYON_APP: _denpyon_definition}


# --- インストール済み instance からの定義解決（マーケット install との接続 / M-004） --


def _installed_definition(app: str) -> ExternalAppDefinition | None:
    """マーケット install 済みの external-app（external_app_instances）から定義を復元する。

    最新登録（list は新しい順）を採用する。定義は配布表現のまま保存され validate で復元する。
    install は **platform 全体**（署名検証済み・運用者ゲート。版全体一意）なので connector
    等と同じく全体可視で解決する（per-user 限定にしない。BE06-REV-005。詳細は ADR-0021）。
    """
    rows = external_app_store.list_external_apps(app=app)
    for row in rows:
        defn = row.get("definition")
        if not defn:
            continue
        try:
            return validate_external_app(defn)
        except ExternalAppError:  # pragma: no cover - 保存時に検証済み
            continue
    return None


def _resolve_definition(app: str, settings: Settings) -> ExternalAppDefinition | None:
    """app の定義を解決する。builder 構成（.env）優先、無ければ install 済み instance を見る。"""
    builder = _BUILDERS.get(app)
    if builder is not None:
        definition = builder(settings)
        if definition is not None:
            return definition
    return _installed_definition(app)


def _configured_definition(app: str, settings: Settings) -> ExternalAppDefinition:
    """app の定義を返す（builder 構成 or install 済み instance）。未知 app は 404、未構成は 503。

    builder（管理者 .env）も install 済み instance（署名検証済み・運用者ゲートの platform-wide
    install）も全利用者共通に可視（connector/usecase と同じ platform 一貫モデル。BE06-REV-005）。
    """
    if app not in _BUILDERS and not external_app_store.list_external_apps(app=app):
        raise HTTPException(status_code=404, detail=f"未知の external-app: {app}")
    definition = _resolve_definition(app, settings)
    if definition is None:
        raise HTTPException(
            status_code=503,
            detail=f"external-app '{app}' が未構成です（設定 or マーケット install が必要）",
        )
    return definition


# --- Vault SSO secret 解決（実 exchange 用・人間ゲート） --------------------


def _parse_secret_ocids(raw: str) -> dict[str, str]:
    """"ref=ocid,ref2=ocid2" 形式の設定を {ref: ocid} に解す（実値ではなく OCID 参照）。"""
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        ref, _, ocid = part.partition("=")
        ref, ocid = ref.strip(), ocid.strip()
        if ref and ocid:
            mapping[ref] = ocid
    return mapping


def _vault_sso_resolver(settings: Settings):
    """secretRef/clientIdRef → 実値を OCI Vault から解決する resolver。未構成なら None。

    実 OCID・実権限は人間ゲート（SSO 実設定・Vault）。`external_app_secret_ocids` 未設定なら None
    （実 exchange 不能＝呼び出し側で 503 fail-closed）。
    """
    mapping = _parse_secret_ocids(settings.external_app_secret_ocids)
    if not mapping:
        return None

    def _resolve(ref: str) -> str:
        ocid = mapping.get(ref)
        if not ocid:
            raise KeyError(f"secretRef '{ref}' に対応する Vault secret OCID が未登録")
        from jetuse_core.mcp_servers import _read_secret

        return _read_secret(ocid)

    return _resolve


def _bearer_token(request: Request) -> str | None:
    """Authorization: Bearer から生トークン（利用者の実 id_token）を取り出す。無ければ None。

    実 token-exchange の subject_token は **利用者セッションの実 id_token**。これは require_user が
    検証に使う生 Bearer そのもの。保存せずランタイムで exchange に渡す。
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


# --- リクエスト DTO --------------------------------------------------------


class SsoLaunchRequest(BaseModel):
    #: front-channel の CSRF 対策値（呼び出し側＝フロントが一意に生成）。
    state: str = Field(min_length=1, max_length=512)
    #: front-channel のリプレイ対策値（呼び出し側が一意に生成）。
    nonce: str = Field(min_length=1, max_length=512)


# --- 利用者クレームの取り出し（秘密を運ばない） ----------------------------


def _subject_claims(user: AuthContext) -> dict[str, Any]:
    """認証済み利用者の身元クレームを SSO 写像元として取り出す。

    実トークン値は載せない（claims は IdP 検証済みの身元属性。AUTH_REQUIRED=false の dev では
    sub のみ）。claimMapping の欠落クレームは build_sso_handoff が fail-closed にするため、ここでは
    捏造せず「あるものだけ」を渡す。
    """
    subject: dict[str, Any] = dict(user.claims or {})
    subject.setdefault("sub", user.subject)
    return subject


# --- ルート ----------------------------------------------------------------


@router.get("/api/external-apps")
def list_external_apps(
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """external-app 一覧（builder 構成＋install 済み）。埋め込み情報のみ・秘密は含めない。"""
    seen: set[str] = set()
    apps = []

    def _emit(definition: ExternalAppDefinition, source: str) -> None:
        if definition.app in seen:
            return
        seen.add(definition.app)
        apps.append(
            {
                "app": definition.app,
                "embed": definition.embed,
                "url": definition.url,
                "title": definition.title,
                "summary": definition.summary,
                "sso": definition.sso is not None,
                "source": source,
            }
        )

    # builder 構成（.env）を優先。
    for builder in _BUILDERS.values():
        definition = builder(settings)
        if definition is not None:
            _emit(definition, "config")
    # マーケット install 済み instance（external_app_instances）も surface する（M-004）。
    # install は platform-wide（署名検証済み・運用者ゲート）なので connector 等と同じく全体可視
    # （per-user 限定にしない。BE06-REV-005）。
    for row in external_app_store.list_external_apps():
        defn = row.get("definition")
        if not defn:
            continue
        try:
            _emit(validate_external_app(defn), "installed")
        except ExternalAppError:  # pragma: no cover - 保存時に検証済み
            continue
    return {"external_apps": apps}


@router.post("/api/external-apps/{app}/sso-launch")
def sso_launch(
    app: str,
    req: SsoLaunchRequest,
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """OIDC SSO ハンドオフの shape を組み立てて返す（決定的・オフライン・実トークン非保持）。

    返り値は front-channel 起動情報（embed/url/state/nonce）＋ RFC 8693 token-exchange 要求の shape
    （client/secret/subject_token は **参照名**）＋ claimMapping 適用済みクレーム。**実 exchange
    の実行は人間ゲート**（SSO 実設定）なのでルートでは行わない。contains_secret_values=False。
    """
    definition = _configured_definition(app, settings)
    try:
        handoff = build_sso_handoff(
            definition,
            _subject_claims(user),
            state=req.state,
            nonce=req.nonce,
        )
    except SsoHandoffError as e:
        # SSO 未宣言 / 写像元クレーム欠落（dev で email/groups が無い等）は fail-closed = 422。
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ExternalAppError as e:  # pragma: no cover - 構成済み定義は検証済み
        raise HTTPException(status_code=400, detail=str(e)) from e
    return handoff


@router.post("/api/external-apps/{app}/sso-exchange")
def sso_exchange(
    app: str,
    req: SsoLaunchRequest,
    request: Request,
    response: Response,
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """**実 RFC 8693 token-exchange を実行**して id_token を取得する（人間ゲート＝SSO 実設定）。

    **信頼境界（SEC-001）**: 実 exchange は **管理者が .env で構成した app（builder）** に限定する。
    install 済み instance は任意の tokenEndpoint/secretRef を指定し得る（署名は発行者の真正性
    しか保証しない）ため、install だけでは実 exchange の対象にならない（shape の sso-launch は可）。
    さらに **AUTH_REQUIRED=true でなければ実 exchange を禁止**する（dev で未検証
    Bearer を実 IdP へ送らない）。subject_token の種別は access_token（Web の Bearer。AUTH-001）。
    **発行 id_token はブラウザに直接返さない**（front-channel 漏洩回避。BE06-SSO-002）。単回使用・
    短 TTL の handoff code に束ね（sso_handoff_store）、応答は code と起動情報のみ。連携先がバック
    チャネル sso-redeem で id_token を1回だけ受け取る（認可コード型）。

    実行には (a) builder 構成＋sso.tokenEndpoint、(b) Vault secret 解決
    （external_app_secret_ocids=管理者承認済み OCID）、(c) AUTH_REQUIRED=true、(d) 実トークン
    （Authorization: Bearer）、(e) id_token 検証 JWKS が要る。未充足は fail-closed（403/503/401）。
    """
    # no-store: code を含む応答をキャッシュ/プロキシに残さない（SSO-002）。
    response.headers["Cache-Control"] = "no-store"
    # SEC-001: 実 exchange は builder 構成 app のみ（install 済みの任意 endpoint を信頼しない）。
    builder = _BUILDERS.get(app)
    definition = builder(settings) if builder is not None else None
    if definition is None:
        raise HTTPException(
            status_code=403,
            detail="実 token-exchange は管理者が .env 構成した external-app のみ許可",
        )
    # AUTH_REQUIRED=false（dev）では Bearer が未検証なので実 IdP へ送らない（fail-closed）。
    if not settings.auth_required:
        raise HTTPException(
            status_code=403,
            detail="実 token-exchange は AUTH_REQUIRED=true（実 Bearer 検証）でのみ許可",
        )
    if definition.sso is None or definition.sso.token_endpoint is None:
        raise HTTPException(
            status_code=503,
            detail="実 token-exchange は sso.tokenEndpoint 未構成のため不能（人間ゲート）",
        )
    resolver = _vault_sso_resolver(settings)
    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="実 token-exchange は Vault secret 未構成のため不能（要 OCID 設定）",
        )
    # 発行 id_token の **署名/iss/aud/exp 検証**（JWKS）を必須にする（BE06-R002）。JWKS URL は連携先
    # IdP（sso.issuer）の実 JWKS を指す実 IdP 設定＝人間ゲート。未構成なら検証不能のため fail-closed
    # （503）にし、未検証トークンを決して受理しない。
    if not settings.denpyon_jwks_url:
        raise HTTPException(
            status_code=503,
            detail="実 token-exchange は id_token 検証(JWKS)未構成のため不能（denpyon_jwks_url）",
        )
    subject_token = _bearer_token(request)
    if not subject_token:
        raise HTTPException(
            status_code=401,
            detail="実 token-exchange には利用者の実トークン（Authorization: Bearer）が必要",
        )
    # 発行 id_token の **本人束ね**（BE06-BLK-001）に使う expected_subject（認証利用者に対応する
    # denpyon id_token の sub）。実 sub mapping は IdP ごとに定義する人間ゲートのため、ここでは
    # subject-preserving token-exchange を前提に **認証利用者の subject** を期待値とする（denpyon の
    # sub 名前空間が異なれば実 IdP 設定で mapping を確定＝人間ゲート。不一致時は fail-closed=502）。
    expected_subject = user.subject
    try:
        result = exchange_sso_token(
            definition,
            _subject_claims(user),
            state=req.state,
            nonce=req.nonce,
            subject_token=subject_token,
            secret_resolver=resolver,
            token_exchange_caller=http_token_exchange_caller,
            # Web の Bearer は JetUse IdP の access token（AUTH-001）。token endpoint は
            # sso.tokenEndpoint に限定（builder 構成のみ＝SEC-001。任意 endpoint へ送らない）。
            subject_token_type=ACCESS_TOKEN_TYPE,
            id_token_verifier=jwks_id_token_verifier(settings.denpyon_jwks_url),
            # 発行 id_token の sub をこの利用者に束ねる（別利用者/リプレイ拒否。BE06-BLK-001）。
            expected_subject=expected_subject,
        )
    except SsoHandoffError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ExternalAppError as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(e)) from e
    # **発行 id_token をブラウザに直接返さない**（front-channel 漏洩回避。BE06-SSO-002）。単回使用・
    # 短 TTL の handoff code に束ね、ブラウザには code と起動情報だけを返す。連携先がバックチャネル
    # sso-redeem で code を1回だけ交換して id_token を受け取る（認可コード型）。
    code = sso_handoff_store.get_store().mint(
        app=result["app"],
        id_token=result["issued_token"],
        # 検証・束ね済みの id_token sub を保持する（mapped_claims と id_token 本人の食い違いを防ぐ。
        # BE06-BLK-001。expected_subject=user.subject と一致確認済み）。
        subject=result["issued_subject"],
        issued_token_type=result["issued_token_type"],
        # claimMapping 適用済みクレーム（groups→roles 等）も code に束ね、redeem で外部アプリへ渡す
        # （実 SSO セッションに roles を反映。BE06-MAJ-003）。
        mapped_claims=result["mapped_claims"],
    )
    return {
        "app": result["app"],
        "mode": result["mode"],
        "embed": result["embed"],
        "url": result["url"],
        "state": result["state"],
        "nonce": result["nonce"],
        "mapped_claims": result["mapped_claims"],
        "handoff_code": code,
        "expires_in": sso_handoff_store.DEFAULT_TTL_SECONDS,
        # id_token はブラウザに返さない（contains_secret_values=False）。
        "contains_secret_values": False,
    }


class SsoRedeemRequest(BaseModel):
    #: sso-exchange が発行した単回使用 handoff code。
    handoff_code: str = Field(min_length=1, max_length=512)
    #: 連携先（外部アプリ）の OIDC client 識別子（バックチャネル認証）。
    client_id: str = Field(min_length=1, max_length=512)
    #: 連携先の client_secret（Vault 解決した実値と照合してバックチャネル呼出元を認証する）。
    client_secret: str = Field(min_length=1, max_length=4096)


@router.post("/api/external-apps/{app}/sso-redeem")
def sso_redeem(
    app: str,
    req: SsoRedeemRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
):
    """**連携先のバックチャネル**: handoff code を1回だけ交換して id_token を受け取る。

    認可コード型 SSO の token 受領経路。呼出元（外部アプリのバックエンド）を **client_id +
    client_secret**（Vault 解決した実値と照合）で認証し、code の対象アプリ一致・未期限・未使用を検証
    して **1回だけ** id_token を返す（再使用・期限切れは fail-closed）。**実 client_secret の Vault
    束ねと連携先がこの経路を呼ぶ実装合意は人間ゲート**（ADR-0021 / SKIPPED.md）。
    """
    response.headers["Cache-Control"] = "no-store"
    # builder 構成 app のみ（SEC-001。任意 endpoint/secret を信頼しない）。
    builder = _BUILDERS.get(app)
    definition = builder(settings) if builder is not None else None
    if definition is None or definition.sso is None:
        raise HTTPException(status_code=403, detail="redeem は管理者構成の external-app のみ")
    resolver = _vault_sso_resolver(settings)
    if resolver is None:
        raise HTTPException(status_code=503, detail="redeem は Vault secret 未構成のため不能")
    # 呼出元の client 認証（Vault 実値と定数時間比較）。失敗は 401（id_token を出さない）。
    try:
        real_id = resolver(definition.sso.client_id_ref)
        real_secret = resolver(definition.sso.secret_ref)
    except Exception:  # noqa: BLE001 - resolver 例外は資格情報を出さず 401 に正規化
        raise HTTPException(status_code=401, detail="redeem の client 認証に失敗") from None
    if not (
        isinstance(real_id, str)
        and isinstance(real_secret, str)
        and secrets.compare_digest(req.client_id, real_id)
        and secrets.compare_digest(req.client_secret, real_secret)
    ):
        raise HTTPException(status_code=401, detail="redeem の client 認証に失敗")
    entry = sso_handoff_store.get_store().redeem(req.handoff_code, app=app)
    if entry is None:
        # 不明・期限切れ・使用済み・別アプリ向け → fail-closed（存在を区別しない）。
        raise HTTPException(status_code=404, detail="handoff code が無効（未使用・未期限のみ）")
    return {
        "app": app,
        "id_token": entry.id_token,
        "issued_token_type": entry.issued_token_type,
        "subject": entry.subject,
        # claimMapping 適用済みクレーム（groups→roles 等）を外部アプリへ渡す（BE06-MAJ-003）。
        "mapped_claims": entry.mapped_claims,
    }
