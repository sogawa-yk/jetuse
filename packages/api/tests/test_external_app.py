"""external-app（kind）＋ OIDC SSO ブリッジ／伝ぴょん の単体テスト（ASSET-01）。

定義の構造検証（embed/url/sso）・不正拒否（private IP・実値混入・openid 欠落・claim 不正）、
manifest の kind=external-app round-trip、`build_sso_handoff` の決定的ハンドオフ組み立てと
fail-closed（claim 欠落 / sso 未宣言 / state・nonce 欠落）、**実シークレット非保持** を検証する。
実 IdP / 実 client_secret / 実 id_token は投入しない（人間ゲート）。
"""

from __future__ import annotations

import json

import pytest

from jetuse_core.plugins.denpyon_external_app import (
    DENPYON_APP,
    DENPYON_CLIENT_ID_REF,
    DENPYON_SECRET_REF,
    denpyon_external_app_definition,
    denpyon_external_app_manifest,
)
from jetuse_core.plugins.external_app import (
    ExternalAppError,
    SsoHandoffError,
    build_sso_handoff,
    external_app_json_schema,
    validate_external_app,
)
from jetuse_core.plugins.manifest import ManifestError, validate_manifest

URL = "https://denpyon.example.com/app"
ISSUER = "https://idp.example.com"
AUDIENCE = "https://denpyon.example.com"


def _def(**over):
    d = {
        "app": "denpyon",
        "embed": "iframe",
        "url": URL,
        "title": "伝ぴょん",
        "sso": {
            "mode": "oidc",
            "issuer": ISSUER,
            "clientIdRef": "denpyon-oidc-client-id",
            "secretRef": "denpyon-oidc-client-secret",
            "audience": AUDIENCE,
            "scopes": ["openid", "email"],
            "claimMapping": {"sub": "preferred_username", "email": "email"},
        },
    }
    d.update(over)
    return d


# --- 定義の構造検証 -------------------------------------------------------


def test_valid_definition():
    d = validate_external_app(_def())
    assert d.app == "denpyon"
    assert d.embed == "iframe"
    assert d.sso is not None
    assert d.sso.mode == "oidc"
    assert d.sso.client_id_ref == "denpyon-oidc-client-id"
    assert d.sso.claim_mapping["sub"] == "preferred_username"


def test_sso_optional():
    d = validate_external_app(_def(sso=None))
    assert d.sso is None


@pytest.mark.parametrize(
    "url",
    [
        "http://denpyon.example.com/app",  # 非 https
        "https://127.0.0.1/app",  # loopback
        "https://10.0.0.1/app",  # private
        "https://user:pw@denpyon.example.com/app",  # userinfo（認証値混入）
        "https://localhost/app",
    ],
)
def test_bad_url_rejected(url):
    with pytest.raises(ExternalAppError):
        validate_external_app(_def(url=url))


def test_bad_issuer_rejected():
    with pytest.raises(ExternalAppError):
        validate_external_app(_def(sso={**_def()["sso"], "issuer": "https://10.0.0.1"}))


def test_secret_ref_must_not_be_value():
    """secretRef に実シークレット値らしき文字列（空白・記号入り）を入れると拒否。"""
    with pytest.raises(ExternalAppError):
        validate_external_app(
            _def(sso={**_def()["sso"], "secretRef": "super secret value!"})
        )


def test_scopes_must_contain_openid():
    with pytest.raises(ExternalAppError):
        validate_external_app(_def(sso={**_def()["sso"], "scopes": ["email"]}))


def test_scope_with_internal_whitespace_rejected():
    """空白入り scope（'email profile'）は禁止（join で別 scope に化けるのを防ぐ・MAJOR-002）。"""
    with pytest.raises(ExternalAppError):
        validate_external_app(
            _def(sso={**_def()["sso"], "scopes": ["openid", "email profile"]})
        )


def test_claim_mapping_bad_name_rejected():
    with pytest.raises(ExternalAppError):
        validate_external_app(
            _def(sso={**_def()["sso"], "claimMapping": {"sub": "bad name!"}})
        )


@pytest.mark.parametrize(
    "cred",
    [
        "access_token",
        "id_token",
        "client_secret",
        "password",
        "session_token",  # 部分一致 'token'
        "jwt",  # 完全一致
        "sid",  # 完全一致
        "bearer",
        "my_api_key",  # 部分一致 'api_key'
    ],
)
def test_claim_mapping_credential_source_rejected(cred):
    """資格情報系クレーム名を写像元にできない（実トークン転写を塞ぐ・MAJ-001 / 部分＋完全一致）。"""
    with pytest.raises(ExternalAppError):
        validate_external_app(
            _def(sso={**_def()["sso"], "claimMapping": {cred: "roles"}})
        )


