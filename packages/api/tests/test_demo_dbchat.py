"""dbchat デモスコープ縦切り(SP2-03 / specs/18 §4.3)のテスト。

demos・lease・datasets/nl2sql 層は fake、require_demo seam と route 配線は実関数。
読み取り = require_demo(公開デモは非所有者 200)、書き込み = require_demo_owner(同一形 404)。
demo nl2sql のモデルは config.dbchat.model 固定(リクエストの model は無視)。
"""

import contextlib

import pytest
from fastapi.testclient import TestClient
from jetuse_shared.sqlguard import SqlBoundaryError, SqlRejectedError

import service.demo_context as demo_context
import service.main as service_main
from jetuse_core import demo_lease
from jetuse_core.owner_keys import OwnerKeyPreflightError
from service.main import app

client = TestClient(app)

DEMOS = {
    "d1": {"id": "d1", "owner_sub": "dev-user", "name": "mine", "visibility": "private",
           "status": "ready", "config": {}},
    "d2": {"id": "d2", "owner_sub": "dev-user", "name": "mine2", "visibility": "private",
           "status": "ready", "config": {}},
    "theirs": {"id": "theirs", "owner_sub": "user-a", "name": "A's",
               "visibility": "private", "status": "ready", "config": {}},
    "pub": {"id": "pub", "owner_sub": "user-a", "name": "shared",
            "visibility": "public", "status": "ready",
            "config": {"dbchat": {"model": "cohere.command-a-03-2025"}}},
}


class FakeDatasets:
    """owner キー(namespace)ごとに分離した datasets fake。"""

    def __init__(self):
        self.boxes: dict[str, list[dict]] = {}
        self.profile_calls: list[tuple] = []

    def ensure_profile(self, owner, model=None, lease=None):
        self.profile_calls.append((owner, model, lease))
        return f"JETUSE_DS_{owner.upper()[:8]}"

    def list_datasets(self, owner):
        return [dict(d) for d in self.boxes.get(owner, [])]

    def create_dataset(self, owner, display, content, model=None, warmup=True,
                       lease=None):
        ds = {"id": f"{owner}-ds{len(self.boxes.get(owner, [])) + 1}",
              "table_name": f"JETUSE_DS_X_{len(self.boxes.get(owner, [])) + 1}",
              "display_name": display, "columns": ["c1"], "row_count": 1}
        self.boxes.setdefault(owner, []).append(ds)
        return {**ds, "ready": True}

    def generate_dataset(self, owner, description, display_name=None, rows=30,
                         model=None, lease=None):
        return self.create_dataset(owner, display_name or description, b"", model=model,
                                   lease=lease)

    def preview(self, owner, ds_id, limit=20):
        if not any(d["id"] == ds_id for d in self.boxes.get(owner, [])):
            raise ValueError("データセットが見つかりません")
        return {"columns": ["c1"], "rows": [["v"]], "row_count": 1, "truncated": False}

    def delete_dataset(self, owner, ds_id, lease=None):
        box = self.boxes.get(owner, [])
        before = len(box)
        self.boxes[owner] = [d for d in box if d["id"] != ds_id]
        return len(self.boxes[owner]) < before


@pytest.fixture(autouse=True)
def fake_demos(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)


@pytest.fixture(autouse=True)
def fake_lease(monkeypatch):
    leases: list[str] = []

    @contextlib.contextmanager
    def fake_mutation(demo_id, **kw):
        leases.append(demo_id)
        yield demo_lease.DemoLease(demo_id=demo_id, _conn=None)

    monkeypatch.setattr(demo_lease, "mutation", fake_mutation)
    yield leases


@pytest.fixture(autouse=True)
def no_gate(monkeypatch):
    """preflight/route の fast fail-closed 検査(owner_key_gate / VPD 完全性)を no-op 化。"""
    import service.routes.dbchat as dbchat_routes
    import service.routes.demos as demo_routes
    monkeypatch.setattr(demo_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(dbchat_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(dbchat_routes.vpd, "integrity_gate", lambda: None)


@pytest.fixture()
def fake_ds(monkeypatch):
    fake = FakeDatasets()
    for name in ("ensure_profile", "list_datasets", "create_dataset",
                 "generate_dataset", "preview", "delete_dataset"):
        monkeypatch.setattr(service_main.datasets, name, getattr(fake, name))
    yield fake


# --- nl2sql(SSE)— モデルは config 固定 ---

def test_demo_nl2sql_owner_roundtrip(fake_ds, fake_lease, monkeypatch):
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai",
                        lambda q, profile_name=None, model=None:
                        f"SELECT * FROM {profile_name}")
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "売上を見せて"})
    assert res.status_code == 200
    assert '"sql"' in res.text and res.text.rstrip().endswith("data: [DONE]")
    # namespace 固定 + lease 下で ensure_profile(specs/18 §3.2.1)
    owner, model, lease = fake_ds.profile_calls[0]
    assert owner == "demo_d1"
    assert model is None  # config なし → 既定モデル
    assert lease is not None and lease.demo_id == "d1"
    # リースは 2 回取得: preflight(status/可否の fast 検査)+ worker(profile 再構築下)
    # — review-4 M001 で warmup を worker へ残したため(fast 検査だけ stream 前)。
    assert fake_lease == ["d1", "d1"]


