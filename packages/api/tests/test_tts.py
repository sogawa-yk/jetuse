"""TTS(VOICE-03)の縮退メッセージ(PORT-02)。Phoenix未購読等をヒント付き503へ変換する。"""

from unittest import mock

import oci
import pytest
from fastapi.testclient import TestClient

from jetuse_core import tts
from service.main import app

client = TestClient(app)


def test_unknown_voice_raises_value_error():
    with pytest.raises(ValueError):
        tts.synthesize("hello", "NoSuchVoice")


def test_not_authorized_maps_to_hinted_tts_error(monkeypatch):
    err = oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope")
    fake_client = mock.Mock()
    fake_client.synthesize_speech.side_effect = err
    monkeypatch.setattr(tts, "_speech_client", lambda: fake_client)
    with pytest.raises(tts.TtsError) as ei:
        tts.synthesize("こんにちは", tts.DEFAULT_VOICE)
    assert "未購読" in str(ei.value)
    assert "us-phoenix-1" in str(ei.value)


def test_other_service_error_maps_to_generic_tts_error(monkeypatch):
    err = oci.exceptions.ServiceError(500, "InternalError", {}, "boom")
    fake_client = mock.Mock()
    fake_client.synthesize_speech.side_effect = err
    monkeypatch.setattr(tts, "_speech_client", lambda: fake_client)
    with pytest.raises(tts.TtsError):
        tts.synthesize("こんにちは", tts.DEFAULT_VOICE)


def test_auth_mode_guard_runtime_error_maps_to_tts_error(monkeypatch):
    """PORT-02レビュー指摘: _speech_client()がAUTH_MODEガード(genai.load_local_oci_config)
    由来のRuntimeErrorを投げても、TtsErrorに統一されFastAPI/Functions双方で同じ縮退になる
    (統一しないとFunctionsルーターのgeneric except Exceptionで生の500 internal errorに潰れる)。"""
    def boom():
        raise RuntimeError("OCI設定ファイル(~/.oci/config)が見つかりません")

    monkeypatch.setattr(tts, "_speech_client", boom)
    with pytest.raises(tts.TtsError) as ei:
        tts.synthesize("こんにちは", tts.DEFAULT_VOICE)
    assert "~/.oci/config" in str(ei.value)


def test_tts_route_surfaces_hint_as_503(monkeypatch):
    def boom(text, voice):
        raise tts.TtsError("テナンシがus-phoenix-1未購読の可能性")

    monkeypatch.setattr(tts, "synthesize", boom)
    res = client.post("/api/tts", json={"text": "こんにちは", "voice": tts.DEFAULT_VOICE})
    assert res.status_code == 503
    assert "未購読" in res.json()["detail"]
