"""BE-01: kubectl 実行クライアント（kube.py）の単体テスト。

実 kubectl を呼ばずに、引数組み立て・dry-run・server-side・kubeconfig・fail-closed 境界
（空 YAML / 非ゼロ終了 / 不正 namespace / 不正 dry-run）を runner スタブで検証する。
実クラスタへの apply/delete は完了ゲートの E2E（runs/<run-id>/e2e/）で扱う。
"""

import subprocess

import pytest

from jetuse_core import kube
from jetuse_core.kube import KubeError, apply_manifests, delete_namespace


def _runner(returncode=0, stdout="", stderr=""):
    """引数と stdin を記録する runner スタブ（実行は行わない）。"""
    calls: list[dict] = []

    def run(args, stdin):
        calls.append({"args": list(args), "stdin": stdin})
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


_YAML = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: demo\n"


# --- apply_manifests -------------------------------------------------------


def test_apply_builds_expected_args_and_pipes_stdin():
    run = _runner(stdout="namespace/demo\ndeployment.apps/demo\n")
    res = apply_manifests(_YAML, runner=run)

    assert run.calls[0]["stdin"] == _YAML
    args = run.calls[0]["args"]
    assert args[0] == kube.DEFAULT_KUBECTL_BIN
    assert args[1:4] == ["apply", "-f", "-"]
    assert "-o" in args and "name" in args
    # 既定は実 apply（dry-run / server-side なし）。
    assert not any(a.startswith("--dry-run") for a in args)
    assert "--server-side" not in args
    assert res.returncode == 0
    assert res.dry_run is None
    assert "namespace/demo" in res.stdout


def test_apply_client_dry_run_flag():
    run = _runner()
    res = apply_manifests(_YAML, dry_run="client", runner=run)
    assert "--dry-run=client" in run.calls[0]["args"]
    assert res.dry_run == "client"


def test_apply_server_side_adds_force_conflicts():
    run = _runner()
    apply_manifests(_YAML, server_side=True, runner=run)
    args = run.calls[0]["args"]
    assert "--server-side" in args
    assert "--force-conflicts" in args


def test_apply_server_side_with_client_dry_run_rejected():
    # server-side apply は --dry-run=client と併用不可（F-009）。構成ミスとして fail-closed。
    run = _runner()
    with pytest.raises(KubeError):
        apply_manifests(_YAML, server_side=True, dry_run="client", runner=run)
    assert run.calls == []


def test_apply_server_side_with_server_dry_run_ok():
    run = _runner()
    apply_manifests(_YAML, server_side=True, dry_run="server", runner=run)
    args = run.calls[0]["args"]
    assert "--server-side" in args
    assert "--dry-run=server" in args


def test_apply_kubeconfig_is_passed():
    run = _runner()
    apply_manifests(_YAML, kubeconfig="/tmp/kcfg", runner=run)
    args = run.calls[0]["args"]
    assert "--kubeconfig" in args
    assert args[args.index("--kubeconfig") + 1] == "/tmp/kcfg"


def test_apply_empty_yaml_is_failclosed():
    run = _runner()
    with pytest.raises(KubeError):
        apply_manifests("   \n  ", runner=run)
    assert run.calls == []  # 何も実行しない


def test_apply_nonzero_returncode_raises_with_stderr():
    run = _runner(returncode=1, stderr="boom: invalid manifest")
    with pytest.raises(KubeError) as ei:
        apply_manifests(_YAML, runner=run)
    assert "boom: invalid manifest" in str(ei.value)


def test_apply_invalid_dry_run_rejected():
    run = _runner()
    with pytest.raises(KubeError):
        apply_manifests(_YAML, dry_run="maybe", runner=run)


def test_apply_kubectl_missing_is_failclosed():
    def run(args, stdin):
        raise FileNotFoundError("kubectl")

    with pytest.raises(KubeError):
        apply_manifests(_YAML, runner=run)


def test_apply_timeout_is_failclosed():
    def run(args, stdin):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    with pytest.raises(KubeError):
        apply_manifests(_YAML, runner=run)


# --- delete_namespace ------------------------------------------------------


def test_delete_namespace_builds_expected_args():
    run = _runner(stdout='namespace "demo" deleted\n')
    res = delete_namespace("jetuse-demo-faq", runner=run)
    args = run.calls[0]["args"]
    assert args[1:4] == ["delete", "namespace", "jetuse-demo-faq"]
    assert "--ignore-not-found=true" in args
    assert "--wait=false" in args
    assert run.calls[0]["stdin"] is None
    assert res.returncode == 0


def test_delete_namespace_wait_and_ignore_flags():
    run = _runner()
    delete_namespace("demo", ignore_not_found=False, wait=True, runner=run)
    args = run.calls[0]["args"]
    assert "--ignore-not-found=false" in args
    assert "--wait=true" in args


def test_delete_namespace_dry_run():
    run = _runner()
    delete_namespace("demo", dry_run="client", runner=run)
    assert "--dry-run=client" in run.calls[0]["args"]


@pytest.mark.parametrize("bad", ["", "  ", "Demo", "demo_1", "-demo", "demo/evil", "a b"])
def test_delete_namespace_invalid_name_failclosed(bad):
    run = _runner()
    with pytest.raises(KubeError):
        delete_namespace(bad, runner=run)
    assert run.calls == []  # 不正名では実行しない（引数汚染防止）


def test_delete_namespace_length_boundary():
    # 63 文字は OK、64 文字は fail-closed（F-007: 正規表現は長さを見ないため明示検査）。
    ok = "a" * 63
    too_long = "a" * 64
    run = _runner()
    delete_namespace(ok, runner=run)
    assert run.calls[0]["args"][3] == ok
    run2 = _runner()
    with pytest.raises(KubeError):
        delete_namespace(too_long, runner=run2)
    assert run2.calls == []


@pytest.mark.parametrize("protected", ["default", "kube-system", "kube-public"])
def test_delete_protected_namespace_rejected(protected):
    run = _runner()
    with pytest.raises(KubeError):
        delete_namespace(protected, runner=run)
    assert run.calls == []  # 保護 namespace は操作しない


# --- rollout_restart -------------------------------------------------------


def test_rollout_restart_builds_expected_args():
    from jetuse_core.kube import rollout_restart

    run = _runner(stdout="deployment.apps/demo restarted\n")
    res = rollout_restart("jetuse-demo-faq", "jetuse-demo-faq", runner=run)
    args = run.calls[0]["args"]
    assert args[1:3] == ["rollout", "restart"]
    assert "deployment/jetuse-demo-faq" in args
    assert "-n" in args and "jetuse-demo-faq" in args
    assert res.returncode == 0


def test_rollout_restart_dry_run_and_bad_name():
    from jetuse_core.kube import rollout_restart

    run = _runner()
    rollout_restart("demo", "demo", dry_run="server", runner=run)
    assert "--dry-run=server" in run.calls[0]["args"]
    run2 = _runner()
    with pytest.raises(KubeError):
        rollout_restart("Bad Name", "demo", runner=run2)
    assert run2.calls == []
