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
            for c in self.convs.values()
            if c["owner"] == owner and c.get("demo_id") is None
        ]

    def create_conversation(self, owner, model, title, demo_id=None):
        cid = f"c{len(self.convs) + 1}"
        self.convs[cid] = {
            "id": cid, "owner": owner, "model": model, "demo_id": demo_id,
            "title": title or "新しい会話", "messages": [],
        }
        return {"id": cid, "title": title, "model": model}

    def get_conversation(self, owner, cid, demo_id=None):
        # 実装と同じ契約(specs/18 §4.2): user 経路は demo_id IS NULL、demo は exact 一致
        c = self.convs.get(cid)
        if not c or c["owner"] != owner or c.get("demo_id") != demo_id:
            return None
        return {
            **{k: c[k] for k in ("id", "title", "model", "messages")},
            "oci_conversation_id": c.get("oci_conversation_id", "oc-fake"),
        }

    def set_oci_conversation(self, owner, cid, oci_conversation_id):
        self.convs[cid]["oci_conversation_id"] = oci_conversation_id

    def delete_conversation(self, owner, cid):
        c = self.convs.get(cid)
        if not c or c["owner"] != owner or c.get("demo_id") is not None:
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
    # owner_key_gate は preflight(DB 接続)なのでこのユニットでは no-op(M004 ゲートは別テスト)
    import service.routes.chat as chat_routes
    import service.routes.conversations as conv_routes
    monkeypatch.setattr(conv_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)  # review-11 B004
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


def test_stream_with_agent_id_still_validates_conversation(fake_repo, monkeypatch):
    """review-2 B002: agent_id があっても conversation_id の所有者検証は必須。
    保存済み agent 経路(agent_dispatch)が owner 条件なしで append_message するため、
    他人の conversation_id + 自分の agent_id で越境書き込みできてはならない(404 で拒否)。"""
    fake_repo.convs["x1"] = {
        "id": "x1", "owner": "someone-else", "model": "gpt-oss-120b",
        "demo_id": None, "title": "t", "messages": [],
    }
    # get_agent が呼ばれる前に 404 になること(検証は全早期 return より前)
    import jetuse_core.agents as agents_repo
    monkeypatch.setattr(agents_repo, "get_agent",
                        lambda *a, **k: pytest.fail("must 404 before agent dispatch"))
    res = client.post(
        "/api/chat/stream",
        json={"model": "gpt-oss-120b", "conversation_id": "x1", "agent_id": "ag-1",
              "messages": [{"role": "user", "content": "越境書き込み"}]},
    )
    assert res.status_code == 404
