"""実 Platform API ルート(PAPI-03)の API テスト。

各エンドポイントの冒頭 authorize(JWT 検証 → scope 強制 → テナント一致 → 監査)と、通過後の
委譲(db.query 読取限定)/配管(connector.invoke / rag.search)を検証する。監査は best-effort で
実 DB を触るため、ユニットでは `record_broker_access` を捕捉して DB に依存させない(記録は E2E)。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import nl2sql, rag
from jetuse_core import platform_broker as pb
from jetuse_core.plugins import connector_store
from jetuse_core.plugins.slack_connector_builtin import SLACK_CONNECTOR_ID
from jetuse_core.settings import Settings, get_settings
from service.main import app

SECRET = "papi03-test-broker-secret-32bytes!"
TENANT = "ocid1.generativeaiproject.oc1.ap-osaka-1.aaaaaaaatenanta"
TENANT_B = "ocid1.generativeaiproject.oc1.ap-osaka-1.bbbbbbbbtenantb"
PLUGIN = "acme/faq-summarizer"

DB_QUERY = "platform:db.query"
RAG_SEARCH = "platform:rag.search"
CONNECTOR_INVOKE = "platform:connector.invoke"


def _settings(secret: str = SECRET, ttl: int = 300) -> Settings:
    return Settings(platform_broker_secret=secret, platform_token_ttl_seconds=ttl)


@pytest.fixture
def audit(monkeypatch):
    """authorize が残す監査行を捕捉する(実 DB を触らせない)。"""
    records: list[dict] = []
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _token(scopes, *, tenant=TENANT, plugin=PLUGIN, secret=SECRET) -> str:
    return pb.issue_broker_token(plugin, tenant, scopes, settings=_settings(secret=secret))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- db.query: 読取限定で既存エンジンへ委譲 ----------------------------------


def test_db_query_happy_path_delegates_and_audits_allow(client, audit, monkeypatch):
    captured = {}

    def fake_exec(sql):
        captured["sql"] = sql
        return {"columns": ["X"], "rows": [[1]], "row_count": 1, "truncated": False}

    monkeypatch.setattr(nl2sql, "execute_readonly", fake_exec)
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 AS x FROM dual"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 200, res.text
    assert res.json()["row_count"] == 1
    assert captured["sql"] == "SELECT 1 AS x FROM dual"
    allows = [r for r in audit if r["decision"] == "ALLOW"]
    assert len(allows) == 1
    assert allows[0]["scope"] == DB_QUERY
    assert allows[0]["tenant"] == TENANT


def test_db_query_rejects_write_400(client, audit):
    # sanitize_sql(execute_readonly 内)が非 SELECT を pool 取得前に弾く → 読取限定の強制。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "UPDATE t SET a = 1"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 400, res.text
    # authorize は通っている(ALLOW 監査が残る)。拒否は読取限定違反として委譲先で起きる。
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_db_query_scope_denied_403(client, audit):
    # rag.search だけのトークンで db.query を要求 → scope_denied → 403、DENY 監査。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 403, res.text
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "scope_denied"


def test_db_query_tenant_mismatch_403(client, audit):
    # トークン tenant=TENANT だが要求 tenant=TENANT_B → tenant_mismatch → 403。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT_B, "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([DB_QUERY], tenant=TENANT)),
    )
    assert res.status_code == 403, res.text
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "tenant_mismatch"


def test_db_query_invalid_token_401(client, audit):
    bad = _token([DB_QUERY]) + "tampered"
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(bad),
    )
    assert res.status_code == 401, res.text
    assert any(r["decision"] == "DENY" for r in audit)


def test_db_query_missing_token_401(client, audit):
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
    )
    assert res.status_code == 401, res.text
    # 欠如(空トークン)も authorize を通って DENY 監査に残る(全アクセス監査)。
    assert any(r["decision"] == "DENY" for r in audit)


def test_db_query_wrong_secret_401(client, audit):
    # 別の鍵で署名されたトークンは検証で落ちる(fail-closed) → 401。
    forged = _token([DB_QUERY], secret="some-other-secret-32bytes-padding!")
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(forged),
    )
    assert res.status_code == 401, res.text


def test_db_query_broker_unconfigured_503(audit):
    # 署名鍵未設定 = BrokerConfigError → 503。audit fixture で実 DB を触らせない。
    app.dependency_overrides[get_settings] = lambda: _settings(secret="")
    try:
        c = TestClient(app)
        res = c.post(
            "/platform/db/query",
            json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
            headers=_auth("any.token.value"),
        )
        assert res.status_code == 503, res.text
    finally:
        app.dependency_overrides.clear()


def test_db_query_missing_tenant_422(client):
    res = client.post(
        "/platform/db/query",
        json={"tenant": "   ", "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 422, res.text


# --- connector.invoke: 実 invoke(BE-03 / コア builtin Slack 限定) ------------


def _slack_record(cid, *, plugin_id=SLACK_CONNECTOR_ID, source_version=None,
                  provider=None, transport=None):
    """実 Slack コネクタ定義(配布表現)を持つ登録レコード。既定はコア plugin・カノニカル版。

    register_connector が書く列(source_version=manifest.version / provider / transport)を
    既定でカノニカル値にして、版固定整合チェック(MAJ-001)を通す。版/provider/transport を明示
    上書きすると不整合(再インストール要求=409)の否定対照を作れる。
    """
    from jetuse_core.plugins.slack_connector_builtin import (
        slack_connector_definition,
        slack_connector_manifest,
    )

    d = slack_connector_definition()
    return {
        "id": cid,
        "plugin_id": plugin_id,
        "source_version": slack_connector_manifest().version
        if source_version is None
        else source_version,
        "provider": d.provider if provider is None else provider,
        "transport": d.transport if transport is None else transport,
        "definition": d.model_dump(by_alias=True),
    }


def _slack_token(scopes, *, tenant=TENANT, plugin=PLUGIN):
    # 実際の呼出主体は **コネクタ所有 plugin ではない L3 デモ**(sub=デモ自身の plugin_id)。
    # コア Slack は共有 capability なので所有一致は要求しない(BLK-001 の回帰防止)。
    return _token(scopes, tenant=tenant, plugin=plugin)


def _stub_secret(monkeypatch, token="xoxb-FAKE-NOT-A-REAL-TOKEN", *, captured=None):
    from service.routes import platform as platform_route

    def _factory(settings, **kw):
        if captured is not None:
            captured.update(kw)
        return lambda ref: token

    monkeypatch.setattr(platform_route, "_connector_secret_resolver", _factory)


def _stub_http(monkeypatch, fn):
    from jetuse_core.plugins import connector_runtime

    monkeypatch.setattr(connector_runtime, "live_http_caller", fn)


def test_connector_invoke_real_invoke_happy_path(client, audit, monkeypatch):
    # BE-03: 501 解除。**コネクタ所有 plugin ではない L3 デモ**(sub=acme/faq-summarizer)が
    # コア builtin Slack(所有=jetuse/slack-connector)を実 invoke できること=主要経路の到達性
    # (BLK-001 回帰防止)。実 HTTP/Vault は mock 注入(実 Slack 認証・実 Vault IAM は人間ゲート)。
    assert PLUGIN != SLACK_CONNECTOR_ID  # 呼出主体は所有 plugin と別であることを明示
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    captured: dict = {}
    _stub_secret(monkeypatch, captured=captured)
    calls: list[tuple] = []

    def _fake_http(url, headers, body):
        calls.append((url, headers, body))
        return {"ok": True, "channel": "C123", "ts": "1700000000.000100"}

    _stub_http(monkeypatch, _fake_http)

    res = client.post(
        "/platform/connector/invoke",
        json={
            "tenant": TENANT,
            "connector_id": "conn-1",
            "action": "post_message",
            "params": {"channel": "C123", "text": "hi from BE-03"},
        },
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["provider"] == "slack"
    assert body["transport"] == "builtin"
    assert body["output"]["ts"] == "1700000000.000100"
    # 実トークンは応答のどこにも出ない。
    assert "xoxb-FAKE-NOT-A-REAL-TOKEN" not in res.text
    # secret 束縛は **呼出デモ**(tenant＋呼出 plugin＋connector)に対して行う(所有 plugin ではない)。
    assert captured == {"tenant": TENANT, "plugin_id": PLUGIN, "connector_id": "conn-1"}
    # 認可 ALLOW は route(取得前認可)＋ invoke 層(多層防御)で各1回=計2回(二重監査は許容。MAJ-001)。
    allow = [r for r in audit if r["decision"] == "ALLOW" and r["scope"] == CONNECTOR_INVOKE]
    assert len(allow) == 2, audit
    # 実 HTTP は Slack chat.postMessage を1回叩く。
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/chat.postMessage")


def test_connector_invoke_secret_unresolved_503(client, audit, monkeypatch):
    # secret 解決不能(Vault 未マップ/IAM 障害)=サーバー設定/依存の問題 → 503(400 に潰さない)。
    # 外部 HTTP には一切到達しない(秘密解決は transport より前。外部副作用ゼロ)。
    from jetuse_core.plugins.connector_runtime import SecretResolutionError
    from service.routes import platform as platform_route

    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))

    def _deny_resolver(ref):
        raise SecretResolutionError(f"secretRef '{ref}' に対応する Vault secret OCID が未設定")

    monkeypatch.setattr(
        platform_route, "_connector_secret_resolver", lambda settings, **kw: _deny_resolver
    )
    called = {"http": False}
    _stub_http(monkeypatch, lambda *a, **k: called.__setitem__("http", True) or {"ok": True})

    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C123", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 503, res.text
    assert called["http"] is False


def test_connector_invoke_transport_error_502(client, audit, monkeypatch):
    # 外部 SaaS への到達/応答障害(上流側) → 502。
    from jetuse_core.plugins.connector_runtime import ConnectorTransportError

    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)

    def _boom(url, headers, body):
        raise ConnectorTransportError("SaaS API が非 2xx を返した (status=503)")

    _stub_http(monkeypatch, _boom)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C123", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 502, res.text


def test_connector_invoke_slack_logical_error_400(client, audit, monkeypatch):
    # Slack 論理エラー(ok:false, channel_not_found)=クライアント要求の不備 → 400。
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)
    _stub_http(monkeypatch, lambda *a, **k: {"ok": False, "error": "channel_not_found"})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C-bad", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 400, res.text


def test_connector_invoke_slack_auth_error_503(client, audit, monkeypatch):
    # Slack invalid_auth/missing_scope = Bot トークン/scope 設定不備(サーバー側) → 503。
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)
    _stub_http(monkeypatch, lambda *a, **k: {"ok": False, "error": "invalid_auth"})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 503, res.text


def test_connector_invoke_slack_upstream_error_502(client, audit, monkeypatch):
    # Slack internal_error/ratelimited = 上流一時障害 → 502。
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)
    _stub_http(monkeypatch, lambda *a, **k: {"ok": False, "error": "ratelimited"})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 502, res.text


def test_connector_invoke_non_core_connector_501(client, audit, monkeypatch):
    # コア以外(別 plugin 所属の builtin / MCP transport)は実行経路を開かない → 501(fail-closed)。
    # 呼出主体の所有一致は要求しない(BLK-001)ため、コア限定ゲートだけで非コアを 501 に倒す。
    rec = _slack_record("conn-x", plugin_id="acme/custom-slack")  # 非コア plugin
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: rec)
    _stub_secret(monkeypatch)
    _stub_http(monkeypatch, lambda *a, **k: {"ok": True})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-x", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 501, res.text


def test_connector_invoke_unprovisioned_caller_secret_503(client, audit, monkeypatch):
    # コア Slack へは到達するが (tenant, 呼出 plugin, connector) の secret 未プロビジョン → 503 で
    # Slack へ不達(呼出ごとの境界は所有一致ではなく secret 束縛が担う。BLK-001 の fail-closed 面)。
    from jetuse_core.plugins import connector_runtime
    from service.routes import platform as platform_route

    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    # 実 resolver を使い、connector_secret_ocids 未マップ(空)で 503 になることを通す。
    monkeypatch.setattr(
        platform_route,
        "_connector_secret_resolver",
        lambda settings, **kw: connector_runtime.make_vault_secret_resolver(
            _settings(), **kw
        ),
    )
    called = {"http": False}
    _stub_http(monkeypatch, lambda *a, **k: called.__setitem__("http", True) or {"ok": True})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 503, res.text
    assert called["http"] is False


def test_connector_invoke_scope_denied_403(client, audit, monkeypatch):
    # 認証は通るが connector.invoke 未付与 → invoke 層の認可で scope_denied → 403 + DENY 監査。
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "x"}},
        headers=_auth(_slack_token([DB_QUERY])),
    )
    assert res.status_code == 403, res.text
    assert any(r["decision"] == "DENY" and r["reason"] == "scope_denied" for r in audit)


def test_connector_invoke_invalid_token_401(client, audit, monkeypatch):
    # 認証失敗(壊れたトークン)→ verify で 401。DB へも触れない。
    touched = {"db": False}
    monkeypatch.setattr(
        connector_store, "get_connector",
        lambda cid: touched.__setitem__("db", True) or _slack_record(cid),
    )
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message"},
        headers=_auth("not-a-valid-jwt"),
    )
    assert res.status_code == 401, res.text
    assert touched["db"] is False


def test_connector_invoke_unknown_connector_404(client, audit, monkeypatch):
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: None)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "missing", "action": "x"},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 404, res.text


def test_connector_invoke_unknown_action_404(client, audit, monkeypatch):
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    _stub_secret(monkeypatch)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "delete_all"},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 404, res.text


# --- MAJ-001: 版固定スナップショット整合(source_version/provider/transport)----------


def test_connector_invoke_stale_source_version_409(client, audit, monkeypatch):
    # 旧 1.0.0 install 行(コア plugin だが版が古い)に対して現行カノニカル定義(1.1.0)を暗黙実行
    # しない。再インストール要求として 409 で拒否し、外部 HTTP には到達しない(MAJ-001)。
    rec = _slack_record("conn-1", source_version="1.0.0")
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: rec)
    _stub_secret(monkeypatch)
    called = {"http": False}
    _stub_http(monkeypatch, lambda *a, **k: called.__setitem__("http", True) or {"ok": True})
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 409, res.text
    assert "reinstall" in res.text
    assert called["http"] is False


def test_connector_invoke_provider_or_transport_mismatch_409(client, audit, monkeypatch):
    # コア plugin_id を名乗るが provider/transport が不整合の取込物/破損行 → 409(別 provider や
    # transport の行が Slack 挙動で動くのを防ぐ。MAJ-001)。
    rec = _slack_record("conn-1", provider="discord")
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: rec)
    _stub_secret(monkeypatch)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C", "text": "hi"}},
        headers=_auth(_slack_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 409, res.text


# --- BLK-001 方式A: 実 sample-app での synth→approve→issue→route 統合 ----------


def test_connector_invoke_real_sample_app_synth_approve_issue_route(
    client, audit, monkeypatch
):
    """方式A(ADR-0020 D7)の主要経路を**実際の出荷 sample-app**で端から端まで通す。

    架空の消費 manifest で迂回せず、実 SBA-A(`jetuse/support-desk`)を使い:
    synth(Q4=slack で Slack を active 束縛)→ required_scopes 収集 → 承認
    (`validate_grant_scopes` は SBA 自身の manifest.permissions に閉じる)→ issue_token(sub=SBA)
    → `/connector/invoke` 200。invoke 宣言が無ければ承認段で `scope_not_granted` になり到達不能
    だった(review-3 BLK-001)。実 HTTP/Vault は mock 注入(実 Slack/Vault/IAM は人間ゲート)。
    """
    from jetuse_core import platform_grants as pg
    from jetuse_core.deploy import _collect_required_scopes
    from jetuse_core.plugins.sample_app_builtin import sba_a_manifest
    from jetuse_core.recommend import recommend
    from jetuse_core.synth import synthesize

    answers = {"Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
               "Q4": "slack", "Q5": "chat_form", "Q6": "sample"}
    comp = synthesize(recommend(answers))
    assert "slack" in comp.active_connectors  # Slack が active 束縛されている

    sba_manifest = sba_a_manifest()
    sba_plugin = sba_manifest.id  # jetuse/support-desk
    assert sba_plugin != SLACK_CONNECTOR_ID  # 呼出主体はコネクタ所有 plugin ではない

    required = _collect_required_scopes(comp)
    assert CONNECTOR_INVOKE in required

    # 承認は **SBA 自身の manifest.permissions** に閉じる。方式A で invoke 宣言済みゆえ承認可。
    granted = pg.validate_grant_scopes(sba_manifest, required)
    assert CONNECTOR_INVOKE in granted

    # 承認済みグラント(sub=SBA)を stub し、broker トークンを発行する(発行は invoke のみ要求)。
    def fake_get_grant(tenant, plugin_id):
        assert (tenant, plugin_id) == (TENANT, sba_plugin)
        return {
            "id": "g1", "tenant": tenant, "plugin_id": plugin_id,
            "source_version": sba_manifest.version, "scopes": sorted(granted),
            "status": pg.GRANT_STATUS_ACTIVE, "approved_by": "sa@example.com",
            "created_at": "2026-06-29T00:00:00+00:00",
            "updated_at": "2026-06-29T00:00:00+00:00",
        }

    monkeypatch.setattr(pg, "get_grant", fake_get_grant)
    token = pg.issue_token(
        TENANT, sba_plugin, scopes=[CONNECTOR_INVOKE], settings=_settings()
    )

    # route: 実 invoke(コア Slack)。secret/http は mock 注入。
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: _slack_record(cid))
    captured: dict = {}
    _stub_secret(monkeypatch, captured=captured)
    _stub_http(
        monkeypatch,
        lambda url, headers, body: {"ok": True, "channel": "C1", "ts": "1.2"},
    )

    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message",
              "params": {"channel": "C1", "text": "通知です"}},
        headers=_auth(token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    # secret 束縛の plugin は呼出主体=SBA(support-desk)。所有 plugin ではない(BLK-001)。
    assert captured == {"tenant": TENANT, "plugin_id": sba_plugin, "connector_id": "conn-1"}


def test_real_sample_app_without_invoke_declaration_cannot_grant():
    """否定対照: invoke を宣言しない消費 manifest は承認段で拒否され到達不能(迂回不可)。

    SBA-A から connector.invoke を除いた manifest では、Slack を束ねても `validate_grant_scopes` が
    `GrantError` を投げる(approve は manifest.permissions に閉じる)。方式A の宣言が必須だと示す。
    """
    from jetuse_core import platform_grants as pg
    from jetuse_core.plugins.manifest import validate_manifest
    from jetuse_core.plugins.sample_app_builtin import sba_a_manifest

    m = sba_a_manifest()
    raw = m.model_dump(by_alias=True)
    raw["permissions"] = [p for p in raw["permissions"] if p != CONNECTOR_INVOKE]
    no_invoke = validate_manifest(raw)
    with pytest.raises(pg.GrantError):
        pg.validate_grant_scopes(no_invoke, [CONNECTOR_INVOKE])


# --- rag.search: OCI Responses file_search 委譲 ------------------------------


def test_rag_search_happy_path_delegates_and_audits_allow(client, audit, monkeypatch):
    captured = {}

    def fake_search(owner, query, *, top_k=5):
        captured["owner"] = owner
        captured["query"] = query
        captured["top_k"] = top_k
        return {
            "hits": [
                {"file_id": "f1", "filename": "請求書.pdf", "score": 0.91, "text": "..."}
            ],
            "citations": [
                {"file_id": "f1", "filename": "請求書.pdf", "score": 0.91}
            ],
            "answer": "請求書の支払期日は月末です。",
            "store_present": True,
        }

    monkeypatch.setattr(rag, "search", fake_search)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "請求書", "top_k": 3},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tenant"] == TENANT
    assert body["hits"][0]["file_id"] == "f1"
    assert body["citations"][0]["filename"] == "請求書.pdf"
    assert body["answer"]
    # テナント境界: 本体は broker 検証済みテナントをストア所有者キーに使う(呼び出し元は渡さない)。
    assert captured["owner"] == TENANT
    assert captured["query"] == "請求書"
    assert captured["top_k"] == 3
    allows = [r for r in audit if r["decision"] == "ALLOW"]
    assert len(allows) == 1
    assert allows[0]["scope"] == RAG_SEARCH
    assert allows[0]["tenant"] == TENANT


def test_rag_search_empty_store_returns_empty_200(client, audit, monkeypatch):
    # テナントに取り込み済み文書(ストア)が無い場合は越境ではなくデータ未取込 → 空ヒットの 200。
    monkeypatch.setattr(
        rag,
        "search",
        lambda owner, query, *, top_k=5: {
            "hits": [],
            "citations": [],
            "answer": "",
            "store_present": False,
        },
    )
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 200, res.text
    assert res.json()["hits"] == []
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_rag_search_upstream_error_502(client, audit, monkeypatch):
    # 委譲先(OCI Responses file_search)の失敗は曖昧に 200 へ倒さず 502(fail-closed)。
    def boom(owner, query, *, top_k=5):
        raise rag.RagSearchError("genai timeout")

    monkeypatch.setattr(rag, "search", boom)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 502, res.text
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_rag_search_upstream_error_hides_detail(client, audit, monkeypatch):
    # 502 のクライアント応答に上流例外文字列(vector_store_id 等を含みうる)を漏らさない。
    def boom(owner, query, *, top_k=5):
        raise rag.RagSearchError("vector_store_id=vs_kix_SECRET endpoint=https://internal")

    monkeypatch.setattr(rag, "search", boom)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 502, res.text
    assert "vs_kix_SECRET" not in res.text
    assert "internal" not in res.text


def test_rag_search_blank_query_422_no_delegation(client, audit, monkeypatch):
    # 空白のみの query は 422。rag.search に到達しない(課金 API を叩かない)。
    monkeypatch.setattr(rag, "search", lambda *a, **k: pytest.fail("must not delegate"))
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "   "},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 422, res.text


def test_rag_search_overlong_query_422_no_delegation(client, audit, monkeypatch):
    from service.routes.platform import MAX_RAG_QUERY_CHARS

    monkeypatch.setattr(rag, "search", lambda *a, **k: pytest.fail("must not delegate"))
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "あ" * (MAX_RAG_QUERY_CHARS + 1)},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 422, res.text


def test_rag_search_scope_denied_403(client, audit):
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 403, res.text


def test_rag_search_tenant_mismatch_403(client, audit, monkeypatch):
    # 別テナントのトークンで TENANT のストアを検索 → tenant_mismatch で 403(越境拒否 fail-closed)。
    # search に到達しないことも確認(認可で弾かれるため委譲が起きない)。
    called = {"n": 0}

    def fake_search(owner, query, *, top_k=5):
        called["n"] += 1
        return {"hits": [], "citations": [], "answer": "", "store_present": True}

    monkeypatch.setattr(rag, "search", fake_search)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH], tenant=TENANT_B)),
    )
    assert res.status_code == 403, res.text
    assert called["n"] == 0
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "tenant_mismatch"
