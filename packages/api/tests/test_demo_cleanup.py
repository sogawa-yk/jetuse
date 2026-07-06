"""DELETE 後始末オーケストレーションの単体(specs/18 §3.2 — DB/GenAI 層は fake)。

順序・段階付き 503・再 DELETE 収束・NotFound 成功扱い・未登録残骸の実在ベース回収・
台帳が正(設定不一致 503)・versioning ガード・同時 2 本の DELETE・usage_log 保持(会話層は
delete_demo_conversations のみ呼ばれ usage_log に触れない)を検証する。
"""

import contextlib
import threading
from types import SimpleNamespace

import pytest

from jetuse_core import demo_cleanup
from jetuse_core.demo_lease import DemoLease
from jetuse_core.owner_keys import owner_hash

D1 = "aaaaaaaa-0000-4000-8000-000000000001"
NS = f"demo_{D1}"
TAG = owner_hash(NS)


class World:
    """後始末対象の外部世界(fake)。呼び出し順を order に記録する。"""

    def __init__(self):
        self.order: list[str] = []
        self.demo = {"id": D1, "owner_sub": "dev-user", "status": "ready",
                     "visibility": "private"}
        self.rag_rows = [{"id": "f1", "oci_file_id": "ocif-1", "filename": "a.md"}]
        self.store_id = "vs_registered"
        self.orphan_stores = [SimpleNamespace(id="vs_orphan", status="completed",
                                              created_at=1, metadata={"owner": TAG})]
        self.ledger_rows = [{"id": "r1", "filename": "a.md", "ext": "md",
                             "external_file_id": None, "state": "pending",
                             "locator": {}}]
        self.external_files = [{"id": "ocif-9", "filename": f"{TAG}/r9.md"},
                               {"id": "ocif-other", "filename": "elsewhere/x.md"}]
        self.targets: list[dict] = [
            {"kind": "objectstorage",
             "locator": {"region": "r1", "os_namespace": "n", "bucket": "b"}},
        ]
        self.originals = [f"rag/{TAG}/r1.md"]
        self.deleted = {"stores": [], "files": [], "objects": [], "ledger": [],
                        "rag_rows": [], "targets": 0}
        self.versioning = "Disabled"
        self.fail_store_delete = False


