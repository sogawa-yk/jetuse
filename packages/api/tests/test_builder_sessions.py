"""ビルダー・ヒアリング API(SP3-01 / specs/19 §2)の単体テスト。

builder_sessions リポジトリは in-memory fake(SQL の所有者強制・demo_id IS NULL ガードを
再現)、LLM は builder_hearing._complete の fake。ルート・決定的再検査・上限は実物。
"""

import json

import pytest
from fastapi.testclient import TestClient

import jetuse_core.builder_hearing as hearing
import jetuse_core.builder_sessions as repo
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)

SESSION_OUT_KEYS = {"id", "status", "transcript", "requirements", "plan",
                    "demo_id", "demo_status", "created_at", "updated_at"}

FULL_REQ = {
    "industry": "製造",
    "use_case": "設備保全のナレッジ検索",
    "capabilities_hint": ["rag.search"],
    "data_profile": {"documents": "保全マニュアル"},
    "notes": None,
}


def llm_json(reply="了解です", requirements=None, sufficient=False, missing=None):
    return json.dumps({
        "reply": reply,
        "requirements": requirements or {},
        "sufficient": sufficient,
        "missing": missing or [],
    }, ensure_ascii=False)


class FakeBuilderSessions:
    """jetuse_core.builder_sessions と同じ契約(WHERE owner_sub / demo_id IS NULL)。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.seq = 0

    def _now(self):
        self.seq += 1
        return f"2026-07-07T00:00:{self.seq:02d}"

    def create_session(self, owner):
        sid = f"s{len(self.rows) + 1}"
        now = self._now()
        self.rows[sid] = {
            "id": sid, "owner_sub": owner, "status": "hearing", "transcript": [],
            "requirements": None, "plan": None, "demo_id": None,
            "created_at": now, "updated_at": now,
        }
        return self.get_session(owner, sid)

    def get_session(self, owner, sid):
        r = self.rows.get(sid)
        if not r or r["owner_sub"] != owner:
            return None
        out = {k: v for k, v in r.items() if k != "owner_sub"}
        out["demo_status"] = None  # demos JOIN(demo_id なしのため常に None)
        return out

    def save_hearing_turn(self, owner, sid, transcript, requirements, expected_len):
        r = self.rows.get(sid)
        if (
            not r or r["owner_sub"] != owner or r["demo_id"] is not None
            or len(r["transcript"]) != expected_len  # 楽観ロック(JSON 配列長の WHERE)
        ):
            return False
        r["transcript"] = transcript
        r["requirements"] = requirements
        r["updated_at"] = self._now()
        return True


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    fake = FakeBuilderSessions()
    for name in ("create_session", "get_session", "save_hearing_turn"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    yield fake


@pytest.fixture()
def fake_llm(monkeypatch):
    """fake LLM: 呼び出しごとに canned 出力を順に返す。"""
    state = {"outputs": [], "calls": []}

    def _complete(messages):
        state["calls"].append(messages)
        return state["outputs"].pop(0), {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(hearing, "_complete", _complete)
    return state


def _create_sid():
    res = client.post("/api/builder/sessions")
    assert res.status_code == 200
    return res.json()["id"]


# --- セッション作成 / 取得(specs/19 §2.4) ---


def test_create_session_shape():
    res = client.post("/api/builder/sessions")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == SESSION_OUT_KEYS  # owner_sub は返さない
    assert body["status"] == "hearing"
    assert body["transcript"] == [] and body["requirements"] is None
    assert body["demo_id"] is None and body["demo_status"] is None


def test_get_session_roundtrip():
    sid = _create_sid()
    res = client.get(f"/api/builder/sessions/{sid}")
    assert res.status_code == 200
    assert res.json()["id"] == sid


def test_cross_user_session_is_404(fake_repo):
    """越境は存在秘匿の 404(demos と同形 — specs/19 §2.4)。"""
    fake_repo.rows["theirs"] = {
        "id": "theirs", "owner_sub": "user-a", "status": "hearing", "transcript": [],
        "requirements": None, "plan": None, "demo_id": None,
        "created_at": "2026-07-07T00:00:00", "updated_at": "2026-07-07T00:00:00",
    }
    assert client.get("/api/builder/sessions/theirs").status_code == 404
    res = client.post("/api/builder/sessions/theirs/messages", json={"content": "hi"})
    assert res.status_code == 404
    assert res.json()["detail"] == "session not found"
    assert client.get("/api/builder/sessions/no-such").status_code == 404


def test_unauthenticated_is_401(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        assert client.post("/api/builder/sessions").status_code == 401
        assert client.get("/api/builder/sessions/x").status_code == 401
        res = client.post("/api/builder/sessions/x/messages", json={"content": "hi"})
        assert res.status_code == 401
    finally:
        get_settings.cache_clear()


# --- ヒアリング往復(specs/19 §2.2・§2.3) ---


def test_hearing_insufficient_then_followup(fake_llm):
    """不足 → 追質問。LLM の sufficient=false はそのまま返る。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(
        reply="どの業種のデモですか?",
        requirements={"use_case": "ナレッジ検索"},
        sufficient=False, missing=["industry", "data_profile"],
    )]
    res = client.post(f"/api/builder/sessions/{sid}/messages",
                      json={"content": "ナレッジ検索のデモを作りたい"})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"reply", "requirements", "sufficient", "missing"}
    assert body["sufficient"] is False
    assert "industry" in body["missing"]
    # transcript に user/assistant の 1 往復が積まれる
    got = client.get(f"/api/builder/sessions/{sid}").json()
    assert [m["role"] for m in got["transcript"]] == ["user", "assistant"]
    assert got["transcript"][1]["content"] == "どの業種のデモですか?"


