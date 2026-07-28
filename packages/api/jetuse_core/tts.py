"""TTS(VOICE-03)。OCI Speech の音声合成。

TTS_REGION の扱い(FIX-58 で変更):
  かつては Phoenix 限定サービスだったため us-phoenix-1 決め打ちだったが、提供リージョンは
  拡大しており(2026-07-28 実機: us-chicago-1 で合成成功 / ap-osaka-1・ca-toronto-1 は 404)、
  決め打ちは「デプロイ先では使えるのに Phoenix 未購読なので TTS が死ぬ」という誤った失敗を
  生む。TTS_REGION 未指定時は **デプロイリージョン → us-phoenix-1** の順に試し、成功した方を
  プロセス内に記憶する。明示指定時はそのリージョンだけを使う(挙動を予測可能に保つ)。

ハマりどころ(実機確定):
- SynthesizeSpeechDetails に compartment_id 必須(無いと404 NotAuthorizedOrNotFound)
- model_details に language_code="ja-JP" 必須(無いと英語ボイスallowlistと比較されエラー)
"""

import logging
import threading
import time
from typing import Any

from .settings import get_settings

logger = logging.getLogger("jetuse.tts")

# SPIKE-06で実機確認済みの日本語ボイス(TTS_2_NATURAL)
VOICES = ("Yuki", "Satoshi", "Aiko", "Hana", "Sakura")
TTS_MODEL = "TTS_2_NATURAL"
TTS_LANGUAGE_CODE = "ja-JP"
DEFAULT_VOICE = "Yuki"
MAX_TEXT_CHARS = 500

# TTS_REGION 未指定時のフォールバック先(歴史的に唯一の提供リージョン)
FALLBACK_TTS_REGION = "us-phoenix-1"

_clients: dict[str, Any] = {}
_lock = threading.Lock()
# 直近に合成へ成功したリージョン(次回はここから試す。ただし**候補から他を落とさない** —
# 落とすと、そのリージョンが後から使えなくなったときプロセス再起動までTTSが死ぬ)
_resolved_region: str | None = None
# 直近の実合成の結果(health が「設定はあるが実際は失敗している」を偽陽性にしないため)。
# at は単調時刻。古い成功を無期限に信じるとリージョン提供状況や権限の変化を見逃すため、
# PROBE_TTL_S を過ぎたら実測プローブへ戻す。
_last_result: dict[str, Any] = {"ok": None, "region": None, "hint": None, "at": None}
# health 用の到達性プローブ(list_voices)のキャッシュ: (単調時刻, 結果)
PROBE_TTL_S = 300.0
# プローブ用の (connect, read) タイムアウト。SDK既定(10, 60)のままだと候補が無応答のとき
# /api/health がゲートウェイの60秒を超える。
PROBE_TIMEOUT_S = (5, 10)
_probe_cache: tuple[float, dict[str, Any]] | None = None


class TtsError(Exception):
    """TTS失敗(テナンシ未購読・サービス不在等。PORT-02で明確なヒントを付す)。"""


def candidate_regions() -> list[str]:
    """試行するリージョンを優先順で返す。明示指定があればそれ1つだけ。

    自動モードでは「直近に成功したリージョン → デプロイリージョン → us-phoenix-1」の
    重複なし順。成功したリージョンを先頭に置くだけで、他の候補は残す。
    """
    s = get_settings()
    if s.tts_region:
        return [s.tts_region]
    ordered = [_resolved_region, s.oci_region, FALLBACK_TTS_REGION]
    return [r for i, r in enumerate(ordered) if r and r not in ordered[:i]]


def last_result(*, max_age_s: float = PROBE_TTL_S) -> dict[str, Any]:
    """直近の実合成の結果。max_age_s より古い結果は「未実施(ok=None)」として返す。"""
    r = dict(_last_result)
    at = r.pop("at", None)
    if at is not None and time.monotonic() - at > max_age_s:
        return {"ok": None, "region": None, "hint": None}
    return r


