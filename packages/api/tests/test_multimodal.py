"""MM-01: 画像入力チャットのバリデーション単体テスト"""

from fastapi.testclient import TestClient

from service.main import app

client = TestClient(app)

DATA_URI = "data:image/png;base64,iVBORw0KGgo="


def _post(model: str, **extra):
    return client.post(
        "/api/chat/stream",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "これは何の画像？"}],
            "images": [DATA_URI],
            **extra,
        },
    )


def test_images_require_vision_model():
    res = _post("gpt-oss-120b")
    assert res.status_code == 422
    assert "does not support images" in res.json()["detail"]


def test_images_reject_agent_combination():
    res = _post("gemini-2.5-flash", agent=True)
    assert res.status_code == 422


def test_images_reject_non_data_uri():
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "x"}],
            "images": ["https://example.com/a.png"],
        },
    )
    assert res.status_code == 422
    assert "data URI" in res.json()["detail"]


def test_images_reject_oversize():
    big = "data:image/png;base64," + "A" * (2 * 1024 * 1024 + 1)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "x"}],
            "images": [big],
        },
    )
    assert res.status_code == 413


def test_images_reject_last_message_not_user():
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gemini-2.5-flash",
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ],
            "images": [DATA_URI],
        },
    )
    assert res.status_code == 422


def test_vision_flag_in_models_endpoint():
    res = client.get("/api/chat/models")
    models = {m["key"]: m for m in res.json()["models"]}
    assert models["gemini-2.5-flash"]["vision"] is True
    assert models["gpt-oss-120b"]["vision"] is False
    assert "llama-3.2-90b-vision" in models
