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
    exchange_sso_token,
    external_app_json_schema,
    http_token_exchange_caller,
    jwks_id_token_verifier,
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
    # **契約の回帰固定（BE06-R005 / ADR-0021）**: SSO は身元（id_token）を渡すため
    # requested=id_token、subject は呼び出し側 access token（Web Bearer）。exchange と一致。
    assert txr["requested_token_type"] == _ID_TT
    assert txr["subject_token_type"] == _ACC_TT
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


def test_external_app_marketplace_installable(monkeypatch):
    """external-app はマーケット install 対応（BE-06）。_ingest_contributes が external_app_store へ
    定義検証済みで登録し、(table, id) を返す（DB は monkeypatch で差し替え・実 DB は E2E）。"""
    from jetuse_core.plugins import external_app_store, installer

    captured = {}

    def fake_register(manifest, *, registered_by, name=None):
        captured["kind"] = manifest.kind
        captured["registered_by"] = registered_by
        # 構造検証を素通りさせず本物の validate を通す（迂回 manifest を弾く契約を保つ）。
        from jetuse_core.plugins.external_app import validate_external_app

        validate_external_app(manifest)
        return {"id": "ext-1"}

    monkeypatch.setattr(external_app_store, "register_external_app", fake_register)

    m = denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)
    created = installer._ingest_contributes(m, "owner-x", visibility="private")
    assert created == [("external_app_instances", "ext-1")]
    assert captured == {"kind": "external-app", "registered_by": "owner-x"}


def test_external_app_ingest_rejects_invalid_definition(monkeypatch):
    """構造不正な external-app 定義は IngestError に正規化される（500 にしない）。"""
    from jetuse_core.plugins import external_app_store, installer
    from jetuse_core.plugins.external_app import ExternalAppError

    def fake_register(manifest, *, registered_by, name=None):
        raise ExternalAppError("bad definition")

    monkeypatch.setattr(external_app_store, "register_external_app", fake_register)
    m = denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(installer.IngestError):
        installer._ingest_contributes(m, "owner-x", visibility="private")


# --- 実 token-exchange 配線（exchange_sso_token / BE-06） -------------------

#: テスト用の偽 client_secret / id_token（実値ではない。漏洩していないことの検査に使う）。
FAKE_SECRET = "denpyon-client-secret-FAKE-NOT-REAL"
FAKE_CLIENT_ID = "denpyon-client-id-PUBLIC"
FAKE_SUBJECT_TOKEN = "jetuse-id-token-FAKE-NOT-REAL.payload.sig"
TOKEN_ENDPOINT = "https://idp.example.com/oauth2/token"


def _def_with_token_endpoint(**over):
    d = _def(**over)
    d["sso"]["tokenEndpoint"] = TOKEN_ENDPOINT
    return d


def _resolver(ref):
    # secretRef / clientIdRef を実値へ解決する（テストでは固定値。実は Vault 束ね＝人間ゲート）。
    return {
        "denpyon-oidc-client-secret": FAKE_SECRET,
        "denpyon-oidc-client-id": FAKE_CLIENT_ID,
    }[ref]


# 検証は常に必須（BE06-REV-003）。verifier は **検証済み claims（nonce/sub 含む）** を返す契約
# （BE06-BLK-001）。交換機構の確認用に、要求 nonce と本人 sub を載せた claims を返す mock を作る
# （実 JWKS 検証は実 IdP 通信＝人間ゲート。jwks_id_token_verifier は実設定で使う）。
def _verifier(nonce: str = "no", sub: str = "u1"):
    """検証済み claims を返す mock verifier。nonce/sub を expected と揃えて束ねを通す。"""
    def _v(token, issuer, audience):
        return {"nonce": nonce, "sub": sub, "iss": issuer, "aud": audience}
    return _v


#: 既定 mock verifier（verifier 到達前に fail-closed するテスト用）。nonce/sub 束ねを通すテストは
#: `_verifier(<nonce>)` と `expected_subject=<sub>` を明示する。
_ok_verifier = _verifier()


