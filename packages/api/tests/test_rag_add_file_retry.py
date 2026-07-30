"""rag.add_file の DP 伝播404リトライ(SPIKE-03 / SP1-03 REV-005)と
ensure_store の競合/孤児採用(SP2-02 — specs/18 §3.2)。

test_rag.py は autouse fixture が rag.add_file 自体を fake に差し替えるため、
実関数を検証する本テストは別モジュールに置く。
"""

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from openai import NotFoundError

from jetuse_core import rag, rag_metadata


def _not_found() -> NotFoundError:
    req = httpx.Request("POST", "http://x")
    return NotFoundError(
        "not found", response=httpx.Response(404, request=req), body=None
    )


class FakeLedger:
    """rag_ledger の呼び出し面だけを再現(予約/確定/解放の記録)。"""

    def __init__(self):
        self.reserved: list[tuple] = []
        self.released: list[str] = []
        self.external: dict[str, str] = {}
        self.confirmed: list[str] = []
        self.rid = "00000000-0000-4000-8000-000000000001"

    def upload_gate(self):
        pass

    def reserve(self, owner_key, filename, ext):
        self.reserved.append((owner_key, filename, ext))
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
    captured: dict = {}

    class FakeDp:
        class files:
            @staticmethod
            def create(file, purpose):
                return SimpleNamespace(id="file-x")

        class vector_stores:
            class files:
                @staticmethod
                def create(vector_store_id, file_id, attributes=None):
                    calls["n"] += 1
                    captured["attributes"] = attributes
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
                def create(vector_store_id, file_id, attributes=None):
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
    # SYNC-01: 締めの Tx で rag_adb.delete_chunks(RAGM-02)が同じ cursor を使う。
    # この単体は OpenSearch の endpoint 解決が主題なので、チャンク表は「無い」= 0 を返して
    # 消すものが無い扱いにする(チャンク削除の原子性は test_rag_adb.py が担保)。
    last = ""

    def execute(self, sql="", **kw): self.last = sql

    def fetchone(self):
        if "FROM user_tables" in self.last:
            return (0,)
        return ("file-x", "doc.md")  # oci_file_id, filename


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
    monkeypatch.setattr(rag, "rag_ledger", type("L", (), {
        "rows_for_owner_by_id": staticmethod(lambda fid: {
            "id": fid, "ext": "md", "external_file_id": "file-x",
            "locator": {"opensearch_endpoint": "http://saved:9200"}}),
    })())
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

    monkeypatch.setattr(rag, "connect", lambda: boom("ORA-02291"))
    try:
        rag._save_store_id("o", "v")
        raise AssertionError("expected IntegrityError")
    except oracledb.IntegrityError:
        pass


# --- RAGM-01: 取り込み時のメタデータ属性(SP2-02 の予約 ledger 経路に載せた形) ---


def test_add_file_passes_attributes_and_autofills_file_and_sha256(monkeypatch, ledger):
    """ADR-0020 §1: vector_stores.files.create に属性を付ける。
    file/sha256 は未指定なら補い、呼び出し側の明示値が優先する。

    SP2-02 で外部 filename は不透明キー(file_key)になったため、原名が外部に残るのは
    この `file` 属性だけ。ここが落ちると出典表示が不透明キーになる。"""
    captured: dict = {}

    class FakeDp:
        class files:
            @staticmethod
            def create(file, purpose):
                captured["external_filename"] = file[0]
                return SimpleNamespace(id="file-x")

        class vector_stores:
            class files:
                @staticmethod
                def create(vector_store_id, file_id, attributes=None):
                    captured["attributes"] = attributes

    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(rag, "_insert_file_confirmed", lambda *a: None)

    rag.add_file("ns", "spec.md", b"body", {
        "version": "2.0", "sheet": "API一覧", "cells": "B12:F12",
        "current_version": "Y", "kind": "",  # 空値はキーごと落ちる
    })
    attrs = captured["attributes"]
    assert attrs["file"] == "spec.md"
    assert attrs["sha256"] == hashlib.sha256(b"body").hexdigest()
    assert attrs["cells"] == "B12:F12" and attrs["current_version"] == "Y"
    assert "kind" not in attrs
    # 外部 filename は原名でない(SP2-02)。原名は attributes["file"] にだけ残る
    assert captured["external_filename"] != "spec.md"

    rag.add_file("ns", "spec.md", b"body", {"file": "元の仕様書.xlsx"})
    assert captured["attributes"]["file"] == "元の仕様書.xlsx"


