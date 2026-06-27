"""DEP-02: 生成デモコンテナへの Platform API ランタイム注入の単体テスト。

代表構成(SBA-A + Slack active = required_scopes に platform:connector.invoke)から、コンテナ起動時の
注入バンドル(base_url 非秘密 + 短期トークン秘密)を組み立て、承認スコープ閉包・DB 認証情報非注入・
失効/更新・fail-closed 境界を検証する。

DB 永続化(approve_scopes / get_grant の実書込)は実 ADB の E2E(runs/<run-id>/e2e/)で確認する。
本ユニットでは get_grant をスタブして発行フローの分岐(no_grant / grant_revoked / scope_not_granted /
承認に閉じる)を注入経路で検証する(test_platform_grants と同方針)。
"""

from datetime import UTC, datetime, timedelta

import pytest

from jetuse_core import platform_grants as pg
from jetuse_core.deploy import build_deploy_spec
from jetuse_core.deploy_inject import (
    PLATFORM_API_BASE_URL_ENV,
    PLATFORM_TOKEN_ENV,
    InjectionError,
    build_runtime_injection,
    container_start_environment,
    should_refresh,
)
from jetuse_core.platform_broker import verify_broker_token
from jetuse_core.recommend import recommend
from jetuse_core.settings import Settings
from jetuse_core.synth import synthesize

_IMAGE = "kix.ocir.io/exampnamespace/jetuse-demo:latest"
_BASE_URL = "https://platform.example.ap-osaka-1.oci.example.com/platform"
_VAULT_OCID = "ocid1.vaultsecret.oc1.ap-osaka-1.amaaaaaaexamplesecret"
TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
PLUGIN = "acme/demo-app"
_SCOPE = "platform:connector.invoke"


def _settings(**over) -> Settings:
    base = dict(
        oci_region="ap-osaka-1",
        platform_broker_secret="unit-broker-secret-32bytes-minimum!!",
        platform_token_ttl_seconds=300,
        platform_api_base_url="",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


def _spec(settings=None, **over):
    settings = settings or _settings()
    comp = synthesize(recommend(_answers(**over)))
    return build_deploy_spec(comp, settings=settings, image_url=_IMAGE)


def _stub_grant(monkeypatch, *, scopes, status=pg.GRANT_STATUS_ACTIVE, exists=True):
    def fake_get_grant(tenant, plugin_id):
        if not exists or tenant != TENANT or plugin_id != PLUGIN:
            return None
        return {
            "id": "g-1",
            "tenant": TENANT,
            "plugin_id": PLUGIN,
            "source_version": "1.0.0",
            "scopes": sorted(scopes),
            "status": status,
            "approved_by": "sa@example.com",
            "created_at": "2026-06-27T00:00:00+00:00",
            "updated_at": "2026-06-27T00:00:00+00:00",
        }

    monkeypatch.setattr(pg, "get_grant", fake_get_grant)


# --- 正常系 ----------------------------------------------------------------


def test_injection_carries_base_url_and_short_lived_token(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)

    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )

    # 非秘密 env は base_url のみ。トークンは秘密 env に分離。
    assert inj.env() == {PLATFORM_API_BASE_URL_ENV: _BASE_URL}
    assert inj.secret_env() == {PLATFORM_TOKEN_ENV: inj.token}
    assert PLATFORM_TOKEN_ENV not in inj.env()
    # トークンは承認スコープに厳密に閉じた短期 JWT。検証して載ったスコープ・失効を確認。
    ctx = verify_broker_token(inj.token, settings=s)
    assert ctx.scopes == frozenset({_SCOPE})
    assert ctx.tenant == TENANT
    assert ctx.plugin_id == PLUGIN
    assert inj.scopes == (_SCOPE,)
    assert inj.seconds_remaining() > 0
    assert not inj.is_expired()


def test_base_url_falls_back_to_settings(monkeypatch):
    s = _settings(platform_api_base_url=_BASE_URL)
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    inj = build_runtime_injection(_spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s)
    assert inj.base_url == _BASE_URL


