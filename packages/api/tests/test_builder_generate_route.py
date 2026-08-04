"""生成開始ルート POST /api/builder/sessions/{sid}/generate(specs/19 §4.5)の単体テスト。

builder_generate(start/restart/run)・builder_sessions・demos はモック。状態分岐と BackgroundTask
の予約有無を検査する。生成本体(runtime/OS/DB)は builder_generate 側の単体テストが担う。
"""

import pytest
from fastapi.testclient import TestClient

import service.routes.builder as broute
from service.main import app

client = TestClient(app)

PLAN = {"plan_version": 1, "title": "デモ", "description": "説明", "capabilities": ["chat"]}


@pytest.fixture
def fake(monkeypatch):
    state = {"sessions": {}, "demos": {}, "run": [], "start": [], "restart": []}

    monkeypatch.setattr(broute.builder_sessions, "get_session",
                        lambda owner, sid: state["sessions"].get(sid))
    monkeypatch.setattr(broute.demos, "get_demo", lambda did: state["demos"].get(did))

    def start(owner, session):
        state["start"].append(session["id"])
        return "d-new"

    def restart(did):
        state["restart"].append(did)
        return did

    monkeypatch.setattr(broute.builder_generate, "start", start)
    monkeypatch.setattr(broute.builder_generate, "restart", restart)
    monkeypatch.setattr(broute.builder_generate, "run",
                        lambda did, model_key=None: state["run"].append((did, model_key)))
    return state


def _sess(state, sid="s1", demo_id=None, status="designed", plan=PLAN):
    state["sessions"][sid] = {"id": sid, "demo_id": demo_id, "status": status, "plan": plan}


def test_initial_generate_starts_and_schedules(fake):
    _sess(fake)
    res = client.post("/api/builder/sessions/s1/generate")
    assert res.status_code == 202
    assert res.json() == {"demo_id": "d-new"}
    assert fake["start"] == ["s1"]
    # BackgroundTask 予約 → TestClient で実行。body なし = model 未指定(設定既定)
    assert fake["run"] == [("d-new", None)]


def test_generate_with_model_key_passes_through(fake):
    # SP3-06: body {"model": <生成レジストリ key>} が run へ届く(§4.5)
    _sess(fake)
    res = client.post("/api/builder/sessions/s1/generate",
                      json={"model": "gpt-5.6-sol"})
    assert res.status_code == 202
    assert fake["run"] == [("d-new", "gpt-5.6-sol")]


def test_generate_with_unknown_model_is_422_before_side_effects(fake):
    # SP3-06: 未知キーは fail-closed 422。start/attach の副作用前に遮断
    _sess(fake)
    res = client.post("/api/builder/sessions/s1/generate",
                      json={"model": "gpt-6-nonexistent"})
    assert res.status_code == 422
    assert not fake["start"] and not fake["run"]


def test_generate_with_unknown_body_field_is_422(fake):
    # extra=forbid(プロキシ許可外フィールドの素通り禁止)
    _sess(fake)
    res = client.post("/api/builder/sessions/s1/generate",
                      json={"model": "gpt-oss-120b", "prompt": "evil"})
    assert res.status_code == 422
    assert not fake["start"] and not fake["run"]


def test_regenerate_with_model_key(fake):
    # 再実行(failed → restart)でも選択モデルが run へ届く(§4.5 再実行契約)
    _sess(fake, demo_id="d1")
    fake["demos"]["d1"] = {"id": "d1", "status": "failed"}
    res = client.post("/api/builder/sessions/s1/generate",
                      json={"model": "gpt-5.1-codex-mini"})
    assert res.status_code == 202
    assert fake["restart"] == ["d1"]
    assert fake["run"] == [("d1", "gpt-5.1-codex-mini")]


def test_no_plan_is_409(fake):
    _sess(fake, status="hearing", plan=None)
    res = client.post("/api/builder/sessions/s1/generate")
    assert res.status_code == 409
    assert not fake["start"] and not fake["run"]


def test_designed_but_plan_missing_is_409(fake):
    _sess(fake, status="designed", plan=None)
    assert client.post("/api/builder/sessions/s1/generate").status_code == 409


def test_unknown_session_404(fake):
    assert client.post("/api/builder/sessions/nope/generate").status_code == 404


def test_provisioning_rerun_is_409(fake):
    # §4.5: provisioning 中の再実行は 409(冪等 202 ではない)
    _sess(fake, demo_id="d1")
    fake["demos"]["d1"] = {"id": "d1", "status": "provisioning"}
    res = client.post("/api/builder/sessions/s1/generate")
    assert res.status_code == 409
    assert not fake["run"] and not fake["start"] and not fake["restart"]


def test_ready_demo_is_409(fake):
    _sess(fake, demo_id="d1")
    fake["demos"]["d1"] = {"id": "d1", "status": "ready"}
    assert client.post("/api/builder/sessions/s1/generate").status_code == 409


def test_deleting_demo_is_404(fake):
    _sess(fake, demo_id="d1")
    fake["demos"]["d1"] = {"id": "d1", "status": "deleting"}
    assert client.post("/api/builder/sessions/s1/generate").status_code == 404


def test_missing_demo_row_is_404(fake):
    _sess(fake, demo_id="d1")  # demos に行なし
    assert client.post("/api/builder/sessions/s1/generate").status_code == 404


def test_failed_demo_restarts_and_schedules(fake):
    _sess(fake, demo_id="d1")
    fake["demos"]["d1"] = {"id": "d1", "status": "failed"}
    res = client.post("/api/builder/sessions/s1/generate")
    assert res.status_code == 202 and res.json() == {"demo_id": "d1"}
    assert fake["restart"] == ["d1"]
    assert fake["run"] == [("d1", None)]


def test_busy_maps_to_409(fake, monkeypatch):
    # §4.2 N3: 同時生成上限超過は 409（503 ではない）
    _sess(fake)

    def busy(owner, session):
        raise broute.builder_generate.GenerationBusyError("limit")

    monkeypatch.setattr(broute.builder_generate, "start", busy)
    assert client.post("/api/builder/sessions/s1/generate").status_code == 409


def test_start_conflict_maps_to_409(fake, monkeypatch):
    _sess(fake)

    def conflict(owner, session):
        raise broute.builder_generate.GenerationConflictError("race")

    monkeypatch.setattr(broute.builder_generate, "start", conflict)
    assert client.post("/api/builder/sessions/s1/generate").status_code == 409
