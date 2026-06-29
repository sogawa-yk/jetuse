"""BE-01: launch→OKE 配備配線（deploy_runtime.py）の単体テスト（dry-run 検証のみ）。

本タスクは「配線＋マニフェスト/レンダリング検証」まで。実 apply/delete/restart は人間ゲート。
runner スタブで実 kubectl を呼ばずに検証する:
  - 実行層ゲート（oke_deploy_enabled）／実 apply(None) の常時拒否（F-001）。
  - 連携なし構成（required_scopes 空）= base のみ dry-run 検証・注入なし。
  - Slack active 構成（required_scopes あり）= base＋注入を dry-run 検証。
  - 注入を要するのに tenant/plugin 欠落／grant 無し = base を検証せず fail-closed。
  - 命名一意化（instance_key）で複数 launch が衝突しない（F-005）。
  - teardown / refresh も dry-run 検証のみ。oke_deploy_dry_run=False は fail-closed（人間ゲート）。
DB 永続（grant 実書込）は get_grant スタブで代替。実 apply・実機 E2E は完了ゲートの SKIPPED に明記。
"""

import subprocess

import pytest

from jetuse_core import platform_grants as pg
from jetuse_core.deploy_runtime import (
    DeployRuntimeError,
    deploy_demo,
    refresh_injection,
    teardown_demo,
)
from jetuse_core.recommend import recommend
from jetuse_core.settings import Settings
from jetuse_core.synth import synthesize

_IMAGE = "kix.ocir.io/exampnamespace/jetuse-demo:latest"
_BASE_URL = "https://platform.example.ap-osaka-1.oci.example.com/platform"
TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
PLUGIN = "acme/demo-app"
_SCOPE = "platform:connector.invoke"


