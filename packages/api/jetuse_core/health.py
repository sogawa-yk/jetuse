"""機能別 readiness 集約(PORT-02)。

FIX-47の `/api/rag/health`(project/CP/DP 3点検査)を土台に、GenAI以外の機能面
(モデル可用性・NL2SQL・Speech・OCR・TTS)も横断して自己診断できるようにする。
Issue #47 の「切り分け不能」問題のアプリ全域での根治。

各チェックは他機能を巻き込まないよう例外を握りつぶし、ok(bool) + hint(理由) を返す。
実際にAPI課金が発生しうる呼び出し(TTS合成・OCR実行等)は行わない=設定の整合性のみを見る
(実失敗は各機能のリクエスト時に個別に縮退メッセージへ変換済み — PORT-02作業内容4)。
"""

from typing import Any

from . import nl2sql, rag
from .bootstrap import resource_principal_status
from .models import MODELS, model_status
from .settings import get_settings


def _check(ok: bool | None, hint: str | None = None) -> dict[str, Any]:
    """ok=None は「未検証」(例: bootstrap未完了)。ok=Falseと同様に非okだが、
    hintで区別できるようにする(レビュー指摘F-003: 未検証をokと偽らない)。"""
    out: dict[str, Any] = {"ok": ok}
    if not ok and hint:
        out["hint"] = hint
    return out


def _agg(checks: list[dict[str, Any]]) -> str:
    oks = [c["ok"] for c in checks]
    if all(oks):
        return "ok"
    if any(oks):
        return "degraded"
    return "unavailable"


def chat_health() -> dict[str, Any]:
    models = {}
    for key in MODELS:
        ok, hint = model_status(key)
        models[key] = _check(ok, hint)
    return {"status": _agg(list(models.values())) if models else "unavailable", "models": models}


def _rag_health() -> dict[str, Any]:
    try:
        # allow_autocreate=False: 集約healthはGETポーリングされうるため、project未解決を
        # そのまま報告するだけに留め、GenerativeAiProjectの新規作成は起こさない(レビュー指摘)。
        raw = rag.health_check(allow_autocreate=False)
    except Exception as e:  # noqa: BLE001 - RAG個別の想定外失敗で/api/health全体を落とさない
        return {"status": "unavailable", "hint": f"RAG health check failed: {type(e).__name__}"}
    if raw["ok"]:
        return {"status": "ok"}
    project_ok = raw["checks"].get("project", {}).get("ok", False)
    hints = [c["hint"] for c in raw["checks"].values() if not c.get("ok") and c.get("hint")]
    return {
        "status": "degraded" if project_ok else "unavailable",
        "hint": "; ".join(hints)[:500] if hints else None,
        "checks": raw["checks"],
    }


def dbchat_health() -> dict[str, Any]:
    s = get_settings()
    sem_ok = bool(s.semstore_ocid)
    semantic = _check(sem_ok, "SEMSTORE_OCID 未設定" if not sem_ok else None)
    rp = resource_principal_status()
    if rp.get("ok") is None:
        # bootstrap(別プロセス起動 = dev-app では未実行)で rp 未検証のときは、uvicorn プロセスで
        # RP 経路を一度実測する(nl2sql.select_ai_rp_status はキャッシュ)。共有 ADB は変更しない。
        rp = nl2sql.select_ai_rp_status()
    select_ai = _check(rp["ok"], rp.get("hint"))
    try:
        sample = nl2sql.sh_sample_status()
        sample_check = _check(sample["available"], sample.get("reason"))
    except Exception as e:  # noqa: BLE001 - DB未接続等。診断エンドポイントは落とさない
        sample_check = _check(False, f"SHサンプル検査に失敗しました: {type(e).__name__}")
    # sample_dataは「SQL生成能力」ではなく「(生成できた場合に)SHサンプルが読めるか」という
    # 前提条件でしかないため、semantic_store/select_aiと単純に_agg()すると、両方とも
    # 生成不可なのにsample_data=trueだけでdegraded判定になってしまう(レビュー指摘)。
    # 生成経路が1つも無ければunavailable、経路はあるがsampleだけ不調ならdegraded、とする。
    has_generation_backend = semantic["ok"] is True or select_ai["ok"] is True
    if not has_generation_backend:
        status = "unavailable"
    elif sample_check["ok"] is not True:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "semantic_store": semantic,
        "select_ai": select_ai,
        "sample_data": sample_check,
    }


def speech_health() -> dict[str, Any]:
    ok = bool(get_settings().speech_bucket)
    return {"status": "ok" if ok else "unavailable",
            **({"hint": "SPEECH_BUCKET 未設定"} if not ok else {})}


def ocr_health() -> dict[str, Any]:
    # OCR自体に専用設定は不要だが、全OCI呼び出しに必須のcompartment_ocidが空なら
    # 呼び出し前から確実に失敗するため、それだけは検出する(実合成/実OCR自体は課金対象
    # のため呼ばない — レビュー指摘: 常時okは最低限の設定不備すら見逃す)。
    ok = bool(get_settings().compartment_ocid)
    return {"status": "ok" if ok else "unavailable",
            **({"hint": "COMPARTMENT_OCID 未設定"} if not ok else {})}


def tts_health() -> dict[str, Any]:
    # 実合成は課金対象のため呼ばない。設定(TTS_REGION)とcompartment_ocidの有無を報告する。
    ok = bool(get_settings().compartment_ocid)
    return {
        "status": "ok" if ok else "unavailable",
        "region": get_settings().tts_region,
        **({"hint": "COMPARTMENT_OCID 未設定"} if not ok else {}),
    }


def capability_health() -> dict[str, Any]:
    chat = chat_health()
    capabilities = {
        "chat": chat,
        "rag": _rag_health(),
        "dbchat": dbchat_health(),
        "speech": speech_health(),
        "ocr": ocr_health(),
        "tts": tts_health(),
    }
    ok = all(c["status"] == "ok" for c in capabilities.values())
    return {"ok": ok, "capabilities": capabilities}
