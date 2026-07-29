"""TTS(VOICE-03)の縮退メッセージ(PORT-02)とリージョン試行(FIX-58)。

FIX-58: TTS_REGION 未指定時はデプロイリージョン → us-phoenix-1 の順に試す
(Phoenix決め打ちだと、TTSが提供されているデプロイリージョンでもPhoenix未購読の
テナンシで機能が死ぬ — 2026-07-28 実機で us-chicago-1 の合成成功を確認)。
"""

from unittest import mock

import oci
import pytest
from fastapi.testclient import TestClient

from jetuse_core import tts
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_tts_state(monkeypatch):
    monkeypatch.setattr(tts, "_resolved_region", None)
    monkeypatch.setattr(tts, "_clients", {})
    monkeypatch.setattr(tts, "_last_result", {"ok": None, "region": None, "hint": None, "at": None})
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _service_error(status, code="NotAuthorizedOrNotFound"):
    return oci.exceptions.ServiceError(status, code, {}, "nope")


def test_unknown_voice_raises_value_error():
    with pytest.raises(ValueError):
        tts.synthesize("hello", "NoSuchVoice")


def test_candidate_regions_defaults_to_deploy_region_then_phoenix(monkeypatch):
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    get_settings.cache_clear()
    assert tts.candidate_regions() == ["us-chicago-1", "us-phoenix-1"]


def test_candidate_regions_honours_explicit_setting(monkeypatch):
    monkeypatch.setenv("TTS_REGION", "us-phoenix-1")
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    get_settings.cache_clear()
    assert tts.candidate_regions() == ["us-phoenix-1"]


def test_falls_back_to_next_region_when_unavailable(monkeypatch):
    """デプロイリージョンで404でも、Phoenixで合成できれば成功として返す。"""
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    get_settings.cache_clear()
    ok_client = mock.Mock()
    ok_client.synthesize_speech.return_value = mock.Mock(data=mock.Mock(content=b"mp3"))
    ng_client = mock.Mock()
    ng_client.synthesize_speech.side_effect = _service_error(404)
    monkeypatch.setattr(
        tts, "_speech_client", lambda r: ng_client if r == "ap-osaka-1" else ok_client
    )
    assert tts.synthesize("こんにちは", tts.DEFAULT_VOICE) == b"mp3"


def test_all_regions_unavailable_maps_to_hinted_tts_error(monkeypatch):
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    get_settings.cache_clear()
    fake_client = mock.Mock()
    fake_client.synthesize_speech.side_effect = _service_error(404)
    monkeypatch.setattr(tts, "_speech_client", lambda r: fake_client)
    with pytest.raises(tts.TtsError) as ei:
        tts.synthesize("こんにちは", tts.DEFAULT_VOICE)
    assert "us-chicago-1" in str(ei.value)
    assert "us-phoenix-1" in str(ei.value)
    assert "TTS_REGION" in str(ei.value)


def test_resolved_region_stays_first_but_keeps_other_candidates(monkeypatch):
    """F-006: 一度成功したリージョンが後から使えなくなっても、他候補へ切り替われること
    (解決済みリージョンだけに絞ると、プロセス再起動までTTSが死ぬ)。"""
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(tts, "_resolved_region", "us-chicago-1")
    assert tts.candidate_regions() == ["us-chicago-1", "us-phoenix-1"]

    dead = mock.Mock()
    dead.synthesize_speech.side_effect = _service_error(404)
    alive = mock.Mock()
    alive.synthesize_speech.return_value = mock.Mock(data=mock.Mock(content=b"mp3"))
    monkeypatch.setattr(
        tts, "_speech_client", lambda r: dead if r == "us-chicago-1" else alive
    )
    assert tts.synthesize("こんにちは", tts.DEFAULT_VOICE) == b"mp3"
    assert tts.last_result()["region"] == "us-phoenix-1"


def test_other_service_error_maps_to_generic_tts_error(monkeypatch):
    fake_client = mock.Mock()
    fake_client.synthesize_speech.side_effect = _service_error(500, "InternalError")
    monkeypatch.setattr(tts, "_speech_client", lambda r: fake_client)
    with pytest.raises(tts.TtsError):
        tts.synthesize("こんにちは", tts.DEFAULT_VOICE)


def test_auth_mode_guard_runtime_error_maps_to_tts_error(monkeypatch):
    """PORT-02レビュー指摘: _speech_client()がAUTH_MODEガード(oci_auth.load_local_oci_config)
    由来のRuntimeErrorを投げても、TtsErrorに統一されFastAPI/Functions双方で同じ縮退になる
    (統一しないとFunctionsルーターのgeneric except Exceptionで生の500 internal errorに潰れる)。"""
    def boom(region):
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


def test_probe_rejects_region_without_japanese_voices(monkeypatch):
    """F002: list_voices が 200 でも、アプリが使う日本語 TTS_2_NATURAL ボイスが無ければ
    合成は失敗するので ok と言わない。"""
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    monkeypatch.setattr(tts, "_probe_cache", None)
    get_settings.cache_clear()

    def voice(vid, lang, models):
        v = mock.Mock()
        v.voice_id, v.language_code, v.supported_models = vid, lang, models
        return v

    def client(region, timeout=None):
        c = mock.Mock()
        items = [voice("Brian", "en-US", ["TTS_2_NATURAL"])]  # 英語のみ
        if region == "us-phoenix-1":
            items.append(voice("Yuki", "ja-JP", ["TTS_2_NATURAL"]))
        c.list_voices.return_value = mock.Mock(data=mock.Mock(items=items))
        return c

    monkeypatch.setattr(tts, "_speech_client", client)
    out = tts.probe()
    assert out["ok"] is True
    assert out["region"] == "us-phoenix-1"  # 英語しかない chicago は採用しない


def test_probe_uses_short_timeout_client(monkeypatch):
    """F003: health は API Gateway の60秒ルートで返るため、プローブは短いタイムアウトを使う。"""
    monkeypatch.setattr(tts, "_probe_cache", None)
    get_settings.cache_clear()
    seen: list = []

    def client(region, timeout=None):
        seen.append(timeout)
        c = mock.Mock()
        v = mock.Mock()
        v.voice_id, v.language_code, v.supported_models = "Yuki", "ja-JP", ["TTS_2_NATURAL"]
        c.list_voices.return_value = mock.Mock(data=mock.Mock(items=[v]))
        return c

    monkeypatch.setattr(tts, "_speech_client", client)
    tts.probe()
    assert seen and seen[0] == tts.PROBE_TIMEOUT_S


def test_last_result_expires_so_health_re_probes(monkeypatch):
    """F004: 古い成功を無期限に信じない。TTL を過ぎたら「未実施」として実測へ戻す。"""
    monkeypatch.setattr(
        tts, "_last_result",
        {"ok": True, "region": "us-chicago-1", "hint": None, "at": 0.0},
    )
    monkeypatch.setattr(tts.time, "monotonic", lambda: tts.PROBE_TTL_S + 1)
    assert tts.last_result()["ok"] is None
    monkeypatch.setattr(tts.time, "monotonic", lambda: 1.0)
    assert tts.last_result()["ok"] is True