def test_demo_nl2sql_model_fixed_to_config_ignores_request(fake_ds, monkeypatch):
    """非所有者のモデル指定が無効(specs/18 §4.3 — 共有プロファイルを書き換えさせない)。"""
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai",
                        lambda q, profile_name=None, model=None: "SELECT 1 FROM dual")
    res = client.post("/api/demos/pub/dbchat/nl2sql",
                      json={"question": "q", "model": "meta.llama-3.3-70b-instruct",
                            "target": "sample", "backend": "sql_search"})
    assert res.status_code == 200
    owner, model, _ = fake_ds.profile_calls[0]
    assert owner == "demo_pub"
    assert model == "cohere.command-a-03-2025"  # config.dbchat.model が正


def test_demo_nl2sql_404_for_others_private(fake_ds):
    res = client.post("/api/demos/theirs/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 404


# --- preflight を SSE 開始前に実行(review-3 M002): fail-closed 例外は 200 SSE でなく HTTP へ ---

def test_demo_nl2sql_gone_during_preflight_is_404_not_sse(fake_ds, monkeypatch):
    """デモ削除競合(mutation が DemoGoneError)は 404。ストリーム開始後の 200 SSE エラーにしない。"""
    @contextlib.contextmanager
    def gone(demo_id, **kw):
        raise demo_lease.DemoGoneError("demo not found")
        yield  # pragma: no cover

    monkeypatch.setattr(demo_lease, "mutation", gone)
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 404
    assert "data:" not in res.text  # SSE 本体が始まっていない


def test_demo_nl2sql_lease_unavailable_during_preflight_is_503(fake_ds, monkeypatch):
    @contextlib.contextmanager
    def busy(demo_id, **kw):
        raise demo_lease.LeaseUnavailableError("locked")
        yield  # pragma: no cover

    monkeypatch.setattr(demo_lease, "mutation", busy)
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 503
    assert "data:" not in res.text


def test_demo_nl2sql_owner_key_pending_during_preflight_is_503(fake_ds, monkeypatch):
    """owner-key preflight 失敗(fast 検査)も SSE 前に 503 へ写像。"""
    import service.routes.dbchat as dbchat_routes

    def raising():
        raise OwnerKeyPreflightError("owner key migration pending")

    monkeypatch.setattr(dbchat_routes, "owner_key_gate", raising)
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 503
    assert "data:" not in res.text


def test_demo_nl2sql_preflight_lease_is_nowait(fake_ds, monkeypatch):
    """preflight のリース取得は nowait(timeout_s=0)= 競合時に stream 開始前で待たず即 503
    (review-5 M001 — 既定 300s の timeout を防ぐ)。worker 側は既定 timeout(keepalive 下)。"""
    calls = []

    @contextlib.contextmanager
    def rec(demo_id, *, timeout_s=demo_lease.LOCK_TIMEOUT_S):
        calls.append(timeout_s)
        yield demo_lease.DemoLease(demo_id=demo_id, _conn=None)

    monkeypatch.setattr(demo_lease, "mutation", rec)
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai",
                        lambda q, profile_name=None, model=None: "SELECT 1 FROM dual")
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 200
    assert calls[0] == 0                            # preflight = nowait(競合で待たない)
    assert calls[1] == demo_lease.LOCK_TIMEOUT_S    # worker = 既定 timeout(keepalive 下で待てる)


def test_demo_nl2sql_delete_race_after_stream_start_is_explicit_sse_error(fake_ds, monkeypatch):
    """preflight 通過後(stream 開始後)に DELETE 競合で worker の再取得が DemoGone になった場合、
    HTTP 404 は既に送れないため **明示的な SSE エラー契約**(code=demo_gone)で通知する
    (review-6 M001 — 汎用「生成失敗」に埋もれさせない)。"""
    calls = {"n": 0}

    @contextlib.contextmanager
    def racing(demo_id, *, timeout_s=demo_lease.LOCK_TIMEOUT_S):
        calls["n"] += 1
        if calls["n"] == 1:          # preflight は通過(demo 存在)
            yield demo_lease.DemoLease(demo_id=demo_id, _conn=None)
        else:                         # worker 再取得時に DELETE 競合
            raise demo_lease.DemoGoneError(demo_id)

    monkeypatch.setattr(demo_lease, "mutation", racing)
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 200                  # stream は開始済み(preflight 通過)
    assert '"ka"' in res.text                        # keepalive 送出済み
    assert '"code": "demo_gone"' in res.text          # 明示的な SSE エラー契約
    assert "SQL生成に失敗" not in res.text            # 汎用エラーに埋もれていない
    assert res.text.rstrip().endswith("data: [DONE]")


