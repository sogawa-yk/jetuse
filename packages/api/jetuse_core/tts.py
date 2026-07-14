"""TTS(VOICE-03)。Phoenixクロスリージョン呼び出し(SPIKE-06: TTSはPhoenix限定)。

ハマりどころ(実機確定):
- SynthesizeSpeechDetails に compartment_id 必須(無いと404 NotAuthorizedOrNotFound)
- model_details に language_code="ja-JP" 必須(無いと英語ボイスallowlistと比較されエラー)
"""

import logging
import os
import threading
from typing import Any

from .settings import get_settings

logger = logging.getLogger("jetuse.tts")

# SPIKE-06で実機確認済みの日本語ボイス(TTS_2_NATURAL)
VOICES = ("Yuki", "Satoshi", "Aiko", "Hana", "Sakura")
DEFAULT_VOICE = "Yuki"
MAX_TEXT_CHARS = 500

_client: Any = None
_lock = threading.Lock()


class TtsError(Exception):
    """TTS失敗(テナンシ未購読・サービス不在等。PORT-02で明確なヒントを付す)。"""


def _speech_client() -> Any:
    """TTSリージョンのSpeechクライアント(プロセス内キャッシュ)"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import oci

                region = get_settings().tts_region
                if os.environ.get("AUTH_MODE") == "resource_principal":
                    signer = oci.auth.signers.get_resource_principals_signer()
                    _client = oci.ai_speech.AIServiceSpeechClient(
                        {"region": region}, signer=signer
                    )
                else:
                    from .genai import load_local_oci_config

                    cfg = load_local_oci_config()
                    cfg["region"] = region
                    _client = oci.ai_speech.AIServiceSpeechClient(cfg)
    return _client


def synthesize(text: str, voice: str) -> bytes:
    """テキストをmp3へ合成(同期。呼び出し側でto_thread推奨)"""
    import oci
    import oci.ai_speech.models as sm

    if voice not in VOICES:
        raise ValueError(f"unknown voice: {voice}")
    try:
        client = _speech_client()
    except RuntimeError as e:
        # AUTH_MODEガード(genai.load_local_oci_config)由来。TtsErrorに統一し
        # FastAPI/Functionsルーター双方で同じ縮退(503+ヒント)にする(レビュー指摘)。
        raise TtsError(str(e)) from e
    try:
        r = client.synthesize_speech(
            sm.SynthesizeSpeechDetails(
                text=text,
                compartment_id=get_settings().compartment_ocid,
                configuration=sm.TtsOracleConfiguration(
                    model_details=sm.TtsOracleTts2NaturalModelDetails(
                        voice_id=voice, language_code="ja-JP"
                    ),
                    # 既定はWAV(24kHz PCM、1文270KB超)のためMP3を明示(帯域1/10程度)
                    speech_settings=sm.TtsOracleSpeechSettings(output_format="MP3"),
                ),
            )
        )
    except oci.exceptions.ServiceError as e:
        if e.status in (401, 403, 404):
            region = get_settings().tts_region
            raise TtsError(
                f"TTSにアクセスできません(テナンシが{region}未購読の可能性。"
                "TTS_REGIONで対象リージョンを変更できます)"
            ) from e
        raise TtsError(f"音声合成に失敗しました: {e.code} {e.message}") from e
    return r.data.content
