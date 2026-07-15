"""リアルタイム文字起こし中継(VOICE-02)。

クライアント⇔API: 音声=チャンクPOST / 結果=SSE（API GWがWebSocket非対応のため —
docs/comparison/realtime-transport.md）。API⇔OCI: oci-ai-speech-realtime(WS, IAM署名)。

SPIKE-06の実機確定事項:
- model_type は "WHISPER"（WHISPER_MEDIUMは無効値）、partialは来ない（finalのみ）
- should_ignore_invalid_customizations / final_silence_threshold_in_ms は送ると400
セッション(OCI側WS接続)はプロセス内保持 — Container Instance 1台構成が前提。
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from .minutes import _join_tokens
from .settings import get_settings

logger = logging.getLogger("jetuse.stt")

SESSION_IDLE_SECONDS = 120
MAX_SESSIONS = 4
MAX_CHUNK_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 15

_sessions: dict[str, "SttSession"] = {}


class SttSession:
    def __init__(self, owner: str, language: str):
        self.id = str(uuid.uuid4())
        self.owner = owner
        self.language = language
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        self.client: Any = None
        self.task: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.connect_error: str | None = None
        self.closed = False
        self.last_activity = time.monotonic()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity

    def _emit(self, ev: dict[str, Any] | None) -> None:
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            logger.warning("stt event queue full (dropped) session=%s", self.id)


def _make_listener(session: SttSession):  # noqa: ANN202
    from oci_ai_speech_realtime import RealtimeSpeechClientListener

    class _Listener(RealtimeSpeechClientListener):
        def on_result(self, result: dict) -> None:
            tx = (result.get("transcriptions") or [{}])[0]
            text = tx.get("transcription", "")
            if text:
                # Whisperは分かち書きで返す(SPIKE-06) → VOICE-01と同じ結合処理
                text = _join_tokens(text.split(" "))
                session._emit({"text": text, "is_final": bool(tx.get("isFinal"))})

        def on_ack_message(self, ackmessage: dict) -> None:
            pass

        def on_connect(self) -> None:
            pass

        def on_connect_message(self, connectmessage: dict) -> None:
            session.connected.set()

        def on_network_event(self, message: dict) -> None:
            pass

        def on_error(self, error: Exception) -> None:
            logger.warning("stt realtime error session=%s: %s", session.id, error)
            session.connect_error = str(error)[:300]
            session.connected.set()  # connect待ちを解除して失敗を伝える
            session._emit({"error": str(error)[:300]})

        def on_close(self, error_code: int, error_message: str) -> None:
            session._emit({"closed": True, "code": error_code})
            session._emit(None)

    return _Listener()


def _build_client(session: SttSession) -> Any:
    """OCIリアルタイムSTTクライアントを構築(RP/ユーザー認証両対応)"""
    import oci
    from oci_ai_speech_realtime import RealtimeParameters, RealtimeSpeechClient

    params = RealtimeParameters()
    params.language_code = session.language
    params.model_domain = params.MODEL_DOMAIN_GENERIC
    params.model_type = "WHISPER"
    params.encoding = "audio/raw;rate=16000"
    params.should_ignore_invalid_customizations = None
    params.final_silence_threshold_in_ms = None

    s = get_settings()
    url = f"wss://realtime.aiservice.{s.oci_region}.oci.oraclecloud.com"
    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        config: dict = {"region": s.oci_region}
    else:
        from .genai import load_local_oci_config

        signer = None
        config = load_local_oci_config()
    return RealtimeSpeechClient(
        config=config,
        realtime_speech_parameters=params,
        listener=_make_listener(session),
        service_endpoint=url,
        signer=signer,
        compartment_id=s.compartment_ocid,
    )


async def sweep_idle() -> None:
    for sid, sess in list(_sessions.items()):
        if sess.idle_seconds > SESSION_IDLE_SECONDS:
            logger.info("stt session idle close session=%s", sid)
            await _close(sess)


async def _close(session: SttSession) -> None:
    _sessions.pop(session.id, None)
    if session.closed:
        return
    session.closed = True
    try:
        if session.client is not None:
            session.client.close()
    except Exception:
        logger.exception("stt client close failed (ignored)")
    if session.task is not None:
        # connectタスクはclose()でwebsocket切断後に抜けてくる
        try:
            await asyncio.wait_for(session.task, timeout=5)
        except (TimeoutError, Exception):  # noqa: BLE001
            session.task.cancel()
    session._emit({"closed": True})
    session._emit(None)


async def create_session(owner: str, language: str) -> dict[str, Any]:
    # 同一ownerの既存セッションは置き換え(1ユーザー1本)
    for sess in list(_sessions.values()):
        if sess.owner == owner:
            await _close(sess)
    await sweep_idle()
    if len(_sessions) >= MAX_SESSIONS:
        raise RuntimeError("too many active sessions")

    session = SttSession(owner, language)
    session.client = _build_client(session)
    session.task = asyncio.create_task(session.client.connect())
    _sessions[session.id] = session
    try:
        await asyncio.wait_for(session.connected.wait(), timeout=CONNECT_TIMEOUT_SECONDS)
    except TimeoutError:
        await _close(session)
        raise RuntimeError("realtime session connect timeout") from None
    if session.connect_error:
        await _close(session)
        raise RuntimeError(f"realtime connect failed: {session.connect_error}")
    logger.info("stt session opened session=%s owner=%s", session.id, owner)
    return {"id": session.id, "language": language}


def get_session(owner: str, sid: str) -> SttSession | None:
    sess = _sessions.get(sid)
    if not sess or sess.owner != owner:
        return None
    return sess


async def send_audio(owner: str, sid: str, chunk: bytes) -> bool:
    sess = get_session(owner, sid)
    if not sess or sess.closed:
        return False
    sess.touch()
    await sess.client.send_data(chunk)
    return True


async def close_session(owner: str, sid: str) -> bool:
    sess = get_session(owner, sid)
    if not sess:
        return False
    await _close(sess)
    return True