def test_sso_requires_nonempty_claim_mapping():
    """SSO 宣言ありで claimMapping が空なら拒否（身元を渡さない SSO は無意味・MAJOR-001）。"""
    with pytest.raises(ExternalAppError):
        validate_external_app(_def(sso={**_def()["sso"], "claimMapping": {}}))


def test_claim_mapping_duplicate_destination_rejected():
    """宛先クレーム名が重複する写像は拒否（後勝ちで身元属性を黙って上書きしない・MAJOR-001）。"""
    with pytest.raises(ExternalAppError):
        validate_external_app(
            _def(sso={**_def()["sso"], "claimMapping": {"sub": "uid", "email": "uid"}})
        )


def test_claim_mapping_normal_identity_claims_allowed():
    """正当な身元クレーム（sub/email/groups/department 等）は許可される（過剰拒否しない）。"""
    mapping = {"sub": "preferred_username", "email": "email", "department": "dept"}
    d = validate_external_app(_def(sso={**_def()["sso"], "claimMapping": mapping}))
    assert d.sso.claim_mapping["department"] == "dept"


def test_json_schema_has_aliases():
    schema = external_app_json_schema()
    props = schema["properties"]
    assert set(props) >= {"app", "embed", "url", "title", "sso"}


# --- manifest round-trip --------------------------------------------------


def test_manifest_kind_external_app_roundtrip():
    m = validate_manifest(
        {
            "schemaVersion": "1",
            "id": "jetuse/denpyon-external-app",
            "version": "1.0.0",
            "kind": "external-app",
            "name": "伝ぴょん 連携",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": [],
            "contributes": {"external-app": _def()},
        }
    )
    assert m.kind == "external-app"


def test_manifest_rejects_bad_external_app_payload():
    """公開入口 validate_manifest() が contributes['external-app'] 詳細違反を弾く。"""
    with pytest.raises(ManifestError):
        validate_manifest(
            {
                "schemaVersion": "1",
                "id": "jetuse/x",
                "version": "1.0.0",
                "kind": "external-app",
                "name": "x",
                "publisher": "jetuse",
                "jetuse": {"minVersion": "0.3.0"},
                "permissions": [],
                "contributes": {"external-app": _def(url="https://127.0.0.1/app")},
            }
        )


# --- SSO ブリッジ最小実装 -------------------------------------------------

SUBJECT = {"sub": "u-123", "email": "alice@example.com", "groups": ["sales"]}


def test_build_sso_handoff_ok():
    d = validate_external_app(_def())
    h = build_sso_handoff(d, SUBJECT, state="st-1", nonce="nc-1")
    assert h["app"] == "denpyon"
    assert h["mode"] == "oidc"
    assert h["embed"] == "iframe"
    assert h["state"] == "st-1" and h["nonce"] == "nc-1"
    # claimMapping 適用（sub→preferred_username, email→email）。
    assert h["mapped_claims"] == {"preferred_username": "u-123", "email": "alice@example.com"}
    txr = h["token_exchange_request"]
    assert txr["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert txr["audience"] == AUDIENCE
    # token endpoint は固定生成しない。未指定なら OIDC discovery URL を返す。
    assert txr["discovery_url"] == "https://idp.example.com/.well-known/openid-configuration"
    assert "token_endpoint" not in txr
    # 参照名のみ（実値ではない）。
    assert txr["client_secret_ref"] == "denpyon-oidc-client-secret"
    assert txr["client_id_ref"] == "denpyon-oidc-client-id"
    assert h["contains_secret_values"] is False


def test_build_sso_handoff_explicit_token_endpoint():
    """tokenEndpoint 明示指定があればそれを使う（IdP 差異に対応・固定パス生成しない）。"""
    sso = {**_def()["sso"], "tokenEndpoint": "https://idp.example.com/v1/token"}
    d = validate_external_app(_def(sso=sso))
    h = build_sso_handoff(d, SUBJECT, state="s", nonce="n")
    assert h["token_exchange_request"]["token_endpoint"] == "https://idp.example.com/v1/token"


def test_build_sso_handoff_deterministic():
    """同じ入力で同じ出力（決定的・オフライン）。"""
    d = validate_external_app(_def())
    a = build_sso_handoff(d, SUBJECT, state="s", nonce="n")
    b = build_sso_handoff(d, SUBJECT, state="s", nonce="n")
    assert a == b


def test_build_sso_handoff_fail_closed_missing_claim():
    d = validate_external_app(_def())
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, {"email": "a@example.com"}, state="s", nonce="n")  # sub 欠落


def test_build_sso_handoff_fail_closed_empty_claim():
    d = validate_external_app(_def())
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, {"sub": "  ", "email": "a@example.com"}, state="s", nonce="n")