def test_exchange_sso_token_via_mock_caller():
    """tokenEndpoint＋mock caller で実 token-exchange を疎通し、発行トークンを返す。"""
    d = validate_external_app(_def_with_token_endpoint())
    captured = {}

    def caller(token_endpoint, body):
        captured["endpoint"] = token_endpoint
        captured["body"] = body
        return {
            "id_token": "ISSUED-DENPYON-TOKEN",
            "issued_token_type": "urn:ietf:params:oauth:token-type:id_token",
        }

    out = exchange_sso_token(
        d,
        {"sub": "u1", "email": "u1@example.com"},
        state="st",
        nonce="no",
        subject_token=FAKE_SUBJECT_TOKEN,
        secret_resolver=_resolver,
        token_exchange_caller=caller,
        id_token_verifier=_verifier("no"),
        expected_subject="u1",
    )
    assert out["issued_token"] == "ISSUED-DENPYON-TOKEN"
    assert out["issued_subject"] == "u1"  # 検証・束ね済みの本人識別子（BE06-BLK-001）
    assert out["contains_secret_values"] is True
    assert out["mapped_claims"] == {"preferred_username": "u1", "email": "u1@example.com"}
    # caller には実値が渡る（IdP への本物の要求）。
    assert captured["endpoint"] == TOKEN_ENDPOINT
    assert captured["body"]["client_secret"] == FAKE_SECRET
    assert captured["body"]["subject_token"] == FAKE_SUBJECT_TOKEN
    assert captured["body"]["client_id"] == FAKE_CLIENT_ID


def test_exchange_default_caller_is_fail_closed():
    """caller 未注入は fail-closed（実 IdP へ通信しない＝人間ゲート）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d,
            {"sub": "u1", "email": "u1@example.com"},
            state="st",
            nonce="no",
            subject_token=FAKE_SUBJECT_TOKEN,
            secret_resolver=_resolver,
            id_token_verifier=_ok_verifier,
        )


def test_exchange_requires_token_endpoint():
    """tokenEndpoint 未指定は fail-closed（discovery 解決は実 IdP 通信＝人間ゲート）。"""
    d = validate_external_app(_def())  # tokenEndpoint 無し
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d,
            {"sub": "u1", "email": "u1@example.com"},
            state="st",
            nonce="no",
            subject_token=FAKE_SUBJECT_TOKEN,
            secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "x"},
            id_token_verifier=_ok_verifier,
        )


def test_exchange_does_not_leak_secret_on_caller_error():
    """caller が実 secret/subject_token を含む例外を投げても、外へ漏らさない（redact）。"""
    d = validate_external_app(_def_with_token_endpoint())

    def caller(token_endpoint, body):
        raise RuntimeError(f"IdP rejected secret={FAKE_SECRET} subj={FAKE_SUBJECT_TOKEN}")

    with pytest.raises(SsoHandoffError) as ei:
        exchange_sso_token(
            d,
            {"sub": "u1", "email": "u1@example.com"},
            state="st",
            nonce="no",
            subject_token=FAKE_SUBJECT_TOKEN,
            secret_resolver=_resolver,
            token_exchange_caller=caller,
            id_token_verifier=_ok_verifier,
        )
    msg = str(ei.value)
    assert FAKE_SECRET not in msg
    assert FAKE_SUBJECT_TOKEN not in msg


def test_exchange_result_has_no_input_secrets():
    """戻り値（許可リスト）に入力 secret を含まない（SSO-002: token_response も非返却）。"""
    d = validate_external_app(_def_with_token_endpoint())

    def caller(token_endpoint, body):
        # IdP 応答に入力 secret が echo されても、戻り値の許可リストには載らない。
        return {
            "id_token": "ISSUED",
            "issued_token_type": "urn:ietf:params:oauth:token-type:id_token",
            "echo": {"client_secret": FAKE_SECRET, "subject_token": FAKE_SUBJECT_TOKEN},
        }

    out = exchange_sso_token(
        d,
        {"sub": "u1", "email": "u1@example.com"},
        state="st",
        nonce="no",
        subject_token=FAKE_SUBJECT_TOKEN,
        secret_resolver=_resolver,
        token_exchange_caller=caller,
        id_token_verifier=_verifier("no"),
        expected_subject="u1",
    )
    blob = json.dumps(out, ensure_ascii=False)
    assert FAKE_SECRET not in blob
    assert FAKE_SUBJECT_TOKEN not in blob
    assert "echo" not in out  # token_response 全体は返さない（許可リスト）
    assert out["issued_token"] == "ISSUED"


def test_exchange_fail_closed_on_unresolvable_secret():
    """secret_resolver が解決できない（空）と fail-closed。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d,
            {"sub": "u1", "email": "u1@example.com"},
            state="st",
            nonce="no",
            subject_token=FAKE_SUBJECT_TOKEN,
            secret_resolver=lambda ref: "",
            token_exchange_caller=lambda e, b: {"id_token": "x"},
            id_token_verifier=_ok_verifier,
        )


