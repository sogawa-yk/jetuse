"""上流キャンセル伝搬(CHAT-08)。クライアント切断で上流ストリームがcloseされる。

TestClientは実切断を模せない(全身読み切りでハング)ため、実uvicornを立てて検証する。
"""

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import service.main as service_main
from service.main import app

client = TestClient(app)

PORT = 18923


class SlowFakeStream:
    """closeされたことを記録する有限の遅いストリーム(暴走防止で上限あり)"""

    def __init__(self):
        self.closed = False
        self.yielded = 0

    def __call__(self, model_key, messages, temperature=None, user="",
                 oci_conversation_id=None, params=None):
        try:
            for _ in range(50_000):
                self.yielded += 1
                yield {"delta": "x"}
                time.sleep(0.005)
        finally:
            self.closed = True


@pytest.fixture
def real_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield server
    server.should_exit = True
    thread.join(timeout=10)


def test_disconnect_closes_upstream(monkeypatch, real_server):
    fake = SlowFakeStream()
    monkeypatch.setattr(service_main, "stream_chat", fake)
    body = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}]}
    with (
        httpx.Client(timeout=10) as http,
        http.stream("POST", f"http://127.0.0.1:{PORT}/api/chat/stream", json=body) as res,
    ):
        assert res.status_code == 200
        for i, _line in enumerate(res.iter_lines()):
            if i >= 5:
                break  # withを抜ける=クライアント切断

    deadline = time.time() + 10
    while not fake.closed and time.time() < deadline:
        time.sleep(0.05)
    assert fake.closed, "upstream stream was not closed after client disconnect"
    assert fake.yielded < 50_000  # 完走せず途中で打ち切られている


def test_normal_completion_still_works(monkeypatch):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "完"}
        yield {"usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post(
        "/api/chat/stream",
        json={"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}]},
    )
    assert res.status_code == 200
    assert res.text.rstrip().endswith("data: [DONE]")