def _settings(**over) -> Settings:
    base = dict(
        oci_region="ap-osaka-1",
        platform_broker_secret="unit-broker-secret-32bytes-minimum!!",
        platform_token_ttl_seconds=300,
        platform_api_base_url="",
        hosted_demo_image_url="",
        kube_config_path="",
        oke_deploy_enabled=True,   # 実行層ゲート: テストは有効化前提（既定 OFF は別テストで確認）
        oke_deploy_dry_run=True,   # dry-run 検証のみ（実 apply は人間ゲート＝未対応）
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


def _composition(**over):
    return synthesize(recommend(_answers(**over)))


class _CapturingRunner:
    """kubectl 実行を記録するスタブ。verb ごとに決定的な出力を返す（実行はしない）。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, args, stdin):
        self.calls.append({"args": list(args), "stdin": stdin})
        if "apply" in args:
            if "--server-side" in args:
                out = "configmap/jetuse-runtime\nsecret/jetuse-token\n"
            else:
                out = (
                    "namespace/ns\nresourcequota/q\nserviceaccount/sa\n"
                    "configmap/cfg\ndeployment.apps/dep\nservice/svc\n"
                )
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    def applies(self):
        return [c for c in self.calls if "apply" in c["args"]]


def _stub_grant(monkeypatch, *, scopes, status=pg.GRANT_STATUS_ACTIVE):
    def fake_get_grant(tenant, plugin_id):
        if plugin_id != PLUGIN or tenant != TENANT:
            return None
        return {
            "id": "g-1", "tenant": tenant, "plugin_id": PLUGIN,
            "source_version": "1.0.0", "scopes": sorted(scopes), "status": status,
            "approved_by": "sa@example.com",
            "created_at": "2026-06-27T00:00:00+00:00",
            "updated_at": "2026-06-27T00:00:00+00:00",
        }

    monkeypatch.setattr(pg, "get_grant", fake_get_grant)


# --- 実行層ゲート / 人間ゲート（F-001） ------------------------------------


def test_deploy_disabled_is_failclosed():
    s = _settings(oke_deploy_enabled=False)
    runner = _CapturingRunner()
    with pytest.raises(DeployRuntimeError):
        deploy_demo(_composition(Q4="none"), settings=s, image_url=_IMAGE, runner=runner)
    assert runner.calls == []


def test_real_apply_is_failclosed_via_settings():
    # oke_deploy_dry_run=False（実操作要求）は本タスク未対応＝人間ゲートで拒否。
    s = _settings(oke_deploy_dry_run=False)
    runner = _CapturingRunner()
    with pytest.raises(DeployRuntimeError):
        deploy_demo(_composition(Q4="none"), settings=s, image_url=_IMAGE, runner=runner)
    assert runner.calls == []


def test_explicit_none_dry_run_rejected():
    # 公開引数 dry_run=None（実 apply）でも常に拒否（人間ゲートの引数バイパスを塞ぐ）。
    s = _settings()
    runner = _CapturingRunner()
    with pytest.raises(DeployRuntimeError):
        deploy_demo(_composition(Q4="none"), settings=s, image_url=_IMAGE,
                    dry_run=None, runner=runner)
    assert runner.calls == []


def test_deploy_default_dry_run_is_client():
    s = _settings()
    runner = _CapturingRunner()
    outcome = deploy_demo(_composition(Q4="none"), settings=s, image_url=_IMAGE, runner=runner)
    assert outcome.dry_run == "client"
    assert "--dry-run=client" in runner.applies()[0]["args"]
    assert outcome.deploy_status() == "validated"


def test_deploy_server_dry_run_explicit():
    s = _settings()
    runner = _CapturingRunner()
    outcome = deploy_demo(_composition(Q4="none"), settings=s, image_url=_IMAGE,
                          dry_run="server", runner=runner)
    assert outcome.dry_run == "server"
    assert "--dry-run=server" in runner.applies()[0]["args"]


# --- 連携なし: base のみ ----------------------------------------------------


def test_deploy_no_connector_applies_base_only():
    s = _settings()
    comp = _composition(Q4="none")  # required_scopes 空 → 注入不要
    runner = _CapturingRunner()

    outcome = deploy_demo(comp, settings=s, image_url=_IMAGE, runner=runner)

    assert len(runner.applies()) == 1  # base のみ
    assert "--server-side" not in runner.applies()[0]["args"]
    assert outcome.injected is False
    assert outcome.token_expires_at is None
    assert outcome.namespace.startswith("jetuse-demo-")
    assert outcome.namespace == outcome.service_name
    assert outcome.cluster_url.startswith(f"http://{outcome.service_name}.")
    assert "deployment.apps/dep" in outcome.resources


# --- 命名一意化（F-005） ---------------------------------------------------


def test_instance_key_makes_namespace_unique():
    s = _settings()
    comp = _composition(Q4="none")
    o1 = deploy_demo(comp, settings=s, image_url=_IMAGE, instance_key="sess-1",
                     runner=_CapturingRunner())
    o2 = deploy_demo(comp, settings=s, image_url=_IMAGE, instance_key="sess-2",
                     runner=_CapturingRunner())
    assert o1.namespace != o2.namespace  # 同 sample_app でも launch ごとに別 namespace


# --- 連携あり: base + 注入（server dry-run なら server-side） ----------------


def test_deploy_with_connector_validates_base_and_injection(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = _composition(Q4="slack")  # required_scopes に connector.invoke
    runner = _CapturingRunner()

    outcome = deploy_demo(
        comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
        image_url=_IMAGE, base_url=_BASE_URL, dry_run="server", runner=runner,
    )

    applies = runner.applies()
    assert len(applies) == 2  # base + injection
    # server dry-run のときだけ注入は server-side（client では併用不可＝F-009）。
    assert "--server-side" not in applies[0]["args"]
    assert "--server-side" in applies[1]["args"]
    assert outcome.injected is True
    assert outcome.token_expires_at is not None
    assert "secret/jetuse-token" in outcome.resources
    assert "deployment.apps/dep" in outcome.resources


def test_deploy_connector_client_dry_run_no_server_side(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = _composition(Q4="slack")
    runner = _CapturingRunner()

    deploy_demo(comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
                image_url=_IMAGE, base_url=_BASE_URL, runner=runner)  # 既定 client
    # client dry-run では注入 apply に server-side を付けない（F-009 回避）。
    assert all("--server-side" not in c["args"] for c in runner.applies())


def test_deploy_injection_required_but_no_tenant_is_failclosed_without_apply(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = _composition(Q4="slack")
    runner = _CapturingRunner()

    with pytest.raises(DeployRuntimeError):
        deploy_demo(comp, settings=s, plugin_id=PLUGIN, image_url=_IMAGE,
                    base_url=_BASE_URL, runner=runner)
    assert runner.applies() == []  # base を一切検証していない


def test_deploy_injection_grant_missing_does_not_apply_base(monkeypatch):
    s = _settings()
    monkeypatch.setattr(pg, "get_grant", lambda tenant, plugin_id: None)
    comp = _composition(Q4="slack")
    runner = _CapturingRunner()

    with pytest.raises(pg.GrantDenied):
        deploy_demo(comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
                    image_url=_IMAGE, base_url=_BASE_URL, runner=runner)
    assert runner.applies() == []


# --- teardown（dry-run 検証のみ） ------------------------------------------


def test_teardown_dry_run_validates_delete():
    s = _settings()
    runner = _CapturingRunner()
    res = teardown_demo("jetuse-demo-faq", settings=s, runner=runner)
    args = runner.calls[-1]["args"]
    assert args[1:4] == ["delete", "namespace", "jetuse-demo-faq"]
    assert "--dry-run=client" in args
    assert res.dry_run == "client"


def test_teardown_real_delete_is_failclosed():
    s = _settings(oke_deploy_dry_run=False)  # 実 delete は人間ゲート
    runner = _CapturingRunner()
    with pytest.raises(DeployRuntimeError):
        teardown_demo("jetuse-demo-faq", settings=s, runner=runner)
    assert runner.calls == []


def test_teardown_disabled_is_failclosed():
    s = _settings(oke_deploy_enabled=False)
    runner = _CapturingRunner()
    with pytest.raises(DeployRuntimeError):
        teardown_demo("jetuse-demo-faq", settings=s, runner=runner)
    assert runner.calls == []


# --- refresh（dry-run 検証のみ） ------------------------------------------


def test_refresh_validates_secret_reapply_and_rollout(monkeypatch):
    s = _settings()
    _stub_grant(monkeypatch, scopes=[_SCOPE])
    comp = _composition(Q4="slack")
    runner = _CapturingRunner()

    out = refresh_injection(
        comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
        image_url=_IMAGE, base_url=_BASE_URL, dry_run="server", runner=runner,
    )
    assert out.restarted is True
    assert out.token_expires_at
    assert any("--server-side" in c["args"] for c in runner.applies())
    assert any("rollout" in c["args"] and "restart" in c["args"] for c in runner.calls)


def test_refresh_without_scopes_is_rejected():
    s = _settings()
    comp = _composition(Q4="none")  # required_scopes 空
    with pytest.raises(DeployRuntimeError):
        refresh_injection(comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
                          image_url=_IMAGE, base_url=_BASE_URL, runner=_CapturingRunner())


def test_refresh_real_is_failclosed():
    s = _settings(oke_deploy_dry_run=False)
    comp = _composition(Q4="slack")
    with pytest.raises(DeployRuntimeError):
        refresh_injection(comp, settings=s, tenant=TENANT, plugin_id=PLUGIN,
                          image_url=_IMAGE, base_url=_BASE_URL, runner=_CapturingRunner())
