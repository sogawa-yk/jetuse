"""kubectl 実行の薄いクライアント（L3 デモの OKE 実 apply 経路 / ADR-0017 §5）。

`deploy.py` / `deploy_inject.py` が**決定的に描画**した K8s マニフェスト（Namespace/Deployment/
Service ／ 注入 ConfigMap・Secret）を **実際に OKE へ `kubectl apply`** し、デモ削除時は
`kubectl delete namespace` で namespace ごと撤去する（1 namespace = 1 デモ。trivial delete）。
描画（render）と実行（apply）を分離し、本モジュールは **実行だけ** を担う（マニフェスト生成や秘密の
判断は持たない＝描画側の fail-closed をそのまま流す）。

セキュリティ姿勢（描画側と同方針の fail-closed）:
  - 入力 YAML が空なら apply しない（`KubeError`）。
  - namespace 名は DNS-1123 ラベルに限定（任意文字列で `kubectl delete` 引数を汚染させない）。
  - `--dry-run` を明示できる（`client`=オフライン検証／`server`=クラスタ検証）。
    実 apply（dry_run=None）は OKE への実配備＝課金・人間ゲート。本モジュールは実行可能にするだけ。
    有効化は呼び出し側（`settings.oke_deploy_enabled` ／ `oke_deploy_dry_run`）が決める。
  - 短期トークン Secret は **`--server-side`** で apply する（client-side だと
    `last-applied-configuration` annotation に平文が残るため。tools/render_injection.py と整合）。
  - 実行関数（`runner`）を差し替え可能にし、テストで実 kubectl を呼ばずに引数/stdin を検証できる。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

#: kubectl 実行ファイル名（PATH 解決）。
DEFAULT_KUBECTL_BIN = "kubectl"
#: apply / delete の既定タイムアウト（暴走防止）。delete は namespace finalize を待たない設定。
APPLY_TIMEOUT_SECONDS = 120
DELETE_TIMEOUT_SECONDS = 120

#: `--dry-run` の許容値（None=実 apply）。
_DRY_RUN_MODES = frozenset({"client", "server"})

#: K8s namespace（DNS-1123 ラベル。英小文字数字とハイフン・英数字始終）。長さ<=63 は別途明示検査。
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
#: DNS-1123 ラベルの最大長。正規表現は長さを見ないため明示検査する（F-007）。
MAX_NAMESPACE_LEN = 63

#: 削除してはいけない保護 namespace（クラスタ基盤・既定。誤って消すと人間ゲート違反になる）。
#: JetUse 管理デモ namespace は `jetuse-demo-*`（prefix 由来）であり、ここには該当しない。
PROTECTED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "kube-flannel",
        "cattle-system",
        "ingress-nginx",
    }
)

#: 実行関数の型（テスト差し替え注入点）。引数リストと stdin を受け、CompletedProcess を返す。
Runner = Callable[[list[str], "str | None"], "subprocess.CompletedProcess[str]"]


class KubeError(RuntimeError):
    """kubectl 実行を組み立てられない／失敗した（fail-closed）。"""


@dataclass(frozen=True)
class KubeResult:
    """kubectl 実行結果（成功時のみ返す。失敗は KubeError）。"""

    #: 実行した引数（kubectl を含む全トークン）。
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    #: 適用した dry-run モード（None=実 apply / "client" / "server"）。
    dry_run: str | None


def kubectl_available(kubectl_bin: str = DEFAULT_KUBECTL_BIN) -> bool:
    """kubectl 実行ファイルが PATH にあるか（実 apply 可否の事前判定に使う）。"""
    return shutil.which(kubectl_bin) is not None


def _default_runner(timeout: int) -> Runner:
    """既定の実行関数（subprocess。capture/text・タイムアウト付き）。"""

    def _run(args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - 引数は本モジュールで固定生成（shell=False）
            args,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run


def _validate_namespace(namespace: str) -> str:
    """namespace 名を DNS-1123 ラベル＋長さ＋保護リストで検証する（fail-closed）。

    任意文字列で `kubectl` 引数を汚染させない／保護 namespace（kube-system 等）を消させない。
    """
    ns = (namespace or "").strip()
    if not _NAMESPACE_RE.match(ns) or len(ns) > MAX_NAMESPACE_LEN:
        raise KubeError(
            f"namespace 名が不正です（DNS-1123 ラベル・1..{MAX_NAMESPACE_LEN} 文字）: {namespace!r}"
        )
    if ns in PROTECTED_NAMESPACES:
        raise KubeError(f"保護 namespace は操作できません: {ns}")
    return ns


def _validate_dry_run(dry_run: str | None) -> str | None:
    if dry_run is None:
        return None
    if dry_run not in _DRY_RUN_MODES:
        raise KubeError(
            f"dry_run は {sorted(_DRY_RUN_MODES)} か None で指定してください（実値={dry_run!r}）"
        )
    return dry_run


def _base_args(kubectl_bin: str, kubeconfig: str) -> list[str]:
    args = [kubectl_bin]
    # kubeconfig は環境依存実値。空なら kubectl 既定（KUBECONFIG / ~/.kube/config）に委ねる。
    if kubeconfig and kubeconfig.strip():
        args += ["--kubeconfig", kubeconfig.strip()]
    return args


def _run(
    args: list[str],
    *,
    stdin: str | None,
    dry_run: str | None,
    timeout: int,
    runner: Runner | None,
) -> KubeResult:
    """実行して結果を検証する共通経路（非ゼロ終了は fail-closed で KubeError）。"""
    run = runner or _default_runner(timeout)
    try:
        proc = run(args, stdin)
    except FileNotFoundError as exc:  # kubectl 不在
        raise KubeError(f"kubectl を実行できません（未インストール？）: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubeError(f"kubectl がタイムアウトしました（{timeout}s）: {' '.join(args)}") from exc
    except OSError as exc:
        # 実行不可（PermissionError 等、FileNotFoundError 以外の OSError）も fail-closed で
        # KubeError に統一（ルート側の 409/503 変換を素通りして 500 になるのを防ぐ＝F-007）。
        raise KubeError(f"kubectl の起動に失敗しました: {exc}") from exc
    if proc.returncode != 0:
        raise KubeError(
            f"kubectl 失敗（rc={proc.returncode}）: {' '.join(args)}\n"
            f"stderr: {(proc.stderr or '').strip()}"
        )
    return KubeResult(
        args=tuple(args),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        dry_run=dry_run,
    )


def apply_manifests(
    yaml_text: str,
    *,
    server_side: bool = False,
    dry_run: str | None = None,
    kubeconfig: str = "",
    kubectl_bin: str = DEFAULT_KUBECTL_BIN,
    runner: Runner | None = None,
) -> KubeResult:
    """マニフェスト YAML を `kubectl apply -f -`（stdin）で適用する。

    - `server_side=True` で `--server-side`（注入 Secret は平文 annotation 残留を避けるため必須）。
    - `dry_run="client"` はオフライン検証（クラスタ接続不要）、`"server"` はクラスタ検証。
    - `-o name` で適用済みリソース（`kind/name`）を stdout に得る（deploy_runtime が記録に使う）。
    空 YAML は fail-closed（何も適用しない）。
    """
    dry = _validate_dry_run(dry_run)
    if not yaml_text or not yaml_text.strip():
        raise KubeError("適用するマニフェストが空です（fail-closed）")
    # server-side apply は client dry-run と併用不可（kubectl が拒否する）。client 検証は
    # 永続しない＝server-side の意味が無いので、組み合わせは構成ミスとして fail-closed（F-009）。
    if server_side and dry == "client":
        raise KubeError(
            "server-side apply は --dry-run=client と併用できません"
            "（client 検証は server-side 不要。server 検証なら dry_run='server'）"
        )
    args = _base_args(kubectl_bin, kubeconfig)
    args += ["apply", "-f", "-", "-o", "name"]
    if server_side:
        # 同一 field-manager の再 apply（refresh）で衝突した場合も決定的に上書きする。
        args += ["--server-side", "--force-conflicts"]
    if dry:
        args.append(f"--dry-run={dry}")
    return _run(
        args, stdin=yaml_text, dry_run=dry, timeout=APPLY_TIMEOUT_SECONDS, runner=runner
    )


def delete_namespace(
    namespace: str,
    *,
    ignore_not_found: bool = True,
    wait: bool = False,
    dry_run: str | None = None,
    kubeconfig: str = "",
    kubectl_bin: str = DEFAULT_KUBECTL_BIN,
    runner: Runner | None = None,
) -> KubeResult:
    """デモ namespace を撤去する（`kubectl delete namespace <ns>`。1 namespace = 1 デモ）。

    namespace ごと消すことで Deployment/Service/ConfigMap/Secret/SA/Quota がまとめて消える
    （残骸なし）。`ignore_not_found=True` は冪等（既に無くても成功）。`wait=False` は finalize を
    待たない（呼び出し側の応答性確保。namespace 削除は非同期で進む）。
    """
    ns = _validate_namespace(namespace)
    dry = _validate_dry_run(dry_run)
    args = _base_args(kubectl_bin, kubeconfig)
    args += ["delete", "namespace", ns]
    args.append(f"--ignore-not-found={'true' if ignore_not_found else 'false'}")
    args.append(f"--wait={'true' if wait else 'false'}")
    if dry:
        args.append(f"--dry-run={dry}")
    return _run(
        args, stdin=None, dry_run=dry, timeout=DELETE_TIMEOUT_SECONDS, runner=runner
    )


def rollout_restart(
    deployment: str,
    namespace: str,
    *,
    dry_run: str | None = None,
    kubeconfig: str = "",
    kubectl_bin: str = DEFAULT_KUBECTL_BIN,
    runner: Runner | None = None,
) -> KubeResult:
    """Deployment を rolling restart する（Secret 更新後に Pod へ反映＝refresh の最終段）。"""
    ns = _validate_namespace(namespace)
    name = (deployment or "").strip()
    if not _NAMESPACE_RE.match(name) or len(name) > MAX_NAMESPACE_LEN:
        raise KubeError(f"deployment 名が不正です（DNS-1123 ラベル）: {deployment!r}")
    dry = _validate_dry_run(dry_run)
    args = _base_args(kubectl_bin, kubeconfig) + [
        "rollout", "restart", f"deployment/{name}", "-n", ns
    ]
    if dry:
        args.append(f"--dry-run={dry}")
    return _run(
        args, stdin=None, dry_run=dry, timeout=APPLY_TIMEOUT_SECONDS, runner=runner
    )


__all__ = [
    "DEFAULT_KUBECTL_BIN",
    "MAX_NAMESPACE_LEN",
    "PROTECTED_NAMESPACES",
    "KubeError",
    "KubeResult",
    "Runner",
    "apply_manifests",
    "delete_namespace",
    "kubectl_available",
    "rollout_restart",
]
