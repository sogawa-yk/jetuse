"""rag.add_file の DP 伝播404リトライ(SPIKE-03 / SP1-03 REV-005)。

test_rag.py は autouse fixture が rag.add_file 自体を fake に差し替えるため、
実関数を検証する本テストは別モジュールに置く。
"""

from types import SimpleNamespace

import httpx
from openai import NotFoundError

from jetuse_core import rag


def _not_found() -> NotFoundError:
    req = httpx.Request("POST", "http://x")
    return NotFoundError(
        "not found", response=httpx.Response(404, request=req), body=None
    )


def test_add_file_retries_dp_propagation_404(monkeypatch):
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

    monkeypatch.setattr(rag, "ensure_store", lambda owner: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(rag, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    out = rag.add_file("ns", "a.md", b"x")
    assert out["status"] == "processing"
    assert calls["n"] == 3  # 2回404 → 3回目成功


def test_add_file_gives_up_after_bounded_retries(monkeypatch):
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

    monkeypatch.setattr(rag, "ensure_store", lambda owner: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(rag, "_backup_original", lambda *a: None)
    monkeypatch.setattr(
        rag, "_delete_original", lambda *a: cleaned.update(original=True)
    )
    monkeypatch.setattr(rag, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    try:
        rag.add_file("ns", "a.md", b"x")
        raise AssertionError("expected StoreNotReadyError")
    except rag.StoreNotReadyError:
        pass
    assert calls["n"] == 6  # 有界(6回)で諦める
    # REV-007: 枯渇時はDB行が無く辿れない孤立物(OCI File/原本)を即後始末する
    assert cleaned == {"file": "file-x", "original": True}


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
