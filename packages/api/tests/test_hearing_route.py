"""ヒアリングフロー API(HBD-01)のルートテスト。リポジトリは fake、推薦は実関数。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

import jetuse_core.hearing_genai as hg
import service.main as service_main
from jetuse_core.hearing_schema import (
    SESSION_STATUSES,
    HearingSchemaError,
    validate_answer,
)
from jetuse_core.recommend import Recommendation
from service.main import app

client = TestClient(app)

FULL = {
    "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
    "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
}


class FakeHearingRepo:
    """所有権・upsert・推薦保存を最小限に模した in-memory リポジトリ。"""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.answers: dict[str, dict[str, Any]] = {}
        self.recs: dict[str, dict] = {}
        self.launches: dict[str, dict] = {}
        self._n = 0

    def _own(self, owner, sid):
        s = self.sessions.get(sid)
        return s if s and s["owner_sub"] == owner else None

    def create_session(self, owner, input_notes=None):
        self._n += 1
        sid = f"s{self._n}"
        self.sessions[sid] = {
            "id": sid, "owner_sub": owner, "status": "draft", "input_notes": input_notes
        }
        self.answers[sid] = {}
        return {"id": sid, "owner_sub": owner, "status": "draft", "input_notes": input_notes}

    def list_sessions(self, owner):
        return [
            {"id": s["id"], "status": s["status"]}
            for s in self.sessions.values() if s["owner_sub"] == owner
        ]

    def get_session(self, owner, sid):
        s = self._own(owner, sid)
        if not s:
            return None
        return {
            **s,
            "answers": [
                {"question_id": q, "value": v["value"], "source": v["source"]}
                for q, v in self.answers[sid].items()
            ],
            "recommendation": self.recs.get(sid),
        }

    def update_session(self, owner, sid, *, status=None, input_notes=None):
        s = self._own(owner, sid)
        if not s:
            return None
        if status is not None:
            if status not in SESSION_STATUSES:  # 実 repo と同じ契約(F-005)
                raise HearingSchemaError(f"未知の status: {status!r}")
            if status == "confirmed":  # 汎用 PATCH からの確定遷移は拒否
                raise HearingSchemaError("status='confirmed' は confirm 経由のみ")
            s["status"] = status
        if input_notes is not None:
            s["input_notes"] = input_notes
        return self.get_session(owner, sid)

    def delete_session(self, owner, sid):
        if self._own(owner, sid):
            del self.sessions[sid]
            return True
        return False

    def save_answer(self, owner, sid, qid, value, *, source="sa"):
        if not self._own(owner, sid):
            return None
        normalized = validate_answer(qid, value)  # 実検証(未知選択肢を弾く)
        self.answers[sid][qid] = {"value": normalized, "source": source}
        # 回答変更で陳腐化推薦＋起動記録を削除し、確定済みなら status を ready へ(実 repo 契約)。
        self.recs.pop(sid, None)
        self.launches.pop(sid, None)
        if self.sessions[sid]["status"] == "confirmed":
            self.sessions[sid]["status"] = "ready"
        return {"question_id": qid, "value": normalized, "source": source}

    def get_answers(self, owner, sid):
        if not self._own(owner, sid):
            return None
        return {q: v["value"] for q, v in self.answers[sid].items()}

    def save_recommendation(self, owner, sid, rec: Recommendation):
        if not self._own(owner, sid):
            return None
        detail = {**rec.model_dump(), "confirmed_at": None}
        self.recs[sid] = detail
        # 推薦保存で status を整える(実 repo 契約): draft→ready へ進め、confirmed→ready へ戻す。
        if self.sessions[sid]["status"] in ("draft", "confirmed"):
            self.sessions[sid]["status"] = "ready"
        # 再推薦は確定を解除(confirmed_at=None)するので、旧推薦に基づく起動記録も無効化する。
        self.launches.pop(sid, None)
        return detail

    def confirm_recommendation(self, owner, sid):
        if not self._own(owner, sid) or sid not in self.recs:
            return "not_found"
        if self.recs[sid].get("sample_app") is None:
            return "unresolved"
        self.recs[sid]["confirmed_at"] = "2026-01-01T00:00:00"
        self.sessions[sid]["status"] = "confirmed"
        return "confirmed"

    def record_launch(self, owner, sid, *, sample_app, instance_id, entry_slot,
                      demo_url, composition):
        if not self._own(owner, sid):
            return None
        self.launches[sid] = {
            "id": f"l-{sid}", "session_id": sid, "sample_app": sample_app,
            "instance_id": instance_id, "entry_slot": entry_slot, "demo_url": demo_url,
            "composition": composition, "status": "launched",
            "launched_at": "2026-01-01T00:00:00",
        }
        return self.launches[sid]

    def get_launch(self, owner, sid):
        if not self._own(owner, sid):
            return None
        return self.launches.get(sid)


@pytest.fixture
def repo(monkeypatch):
    fake = FakeHearingRepo()
    for name in (
        "create_session", "list_sessions", "get_session", "update_session",
        "delete_session", "save_answer", "get_answers", "save_recommendation",
        "confirm_recommendation", "record_launch", "get_launch",
    ):
        monkeypatch.setattr(service_main.hearing_repo, name, getattr(fake, name))
    # 実DBへ触れないことを保証する: 万一 fake 未差し替えの repo 関数が呼ばれても
    # connect() で即座に失敗させ、暗黙の実DB接続を防ぐ(F-004)。
    def _no_db(*a, **k):
        raise AssertionError("route test must not touch the real DB")

    monkeypatch.setattr(service_main.hearing_repo, "connect", _no_db)
    return fake


def _create(notes=None):
    r = client.post("/api/hearing/sessions", json={"input_notes": notes})
    assert r.status_code == 200
    return r.json()["id"]


def test_get_questions():
    r = client.get("/api/hearing/questions")
    assert r.status_code == 200
    body = r.json()
    assert body["questions"][0]["id"] == "Q1"
    assert len(body["questions"]) == 7


def test_session_crud_and_answers(repo):
    sid = _create("メモ: サポート部門")
    # 回答保存
    for qid, val in FULL.items():
        r = client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
        assert r.status_code == 200, (qid, r.text)
    # 取得で回答が見える
    got = client.get(f"/api/hearing/sessions/{sid}").json()
    assert {a["question_id"] for a in got["answers"]} == set(FULL)
    # 一覧
    assert any(s["id"] == sid for s in client.get("/api/hearing/sessions").json()["sessions"])
    # 更新
    r = client.patch(f"/api/hearing/sessions/{sid}", json={"status": "ready"})
    assert r.json()["status"] == "ready"
    # 削除
    assert client.delete(f"/api/hearing/sessions/{sid}").status_code == 200
    assert client.get(f"/api/hearing/sessions/{sid}").status_code == 404


def test_answer_upsert_replaces(repo):
    sid = _create()
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "support"})
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "sales"})
    answers = client.get(f"/api/hearing/sessions/{sid}").json()["answers"]
    q1 = [a for a in answers if a["question_id"] == "Q1"]
    assert len(q1) == 1 and q1[0]["value"] == "sales"


def test_answer_invalid_choice_422(repo):
    sid = _create()
    r = client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "bogus"})
    assert r.status_code == 422


def test_public_answer_save_forces_source_sa(repo):
    """公開の手入力保存はクライアントの source 指定を無視し常に 'sa'(監査区分の保護 / F-001)。"""
    sid = _create()
    r = client.put(
        f"/api/hearing/sessions/{sid}/answers/Q1",
        json={"value": "support", "source": "genai_suggested"},
    )
    assert r.status_code == 200
    assert r.json()["source"] == "sa"
    got = client.get(f"/api/hearing/sessions/{sid}").json()["answers"]
    assert next(a for a in got if a["question_id"] == "Q1")["source"] == "sa"


def test_recommend_happy_path(repo):
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    r = client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert r.status_code == 200
    body = r.json()
    assert body["sample_app"] == "SBA-A"
    assert set(body["ai_parts"]) == {"rag.search", "summarize", "classify"}
    assert body["connectors"] == ["slack"]
    assert body["needs_genai_nearest"] is False
    # 取得でも推薦が見える
    assert client.get(f"/api/hearing/sessions/{sid}").json()["recommendation"]["ui"] == "chat"


def test_preview_composes_from_saved_recommendation(repo):
    """推薦→/preview で合成したデモ構成(画面・組込点・使うAI・データ)が返る(HBD-03)。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    r = client.post(f"/api/hearing/sessions/{sid}/preview")
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["ok"] is True
    assert comp["sample_app"] == "SBA-A"
    assert comp["instance_id"] == "builtin-sba-a"
    assert "rag.search" in comp["active_parts"]
    assert {s["key"] for s in comp["screens"]} >= {"faq", "inbox", "console"}
    assert comp["composition_report"]["ok"] is True  # 配布表現は再検証可能