@pytest.fixture()
def world(monkeypatch):
    w = World()

    # --- lease(実 DB なし。直列化は test_concurrent で threading.Lock 代替) ---
    @contextlib.contextmanager
    def fake_acquire(demo_id, **kw):
        w.order.append("lease-acquire")
        yield DemoLease(demo_id=demo_id, _conn=None)
        w.order.append("lease-release")

    monkeypatch.setattr(demo_cleanup.demo_lease, "acquire", fake_acquire)
    monkeypatch.setattr(demo_cleanup, "owner_key_gate", lambda: None)

    # --- demos ---
    monkeypatch.setattr(demo_cleanup.demos, "get_demo",
                        lambda i: dict(w.demo) if w.demo else None)

    def set_status(demo_id, frm, to):
        w.order.append(f"status:{frm}->{to}")
        w.demo["status"] = to
        return True

    monkeypatch.setattr(demo_cleanup.demos, "set_status", set_status)

    def delete_row(owner, demo_id):
        w.order.append("demos-row-delete")
        w.demo = None
        return True

    monkeypatch.setattr(demo_cleanup.demos, "delete_demo", delete_row)

    # --- datasets / conversations ---
    monkeypatch.setattr(demo_cleanup.datasets, "delete_owner",
                        lambda ns: w.order.append("datasets"))
    monkeypatch.setattr(demo_cleanup.conversations, "delete_demo_conversations",
                        lambda demo_id: w.order.append("conversations") or
                        {"messages": 0, "conversations": 0})

    # --- rag 層 ---
    monkeypatch.setattr(demo_cleanup.rag, "get_store_id",
                        lambda ns: w.store_id)
    monkeypatch.setattr(demo_cleanup.rag, "list_files",
                        lambda ns: [dict(r) for r in w.rag_rows])
    monkeypatch.setattr(demo_cleanup.rag, "delete_external_file",
                        lambda fid, dp=None: w.deleted["files"].append(fid))
    monkeypatch.setattr(demo_cleanup.rag, "find_orphan_stores",
                        lambda ns, cp=None: list(w.orphan_stores))
    monkeypatch.setattr(demo_cleanup.rag, "list_all_external_files",
                        lambda dp=None, **kw: list(w.external_files))
    monkeypatch.setattr(
        demo_cleanup.rag, "delete_original_exact",
        lambda ns, rid, ext, locator=None: w.deleted["objects"].append(f"{rid}.{ext}"))
    monkeypatch.setattr(demo_cleanup.rag, "bucket_versioning",
                        lambda loc=None: w.versioning)
    monkeypatch.setattr(demo_cleanup.rag, "list_original_objects",
                        lambda ns, loc=None, **kw: list(w.originals))
    monkeypatch.setattr(demo_cleanup.rag, "delete_objects",
                        lambda names, loc=None: w.deleted["objects"].extend(names))

    # --- ledger ---
    monkeypatch.setattr(demo_cleanup.rag_ledger, "rows_for_owner",
                        lambda ns: [dict(r) for r in w.ledger_rows
                                    if r["id"] not in w.deleted["ledger"]])

    def release(rid):
        w.deleted["ledger"].append(rid)

    monkeypatch.setattr(demo_cleanup.rag_ledger, "release", release)

    # --- 台帳 ---
    monkeypatch.setattr(demo_cleanup.demo_targets, "targets_for",
                        lambda ns, kind=None: [t for t in w.targets
                                               if kind is None or t["kind"] == kind])

    def delete_targets(ns):
        w.order.append("targets-delete")
        w.deleted["targets"] += 1
        return 1

    monkeypatch.setattr(demo_cleanup.demo_targets, "delete_targets", delete_targets)

    # --- GenAI クライアント(store 削除)。削除は orphan 一覧にも反映(事後確認が通る) ---
    class FakeCp:
        class vector_stores:
            @staticmethod
            def delete(vector_store_id):
                if w.fail_store_delete:
                    raise RuntimeError("cp 500")
                w.deleted["stores"].append(vector_store_id)
                w.orphan_stores = [vs for vs in w.orphan_stores
                                   if vs.id != vector_store_id]
                if vector_store_id == w.store_id:
                    w.store_id = None

    class FakeDp:
        class vector_stores:
            class files:
                @staticmethod
                def delete(vector_store_id, file_id):
                    w.deleted.setdefault("detached", []).append(file_id)

    monkeypatch.setattr(demo_cleanup, "make_cp_client", lambda: FakeCp)
    monkeypatch.setattr(demo_cleanup, "make_inference_client", lambda **kw: FakeDp)
    # step_files は行ごとの ledger locator で client を構成する(B002)。fake もそれに合わせる。
    monkeypatch.setattr(demo_cleanup.rag_ledger, "rows_for_owner_by_id",
                        lambda rid: {"id": rid, "locator": None})
    monkeypatch.setattr(demo_cleanup.rag, "_dp_for", lambda loc=None: FakeDp)

    # --- rag_files 行削除(connect 経由) ---
    class Cur:
        def execute(self, sql, **binds):
            if "DELETE FROM rag_files" in sql:
                w.deleted["rag_rows"].append(binds["id"])
                w.rag_rows = [r for r in w.rag_rows if r["id"] != binds["id"]]
            elif "DELETE FROM rag_stores" in sql:
                w.order.append("rag-stores-row-delete")
                w.store_id = None

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    monkeypatch.setattr(demo_cleanup, "connect",
                        lambda: contextlib.nullcontext(Conn()))
    # select_ai / opensearch
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa

    monkeypatch.setattr(rsa, "delete_owner",
                        lambda ns: w.order.append("select-ai"))
    monkeypatch.setattr(ros, "delete_owner",
                        lambda ns, endpoint=None: w.order.append(f"opensearch:{endpoint}"))
    return w


