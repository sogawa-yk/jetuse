"""生成 runtime の Container Instance バックエンド(SP3-08)の単体テスト。

実 OCI は呼ばない — CI/OS クライアントをモックし、fail-closed 境界(S1/S2/N2)と
ADR-0023 §1 の契約(命名・RP 無効・PAR 分離・補償削除・reconcile)を検査する。
"""

import io
import tarfile
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jetuse_core import generation_runtime as gr
from jetuse_core import generation_runtime_ci as ci
from jetuse_core.gen_models import GEN_MODELS


def _tgz(entries: dict[str, bytes], *, symlink: str | None = None,
         abspath: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/home/opc/.env"
            tf.addfile(info)
        if abspath:
            info = tarfile.TarInfo(abspath)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


# --- 敵性 tar の検証(相1 src / 相2 dist の取り込み境界) ---

def test_extract_validated_ok_normalizes_dot_prefix():
    out = ci._extract_validated(_tgz({"./App.jsx": b"a", "api/x.js": b"b"}),
                                max_files=10, max_bytes=1000)
    assert set(out) == {"App.jsx", "api/x.js"}


def test_extract_validated_rejects_symlink():
    with pytest.raises(RuntimeError, match="non-regular"):
        ci._extract_validated(_tgz({"ok.js": b"x"}, symlink="evil"),
                              max_files=10, max_bytes=1000)


def test_extract_validated_rejects_traversal_and_absolute():
    with pytest.raises(RuntimeError, match="unsafe path"):
        ci._extract_validated(_tgz({"../evil.js": b"x"}), max_files=10, max_bytes=1000)
    with pytest.raises(RuntimeError, match="unsafe path"):
        ci._extract_validated(_tgz({}, abspath="/etc/passwd"), max_files=10, max_bytes=1000)


def test_extract_validated_rejects_duplicate():
    # tar は同名メンバーを複数持てる(後勝ち上書きの温床)— 重複は fail-closed
    with pytest.raises(RuntimeError, match="duplicate"):
        ci._extract_validated(_tgz({"a.js": b"1", "./a.js": b"2"}),
                              max_files=10, max_bytes=1000)


def test_extract_validated_caps():
    with pytest.raises(RuntimeError, match="file-count cap"):
        ci._extract_validated(_tgz({f"f{i}.js": b"x" for i in range(5)}),
                              max_files=3, max_bytes=1000)
    with pytest.raises(RuntimeError, match="size cap"):
        ci._extract_validated(_tgz({"big.js": b"x" * 100}), max_files=10, max_bytes=50)


def test_extract_validated_rejects_garbage():
    with pytest.raises(RuntimeError, match="not a valid tar.gz"):
        ci._extract_validated(b"not-a-tarball", max_files=10, max_bytes=1000)


def test_pack_roundtrip():
    files = {"api/x.js": b"a", "App.jsx": b"b"}
    assert ci._extract_validated(ci._pack(files), max_files=10, max_bytes=1000) == files


# --- プロキシ URL の自 IP 解決(SP3-07 residual) ---

def test_proxy_url_for_ci_replaces_localhost(monkeypatch):
    monkeypatch.setattr(ci, "_self_ip", lambda: "10.9.2.99")
    s = SimpleNamespace(generation_proxy_url="http://localhost:8000/gen-proxy/v1")
    assert ci.proxy_url_for_ci(s) == "http://10.9.2.99:8000/gen-proxy/v1"
    s = SimpleNamespace(generation_proxy_url="http://10.9.2.5:8766/v1")
    assert ci.proxy_url_for_ci(s) == "http://10.9.2.5:8766/v1"


# --- 設定の fail-fast ---

def test_check_settings_requires_wiring():
    s = SimpleNamespace(generation_ci_subnet_ocid="", generation_ci_ad="ad",
                        generation_gen_image_url="g", generation_build_image_url="b",
                        rag_bucket="bkt")
    with pytest.raises(RuntimeError, match="generation_ci_subnet_ocid"):
        ci.check_settings(s)
    s.generation_ci_subnet_ocid = "ocid1.subnet.x"
    s.rag_bucket = ""
    with pytest.raises(RuntimeError, match="RAG_BUCKET"):
        ci.check_settings(s)
    s.rag_bucket = "bkt"
    ci.check_settings(s)  # 全部あれば例外なし


def test_build_frontend_dispatches_to_oci_ci(monkeypatch):
    # runtime=oci-ci は podman(_ensure_image/_attempt)へ到達せず CI backend を呼ぶ
    monkeypatch.setenv("GENERATION_PROXY_URL", "http://localhost:8000/gen-proxy/v1")
    monkeypatch.setenv("GENERATION_RUNTIME", "oci-ci")
    monkeypatch.setenv("GENERATION_CI_SUBNET_OCID", "ocid1.subnet.x")
    monkeypatch.setenv("GENERATION_CI_AD", "AD-1")
    monkeypatch.setenv("GENERATION_GEN_IMAGE_URL", "ocir/gen:t")
    monkeypatch.setenv("GENERATION_BUILD_IMAGE_URL", "ocir/build:t")
    monkeypatch.setenv("RAG_BUCKET", "bkt")
    gr.get_settings.cache_clear()
    called = {}

    def fake_attempt(plan, model, s, deadline, generator):
        called["model"] = model.oci_id
        raise RuntimeError("stop here")

    monkeypatch.setattr(ci, "attempt", fake_attempt)
    monkeypatch.setattr(gr, "_ensure_image",
                        lambda *a: (_ for _ in ()).throw(AssertionError("podman path used")))
    try:
        with pytest.raises(RuntimeError, match="generation failed after"):
            gr.build_frontend({"title": "x"}, model_key="gpt-oss-120b")
    finally:
        gr.get_settings.cache_clear()
    assert called["model"] == "openai.gpt-oss-120b"


def test_build_frontend_rejects_unknown_runtime(monkeypatch):
    monkeypatch.setenv("GENERATION_PROXY_URL", "http://p/v1")
    monkeypatch.setenv("GENERATION_RUNTIME", "kubernetes")
    gr.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="unknown generation_runtime"):
            gr.build_frontend({"title": "x"}, model_key="gpt-oss-120b")
    finally:
        gr.get_settings.cache_clear()