def test_redacted_masks_token(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    inj = build_runtime_injection(
        _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    red = inj.redacted()
    assert red["token"] == "***redacted***"
    assert inj.token not in str(red)


# --- DB 認証情報を注入しない(D5) -------------------------------------------


def test_no_db_credentials_in_injection(monkeypatch):
    s = _settings(adb_password="super-secret-db-pw", adb_dsn="jetuseloop_low")
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    inj = build_runtime_injection(
        _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    nonsecret, secret = container_start_environment(_spec(s), inj)
    blob = " ".join([*nonsecret.keys(), *nonsecret.values(), *secret.keys()])
    # DB 資格情報名も実値も注入物に現れない。
    for needle in ("super-secret-db-pw", "PASSWORD", "ADB_", "DB_PASS", "DSN", "WALLET"):
        assert needle not in blob
    # 秘密 env はトークンのみ(キー名で資格情報を運ばない)。
    assert set(secret) == {PLATFORM_TOKEN_ENV}


# --- 承認スコープに厳密に閉じる --------------------------------------------


def test_scope_outside_deploy_spec_rejected(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE, "platform:db.query"])
    spec = _spec(s)  # required_scopes = (connector.invoke,) のみ
    with pytest.raises(InjectionError):
        build_runtime_injection(
            spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL,
            scopes=["platform:db.query"],  # 配備仕様の宣言外
        )


def test_empty_requested_scopes_rejected(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    with pytest.raises(InjectionError):
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s,
            base_url=_BASE_URL, scopes=[],
        )


def test_token_strictly_closed_to_grant_scope_not_granted(monkeypatch):
    # 配備仕様は connector.invoke を要求するが、承認グラントに connector.invoke が無い
    # → issue_token が scope_not_granted で拒否(トークン未発行 / fail-closed)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=["platform:rag.search"])
    with pytest.raises(pg.GrantDenied) as e:
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )
    assert e.value.reason == "scope_not_granted"


# --- 失効 / グラント不在(fail-closed) -------------------------------------


def test_no_grant_refused(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE], exists=False)
    with pytest.raises(pg.GrantDenied) as e:
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )
    assert e.value.reason == "no_grant"


def test_revoked_grant_refused(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE], status=pg.GRANT_STATUS_REVOKED)
    with pytest.raises(pg.GrantDenied) as e:
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )
    assert e.value.reason == "grant_revoked"


# --- base_url 検証(fail-closed) -------------------------------------------


def test_missing_base_url_refused(monkeypatch):
    s = _settings(platform_api_base_url="")
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    with pytest.raises(InjectionError):
        build_runtime_injection(_spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s)


def test_plain_http_base_url_refused(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    with pytest.raises(InjectionError):
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s,
            base_url="http://platform.example.com/platform",
        )


def test_vault_ocid_in_base_url_refused(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    with pytest.raises(InjectionError):
        build_runtime_injection(
            _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s,
            base_url=f"https://platform.example.com/{_VAULT_OCID}",
        )


def test_spec_without_scopes_refused(monkeypatch):
    # SBA-A 単体(コネクタ無し)= required_scopes 空 → 注入は不要として拒否。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s, Q4="none")
    assert spec.required_scopes == ()
    with pytest.raises(InjectionError):
        build_runtime_injection(
            spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )


# --- 起動 env 合流 / キー衝突 ----------------------------------------------


def test_container_start_environment_merges_nonsecret_and_separates_secret(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    nonsecret, secret = container_start_environment(spec, inj)
    # 配備仕様の非秘密 env + base_url が非秘密側に、トークンは秘密側に。
    assert PLATFORM_API_BASE_URL_ENV in nonsecret
    assert "OCI_REGION" in nonsecret
    assert secret == {PLATFORM_TOKEN_ENV: inj.token}
    assert PLATFORM_TOKEN_ENV not in nonsecret
    # 決定的(キーソート済み)。
    assert list(nonsecret) == sorted(nonsecret)


# --- 更新(refresh)方針 ----------------------------------------------------


def test_should_refresh_near_expiry(monkeypatch):
    s = _settings(platform_token_ttl_seconds=300)
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    inj = build_runtime_injection(
        _spec(s), tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    now = datetime.now(UTC)
    # 発行直後は更新不要、失効間際は更新要。
    assert not should_refresh(inj, now=now, skew_seconds=30)
    assert should_refresh(inj, now=inj.expires_at - timedelta(seconds=10), skew_seconds=30)
    assert inj.is_expired(now=inj.expires_at + timedelta(seconds=1))
