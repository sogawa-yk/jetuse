"""rag.add_file の DP 伝播404リトライ(SPIKE-03 / SP1-03 REV-005)と
ensure_store の競合/孤児採用(SP2-02 — specs/18 §3.2)。

test_rag.py は autouse fixture が rag.add_file 自体を fake に差し替えるため、
実関数を検証する本テストは別モジュールに置く。
"""

from types import SimpleNamespace

import httpx
import pytest
from openai import NotFoundError

from jetuse_core import rag


def _not_found() -> NotFoundError:
    req = httpx.Request("POST", "http://x")
    return NotFoundError(
        "not found", response=httpx.Response(404, request=req), body=None
    )


class FakeLedger:
    """rag_ledger の呼び出し面だけを再現(予約/確定/解放の記録)。"""

    def __init__(self):
        self.released: list[str] = []
        self.external: dict[str, str] = {}
        self.confirmed: list[str] = []
        self.rid = "00000000-0000-4000-8000-000000000001"

    def upload_gate(self):
        pass

    def reserve(self, owner_key, filename, ext):
        return self.rid

    def set_external(self, rid, ext_id):
        self.external[rid] = ext_id

    def release(self, rid):
        self.released.append(rid)

    def confirm_in_tx(self, cur, rid):
        self.confirmed.append(rid)


@pytest.fixture()
def ledger(monkeypatch):
    fake = FakeLedger()
    monkeypatch.setattr(rag, "rag_ledger", fake)
    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag, "_put_original", lambda *a, **kw: None)
    return fake


def test_add_file_retries_dp_propagation_404(monkeypatch, ledger):
    calls = {"n": 0}

    class FakeDp:
        class files:
            @staticmethod
            def create(file, purpose):
                return SimpleNamespace(id="file-x")

        class vector_stores:
            class files:
                @staticmethod
                def create(vector_store_id, file_id):
                    calls["n"] += 1
                    if calls["n"] < 3:
                        raise _not_found()

    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    inserted = {}
    monkeypatch.setattr(rag, "_insert_file_confirmed",
                        lambda *a: inserted.update(args=a))
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    out = rag.add_file("ns", "a.md", b"x")
    assert out["status"] == "processing"
    assert out["id"] == ledger.rid  # rag_files.id = reservation_id(specs/18 §3.1)
    assert calls["n"] == 3  # 2回404 → 3回目成功
    # 外部 filename は file_key 導出(<sha1(owner)>/<rid>.<ext>)
    assert ledger.external[ledger.rid] == "file-x"


def test_add_file_gives_up_after_bounded_retries(monkeypatch, ledger):
    calls = {"n": 0}
    cleaned = {"file": None, "original": False}

    class FakeDp:
        class files:
            @staticmethod
            def create(file, purpose):
                return SimpleNamespace(id="file-x")

            @staticmethod
            def delete(file_id):
                cleaned["file"] = file_id

        class vector_stores:
            class files:
                @staticmethod
                def create(vector_store_id, file_id):
                    calls["n"] += 1
                    raise _not_found()

    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(
        rag, "delete_original_exact",
        lambda *a, **kw: cleaned.update(original=True),
    )
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    try:
        rag.add_file("ns", "a.md", b"x")
        raise AssertionError("expected StoreNotReadyError")
    except rag.StoreNotReadyError:
        pass
    assert calls["n"] == 6  # 有界(6回)で諦める
    # REV-007: 枯渇時はDB行が無く辿れない孤立物(OCI File/原本/予約)を即後始末する
    assert cleaned == {"file": "file-x", "original": True}
    assert ledger.released == [ledger.rid]  # 予約解放(枠が漏れない)


def test_add_file_put_failure_releases_reservation(monkeypatch, ledger):
    """原本 put 失敗で upload を成功にしない。exact 削除が確定できたときだけ予約を解放する
    (specs/18 §3.1 / B003)。"""
    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "_put_original",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("os down")))
    cleaned = {"n": 0}
    monkeypatch.setattr(rag, "delete_original_exact",
                        lambda *a, **kw: cleaned.update(n=cleaned["n"] + 1))
    try:
        rag.add_file("ns", "a.md", b"x")
        raise AssertionError("expected StoreNotReadyError")
    except rag.StoreNotReadyError:
        pass
    assert cleaned["n"] == 1  # 曖昧成功に備え exact 削除を試みた
    assert ledger.released == [ledger.rid]


def test_add_file_put_failure_keeps_reservation_when_cleanup_uncertain(monkeypatch, ledger):
    """put が ambiguous success(サーバ保存後に応答失敗)で exact 削除も不確定なら、予約を残して
    reconcile に委ねる = 原本だけ残して台帳から辿れなくしない(fail-closed / B003)。"""
    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "_put_original",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("timeout after save")))
    monkeypatch.setattr(rag, "delete_original_exact",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("os 503")))
    try:
        rag.add_file("ns", "a.md", b"x")
        raise AssertionError("expected UnmanagedFilesError")
    except rag.UnmanagedFilesError:
        pass
    assert ledger.released == []  # 予約は残す(枠は返さない)


_EMPTY_PAGE = SimpleNamespace(data=[], has_more=False)


