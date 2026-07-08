"""デモ設計(SP3-02 / specs/19 §3)の単体テスト。

語彙導出(§3.4)・プランスキーマ検証(§3.2・§3.3 fail-closed)・design ルート(§3.1)を
fake LLM(builder_hearing._complete 差し替え)と in-memory fake リポジトリで検証する。
スナップショット = 固定 fake 出力 → 検証済み(正規化済み)プランの完全一致。
"""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_builder_sessions import FULL_REQ, FakeBuilderSessions

import jetuse_core.builder_design as design
import jetuse_core.builder_hearing as hearing
import jetuse_core.builder_sessions as repo
from jetuse_core.capabilities import CAPABILITIES, demo_plan_vocabulary
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)

VOCAB = ["chat", "rag.search", "dbchat"]  # テスト入力として明示(実装は導出 — §3.4)

# specs/19 §3.2 の例に準拠した検証合格プラン(スナップショットの正)
SPEC_PLAN = {
    "plan_version": 1,
    "title": "設備保全アシスタント",
    "description": "保全マニュアル検索と故障履歴照会を 1 画面で見せるデモ",
    "capabilities": ["chat", "rag.search", "dbchat"],
    "screens": [{
        "id": "home",
        "title": "保全デスク",
        "description": "検索と照会を並べたメイン画面",
        "blocks": [
            {"type": "rag.search", "title": "マニュアル検索",
             "suggested_prompts": ["ポンプ P-102 の分解手順は?"]},
            {"type": "dbchat", "title": "故障履歴照会",
             "suggested_prompts": ["直近3ヶ月で故障が多い設備は?"]},
            {"type": "chat", "title": "保全アシスタント",
             "system_prompt": "あなたは設備保全の専門アシスタント。",
             "suggested_prompts": ["予知保全の始め方を教えて"]},
        ],
    }],
    "data": {
        "tables": [{
            "name": "equipment", "title": "設備台帳", "rows": 50,
            "columns": [
                {"name": "equipment_id", "type": "VARCHAR2(20 CHAR)", "description": "設備ID"},
                {"name": "installed_on", "type": "DATE", "description": "設置日"},
            ],
        }],
        "documents": [{
            "filename": "maintenance_manual.md", "title": "保全マニュアル",
            "outline": "章立て: 安全注意 / 日常点検 / 分解整備 / トラブルシュート",
        }],
    },
}


def plan_with(**overrides):
    p = copy.deepcopy(SPEC_PLAN)
    p.update(overrides)
    return p


def chat_only_plan():
    """chat のみ・データ定義なしの最小プラン(整合 OK の対照)。"""
    return {
        "plan_version": 1, "title": "アシスタント", "description": "チャットのみ",
        "capabilities": ["chat"],
        "screens": [{"id": "home", "title": "ホーム",
                     "blocks": [{"type": "chat", "title": "チャット"}]}],
        "data": {"tables": [], "documents": []},
    }


def err_of(plan, vocab=None):
    with pytest.raises(design.PlanValidationError) as ei:
        design.validate_plan(plan, VOCAB if vocab is None else vocab)
    return str(ei.value)


# --- プラン語彙の構造的導出(specs/19 §3.4 — ハードコードしない) ---


def test_vocabulary_is_structural_not_hardcoded():
    """demo_safe=true かつデモスコープルートを持つ能力だけが語彙に入る(合成カタログで証明)。"""
    fake_catalog = [
        {"capability": "x.in", "demo_safe": True,
         "routes": [{"path": "/api/demos/{demo_id}/x", "method": "post"}]},
        {"capability": "x.user_only", "demo_safe": True,
         "routes": [{"path": "/api/x", "method": "post"}]},
        {"capability": "x.unsafe", "demo_safe": False,
         "routes": [{"path": "/api/demos/{demo_id}/y", "method": "post"}]},
    ]
    assert demo_plan_vocabulary(fake_catalog) == ["x.in"]


def test_vocabulary_from_current_catalog():
    """現行カタログからの導出結果 = 3 系統(specs/19 §3.4。カタログ順)。"""
    assert demo_plan_vocabulary() == ["chat", "rag.search", "dbchat"]
    assert demo_plan_vocabulary(CAPABILITIES) == demo_plan_vocabulary()


# --- スキーマ検証: 合格(specs/19 §3.2) ---


