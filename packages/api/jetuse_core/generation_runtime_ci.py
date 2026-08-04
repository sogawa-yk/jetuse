"""生成 runtime の Container Instance バックエンド(SP3-08 / ADR-0023 §1 決定 B')。

1 生成ジョブ = 順次 2 つの使い捨て OCI Container Instance(同時には 1 つ稼働):

- 相1 生成(非信頼) `jetuse-builder-gen-<job_id>`: OCIR 生成イメージ(OpenCode + vendored
  scaffold)。plan/opencode.json を PAR で取得 → OpenCode 実行 → src/ を tar にして PAR へ PUT。
- 相2 信頼ビルド `jetuse-builder-build-<job_id>`: 信頼ビルドイメージ(OpenCode 非搭載・
  node_modules 焼き込み)。API が検証した src を PAR で取得 → vite build → dist を PAR へ PUT。

鍵レス受け渡し(S2): rag_bucket の `jetuse-builder-jobs/<job_id>/` prefix + 期限付き
オブジェクト単位 PAR(読取専用/書込専用を相・方向ごとに分離)。CI コンテナは
`is_resource_principal_disabled=True` で RP 経路自体が無く、OCI 資格情報も渡さない。
ログは CI 側へ権限を与えず API の retrieve_logs で回収する(N4 — SP3-07 Tips)。

失敗/タイムアウトは CI 補償削除 + RuntimeError(builder_generate が demo を failed へ)。
プロセス死・削除漏れは reconcile(起動時 + 定期)が `jetuse-builder-` 命名と経過時間から
exact に回収する(ADR-0023 §4)。
"""
from __future__ import annotations

import io
import json
import logging
import posixpath
import socket
import tarfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from .builder_generate import GenerationResult
from .gen_models import GenModelDef
from .settings import get_settings

logger = logging.getLogger("jetuse.generation_runtime_ci")

_CI_SHAPE = "CI.Standard.E4.Flex"   # ADR-0023 §1: 1 OCPU / 4GB(N7)
_CI_OCPUS = 1
_CI_MEMORY_GB = 4
_NAME_PREFIX = "jetuse-builder-"    # reconcile がこの命名から exact 回収する(ADR §1/§4)
_JOB_ROOT = "jetuse-builder-jobs"   # rag_bucket 内のジョブ受け渡し prefix
_POLL_S = 5
_STARTUP_GRACE_S = 240              # CI 起動(pull 含む)の猶予。実測 43s(node 単体)+ pull 増
_PAR_MARGIN_S = 300                 # PAR 期限 = 相タイムアウト + 起動猶予 + 余裕(期限付き)
_RECONCILE_MARGIN_S = 180           # N1 ハードキャップ + これを超えた残骸を回収


def check_settings(s) -> None:
    """oci-ci runtime の必須設定(fail-fast — 未配線のまま CI 作成へ進まない)。"""
    missing = [k for k in ("generation_ci_subnet_ocid", "generation_ci_ad",
                           "generation_gen_image_url", "generation_build_image_url")
               if not getattr(s, k)]
    if missing:
        raise RuntimeError(f"generation_runtime=oci-ci requires settings: {missing}")
    if not s.rag_bucket:
        raise RuntimeError("generation_runtime=oci-ci requires RAG_BUCKET (job handoff bucket)")


# --- OCI クライアント(遅延 import。ユニットテストは以下 2 関数を monkeypatch) ---

def _ci_client():
    import os as _os

    import oci
    s = get_settings()
    if _os.environ.get("AUTH_MODE") == "resource_principal":
        return oci.container_instances.ContainerInstanceClient(
            {"region": s.oci_region}, signer=oci.auth.signers.get_resource_principals_signer())
    cfg = {**oci.config.from_file(), "region": s.oci_region}
    return oci.container_instances.ContainerInstanceClient(cfg)


def _os_client():
    from .rag import _os_client as impl

    return impl()


# --- 鍵レス受け渡し(ジョブ prefix + 期限付き PAR) ---