def test_preview_without_recommendation_409(repo):
    sid = _create()
    assert client.post(f"/api/hearing/sessions/{sid}/preview").status_code == 409


def test_preview_unknown_session_404(repo):
    assert client.post("/api/hearing/sessions/nope/preview").status_code == 404


def test_validate_gate_passes_valid_composition(repo):
    """推薦→/validate で構成と合成バリデーション(ガバナンス4制約)が返る。妥当なら ok。HBD-04。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    r = client.post(f"/api/hearing/sessions/{sid}/validate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["composition"]["sample_app"] == "SBA-A"
    gov = body["governance"]
    assert gov["ok"] is True
    assert gov["violations"] == []
    assert all(gov["checks"].values())


def test_validate_gate_flags_disallowed_combination(repo):
    """未実装 SBA(Q1=accounting→SBA-D)に着地する構成は合成不能で弾かれ、代替提案が返る。

    自動フィットにより業務×AI 不一致は合成側で吸収される(対象外として除外)ため、validate が
    FAIL を返す経路は「主 SBA を解決できない」unresolved_composition に集約される。"""
    sid = _create()
    answers = {**FULL, "Q1": "accounting"}
    for qid, val in answers.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    r = client.post(f"/api/hearing/sessions/{sid}/validate")
    assert r.status_code == 200, r.text
    gov = r.json()["governance"]
    assert gov["ok"] is False
    nl = next(
        v for v in gov["violations"]
        if v["kind"] == "unresolved_composition"
    )
    assert nl["alternative"]  # 外させない代替提案


def test_validate_without_recommendation_409(repo):
    sid = _create()
    assert client.post(f"/api/hearing/sessions/{sid}/validate").status_code == 409


def test_validate_unknown_session_404(repo):
    assert client.post("/api/hearing/sessions/nope/validate").status_code == 404


def test_recommend_incomplete_answers_422(repo):
    sid = _create()
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "support"})
    assert client.post(f"/api/hearing/sessions/{sid}/recommend").status_code == 422


def test_recommend_empty_required_multi_422(repo):
    """Q2(必須 multi)が空配列のまま recommend すると 422(素地未決の穴を塞ぐ / F-002)。"""
    sid = _create()
    for qid, val in {**FULL, "Q2": []}.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    assert client.post(f"/api/hearing/sessions/{sid}/recommend").status_code == 422


def test_answer_change_drops_stale_recommendation(repo):
    """確定後に回答を変えると陳腐化推薦は削除され、再推薦前の confirm は 404(F-002)。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.post(f"/api/hearing/sessions/{sid}/recommend/confirm").status_code == 200
    # 回答変更で推薦が消える → GET は recommendation なし
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "sales"})
    assert sid not in repo.recs
    assert client.get(f"/api/hearing/sessions/{sid}").json()["recommendation"] is None
    # 再推薦前の confirm は 404(陳腐化推薦の再確定は構造的に不能)
    assert client.post(f"/api/hearing/sessions/{sid}/recommend/confirm").status_code == 404
    # 再推薦すれば再び確定可能
    body = client.post(f"/api/hearing/sessions/{sid}/recommend").json()
    assert body["confirmed_at"] is None and body["sample_app"] == "SBA-C"
    assert client.post(f"/api/hearing/sessions/{sid}/recommend/confirm").status_code == 200