def test_spec_example_plan_validates_and_normalizes():
    out = design.validate_plan(copy.deepcopy(SPEC_PLAN), VOCAB)
    assert out == SPEC_PLAN  # exclude_none 正規化後も同形(スナップショットの前提)
    assert design.validate_plan(out, VOCAB) == out  # 正規化はべき等(保存形の再検証が通る)


def test_chat_only_plan_without_data_is_valid():
    out = design.validate_plan(chat_only_plan(), VOCAB)
    assert out["capabilities"] == ["chat"]
    assert out["screens"][0]["blocks"][0]["suggested_prompts"] == []  # 既定値の実体化


def test_allowed_column_types():
    for t in ["VARCHAR2(1000 CHAR)", "NUMBER", "NUMBER(10)", "NUMBER(10,2)",
              "DATE", "TIMESTAMP"]:
        p = plan_with()
        p["data"]["tables"][0]["columns"][0]["type"] = t
        design.validate_plan(p, VOCAB)


# --- スキーマ検証: fail-closed(specs/19 §3.3) ---


def test_unknown_capability_is_rejected():
    """語彙外(カタログには居るがデモスコープなし)も未知能力も 422 相当。"""
    for cap in ["agents", "translate", "no.such"]:
        assert "語彙" in err_of(plan_with(capabilities=[*VOCAB, cap]))


def test_empty_and_duplicate_capabilities_rejected():
    err_of(plan_with(capabilities=[]))
    assert "重複" in err_of(plan_with(capabilities=["chat", "chat"]))


def test_unknown_plan_version_rejected():
    err_of(plan_with(plan_version=2))


def test_extra_fields_forbidden_no_free_wiring():
    """URL/パスの自由記述フィールドは存在しない(§3.2 — extra=forbid の構造的防止)。"""
    err_of(plan_with(base_url="https://evil.example.com"))
    p = plan_with()
    p["screens"][0]["blocks"][0]["url"] = "/api/other"
    err_of(p)
    p2 = plan_with()
    p2["screens"][0]["blocks"][0]["endpoint"] = "http://x"
    err_of(p2)


def test_block_type_must_be_in_plan_capabilities():
    p = plan_with(capabilities=["chat", "rag.search"])
    p["data"]["tables"] = []
    # dbchat ブロックが残っている(capabilities に無い)
    assert "capabilities" in err_of(p)


def test_screens_and_blocks_count_bounds():
    err_of(plan_with(screens=[]))
    scr = copy.deepcopy(SPEC_PLAN["screens"][0])
    six = []
    for i in range(6):
        s = copy.deepcopy(scr)
        s["id"] = f"s{i}"
        six.append(s)
    err_of(plan_with(screens=six))
    p = plan_with()
    p["screens"][0]["blocks"] = []
    err_of(p)
    p2 = plan_with()
    p2["screens"][0]["blocks"] = [
        {"type": "chat", "title": f"b{i}"} for i in range(9)]
    err_of(p2)


def test_tables_columns_rows_bounds():
    p = plan_with()
    t = p["data"]["tables"][0]
    p["data"]["tables"] = []
    for i in range(6):
        c = copy.deepcopy(t)
        c["name"] = f"t{i}"
        p["data"]["tables"].append(c)
    err_of(p)
    for bad_rows in (0, 501):
        p = plan_with()
        p["data"]["tables"][0]["rows"] = bad_rows
        err_of(p)
    p = plan_with()
    p["data"]["tables"][0]["columns"] = []
    err_of(p)
    p = plan_with()
    col = p["data"]["tables"][0]["columns"][0]
    p["data"]["tables"][0]["columns"] = [
        {**copy.deepcopy(col), "name": f"c{i}"} for i in range(21)]
    err_of(p)


def test_identifier_allowlist():
    """表・列名は ^[a-z][a-z0-9_]{0,29}$ のみ(SQL/命名機構への信頼境界)。"""
    for bad in ["Equipment", "1x", "a" * 31, "tbl-x", "tbl x", ""]:
        p = plan_with()
        p["data"]["tables"][0]["name"] = bad
        err_of(p)
    p = plan_with()
    p["data"]["tables"][0]["columns"][0]["name"] = "EquipmentId"
    err_of(p)


def test_duplicate_table_names_and_screen_ids_rejected():
    p = plan_with()
    dup = copy.deepcopy(p["data"]["tables"][0])
    p["data"]["tables"].append(dup)
    assert "重複" in err_of(p)
    p2 = plan_with()
    p2["screens"] = [copy.deepcopy(p2["screens"][0]), copy.deepcopy(p2["screens"][0])]
    assert "重複" in err_of(p2)