def probe(ttl_s: float = PROBE_TTL_S) -> dict[str, Any]:
    """課金なしで TTS の到達性を確かめる(health 用)。

    `/api/tts` は API Gateway 経由で **Functions** が処理し、`/api/health` は Container Instance が
    返すため、実合成の結果(_last_result)は health を出すプロセスには届かない。設定の有無だけで
    ok と言うと「実際は 503 なのに health は緑」になる(F-007)ので、`list_voices`
    (合成しない=課金なし)で候補リージョンの到達性を実際に確かめる。結果は ttl_s 秒キャッシュする。
    """
    global _probe_cache, _resolved_region
    now = time.monotonic()
    if _probe_cache and now - _probe_cache[0] < ttl_s:
        return dict(_probe_cache[1])

    regions = candidate_regions()
    # compartment_id を省くとテナンシルート扱いになり、リソースプリンシパルでは 404 になる
    # (ユーザープリンシパルでは通るため見落としやすい — 2026-07-28 実機)。必ず渡す。
    compartment = get_settings().compartment_ocid
    result: dict[str, Any] = {"ok": False, "region": None, "hint": None}
    for region in regions:
        try:
            # health は API Gateway の汎用ルート(read_timeout 60秒)で返るため、
            # 候補が無応答のときフォールバック前に 504 にならないよう短いタイムアウトを課す。
            resp = _speech_client(region, timeout=PROBE_TIMEOUT_S).list_voices(
                compartment_id=compartment
            )
        except Exception as e:  # noqa: BLE001 - 診断用。到達不可は次の候補へ
            status = getattr(e, "status", None)
            result["hint"] = (
                f"TTSに到達できません(試行: {', '.join(regions)}"
                f"{f' / 直近 HTTP {status}' if status else ''})。"
                "テナンシの購読状況を確認するか TTS_REGION で提供リージョンを指定してください"
            )
            continue
        # 200 でも「このアプリが使う日本語 TTS_2_NATURAL ボイス」が無ければ合成は失敗する。
        # 到達性だけで ok と言うと偽陽性になるため、実際に使うボイスの存在まで確認する。
        items = getattr(resp.data, "items", None) or []
        usable = [
            v for v in items
            if getattr(v, "voice_id", None) in VOICES
            and getattr(v, "language_code", "") == TTS_LANGUAGE_CODE
            and TTS_MODEL in (getattr(v, "supported_models", None) or [])
        ]
        if not usable:
            result["hint"] = (
                f"{region} は TTS に到達できますが、日本語ボイス({', '.join(VOICES)} / "
                f"{TTS_MODEL})が提供されていません。TTS_REGION で提供リージョンを指定してください"
            )
            continue
        _resolved_region = region
        result = {"ok": True, "region": region, "hint": None}
        break
    _probe_cache = (now, dict(result))
    return dict(result)


def _speech_client(region: str, timeout: Any = None) -> Any:
    """指定リージョンのSpeechクライアント(プロセス内キャッシュ)。

    timeout は (connect, read) 秒。診断プローブは短いタイムアウトを使うため、
    合成用クライアントとはキャッシュキーを分ける。
    """
    key = f"{region}|{timeout}"
    if key not in _clients:
        with _lock:
            if key not in _clients:
                import oci

                from .oci_auth import sdk_signer_args

                args = sdk_signer_args(region)
                args["config"]["region"] = region  # config_file の config にも region を効かせる
                if timeout is not None:
                    args["timeout"] = timeout
                _clients[key] = oci.ai_speech.AIServiceSpeechClient(**args)
    return _clients[key]


def synthesize(text: str, voice: str) -> bytes:
    """テキストをmp3へ合成(同期。呼び出し側でto_thread推奨)"""
    global _resolved_region, _last_result, _probe_cache
    import oci
    import oci.ai_speech.models as sm

    if voice not in VOICES:
        raise ValueError(f"unknown voice: {voice}")

    regions = candidate_regions()
    last_unavailable: oci.exceptions.ServiceError | None = None
    for region in regions:
        try:
            client = _speech_client(region)
        except RuntimeError as e:
            # AUTH_MODEガード(oci_auth.load_local_oci_config)由来。TtsErrorに統一し
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
                # 未購読/未提供。次の候補があれば試す
                last_unavailable = e
                logger.info("TTS unavailable in %s (HTTP %s); trying next region", region, e.status)
                continue
            hint = f"音声合成に失敗しました: {e.code} {e.message}"
            _last_result = {"ok": False, "region": region, "hint": hint, "at": time.monotonic()}
            _probe_cache = None  # 実失敗を観測したら古い成功キャッシュを捨てる
            raise TtsError(hint) from e
        _resolved_region = region
        _last_result = {"ok": True, "region": region, "hint": None, "at": time.monotonic()}
        return r.data.content

    hint = (
        f"TTSにアクセスできません(試行: {', '.join(regions)})。"
        "テナンシがこれらのリージョンを未購読か、TTSが未提供の可能性があります。"
        "TTS_REGION で提供リージョンを明示できます"
    )
    _last_result = {"ok": False, "region": None, "hint": hint, "at": time.monotonic()}
    _probe_cache = None  # 実失敗を観測したら古い成功キャッシュを捨てる
    raise TtsError(hint) from last_unavailable