def test_add_file_rejects_bad_attributes_before_calling_oci(monkeypatch, ledger):
    """検証は OCI 呼び出しより前。未知キーで Files API を汚さない。

    統合後は**予約(reserve)よりも前**であること = 不正な属性で 422 になるときに
    箱のファイル数枠を消費しない(SP2-02 の quota gate と両立させるための順序)。"""
    called = {"n": 0}

    class FakeDp:
        class files:
            @staticmethod
            def create(file, purpose):
                called["n"] += 1
                return SimpleNamespace(id="file-x")

    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)

    for bad in ({"versoin": "2.0"}, {"cells": "x" * 513}, {"sheet": {"a": 1}}):
        try:
            rag.add_file("ns", "spec.md", b"body", bad)
            raise AssertionError("expected MetadataError")
        except rag_metadata.MetadataError:
            pass
    assert called["n"] == 0
    assert ledger.reserved == []  # 予約も消費しない(枠が漏れない)


# --- SYNC-01 review-1 F-001: 原本の拡張子は台帳(ledger)の ext が正 ---


def test_delete_uses_ledger_ext_not_truncated_filename(monkeypatch):
    """400 文字のマルチバイト名で、原本キーの拡張子が upload 時と一致すること。

    `_fit()` は 400 **バイト**で切るので、ルートが受理する 400 **文字**の日本語名では
    拡張子まで落ちる(`あ*397 + .md` → 133 文字・拡張子なし)。切り詰め後の
    `rag_files.filename` から ext を導出すると upload は `.md`・delete は `.bin` になり、
    原本を消し残したまま台帳行だけ消えて「削除成功」を返す(追跡不能な残存)。
    ext は予約時に ledger へ記録済み(`rag_ledger.reconcile` も ext を正本として使う)。
    """
    from jetuse_core import rag_opensearch, rag_select_ai

    name = "あ" * 397 + ".md"
    assert len(name) <= rag.MAX_FILENAME_CHARS  # ルートは受理する
    assert rag.normalize_ext(rag._fit(name)) != "md"  # 切り詰め後からは導けない

    stored = rag._fit(name)  # 実際に rag_files.filename へ入る値

    class _Cur:
        last = ""

        def execute(self, sql="", **kw):
            self.last = sql

        def fetchone(self):
            if "FROM user_tables" in self.last:
                return (0,)
            return ("file-x", stored)  # 台帳が持つのは**切り詰め後**の名前

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen: dict = {}
    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag, "connect", lambda: _Conn())
    monkeypatch.setattr(rag, "rag_ledger", type("L", (), {
        "rows_for_owner_by_id": staticmethod(
            lambda fid: {"id": fid, "ext": "md", "external_file_id": "file-x",
                         "locator": None}),
    })())
    monkeypatch.setattr(rag, "get_store_id", lambda owner: None)
    monkeypatch.setattr(rag, "_dp_for", lambda loc=None: SimpleNamespace())
    monkeypatch.setattr(rag, "delete_external_file", lambda oid, dp=None: None)
    monkeypatch.setattr(rag, "delete_original_exact",
                        lambda o, rid, ext, locator=None: seen.update(ext=ext))
    monkeypatch.setattr(rag, "_delete_original_legacy", lambda *a, **kw: None)
    monkeypatch.setattr(rag_opensearch, "enabled", lambda: False)
    monkeypatch.setattr(rag_select_ai, "sync_remove_file", lambda o, f: None)

    assert rag.delete_file("ns", "f1") is True
    assert seen["ext"] == "md"  # upload が put したキーと一致する
