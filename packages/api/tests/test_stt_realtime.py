"""VOICE-02: リアルタイムSTT中継セッション管理の単体テスト(OCIクライアントはフェイク)"""

import asyncio

import pytest

from jetuse_core import stt_realtime


class FakeClient:
    def __init__(self, session):
        self.session = session
        self.sent: list[bytes] = []
        self.closed = False

    async def connect(self):
        self.session.connected.set()
        while not self.closed:  # close()まで生き続けるWS受信ループの代役
            await asyncio.sleep(0.01)

    async def send_data(self, data: bytes):
        self.sent.append(data)

    def close(self):
        self.closed = True
        self.session._emit({"closed": True})
        self.session._emit(None)


@pytest.fixture(autouse=True)
def fake_build(monkeypatch):
    def _fake(session):
        return FakeClient(session)

    monkeypatch.setattr(stt_realtime, "_build_client", _fake)
    stt_realtime._sessions.clear()
    yield
    stt_realtime._sessions.clear()


def test_create_send_close_roundtrip():
    async def run():
        rec = await stt_realtime.create_session("u1", "ja")
        sid = rec["id"]
        assert stt_realtime.get_session("u1", sid) is not None
        # 他人からは見えない(owner分離)
        assert stt_realtime.get_session("u2", sid) is None

        assert await stt_realtime.send_audio("u1", sid, b"\x00\x01")
        sess = stt_realtime.get_session("u1", sid)
        assert sess.client.sent == [b"\x00\x01"]

        # 結果イベントの中継
        sess.client.session._emit({"text": "こんにちは", "is_final": True})
        ev = sess.queue.get_nowait()
        assert ev == {"text": "こんにちは", "is_final": True}

        assert await stt_realtime.close_session("u1", sid)
        assert stt_realtime.get_session("u1", sid) is None
        assert not await stt_realtime.send_audio("u1", sid, b"x")

    asyncio.run(run())


def test_same_owner_replaces_session():
    async def run():
        first = await stt_realtime.create_session("u1", "ja")
        second = await stt_realtime.create_session("u1", "ja")
        assert stt_realtime.get_session("u1", first["id"]) is None
        assert stt_realtime.get_session("u1", second["id"]) is not None
        assert len(stt_realtime._sessions) == 1

    asyncio.run(run())


def test_idle_sweep(monkeypatch):
    async def run():
        rec = await stt_realtime.create_session("u1", "ja")
        sess = stt_realtime.get_session("u1", rec["id"])
        sess.last_activity -= stt_realtime.SESSION_IDLE_SECONDS + 1
        await stt_realtime.sweep_idle()
        assert stt_realtime.get_session("u1", rec["id"]) is None

    asyncio.run(run())


def test_max_sessions(monkeypatch):
    async def run():
        for i in range(stt_realtime.MAX_SESSIONS):
            await stt_realtime.create_session(f"u{i}", "ja")
        with pytest.raises(RuntimeError, match="too many"):
            await stt_realtime.create_session("overflow", "ja")

    asyncio.run(run())