def test_re_recommend_after_confirm_reverts_status(repo):
    """確定後に再推薦すると status が confirmed→ready に戻る(status/推薦の整合 / review-8)。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    client.post(f"/api/hearing/sessions/{sid}/recommend/confirm")
    assert client.get(f"/api/hearing/sessions/{sid}").json()["status"] == "confirmed"
    # 再推薦で確定解除 → status は ready、confirmed_at は None
    body = client.post(f"/api/hearing/sessions/{sid}/recommend").json()
    assert body["confirmed_at"] is None
    assert client.get(f"/api/hearing/sessions/{sid}").json()["status"] == "ready"


def test_patch_cannot_set_confirmed_status(repo):
    """汎用 PATCH では status='confirmed' へ遷移できない(確定ゲート迂回の防止 / review-7)。"""
    sid = _create()
    assert client.patch(
        f"/api/hearing/sessions/{sid}", json={"status": "confirmed"}
    ).status_code == 422


def test_update_session_clears_notes_with_empty_string(repo):
    """input_notes は空文字でクリアできる(未指定=据え置き / 空文字=クリアの明示 / F-003)。"""
    sid = _create("初期メモ")
    # 空文字でクリア
    body = client.patch(f"/api/hearing/sessions/{sid}", json={"input_notes": ""}).json()
    assert body["input_notes"] == ""
    # input_notes を送らない更新はメモを据え置く
    client.patch(f"/api/hearing/sessions/{sid}", json={"input_notes": "再設定"})
    body = client.patch(f"/api/hearing/sessions/{sid}", json={"status": "ready"}).json()
    assert body["input_notes"] == "再設定"


def test_update_session_unknown_status_422(repo):
    sid = _create()
    assert client.patch(
        f"/api/hearing/sessions/{sid}", json={"status": "bogus"}
    ).status_code == 422


def test_recommend_unknown_session_404(repo):
    assert client.post("/api/hearing/sessions/nope/recommend").status_code == 404


def test_confirm_recommendation(repo):
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    r = client.post(f"/api/hearing/sessions/{sid}/recommend/confirm")
    assert r.status_code == 200 and r.json()["confirmed"] is True
    # 確定でセッション status が confirmed へ遷移する。
    assert client.get(f"/api/hearing/sessions/{sid}").json()["status"] == "confirmed"


def test_confirm_unresolved_recommendation_409(repo):
    """Q1=other(主SBA未確定)の推薦は確定を 409 で拒否する。"""
    sid = _create("製造現場の保全管理")
    for qid, val in {**FULL, "Q1": "other"}.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.post(f"/api/hearing/sessions/{sid}/recommend/confirm").status_code == 409


def test_suggest_does_not_overwrite_existing_answer(repo, monkeypatch):
    """SA 既答は GenAI 提案で上書きしない(skipped_existing に記録)。"""
    monkeypatch.setattr(
        hg, "suggest_answers_from_notes",
        lambda notes, *, model_key: {"Q1": "support", "Q2": ["docs"]},
    )
    sid = _create("メモ")
    # SA が手入力で Q1=sales を確定済み
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "sales"})
    body = client.post(f"/api/hearing/sessions/{sid}/suggest").json()
    assert body["skipped_existing"] == ["Q1"]
    assert body["saved"] == ["Q2"]
    got = client.get(f"/api/hearing/sessions/{sid}").json()["answers"]
    answers = {a["question_id"]: a for a in got}
    assert answers["Q1"]["value"] == "sales" and answers["Q1"]["source"] == "sa"


def test_suggest_saves_genai_suggested(repo, monkeypatch):
    monkeypatch.setattr(
        hg, "suggest_answers_from_notes",
        lambda notes, *, model_key: {"Q1": "support", "Q2": ["docs"]},
    )
    sid = _create("サポート部門。社内マニュアルで回答したい")
    body = client.post(f"/api/hearing/sessions/{sid}/suggest").json()
    assert body["genai"] == "ok"
    assert set(body["saved"]) == {"Q1", "Q2"}
    # 保存された回答は source=genai_suggested。
    got = client.get(f"/api/hearing/sessions/{sid}").json()["answers"]
    answers = {a["question_id"]: a for a in got}
    assert answers["Q1"]["source"] == "genai_suggested"
    assert answers["Q2"]["value"] == ["docs"]


def test_suggest_re_proposes_over_prior_genai(repo, monkeypatch):
    """過去の genai_suggested 回答は再提案で更新する(手入力 'sa' だけ skip / review-9)。"""
    monkeypatch.setattr(
        hg, "suggest_answers_from_notes", lambda notes, *, model_key: {"Q1": "sales"}
    )
    sid = _create("商談・案件の管理")
    # 1回目: GenAI が Q1=support を提案・保存
    repo.answers[sid]["Q1"] = {"value": "support", "source": "genai_suggested"}
    body = client.post(f"/api/hearing/sessions/{sid}/suggest").json()
    assert body["saved"] == ["Q1"] and body["skipped_existing"] == []


def test_recommend_advances_draft_to_ready(repo):
    """draft セッションで推薦を保存すると status が ready へ進む(review-9)。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    assert client.get(f"/api/hearing/sessions/{sid}").json()["status"] == "draft"
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.get(f"/api/hearing/sessions/{sid}").json()["status"] == "ready"


