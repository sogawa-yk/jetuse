"""会話API(CHAT-02)のエンドポイントテスト。リポジトリ層はfakeに差し替え。"""

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


class FakeRepo:
    def __init__(self):
        self.convs: dict[str, dict] = {}

    def list_conversations(self, owner):
        return [
            {"id": c["id"], "title": c["title"], "model": c["model"], "updated_at": ""}
            for c in self.convs.values() if c["owner"] == owner
        ]

    def create_conversation(self, owner, model, title):
        cid = f"c{len(self.convs) + 1}"
        self.convs[cid] = {
            "id": cid, "owner": owner, "model": model,
            "title": title or "新しい会話", "messages": [],
        }
        return {"id": cid, "title": title, "model": model}

    def get_conversation(self, owner, cid):
        c = self.convs.get(cid)
        if not c or c["owner"] != owner:
            return None
        return {
            **{k: c[k] for k in ("id", "title", "model", "messages")},
            "oci_conversation_id": c.get("oci_conversation_id", "oc-fake"),
        }

    def set_oci_conversation(self, owner, cid, oci_conversation_id):
        self.convs[cid]["oci_conversation_id"] = oci_conversation_id

    def delete_conversation(self, owner, cid):
        c = self.convs.get(cid)
        if not c or c["owner"] != owner:
            return False
        del self.convs[cid]
        return True

    def append_message(self, cid, role, content):
        self.convs[cid]["messages"].append({"role": role, "content": content})

    def log_usage(self, owner, cid, model, input_tokens, output_tokens):
        pass


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    repo = FakeRepo()
    for name in (
        "list_conversations", "create_conversation", "get_conversation",
        "delete_conversation", "append_message", "log_usage", "set_oci_conversation",
    ):
        monkeypatch.setattr(service_main.conv_repo, name, getattr(repo, name))
    # OCI Conversation削除同期(CHAT-09)は実呼び出しせず記録のみ
    repo.deleted_oci: list[str] = []
    monkeypatch.setattr(
        service_main, "delete_oci_conversation", repo.deleted_oci.append
    )
    get_settings.cache_clear()
    yield repo
    get_settings.cache_clear()


def test_conversation_crud(fake_repo):
    res = client.post("/api/conversations", json={"model": "gpt-oss-120b", "title": "テスト"})
    assert res.status_code == 200
    cid = res.json()["id"]

    assert any(c["id"] == cid for c in client.get("/api/conversations").json()["conversations"])
    assert client.get(f"/api/conversations/{cid}").status_code == 200
    assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": True}
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    # ADB削除に成功したらOCI Conversation側も削除される(CHAT-09)
    assert fake_repo.deleted_oci == ["oc-fake"]


def test_delete_succeeds_even_if_oci_delete_fails(fake_repo, monkeypatch):
    def boom(_):
        raise RuntimeError("oci down")

    monkeypatch.setattr(service_main, "delete_oci_conversation", boom)
    cid = client.post("/api/conversations", json={"model": "gpt-oss-120b"}).json()["id"]
    assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": True}


def test_stream_persists_messages(fake_repo, monkeypatch):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "応答"}
        yield {"usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    cid = client.post("/api/conversations", json={"model": "gpt-oss-120b"}).json()["id"]
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "conversation_id": cid,
            "messages": [{"role": "user", "content": "質問"}],
        },
    )
    assert res.status_code == 200
    msgs = fake_repo.convs[cid]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "応答"


def test_stream_rejects_others_conversation(fake_repo, monkeypatch):
    # dev-user以外の所有会話は404(所有者分離)
    fake_repo.convs["x1"] = {
        "id": "x1", "owner": "someone-else", "model": "gpt-oss-120b",
        "title": "t", "messages": [],
    }
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "conversation_id": "x1",
            "messages": [{"role": "user", "content": "盗み見"}],
        },
    )
    assert res.status_code == 404
    assert client.get("/api/conversations/x1").status_code == 404
    assert client.delete("/api/conversations/x1").status_code == 404
