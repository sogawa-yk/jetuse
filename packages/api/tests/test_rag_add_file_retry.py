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
                    raise _not_found()

    monkeypatch.setattr(rag, "ensure_store", lambda owner: "vs_x")
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: FakeDp)
    monkeypatch.setattr(rag, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag.time, "sleep", lambda s: None)
    try:
        rag.add_file("ns", "a.md", b"x")
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass
    assert calls["n"] == 6  # 有界(6回)で諦める
