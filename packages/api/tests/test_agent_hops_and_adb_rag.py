"""AGT-04: ホップ上限の設定化と、エージェント文書検索の adb バックエンド。

多段の業務フロー(API を順に呼ぶ手続き)が最後まで通るかは実環境 E2E で見る
(`runs/<run-id>/e2e/`)。ここで固めるのは、その手前の
「上限が設定で動くか」「天井を超える値を拒むか」「打ち切りが黙って起きないか」
「adb の出典(シート名・セル範囲)が結果に載るか」「既定の挙動が変わらないか」。
"""

import json

import pytest
from fastapi.testclient import TestClient

import jetuse_core.chat as chat_mod
import jetuse_core.tools as tools_mod
from jetuse_core.settings import (
    AGENT_MAX_TOOL_HOPS_CEILING,
    AGENT_MAX_TOOL_HOPS_DEFAULT,
    get_settings,
)
from service.main import app

client = TestClient(app)


# --- テスト用の Responses スタブ ---------------------------------------------


class FakeItem:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


class FakeEvent:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class FakeStream:
    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)

    def close(self):
        pass


def always_calls_tool(counter, name="web_search", arguments='{"query": "x"}'):
    """毎ホップ function_call を返し続ける = 自力では止まらないモデル。"""

    class FakeResponses:
        def create(self, **kw):
            counter["n"] += 1
            counter.setdefault("tools", kw.get("tools"))  # 最初のホップの tools を見る
            call = FakeItem(type="function_call", name=name,
                            arguments=arguments, call_id=f"c{counter['n']}", id=None)
            return FakeStream([FakeEvent("response.output_item.done", item=call)])

    class FakeClient:
        responses = FakeResponses()

    return FakeClient()


@pytest.fixture()
def no_tool_side_effects(monkeypatch):
    monkeypatch.setattr(tools_mod, "execute_with",
                        lambda registry, name, args: '{"results": []}')


# --- ホップ上限の解決 ---------------------------------------------------------


def test_resolve_max_tool_hops_uses_settings_by_default():
    get_settings.cache_clear()
    assert chat_mod.resolve_max_tool_hops() == AGENT_MAX_TOOL_HOPS_DEFAULT


def test_resolve_max_tool_hops_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_HOPS", "12")
    get_settings.cache_clear()
    try:
        assert chat_mod.resolve_max_tool_hops() == 12
    finally:
        get_settings.cache_clear()


def test_resolve_max_tool_hops_request_overrides_settings():
    assert chat_mod.resolve_max_tool_hops(7) == 7


def test_resolve_max_tool_hops_rejects_above_ceiling():
    # クランプしない: 黙って下げると「上げたのに効かない」になる
    with pytest.raises(ValueError, match="ceiling"):
        chat_mod.resolve_max_tool_hops(AGENT_MAX_TOOL_HOPS_CEILING + 1)


def test_resolve_max_tool_hops_rejects_non_positive_and_bool():
    with pytest.raises(ValueError):
        chat_mod.resolve_max_tool_hops(0)
    with pytest.raises(ValueError):
        chat_mod.resolve_max_tool_hops(True)  # noqa: FBT003 - bool は int の派生


def test_non_integer_setting_breaks_only_agent_mode(monkeypatch):
    """`AGENT_MAX_TOOL_HOPS=abc` で全 API が 500 にならない(壊すのは当該機能だけ)。"""
    monkeypatch.setenv("AGENT_MAX_TOOL_HOPS", "abc")
    get_settings.cache_clear()
    try:
        import service.main as service_main
        monkeypatch.setattr(service_main, "stream_chat", _one_delta)
        plain = client.post("/api/chat/stream", json={
            "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}]})
        assert plain.status_code == 200 and '"ok"' in plain.text
        agent = client.post("/api/chat/stream", json=_agent_body())
        assert agent.status_code == 400
        assert "must be an integer" in agent.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_empty_setting_falls_back_to_default(monkeypatch):
    """`.env.example` の雛形（空値）でエージェントが壊れない。"""
    monkeypatch.setenv("AGENT_MAX_TOOL_HOPS", "")
    get_settings.cache_clear()
    try:
        assert chat_mod.resolve_max_tool_hops() == AGENT_MAX_TOOL_HOPS_DEFAULT
    finally:
        get_settings.cache_clear()


def test_resolve_max_tool_hops_rejects_bad_setting(monkeypatch):
    """設定値が天井超えなら、エージェント実行の入口で拒む(黙って使わない)。"""
    monkeypatch.setenv("AGENT_MAX_TOOL_HOPS", str(AGENT_MAX_TOOL_HOPS_CEILING + 5))
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="ceiling"):
            chat_mod.resolve_max_tool_hops()
    finally:
        get_settings.cache_clear()


# --- 上限に当たったことが応答から分かる --------------------------------------


def test_stream_agent_stops_at_configured_hops_and_says_so(monkeypatch,
                                                           no_tool_side_effects):
    counter = {"n": 0}
    monkeypatch.setattr(chat_mod, "make_inference_client",
                        lambda **kw: always_calls_tool(counter))
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, max_tool_hops=3,
    ))
    # 3 ホップ + 最終回答強制の 1 回
    assert counter["n"] == 4
    limit = [e["limit_reached"] for e in events if "limit_reached" in e]
    assert limit == [{"reason": "max_tool_hops", "limit": 3}]
    # 現行 UI は notice を描かないので、本文にも打ち切りの理由が出る
    assert any("上限" in e.get("delta", "") for e in events)