def test_build_sso_handoff_fail_closed_empty_collection_claim():
    """空 list の groups（groups=[]）も fail-closed（MIN-001）。"""
    mapping = {"sub": "preferred_username", "groups": "roles"}
    d = validate_external_app(_def(sso={**_def()["sso"], "claimMapping": mapping}))
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, {"sub": "u-1", "groups": []}, state="s", nonce="n")


def test_build_sso_handoff_nonstring_args_fail_closed():
    """state/nonce/subject_token_ref が非文字列でも一貫して SsoHandoffError（MINOR-001）。"""
    d = validate_external_app(_def())
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state=123, nonce="n")  # type: ignore[arg-type]
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce=None)  # type: ignore[arg-type]
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce="n", subject_token_ref=123)  # type: ignore[arg-type]


def test_build_sso_handoff_rejects_non_json_claim_value():
    """JSON 化できない subject 値（set/bytes 等）は fail-closed（MINOR-001）。"""
    d = validate_external_app(_def())
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(
            d, {"sub": {1, 2, 3}, "email": "a@example.com"}, state="s", nonce="n"
        )
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(
            d, {"sub": b"bytes", "email": "a@example.com"}, state="s", nonce="n"
        )


def test_build_sso_handoff_requires_sso():
    d = validate_external_app(_def(sso=None))
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce="n")


def test_build_sso_handoff_rejects_real_token_as_ref():
    """subject_token_ref に実 JWT らしき値を渡すと fail-closed（ASSET-MAJ-001）。"""
    d = validate_external_app(_def())
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1In0.sig"
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce="n", subject_token_ref=jwt_like)
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce="n", subject_token_ref="")
    # 正しい参照名は通る。
    h = build_sso_handoff(d, SUBJECT, state="s", nonce="n", subject_token_ref="my-session-ref")
    assert h["token_exchange_request"]["subject_token_ref"] == "my-session-ref"


def test_build_sso_handoff_requires_state_nonce():
    d = validate_external_app(_def())
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="", nonce="n")
    with pytest.raises(SsoHandoffError):
        build_sso_handoff(d, SUBJECT, state="s", nonce="")


def test_handoff_contains_no_secret_value():
    """ハンドオフ全体（JSON 化）に実シークレットらしき値が無い（参照名のみ）。"""
    d = validate_external_app(_def())
    h = build_sso_handoff(d, SUBJECT, state="s", nonce="n")
    blob = json.dumps(h, ensure_ascii=False)
    # 参照名は出る（許容）。実 client_secret 値の注入経路が無いことを型で担保しているため、
    # ここでは "secret value" のような実値プレースホルダが無いことを確認する。
    assert "client_secret_value" not in blob
    assert "BEGIN PRIVATE KEY" not in blob


# --- 伝ぴょん builder ------------------------------------------------------


def test_denpyon_definition_builder():
    d = denpyon_external_app_definition(url=URL, issuer=ISSUER, audience=AUDIENCE)
    assert d.app == DENPYON_APP
    assert d.embed == "iframe"
    assert d.sso.client_id_ref == DENPYON_CLIENT_ID_REF
    assert d.sso.secret_ref == DENPYON_SECRET_REF
    assert d.sso.claim_mapping == {
        "sub": "preferred_username",
        "email": "email",
        "groups": "roles",
    }


def test_denpyon_manifest_builder():
    m = denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)
    assert m.kind == "external-app"
    assert m.id == "jetuse/denpyon-external-app"
    blob = json.dumps(m.contributes, ensure_ascii=False)
    assert DENPYON_SECRET_REF in blob  # 参照名は保持してよい
    assert "BEGIN PRIVATE KEY" not in blob


def test_denpyon_definition_dict_does_not_share_module_constant():
    """返却 dict の claimMapping を変更してもモジュール定数を汚染しない（MINOR-001）。"""
    from jetuse_core.plugins.denpyon_external_app import (
        DENPYON_CLAIM_MAPPING,
        denpyon_external_app_definition_dict,
    )

    d = denpyon_external_app_definition_dict(url=URL, issuer=ISSUER, audience=AUDIENCE)
    d["sso"]["claimMapping"]["sub"] = "TAMPERED"
    assert DENPYON_CLAIM_MAPPING["sub"] == "preferred_username"


def test_external_app_not_marketplace_installable_fail_closed():
    """external-app はマーケット取込未対応で fail-closed に拒否される（§14.4 / MAJOR-001 境界）。"""
    from jetuse_core.plugins import installer

    m = denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(installer.IngestError):
        installer._ingest_contributes(m, "owner-x", visibility="private")