def test_suggest_genai_unavailable_is_soft(repo, monkeypatch):
    # GenAI 失敗時は空提案で 200(フォールバック)。決定ルールの推薦経路は無傷。
    monkeypatch.setattr(hg, "suggest_answers_from_notes", lambda notes, *, model_key: {})
    sid = _create("メモ")
    body = client.post(f"/api/hearing/sessions/{sid}/suggest").json()
    assert body["genai"] == "no_suggestions" and body["saved"] == []


def test_recommend_other_includes_nearest_advisory(repo, monkeypatch):
    monkeypatch.setattr(hg, "nearest_sample_app", lambda notes, *, model_key: "SBA-C")
    sid = _create("新規事業の商談・案件を管理したい")
    for qid, val in {**FULL, "Q1": "other"}.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    body = client.post(f"/api/hearing/sessions/{sid}/recommend").json()
    assert body["sample_app"] is None  # 決定ルールは None を保持
    assert body["needs_genai_nearest"] is True
    assert body["genai_nearest_sample_app"] == "SBA-C"  # GenAI は助言として添える


def test_other_owner_cannot_access(repo):
    # fake は require_user=dev-user 固定のため、所有権分岐は repo 単体テストで担保。
    # ここでは未知セッションが 404 になることだけ確認する。
    assert client.get("/api/hearing/sessions/unknown").status_code == 404
    assert client.put(
        "/api/hearing/sessions/unknown/answers/Q1", json={"value": "support"}
    ).status_code == 404