class _ParSet:
    """ジョブが発行した PAR の一括後始末(review-2 M001 — 期限切れはアクセス無効化であって
    リソース削除ではないため、発行した id を保持して finally で冪等に削除する)。"""

    def __init__(self, client, ns: str, bucket: str):
        self.client, self.ns, self.bucket = client, ns, bucket
        self.ids: list[str] = []

    def url(self, object_name: str, access_type: str, expires_s: int, label: str) -> str:
        """オブジェクト単位の期限付き PAR を作り、完全 URL を返す(id は後始末用に保持)。

        access_type は ObjectRead / ObjectWrite のみ(読取と書込を相・方向で分離 — 1 つの
        PAR に両権限を持たせない)。listing は与えない。
        """
        from oci.object_storage import models as m

        if access_type not in ("ObjectRead", "ObjectWrite"):
            raise ValueError(f"unexpected PAR access_type: {access_type}")
        details = m.CreatePreauthenticatedRequestDetails(
            name=f"{_NAME_PREFIX}{label}",
            object_name=object_name,
            access_type=access_type,
            time_expires=datetime.now(UTC) + timedelta(seconds=expires_s),
        )
        par = self.client.create_preauthenticated_request(self.ns, self.bucket, details).data
        self.ids.append(par.id)
        return par.full_path

    def cleanup(self) -> None:
        """発行済み PAR を全削除(404 = 済み。他失敗は warn — reconcile の期限切れ掃除が回収)。"""
        import oci as oci_sdk

        for par_id in self.ids:
            try:
                self.client.delete_preauthenticated_request(self.ns, self.bucket, par_id)
            except oci_sdk.exceptions.ServiceError as e:
                if e.status != 404:
                    logger.warning("PAR delete failed (reconcile will sweep): %s", par_id)
            except Exception:
                logger.warning("PAR delete failed (reconcile will sweep): %s", par_id,
                               exc_info=True)


def _self_ip() -> str:
    """API コンテナ自身の private IP(生成 CI から見た署名プロキシの宛先 — SP3-07 residual)。

    UDP connect はパケットを送らず egress インターフェースの自 IP だけを解決する。
    """
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.connect(("192.0.2.1", 1))  # TEST-NET-1(到達不要 — 経路選択のみ)
        return sk.getsockname()[0]
    finally:
        sk.close()


def proxy_url_for_ci(s) -> str:
    """生成 CI から到達できる署名プロキシ URL。localhost(API 内 mount の自己参照 —
    SP3-07 の既定)は自 IP へ置換する。それ以外は設定値をそのまま使う。"""
    u = urlsplit(s.generation_proxy_url)
    if u.hostname in ("localhost", "127.0.0.1"):
        return urlunsplit(u._replace(netloc=f"{_self_ip()}:{u.port or 8000}"))
    return s.generation_proxy_url


# --- 敵性 tar の検証(相1 の src / 相2 の dist を API プロセスへ安全に取り込む) ---

def _extract_validated(raw: bytes, *, max_files: int, max_bytes: int) -> dict[str, bytes]:
    """tar.gz を {正規化相対 posix パス: bytes} へ。symlink/hardlink/非通常・絶対/`..`/重複
    パス・数/サイズ超過は fail-closed(S1 — podman 経路の _reject_unsafe/_read_tree と同じ規律)。"""
    out: dict[str, bytes] = {}
    total = 0
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, EOFError) as e:
        raise RuntimeError(f"artifact is not a valid tar.gz: {e}") from None
    with tf:
        for member in tf:
            if member.isdir():
                continue
            if not member.isreg():
                raise RuntimeError(f"artifact has a non-regular member (rejected): {member.name}")
            # normpath が `./` を落とす。lstrip は文字集合除去で `../` を壊すため使わない
            rel = posixpath.normpath(member.name)
            if (not rel or rel in (".", "..") or rel.startswith(("/", "../"))):
                raise RuntimeError(f"artifact has an unsafe path (rejected): {member.name}")
            if rel in out:
                raise RuntimeError(f"artifact has a duplicate path (rejected): {rel}")
            if len(out) >= max_files:
                raise RuntimeError(f"artifact exceeds file-count cap ({max_files})")
            total += member.size
            if total > max_bytes:
                raise RuntimeError(f"artifact exceeds size cap ({max_bytes} bytes)")
            f = tf.extractfile(member)
            out[rel] = f.read() if f else b""
    return out


