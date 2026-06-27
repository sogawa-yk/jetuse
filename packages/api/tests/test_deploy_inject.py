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
    return build_deploy_spec(comp, settings=settings, image_url=_IMAGE, plugin_id=PLUGIN)


def _stub_grant(monkeypatch, *, scopes, status=pg.GRANT_STATUS_ACTIVE, exists=True,
                tenant_match_any=False):
    def fake_get_grant(tenant, plugin_id):
        if not exists or plugin_id != PLUGIN:
            return None
        if not tenant_match_any and tenant != TENANT:
            return None
        return {
            "id": "g-1",
            "tenant": tenant,
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


def test_tenant_mismatch_against_spec_refused(monkeypatch):
    # F-001 回帰: tenant ハッシュ付き spec(= tenant A の namespace/Secret)に、別テナント B の
    # トークンを注入しようとしたら fail-closed(別テナントの Secret 上書き=分離破壊を防ぐ)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = synthesize(recommend(_answers()))
    spec_a = build_deploy_spec(comp, settings=s, image_url=_IMAGE, tenant=TENANT, plugin_id=PLUGIN)
    with pytest.raises(InjectionError):
        build_runtime_injection(
            spec_a, tenant="ocid1.tenancy.oc1..aaaa-tenant-B",
            plugin_id=PLUGIN, settings=s, base_url=_BASE_URL,
        )


def test_render_with_foreign_spec_refused(monkeypatch):
    # F-001 回帰: tenant B で発行した注入を tenant A の spec で描画/起動しようとしたら fail-closed。
    # (render_secret_manifest / render_runtime_configmap / container_start_environment すべて)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE], tenant_match_any=True)
    comp = synthesize(recommend(_answers()))
    spec_a = build_deploy_spec(comp, settings=s, image_url=_IMAGE, tenant=TENANT, plugin_id=PLUGIN)
    tenant_b = "ocid1.tenancy.oc1..aaaa-tenant-B"
    spec_b = build_deploy_spec(comp, settings=s, image_url=_IMAGE,
                               tenant=tenant_b, plugin_id=PLUGIN)
    inj_b = build_runtime_injection(
        spec_b, tenant=tenant_b, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    with pytest.raises(InjectionError):
        inj_b.render_secret_manifest(spec_a)
    with pytest.raises(InjectionError):
        inj_b.render_runtime_configmap(spec_a)
    with pytest.raises(InjectionError):
        inj_b.render_injection_manifests(spec_a)
    with pytest.raises(InjectionError):
        container_start_environment(spec_a, inj_b)
    # 自分の spec(B)になら描画できる(正常系)。
    assert inj_b.render_secret_manifest(spec_b)["metadata"]["namespace"] == spec_b.namespace


def test_plugin_swap_against_spec_refused_in_core(monkeypatch):
    # blocker 回帰: spec.plugin_id=A の Secret に別 plugin=B のグラントで発行しようとしても
    # core(build_runtime_injection)が fail-closed(CLI live-check だけに依存しない多層防御)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE], tenant_match_any=True)
    comp = synthesize(recommend(_answers()))
    spec_a = build_deploy_spec(comp, settings=s, image_url=_IMAGE, tenant=TENANT, plugin_id=PLUGIN)
    with pytest.raises(InjectionError):
        build_runtime_injection(
            spec_a, tenant=TENANT, plugin_id="evil/other-plugin", settings=s, base_url=_BASE_URL
        )


def test_injectable_spec_without_plugin_refused(monkeypatch):
    # major 回帰: scoped(注入が要る)spec を plugin 未固定で deploy → 注入は ground truth が無いため
    # core で fail-closed(別プラグインへのすり替えを防ぐ。plugin 固定 deploy を要求)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = synthesize(recommend(_answers()))
    spec_noplugin = build_deploy_spec(comp, settings=s, image_url=_IMAGE, tenant=TENANT)
    assert spec_noplugin.needs_platform_injection and not spec_noplugin.plugin_id
    with pytest.raises(InjectionError):
        build_runtime_injection(
            spec_noplugin, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_tenant_injection_refused(monkeypatch, bad):
    # 空/空白 tenant はトークン発行前に fail-closed(環境変数展開ミス対策)。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    with pytest.raises(InjectionError):
        build_runtime_injection(
            _spec(s), tenant=bad, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
        )


def test_tenant_match_against_spec_ok(monkeypatch):
    # 同一 tenant なら注入は成立し、Secret 名は spec(tenant ハッシュ込み)と一致する。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = synthesize(recommend(_answers()))
    spec_a = build_deploy_spec(comp, settings=s, image_url=_IMAGE, tenant=TENANT, plugin_id=PLUGIN)
    inj = build_runtime_injection(
        spec_a, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    assert inj.token
    assert spec_a.token_secret_name.endswith("-platform-token")


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


# ---- K8s(OKE)注入マニフェスト描画(ADR-0017 §5) ----


def test_render_secret_manifest_carries_token_and_correct_naming(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    secret = inj.render_secret_manifest(spec)

    assert secret["kind"] == "Secret"
    assert secret["type"] == "Opaque"
    # 名前/namespace は deploy.py の命名規約に一致(Deployment の envFrom と整合)。
    assert secret["metadata"]["name"] == spec.token_secret_name
    assert secret["metadata"]["namespace"] == spec.namespace
    # トークンは base64 済み data のみ(allowlist キー)。server-side apply で決定的に更新される。
    import base64
    assert secret["data"] == {
        PLATFORM_TOKEN_ENV: base64.b64encode(inj.token.encode()).decode()
    }
    assert "stringData" not in secret
    # 失効時刻は annotation で公開(非秘密)。トークン値(平文/base64)は annotation/label に出さない。
    ann = secret["metadata"]["annotations"]
    assert ann["jetuse.dev/token-expires-at"] == inj.expires_at.isoformat()
    assert inj.token not in str(secret["metadata"])
    assert base64.b64encode(inj.token.encode()).decode() not in str(secret["metadata"])


def test_render_runtime_configmap_is_nonsecret_base_url(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    cm = inj.render_runtime_configmap(spec)

    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == spec.runtime_config_map_name
    # base_url(非秘密)のみ。トークンは絶対に載らない。
    assert cm["data"] == {PLATFORM_API_BASE_URL_ENV: _BASE_URL}
    assert inj.token not in str(cm)


def test_injection_manifests_token_only_in_secret(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    manifests = inj.render_injection_manifests(spec)
    kinds = [m["kind"] for m in manifests]
    assert kinds == ["ConfigMap", "Secret"]
    # トークン(base64 済み)は Secret のドキュメントにのみ現れる(ConfigMap には現れない)。
    import base64
    b64 = base64.b64encode(inj.token.encode()).decode()
    cm, secret = manifests
    assert inj.token not in str(cm) and b64 not in str(cm)
    assert b64 in str(secret)


def test_refresh_reissues_new_token_in_secret(monkeypatch):
    # ADR-0017 §6: 更新は build_runtime_injection 再呼び出し → Secret を新値で再 apply。
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj1 = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    inj2 = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    # 呼び出しごと発行(リプレイ露出窓の最小化)。Secret の中身が更新される。
    assert inj1.token != inj2.token
    sec1 = inj1.render_secret_manifest(spec)
    sec2 = inj2.render_secret_manifest(spec)
    # 同一 Secret 名(= in-place 更新 → rolling restart で反映)。base64 data は別トークン。
    assert sec1["metadata"]["name"] == sec2["metadata"]["name"]
    assert sec1["data"] != sec2["data"]


def test_should_refresh_true_when_near_expiry(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    spec = _spec(s)
    inj = build_runtime_injection(
        spec, tenant=TENANT, plugin_id=PLUGIN, settings=s, base_url=_BASE_URL
    )
    # 失効直前(skew 内)なら更新すべき。
    near = inj.expires_at - timedelta(seconds=5)
    assert should_refresh(inj, now=near, skew_seconds=30) is True
    # まだ余裕があれば更新不要。
    early = datetime.now(UTC)
    assert should_refresh(inj, now=early, skew_seconds=30) is False
