"""デモスコープ能力ルート(SP1-03 / specs/17 §5)のテスト。

demos リポジトリと rag 層は fake、require_demo seam は実関数。
箱 = `demo_<id>` 名前空間(rag の owner キー)がユーザー単位と分離されることを検証する。
"""

import pytest
from fastapi.testclient import TestClient

import service.demo_context as demo_context
import service.main as service_main
from jetuse_core.models import DEFAULT_MODEL
from service.main import app

client = TestClient(app)

DEMOS = {
    "d1": {"id": "d1", "owner_sub": "dev-user", "name": "mine", "visibility": "private",
           "status": "ready"},
    "d2": {"id": "d2", "owner_sub": "dev-user", "name": "mine2", "visibility": "private",
           "status": "ready"},
    "theirs": {"id": "theirs", "owner_sub": "user-a", "name": "A's", "visibility": "private",
               "status": "ready"},
    "pub": {"id": "pub", "owner_sub": "user-a", "name": "shared", "visibility": "public",
            "status": "ready"},
}

CHAT_BODY = {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}]}


class NsFakeRag:
    """名前空間キー(owner引数)ごとにファイル・ストアを分離する fake。"""

    def __init__(self):
        self.files: dict[str, dict[str, dict]] = {}
        self.stores: dict[str, str] = {}

    def list_files(self, ns):
        return [dict(v) for v in self.files.get(ns, {}).values()]

    def refresh_statuses(self, ns, files):
        return files

    def add_file(self, ns, filename, content):
        box = self.files.setdefault(ns, {})
        fid = f"{ns}-f{len(box) + 1}"
        box[fid] = {
            "id": fid, "filename": filename, "status": "processing",
            "bytes": len(content), "oci_file_id": f"file-{fid}",
        }
        self.stores.setdefault(ns, f"vs_{ns}")
        return box[fid]

    def delete_file(self, ns, file_id):
        return self.files.get(ns, {}).pop(file_id, None) is not None

    def get_store_id(self, ns):
        return self.stores.get(ns)


@pytest.fixture(autouse=True)
def fake_demos(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)


@pytest.fixture(autouse=True)
def fake_rag(monkeypatch):
    fake = NsFakeRag()
    for name in ("list_files", "refresh_statuses", "add_file", "delete_file", "get_store_id"):
        monkeypatch.setattr(service_main.rag, name, getattr(fake, name))
    yield fake


def test_owner_chat_streams(monkeypatch):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "demo"}
        yield {"delta": "応答"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/demos/d1/chat", json=CHAT_BODY)
    assert res.status_code == 200
    body = res.text
    assert body.startswith('data: {"ka": 1}')
    assert '"delta": "demo"' in body
    assert body.rstrip().endswith("data: [DONE]")


def test_demo_chat_rag_uses_demo_namespace_store(monkeypatch):
    # デモの箱にだけ文書がある状態を作る
    client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("policy.md", b"# rules", "text/markdown")},
    )
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["store"] = params.file_search_store
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/demos/d1/chat", json={**CHAT_BODY, "rag": True})
    assert res.status_code == 200
    assert captured["store"] == "vs_demo_d1"
    # user 単位のストアは空のまま → 既存 /api/chat/stream の rag は 400(ストア未作成)
    assert client.post("/api/chat/stream", json={**CHAT_BODY, "rag": True}).status_code == 400


def test_owner_rag_upload_list_delete():
    res = client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("policy.md", b"# regulations", "text/markdown")},
    )
    assert res.status_code == 200
    fid = res.json()["id"]
    assert res.json()["status"] == "processing"
    listed = client.get("/api/demos/d1/rag/files").json()["files"]
    assert any(f["id"] == fid for f in listed)
    assert client.delete(f"/api/demos/d1/rag/files/{fid}").json() == {"deleted": True}
    assert client.delete(f"/api/demos/d1/rag/files/{fid}").status_code == 404


def test_demo_upload_same_validation_as_user_route():
    res = client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("doc.docx", b"x", "application/octet-stream")},
    )
    assert res.status_code == 422
    assert "docx" in res.json()["detail"]
    assert client.post(
        "/api/demos/d1/rag/files", files={"file": ("a.md", b"", "x")}
    ).status_code == 422


def test_demo_upload_returns_503_when_store_not_ready(monkeypatch):
    def not_ready(ns, filename, content):
        raise service_main.rag.StoreNotReadyError("dp propagation timeout")

    monkeypatch.setattr(service_main.rag, "add_file", not_ready)
    res = client.post(
        "/api/demos/d1/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    )
    assert res.status_code == 503


def test_cross_user_demo_is_404_for_chat_and_rag():
    # dev-user が user-a の private デモへアクセス(存在秘匿 = 404)
    assert client.post("/api/demos/theirs/chat", json=CHAT_BODY).status_code == 404
    assert client.get("/api/demos/theirs/rag/files").status_code == 404
    assert client.post(
        "/api/demos/theirs/rag/files",
        files={"file": ("a.md", b"x", "text/markdown")},
    ).status_code == 404
    assert client.delete("/api/demos/theirs/rag/files/f1").status_code == 404


def test_demo_boxes_are_isolated(fake_rag):
    client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("only-in-d1.md", b"x", "text/markdown")},
    )
    assert client.get("/api/demos/d2/rag/files").json()["files"] == []
    d1_files = client.get("/api/demos/d1/rag/files").json()["files"]
    assert [f["filename"] for f in d1_files] == ["only-in-d1.md"]
    # user 単位ルートの箱にも現れない
    assert client.get("/api/rag/files").json()["files"] == []


def test_public_demo_non_owner_can_read_and_chat_but_not_write(monkeypatch):
    """公開デモは非所有者も閲覧・実行(chat/GET)可。書き込み(POST/DELETE)は所有者のみ(REV-002)。"""

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    # dev-user は "pub"(user-a 所有・public)の非所有者
    assert client.post("/api/demos/pub/chat", json=CHAT_BODY).status_code == 200
    assert client.get("/api/demos/pub/rag/files").status_code == 200
    assert client.post(
        "/api/demos/pub/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    ).status_code == 404
    assert client.delete("/api/demos/pub/rag/files/f1").status_code == 404


def test_owner_can_write_own_public_demo(monkeypatch):
    monkeypatch.setitem(DEMOS, "mypub",
                        {"id": "mypub", "owner_sub": "dev-user", "name": "p",
                         "visibility": "public", "status": "ready"})
    assert client.post(
        "/api/demos/mypub/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    ).status_code == 200


def test_demo_chat_rejects_conversation_id():
    """デモ会話の demo_id 紐付けは SP2。それまで user 会話の持ち込みを拒否する(REV-004)。"""
    res = client.post("/api/demos/d1/chat", json={**CHAT_BODY, "conversation_id": "c1"})
    assert res.status_code == 422


def test_capabilities_list_demo_scoped_routes():
    from jetuse_core.capabilities import CAPABILITIES

    by_name = {c["capability"]: c for c in CAPABILITIES}
    chat_paths = {r["path"] for r in by_name["chat"]["routes"]}
    rag_paths = {r["path"] for r in by_name["rag.search"]["routes"]}
    assert "/api/demos/{demo_id}/chat" in chat_paths
    assert "/api/demos/{demo_id}/rag/files" in rag_paths
