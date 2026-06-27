"""Platform API ブローカー認可コアのテスト(PAPI-01 / ADR-0014)。

正常系(発行→検証→スコープ強制→テナント一致)と拒否系(未知スコープ・期限切れ・改竄・
テナント越境・スコープ不足・鍵未設定の fail-closed)を網羅する。DB 監査は別の E2E で実環境確認する
(本ユニットでは audit=False で配管のみ検証)。
"""

from datetime import UTC, datetime, timedelta

import pytest

from jetuse_core.platform_broker import (
    AUDIENCE,
    ISSUER,
    MAX_TTL_SECONDS,
    BrokerConfigError,
    BrokerContext,
    BrokerDenied,
    authorize,
    issue_broker_token,
    verify_broker_token,
)
from jetuse_core.settings import Settings

TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
TENANT_B = "ocid1.tenancy.oc1..bbbb-tenant-B"
PLUGIN = "acme/support-demo"


def _settings(secret: str = "spike-broker-secret-32bytes-min!!", ttl: int = 300) -> Settings:
    return Settings(platform_broker_secret=secret, platform_token_ttl_seconds=ttl)


# --- 発行 → 検証 の往復(正常系) -------------------------------------------


def test_issue_and_verify_roundtrip():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    ctx = verify_broker_token(token, settings=s)
    assert isinstance(ctx, BrokerContext)
    assert ctx.plugin_id == PLUGIN
    assert ctx.tenant == TENANT
    assert ctx.scopes == frozenset({"platform:rag.search"})
    assert ctx.jti  # 監査・失効の継ぎ目として一意 ID が入る
    assert ctx.expires_at > datetime.now(UTC)


def test_token_claims_are_fixed_iss_aud():
    import jwt

    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:db.query"], settings=s)
    # 署名を信頼せずに中身だけ覗いて iss/aud/tenant/scope を確認する。
    claims = jwt.decode(token, options={"verify_signature": False}, audience=AUDIENCE)
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE
    assert claims["sub"] == PLUGIN
    assert claims["tenant"] == TENANT
    assert claims["scope"] == "platform:db.query"
    assert claims["jti"]


def test_multiple_scopes_sorted_and_preserved():
    s = _settings()
    token = issue_broker_token(
        PLUGIN, TENANT, ["platform:files.write", "platform:files.read"], settings=s
    )
    ctx = verify_broker_token(token, settings=s)
    assert ctx.scopes == frozenset({"platform:files.read", "platform:files.write"})


# --- 発行側の入力検証 -------------------------------------------------------


def test_issue_rejects_unknown_scope():
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        issue_broker_token(PLUGIN, TENANT, ["platform:admin.everything"], settings=s)
    assert ei.value.reason == "unknown_scope"


def test_issue_rejects_empty_scope():
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        issue_broker_token(PLUGIN, TENANT, [], settings=s)
    assert ei.value.reason == "empty_scope"


def test_issue_rejects_blank_plugin_and_tenant():
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        issue_broker_token("  ", TENANT, ["platform:rag.search"], settings=s)
    assert ei.value.reason == "missing_plugin"
    with pytest.raises(BrokerDenied) as ej:
        issue_broker_token(PLUGIN, "", ["platform:rag.search"], settings=s)
    assert ej.value.reason == "missing_tenant"


def test_issue_rejects_ttl_over_max():
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        issue_broker_token(
            PLUGIN, TENANT, ["platform:rag.search"], settings=s, ttl_seconds=MAX_TTL_SECONDS + 1
        )
    assert ei.value.reason == "bad_ttl"


def test_issue_rejects_nonpositive_ttl():
    s = _settings()
    with pytest.raises(BrokerDenied):
        issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s, ttl_seconds=0)


# --- 検証の fail-closed ----------------------------------------------------


def test_verify_rejects_expired_token():
    s = _settings()
    past = datetime.now(UTC) - timedelta(hours=1)
    token = issue_broker_token(
        PLUGIN, TENANT, ["platform:rag.search"], settings=s, ttl_seconds=60, now=past
    )
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(token, settings=s)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_not_yet_valid_token():
    s = _settings()
    future = datetime.now(UTC) + timedelta(hours=1)
    token = issue_broker_token(
        PLUGIN, TENANT, ["platform:rag.search"], settings=s, now=future
    )
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(token, settings=s)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_tampered_signature():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(tampered, settings=s)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_wrong_secret():
    s_issue = _settings(secret="secret-one-issuer-side-aaaaaaaaaa")
    s_verify = _settings(secret="secret-two-verifier-side-bbbbbbb")
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s_issue)
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(token, settings=s_verify)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_unknown_scope_smuggled_in():
    # 別経路で鋳造され未知スコープが載ったトークンを、検証側でも弾く(語彙の二重チェック)。
    import jwt

    s = _settings()
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": PLUGIN,
            "tenant": TENANT,
            "scope": "platform:rag.search platform:admin.everything",
            "jti": "x",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=300),
        },
        s.platform_broker_secret,
        algorithm="HS256",
    )
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(forged, settings=s)
    assert ei.value.reason == "unknown_scope"


def _forge(s: Settings, **overrides) -> str:
    """必須 claim をすべて満たす土台を作り、overrides で個別に欠落/改変させる。"""
    import jwt

    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": PLUGIN,
        "tenant": TENANT,
        "scope": "platform:rag.search",
        "jti": "x",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=300),
    }
    for k, v in overrides.items():
        if v is _OMIT:
            payload.pop(k, None)
        else:
            payload[k] = v
    return jwt.encode(payload, s.platform_broker_secret, algorithm="HS256")