def _pack(files: dict[str, bytes]) -> bytes:
    """検証済み {相対パス: bytes} を tar.gz へ(相2 への信頼入力 — API が唯一の作成者)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in sorted(files):
            info = tarfile.TarInfo(rel)
            info.size = len(files[rel])
            tf.addfile(info, io.BytesIO(files[rel]))
    return buf.getvalue()


# --- CI ライフサイクル ---

def _create_ci(client, s, name: str, image_url: str, env: dict[str, str]) -> str:
    """使い捨て CI を作成して id を返す。資格情報ゼロ: RP 無効・pull は public repo の匿名 pull
    (ADR-0011)・env に秘密なし(PAR は期限付き capability URL)。"""
    from oci.container_instances import models as m

    details = m.CreateContainerInstanceDetails(
        compartment_id=s.compartment_ocid,
        availability_domain=s.generation_ci_ad,
        display_name=name,
        shape=_CI_SHAPE,
        shape_config=m.CreateContainerInstanceShapeConfigDetails(
            ocpus=_CI_OCPUS, memory_in_gbs=_CI_MEMORY_GB),
        vnics=[m.CreateContainerVnicDetails(
            subnet_id=s.generation_ci_subnet_ocid, is_public_ip_assigned=False)],
        container_restart_policy="NEVER",  # 相スクリプトは一度きり(終了で INACTIVE へ)
        graceful_shutdown_timeout_in_seconds=5,
        containers=[m.CreateContainerDetails(
            display_name=name,
            image_url=image_url,
            environment_variables=env,
            is_resource_principal_disabled=True,  # S2: RPST 取得経路自体を消す(ADR §1)
        )],
    )
    ci = client.create_container_instance(details).data
    logger.info("created %s (%s)", name, ci.id)
    return ci.id


def _delete_ci_quiet(client, ci_id: str | None) -> None:
    """補償削除(冪等)。失敗しても reconcile が命名から回収するため伝播させない。"""
    if not ci_id:
        return
    try:
        client.delete_container_instance(ci_id)
    except Exception:
        logger.warning("container instance delete failed (reconcile will sweep): %s",
                       ci_id, exc_info=True)


def _delete_ci_confirmed(client, ci_id: str, wait_s: int = 120) -> None:
    """削除要求(有界再試行)→ DELETED 到達の確認(review-2 M003 — 「同時には 1 つ稼働」と
    残骸ゼロを要求だけでなく終端状態で確認する)。確認不能でも伝播させない(reconcile が回収)。"""
    for i in range(3):
        try:
            client.delete_container_instance(ci_id)
            break
        except Exception as e:
            if getattr(e, "status", None) in (404, 409):
                break  # 404 = 既に無い / 409 = 既に DELETING — どちらも削除は進行済み
            logger.warning("CI delete attempt %d failed: %s", i + 1, ci_id)
            time.sleep(2 * (i + 1))
    else:
        logger.warning("CI delete requests exhausted (reconcile will sweep): %s", ci_id)
        return
    waited = 0
    while waited < wait_s:
        try:
            st = client.get_container_instance(ci_id).data.lifecycle_state
        except Exception:
            return  # 404 等 = もう存在しない
        if st == "DELETED":
            return
        time.sleep(_POLL_S)
        waited += max(_POLL_S, 1)
    logger.warning("CI not DELETED within %ss (reconcile will sweep): %s", wait_s, ci_id)


def _retrieve_logs(client, ci_id: str) -> str:
    """コンテナ stdout/stderr を API 側権限で回収(最終フォールバック)。

    実機確認: retrieve-logs はコンテナ INACTIVE 後 409 で取得不能。よって一次経路は
    相スクリプト自身の LOG_URL(書込 PAR)への書き出し(_fetch_log)で、これは起動失敗
    (pull 失敗等でスクリプトが走らない)時のみ意味を持つ。"""
    try:
        ci = client.get_container_instance(ci_id).data
        cid = ci.containers[0].container_id
        data = client.retrieve_logs(cid).data
        text = data.text if hasattr(data, "text") else data.content.decode("utf-8", "replace")
        return text
    except Exception:
        logger.info("retrieve_logs unavailable (expected after exit): %s", ci_id)
        return ""


def _fetch_log(os_client, ns: str, bucket: str, log_obj: str, cap: int = 64 * 1024) -> str:
    """相スクリプトが LOG_URL へ書いたログを回収(N4 一次経路。無ければ空)。"""
    if not _object_exists(os_client, ns, bucket, log_obj):
        return ""
    try:
        return _get_object_bytes(os_client, ns, bucket, log_obj, cap).decode("utf-8", "replace")
    except Exception:
        logger.warning("phase log fetch failed: %s", log_obj, exc_info=True)
        return ""


def _iter_objects(client, ns: str, bucket: str, prefix: str, fields: str = "name"):
    """prefix 配下の object をページネーション完走で全列挙。"""
    start = None
    while True:
        kw = {"prefix": prefix, "fields": fields}
        if start:
            kw["start"] = start
        resp = client.list_objects(ns, bucket, **kw)
        yield from resp.data.objects
        start = resp.data.next_start_with
        if not start:
            return


def _object_exists(os_client, ns: str, bucket: str, name: str) -> bool:
    import oci as oci_sdk

    try:
        os_client.head_object(ns, bucket, name)
        return True
    except oci_sdk.exceptions.ServiceError as e:
        if e.status == 404:
            return False
        raise


def _await_phase(ci_client, os_client, ns: str, bucket: str, ci_id: str, out_obj: str,
                 log_obj: str, deadline: float, phase_timeout_s: int, phase: str) -> str:
    """相の完了(成果物オブジェクト出現)を待つ。終了/タイムアウトはログ回収 → CI 削除。

    成功判定 = 成果物の存在(スクリプトは成功時のみ最後に PUT する)。コンテナ終了を観測しても
    直後の PUT 反映と競合しうるため、終端状態では一拍おいて再確認してから失敗と断定する。
    ログはスクリプトが LOG_URL(書込 PAR)へ書いた log_obj から回収する(N4 — INACTIVE 後の
    retrieve-logs は 409 で不能と実機確認済み。起動失敗時のみ retrieve_logs フォールバック)。
    """
    watch_until = time.monotonic() + _STARTUP_GRACE_S + phase_timeout_s
    try:
        while True:
            if _object_exists(os_client, ns, bucket, out_obj):
                return _fetch_log(os_client, ns, bucket, log_obj)
            state = ci_client.get_container_instance(ci_id).data.lifecycle_state
            if state in ("INACTIVE", "FAILED", "DELETING", "DELETED"):
                time.sleep(3)  # 終了直前の PUT の反映待ち(存在すれば成功)
                if _object_exists(os_client, ns, bucket, out_obj):
                    return _fetch_log(os_client, ns, bucket, log_obj)
                log = (_fetch_log(os_client, ns, bucket, log_obj)
                       or _retrieve_logs(ci_client, ci_id))
                raise RuntimeError(
                    f"{phase} container exited without artifact (state={state}): {log[-2000:]}")
            now = time.monotonic()
            if now > watch_until or now > deadline - 30:
                raise RuntimeError(f"{phase} phase timed out after {phase_timeout_s}s")
            time.sleep(_POLL_S)
    finally:
        # DELETED 到達まで確認(次相と稼働を重ねない・残骸ゼロを状態で確認 — review-2 M003)
        _delete_ci_confirmed(ci_client, ci_id)


def _get_object_bytes(os_client, ns: str, bucket: str, name: str, cap: int) -> bytes:
    """成果物オブジェクトを上限付きで取得(N2 — 巨大成果物を丸読みしない)。"""
    resp = os_client.get_object(ns, bucket, name)
    buf = bytearray()
    for chunk in resp.data.iter_content(chunk_size=1024 * 1024):
        buf += chunk
        if len(buf) > cap:
            raise RuntimeError(f"artifact exceeds size cap ({cap} bytes)")
    return bytes(buf)


# --- 相ドライバ(generation_runtime.build_frontend からディスパッチされる) ---

def attempt(plan: dict, model: GenModelDef, s, deadline: float,
            generator: dict) -> GenerationResult:
    """1 回の生成試行(相1 生成 CI → API 検証 → 相2 ビルド CI)。失敗は RuntimeError。"""
    # 重い定数・検査規律は podman 経路と同一の単一真実源を使う(遅延 import — 循環回避)
    from . import generation_runtime as gr
    from .models import DEFAULT_MODEL as demo_runtime_model

    job_id = uuid.uuid4().hex[:12]
    prefix = f"{_JOB_ROOT}/{job_id}/"
    os_client = _os_client()
    from .rag import _resolve_os_namespace

    ns = _resolve_os_namespace(os_client)
    bucket = s.rag_bucket
    ci = _ci_client()
    pars = _ParSet(os_client, ns, bucket)
    gen_ci_id = build_ci_id = None
    timings: dict[str, float] = {}

    def _remaining() -> int:
        r = int(deadline - time.monotonic())
        if r <= 30:
            raise RuntimeError("generation deadline exceeded")
        return r

    try:
        # --- ジョブ入力(信頼・API 書込): plan と opencode 設定 ---
        plan_obj = f"{prefix}in/demo-plan.json"
        conf_obj = f"{prefix}in/opencode.json"
        os_client.put_object(ns, bucket, plan_obj,
                             json.dumps(plan, ensure_ascii=False).encode())
        os_client.put_object(ns, bucket, conf_obj,
                             gr._opencode_config(model, proxy_url_for_ci(s)).encode())

        # --- 相1: 生成 CI(非信頼) ---
        t1 = min(s.generation_ci_gen_timeout_s, _remaining())
        par_ttl1 = t1 + _PAR_MARGIN_S
        src_obj = f"{prefix}out/src.tgz"
        gen_log_obj = f"{prefix}out/gen.log"
        started = time.monotonic()
        gen_ci_id = _create_ci(ci, s, f"{_NAME_PREFIX}gen-{job_id}",
                               s.generation_gen_image_url, {
            "PLAN_URL": pars.url(plan_obj, "ObjectRead",
                                 par_ttl1, f"{job_id}-p1-plan-r"),
            "CONFIG_URL": pars.url(conf_obj, "ObjectRead",
                                   par_ttl1, f"{job_id}-p1-conf-r"),
            "OUT_URL": pars.url(src_obj, "ObjectWrite",
                                par_ttl1, f"{job_id}-p1-src-w"),
            "LOG_URL": pars.url(gen_log_obj, "ObjectWrite",
                                par_ttl1, f"{job_id}-p1-log-w"),
            "GEN_MODEL": f"oci/{model.oci_id}",
            "GEN_PROMPT": gr._GEN_PROMPT,
            "PHASE_TIMEOUT_S": str(t1),
        })
        gen_log = _await_phase(ci, os_client, ns, bucket, gen_ci_id, src_obj,
                               gen_log_obj, deadline, t1, "generation")
        gen_ci_id = None  # _await_phase が削除済み
        timings["phase1_s"] = round(time.monotonic() - started, 1)

        # --- API 検証: 敵性 src を取り込み、保護原本は信頼イメージの scaffold に委ねる ---
        raw = _get_object_bytes(os_client, ns, bucket, src_obj, gr._MAX_BUNDLE_BYTES)
        src_files = _extract_validated(raw, max_files=gr._MAX_FILES,
                                       max_bytes=gr._MAX_BUNDLE_BYTES)
        for rel in gr._SRC_PROTECTED:
            src_files.pop(rel, None)  # 生成側の client.js 改変は持ち込まない(層0)
        v_obj = f"{prefix}p2/src.tgz"
        os_client.put_object(ns, bucket, v_obj, _pack(src_files))

        # --- 相2: 信頼ビルド CI ---
        t2 = min(s.generation_ci_build_timeout_s, _remaining())
        par_ttl2 = t2 + _PAR_MARGIN_S
        dist_obj = f"{prefix}out/dist.tgz"
        build_log_obj = f"{prefix}out/build.log"
        started = time.monotonic()
        build_ci_id = _create_ci(ci, s, f"{_NAME_PREFIX}build-{job_id}",
                                 s.generation_build_image_url, {
            "SRC_URL": pars.url(v_obj, "ObjectRead",
                                par_ttl2, f"{job_id}-p2-src-r"),
            "PLAN_URL": pars.url(plan_obj, "ObjectRead",
                                 par_ttl2, f"{job_id}-p2-plan-r"),
            "OUT_URL": pars.url(dist_obj, "ObjectWrite",
                                par_ttl2, f"{job_id}-p2-dist-w"),
            "LOG_URL": pars.url(build_log_obj, "ObjectWrite",
                                par_ttl2, f"{job_id}-p2-log-w"),
            # デモ実行時チャットのモデル(共用 MODELS キー — 生成モデルとは別名前空間 SP3-06)
            "VITE_DEMO_MODEL": demo_runtime_model,
            "PHASE_TIMEOUT_S": str(t2),
        })
        build_log = _await_phase(ci, os_client, ns, bucket, build_ci_id, dist_obj,
                                 build_log_obj, deadline, t2, "build")
        build_ci_id = None
        timings["phase2_s"] = round(time.monotonic() - started, 1)

        dist_files = _extract_validated(
            _get_object_bytes(os_client, ns, bucket, dist_obj, gr._MAX_BUNDLE_BYTES),
            max_files=gr._MAX_FILES, max_bytes=gr._MAX_BUNDLE_BYTES)
        if "index.html" not in dist_files:
            raise RuntimeError(f"build produced no index.html | opencode: {gr._tail(gen_log)}")
        log = gr._tail(f"# opencode\n{gen_log}\n\n# build\n{build_log}")
        meta = {**generator, "runtime": "oci-ci", "job_id": job_id, "timings_s": timings}
        return GenerationResult(src_files, dist_files, gr._SRC_PROTECTED, log, meta)
    finally:
        _delete_ci_quiet(ci, gen_ci_id)
        _delete_ci_quiet(ci, build_ci_id)
        pars.cleanup()  # 発行 PAR をリソースとして削除(期限失効に頼らない — review-2 M001)
        try:  # ジョブ prefix の後始末。残っても reconcile が回収
            for name in [o.name for o in _iter_objects(os_client, ns, bucket, prefix)]:
                os_client.delete_object(ns, bucket, name)
        except Exception:
            logger.warning("job prefix cleanup failed: %s", prefix, exc_info=True)


# --- reconcile(ADR-0023 §4: 起動時 + 定期。API 再起動・各相タイムアウトの取り残しを回収) ---

# 生成ジョブ全体(データ投入 → build_frontend の N1 15 分)を包む demo 側の見切り閾値。
# updated_at は provisioning 開始時刻で、runtime の deadline はデータ投入後に始まるため、
# N1 + データ投入余裕をとる(review-2 M002 — 正常稼働中の生成を先に failed 化しない)。
_DEMO_STALE_MARGIN_S = 600


def reconcile() -> dict:
    """孤児 CI(jetuse-builder-*)の補償削除・N1 超過 provisioning の failed 化・
    残置ジョブオブジェクト/期限切れ PAR の掃除。各段は独立に best-effort(例外を伝播させない)。"""
    s = get_settings()
    cap_s = s.generation_timeout_s + _RECONCILE_MARGIN_S
    cutoff = datetime.now(UTC) - timedelta(seconds=cap_s)
    out = {"ci_deleted": 0, "demos_failed": 0, "objects_deleted": 0, "pars_deleted": 0}

    try:
        import oci as oci_sdk

        client = _ci_client()
        items = oci_sdk.pagination.list_call_get_all_results(
            client.list_container_instances, s.compartment_ocid).data
        for it in items:
            name = getattr(it, "display_name", "") or ""
            if not name.startswith(_NAME_PREFIX):
                continue  # 生成ジョブ以外の CI(API 本体等)には触れない(exact 回収)
            if it.lifecycle_state in ("DELETED", "DELETING"):
                continue
            if it.time_created and it.time_created < cutoff:
                _delete_ci_quiet(client, it.id)
                out["ci_deleted"] += 1
    except Exception:
        logger.warning("reconcile: CI sweep failed", exc_info=True)

    try:
        out["demos_failed"] = _fail_stale_provisioning(
            s.generation_timeout_s + _DEMO_STALE_MARGIN_S)
    except Exception:
        logger.warning("reconcile: stale provisioning sweep failed", exc_info=True)

    try:
        out["objects_deleted"] = _sweep_stale_job_objects(cutoff)
    except Exception:
        logger.warning("reconcile: job object sweep failed", exc_info=True)

    try:
        out["pars_deleted"] = _sweep_expired_pars()
    except Exception:
        logger.warning("reconcile: PAR sweep failed", exc_info=True)

    if any(out.values()):
        logger.info("reconcile: %s", out)
    return out


def _fail_stale_provisioning(stale_s: int) -> int:
    """N1+余裕を超えた provisioning を demo リース下で failed 化(review-2 M002 —
    _publish のポインタ切替〜ready 遷移と直列化し、公開途中の demo を failed に落とさない)。"""
    from . import demo_lease, demos

    n = 0
    for demo_id in demos.list_stale_provisioning(stale_s):
        try:
            with demo_lease.acquire(demo_id):
                # リース下で再確認(いま publish/削除が確定した直後なら 0 行 = 触らない)
                if demo_id not in demos.list_stale_provisioning(stale_s):
                    continue
                if demos.set_status(demo_id, "provisioning", "failed"):
                    demos.merge_config(demo_id, {"generation": {
                        "error": "generation timed out (reconciled)",
                        "failed_at": datetime.now(UTC).isoformat()}})
                    n += 1
        except Exception:
            logger.warning("reconcile: demo %s sweep skipped", demo_id, exc_info=True)
    return n


def _sweep_stale_job_objects(cutoff) -> int:
    """jetuse-builder-jobs/ 配下で作成時刻が cutoff より古いオブジェクトを削除。"""
    from .rag import _resolve_os_namespace, delete_objects

    s = get_settings()
    if not s.rag_bucket:
        return 0
    client = _os_client()
    ns = _resolve_os_namespace(client)
    stale = [o.name for o in _iter_objects(client, ns, s.rag_bucket, f"{_JOB_ROOT}/",
                                           fields="name,timeCreated")
             if o.time_created and o.time_created < cutoff]
    delete_objects(stale)
    return len(stale)


def _sweep_expired_pars() -> int:
    """jetuse-builder- 命名で期限切れの PAR リソースを削除(review-2 M001 — 期限は
    アクセス無効化のみでリソースは残る。プロセス死で cleanup が漏れた分の回収)。"""
    s = get_settings()
    if not s.rag_bucket:
        return 0
    client = _os_client()
    from .rag import _resolve_os_namespace

    ns = _resolve_os_namespace(client)
    now = datetime.now(UTC)
    n = 0
    page = None
    while True:
        kw = {"page": page} if page else {}
        resp = client.list_preauthenticated_requests(ns, s.rag_bucket, **kw)
        for p in resp.data:
            if (p.name or "").startswith(_NAME_PREFIX) and p.time_expires and \
                    p.time_expires < now:
                client.delete_preauthenticated_request(ns, s.rag_bucket, p.id)
                n += 1
        page = resp.headers.get("opc-next-page") if hasattr(resp, "headers") else None
        if not page:
            return n