def test_exchange_requires_subject_token():
    """subject_token（JetUse セッションの実 id_token）欠如は fail-closed。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d,
            {"sub": "u1", "email": "u1@example.com"},
            state="st",
            nonce="no",
            subject_token="",
            secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "x"},
        )


def test_exchange_requires_id_token_type(monkeypatch):
    """id_token を要求した以上、issued_token_type が id_token でない応答は fail-closed（M-001）。"""
    d = validate_external_app(_def_with_token_endpoint())

    # access_token のみ・issued_token_type 無し → id_token を取り出せず fail-closed。
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"access_token": "ACC", "token_type": "Bearer"},
            id_token_verifier=_ok_verifier,
        )


def test_exchange_accepts_access_token_field_when_type_is_id_token():
    """RFC 8693: 発行 id_token が access_token に載り issued_token_type=id_token なら受理。"""
    d = validate_external_app(_def_with_token_endpoint())
    out = exchange_sso_token(
        d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
        subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
        token_exchange_caller=lambda e, b: {
            "access_token": "ISSUED-ID-TOKEN",
            "issued_token_type": "urn:ietf:params:oauth:token-type:id_token",
        },
        id_token_verifier=_verifier("n"),
        expected_subject="u1",
    )
    assert out["issued_token"] == "ISSUED-ID-TOKEN"
    assert out["issued_token_type"] == "urn:ietf:params:oauth:token-type:id_token"


_ID_TT = "urn:ietf:params:oauth:token-type:id_token"
_ACC_TT = "urn:ietf:params:oauth:token-type:access_token"


def test_exchange_requests_id_token_and_default_subject_access_token():
    """requested_token_type=id_token（M-001）／subject_token_type 既定=access_token（AUTH-001）。"""
    d = validate_external_app(_def_with_token_endpoint())
    captured = {}
    exchange_sso_token(
        d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
        subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
        token_exchange_caller=lambda e, b: captured.update(b)
        or {"id_token": "X", "issued_token_type": _ID_TT},
        id_token_verifier=_verifier("n"),
        expected_subject="u1",
    )
    assert captured["requested_token_type"] == _ID_TT
    assert captured["subject_token_type"] == _ACC_TT  # Web の Bearer は access token


def test_exchange_rejects_id_token_field_with_wrong_type():
    """id_token があっても issued_token_type が access_token なら fail-closed（SSO-001）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "X", "issued_token_type": _ACC_TT},
            id_token_verifier=_ok_verifier,
        )


def test_exchange_result_excludes_token_response_and_refresh_token():
    """戻り値は許可リストに縮小し、token_response 全体・refresh_token を返さない（SSO-002）。"""
    d = validate_external_app(_def_with_token_endpoint())
    out = exchange_sso_token(
        d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
        subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
        token_exchange_caller=lambda e, b: {
            "id_token": "ISSUED", "issued_token_type": _ID_TT,
            "refresh_token": "REFRESH-SECRET", "access_token": "ACC-SECRET",
        },
        id_token_verifier=_verifier("n"),
        expected_subject="u1",
    )
    assert "token_response" not in out
    blob = json.dumps(out, ensure_ascii=False)
    assert "REFRESH-SECRET" not in blob
    assert out["issued_token"] == "ISSUED"


