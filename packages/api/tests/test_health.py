"""capability readiness集約(PORT-02)。GET /api/health が chat/rag/dbchat/speech/ocr/tts の
ok/degraded/unavailable + hint を返すことを検証する(FIX-47の/api/rag/healthを踏まえて拡張)。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import bootstrap, health, models, nl2sql, rag, tts
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    # RP状態はコンテナ内でプロセスをまたぐためファイル経由で共有される。テストでは
    # 共有/tmpを汚さないようテストごとの一時ファイルへ向ける(相互汚染も防ぐ)。
    monkeypatch.setattr(bootstrap, "_RP_STATUS_FILE", str(tmp_path / "rp-status.json"))
    get_settings.cache_clear()
    models.clear_unavailable()
    bootstrap._set_resource_principal_status(True)
    yield
    get_settings.cache_clear()
    models.clear_unavailable()
    bootstrap._set_resource_principal_status(True)


def test_chat_health_all_ok_by_default():
    out = health.chat_health()
    assert out["status"] == "ok"
    assert all(m["ok"] for m in out["models"].values())


def test_chat_health_degraded_when_one_model_unavailable():
    models.mark_unavailable("gemini-2.5-pro", "HTTP 404")
    out = health.chat_health()
    assert out["status"] == "degraded"
    assert out["models"]["gemini-2.5-pro"]["ok"] is False
    assert out["models"]["gemini-2.5-pro"]["hint"] == "HTTP 404"


def test_dbchat_health_reflects_semstore_select_ai_and_sample(monkeypatch):
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(nl2sql, "get_schema_info", lambda: {"schema": "SH", "tables": []})
    bootstrap._set_resource_principal_status(True)
    out = health.dbchat_health()
    assert out["semantic_store"]["ok"] is False
    assert "SEMSTORE_OCID" in out["semantic_store"]["hint"]
    assert out["select_ai"]["ok"] is True
    assert out["sample_data"]["ok"] is False
    assert out["status"] == "degraded"  # select_aiは生きているので全滅ではない


def test_dbchat_health_unavailable_when_no_generation_backend_works(monkeypatch):
    """PORT-02レビュー指摘: sample_dataは生成能力ではなく前提条件でしかないため、
    semantic_store/select_aiが両方不可ならsample_dataの成否に関わらずunavailableとする。"""
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(
        nl2sql, "get_schema_info",
        lambda: {"schema": "SH", "tables": [{"name": "SALES"}]},  # sample自体は読める
    )
    bootstrap._set_resource_principal_status(False, "ENABLE_RESOURCE_PRINCIPAL失敗")
    out = health.dbchat_health()
    assert out["semantic_store"]["ok"] is False
    assert out["select_ai"]["ok"] is False
    assert out["sample_data"]["ok"] is True  # サンプル自体は読めるが…
    assert out["status"] == "unavailable"  # …生成経路が無いのでunavailable


def test_dbchat_health_reports_unverified_select_ai_as_not_ok(tmp_path, monkeypatch):
    # PORT-02 F-003: bootstrap未完了(未検証)をokと偽らない — ok=Noneで区別する
    monkeypatch.setattr(bootstrap, "_RP_STATUS_FILE", str(tmp_path / "absent.json"))
    bootstrap._rp_status = {
        "ok": None,
        "hint": "起動時のENABLE_RESOURCE_PRINCIPAL検証が未実行です(bootstrap未完了)",
    }
    out = health.dbchat_health()
    assert out["select_ai"]["ok"] is None
    assert out["select_ai"]["hint"]


def test_resource_principal_status_crosses_processes_via_file(tmp_path, monkeypatch):
    """FIX-58: bootstrap は entrypoint から別プロセスで動くため、結果はファイル経由で
    uvicorn 側へ渡る。プロセス内が未検証でもファイルがあればそれを採用する。"""
    path = tmp_path / "rp-status.json"
    monkeypatch.setattr(bootstrap, "_RP_STATUS_FILE", str(path))
    bootstrap._set_resource_principal_status(True)  # bootstrap プロセス相当
    monkeypatch.setattr(  # uvicorn プロセス相当(プロセス内は未検証のまま)
        bootstrap, "_rp_status", {"ok": None, "hint": "bootstrap未完了"}
    )
    assert bootstrap.resource_principal_status()["ok"] is True
    assert health.dbchat_health()["select_ai"]["ok"] is True


def test_dbchat_health_survives_sample_check_crash(monkeypatch):
    # DB未接続等でsh_sample_status()自体が例外を投げても、/api/health全体は落とさない
    def boom():
        raise RuntimeError("DPY-4000")

    monkeypatch.setattr(nl2sql, "sh_sample_status", boom)
    out = health.dbchat_health()
    assert out["sample_data"]["ok"] is False
    assert "sample_data" in out


def test_speech_health_unavailable_without_bucket(monkeypatch):
    monkeypatch.delenv("SPEECH_BUCKET", raising=False)
    get_settings.cache_clear()
    out = health.speech_health()
    assert out["status"] == "unavailable"


def test_ocr_and_tts_health_unavailable_without_compartment_ocid(monkeypatch):
    # PORT-02レビュー指摘: 全OCI呼び出しに必須のCOMPARTMENT_OCIDが空なら
    # 常時okと偽らない(最低限の設定不備検出)。
    # delenv だけだと Settings が .env(env_file) から拾い直すため、開発機のように
    # .env に COMPARTMENT_OCID がある環境で落ちる。空文字を明示すると env が .env に優先する。
    monkeypatch.setenv("COMPARTMENT_OCID", "")
    get_settings.cache_clear()
    assert health.ocr_health()["status"] == "unavailable"
    tts_out = health.tts_health()
    assert tts_out["status"] == "unavailable"
    assert tts_out["region"]  # region報告自体は維持


def test_ocr_and_tts_health_ok_with_compartment_ocid(monkeypatch):
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(tts, "_last_result", {"ok": None, "region": None, "hint": None, "at": None})
    monkeypatch.setattr(
        tts, "probe", lambda *a, **k: {
            "ok": True, "region": "us-phoenix-1", "hint": None,
            "at": None,
        }
    )
    get_settings.cache_clear()
    assert health.ocr_health()["status"] == "ok"
    assert health.tts_health()["status"] == "ok"


def test_rag_health_aggregation_disables_autocreate(monkeypatch):
    """PORT-02レビュー指摘: /api/healthはGETポーリングされうるため、
    rag.health_check()をallow_autocreate=Falseで呼び、project未解決時に新規作成しない。"""
    seen = {}

    def fake_health_check(**kw):
        seen.update(kw)
        return {"ok": True, "checks": {}}

    monkeypatch.setattr(rag, "health_check", fake_health_check)
    health._rag_health()
    assert seen == {"allow_autocreate": False}


def test_capability_health_endpoint_returns_all_six(monkeypatch):
    monkeypatch.setattr(rag, "health_check", lambda **kw: {"ok": True, "checks": {}})
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert set(body["capabilities"]) == {"chat", "rag", "dbchat", "speech", "ocr", "tts"}
    assert isinstance(body["ok"], bool)


def test_capability_health_endpoint_degrades_instead_of_crashing_on_rag_failure(monkeypatch):
    # PORT-02 F-003: RAG個別の想定外失敗は/api/health全体を巻き込まず、
    # rag capabilityをunavailableとして構造化して返す(500/503を漏らさない)。
    def boom(**kw):
        raise RuntimeError("cp down")

    monkeypatch.setattr(rag, "health_check", boom)
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["capabilities"]["rag"]["status"] == "unavailable"
    assert "RuntimeError" in body["capabilities"]["rag"]["hint"]


def test_capability_health_endpoint_503_on_true_crash(monkeypatch):
    # 集約処理自体が壊れた場合の最後の防衛線(ルート側のtry/except)は維持する。
    def boom():
        raise RuntimeError("aggregation itself broken")

    monkeypatch.setattr(health, "capability_health", boom)
    res = client.get("/api/health")
    assert res.status_code == 503


def test_tts_health_keeps_region_single_valued_and_lists_candidates(monkeypatch):
    """F-005: region は従来どおり単一のリージョン文字列(既存クライアント互換)。
    候補一覧は新フィールド candidate_regions で返す。"""
    monkeypatch.setenv("OCI_REGION", "us-chicago-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(tts, "_resolved_region", None)
    monkeypatch.setattr(tts, "_last_result", {"ok": None, "region": None, "hint": None, "at": None})
    monkeypatch.setattr(
        tts, "probe", lambda *a, **k: {
            "ok": True, "region": "us-chicago-1", "hint": None,
            "at": None,
        }
    )
    get_settings.cache_clear()
    out = health.tts_health()
    assert out["region"] == "us-chicago-1"  # カンマ連結しない
    assert out["candidate_regions"] == ["us-chicago-1", "us-phoenix-1"]
    assert out["verified"] is True


def test_tts_health_reports_unavailable_after_real_failure(monkeypatch):
    """F-007: 設定が揃っていても実際に到達できなければ ok と言わない。"""
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(
        tts, "_last_result", {"ok": False, "region": None, "hint": "古い失敗", "at": None}
    )
    monkeypatch.setattr(
        tts, "probe", lambda *a, **k: {"ok": False, "region": None, "hint": "未購読の可能性"}
    )
    get_settings.cache_clear()
    out = health.tts_health()
    assert out["status"] == "unavailable"
    assert "未購読" in out["hint"]


def test_tts_health_recovers_after_transient_synthesis_failure(monkeypatch):
    """一度の合成失敗を無期限に引きずらない: 実測プローブが通れば ok に戻る。"""
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(
        tts, "_last_result", {"ok": False, "region": "us-chicago-1", "hint": "一時障害", "at": None}
    )
    monkeypatch.setattr(
        tts, "probe", lambda *a, **k: {
            "ok": True, "region": "us-chicago-1", "hint": None,
            "at": None,
        }
    )
    get_settings.cache_clear()
    out = health.tts_health()
    assert out["status"] == "ok"
    assert out["verified"] is True


def test_tts_health_ok_and_verified_after_successful_synthesis(monkeypatch):
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(
        tts, "_last_result", {"ok": True, "region": "us-chicago-1", "hint": None, "at": None}
    )
    get_settings.cache_clear()
    out = health.tts_health()
    assert out["status"] == "ok"
    assert out["verified"] is True
    assert out["region"] == "us-chicago-1"


def test_tts_health_uses_non_billable_probe_when_process_never_synthesized(monkeypatch):
    """F-007後追い: /api/tts は Functions、/api/health は Container Instance が返すため
    実合成の結果は health のプロセスに届かない。list_voices（合成しない=課金なし）で
    到達性を実測し、届かなければ ok と言わない。"""
    monkeypatch.setenv("COMPARTMENT_OCID", "ocid1.compartment.oc1..x")
    monkeypatch.setattr(tts, "_last_result", {"ok": None, "region": None, "hint": None, "at": None})
    monkeypatch.setattr(
        tts, "probe", lambda *a, **k: {"ok": False, "region": None, "hint": "到達できません"}
    )
    get_settings.cache_clear()
    out = health.tts_health()
    assert out["status"] == "unavailable"
    assert out["verified"] is False
    assert "到達できません" in out["hint"]


def test_tts_probe_caches_and_falls_back_across_regions(monkeypatch):
    monkeypatch.setenv("OCI_REGION", "ap-osaka-1")
    monkeypatch.delenv("TTS_REGION", raising=False)
    monkeypatch.setattr(tts, "_resolved_region", None)
    monkeypatch.setattr(tts, "_probe_cache", None)
    get_settings.cache_clear()
    calls: list[str] = []

    def client(region, timeout=None):
        calls.append(region)
        c = type("C", (), {})()
        voice = type("V", (), {"voice_id": "Yuki", "language_code": "ja-JP",
                               "supported_models": ["TTS_2_NATURAL"]})()
        data = type("D", (), {"items": [voice]})()
        if region == "ap-osaka-1":
            c.list_voices = lambda **kw: (_ for _ in ()).throw(RuntimeError("404"))
        else:
            c.list_voices = lambda **kw: type("R", (), {"data": data})()
        return c

    monkeypatch.setattr(tts, "_speech_client", client)
    assert tts.probe()["region"] == "us-phoenix-1"
    n = len(calls)
    tts.probe()  # キャッシュが効き再問い合わせしない
    assert len(calls) == n