def test_hearing_sufficient_roundtrip(fake_llm):
    """充足 → sufficient=true。requirements が保存され GET で見える。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(
        reply="要件が揃いました", requirements=FULL_REQ, sufficient=True)]
    res = client.post(f"/api/builder/sessions/{sid}/messages",
                      json={"content": "製造業の設備保全デモ。文書は保全マニュアル"})
    assert res.status_code == 200
    body = res.json()
    assert body["sufficient"] is True and body["missing"] == []
    assert body["requirements"]["industry"] == "製造"
    got = client.get(f"/api/builder/sessions/{sid}").json()
    assert got["requirements"]["industry"] == "製造"
    assert got["status"] == "hearing"  # designed へは SP3-02 の design が遷移させる


def test_llm_receives_full_transcript(fake_llm):
    """LLM 入力 = system + 全 transcript + 新規発話(毎ターン完全な文脈)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(), llm_json()]
    client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "一言目"})
    client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "二言目"})
    second_call = fake_llm["calls"][1]
    assert second_call[0]["role"] == "system"
    assert [m["content"] for m in second_call[1:]] == ["一言目", "了解です", "二言目"]


# --- サーバ側の決定的再検査(fail-closed — specs/19 §2.3) ---


def test_recheck_overrides_llm_sufficient_true(fake_llm):
    """LLM が sufficient=true でも必須欠落なら false(LLM を信頼境界にしない)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(
        requirements={"use_case": "検索", "data_profile": {"documents": "文書"}},
        sufficient=True,  # industry 欠落なのに true と主張
    )]
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    body = res.json()
    assert body["sufficient"] is False
    assert "industry" in body["missing"]


def test_recheck_data_profile_either_side_ok(fake_llm):
    """data_profile は documents / tables のどちらか一方以上で必須充足(specs/19 §2.2)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(
        requirements={"industry": "小売", "use_case": "売上照会",
                      "data_profile": {"tables": "売上明細"}},
        sufficient=True,
    )]
    assert client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": "x"}).json()["sufficient"] is True


def test_llm_false_wins_even_if_required_filled(fake_llm):
    """必須が揃っていても LLM が false なら false(追加確認の余地 — specs/19 §2.3)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(requirements=FULL_REQ, sufficient=False,
                                    missing=["notes"])]
    assert client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": "x"}).json()["sufficient"] is False


def test_blank_strings_do_not_satisfy_required(fake_llm):
    """空白のみの必須フィールドは未充足扱い(決定的再検査の境界)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(
        requirements={"industry": " ", "use_case": "x",
                      "data_profile": {"documents": ""}},
        sufficient=True,
    )]
    body = client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": "x"}).json()
    assert body["sufficient"] is False
    assert {"industry", "data_profile"} <= set(body["missing"])


# --- LLM 出力の頑健性(構造化出力の強制) ---


def test_code_fenced_json_is_parsed(fake_llm):
    sid = _create_sid()
    fake_llm["outputs"] = ["```json\n" + llm_json(sufficient=False) + "\n```"]
    assert client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": "x"}).status_code == 200


def test_garbage_then_valid_retries_once(fake_llm):
    sid = _create_sid()
    fake_llm["outputs"] = ["これはJSONではありません", llm_json()]
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 200
    assert len(fake_llm["calls"]) == 2


def test_garbage_twice_is_502_and_nothing_persisted(fake_llm):
    sid = _create_sid()
    fake_llm["outputs"] = ["garbage", "まだ garbage"]
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 502
    # 失敗ターンは transcript に残さない(次の発話でやり直せる)
    assert client.get(f"/api/builder/sessions/{sid}").json()["transcript"] == []


# --- 信頼境界の入力上限(specs/19 §2.1) ---


def test_message_over_4000_chars_is_422():
    sid = _create_sid()
    res = client.post(f"/api/builder/sessions/{sid}/messages",
                      json={"content": "あ" * 4001})
    assert res.status_code == 422


def test_empty_message_is_422():
    sid = _create_sid()
    assert client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": ""}).status_code == 422


def test_50_round_trips_reached_is_422(fake_repo, fake_llm):
    sid = _create_sid()
    fake_repo.rows[sid]["transcript"] = (
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 50
    )
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 422
    assert "新しいセッション" in res.json()["detail"]
    assert fake_llm["calls"] == []  # LLM 入力の有界化(呼び出し前に遮断)