def test_exchange_normalizes_resolver_exception_without_leak():
    """resolver が実 secret を含む例外を投げても SsoHandoffError へ正規化し漏らさない（M-003）。"""
    d = validate_external_app(_def_with_token_endpoint())

    def boom(ref):
        raise RuntimeError(f"vault denied secret={FAKE_SECRET}")

    with pytest.raises(SsoHandoffError) as ei:
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=boom,
            token_exchange_caller=lambda e, b: {"id_token": "X"},
            id_token_verifier=_ok_verifier,
        )
    assert FAKE_SECRET not in str(ei.value)
    # 連鎖（__cause__/__context__）にも元例外を残さない。
    assert ei.value.__cause__ is None


def test_http_token_exchange_caller_normalizes_oauth_error(monkeypatch):
    """本番 caller は OAuth エラー応答を SsoHandoffError へ正規化する（B-002）。"""
    import httpx

    class FakeResp:
        status_code = 400

        def json(self):
            return {"error": "invalid_client"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    with pytest.raises(SsoHandoffError):
        http_token_exchange_caller("https://idp.example.com/oauth2/token", {"x": "y"})


def test_http_token_exchange_caller_rejects_3xx(monkeypatch):
    """2xx 以外（JSON を伴う 302 等）は成功と誤認せず拒否する（BE06-R006）。"""
    import httpx

    class FakeResp:
        status_code = 302

        def json(self):
            return {"id_token": "X", "issued_token_type": _ID_TT}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    with pytest.raises(SsoHandoffError):
        http_token_exchange_caller("https://idp.example.com/oauth2/token", {"x": "y"})


def test_exchange_real_caller_requires_verifier():
    """実 HTTP caller 経路は id_token 検証関数が必須＝未注入は交換前に fail-closed（BE06-R002）。"""
    import jetuse_core.plugins.external_app as ext

    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=ext.http_token_exchange_caller,  # 実 caller・verifier 無し
        )


def test_jwks_id_token_verifier_accepts_valid_rejects_bad(monkeypatch):
    """実鍵署名 JWT を verifier が受理し、改ざん/期限切れ/exp欠落/iss を拒否（BE06-TEST-001）。"""
    import time

    import jwt as jwtlib
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key()

    class _FakeSigningKey:
        def __init__(self, k):
            self.key = k

    class _FakeJWKClient:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey(pub)

    # verifier 内の `from jwt import PyJWKClient` が拾う属性を差し替える（実鍵で検証する）。
    monkeypatch.setattr(jwtlib, "PyJWKClient", _FakeJWKClient)
    verify = jwks_id_token_verifier("https://idp.example.com/jwks")

    now = int(time.time())
    base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u1", "iat": now, "nonce": "n1"}
    good = jwtlib.encode({**base, "exp": now + 300}, key, algorithm="RS256")
    # verifier は **検証済み claims（nonce/sub 含む）** を返す（BE06-BLK-001）。
    claims = verify(good, ISSUER, AUDIENCE)
    assert claims["sub"] == "u1" and claims["nonce"] == "n1"

    # 署名改ざん（末尾を破壊）→ 検証失敗。
    with pytest.raises(jwtlib.PyJWTError):
        verify(good[:-2] + ("aa" if not good.endswith("aa") else "bb"), ISSUER, AUDIENCE)
    # 期限切れ → 拒否。
    expired = jwtlib.encode({**base, "exp": now - 10}, key, algorithm="RS256")
    with pytest.raises(jwtlib.PyJWTError):
        verify(expired, ISSUER, AUDIENCE)
    # exp 欠落（require=["exp",...]）→ 拒否。
    no_exp = jwtlib.encode(base, key, algorithm="RS256")
    with pytest.raises(jwtlib.PyJWTError):
        verify(no_exp, ISSUER, AUDIENCE)
    # nonce 欠落（require=[...,"nonce"]）→ 拒否（トランザクション束ね不能。BE06-BLK-001）。
    no_nonce = jwtlib.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u1", "iat": now, "exp": now + 300},
        key, algorithm="RS256",
    )
    with pytest.raises(jwtlib.PyJWTError):
        verify(no_nonce, ISSUER, AUDIENCE)
    # issuer 不一致 → 拒否。
    with pytest.raises(jwtlib.PyJWTError):
        verify(good, "https://evil.example.com", AUDIENCE)
    # audience 不一致 → 拒否。
    with pytest.raises(jwtlib.PyJWTError):
        verify(good, ISSUER, "https://evil.example.com")


