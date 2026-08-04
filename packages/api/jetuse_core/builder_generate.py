"""生成オーケストレーション(specs/19 §4.5・ADR-0023)。

designed セッションから Demo(provisioning)を作り、バックグラウンドで
③a データ投入(builder_data seam — SP3-05 で配線)→③b フロント生成(runtime seam)→
③c 静的検査(fail-closed)→公開(S5 ポインタ切替)を回し、ready/failed に落とす。

- **N3 同時生成上限**: 固定名グローバルロック下で provisioning 数を数える(§4.2 N3 = ≤2)。
- **リースの持ち方**: 長い build(③b)はリース外。namespace への書き込み(公開)だけ demo lease 下で
  行い、その直前に status='provisioning' を再確認する(build 中に DELETE が走っても孤児バンドルを
  作らない — DELETE と生成は同一 demo lock で相互排他ゆえ、公開区間に入れた時点で demo は生存)。
- **runtime seam**: `build_frontend` は module 属性(既定 = generation_runtime.build_frontend の遅延
  import)。テストは monkeypatch でモック。生成物 = src(層1検査)+ dist(層2検査 + 配信)。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import (
    builder_sessions,
    bundle_inspect,
    bundles,
    conversations,
    demo_lease,
    demo_targets,
    demos,
)
from .settings import get_settings

logger = logging.getLogger("jetuse.builder_generate")


class GenerationBusyError(Exception):
    """同時生成数が N3 上限(§4.2)に達している。ルートは 503(再試行可)。"""


class GenerationConflictError(Exception):
    """attach/status 遷移が競合(別リクエストが先着)。ルートは 409。"""


@dataclass
class GenerationResult:
    """runtime の生成物(§4.5 ③b)。src=層1検査対象、dist=層2検査 + 配信対象。

    log = 生成/ビルド出力の末尾(N4)。generator = {model, prompt_version, opencode_version}(N6)。
    """

    src_files: dict[str, bytes]
    dist_files: dict[str, bytes]
    protected: frozenset[str] = field(default_factory=frozenset)
    log: str = ""
    generator: dict = field(default_factory=dict)


def _default_build_frontend(plan: dict, *, model_key: str) -> GenerationResult:
    """既定 runtime(使い捨て podman コンテナ)。重い依存は遅延 import(ユニットは monkeypatch)。"""
    from .generation_runtime import build_frontend

    return build_frontend(plan, model_key=model_key)


# フロント生成 runtime のシーム(テストは build_frontend を monkeypatch で差し替える)。
build_frontend = _default_build_frontend


def _default_provision_data(demo_id: str, plan: dict) -> dict:
    """③a データ投入の既定実装(SP3-04)。重い依存は遅延 import(ユニットは monkeypatch)。"""
    from .builder_data import provision_data as impl

    return impl(demo_id, plan)


# ③a データ投入のシーム(SP3-05 で配線 — specs/19 §4.5。テストは monkeypatch で差し替える)。
provision_data = _default_provision_data


def _record_data_usage(owner: str, usage: dict) -> None:
    """③a の LLM 使用を owner に記録(§8.3)。usage ゼロ = LLM 未起動(data なし)は記録しない。"""
    if not (usage.get("input_tokens") or usage.get("output_tokens")):
        return
    try:
        from .datasets import GEN_MODEL

        conversations.log_usage(owner, None, GEN_MODEL,
                                usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    except Exception:
        logger.warning("data provision usage log failed", exc_info=True)


def _os_locator() -> dict:
    """公開バンドルの objectstorage locator(rag の原本と同一形 — 3f/3g 掃除が同じ台帳で回収)。"""
    s = get_settings()
    return {"region": s.oci_region, "os_namespace": s.os_namespace, "bucket": s.rag_bucket}


def start(owner: str, session: dict) -> str:
    """グローバルロック下で N3 確認 → Demo(provisioning)作成 → セッションへ attach。demo_id を返す。

    呼び出し側(ルート)が status='designed' + plan を検証済みの前提。競合(attach 0 行)は
    作成した孤児行を消して Conflict。上限超過は Busy。
    """
    plan = session["plan"]
    s = get_settings()
    with demo_lease.acquire_global("gen"):
        if demos.count_provisioning() >= s.demo_max_concurrent_generations:
            raise GenerationBusyError("concurrent generation limit reached")
        demo = demos.create_demo(
            owner, plan["title"], plan.get("description"),
            config={"plan": plan}, status="provisioning",
        )
        if not builder_sessions.attach_demo(owner, session["id"], demo["id"]):
            demos.delete_demo(owner, demo["id"])  # 競合で別リクエストが先着 → 孤児を消す
            raise GenerationConflictError("session already attached to another demo")
        # attach 成立後はセッション不変(save_plan の demo_id IS NULL ガード)。ルートの
        # 読み取り〜attach の間に並行 PATCH /plan が新プランを保存した可能性があるため、
        # 確定版を読み直して demo に反映する(codex review-1: 旧プランでの生成を構造的に防ぐ)
        fresh = builder_sessions.get_session(owner, session["id"])
        fresh_plan = (fresh or {}).get("plan")
        if fresh_plan and fresh_plan != plan:
            demos.update_demo(owner, demo["id"], {
                "name": fresh_plan["title"],
                "description": fresh_plan.get("description"),
                "config": {"plan": fresh_plan},  # 作成直後 config は plan のみ = 全置換で正確
            })
    return demo["id"]


def restart(demo_id: str) -> str:
    """failed → provisioning へ再投入(§4.5 の再実行)。グローバルロック下で N3 を再確認。

    set_status(failed→provisioning) が 0 行なら別リクエストが先に動かした = Conflict。
    """
    s = get_settings()
    with demo_lease.acquire_global("gen"):
        if demos.count_provisioning() >= s.demo_max_concurrent_generations:
            raise GenerationBusyError("concurrent generation limit reached")
        if not demos.set_status(demo_id, "failed", "provisioning"):
            raise GenerationConflictError("demo is not in failed state")
    demos.merge_config(demo_id, {"generation": None})  # 前回エラーをクリア
    return demo_id


def run(demo_id: str, model_key: str | None = None) -> None:
    """バックグラウンド生成本体(§4.5 ③a/③b/③c/公開)。例外は failed に落とす(fail-closed)。

    model_key = generate body で選ばれた生成レジストリ key(SP3-06)。None = 設定既定。
    検証(未知キー 422)はルート側 — ここへは検証済みの値だけが届く。
    """
    owner = model = None
    try:
        demo = demos.get_demo(demo_id)
        if not demo or demo["status"] != "provisioning":
            return  # 競合(削除/別実行)— 何もしない
        owner = demo["owner_sub"]
        plan = demo["config"].get("plan")
        if not plan:
            raise GenerationConflictError("provisioning demo has no plan")

        # ③a データ投入(SP3-04 実装への配線 — specs/19 §4.5。LLM 生成はリース外・
        # 箱への書き込みは provision_data 内の demo_lease.mutation 下 — §8.2)。
        # usage は成功時ここで、失敗時は except 側(DataProvisionError.usage)で記録する。
        data_result = provision_data(demo_id, plan)
        _record_data_usage(owner, data_result["usage"])

        # フェーズ境界の status 再確認(§1.2 — deleting を観測したら即中止。後始末は DELETE 側)
        demo = demos.get_demo(demo_id)
        if not demo or demo["status"] != "provisioning":
            return

        # ③b フロント生成(リース外 — 長い build)。N2 サイズ超過は runtime が例外→failed。
        # model は build 前に確定 → 生成が失敗しても N5 使用を記録できる(下の finally 相当)。
        model = model_key or get_settings().generation_model
        result = build_frontend(plan, model_key=model)

        # ③c 静的検査(fail-closed — 合格したバンドルだけ公開)
        violations = bundle_inspect.inspect(
            result.src_files, result.dist_files, result.protected)
        if violations:
            raise RuntimeError(f"static inspection failed: {violations[:5]}")

        _publish(demo_id, result)
    except demo_lease.DemoGoneError as e:
        # ③a 中に DELETE を観測(§1.2)。即中止 — 遷移・後始末は DELETE 側が所有する。
        # リース前の LLM 生成で消費した usage は落とさない(provision_data が例外に添付)
        logger.info("generation aborted (demo gone): %s", demo_id)
        if owner and isinstance(getattr(e, "usage", None), dict):
            _record_data_usage(owner, e.usage)
        return
    except Exception as e:  # noqa: BLE001 — 全失敗を failed に写像(残骸は DELETE が回収)
        logger.exception("generation failed: %s", demo_id)
        # ③a 失敗(DataProvisionError)は消費済み usage を持つ — エラー経路でも記録する
        if owner and isinstance(getattr(e, "usage", None), dict):
            _record_data_usage(owner, e.usage)
        # N4: 失敗理由(生成/ビルド出力末尾を含む)を config.generation.error に記録(F1 で秘密不入)
        demos.merge_config(demo_id, {"generation": {
            "error": str(e)[:2000], "failed_at": _now()}})
        demos.set_status(demo_id, "provisioning", "failed")  # deleting なら 0 行 = 無害
    finally:
        # N5: build を開始した(model 確定)なら成功/失敗を問わず生成 LLM 使用を記録。
        # ponytail: model が None = 生成前(no-plan/競合)の離脱 → LLM 未起動ゆえ記録しない。
        if owner and model:
            _record_usage(owner, model)


def _record_usage(owner: str, model: str | None) -> None:
    """N5: 生成の LLM 使用を usage_log の流儀で owner に記録(マーカー)。

    ponytail: 正確なトークン数は署名プロキシがジョブ紐付けで確定するのが本筋(ADR — 別ブローカーは
    人間承認済み緩和で先送り)。ここでは owner+model の生成イベントを記録する最小実装。
    """
    try:
        conversations.log_usage(owner, None, model or "unknown", 0, 0)
    except Exception:
        logger.warning("generation usage log failed", exc_info=True)


def _publish(demo_id: str, result: GenerationResult) -> None:
    """公開区間(demo lease 下): status 再確認 → S6 write-ahead → put → ポインタ切替 → ready。"""
    ns = f"demo_{demo_id}"
    with demo_lease.acquire(demo_id):
        demo = demos.get_demo(demo_id)
        if not demo or demo["status"] != "provisioning":
            return  # build 中に DELETE/別遷移が確定 — 生成物は捨てる(孤児を作らない)
        bundle_id = str(uuid.uuid4())
        demo_targets.record_target(ns, "objectstorage", _os_locator())  # S6 write-ahead
        bundles.put_files(ns, bundle_id, result.dist_files)
        old = (demo["config"].get("frontend") or {}).get("bundle")
        demos.merge_config(demo_id, {  # S5: 単一 UPDATE でポインタ切替 + ログ更新
            # N6 再現性: {bundle, entry, generated_at, generator}(§5.3・§4.2 N6)
            "frontend": {"bundle": bundle_id, "entry": "index.html",
                         "generated_at": _now(), "generator": result.generator},
            "generation": {"log": result.log},  # N4: 成功時も生成ログを保存
        })
        if not demos.set_status(demo_id, "provisioning", "ready"):
            raise GenerationConflictError("status moved during publish")
        if old and old != bundle_id:  # 旧バンドル掃除(best-effort — 残っても DELETE 3g が回収)
            try:
                bundles.delete_bundle(ns, old)
            except Exception:
                logger.warning("stale bundle cleanup failed: %s/%s", ns, old, exc_info=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()