def test_input_notes_bound_rejected_by_repo():
    # repo の _bound_notes は HearingSchemaError を投げる(route 経由でも 422 になる)。
    from jetuse_core import hearing as real_repo

    with pytest.raises(HearingSchemaError):
        real_repo._bound_notes("x" * 9000)


# --- HBD-05: デモ起動 + 構成サマリ -----------------------------------------


def _confirm_full(sid, answers=None):
    """FULL 回答→recommend→confirm までを通す(launch/summary の前提)。"""
    for qid, val in (answers or FULL).items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.post(f"/api/hearing/sessions/{sid}/recommend/confirm").status_code == 200


def test_launch_happy_path_persists_and_returns_run_target(repo):
    """確定→/launch でガバナンス PASS の構成が起動記録され、主役 AI 実行導線が返る(一気通貫)。"""
    sid = _create()
    _confirm_full(sid)
    r = client.post(f"/api/hearing/sessions/{sid}/launch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance"]["ok"] is True
    launch = body["launch"]
    assert launch["instance_id"] == "builtin-sba-a"
    assert launch["demo_url"] == "/sba/builtin-sba-a"
    # 主役(rag.search)の active スロットが実行起点に選ばれる。
    assert launch["entry_slot"]
    assert launch["status"] == "launched"
    # 永続: GET /launch で再取得できる。
    got = client.get(f"/api/hearing/sessions/{sid}/launch")
    assert got.status_code == 200
    assert got.json()["instance_id"] == "builtin-sba-a"


def test_launch_requires_confirmation_409(repo):
    """未確定の推薦は起動できない(409。一気通貫は確定を経由する)。"""
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.post(f"/api/hearing/sessions/{sid}/launch").status_code == 409


def test_launch_blocked_when_governance_fails(repo):
    """境界: governance FAIL 構成は起動に進めず、代替提案つき違反が返る。

    自動フィットで業務×AI 不一致は合成側で吸収されるため、残る FAIL 経路=未実装 SBA
    (Q1=accounting→SBA-D)で合成不能(unresolved_composition)を起こしてゲートを検証する。"""
    sid = _create()
    _confirm_full(sid, {**FULL, "Q1": "accounting"})
    r = client.post(f"/api/hearing/sessions/{sid}/launch")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["governance"]["ok"] is False
    v = next(
        v for v in detail["governance"]["violations"]
        if v["kind"] == "unresolved_composition"
    )
    assert v["alternative"]  # 外させない代替提案(最近傍 SBA へ誘導)
    # 起動記録は作られない。
    assert client.get(f"/api/hearing/sessions/{sid}/launch").status_code == 404


def test_launch_unknown_session_404(repo):
    assert client.post("/api/hearing/sessions/nope/launch").status_code == 404