def test_ensure_store_lost_race_uses_winner(monkeypatch):
    """REV-008: 同時作成でINSERTに負けたら勝者のstoreを使い、自分の箱は削除する"""
    ids = iter([None, "vs_winner"])
    deleted = []

    class FakeCp:
        class vector_stores:
            @staticmethod
            def create(name, metadata):
                return SimpleNamespace(id="vs_mine")

            @staticmethod
            def retrieve(vector_store_id):
                return SimpleNamespace(status="completed")

            @staticmethod
            def delete(vector_store_id):
                deleted.append(vector_store_id)

            @staticmethod
            def list(**kw):
                return _EMPTY_PAGE

    class FakeDp:
        class vector_stores:
            class files:
                @staticmethod
                def list(vector_store_id):
                    return []

    monkeypatch.setattr(rag, "get_store_id", lambda owner: next(ids))
    monkeypatch.setattr(rag, "make_cp_client", lambda: FakeCp)
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(rag, "_save_store_id", lambda o, v: False)
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    assert rag.ensure_store("ns") == "vs_winner"
    assert deleted == ["vs_mine"]

    # 競合したのに勝者行が無い(想定外)は、未登録IDを返さず503相当の例外
    ids2 = iter([None, None])
    deleted.clear()
    monkeypatch.setattr(rag, "get_store_id", lambda owner: next(ids2))
    try:
        rag.ensure_store("ns")
        raise AssertionError("expected StoreNotReadyError")
    except rag.StoreNotReadyError:
        pass
    assert deleted == ["vs_mine"]


def test_ensure_store_adopts_oldest_usable_orphan(monkeypatch):
    """孤児採用(specs/18 §3.2): metadata.owner=sha1(owner) の未登録 store を採用。
    最古の usable(completed)を正本とし、failed 含む余剰は削除する(SP2-00 M005)。"""
    from jetuse_core.owner_keys import owner_hash

    tag = owner_hash("ns")
    orphans = [
        SimpleNamespace(id="vs_new", status="completed", created_at=200,
                        metadata={"owner": tag}),
        SimpleNamespace(id="vs_old", status="completed", created_at=100,
                        metadata={"owner": tag}),
        SimpleNamespace(id="vs_failed", status="failed", created_at=50,
                        metadata={"owner": tag}),
        SimpleNamespace(id="vs_other", status="completed", created_at=10,
                        metadata={"owner": "someone-else"}),
    ]
    deleted = []

    class FakeCp:
        class vector_stores:
            @staticmethod
            def list(**kw):
                return SimpleNamespace(data=orphans, has_more=False)

            @staticmethod
            def delete(vector_store_id):
                deleted.append(vector_store_id)

    saved = {}
    monkeypatch.setattr(rag, "get_store_id", lambda owner: None)
    monkeypatch.setattr(rag, "make_cp_client", lambda: FakeCp)
    monkeypatch.setattr(rag, "_save_store_id",
                        lambda o, v: saved.update({o: v}) or True)
    assert rag.ensure_store("ns") == "vs_old"  # 最古の completed
    assert saved == {"ns": "vs_old"}
    assert sorted(deleted) == ["vs_failed", "vs_new"]  # 他人の store は触らない


class _DCur:
    def execute(self, *a, **kw): pass
    def fetchone(self): return ("file-x", "doc.md")  # oci_file_id, filename


class _DConn:
    def cursor(self): return _DCur()
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_delete_file_uses_saved_opensearch_endpoint(monkeypatch):
    """B004: 個別 DELETE は台帳 locator の保存 endpoint で OpenSearch を消す。取り込み後に
    endpoint を無効化(enabled()=False)しても、保存 endpoint で削除を試みる(旧チャンクを
    検索可能なまま残さない)。"""
    from jetuse_core import rag_opensearch

    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag, "connect", lambda: _DConn())
    monkeypatch.setattr(rag, "_ledger_locator",
                        lambda fid: {"opensearch_endpoint": "http://saved:9200"})
    monkeypatch.setattr(rag, "get_store_id", lambda owner: None)
    monkeypatch.setattr(rag, "_dp_for", lambda loc: SimpleNamespace())
    monkeypatch.setattr(rag, "delete_external_file", lambda oci_id, dp: None)
    monkeypatch.setattr(rag, "delete_original_exact", lambda *a, **kw: None)
    monkeypatch.setattr(rag, "_delete_original_legacy", lambda *a, **kw: None)
    monkeypatch.setattr(rag_opensearch, "enabled", lambda: False)  # 現在は無効
    captured = {}
    monkeypatch.setattr(rag_opensearch, "delete_file",
                        lambda owner, fid, endpoint=None: captured.update(ep=endpoint))
    from jetuse_core import rag_select_ai
    monkeypatch.setattr(rag_select_ai, "sync_remove_file", lambda *a, **kw: None)

    assert rag.delete_file("ns", "f1") is True
    assert captured["ep"] == "http://saved:9200"  # 保存 endpoint で削除(現在設定に依らない)


def test_save_store_id_conflict_only_on_unique_violation(monkeypatch):
    """ORA-00001だけ競合扱い(False)。他のIntegrityErrorは再送出する。"""
    import oracledb

    def boom(full_code):
        class Ctx:
            def __enter__(self):
                raise oracledb.IntegrityError(SimpleNamespace(full_code=full_code))

            def __exit__(self, *a):
                return False

        return Ctx()

    monkeypatch.setattr(rag, "connect", lambda: boom("ORA-00001"))
    assert rag._save_store_id("o", "v") is False
