"""伝ぴょん（denpyon）の external-app オンボード定義（ASSET-01）。

既存資産「伝ぴょん」は独自フロントを持つ外部アプリ。検索/NL2SQL のような API パイプラインではなく
**UI そのものを JetUse へ埋め込む（iframe）＋ OIDC SSO** するのが自然なオンボード方式
（方式比較は docs/verification/ASSET-01.md）。本モジュールは `kind: external-app`
（`external_app.ExternalAppDefinition`）として伝ぴょんの配布表現（manifest／定義）を正規化する。

設計（external_app の契約を踏襲）:
  - `embed=iframe`・`url`＝伝ぴょんの HTTPS エンドポイント（環境依存のため builder 引数）。
  - `sso`＝OIDC SSO ブリッジ。**実 client_secret / 実トークンを持たない**（clientIdRef /
    secretRef ＝ Vault 束ね対象の論理参照名のみ）。claimMapping で JetUse の身元
    （sub/email/groups）を伝ぴょん側（preferred_username/email/roles）へ写像する。
  - 実 URL・実 IdP・実 client_secret 投入は人間ゲート（実資産接続・SSO 実設定）。
"""

from __future__ import annotations

from typing import Any

from .external_app import ExternalAppDefinition, validate_external_app
from .manifest import PluginManifest, validate_manifest

#: 伝ぴょんの安定キー / オンボード識別子。
DENPYON_APP = "denpyon"
DENPYON_PLUGIN_ID = "jetuse/denpyon-external-app"
DENPYON_VERSION = "1.0.0"

#: OIDC client_id / client_secret の **論理参照名**（実値ではない。install 時に Vault 解決）。
DENPYON_CLIENT_ID_REF = "denpyon-oidc-client-id"
DENPYON_SECRET_REF = "denpyon-oidc-client-secret"

#: JetUse subject クレーム → 伝ぴょん側クレームの既定写像（SSO で渡す身元属性）。
DENPYON_CLAIM_MAPPING = {
    "sub": "preferred_username",
    "email": "email",
    "groups": "roles",
}


def denpyon_external_app_definition_dict(
    *,
    url: str,
    issuer: str,
    audience: str,
    client_id_ref: str = DENPYON_CLIENT_ID_REF,
    secret_ref: str = DENPYON_SECRET_REF,
    token_endpoint: str | None = None,
) -> dict[str, Any]:
    """伝ぴょん external-app 定義（contributes["external-app"]）の配布表現 dict を組み立てる。

    `url`（伝ぴょんの埋め込み先）・`issuer`（OIDC IdP）・`audience`（伝ぴょんの token audience）は
    環境依存値（オンボード時にオペレータが与える。実値は .env / Vault・人間ゲート）。token_endpoint
    を与えると実 token-exchange（`exchange_sso_token`）の POST 先になる（未指定なら shape のみ＝
    discovery 解決は人間ゲート）。
    """
    sso: dict[str, Any] = {
        "mode": "oidc",
        "issuer": issuer,
        "clientIdRef": client_id_ref,
        "secretRef": secret_ref,
        "audience": audience,
        "scopes": ["openid", "profile", "email"],
        # モジュール定数の汚染を防ぐためコピーを返す（呼び出し側が claimMapping を変更しても
        # 既定写像が変わらない。ASSET-01-MINOR-001）。
        "claimMapping": dict(DENPYON_CLAIM_MAPPING),
    }
    if token_endpoint:
        sso["tokenEndpoint"] = token_endpoint
    return {
        "app": DENPYON_APP,
        "embed": "iframe",
        "url": url,
        "title": "伝ぴょん",
        "sso": sso,
        "summary": "伝ぴょん（既存資産）を iframe 埋め込み＋OIDC SSO で連携する external-app。",
    }


def denpyon_external_app_definition(
    *,
    url: str,
    issuer: str,
    audience: str,
    client_id_ref: str = DENPYON_CLIENT_ID_REF,
    secret_ref: str = DENPYON_SECRET_REF,
    token_endpoint: str | None = None,
) -> ExternalAppDefinition:
    """検証済みの伝ぴょん external-app 定義。"""
    return validate_external_app(
        denpyon_external_app_definition_dict(
            url=url,
            issuer=issuer,
            audience=audience,
            client_id_ref=client_id_ref,
            secret_ref=secret_ref,
            token_endpoint=token_endpoint,
        )
    )


def denpyon_external_app_manifest(
    *,
    url: str,
    issuer: str,
    audience: str,
    client_id_ref: str = DENPYON_CLIENT_ID_REF,
    secret_ref: str = DENPYON_SECRET_REF,
) -> PluginManifest:
    """検証済みの伝ぴょん external-app manifest（kind=external-app）。"""
    definition = denpyon_external_app_definition_dict(
        url=url,
        issuer=issuer,
        audience=audience,
        client_id_ref=client_id_ref,
        secret_ref=secret_ref,
    )
    return validate_manifest(
        {
            "schemaVersion": "1",
            "id": DENPYON_PLUGIN_ID,
            "version": DENPYON_VERSION,
            "kind": "external-app",
            "name": "伝ぴょん 連携",
            "description": "既存資産 伝ぴょん を iframe＋OIDC SSO で連携する external-app。",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            # external-app は UI 埋め込み＋SSO であり Platform API スコープを要求しない。
            "permissions": [],
            "contributes": {"external-app": definition},
            "tags": ["denpyon", "external-app", "iframe", "sso", "asset"],
            "icon": "📨",
        }
    )