def test_column_type_allowlist_rejects_others():
    for bad in ["VARCHAR2(1001 CHAR)", "VARCHAR2(100)", "varchar2(10 char)", "CLOB",
                "NUMBER(39)", "NUMBER(10,99)", "TIMESTAMP(6)", "FLOAT", "",
                "DATE\n", "NUMBER\n"]:  # 末尾改行は $ アンカーの罠(review-1 F005 — fullmatch)
        p = plan_with()
        p["data"]["tables"][0]["columns"][0]["type"] = bad
        err_of(p)


def test_document_filename_allowlist():
    for ok in ["a.md", "manual_v2-1.txt", "x" * 64 + ".md"]:
        p = plan_with()
        p["data"]["documents"][0]["filename"] = ok
        design.validate_plan(p, VOCAB)
    for bad in ["Manual.md", "a.pdf", ".md", "x" * 65 + ".md", "日本語.md", "a b.md"]:
        p = plan_with()
        p["data"]["documents"][0]["filename"] = bad
        err_of(p)


def test_documents_count_bound():
    p = plan_with()
    d = p["data"]["documents"][0]
    p["data"]["documents"] = [
        {**copy.deepcopy(d), "filename": f"doc{i}.md"} for i in range(11)]
    err_of(p)


def test_string_length_bounds():
    err_of(plan_with(title="あ" * 201))
    err_of(plan_with(description="あ" * 1001))
    p = plan_with()
    p["screens"][0]["blocks"][2]["system_prompt"] = "あ" * 4001
    err_of(p)
    p = plan_with()
    p["screens"][0]["blocks"][0]["suggested_prompts"] = ["q"] * 6
    err_of(p)
    p = plan_with()
    p["screens"][0]["blocks"][0]["suggested_prompts"] = ["あ" * 201]
    err_of(p)
    p = plan_with()
    p["data"]["documents"][0]["outline"] = "あ" * 1001
    err_of(p)


def test_capability_data_consistency_both_directions():
    """dbchat ⇔ tables / rag.search ⇔ documents(能力⇔データ定義の整合 — §3.3)。"""
    p = plan_with()
    p["data"]["tables"] = []  # dbchat あり tables なし
    assert "dbchat" in err_of(p)
    p = plan_with()
    p["data"]["documents"] = []  # rag.search あり documents なし
    assert "rag.search" in err_of(p)
    p = chat_only_plan()
    p["data"]["tables"] = SPEC_PLAN["data"]["tables"]  # tables あり dbchat なし
    assert "dbchat" in err_of(p)
    p = chat_only_plan()
    p["data"]["documents"] = SPEC_PLAN["data"]["documents"]  # documents あり rag.search なし
    assert "rag.search" in err_of(p)


def test_plan_over_256kb_rejected():
    p = plan_with()
    scr = p["screens"][0]
    p["screens"] = []
    for i in range(5):
        s = copy.deepcopy(scr)
        s["id"] = f"s{i}"
        s["blocks"] = [{"type": "chat", "title": f"b{j}",
                        "system_prompt": "あ" * 4000} for j in range(8)]
        p["screens"].append(s)
    # 5 画面 × 8 ブロック × 4000 文字(多バイト) ≈ 480KB > 256KB
    assert "256KB" in err_of(p)


def test_non_dict_plan_rejected():
    err_of([SPEC_PLAN])
    err_of("just text")


# --- run_design: 有界再試行(specs/19 §3.1 — 同一リクエスト内 最大 2 回再生成) ---