def test_answer_change_invalidates_launch(repo):
    """起動後に回答を変えると陳腐化した起動記録は無効化され GET /launch が 404 に戻る(F-002)。"""
    sid = _create()
    _confirm_full(sid)
    assert client.post(f"/api/hearing/sessions/{sid}/launch").status_code == 200
    assert client.get(f"/api/hearing/sessions/{sid}/launch").status_code == 200
    # 回答変更 → 推薦も起動記録も陳腐化 → GET /launch は 404。
    client.put(f"/api/hearing/sessions/{sid}/answers/Q1", json={"value": "sales"})
    assert client.get(f"/api/hearing/sessions/{sid}/launch").status_code == 404


def test_re_recommend_invalidates_launch(repo):
    """再推薦(確定解除)で旧起動記録が無効化される(F-002)。"""
    sid = _create()
    _confirm_full(sid)
    assert client.post(f"/api/hearing/sessions/{sid}/launch").status_code == 200
    client.post(f"/api/hearing/sessions/{sid}/recommend")  # 再推薦 → confirmed_at=None
    assert client.get(f"/api/hearing/sessions/{sid}/launch").status_code == 404


def test_get_launch_before_launch_404(repo):
    sid = _create()
    _confirm_full(sid)
    assert client.get(f"/api/hearing/sessions/{sid}/launch").status_code == 404


def test_summary_genai_narrative(repo, monkeypatch):
    """確定→/summary で 4 項目(構成図/OCIサービス/手順/効果)が返り、効果は GenAI 文章化される。"""
    monkeypatch.setattr(
        hg, "summary_narrative", lambda comp, *, model_key: "顧客提示用の効果文（GenAI）。"
    )
    sid = _create()
    _confirm_full(sid)
    r = client.post(f"/api/hearing/sessions/{sid}/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_app"] == "SBA-A"
    assert body["diagram"]  # ①構成図(どのデータに何のAIが効くか)
    assert body["oci_services"]  # ②使うOCIサービス
    assert body["steps"]  # ③デモ手順
    assert body["impact_source"] == "genai"
    assert "GenAI" in body["impact"]
    # active な主役 rag.search が構成図に現れる。
    assert any(f["capability"] == "rag.search" for f in body["diagram"])
    assert body["markdown"].startswith("# 構成サマリ")


def test_summary_falls_back_when_genai_unavailable(repo, monkeypatch):
    """GenAI 不在/失敗でも決定的フォールバックでサマリは成立する(構成図/手順は常に決定的)。"""
    monkeypatch.setattr(hg, "summary_narrative", lambda comp, *, model_key: None)
    sid = _create()
    _confirm_full(sid)
    body = client.post(f"/api/hearing/sessions/{sid}/summary").json()
    assert body["impact_source"] == "deterministic"
    assert body["impact"]
    assert body["oci_services"]


def test_summary_blocked_when_governance_fails(repo):
    """境界: ガバナンス FAIL の構成では構成サマリを生成できない(409＋代替提案 / F-003)。"""
    sid = _create()
    _confirm_full(sid, {**FULL, "Q1": "accounting"})  # SBA-D 未実装 → 合成不能 → governance FAIL
    r = client.post(f"/api/hearing/sessions/{sid}/summary")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["governance"]["ok"] is False
    # エクスポートも同様に拒否される。
    assert client.get(f"/api/hearing/sessions/{sid}/summary/export").status_code == 409


def test_summary_requires_confirmation_409(repo):
    sid = _create()
    for qid, val in FULL.items():
        client.put(f"/api/hearing/sessions/{sid}/answers/{qid}", json={"value": val})
    client.post(f"/api/hearing/sessions/{sid}/recommend")
    assert client.post(f"/api/hearing/sessions/{sid}/summary").status_code == 409


def test_summary_export_markdown(repo):
    """エクスポートは text/markdown を添付ダウンロードで返す(プリセールス転用)。"""
    sid = _create()
    _confirm_full(sid)
    r = client.get(f"/api/hearing/sessions/{sid}/summary/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "## ① 構成図" in r.text
    assert "## ② 使う OCI サービス" in r.text
    assert "## ④ 想定効果" in r.text


def test_summary_unknown_session_404(repo):
    assert client.post("/api/hearing/sessions/nope/summary").status_code == 404
    assert client.get("/api/hearing/sessions/nope/summary/export").status_code == 404