def test_exchange_id_token_verifier_rejects(monkeypatch):
    """id_token_verifier が False を返すと fail-closed（BE06-004: OIDC 検証の継ぎ目）。"""
    d = validate_external_app(_def_with_token_endpoint())
    seen = {}

    def verifier(token, issuer, audience):
        seen["args"] = (token, issuer, audience)
        return False

    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=verifier,
        )
    # verifier には issuer と **RP の client_id**（id_token の期待 aud）が渡る（BE06-BLK-002）。
    assert seen["args"][1] == ISSUER and seen["args"][2] == FAKE_CLIENT_ID


def test_exchange_id_token_verifier_accepts():
    """verifier が claims を返し nonce/sub の束ねを通れば受理し id_token を返す（BE06-004）。"""
    d = validate_external_app(_def_with_token_endpoint())
    out = exchange_sso_token(
        d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
        subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
        token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
        id_token_verifier=_verifier("n"),
        expected_subject="u1",
    )
    assert out["issued_token"] == "ISSUED"


def test_exchange_rejects_nonce_mismatch():
    """発行 id_token の nonce が要求と違えば fail-closed（リプレイ/別Tx。BE06-BLK-001）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=_verifier("DIFFERENT-NONCE"),  # 要求 nonce="n" と不一致
            expected_subject="u1",
        )


def test_exchange_rejects_missing_nonce_claim():
    """発行 id_token に nonce claim が無ければ fail-closed（束ね不能。BE06-BLK-001）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=lambda tok, iss, aud: {"sub": "u1"},  # nonce 欠落
            expected_subject="u1",
        )


def test_exchange_rejects_subject_mismatch():
    """発行 id_token の sub が expected_subject と違えば fail-closed（別利用者。BLK-001）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=_verifier("n", sub="ATTACKER"),  # 別利用者の sub
            expected_subject="u1",
        )


def test_exchange_requires_expected_subject():
    """expected_subject 未指定なら本人束ね不能で fail-closed（IdP 別 mapping＝人間ゲート）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=_verifier("n"),  # expected_subject 未指定
        )


def test_exchange_rejects_non_dict_verifier_result():
    """verifier が dict 以外（旧 bool 契約）を返したら fail-closed（BE06-BLK-001 契約変更）。"""
    d = validate_external_app(_def_with_token_endpoint())
    with pytest.raises(SsoHandoffError):
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=_resolver,
            token_exchange_caller=lambda e, b: {"id_token": "ISSUED", "issued_token_type": _ID_TT},
            id_token_verifier=lambda tok, iss, aud: True,  # 旧契約（bool）
            expected_subject="u1",
        )


def test_exchange_resolver_raising_sso_error_is_normalized():
    """resolver が secret 入り SsoHandoffError を投げても固定文言へ正規化（BE06-005）。"""
    d = validate_external_app(_def_with_token_endpoint())

    def boom(ref):
        raise SsoHandoffError(f"vault internal secret={FAKE_SECRET}")

    with pytest.raises(SsoHandoffError) as ei:
        exchange_sso_token(
            d, {"sub": "u1", "email": "u1@example.com"}, state="s", nonce="n",
            subject_token=FAKE_SUBJECT_TOKEN, secret_resolver=boom,
            token_exchange_caller=lambda e, b: {"id_token": "X", "issued_token_type": _ID_TT},
        )
    assert FAKE_SECRET not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_external_app_ingest_normalizes_store_value_error(monkeypatch):
    """store の入力検証 ValueError も IngestError に正規化する（BE06-007）。"""
    from jetuse_core.plugins import external_app_store, installer

    def fake_register(manifest, *, registered_by, name=None):
        raise ValueError("registered_by は 255 文字以内でなければならない")

    monkeypatch.setattr(external_app_store, "register_external_app", fake_register)
    m = denpyon_external_app_manifest(url=URL, issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(installer.IngestError):
        installer._ingest_contributes(m, "owner-x", visibility="private")
