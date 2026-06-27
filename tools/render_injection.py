#!/usr/bin/env python3
"""L3 デモの Platform API 注入マニフェスト(ConfigMap=base_url / Secret=token)を標準出力へ描画する。

ADR-0017 §5 の「アウトオブバンド注入」を `kubectl apply --server-side -f -` で流すための薄い CLI。
短期トークン Secret は **コミット/Terraform/state を通さない**(出力をパイプで apply する)。
**必ず `--server-side`** で apply する: client-side apply だと短期トークンが
`kubectl.kubernetes.io/last-applied-configuration` annotation に平文で残るため(README/E2E と整合)。

使い方(例。`--prefix` と `--answers` は必須=deploy 時と同一を渡す):
  python tools/render_injection.py \
      --prefix jetuse-demo-faq \
      --answers "$ANSWERS" \
      --image kix.ocir.io/<ns>/jetuse-demo:latest \
      --tenant ocid1.tenancy... --plugin acme/demo --base-url https://platform.example/... \
      | kubectl apply --server-side -f -

  # 更新(refresh)時は Secret のみ(同じ --prefix/--answers/--tenant):
  python tools/render_injection.py --secret-only --prefix jetuse-demo-faq \
      --answers '{...}' --image ... --tenant ... --plugin ... --base-url ... \
      | kubectl apply --server-side -f -

注意: 承認グラント(`platform_grants`)とブローカー署名鍵(`settings.platform_broker_secret`)が
必要。グラント無し/失効/承認超過は fail-closed で発行されない(ADR-0014 / ADR-0016)。

**deploy-spec 閉包の運用迂回防止**: トークン発行前に、再構築 spec の required_scopes が
**実デプロイ済み Deployment の `jetuse.dev/required-scopes` 注釈(ground truth)** と一致することを
kubectl で検証する(常時・無効化スイッチ無し)。deploy 時と違う answers を渡して宣言外スコープの
トークンを同名 Secret に上書きする経路を塞ぐ(不一致/Deployment 不在は fail-closed=トークン未発行)。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Callable, Iterable

# jetuse_core は packages/api 配下。editable install 無しでもリポジトリルートから
# `python tools/render_injection.py ...` で実行できるよう import path を通す(review 対応)。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "packages" / "api"))

from jetuse_core.deploy import ContainerDeploySpec, build_deploy_spec, dump_manifests  # noqa: E402
from jetuse_core.deploy_inject import build_runtime_injection  # noqa: E402
from jetuse_core.recommend import recommend  # noqa: E402
from jetuse_core.synth import synthesize  # noqa: E402

#: deploy.py が Deployment に付ける「配備仕様の宣言スコープ」注釈(deploy-spec 閉包の ground truth)。
REQUIRED_SCOPES_ANNOTATION = "jetuse.dev/required-scopes"

#: kubectl 実行関数の型(テストで差し替え可能にするための注入点)。
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class LiveSpecMismatch(RuntimeError):
    """注入対象の spec が実デプロイ(live Deployment)と一致しないときの fail-closed 例外。"""


def _parse_scopes(text: str | None) -> frozenset[str]:
    """注釈/CLI のカンマ区切りスコープ文字列を集合へ(空要素は除去)。"""
    return frozenset(s.strip() for s in (text or "").split(",") if s.strip())


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _fetch_live_annotations(
    spec: ContainerDeploySpec, runner: Runner
) -> tuple[frozenset[str], str]:
    """実デプロイ済み Deployment の deploy-spec 閉包注釈(required-scopes / plugin-id)を ground truth
    として読む。Deployment が存在しない/取得失敗は fail-closed(注入は **実在するデモ** にのみ行う)。
    """
    jsonpath = ("jsonpath={.metadata.annotations.jetuse\\.dev/required-scopes}"
                "|{.metadata.annotations.jetuse\\.dev/plugin-id}")
    r = runner(["kubectl", "-n", spec.namespace, "get",
                f"deploy/{spec.prefix}", "-o", jsonpath])
    if r.returncode != 0:
        raise LiveSpecMismatch(
            f"live Deployment {spec.namespace}/{spec.prefix} を取得できません"
            f"(先に deploy 手順でマニフェストを apply すること): {r.stderr.strip()}"
        )
    parts = r.stdout.split("|", 1)
    scopes = _parse_scopes(parts[0])
    plugin = parts[1].strip() if len(parts) > 1 else ""
    return scopes, plugin


def _assert_matches_live(rebuilt: Iterable[str], live: frozenset[str]) -> None:
    """再構築 spec の required_scopes が live Deployment の注釈と一致することを fail-closed 検証。

    deploy 時と異なる answers を渡して **宣言外スコープのトークンを同名 Secret へ上書き** する経路を
    塞ぐ(deploy-spec 閉包の運用迂回防止。承認グラント閉包との二重閉包を運用経路でも維持)。
    """
    want = frozenset(rebuilt)
    if want != live:
        raise LiveSpecMismatch(
            "再構築した required_scopes が実デプロイ(live Deployment 注釈)と不一致。"
            "deploy 時と同じ answers/prefix/tenant を渡しているか確認すること: "
            f"live={sorted(live)} 再構築={sorted(want)}"
        )


def _assert_plugin_matches_live(requested: str, live_plugin: str) -> None:
    """注入の plugin_id が live Deployment の `jetuse.dev/plugin-id`(ground truth)と一致するか検証。

    一致を要求しないと、対象プラグインのグラントを revoke しても、同一 tenant・同一 scope を持つ
    **別プラグインの ACTIVE グラント**を `--plugin` に指定して同名 Secret を更新でき、承認グラント
    閉包を運用経路で迂回できる(plugin すり替え防止)。注釈が無い deploy は fail-closed。
    """
    if not live_plugin:
        raise LiveSpecMismatch(
            "live Deployment に jetuse.dev/plugin-id 注釈が無い。deploy 時に --plugin を固定して"
            "再デプロイすること(plugin すり替えを防ぐため注入は plugin 固定デモにのみ行う)。"
        )
    if requested.strip() != live_plugin:
        raise LiveSpecMismatch(
            "注入の plugin_id が実デプロイ(live 注釈)と不一致。deploy 時と同じ --plugin を"
            f"渡すこと: live={live_plugin} 指定={requested.strip()}"
        )


def _verify_against_live(spec: ContainerDeploySpec, runner: Runner, plugin_id: str) -> None:
    """トークン発行前に、注入対象 spec(scopes＋発行プラグイン)が実デプロイと一致するか検証する。"""
    live_scopes, live_plugin = _fetch_live_annotations(spec, runner)
    _assert_matches_live(spec.required_scopes, live_scopes)
    _assert_plugin_matches_live(plugin_id, live_plugin)


def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True, help="OCIR イメージ URL(kix.ocir.io/...)")
    # --answers は必須: 既定の代表構成へ黙ってフォールバックすると、実際に配備したデモと異なる
    # required_scopes の spec を再構築し、宣言外スコープのトークンを同名 Secret へ注入しうる
    # (deploy-spec 閉包が崩れる)。deploy 時と **同じ answers** を明示する。
    p.add_argument("--answers", required=True, help="ヒアリング回答 JSON(deploy 時と同一)")
    # --prefix は必須: Secret/ConfigMap 名(`<prefix>-platform-token` 等)を Deployment の envFrom と
    # 一致させるため、deploy 時と **同じ base prefix** を指定する(refresh で別名を作らない)。
    # 最終 namespace は deploy と同様 `build_deploy_spec(prefix=..., tenant=...)` が tenant ハッシュ
    # を付けて決定するため、deploy 側でも **同じ tenant を渡すこと**(README §2)。
    p.add_argument("--prefix", required=True, help="namespace/Deployment 名の基点(deploy 時と同一)")
    p.add_argument("--tenant", required=True, help="テナント(Project OCID)")
    p.add_argument("--plugin", required=True, help="発行主体プラグイン ID")
    p.add_argument("--base-url", default=None, help="Platform API ベース URL(https)")
    p.add_argument("--secret-only", action="store_true", help="Secret のみ描画(refresh 用)")
    return p.parse_args()


def _render(args: argparse.Namespace, runner: Runner = _default_runner) -> str:
    answers = json.loads(args.answers)
    composition = synthesize(recommend(answers))
    # tenant を渡して deploy 時と **同一の** namespace/Secret 名(tenant ハッシュ込み)を導出する。
    # これで注入 Secret が必ず deploy したデモと同じ namespace に着地する(テナント分離を保つ)。
    spec = build_deploy_spec(
        composition, image_url=args.image, prefix=args.prefix,
        tenant=args.tenant, plugin_id=args.plugin,
    )
    # トークン発行前に **実デプロイ(ground truth)** と required_scopes ＋ 発行プラグインが一致するか
    # 検証する(常時・無効化スイッチ無し)。deploy 時と違う answers で宣言外スコープのトークンを同名
    # Secret へ上書きする経路、および別プラグインのグラントへすり替える経路を fail-closed で塞ぐ。
    # kubectl で live Deployment を読めなければ発行しない。
    _verify_against_live(spec, runner, args.plugin)
    injection = build_runtime_injection(
        spec, tenant=args.tenant, plugin_id=args.plugin, base_url=args.base_url
    )
    if args.secret_only:
        manifests = [injection.render_secret_manifest(spec)]
    else:
        manifests = injection.render_injection_manifests(spec)
    return dump_manifests(manifests)


def main() -> int:
    args = _build_args()
    try:
        out = _render(args)
    except json.JSONDecodeError as exc:
        # 想定済みの失敗(不正 answers JSON)は traceback を出さず stderr に簡潔に出す。
        # stdout には何も書かない(`| kubectl apply -f -` に壊れた入力を流さない)。
        print(f"error: --answers が不正な JSON です: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # build_deploy_spec / build_runtime_injection の想定済み失敗(DeploySpecError /
        # InjectionError / GrantDenied / BrokerConfigError 等)。stdout を汚さず stderr に要約のみ。
        print(f"error: 注入マニフェスト生成に失敗: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
