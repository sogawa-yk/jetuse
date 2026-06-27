"""tools/render_injection.py の live-spec 照合(deploy-spec 閉包の運用迂回防止)の単体テスト。

注入 CLI は再構築 spec の required_scopes が **実デプロイ済み Deployment の注釈(ground truth)** と
一致することを fail-closed 検証する。deploy 時と違う answers を渡して宣言外スコープのトークンを
同名 Secret へ上書きする経路を塞ぐ(review F-001/blocker 対応)。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

import pytest

from jetuse_core.deploy import build_deploy_spec
from jetuse_core.recommend import recommend
from jetuse_core.settings import Settings
from jetuse_core.synth import synthesize

# tools/ はリポジトリルート直下(packages/api の外)。テストから import するため path を通す。
_TOOLS = pathlib.Path(__file__).resolve().parents[3] / "tools"
sys.path.insert(0, str(_TOOLS))

import render_injection as ri  # noqa: E402

_IMAGE = "kix.ocir.io/exampnamespace/jetuse-demo:latest"
_TENANT = "ocid1.tenancy.oc1..aaaa-tenant-A"
_ANSWERS = {"Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
            "Q4": "slack", "Q5": "chat_form", "Q6": "sample"}


def _settings() -> Settings:
    return Settings(_env_file=None, oci_region="ap-osaka-1",
                    platform_broker_secret="e2e-broker-secret-32bytes-minimum!!",
                    platform_token_ttl_seconds=300, platform_api_base_url="https://p.example/v1")


_PLUGIN = "acme/demo-app"


def _spec(tenant=_TENANT):
    comp = synthesize(recommend(_ANSWERS))
    return build_deploy_spec(comp, settings=_settings(), image_url=_IMAGE,
                             prefix="jetuse-demo-faq", tenant=tenant, plugin_id=_PLUGIN)


def _runner(stdout: str, rc: int = 0, stderr: str = ""):
    def run(cmd):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)
    return run


def _live(scopes, plugin=_PLUGIN):
    """`_fetch_live_annotations` が読む jsonpath 出力(`<scopes>|<plugin>`)を組み立てる。"""
    return _runner(f"{','.join(scopes)}|{plugin}")


def test_parse_scopes_splits_and_strips():
    assert ri._parse_scopes("a, b ,, c") == frozenset({"a", "b", "c"})
    assert ri._parse_scopes("") == frozenset()
    assert ri._parse_scopes(None) == frozenset()


def test_verify_against_live_ok_when_matches():
    spec = _spec()
    # 一致(scopes＋plugin)→ 例外なし(正常系: deploy 時と同じ answers/prefix/tenant/plugin)。
    ri._verify_against_live(spec, _live(spec.required_scopes), _PLUGIN)


def test_verify_against_live_rejects_when_live_differs():
    # 迂回経路の再現: deploy 時と違う answers を渡すと再構築 spec の required_scopes が実デプロイの
    # ground truth(live Deployment 注釈)と食い違う。一致しなければ fail-closed(同名 Secret へ
    # 宣言外スコープのトークンを上書きできない)。
    spec = _spec()
    assert frozenset(spec.required_scopes) != ri._parse_scopes("platform:other.scope")
    with pytest.raises(ri.LiveSpecMismatch):
        ri._verify_against_live(spec, _live(["platform:other.scope"]), _PLUGIN)


def test_verify_against_live_rejects_plugin_swap():
    # plugin すり替え防止: scopes は一致しても、注入の plugin が live 注釈と違えば fail-closed
    # (別プラグインの ACTIVE グラントへすり替えて同名 Secret を更新する経路を塞ぐ)。
    spec = _spec()
    with pytest.raises(ri.LiveSpecMismatch):
        ri._verify_against_live(spec, _live(spec.required_scopes, plugin=_PLUGIN),
                                "evil/other-plugin")


def test_verify_against_live_rejects_missing_plugin_annotation():
    # plugin-id 注釈が無い(plugin 非固定)deploy は fail-closed。
    spec = _spec()
    with pytest.raises(ri.LiveSpecMismatch):
        ri._verify_against_live(spec, _live(spec.required_scopes, plugin=""), _PLUGIN)


def test_assert_matches_live_pure():
    # 純関数: 集合一致のみ許可(順序非依存)。
    ri._assert_matches_live(["a", "b"], frozenset({"b", "a"}))
    with pytest.raises(ri.LiveSpecMismatch):
        ri._assert_matches_live(["a", "b"], frozenset({"a"}))


def test_verify_against_live_rejects_missing_deployment():
    # Deployment 不在(rc!=0)は fail-closed(実在するデモにのみ注入する)。
    spec = _spec()
    with pytest.raises(ri.LiveSpecMismatch):
        ri._verify_against_live(spec, _runner("", rc=1, stderr="NotFound"), _PLUGIN)


def test_render_fails_closed_on_mismatch_before_token():
    # _render は **常に** live-check を通す(無効化不可)。不一致なら token 発行前に fail-closed。
    args = argparse.Namespace(
        image=_IMAGE, answers=json.dumps(_ANSWERS), prefix="jetuse-demo-faq",
        tenant=_TENANT, plugin=_PLUGIN, base_url="https://p.example/v1", secret_only=False,
    )
    with pytest.raises(ri.LiveSpecMismatch):
        ri._render(args, runner=_live(["platform:other.scope"]))
