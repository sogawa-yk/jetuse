"""生成パラメータ拡張(CHAT-04b)。バリデーションとstream_chatへのパススルー。"""

from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.chat import GenParams, _extra_responses_params
from jetuse_core.models import MODELS
from service.main import app

client = TestClient(app)


def test_params_passed_to_stream_chat(monkeypatch):
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["params"] = params
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "q"}],
            "top_p": 0.5,
            "max_tokens": 100,
            "reasoning_effort": "low",
        },
    )
    assert res.status_code == 200
    assert captured["params"] == GenParams(top_p=0.5, max_tokens=100, reasoning_effort="low")


def test_params_validation():
    base = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}]}
    assert client.post("/api/chat/stream", json={**base, "top_p": 1.5}).status_code == 422
    assert client.post("/api/chat/stream", json={**base, "top_p": 0}).status_code == 422
    assert client.post("/api/chat/stream", json={**base, "max_tokens": 0}).status_code == 422
    bad_effort = {**base, "reasoning_effort": "max"}
    assert client.post("/api/chat/stream", json=bad_effort).status_code == 422


def test_reasoning_effort_only_for_reasoning_models():
    # gpt-oss(reasoning=True)はreasoningを付与、非推論モデル想定ではeffortを無視
    p = GenParams(top_p=0.9, max_tokens=64, reasoning_effort="high")
    out = _extra_responses_params(MODELS["gpt-oss-120b"], p)
    assert out == {"top_p": 0.9, "max_output_tokens": 64, "reasoning": {"effort": "high"}}
    non_reasoning = MODELS["llama-3.3-70b"]  # reasoning=False
    out2 = _extra_responses_params(non_reasoning, p)
    assert "reasoning" not in out2


def test_models_endpoint_exposes_capabilities():
    res = client.get("/api/chat/models").json()["models"]
    gpt = next(m for m in res if m["key"] == "gpt-oss-120b")
    assert gpt["api"] == "responses" and gpt["reasoning"] is True
    llama = next(m for m in res if m["key"] == "llama-3.3-70b")
    assert llama["reasoning"] is False
    gemini = next(m for m in res if m["key"] == "gemini-2.5-flash")
    assert gemini["min_max_tokens"] == 2048  # 思考型モデルの実用下限


def test_gemini_max_tokens_clamped_to_floor():
    # 思考型モデルは小さいmax_tokensで空応答/ハングするため下限でクランプ(実機挙動)
    from jetuse_core.chat import _stream_chat_completions

    class FakeStream:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                captured = {}

                @classmethod
                def create(cls, **kw):
                    cls.captured = kw
                    return FakeStream()

    gen = _stream_chat_completions(
        FakeClient, MODELS["gemini-2.5-flash"], [{"role": "user", "content": "q"}],
        0.7, GenParams(max_tokens=100),
    )
    list(gen)
    assert FakeClient.chat.completions.captured["max_tokens"] == 2048
