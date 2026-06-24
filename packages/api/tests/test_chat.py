import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.models import DEFAULT_MODEL, MODELS
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_responses_input_uses_input_text_for_all_roles():
    # output_textはgpt-ossが400で拒否する(実機確定)ため全ロールinput_text
    from jetuse_core.chat import _to_responses_input

    out = _to_responses_input([
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ])
    assert all(item["type"] == "message" for item in out)
    assert all(item["content"][0]["type"] == "input_text" for item in out)


def test_models_registry_consistency():
    assert DEFAULT_MODEL in MODELS
    for m in MODELS.values():
        assert m.api in ("responses", "chat")


def test_list_models():
    res = client.get("/api/chat/models")
    assert res.status_code == 200
    keys = [m["key"] for m in res.json()["models"]]
    assert DEFAULT_MODEL in keys


def test_chat_stream_unknown_model():
    res = client.post(
        "/api/chat/stream",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 400


def test_chat_stream_sse_format(monkeypatch):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "こん"}
        yield {"delta": "にちは"}
        yield {"usage": {"input_tokens": 3, "output_tokens": 2}}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post(
        "/api/chat/stream",
        json={"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    body = res.text
    assert body.startswith('data: {"ka": 1}')
    assert '"delta": "こん"' in body
    assert '"usage"' in body
    assert body.rstrip().endswith("data: [DONE]")


def test_chat_stream_requires_auth(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    res = client.post(
        "/api/chat/stream",
        json={"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 401