def test_happy_path_order_and_convergence(world):
    out = demo_cleanup.delete_demo_box(D1, "dev-user")
    assert out == {"deleted": True}
    # specs/18 §3.2 の順序: リース → deleting 遷移 → DB 箱 → RAG 箱 → 会話 → demos 行 → 解放
    assert world.order == [
        "lease-acquire", "status:ready->deleting", "datasets",
        "select-ai",  # 3d(3a-3c は order 非記録の削除操作)
        "rag-stores-row-delete", "targets-delete",  # 3 完了後に行を消す
        "conversations", "demos-row-delete", "lease-release",
    ]
    # 登録 store と未登録孤児 store の両方が消えている(実在ベース回収)
    assert set(world.deleted["stores"]) == {"vs_registered", "vs_orphan"}
    # rag_files 行の DP File・ledger 全行・prefix 孤児 File・原本が消えている
    assert "ocif-1" in world.deleted["files"]      # 3a
    assert "ocif-9" in world.deleted["files"]      # 3c(接頭辞一致の孤児)
    assert "ocif-other" not in world.deleted["files"]  # 他所の File は触らない
    # 3a は行ごとに対応 ledger を冪等解放、3c は残り全行(pending 含む)を解放
    assert world.deleted["ledger"] == ["f1", "r1"]  # 事後条件: owner の ledger 行ゼロ
    assert f"rag/{TAG}/r1.md" in world.deleted["objects"]  # 3f
    # usage_log は会話層で触れない(delete_demo_conversations の契約 — 別テスト)


def test_nonowner_and_missing_are_same_404(world):
    with pytest.raises(demo_cleanup.DemoNotFoundError):
        demo_cleanup.delete_demo_box(D1, "someone-else")
    world.demo = None
    with pytest.raises(demo_cleanup.DemoNotFoundError):
        demo_cleanup.delete_demo_box(D1, "dev-user")


def test_store_delete_failure_is_staged_503_then_retry_converges(world):
    """store 削除失敗は中断(503)。demo 行と台帳を保持し、再 DELETE で収束する。"""
    world.fail_store_delete = True
    with pytest.raises(demo_cleanup.CleanupError) as ei:
        demo_cleanup.delete_demo_box(D1, "dev-user")
    assert ei.value.stage == "rag-store"
    assert world.demo is not None            # demos 行は残る
    assert world.demo["status"] == "deleting"  # 残骸は deleting のまま
    assert world.deleted["targets"] == 0     # 台帳行は保持(旧 locator 参照可)
    assert "rag-stores-row-delete" not in world.order

    # 再 DELETE(deleting 状態でも受理)→ 完走
    world.fail_store_delete = False
    out = demo_cleanup.delete_demo_box(D1, "dev-user")
    assert out == {"deleted": True}
    assert world.demo is None
    assert world.deleted["targets"] == 1


def test_opensearch_target_without_config_is_503(world):
    """台帳が正: opensearch 行があれば endpoint 欠落は 503(スキップしない)。"""
    world.targets.append({"kind": "opensearch", "locator": {}})
    with pytest.raises(demo_cleanup.CleanupError) as ei:
        demo_cleanup.delete_demo_box(D1, "dev-user")
    assert ei.value.stage == "rag-opensearch"


def test_opensearch_target_uses_recorded_endpoint(world):
    world.targets.append({"kind": "opensearch",
                          "locator": {"endpoint": "https://old-os:9200"}})
    demo_cleanup.delete_demo_box(D1, "dev-user")
    assert "opensearch:https://old-os:9200" in world.order