def test_hop_limit_final_answer_reports_usage(monkeypatch, no_tool_side_effects):
    """打ち切り後の最終回答（上限外の 1 往復）の usage も出す。

    出さないと、打ち切ったターンだけコストが記録から漏れる（= 上限を上げる判断の
    根拠が実際より安く見える）。
    """
    class FakeUsage:
        input_tokens, output_tokens = 100, 20

    class FakeResponse:
        usage = FakeUsage()
        output: list = []

    hops = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            hops["n"] += 1
            if kw.get("tools"):
                call = FakeItem(type="function_call", name="web_search",
                                arguments='{"query": "x"}', call_id=f"c{hops['n']}", id=None)
                return FakeStream([FakeEvent("response.output_item.done", item=call)])
            return FakeStream([FakeEvent("response.completed", response=FakeResponse())])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, max_tool_hops=2,
    ))
    assert hops["n"] == 3  # 上限 2 ホップ + 最終回答 1 往復
    assert [e["usage"] for e in events if "usage" in e] == [
        {"input_tokens": 100, "output_tokens": 20}
    ]


def test_stream_agent_hop_limit_default_is_not_five(monkeypatch, no_tool_side_effects):
    """既定は設定値。旧固定値 5 で止まらない(業務フローが入口で力尽きていた)。"""
    counter = {"n": 0}
    monkeypatch.setattr(chat_mod, "make_inference_client",
                        lambda **kw: always_calls_tool(counter))
    get_settings.cache_clear()
    list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}], auto_tools=True,
    ))
    assert counter["n"] == AGENT_MAX_TOOL_HOPS_DEFAULT + 1


def test_stream_agent_notifies_cumulative_tool_results_cap(monkeypatch):
    """承認往復の累計上限も黙って打ち切らない。上限はホップ上限に連動する。"""
    counter = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            counter["n"] += 1
            counter.setdefault("tools", kw.get("tools"))  # 最初のホップの tools を見る
            return FakeStream([FakeEvent("response.output_text.delta", delta="答え")])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    results = [{"call": {"type": "function_call", "call_id": f"c{i}"}, "output": "{}"}
               for i in range(chat_mod.MIN_TOOL_RESULTS_CAP)]
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, tool_results=results, max_tool_hops=5,
    ))
    assert [e["limit_reached"] for e in events if "limit_reached" in e] == [
        {"reason": "max_tool_results", "limit": chat_mod.MIN_TOOL_RESULTS_CAP}
    ]
    assert counter["tools"] == []  # 上限到達時はツールを外して最終回答を強制


def test_tool_results_cap_follows_raised_hop_limit(monkeypatch):
    """ホップ上限を上げたら累計上限も上がる(承認モードだけ先に打ち切られない)。"""
    counter = {"n": 0}
    monkeypatch.setattr(chat_mod, "make_inference_client",
                        lambda **kw: always_calls_tool(counter))
    monkeypatch.setattr(tools_mod, "execute_with",
                        lambda registry, name, args: '{"results": []}')
    results = [{"call": {"type": "function_call", "call_id": f"c{i}"}, "output": "{}"}
               for i in range(chat_mod.MIN_TOOL_RESULTS_CAP)]
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, tool_results=results,
        max_tool_hops=chat_mod.MIN_TOOL_RESULTS_CAP + 4,
    ))
    reasons = [e["limit_reached"]["reason"] for e in events if "limit_reached" in e]
    assert reasons == ["max_tool_hops"]  # 累計 16 件では打ち切られない


# --- エージェントの文書検索: adb バックエンド --------------------------------


ADB_HIT = {
    "text": "申込は前営業日 17:00 までに受け付ける。",
    "score": 0.87,
    "file_id": "f1",
    "filename": "受付仕様.xlsx",
    "source": {
        "chunk_id": "f1-3", "chunk_no": 3, "file": "受付仕様.xlsx", "version": "1.0",
        "sheet": "制約", "cells": "C5:E5", "sha256": "abc123def456",
        "kind": "doc", "current_version": "Y", "attributes": {},
    },
}