# --- 2 相ジョブ本体(CI/OS モック) ---

class FakeOS:
    """put/head/get/PAR/list/delete を記録する Object Storage モック。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.pars: list[dict] = []
        self.pars_deleted: list[str] = []

    def put_object(self, ns, bucket, name, data):
        self.objects[name] = data if isinstance(data, bytes) else data.encode()

    def head_object(self, ns, bucket, name):
        if name not in self.objects:
            import oci as oci_sdk
            raise oci_sdk.exceptions.ServiceError(404, "NotFound", {}, "missing")

    def get_object(self, ns, bucket, name):
        data = self.objects[name]
        return SimpleNamespace(data=SimpleNamespace(
            iter_content=lambda chunk_size: iter([data])))

    def create_preauthenticated_request(self, ns, bucket, details):
        par_id = f"par-{len(self.pars)}"
        self.pars.append({"id": par_id, "name": details.name, "object": details.object_name,
                          "access": details.access_type, "expires": details.time_expires})
        return SimpleNamespace(data=SimpleNamespace(
            id=par_id,
            full_path=f"https://os.example/p/{details.name}/n/ns/b/{bucket}/o/{details.object_name}"))

    def delete_preauthenticated_request(self, ns, bucket, par_id):
        self.pars_deleted.append(par_id)

    def list_objects(self, ns, bucket, **kw):
        objs = [SimpleNamespace(name=n, time_created=datetime.now(UTC))
                for n in self.objects if n.startswith(kw.get("prefix", ""))]
        return SimpleNamespace(data=SimpleNamespace(objects=objs, next_start_with=None))

    def delete_object(self, ns, bucket, name):
        self.objects.pop(name, None)

    def get_namespace(self):
        return SimpleNamespace(data="testns")


class FakeCI:
    """create/get/delete/logs を記録する Container Instance モック。

    on_create(name, env) フックで「CI が動いた結果」(成果物 put など)を注入する。
    """

    def __init__(self, fake_os: FakeOS, on_create):
        self.fake_os = fake_os
        self.on_create = on_create
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.states: dict[str, str] = {}

    def create_container_instance(self, details):
        cid = f"ocid1.ci.{len(self.created)}"
        c = details.containers[0]
        self.created.append({
            "id": cid, "name": details.display_name, "shape": details.shape,
            "restart": details.container_restart_policy,
            "subnet": details.vnics[0].subnet_id, "ad": details.availability_domain,
            "image": c.image_url, "env": c.environment_variables,
            "rp_disabled": c.is_resource_principal_disabled,
        })
        self.states[cid] = "ACTIVE"
        self.on_create(self, details.display_name, c.environment_variables)
        return SimpleNamespace(data=SimpleNamespace(id=cid))

    def get_container_instance(self, cid):
        return SimpleNamespace(data=SimpleNamespace(
            lifecycle_state=self.states[cid],
            containers=[SimpleNamespace(container_id=f"{cid}-c0")]))

    def delete_container_instance(self, cid):
        self.deleted.append(cid)
        self.states[cid] = "DELETED"  # 高速削除の再現(_delete_ci_confirmed が終端を確認)

    def retrieve_logs(self, container_id):
        return SimpleNamespace(data=SimpleNamespace(text=f"logs-of-{container_id}"))


def _settings():
    return SimpleNamespace(
        compartment_ocid="ocid1.compartment.test",
        oci_region="ap-osaka-1",
        rag_bucket="bkt",
        generation_proxy_url="http://localhost:8000/gen-proxy/v1",
        generation_ci_subnet_ocid="ocid1.subnet.test",
        generation_ci_ad="AD-1",
        generation_gen_image_url="ocir.example/ns/jetuse-dev-gen:t",
        generation_build_image_url="ocir.example/ns/jetuse-dev-build:t",
        generation_ci_gen_timeout_s=540,
        generation_ci_build_timeout_s=120,
        generation_timeout_s=900,
    )


@pytest.fixture
def wired(monkeypatch):
    """attempt() を FakeOS/FakeCI で配線。on_create は相ごとに成果物を置く既定動作。"""
    fake_os = FakeOS()

    def on_create(fake, name, env):
        # 成果物 = スクリプト成功の再現。gen 相は src tar、build 相は dist tar を PUT した体。
        # ログはスクリプトの trap が常に LOG_URL へ PUT する(N4 — retrieve-logs 409 対策)
        fake.fake_os.objects[_obj(env["LOG_URL"])] = f"log-of-{name}".encode()
        if name.startswith("jetuse-builder-gen-"):
            fake.fake_os.objects[_obj(env["OUT_URL"])] = _tgz({
                "App.jsx": b"generated", "api/client.js": b"EVIL-overwrite"})
        else:
            fake.fake_os.objects[_obj(env["OUT_URL"])] = _tgz({
                "index.html": b"<html>built", "assets/x.js": b"js"})
        fake.states[fake.created[-1]["id"]] = "INACTIVE"

    def _obj(par_url):  # FakeOS の PAR URL から object 名を戻す
        return par_url.split("/o/", 1)[1]

    fake_ci = FakeCI(fake_os, on_create)
    monkeypatch.setattr(ci, "_os_client", lambda: fake_os)
    monkeypatch.setattr(ci, "_ci_client", lambda: fake_ci)
    monkeypatch.setattr(ci, "_self_ip", lambda: "10.9.2.99")
    monkeypatch.setattr(ci, "_POLL_S", 0)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    return fake_os, fake_ci


def test_attempt_two_phase_success(wired):
    fake_os, fake_ci = wired
    res = ci.attempt({"title": "t"}, GEN_MODELS["gpt-oss-120b"], _settings(),
                     time.monotonic() + 900, {"model": "gpt-oss-120b"})
    # 発行した PAR は全てリソースとして削除される(期限失効に頼らない — review-2 M001)
    assert set(fake_os.pars_deleted) == {p["id"] for p in fake_os.pars}
    # 命名・shape・RP 無効・使い捨て(NEVER)・両 CI とも削除済み(残骸ゼロ)
    names = [c["name"] for c in fake_ci.created]
    assert len(names) == 2
    assert names[0].startswith("jetuse-builder-gen-")
    assert names[1].startswith("jetuse-builder-build-")
    job_id = names[0].removeprefix("jetuse-builder-gen-")
    assert names[1] == f"jetuse-builder-build-{job_id}"
    for c in fake_ci.created:
        assert c["shape"] == "CI.Standard.E4.Flex"
        assert c["restart"] == "NEVER"
        assert c["rp_disabled"] is True
        assert c["subnet"] == "ocid1.subnet.test"
        # S2: env に OCI 資格情報・OCID・鍵を渡さない(PAR URL とジョブ入力のみ)
        joined = " ".join(f"{k}={v}" for k, v in c["env"].items())
        assert "ocid1.tenancy" not in joined and "BEGIN" not in joined
        assert not any(k.startswith("OCI_") for k in c["env"])
    assert {fake_ci.created[0]["id"], fake_ci.created[1]["id"]} <= set(fake_ci.deleted)
    # 保護原本は非信頼 src から落ちる(層0 — ビルドイメージの信頼版が使われる)
    assert "api/client.js" not in res.src_files
    assert res.src_files["App.jsx"] == b"generated"
    assert "index.html" in res.dist_files
    assert res.generator["runtime"] == "oci-ci"
    # ジョブ prefix は後始末済み
    assert not [n for n in fake_os.objects if n.startswith("jetuse-builder-jobs/")]


def test_attempt_par_separation(wired):
    fake_os, fake_ci = wired
    ci.attempt({"title": "t"}, GEN_MODELS["gpt-oss-120b"], _settings(),
               time.monotonic() + 900, {})
    reads = [p for p in fake_os.pars if p["access"] == "ObjectRead"]
    writes = [p for p in fake_os.pars if p["access"] == "ObjectWrite"]
    # 読取 = 入力のみ(plan/config/検証済み src)、書込 = 成果物とログのみ。混在しない
    assert {p["object"].split("/", 2)[2] for p in reads} == {
        "in/demo-plan.json", "in/opencode.json", "p2/src.tgz"}
    assert {p["object"].split("/", 2)[2] for p in writes} == {
        "out/src.tgz", "out/dist.tgz", "out/gen.log", "out/build.log"}
    # 全 PAR が期限付き
    now = datetime.now(UTC)
    for p in fake_os.pars:
        assert now < p["expires"] < now + timedelta(hours=1)


def test_attempt_gen_ci_receives_proxy_config(wired):
    fake_os, fake_ci = wired
    ci.attempt({"title": "t"}, GEN_MODELS["gpt-oss-120b"], _settings(),
               time.monotonic() + 900, {})
    conf = fake_os.pars  # opencode.json は put されている(削除前に on_create が読む前提はない)
    # 生成相 env: モデルとタイムアウトが opencode 呼び出しに渡る
    env = fake_ci.created[0]["env"]
    assert env["GEN_MODEL"] == "oci/openai.gpt-oss-120b"
    assert env["PHASE_TIMEOUT_S"] == "540"
    # ビルド相 env: デモ実行時モデルは共用 MODELS キー(生成キーではない)
    from jetuse_core.models import MODELS
    assert fake_ci.created[1]["env"]["VITE_DEMO_MODEL"] in MODELS
    assert conf  # PAR が発行されている


def test_attempt_phase_failure_deletes_ci_and_reports_logs(wired):
    fake_os, fake_ci = wired

    def fail_create(fake, name, env):  # 成果物を置かずに終了 = 相失敗(ログだけ残す)
        fake.fake_os.objects[env["LOG_URL"].split("/o/", 1)[1]] = b"opencode exploded"
        fake.states[fake.created[-1]["id"]] = "INACTIVE"

    fake_ci.on_create = fail_create
    with pytest.raises(RuntimeError,
                       match="generation container exited without artifact") as ei:
        ci.attempt({"title": "t"}, GEN_MODELS["gpt-oss-120b"], _settings(),
                   time.monotonic() + 900, {})
    assert "opencode exploded" in str(ei.value)  # 失敗ログが診断に載る(N4)
    assert fake_ci.deleted  # 補償削除
    assert not [n for n in fake_os.objects if n.startswith("jetuse-builder-jobs/")]
    # 失敗経路でも発行済み PAR は全削除(review-2 M001)
    assert set(fake_os.pars_deleted) == {p["id"] for p in fake_os.pars}


def test_attempt_deadline_timeout_deletes_ci(wired):
    fake_os, fake_ci = wired

    def hang_create(fake, name, env):  # ACTIVE のまま成果物を置かない
        pass

    fake_ci.on_create = hang_create
    with pytest.raises(RuntimeError, match="timed out"):
        ci.attempt({"title": "t"}, GEN_MODELS["gpt-oss-120b"], _settings(),
                   time.monotonic() + 45, {})  # deadline 直近 → 即タイムアウト
    assert fake_ci.deleted


# --- reconcile(ADR-0023 §4) ---

def test_reconcile_sweeps_stale_builder_cis_only(monkeypatch):
    old = datetime.now(UTC) - timedelta(hours=1)
    fresh = datetime.now(UTC)
    items = [
        SimpleNamespace(id="a", display_name="jetuse-builder-gen-x",
                        lifecycle_state="ACTIVE", time_created=old),
        SimpleNamespace(id="b", display_name="jetuse-builder-build-y",
                        lifecycle_state="INACTIVE", time_created=old),
        SimpleNamespace(id="c", display_name="jetuse-builder-gen-z",
                        lifecycle_state="ACTIVE", time_created=fresh),   # 稼働中 — 触らない
        SimpleNamespace(id="d", display_name="jetuse-dev-app-api",
                        lifecycle_state="ACTIVE", time_created=old),     # API 本体 — 触らない
        SimpleNamespace(id="e", display_name="jetuse-builder-gen-w",
                        lifecycle_state="DELETING", time_created=old),   # 削除中 — 冪等
    ]
    deleted = []

    class FakeClient:
        def list_container_instances(self, compartment_id, **kw):
            return SimpleNamespace(data=items, has_next_page=False, next_page=None,
                                   headers={})

        def delete_container_instance(self, cid):
            deleted.append(cid)

    import contextlib

    import oci as oci_sdk
    monkeypatch.setattr(ci, "_ci_client", lambda: FakeClient())
    monkeypatch.setattr(oci_sdk.pagination, "list_call_get_all_results",
                        lambda fn, *a, **kw: fn(*a, **kw))
    from jetuse_core import demo_lease as lease_mod
    from jetuse_core import demos as demos_mod
    leased = []

    @contextlib.contextmanager
    def fake_acquire(demo_id):
        leased.append(demo_id)
        yield

    monkeypatch.setattr(lease_mod, "acquire", fake_acquire)
    monkeypatch.setattr(demos_mod, "list_stale_provisioning", lambda s: ["demo-1"])
    monkeypatch.setattr(demos_mod, "set_status", lambda d, f, t: True)
    merged = {}
    monkeypatch.setattr(demos_mod, "merge_config", lambda d, p: merged.update({d: p}))
    monkeypatch.setattr(ci, "_sweep_stale_job_objects", lambda cutoff: 3)
    monkeypatch.setattr(ci, "_sweep_expired_pars", lambda: 2)
    out = ci.reconcile()
    assert set(deleted) == {"a", "b"}
    assert out == {"ci_deleted": 2, "demos_failed": 1, "objects_deleted": 3,
                   "pars_deleted": 2}
    assert leased == ["demo-1"]  # M002: demo リース下で遷移(publish と直列化)
    assert merged["demo-1"]["generation"]["error"].startswith("generation timed out")


def test_lifespan_runs_reconcile_for_oci_ci(monkeypatch):
    # review-1 minor: 起動時 reconcile が oci-ci でだけ走り、shutdown でタスクが片づく
    import asyncio
    import threading

    from service import main as service_main
    called = threading.Event()
    monkeypatch.setattr(service_main, "get_settings",
                        lambda: SimpleNamespace(generation_runtime="oci-ci"))
    from jetuse_core import generation_runtime_ci as ci_mod
    monkeypatch.setattr(ci_mod, "reconcile", lambda: called.set())

    async def _run():
        async with service_main._lifespan(None):
            await asyncio.to_thread(called.wait, 5)

    asyncio.run(_run())  # 正常終了 = cancel 済みタスクが run を塞がない
    assert called.is_set()


def test_lifespan_noop_for_podman(monkeypatch):
    import asyncio

    from service import main as service_main
    monkeypatch.setattr(service_main, "get_settings",
                        lambda: SimpleNamespace(generation_runtime="podman"))
    from jetuse_core import generation_runtime_ci as ci_mod
    monkeypatch.setattr(ci_mod, "reconcile",
                        lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    async def _run():
        async with service_main._lifespan(None):
            await asyncio.sleep(0.05)

    asyncio.run(_run())  # reconcile が呼ばれれば AssertionError で落ちる


def test_reconcile_never_raises(monkeypatch):
    monkeypatch.setattr(ci, "_ci_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("no auth")))
    from jetuse_core import demos as demos_mod
    monkeypatch.setattr(demos_mod, "list_stale_provisioning",
                        lambda s: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(ci, "_sweep_stale_job_objects",
                        lambda cutoff: (_ for _ in ()).throw(RuntimeError("os down")))
    monkeypatch.setattr(ci, "_sweep_expired_pars",
                        lambda: (_ for _ in ()).throw(RuntimeError("par down")))
    out = ci.reconcile()  # 例外を伝播させない(起動タスクを殺さない)
    assert out == {"ci_deleted": 0, "demos_failed": 0, "objects_deleted": 0,
                   "pars_deleted": 0}


def test_delete_ci_confirmed_retries_then_confirms(monkeypatch):
    # M003: 削除要求の有界再試行 + DELETED 到達確認
    calls = {"delete": 0, "get": 0}

    class Flaky:
        def delete_container_instance(self, cid):
            calls["delete"] += 1
            if calls["delete"] == 1:
                raise RuntimeError("transient")

        def get_container_instance(self, cid):
            calls["get"] += 1
            st = "DELETING" if calls["get"] < 3 else "DELETED"
            return SimpleNamespace(data=SimpleNamespace(lifecycle_state=st))

    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    ci._delete_ci_confirmed(Flaky(), "ocid1.ci.x", wait_s=60)
    assert calls["delete"] == 2       # 1 回目失敗 → 再試行で成功
    assert calls["get"] >= 3          # DELETED を確認してから戻る


def test_sweep_expired_pars_deletes_only_expired_builder_pars(monkeypatch):
    now = datetime.now(UTC)
    pars = [
        SimpleNamespace(id="p1", name="jetuse-builder-x-r",
                        time_expires=now - timedelta(minutes=5)),   # 掃除対象
        SimpleNamespace(id="p2", name="jetuse-builder-y-w",
                        time_expires=now + timedelta(minutes=5)),   # 有効期限内 — 残す
        SimpleNamespace(id="p3", name="spa-read-par",
                        time_expires=now - timedelta(minutes=5)),   # 別用途 — 触らない
    ]
    deleted = []

    class FakeOSC:
        def list_preauthenticated_requests(self, ns, bucket, **kw):
            return SimpleNamespace(data=pars, headers={})

        def delete_preauthenticated_request(self, ns, bucket, par_id):
            deleted.append(par_id)

        def get_namespace(self):
            return SimpleNamespace(data="ns")

    monkeypatch.setattr(ci, "_os_client", lambda: FakeOSC())
    monkeypatch.setenv("RAG_BUCKET", "bkt")
    ci.get_settings.cache_clear()
    try:
        assert ci._sweep_expired_pars() == 1
    finally:
        ci.get_settings.cache_clear()
    assert deleted == ["p1"]