def test_demo_nl2sql_profile_build_stays_in_worker_not_preflight(fake_ds, monkeypatch):
    """cold cache の profile 再構築/warmup は SSE ワーカー内(review-4 M001): preflight を通過した後
    ensure_profile が失敗しても **stream は開始済み**(keepalive 送出済み)で 200 SSE エラーになる
    — つまり遅い warmup が stream 開始をブロックしない(fast 検査だけが 404/503 を返す)。"""
    def slow_build_fails(owner, model=None, lease=None):
        raise RuntimeError("select AI profile warmup failed (cold)")

    monkeypatch.setattr(service_main.datasets, "ensure_profile", slow_build_fails)
    res = client.post("/api/demos/d1/dbchat/nl2sql", json={"question": "q"})
    assert res.status_code == 200                     # stream は開始済み(preflight は通過)
    assert '"ka"' in res.text                          # 最初の keepalive が出ている
    assert "SQL生成に失敗" in res.text                  # build 失敗は SSE エラーフレーム
    assert res.text.rstrip().endswith("data: [DONE]")


# --- execute — 層2ゲート(403)と VPD owner キー ---

def test_demo_execute_passes_namespace_owner_key(fake_ds, monkeypatch):
    captured = {}

    def fake_exec(sql, owner_key):
        captured["owner_key"] = owner_key
        return {"columns": ["C"], "rows": [["1"]], "row_count": 1, "truncated": False}

    monkeypatch.setattr(service_main.nl2sql, "execute_readonly", fake_exec)
    res = client.post("/api/demos/d1/dbchat/execute", json={"sql": "SELECT 1 FROM dual"})
    assert res.status_code == 200
    assert captured["owner_key"] == "demo_d1"


def test_demo_execute_boundary_violation_is_403(fake_ds, monkeypatch):
    def fake_exec(sql, owner_key):
        raise SqlBoundaryError("データディクショナリ/動的ビュー ALL_TAB_COLUMNS は参照できません")

    monkeypatch.setattr(service_main.nl2sql, "execute_readonly", fake_exec)
    res = client.post("/api/demos/d1/dbchat/execute",
                      json={"sql": "SELECT * FROM ALL_TAB_COLUMNS"})
    assert res.status_code == 403


def test_demo_execute_sanitize_rejection_is_400(fake_ds, monkeypatch):
    def fake_exec(sql, owner_key):
        raise SqlRejectedError("SELECT文のみ実行できます")

    monkeypatch.setattr(service_main.nl2sql, "execute_readonly", fake_exec)
    res = client.post("/api/demos/d1/dbchat/execute", json={"sql": "DROP TABLE x"})
    assert res.status_code == 400


def test_user_execute_boundary_violation_is_403(monkeypatch):
    """user 経路の execute も同じ層2ゲート(specs/18 §4.3 呼び出し元契約)。"""
    def fake_exec(sql, owner_key):
        assert owner_key == "dev-user"  # owner キーはヘルパー経由
        raise SqlBoundaryError("パッケージ DBMS_XMLGEN は呼び出せません")

    monkeypatch.setattr(service_main.nl2sql, "execute_readonly", fake_exec)
    res = client.post("/api/dbchat/execute",
                      json={"sql": "SELECT DBMS_XMLGEN.GETXML('x') FROM dual"})
    assert res.status_code == 403


def test_demo_execute_gate_fail_closed_503(fake_ds, monkeypatch):
    """未分類の予約接頭辞行が残る間は demo execute も fail-closed(503)。"""
    import service.routes.demos as demo_routes

    def gate():
        raise OwnerKeyPreflightError("1 reserved-prefix owner rows need classification")

    monkeypatch.setattr(demo_routes, "owner_key_gate", gate)
    res = client.post("/api/demos/d1/dbchat/execute", json={"sql": "SELECT 1 FROM dual"})
    assert res.status_code == 503


# --- schema — 箱の datasets から(登録簿ベース) ---

def test_demo_schema_reflects_only_own_box(fake_ds):
    # schema_info は実関数のまま(モジュール内の list_datasets が fake に差し替わっている)
    fake_ds.create_dataset("demo_d1", "売上", b"")
    res = client.get("/api/demos/d1/dbchat/schema")
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "datasets"
    assert [t["comment"] for t in body["tables"]] == ["売上"]
    # 空の箱は空(他の箱の表が混ざらない)
    assert client.get("/api/demos/d2/dbchat/schema").json()["tables"] == []


# --- datasets CRUD — 認可と箱の分離 ---

