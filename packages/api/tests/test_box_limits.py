"""箱あたり上限(422)・アプリ全体 quota(422)・外部先行 delete の 503(specs/18 §3.1・§3.2)。"""

import contextlib

import pytest
from fastapi.testclient import TestClient

import service.demo_context as demo_context
from jetuse_core import demo_lease, rag, rag_ledger
from service.main import app

client = TestClient(app)

DEMOS = {"d1": {"id": "d1", "owner_sub": "dev-user", "name": "m",
                "visibility": "private", "status": "ready"}}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)

    @contextlib.contextmanager
    def fake_mutation(demo_id, **kw):
        yield demo_lease.DemoLease(demo_id=demo_id, _conn=None)

    monkeypatch.setattr(demo_lease, "mutation", fake_mutation)


def test_demo_box_file_limit_422(monkeypatch):
    monkeypatch.setattr(
        rag, "add_file",
        lambda ns, name, content, lease=None: (_ for _ in ()).throw(
            rag.BoxLimitExceededError("RAGファイル数の上限(20)に達しています")),
    )
    res = client.post("/api/demos/d1/rag/files",
                      files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 422
    assert "上限" in res.json()["detail"]


def test_global_quota_422_via_handler(monkeypatch):
    """予約 ledger の QuotaExceededError は 422 に統一(specs/18 §3.1)。user 経路にも適用。"""
    monkeypatch.setattr(
        rag, "add_file",
        lambda ns, name, content, lease=None: (_ for _ in ()).throw(
            rag_ledger.QuotaExceededError("limit 2000")),
    )
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 422


def test_filename_over_400_chars_is_422():
    long = "あ" * 401 + ".md"
    res = client.post("/api/demos/d1/rag/files",
                      files={"file": (long, b"x", "text/markdown")})
    assert res.status_code == 422
    assert "filename too long" in res.json()["detail"]


def test_add_file_checks_demo_limit_before_reserve(monkeypatch):
    """上限判定は demo namespace のみ(user は無制限 = 既定の挙動不変)。上限は予約 ledger
    行数(count_for_owner)で測る — 外部削除失敗で残した pending 行も数える(M002)。"""
    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)

    class L:
        @staticmethod
        def upload_gate():
            pass

        @staticmethod
        def count_for_owner(owner):
            return 20  # 上限到達(rag_files 行でなく ledger 行数で測る)

        @staticmethod
        def reserve(*a):
            raise AssertionError("予約前に上限で止まるべき")

    monkeypatch.setattr(rag, "rag_ledger", L)
    lease = demo_lease.DemoLease(demo_id="d1", _conn=None)
    monkeypatch.setattr(rag, "demo_targets",
                        type("T", (), {"record_target": staticmethod(lambda *a: None)}))
    with pytest.raises(rag.BoxLimitExceededError):
        rag.add_file("demo_d1", "a.md", b"x", lease=lease)


def test_delete_file_external_failure_maps_to_503(monkeypatch):
    monkeypatch.setattr(
        rag, "delete_file",
        lambda ns, fid: (_ for _ in ()).throw(
            rag.ExternalDeleteError("original delete failed: 500")),
    )
    res = client.delete("/api/demos/d1/rag/files/f1")
    assert res.status_code == 503  # 行とカウンタを保持して再試行で収束(specs/18 §3.2)