def test_no_targets_means_skip_is_allowed(world):
    """台帳に行が無いバックエンドだけスキップしてよい(specs/18 §3.2)。"""
    world.targets = []
    out = demo_cleanup.delete_demo_box(D1, "dev-user")
    assert out == {"deleted": True}
    assert not any(o.startswith("opensearch") for o in world.order)


@pytest.mark.parametrize("versioning", ["Enabled", "Suspended"])
def test_bucket_versioning_not_disabled_is_503(world, versioning):
    """M004: Suspended でも既存 version が残る → Disabled 以外は 503。"""
    world.versioning = versioning
    with pytest.raises(demo_cleanup.CleanupError) as ei:
        demo_cleanup.delete_demo_box(D1, "dev-user")
    assert ei.value.stage == "rag-originals"


def test_concurrent_deletes_serialize_second_gets_404(world, monkeypatch):
    """同時 2 本の DELETE: リースで直列化、後着は行なし(先着成功)を見て 404。"""
    gate = threading.Lock()

    @contextlib.contextmanager
    def locking_acquire(demo_id, **kw):
        with gate:  # 排他リースの直列化を実プロセス内 Lock で再現
            yield DemoLease(demo_id=demo_id, _conn=None)

    monkeypatch.setattr(demo_cleanup.demo_lease, "acquire", locking_acquire)
    results = {}

    def run(name):
        try:
            results[name] = demo_cleanup.delete_demo_box(D1, "dev-user")
        except demo_cleanup.DemoNotFoundError:
            results[name] = "404"

    t1 = threading.Thread(target=run, args=("a",))
    t2 = threading.Thread(target=run, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(str(v) for v in results.values()) == ["404", "{'deleted': True}"]


def test_dp_clients_prefers_saved_locator_over_current(monkeypatch):
    """B002: 台帳 kind='files' の locator を正とし、記録があれば現在設定は使わない
    (構成ドリフトで現在設定が空/無効でも旧 File に到達する)。記録が無い legacy だけ現在設定。"""
    current = {"n": 0}
    made = []
    monkeypatch.setattr(demo_cleanup, "make_inference_client",
                        lambda **kw: current.__setitem__("n", current["n"] + 1) or "CURRENT")
    monkeypatch.setattr(demo_cleanup, "make_inference_client_for",
                        lambda r, c, p: made.append((r, c, p)) or f"FOR:{r}")

    monkeypatch.setattr(
        demo_cleanup.demo_targets, "targets_for",
        lambda ns, kind=None: (
            [{"kind": "files", "locator": {"region": "r2", "compartment": "c2",
                                           "project": "p2"}}] if kind == "files" else []))
    assert demo_cleanup._dp_clients("demo_x") == ["FOR:r2"]
    assert current["n"] == 0  # 現在設定へフォールバックしない

    monkeypatch.setattr(demo_cleanup.demo_targets, "targets_for",
                        lambda ns, kind=None: [])
    assert demo_cleanup._dp_clients("demo_x") == ["CURRENT"]  # 記録なし=legacy フォールバック
    assert current["n"] == 1


def test_cp_clients_prefers_saved_locator_over_current(monkeypatch):
    """B002: store 側も同様に台帳を正とする。"""
    current = {"n": 0}
    monkeypatch.setattr(demo_cleanup, "make_cp_client",
                        lambda: current.__setitem__("n", current["n"] + 1) or "CURRENT")
    monkeypatch.setattr(demo_cleanup, "make_cp_client_for",
                        lambda r, c: f"FOR:{r}")
    monkeypatch.setattr(
        demo_cleanup.demo_targets, "targets_for",
        lambda ns, kind=None: (
            [{"kind": "vector_store", "locator": {"region": "r2", "compartment": "c2"}}]
            if kind == "vector_store" else []))
    assert demo_cleanup._cp_clients("demo_x") == ["FOR:r2"]
    assert current["n"] == 0