def test_demo_datasets_crud_isolated_per_box(fake_ds, fake_lease):
    up = client.post("/api/demos/d1/db/datasets",
                     files={"file": ("sales.csv", b"a,b\n1,2", "text/csv")})
    assert up.status_code == 200
    ds_id = up.json()["id"]
    assert [d["id"] for d in client.get("/api/demos/d1/db/datasets").json()["datasets"]] \
        == [ds_id]
    assert client.get("/api/demos/d2/db/datasets").json()["datasets"] == []
    assert "d1" in fake_lease  # 書き込みはリース下(specs/18 §3.2.1)

    assert client.get(f"/api/demos/d1/db/datasets/{ds_id}/preview").status_code == 200
    assert client.get(f"/api/demos/d2/db/datasets/{ds_id}/preview").status_code == 404

    assert client.delete(f"/api/demos/d1/db/datasets/{ds_id}").json() == {"deleted": True}
    assert client.get("/api/demos/d1/db/datasets").json()["datasets"] == []


def test_demo_datasets_upload_validation_same_as_user_route(fake_ds):
    res = client.post("/api/demos/d1/db/datasets",
                      files={"file": ("a.txt", b"x", "text/plain")})
    assert res.status_code == 422  # CSV のみ
    res = client.post("/api/demos/d1/db/datasets",
                      files={"file": ("a.csv", b"", "text/csv")})
    assert res.status_code == 422  # 空ファイル


def test_public_demo_non_owner_reads_200_writes_404(fake_ds, monkeypatch):
    """public デモの非所有者: 読み取り系は demo namespace で 200、書き込み系は同一形 404
    (require_demo/require_demo_owner 契約の回帰検出 — specs/18 §4.3)。"""
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai",
                        lambda q, profile_name=None, model=None: "SELECT 1 FROM dual")
    monkeypatch.setattr(
        service_main.nl2sql, "execute_readonly",
        lambda sql, owner_key: {"columns": ["C"], "rows": [], "row_count": 0,
                                "truncated": False})
    fake_ds.create_dataset("demo_pub", "公開データ", b"")
    ds_id = fake_ds.boxes["demo_pub"][0]["id"]

    # 読み取り = require_demo → 200
    assert client.post("/api/demos/pub/dbchat/nl2sql",
                       json={"question": "q"}).status_code == 200
    assert client.post("/api/demos/pub/dbchat/execute",
                       json={"sql": "SELECT 1 FROM dual"}).status_code == 200
    assert client.get("/api/demos/pub/dbchat/schema").status_code == 200
    assert client.get("/api/demos/pub/db/datasets").status_code == 200
    assert client.get(f"/api/demos/pub/db/datasets/{ds_id}/preview").status_code == 200

    # 書き込み = require_demo_owner → 存在秘匿と同一形の 404
    assert client.post("/api/demos/pub/db/datasets",
                       files={"file": ("a.csv", b"a\n1", "text/csv")}).status_code == 404
    assert client.post("/api/demos/pub/db/datasets/generate",
                       json={"description": "d"}).status_code == 404
    assert client.delete(f"/api/demos/pub/db/datasets/{ds_id}").status_code == 404


def test_cross_user_demo_404_for_all_dbchat_routes(fake_ds):
    assert client.post("/api/demos/theirs/dbchat/execute",
                       json={"sql": "SELECT 1 FROM dual"}).status_code == 404
    assert client.get("/api/demos/theirs/dbchat/schema").status_code == 404
    assert client.get("/api/demos/theirs/db/datasets").status_code == 404
    assert client.post("/api/demos/theirs/db/datasets",
                       files={"file": ("a.csv", b"a\n1", "text/csv")}).status_code == 404


def test_demo_generate_dataset_model_from_config(fake_ds, monkeypatch):
    """generate も config モデル固定(リクエストの model は無視)。"""
    captured = {}

    def fake_generate(owner, description, display_name=None, rows=30, model=None,
                      lease=None):
        captured.update(owner=owner, model=model, lease=lease)
        return {"id": "x", "table_name": "T", "display_name": "d", "columns": [],
                "row_count": 0, "ready": True}

    monkeypatch.setattr(service_main.datasets, "generate_dataset", fake_generate)
    monkeypatch.setitem(DEMOS, "mypub",
                        {"id": "mypub", "owner_sub": "dev-user", "name": "p",
                         "visibility": "public", "status": "ready",
                         "config": {"dbchat": {"model": "cohere.command-a-03-2025"}}})
    res = client.post("/api/demos/mypub/db/datasets/generate",
                      json={"description": "テスト", "model": "meta.llama-3.3-70b-instruct"})
    assert res.status_code == 200
    assert captured["owner"] == "demo_mypub"
    assert captured["model"] == "cohere.command-a-03-2025"
    assert captured["lease"].demo_id == "mypub"