def test_transcript_256kb_reached_is_422(fake_repo, fake_llm):
    sid = _create_sid()
    big = "あ" * 3900
    fake_repo.rows[sid]["transcript"] = [
        {"role": "user", "content": big} for _ in range(23)
    ]  # 直列化 ≈ 270KB > 256KB
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 422
    assert "新しいセッション" in res.json()["detail"]
    assert fake_llm["calls"] == []


# --- demo_id 設定後は読み取り専用(specs/19 §2.1) ---


def test_messages_after_demo_id_is_409(fake_repo, fake_llm):
    sid = _create_sid()
    fake_repo.rows[sid]["demo_id"] = "d1"
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 409
    assert fake_llm["calls"] == []


def test_concurrent_demo_id_set_between_read_and_save_is_409(fake_repo, fake_llm,
                                                             monkeypatch):
    """読み→LLM→書きの間に生成が始まった場合、保存の demo_id IS NULL ガードで 409。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json()]
    real_save = fake_repo.save_hearing_turn

    def racy_save(owner, s, transcript, requirements, expected_len):
        fake_repo.rows[sid]["demo_id"] = "d1"  # 保存直前に生成開始が割り込む
        return real_save(owner, s, transcript, requirements, expected_len)

    monkeypatch.setattr(repo, "save_hearing_turn", racy_save)
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 409


def test_concurrent_messages_do_not_lose_turns(fake_repo, fake_llm, monkeypatch):
    """並行 messages の後勝ち全置換で先行の往復が消えない(楽観ロック — review-1 M002)。

    A の LLM 呼び出し中に並行リクエスト B の往復がコミットされた状況を再現:
    A の保存は transcript 件数不一致で 0 行 → 409。B の往復は残る。
    """
    sid = _create_sid()
    concurrent_turn = [{"role": "user", "content": "B の発話"},
                       {"role": "assistant", "content": "B への応答"}]

    def complete_with_interleave(messages):
        # A が LLM を呼んでいる間に B の往復が先にコミットされる
        fake_repo.rows[sid]["transcript"] = list(concurrent_turn)
        return llm_json(), {"input_tokens": 1, "output_tokens": 1}

    import jetuse_core.builder_hearing as hearing_mod
    monkeypatch.setattr(hearing_mod, "_complete", complete_with_interleave)
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "A の発話"})
    assert res.status_code == 409
    # B の往復は失われていない(A の全置換が棄却された)
    assert fake_repo.rows[sid]["transcript"] == concurrent_turn


def test_assistant_reply_overflowing_256kb_is_422_and_not_saved(fake_repo, fake_llm):
    """assistant 応答込みの最終 transcript が 256KB を超えたら保存せず 422
    (保存前の再検査 — review-1 M003)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json(reply="ん" * 90_000)]  # 直列化で ~270KB
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 422
    assert "新しいセッション" in res.json()["detail"]
    assert fake_repo.rows[sid]["transcript"] == []  # 超過ターンは永続化しない


# --- usage_log(specs/19 §8.3 — LLM 使用は owner に紐づけて記録) ---


def test_hearing_usage_logged_to_owner(fake_llm, monkeypatch):
    import service.routes.builder as builder_routes

    logged = []
    monkeypatch.setattr(builder_routes.conv_repo, "log_usage",
                        lambda *a: logged.append(a))
    sid = _create_sid()
    fake_llm["outputs"] = [llm_json()]
    client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert logged == [("dev-user", None, hearing.HEARING_MODEL, 10, 5)]


def test_llm_nulls_for_optional_fields_are_accepted(fake_llm):
    """実 LLM はプロンプト指示どおり不明項目を null で返す(2026-07-07 プレビュー実機)。
    null を型不一致で 502 にしない(capabilities_hint/missing/data_profile/requirements)。"""
    sid = _create_sid()
    fake_llm["outputs"] = [json.dumps({
        "reply": "追加で教えてください",
        "requirements": {"industry": "製造業", "use_case": None,
                         "capabilities_hint": None,
                         "data_profile": {"documents": None, "tables": None},
                         "notes": None},
        "sufficient": False,
        "missing": None,
    }, ensure_ascii=False)]
    res = client.post(f"/api/builder/sessions/{sid}/messages", json={"content": "x"})
    assert res.status_code == 200
    body = res.json()
    assert body["requirements"]["capabilities_hint"] == []
    assert len(fake_llm["calls"]) == 1  # 再試行に落ちていない


def test_llm_null_requirements_is_accepted(fake_llm):
    sid = _create_sid()
    fake_llm["outputs"] = [json.dumps(
        {"reply": "業種を教えてください", "requirements": None,
         "sufficient": False, "missing": ["industry"]}, ensure_ascii=False)]
    assert client.post(f"/api/builder/sessions/{sid}/messages",
                       json={"content": "x"}).status_code == 200


# --- 決定的再検査の単体(HTTP 非経由) ---


def test_missing_required_pure():
    assert hearing.missing_required(hearing.Requirements()) == [
        "industry", "use_case", "data_profile"]
    full = hearing.Requirements.model_validate(FULL_REQ)
    assert hearing.missing_required(full) == []