@pytest.fixture()
def fake_llm(monkeypatch):
    state = {"outputs": [], "calls": [], "schemas": []}

    def _complete(messages, response_schema=None):
        state["calls"].append(messages)
        state["schemas"].append(response_schema)
        return state["outputs"].pop(0), {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(hearing, "_complete", _complete)
    return state


def _catalog():
    return [c for c in CAPABILITIES if c["capability"] in VOCAB]


def test_run_design_first_try_ok(fake_llm):
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, usage = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    assert usage == {"input_tokens": 10, "output_tokens": 5}
    assert len(fake_llm["calls"]) == 1


def test_run_design_feeds_back_errors_then_succeeds(fake_llm):
    bad = plan_with(capabilities=[*VOCAB, "agents"])
    fake_llm["outputs"] = [json.dumps(bad, ensure_ascii=False),
                           json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, usage = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    assert len(fake_llm["calls"]) == 2
    feedback = fake_llm["calls"][1][-1]
    assert feedback["role"] == "user"
    assert "語彙" in feedback["content"]  # 検証エラーをフィードバックして再生成
    assert usage == {"input_tokens": 20, "output_tokens": 10}


def test_run_design_garbage_counts_as_failure_and_retries(fake_llm):
    fake_llm["outputs"] = ["JSONではない", json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    assert "JSON" in fake_llm["calls"][1][-1]["content"]


def test_run_design_code_fence_is_stripped(fake_llm):
    fake_llm["outputs"] = ["```json\n" + json.dumps(SPEC_PLAN, ensure_ascii=False) + "\n```"]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN


def test_run_design_trailing_extra_data_is_tolerated(fake_llm):
    """実機観測(2026-07-07 プレビュー): 完全な JSON の後に余分なデータが続き
    json.loads 全文パースが Extra data で落ちる。最初のオブジェクトだけを頑健に取り出す
    (検証は strict スキーマのままなので fail-closed は弱まらない)。"""
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False) + "}\n以上がプランです。"]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    assert len(fake_llm["calls"]) == 1


def test_run_design_leading_prose_is_tolerated(fake_llm):
    fake_llm["outputs"] = ["以下がデモプランです:\n" + json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN


def test_run_design_no_json_object_at_all_is_failure(fake_llm):
    fake_llm["outputs"] = ["プランを作成できませんでした",
                           json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    assert len(fake_llm["calls"]) == 2  # 1 回目は JSON なし → フィードバック再生成


def test_run_design_gives_up_after_two_regenerations(fake_llm):
    bad = json.dumps(plan_with(plan_version=2), ensure_ascii=False)
    fake_llm["outputs"] = [bad, bad, bad]
    with pytest.raises(design.DesignError) as ei:
        design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert len(fake_llm["calls"]) == 3  # 初回 + 再生成 2 回で打ち切り
    assert ei.value.usage == {"input_tokens": 30, "output_tokens": 15}


def test_run_design_prompt_carries_requirements_and_catalog(fake_llm):
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False)]
    design.run_design(FULL_REQ, _catalog(), VOCAB)
    system = fake_llm["calls"][0][0]
    user = fake_llm["calls"][0][1]
    assert system["role"] == "system"
    for cap in VOCAB:  # 語彙とカタログ由来の説明が入る(能力 id をコードに固定しない)
        assert cap in system["content"]
    assert "文書への検索" in system["content"]  # カタログ summary 由来
    assert "設備保全" in user["content"]  # 要求サマリ由来


# --- design ルート(specs/19 §3.1) ---


class FakeRepo(FakeBuilderSessions):
    def save_plan(self, owner, sid, plan, expected_len):
        r = self.rows.get(sid)
        if (
            not r or r["owner_sub"] != owner or r["demo_id"] is not None
            or len(r["transcript"]) != expected_len  # 楽観ロック(F003)
        ):
            return False
        r["plan"] = plan
        r["status"] = "designed"
        r["updated_at"] = self._now()
        return True


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    fake = FakeRepo()
    for name in ("create_session", "get_session", "save_hearing_turn", "save_plan"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    yield fake


def _sufficient_sid(fake_repo):
    sid = client.post("/api/builder/sessions").json()["id"]
    fake_repo.rows[sid]["requirements"] = dict(FULL_REQ)
    fake_repo.rows[sid]["sufficient"] = True  # 直近ヒアリングの最終判定(永続化済み)
    fake_repo.rows[sid]["transcript"] = [
        {"role": "user", "content": "製造業のデモ"}, {"role": "assistant", "content": "了解"}]
    return sid


def test_design_success_persists_plan_and_designed(fake_repo, fake_llm):
    sid = _sufficient_sid(fake_repo)
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False)]
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "designed"
    assert body["plan"] == SPEC_PLAN  # スナップショット(固定 fake 出力 → 検証済みプラン)
    got = client.get(f"/api/builder/sessions/{sid}").json()
    assert got["plan"] == SPEC_PLAN and got["status"] == "designed"


def test_design_insufficient_requirements_is_409(fake_repo, fake_llm):
    """sufficient 判定は永続化済み requirements への決定的再検査で導出(M001 の確定)。"""
    sid = client.post("/api/builder/sessions").json()["id"]
    res = client.post(f"/api/builder/sessions/{sid}/design")  # requirements なし
    assert res.status_code == 409
    fake_repo.rows[sid]["requirements"] = {"industry": "製造"}  # 必須欠落
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 409
    assert "use_case" in res.json()["detail"]
    assert fake_llm["calls"] == []  # 前提不成立では LLM を呼ばない