@pytest.fixture()
def fake_adb_search(monkeypatch):
    calls = []

    def search(owner, query, *, k=5, filters=None):
        calls.append({"owner": owner, "query": query, "k": k, "filters": filters})
        return [ADB_HIT]

    import jetuse_core.rag_adb as rag_adb_mod
    monkeypatch.setattr(rag_adb_mod, "search", search)
    return calls


def test_adb_rag_search_tool_returns_sheet_and_cells(fake_adb_search):
    tool = tools_mod.adb_rag_search_tool("owner-1")
    assert tool.name == tools_mod.RAG_SEARCH
    assert tool.requires_approval is False  # 検索のたびに承認で止めない
    out = json.loads(tools_mod.execute_with(
        {tool.name: tool}, tool.name, json.dumps({"query": "締切"})
    ))
    src = out["results"][0]["source"]
    assert src["sheet"] == "制約" and src["cells"] == "C5:E5"
    assert out["results"][0]["text"] == ADB_HIT["text"]  # 本文は切り詰めない
    assert fake_adb_search[0]["filters"] == {"current_version": "Y"}
    assert fake_adb_search[0]["owner"] == "owner-1"


def test_stream_agent_adb_backend_uses_function_tool_and_emits_citations(
    monkeypatch, fake_adb_search
):
    counter = {"n": 0}
    monkeypatch.setattr(
        chat_mod, "make_inference_client",
        lambda **kw: always_calls_tool(counter, name="rag_search",
                                       arguments='{"query": "締切"}'),
    )
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "締切は?"}],
        auto_tools=True, enabled_tools=["rag_search"],
        rag_backend="adb", rag_owner="owner-1", max_tool_hops=1,
    ))
    # file_search built-in ではなく function tool として渡る
    specs = counter["tools"]
    assert [s["type"] for s in specs] == ["function"]
    assert specs[0]["name"] == "rag_search"
    cites = [e["citations"] for e in events if "citations" in e]
    assert cites and cites[0][0]["source"]["cells"] == "C5:E5"
    assert "制約" in next(e["tool_result"]["preview"] for e in events if "tool_result" in e)


def test_stream_agent_default_backend_still_uses_file_search(monkeypatch):
    """回帰: バックエンド未指定なら従来どおり Vector Store の file_search。"""
    counter = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            counter.setdefault("tools", kw.get("tools"))  # 最初のホップの tools を見る
            return FakeStream([FakeEvent("response.output_text.delta", delta="答え")])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, enabled_tools=["rag_search"], rag_store="vs_1",
    ))
    assert counter["tools"] == [{"type": "file_search", "vector_store_ids": ["vs_1"]}]


# --- ルート境界 ---------------------------------------------------------------


def _one_delta(*args, **kw):
    """ルートの差し替え用。**ジェネレータで返す**(ルートは finally で close() する)。"""
    yield {"delta": "ok"}


def _agent_body(**kw):
    return {"model": "gpt-oss-120b", "agent": True,
            "messages": [{"role": "user", "content": "x"}], **kw}


def test_route_rejects_hops_above_ceiling():
    res = client.post("/api/chat/stream",
                      json=_agent_body(max_tool_hops=AGENT_MAX_TOOL_HOPS_CEILING + 1))
    assert res.status_code == 422


def test_route_rejects_agent_params_without_agent_mode():
    base = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}]}
    assert client.post("/api/chat/stream",
                       json={**base, "max_tool_hops": 10}).status_code == 400
    assert client.post("/api/chat/stream",
                       json={**base, "agent_rag_backend": "adb"}).status_code == 400


def test_explicit_default_backend_is_treated_as_unspecified(monkeypatch):
    """API-01: 検査が走るのは**既定でない値**のときだけ、という境界を固定する。

    `agent_rag_backend="vector_store"` を明示しても、エージェント外・`rag_search` 無しでも
    通る(未指定と同じ扱い)。OpenAPI の description をこの境界どおりに書いているので、
    ここが変わると仕様の記述が実挙動と食い違う(review-1 F-002)。
    """
    import service.main as service_main
    monkeypatch.setattr(service_main, "stream_chat", _one_delta)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}],
        "agent_rag_backend": "vector_store"})
    assert res.status_code == 200


def test_route_sums_usage_across_hops(monkeypatch):
    """ホップごとの usage を合算して記録する（最後の 1 往復だけにしない）。"""
    logged = {}

    def multi_hop_usage(*args, **kw):
        yield {"usage": {"input_tokens": 100, "output_tokens": 10}}
        yield {"usage": {"input_tokens": 250, "output_tokens": 30}}
        yield {"delta": "ok"}

    import service.main as service_main
    import service.routes.chat as chat_route
    monkeypatch.setattr(service_main, "stream_agent", multi_hop_usage)
    monkeypatch.setattr(chat_route.audit, "log_event",
                        lambda *a, **kw: logged.update(kw))
    res = client.post("/api/chat/stream", json=_agent_body(auto_tools=True))
    assert res.status_code == 200
    assert logged["input_tokens"] == 350
    assert logged["output_tokens"] == 40


