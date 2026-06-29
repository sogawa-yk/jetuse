"""launch → OKE 配備配線のオーケストレーション（BE-01 / ADR-0017）。

`deploy.py`（宣言的配備仕様の決定的 render）と `deploy_inject.py`（base_url ConfigMap＋短期トークン
Secret の描画）を **kubectl 経路（`kube.py`）につなぐ** 一本のオーケストレーション。

  composition → build_deploy_spec → render_manifests → kubectl apply（base）
              → （要すれば）build_runtime_injection → render_injection_manifests
                → kubectl apply（注入 ConfigMap/Secret）

**スコープ（重要）**: 本タスクは「配線＋マニフェスト/レンダリング検証」までを実装する。
**実 OKE への apply/delete/restart（実配備・課金・既存リソース変更）は人間ゲート**であり、
本モジュールは **dry-run 検証のみ**を行う（`--dry-run=client`=オフライン検証 /
`--dry-run=server`=実クラスタ検証で、いずれも **リソースを作成・変更・削除しない**）。実 apply
（`dry_run=None`）は `_resolve_dry_run` が **常に拒否**する（人間ゲートの公開引数バイパスを構造的に
塞ぐ＝F-001）。実 apply＋そのライフサイクル（孤児化防止・所有権/UID 照合・cascade 整合・トークン
定期更新＝reconciler/outbox）は **別タスク**で実装し、本 run の `e2e/SKIPPED.md` に明記する。
これにより実クラスタ変更に起因する孤児化/誤削除の経路は **構造的に発生しない**（dry-run は無痕）。

役割分担（境界を侵さない）:
  - 描画の fail-closed（秘密 allowlist・スコープ閉包・テナント分離・命名健全化）は deploy.py /
    deploy_inject.py が担保する。本モジュールは描画結果を **dry-run で検証する** だけ。
  - 実行層ゲート（`oke_deploy_enabled`）必須。既定 OFF では一切実行しない（後方互換）。
  - 命名の一意化: 配備 prefix に launch 一意キー（session/launch id）のハッシュと tenant ハッシュを
    含め、同一 sample_app の複数 launch が namespace 衝突しない（実 apply 時の前提も満たす）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import kube
from .deploy import (
    DEFAULT_SERVICE_PORT,
    MAX_PREFIX_LEN,
    build_deploy_spec,
    dump_manifests,
)
from .deploy_inject import build_runtime_injection
from .settings import Settings, get_settings

if TYPE_CHECKING:  # 循環 import 回避（synth は本モジュールに依存しない）
    from collections.abc import Iterable

    from .deploy import ContainerDeploySpec
    from .deploy_inject import RuntimeInjection
    from .synth import DemoComposition

#: dry_run 省略時に「settings から導出」することを示すセンチネル。
_DERIVE = "__derive_from_settings__"

#: dry-run の許容モード（実 apply=None は本タスクで非対応＝人間ゲート）。
_DRY_RUN_MODES = ("client", "server")

#: launch 一意キーの短縮ハッシュ長（prefix へ含める。命名衝突回避）。48bit。
#: build_deploy_spec が末尾に付ける tenant suffix（"-"＋12hex=13）で再切詰めされても、
#: 下の compact prefix なら MAX_PREFIX_LEN 内にこの 12hex がフルで残る（F-006）。
_INSTANCE_HASH_LEN = 12


class DeployRuntimeError(RuntimeError):
    """launch→配備のオーケストレーションを組み立てられない（fail-closed）。"""


@dataclass(frozen=True)
class DeployOutcome:
    """配備（dry-run 検証）結果のサマリ。demo_launch の記録・レスポンスに使う非秘密メタのみ。

    短期トークンや Vault OCID は **一切含めない**（描画側の secret 分離を保つ）。`token_expires_at`
    は失効時刻（非秘密メタ）だけを公開する。`dry_run` は常に client/server（実 apply はしない）。
    """

    #: デモ namespace（= spec.prefix。1 namespace = 1 デモ）。
    namespace: str
    #: クラスタ内 Service / Deployment 名（= spec.prefix）。
    service_name: str
    #: クラスタ内到達 URL（ClusterIP Service。外部公開は本体 Ingress/LB 側で別途）。
    cluster_url: str
    #: 検証したリソース（`kind/name` の列。kubectl -o name の出力由来・ソート済み）。
    resources: tuple[str, ...]
    #: Platform API 注入（base_url＋短期トークン）を組み立てたか。
    injected: bool
    #: 適用した dry-run モード（"client" / "server"。実 apply はしない）。
    dry_run: str
    #: 注入したトークンの失効時刻（ISO8601, 注入時のみ。非秘密メタ）。
    token_expires_at: str | None = None

    def deploy_status(self) -> str:
        """demo_launch.deploy_status に書く値。

        本タスクは dry-run 検証のみのため常に "validated"（= マニフェスト/レンダリング検証済み・
        実ワークロード未作成）。実 apply 済みの "deployed" は本タスクでは付かない（人間ゲート）。
        """
        return "validated"


@dataclass(frozen=True)
class RefreshOutcome:
    """注入トークン更新（refresh）の dry-run 検証結果のサマリ（非秘密メタのみ）。"""

    namespace: str
    deployment: str
    resources: tuple[str, ...]
    restarted: bool
    dry_run: str
    token_expires_at: str


def _require_enabled(settings: Settings) -> None:
    """実行層ゲート。OKE 配備が無効なら一切実行しない（既定 OFF=後方互換）。"""
    if not settings.oke_deploy_enabled:
        raise DeployRuntimeError(
            "OKE 配備は無効です（settings.oke_deploy_enabled=False）。"
            "有効化は人間が .env で行う（実配備・課金は人間ゲート）"
        )


def _resolve_dry_run(settings: Settings, dry_run: str | None) -> str:
    """dry-run モードを決める（実 apply=None は **常に拒否**＝人間ゲートの構造的封鎖＝F-001）。

    - `settings.oke_deploy_dry_run=False`（= 人間が実操作を意図）は、本タスクでは実 apply 未対応の
      ため fail-closed（実 apply＋ライフサイクルは別タスク。e2e/SKIPPED.md 参照）。
    - 省略（_DERIVE）は安全側の "client"。明示は "client"/"server" のみ。`None`（実 apply）は拒否。
    """
    if not settings.oke_deploy_dry_run:
        raise DeployRuntimeError(
            "実 OKE apply/delete/restart は本タスクでは未対応（人間ゲート）。"
            "oke_deploy_dry_run=True で dry-run 検証のみ可（実配備は別タスク・SKIPPED.md 参照）"
        )
    mode = "client" if dry_run == _DERIVE else dry_run
    if mode not in _DRY_RUN_MODES:
        raise DeployRuntimeError(
            f"dry_run は {list(_DRY_RUN_MODES)} のみ（実 apply=None は人間ゲートで不可）: {mode!r}"
        )
    return mode


def _parse_resource_names(stdout: str) -> tuple[str, ...]:
    """`kubectl apply -o name` の出力から `kind/name` 行を抽出（決定的・ソート済み）。"""
    names = {
        line.strip()
        for line in (stdout or "").splitlines()
        if line.strip() and "/" in line
    }
    return tuple(sorted(names))


def _instance_hash(instance_key: str) -> str:
    """launch 一意キーの短縮ハッシュ（命名衝突回避。生 ID を prefix へそのまま出さない）。"""
    return hashlib.sha256(instance_key.encode("utf-8")).hexdigest()[:_INSTANCE_HASH_LEN]


def _demo_prefix(composition: DemoComposition, instance_key: str | None) -> str | None:
    """launch 一意キーを含む配備 prefix の素を作る（None なら build_deploy_spec の既定に委ねる）。

    `jd-<sample(<=6)>-<instance hash(12hex)>`（最大 `3+6+1+12=22` 文字）。tenant ハッシュ
    （"-"＋12hex＝13）は build_deploy_spec が末尾へ付け、base を `MAX_PREFIX_LEN-13` で切詰める。
    本 prefix は 22<=（40-13=27）なので **tenant 付きでも instance hash 12hex がフルで残り**、
    複数 launch が namespace 衝突しない（F-006）。compact 形はこの切詰め耐性のため。
    """
    if not instance_key or not instance_key.strip():
        return None
    sample = re.sub(r"[^a-z0-9]+", "", (composition.sample_app or "app").lower())[:6] or "app"
    base = f"jd-{sample}-{_instance_hash(instance_key.strip())}"
    return base[:MAX_PREFIX_LEN]


def _build_spec(
    composition: DemoComposition,
    *,
    settings: Settings,
    tenant: str | None,
    plugin_id: str,
    image_url: str | None,
    instance_key: str | None,
    sdk: str | None,
    required_secrets: Iterable[str] | None,
) -> ContainerDeploySpec:
    return build_deploy_spec(
        composition,
        settings=settings,
        image_url=image_url,
        prefix=_demo_prefix(composition, instance_key),
        tenant=tenant,
        plugin_id=plugin_id or None,
        sdk=sdk,
        required_secrets=required_secrets,
    )


def _build_injection_if_needed(
    spec: ContainerDeploySpec,
    *,
    settings: Settings,
    tenant: str | None,
    plugin_id: str,
    base_url: str | None,
) -> RuntimeInjection | None:
    """注入が要るデモなら base apply の前に注入物を組み立てる（グラント失敗を apply 前に弾く）。"""
    if not spec.needs_platform_injection:
        return None
    # 注入を要する（スコープあり）デモは tenant＋発行プラグインが必須。欠けると別テナント/別
    # プラグインのグラントへのすり替え余地が生まれるため早期に fail-closed（inject 側でも拒否）。
    if not (tenant and tenant.strip()):
        raise DeployRuntimeError("Platform 注入を要するデモは tenant（Project OCID）が必須です")
    if not (plugin_id and plugin_id.strip()):
        raise DeployRuntimeError("Platform 注入を要するデモは plugin_id（発行主体）が必須です")
    return build_runtime_injection(
        spec, tenant=tenant, plugin_id=plugin_id, settings=settings, base_url=base_url
    )


def deploy_demo(
    composition: DemoComposition,
    *,
    settings: Settings | None = None,
    tenant: str | None = None,
    plugin_id: str = "",
    instance_key: str | None = None,
    image_url: str | None = None,
    base_url: str | None = None,
    sdk: str | None = None,
    required_secrets: Iterable[str] | None = None,
    dry_run: str | None = _DERIVE,
    runner: kube.Runner | None = None,
) -> DeployOutcome:
    """合成済みデモ構成の OKE 配備を **dry-run で検証** する（描画→kubectl 検証の一本化）。

    実行層ゲート（`oke_deploy_enabled`）必須。実 apply（dry_run=None）は人間ゲートで常に拒否し、
    dry-run（client/server）でマニフェストを検証する。注入物は base 検証の前に組み立て、
    グラント無し/失効/スコープ閉包違反なら一切 apply（検証含む）せず止める。
    """
    settings = settings or get_settings()
    _require_enabled(settings)
    mode = _resolve_dry_run(settings, dry_run)

    spec = _build_spec(
        composition,
        settings=settings,
        tenant=tenant,
        plugin_id=plugin_id,
        image_url=image_url,
        instance_key=instance_key,
        sdk=sdk,
        required_secrets=required_secrets,
    )

    # 注入物は base の前に組み立てる（grant 無し/失効/スコープ違反なら検証も進めない）。
    injection = _build_injection_if_needed(
        spec, settings=settings, tenant=tenant, plugin_id=plugin_id, base_url=base_url
    )

    kubeconfig = settings.kube_config_path
    base_result = kube.apply_manifests(
        spec.render_manifests_yaml(),
        server_side=False,
        dry_run=mode,
        kubeconfig=kubeconfig,
        runner=runner,
    )
    resources = list(_parse_resource_names(base_result.stdout))

    injected = False
    token_expires_at: str | None = None
    if injection is not None:
        inj_yaml = dump_manifests(injection.render_injection_manifests(spec))
        # 注入の実 apply は本来 server-side（平文 annotation 残留回避）。dry-run 検証では
        # server-side は client と併用不可（F-009）。server 検証のときだけ server-side にする。
        inj_result = kube.apply_manifests(
            inj_yaml,
            server_side=(mode == "server"),
            dry_run=mode,
            kubeconfig=kubeconfig,
            runner=runner,
        )
        resources.extend(_parse_resource_names(inj_result.stdout))
        injected = True
        token_expires_at = injection.expires_at.isoformat()

    # ClusterIP Service の in-cluster DNS（<service>.<namespace>.svc.cluster.local）。
    # service 名・namespace はともに spec.prefix。外部公開は本体 Ingress/LB 側で別途。
    cluster_url = (
        f"http://{spec.prefix}.{spec.namespace}.svc.cluster.local:{DEFAULT_SERVICE_PORT}"
    )
    return DeployOutcome(
        namespace=spec.namespace,
        service_name=spec.prefix,
        cluster_url=cluster_url,
        resources=tuple(sorted(set(resources))),
        injected=injected,
        dry_run=mode,
        token_expires_at=token_expires_at,
    )


def refresh_injection(
    composition: DemoComposition,
    *,
    settings: Settings | None = None,
    tenant: str,
    plugin_id: str,
    instance_key: str | None = None,
    image_url: str | None = None,
    base_url: str | None = None,
    sdk: str | None = None,
    required_secrets: Iterable[str] | None = None,
    restart: bool = True,
    dry_run: str | None = _DERIVE,
    runner: kube.Runner | None = None,
) -> RefreshOutcome:
    """連携ありデモのトークン更新（再発行→Secret 再 apply→rolling restart）を dry-run で検証する。

    実 apply は人間ゲート（本タスク未対応）。本関数は更新フローのレンダリング/コマンドを dry-run で
    検証するに留める。実運用の定期 refresh（保存 namespace/principal/失効時刻に基づく
    期限前更新ジョブ）は別タスクで配線する（e2e/SKIPPED.md 参照）。
    """
    settings = settings or get_settings()
    _require_enabled(settings)
    mode = _resolve_dry_run(settings, dry_run)

    spec = _build_spec(
        composition,
        settings=settings,
        tenant=tenant,
        plugin_id=plugin_id,
        image_url=image_url,
        instance_key=instance_key,
        sdk=sdk,
        required_secrets=required_secrets,
    )
    if not spec.needs_platform_injection:
        raise DeployRuntimeError(
            "このデモは Platform 注入を必要としません（required_scopes 空）。refresh 不要"
        )
    injection = build_runtime_injection(
        spec, tenant=tenant, plugin_id=plugin_id, settings=settings, base_url=base_url
    )

    kubeconfig = settings.kube_config_path
    inj_yaml = dump_manifests(injection.render_injection_manifests(spec))
    inj_result = kube.apply_manifests(
        inj_yaml,
        server_side=(mode == "server"),
        dry_run=mode,
        kubeconfig=kubeconfig,
        runner=runner,
    )
    resources = list(_parse_resource_names(inj_result.stdout))

    restarted = False
    if restart:
        kube.rollout_restart(
            spec.prefix,
            spec.namespace,
            dry_run=mode,
            kubeconfig=kubeconfig,
            runner=runner,
        )
        restarted = True

    return RefreshOutcome(
        namespace=spec.namespace,
        deployment=spec.prefix,
        resources=tuple(sorted(set(resources))),
        restarted=restarted,
        dry_run=mode,
        token_expires_at=injection.expires_at.isoformat(),
    )


def teardown_demo(
    namespace: str,
    *,
    settings: Settings | None = None,
    dry_run: str | None = _DERIVE,
    runner: kube.Runner | None = None,
) -> kube.KubeResult:
    """デモ namespace 撤去（`kubectl delete namespace`）を **dry-run で検証** する。

    実行層ゲート（`oke_deploy_enabled`）必須。実 delete は人間ゲート（本タスク未対応）で
    `_resolve_dry_run` が拒否する。保護 ns 拒否・DNS-1123/長さ検査は kube.delete_namespace 側。
    実 delete 時の所有権（managed-by/UID）照合・finalize 待ち・冪等な再回収は
    実 apply ライフサイクルの別タスクで実装する（e2e/SKIPPED.md 参照）。
    """
    settings = settings or get_settings()
    _require_enabled(settings)
    mode = _resolve_dry_run(settings, dry_run)
    return kube.delete_namespace(
        namespace,
        ignore_not_found=True,
        wait=False,
        dry_run=mode,
        kubeconfig=settings.kube_config_path,
        runner=runner,
    )


__all__ = [
    "DeployOutcome",
    "DeployRuntimeError",
    "RefreshOutcome",
    "deploy_demo",
    "refresh_injection",
    "teardown_demo",
]