def test_design_rerun_overwrites_plan(fake_repo, fake_llm):
    """designed 後の再実行でプランを上書きできる(demo_id が付くまで — §3.1)。"""
    sid = _sufficient_sid(fake_repo)
    second = plan_with(title="第2版プラン")
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False),
                           json.dumps(second, ensure_ascii=False)]
    assert client.post(f"/api/builder/sessions/{sid}/design").status_code == 200
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 200
    assert res.json()["plan"]["title"] == "第2版プラン"


def test_design_after_demo_id_is_409(fake_repo, fake_llm):
    sid = _sufficient_sid(fake_repo)
    fake_repo.rows[sid]["demo_id"] = "d1"
    assert client.post(f"/api/builder/sessions/{sid}/design").status_code == 409
    assert fake_llm["calls"] == []


def test_design_demo_id_set_during_llm_call_is_409(fake_repo, fake_llm, monkeypatch):
    """読み→LLM→保存の間に生成が始まったら save_plan の demo_id IS NULL ガードで 409。"""
    sid = _sufficient_sid(fake_repo)

    def complete_and_start_generation(messages, response_schema=None):
        fake_repo.rows[sid]["demo_id"] = "d1"
        return json.dumps(SPEC_PLAN, ensure_ascii=False), {
            "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(hearing, "_complete", complete_and_start_generation)
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 409
    assert fake_repo.rows[sid]["plan"] is None


def test_design_cross_user_and_absent_is_404(fake_repo):
    fake_repo.rows["theirs"] = {
        "id": "theirs", "owner_sub": "user-a", "status": "hearing", "transcript": [],
        "requirements": dict(FULL_REQ), "plan": None, "demo_id": None, "sufficient": True,
        "created_at": "2026-07-07T00:00:00", "updated_at": "2026-07-07T00:00:00",
    }
    res = client.post("/api/builder/sessions/theirs/design")
    assert res.status_code == 404
    assert res.json()["detail"] == "session not found"
    assert client.post("/api/builder/sessions/no-such/design").status_code == 404


def test_design_unauthenticated_is_401(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        assert client.post("/api/builder/sessions/x/design").status_code == 401
    finally:
        get_settings.cache_clear()


def test_design_three_failures_is_422_and_nothing_persisted(fake_repo, fake_llm):
    sid = _sufficient_sid(fake_repo)
    bad = json.dumps(plan_with(capabilities=["no.such"]), ensure_ascii=False)
    fake_llm["outputs"] = [bad, bad, bad]
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 422
    assert "検証" in res.json()["detail"]
    row = fake_repo.rows[sid]
    assert row["plan"] is None and row["status"] == "hearing"
    assert len(row["transcript"]) == 2  # transcript は消さない(§3.1)


def test_design_llm_transport_error_is_502(fake_repo, monkeypatch):
    sid = _sufficient_sid(fake_repo)

    def boom(messages):
        raise ConnectionError("upstream down")

    monkeypatch.setattr(hearing, "_complete", boom)
    assert client.post(f"/api/builder/sessions/{sid}/design").status_code == 502


# --- review-1 修正の回帰(F001/F002/F003/F004) ---

# 実機で捕捉した失敗形(2026-07-07 プレビュー・gpt-oss-120b): 画面オブジェクト間に余分な
# 閉じ括弧が入る構造的に壊れた JSON("...]}}, {" — Expecting ',' delimiter)
MALFORMED_PLAN_OUTPUT = (
    '{"plan_version":1,"title":"デモ","description":"説明",'
    '"capabilities":["chat"],"screens":[{"id":"home","title":"ホーム",'
    '"blocks":[{"type":"chat","title":"チャット"}]}},'  # ← 余分な "}"(実機の失敗形)
    '{"id":"second","title":"2","blocks":[{"type":"chat","title":"c"}]}],'
    '"data":{"tables":[],"documents":[]}}'
)


def test_run_design_passes_plan_schema_to_llm(fake_llm):
    """生成は json_schema 構造化出力で依頼する(review-1 F001 — 実機 6/6 合格を確認済み)。"""
    fake_llm["outputs"] = [json.dumps(SPEC_PLAN, ensure_ascii=False)]
    design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert fake_llm["schemas"] == [design.DemoPlan.model_json_schema()]


def test_run_design_malformed_json_feedback_has_error_position(fake_llm):
    """構造的に壊れた JSON は位置付きの構文エラーをフィードバックして再生成(F001)。"""
    fake_llm["outputs"] = [MALFORMED_PLAN_OUTPUT, json.dumps(SPEC_PLAN, ensure_ascii=False)]
    plan, _ = design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert plan == SPEC_PLAN
    feedback = fake_llm["calls"][1][-1]["content"]
    assert "構文エラー" in feedback and "位置" in feedback  # 場所を特定できる情報を返す


def test_run_design_transport_error_carries_consumed_usage(monkeypatch):
    """検証不合格で usage 消費後に通信例外 → 消費分を保持した専用例外(F004)。"""
    calls = {"n": 0}

    def sequenced(messages, response_schema=None):
        calls["n"] += 1
        if calls["n"] == 1:  # 1 回目 = 不正出力(usage 消費)、2 回目 = 通信例外
            return MALFORMED_PLAN_OUTPUT, {"input_tokens": 10, "output_tokens": 5}
        raise ConnectionError("upstream down")

    monkeypatch.setattr(hearing, "_complete", sequenced)
    with pytest.raises(design.DesignUpstreamError) as ei:
        design.run_design(FULL_REQ, _catalog(), VOCAB)
    assert ei.value.usage == {"input_tokens": 10, "output_tokens": 5}


def test_design_route_upstream_error_after_consumption_logs_usage_and_502(
        fake_repo, fake_llm, monkeypatch):
    import service.routes.builder as builder_routes

    logged = []
    monkeypatch.setattr(builder_routes.conv_repo, "log_usage",
                        lambda *a: logged.append(a))
    sid = _sufficient_sid(fake_repo)
    calls = {"n": 0}

    def sequenced(messages, response_schema=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return MALFORMED_PLAN_OUTPUT, {"input_tokens": 10, "output_tokens": 5}
        raise ConnectionError("upstream down")

    monkeypatch.setattr(hearing, "_complete", sequenced)
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 502
    assert logged == [("dev-user", None, design.DESIGN_MODEL, 10, 5)]  # 消費分は記録


def test_design_requires_persisted_sufficient_even_if_required_filled(fake_repo, fake_llm):
    """必須充足でも直近の最終判定が sufficient=false なら 409(F002 — LLM false 優先契約)。"""
    sid = client.post("/api/builder/sessions").json()["id"]
    fake_repo.rows[sid]["requirements"] = dict(FULL_REQ)  # 必須は全て充足
    fake_repo.rows[sid]["sufficient"] = False  # だが直近判定は false(追加確認したい)
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 409
    assert "sufficient" in res.json()["detail"]
    assert fake_llm["calls"] == []


def test_design_concurrent_message_during_llm_is_409(fake_repo, fake_llm, monkeypatch):
    """設計中に並行 messages が transcript を進めたら save_plan の楽観ロックで 409(F003)。"""
    sid = _sufficient_sid(fake_repo)

    def complete_with_interleave(messages, response_schema=None):
        fake_repo.rows[sid]["transcript"] = [
            *fake_repo.rows[sid]["transcript"],
            {"role": "user", "content": "並行発話"}, {"role": "assistant", "content": "応答"}]
        return json.dumps(SPEC_PLAN, ensure_ascii=False), {
            "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(hearing, "_complete", complete_with_interleave)
    res = client.post(f"/api/builder/sessions/{sid}/design")
    assert res.status_code == 409
    assert fake_repo.rows[sid]["plan"] is None  # 古い requirements 由来の plan を保存しない


def test_design_usage_logged_on_success_and_failure(fake_repo, fake_llm, monkeypatch):
    """LLM 使用は owner に紐づけて記録(§8.3)。消費したトークンはエラー経路でも記録する。"""
    import service.routes.builder as builder_routes

    logged = []
    monkeypatch.setattr(builder_routes.conv_repo, "log_usage",
                        lambda *a: logged.append(a))
    sid = _sufficient_sid(fake_repo)
    bad = json.dumps(plan_with(plan_version=9), ensure_ascii=False)
    fake_llm["outputs"] = [bad, json.dumps(SPEC_PLAN, ensure_ascii=False)]
    client.post(f"/api/builder/sessions/{sid}/design")
    assert logged == [("dev-user", None, design.DESIGN_MODEL, 20, 10)]  # 2 回分の合算

    logged.clear()
    fake_llm["outputs"] = [bad, bad, bad]
    assert client.post(f"/api/builder/sessions/{sid}/design").status_code == 422
    assert logged == [("dev-user", None, design.DESIGN_MODEL, 30, 15)]  # 失敗経路でも記録