def test_route_rejects_agent_params_for_saved_agents():
    """保存済みエージェント(agent_id)は別ディスパッチ。受理して黙って無視しない。"""
    base = {"model": "gpt-oss-120b", "agent_id": "a1",
            "messages": [{"role": "user", "content": "x"}]}
    for extra in ({"max_tool_hops": 10}, {"agent_rag_backend": "adb"}):
        res = client.post("/api/chat/stream", json={**base, **extra})
        assert res.status_code == 400
        assert "saved agents" in res.json()["detail"]


def test_bad_hop_setting_does_not_break_plain_chat(monkeypatch):
    """設定値が天井超えでも、壊すのはエージェントだけ(素のチャットは巻き添えにしない)。"""
    monkeypatch.setenv("AGENT_MAX_TOOL_HOPS", str(AGENT_MAX_TOOL_HOPS_CEILING + 1))
    get_settings.cache_clear()
    try:
        import service.main as service_main
        monkeypatch.setattr(service_main, "stream_chat", _one_delta)
        plain = client.post("/api/chat/stream", json={
            "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}]})
        assert plain.status_code == 200 and '"ok"' in plain.text
        agent = client.post("/api/chat/stream", json=_agent_body())
        assert agent.status_code == 400 and "ceiling" in agent.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_tool_results_accepts_up_to_the_hop_ceiling(monkeypatch):
    """承認モードの継続が、上げたホップ上限のぶんだけ送り返せる(422 で詰まらない)。"""
    import service.main as service_main
    monkeypatch.setattr(service_main, "stream_agent", _one_delta)
    results = [{"call": {"type": "function_call", "call_id": f"c{i}"}, "output": "{}"}
               for i in range(AGENT_MAX_TOOL_HOPS_CEILING)]
    res = client.post("/api/chat/stream", json=_agent_body(
        auto_tools=True, tool_results=results,
        max_tool_hops=AGENT_MAX_TOOL_HOPS_CEILING))
    assert res.status_code == 200


def test_route_rejects_adb_backend_without_rag_search():
    """バックエンドだけ指定して rag_search が無効なら断る（黙って無視しない）。"""
    res = client.post("/api/chat/stream", json=_agent_body(
        agent_rag_backend="adb", enabled_tools=["web_search"]))
    assert res.status_code == 400
    assert "requires rag_search" in res.json()["detail"]


def test_route_rejects_adb_backend_when_unavailable(monkeypatch):
    import service.routes.chat as chat_route
    monkeypatch.setattr(chat_route.rag_adb, "availability",
                        lambda: chat_route.rag_adb.ABSENT)
    res = client.post("/api/chat/stream", json=_agent_body(
        agent_rag_backend="adb", enabled_tools=["rag_search"]))
    assert res.status_code == 400
    assert "adb rag backend is not available" in res.json()["detail"]


def test_route_still_rejects_agent_with_rag():
    """併用禁止は維持する(今回はエージェント側の口を増やすだけ)。"""
    res = client.post("/api/chat/stream",
                      json=_agent_body(rag=True, agent_rag_backend="adb"))
    assert res.status_code == 400
    assert res.json()["detail"] == "agent and rag cannot be combined"


# --- 同じ検索の繰り返しの検知（ADR-0026 §4 案 B・人間ゲートで承認） -------------------

def test_normalized_query_ignores_case_and_spacing():
    """表記ゆれだけの違いは「同じ検索」とみなす。"""
    a = chat_mod._normalized_query(json.dumps({"query": "  受付可否  API "}))
    b = chat_mod._normalized_query(json.dumps({"query": "受付可否 API"}))
    assert a == b


def test_normalized_query_returns_none_when_undecidable():
    """判定できないものを「同じ」と決めつけない（握り潰さないため）。"""
    assert chat_mod._normalized_query("not-json") is None
    assert chat_mod._normalized_query(json.dumps({"query": "   "})) is None
    assert chat_mod._normalized_query(json.dumps({})) is None


def test_repeated_search_event_keeps_the_result():
    """通知するだけ。**結果を空にしたり実行を止めたりしない**（案 C を却下した理由）。"""
    ev = chat_mod._repeated_search_event("受付可否 api")
    assert "repeated_search" in ev
    assert "notice" in ev
    # 「返さない」「中止」等の握り潰しを示す語を含めない
    assert "中止" not in ev["notice"]


def test_repeated_search_event_truncates_long_query():
    ev = chat_mod._repeated_search_event("あ" * 200)
    assert len(ev["repeated_search"]["query"]) <= 61