_OMIT = object()


def test_verify_rejects_absent_tenant_claim():
    # tenant claim を完全に欠くトークンは require で弾かれる(invalid_token)。
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(_forge(s, tenant=_OMIT), settings=s)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_blank_tenant_claim():
    # tenant claim は在るが空文字 → 明示チェックで missing_tenant。
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(_forge(s, tenant=""), settings=s)
    assert ei.value.reason == "missing_tenant"


def test_verify_rejects_absent_jti_claim():
    # jti 欠落トークン(scope はある)を許可して監査 jti が空になる穴を塞ぐ。
    s = _settings()
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(_forge(s, jti=_OMIT), settings=s)
    assert ei.value.reason == "invalid_token"


def test_verify_rejects_wrong_audience():
    import jwt

    s = _settings()
    now = datetime.now(UTC)
    wrong_aud = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "some-other-api",
            "sub": PLUGIN,
            "tenant": TENANT,
            "scope": "platform:rag.search",
            "jti": "x",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=300),
        },
        s.platform_broker_secret,
        algorithm="HS256",
    )
    with pytest.raises(BrokerDenied) as ei:
        verify_broker_token(wrong_aud, settings=s)
    assert ei.value.reason == "invalid_token"


# --- 鍵未設定 = fail-closed ------------------------------------------------


def test_no_secret_blocks_issue_and_verify():
    s = _settings(secret="")
    with pytest.raises(BrokerConfigError):
        issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    with pytest.raises(BrokerConfigError):
        verify_broker_token("anything", settings=s)


# --- スコープ強制 / テナント境界 -------------------------------------------


def test_require_scope_and_tenant():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    ctx = verify_broker_token(token, settings=s)
    ctx.require_scope("platform:rag.search")  # 通る
    ctx.require_tenant(TENANT)  # 通る
    with pytest.raises(BrokerDenied) as ei:
        ctx.require_scope("platform:db.query")
    assert ei.value.reason == "scope_denied"
    with pytest.raises(BrokerDenied) as ej:
        ctx.require_tenant(TENANT_B)
    assert ej.value.reason == "tenant_mismatch"


# --- authorize: 仲介本体(監査は無効化して配管のみ検証) ----------------------


def test_authorize_allows_matching_request():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    ctx = authorize(
        token, "platform:rag.search", tenant=TENANT, settings=s, audit=False
    )
    assert ctx.plugin_id == PLUGIN
    assert ctx.tenant == TENANT


def test_authorize_denies_cross_tenant():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    with pytest.raises(BrokerDenied) as ei:
        authorize(token, "platform:rag.search", tenant=TENANT_B, settings=s, audit=False)
    assert ei.value.reason == "tenant_mismatch"


def test_authorize_denies_missing_scope():
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    with pytest.raises(BrokerDenied) as ei:
        authorize(token, "platform:db.query", tenant=TENANT, settings=s, audit=False)
    assert ei.value.reason == "scope_denied"


def test_authorize_cross_tenant_takes_precedence_over_scope():
    # 別テナント かつ scope 不足のとき、scope_denied で上書きせず必ず tenant_mismatch を記録する
    # (ADR-0014 §3: 越境は tenant_mismatch として監査に残す)。
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    with pytest.raises(BrokerDenied) as ei:
        # 要求 scope=db.query(未付与) かつ tenant=B(越境)。
        authorize(token, "platform:db.query", tenant=TENANT_B, settings=s, audit=False)
    assert ei.value.reason == "tenant_mismatch"


def test_authorize_rejects_unknown_required_scope():
    # PAPI-03 実装がスコープ名を typo しても scope_denied で素通りさせず、入口で unknown_scope。
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)
    with pytest.raises(BrokerDenied) as ei:
        authorize(token, "platform:rag.serch", tenant=TENANT, settings=s, audit=False)
    assert ei.value.reason == "unknown_scope"


def test_authorize_records_audit_best_effort(monkeypatch):
    # 監査の継ぎ目を検証: authorize(audit=True) が record_broker_access を ALLOW/DENY で呼ぶ。
    # DB は使わず record_broker_access をスタブして引数を捕捉する(実 DB 記録は E2E で確認)。
    import jetuse_core.platform_broker as pb

    calls: list[dict] = []
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: calls.append(kw))
    s = _settings()
    token = issue_broker_token(PLUGIN, TENANT, ["platform:rag.search"], settings=s)

    pb.authorize(token, "platform:rag.search", tenant=TENANT, settings=s)
    assert calls[-1]["decision"] == "ALLOW"
    assert calls[-1]["plugin_id"] == PLUGIN

    with pytest.raises(BrokerDenied):
        pb.authorize(token, "platform:rag.search", tenant=TENANT_B, settings=s)
    assert calls[-1]["decision"] == "DENY"
    assert calls[-1]["reason"] == "tenant_mismatch"
    # 越境試行でも tenant(要求側)は監査に残る。
    assert calls[-1]["tenant"] == TENANT_B


def test_authorize_audits_config_error(monkeypatch):
    # 鍵未設定でも authorize は DENY を監査してから BrokerConfigError を送出する
    # (fail-closed の監査が設定不備で穴あきにならない)。
    import jetuse_core.platform_broker as pb

    calls: list[dict] = []
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: calls.append(kw))
    s = _settings(secret="")  # 鍵未設定
    with pytest.raises(BrokerConfigError):
        pb.authorize("any.token.here", "platform:rag.search", tenant=TENANT, settings=s)
    assert calls[-1]["decision"] == "DENY"
    assert calls[-1]["reason"] == "broker_unconfigured"
