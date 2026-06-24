"""DB停止時の即時503(CHAT-07)。リポジトリ層がoracledb.Errorを上げたらハングせず503。"""

import oracledb
from fastapi.testclient import TestClient

import service.main as service_main
from service.main import app

client = TestClient(app)


def _raise_db_error(*args, **kwargs):
    raise oracledb.OperationalError("DPY-6005: cannot connect to database")


def test_conversations_return_503_when_db_down(monkeypatch):
    monkeypatch.setattr(service_main.conv_repo, "list_conversations", _raise_db_error)
    monkeypatch.setattr(service_main.conv_repo, "create_conversation", _raise_db_error)
    res = client.get("/api/conversations")
    assert res.status_code == 503
    assert res.json() == {"detail": "database unavailable"}
    res = client.post("/api/conversations", json={"model": "gpt-oss-120b"})
    assert res.status_code == 503


def test_stream_with_conversation_returns_503_when_db_down(monkeypatch):
    monkeypatch.setattr(service_main.conv_repo, "get_conversation", _raise_db_error)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "conversation_id": "c1",
            "messages": [{"role": "user", "content": "q"}],
        },
    )
    assert res.status_code == 503


def test_stateless_stream_works_without_db(monkeypatch):
    """会話IDなしのチャットはDBに触れず通る(DB障害時のフォールバック経路)"""

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post(
        "/api/chat/stream",
        json={"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}]},
    )
    assert res.status_code == 200
    assert '"delta": "ok"' in res.text
