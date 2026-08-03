"""AGT-05: 文書検索をツール往復(ホップ)の予算から外し、検索には別枠の上限を置く。

固めるのは「予算の配分」だけ:
- adb 経路の `rag_search` を何回呼んでも**ホップの残りが減らない**
- ホップは**業務の操作**(外部HTTPツール・MCP・組込ツール)を含む往復でだけ減る
- 検索の上限に達したら `limit_reached` で知らせ、**ホップ上限とは reason で区別**できる
- 上限を跨いだ回の検索結果を**切り詰めない**(黙って結果を減らさない)
- `vector_store` 経路(file_search built-in)の挙動は**変わらない**

多段の業務フローが最後まで通るかは実環境 E2E で見る(`runs/<run-id>/e2e/`)。
"""

import json

import pytest
from fastapi.testclient import TestClient

import jetuse_core.chat as chat_mod
import jetuse_core.tools as tools_mod
from jetuse_core.settings import (
    AGENT_MAX_DOC_SEARCHES_CEILING,
    AGENT_MAX_DOC_SEARCHES_DEFAULT,
    AGENT_MAX_TOOL_HOPS_CEILING,
    get_settings,
)
from service.main import app

client = TestClient(app)

# `rag_adb.search` が返す 1 件(チャンク単位の出典つき)。本文が切り詰められていないかを見る
ADB_HIT = {
    "text": "申込は前営業日 17:00 までに受け付ける。",
    "score": 0.87,
    "file_id": "f1",
    "filename": "受付仕様.xlsx",
    "source": {"chunk_id": "f1-3", "file": "受付仕様.xlsx", "sheet": "制約",
               "cells": "C5:E5", "version": "1.0", "current_version": "Y"},
}


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


def scripted_calls(script: list[list[str]], tail: str | None = None):
    """ホップごとに「呼ぶツール名の並び」を返すモデル。

    script を使い切ったら tail を呼び続ける(tail=None なら最終回答を返して止まる)。
    毎回の tools を記録するので、途中でツールが外れたことも見られる。
    """
    state: dict = {"n": 0, "tools": []}

    class FakeResponses:
        def create(self, **kw):
            state["tools"].append(kw.get("tools"))
            i = state["n"]
            state["n"] += 1
            names = script[i] if i < len(script) else ([tail] if tail else [])
            if not names:
                return FakeStream([FakeEvent("response.output_text.delta", delta="答え")])
            return FakeStream([
                FakeEvent("response.output_item.done",
                          item=FakeItem(type="function_call", name=name,
                                        arguments='{"query": "x"}',
                                        call_id=f"c{i}-{j}", id=None))
                for j, name in enumerate(names)
            ])

    class FakeClient:
        responses = FakeResponses()

    return FakeClient(), state


@pytest.fixture()
def fake_adb_search(monkeypatch):
    """`rag_adb.search` を差し替える(実行された検索の回数を数える)。"""
    calls = []

    def search(owner, query, *, k=5, filters=None):
        calls.append({"owner": owner, "query": query})
        return [ADB_HIT]

    import jetuse_core.rag_adb as rag_adb_mod
    monkeypatch.setattr(rag_adb_mod, "search", search)
    return calls


@pytest.fixture()
def no_business_tool_side_effects(monkeypatch):
    """業務ツールの実体は呼ばない(見たいのは予算の配分だけ)。

    `rag_search` は実装どおり `rag_adb.search`(差し替え済み)へ通す。
    """
    real = tools_mod.execute_with

    def execute_with(registry, name, arguments):
        if name == tools_mod.RAG_SEARCH:
            return real(registry, name, arguments)
        return '{"ok": true}'

    monkeypatch.setattr(tools_mod, "execute_with", execute_with)


def adb_agent(enabled_tools=("rag_search",), **kw):
    """adb 経路のエージェント実行(検索が function tool になる構成)。"""
    return list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, enabled_tools=list(enabled_tools),
        rag_backend="adb", rag_owner="owner-1", **kw,
    ))


def offered_tools(state: dict) -> list[list[str]]:
    """ツールを渡した往復ごとのツール名(最終回答の 1 往復は tools を渡さない)。"""
    return [tool_names(t) for t in state["tools"] if t is not None]


def limits(events: list[dict]) -> list[dict]:
    return [e["limit_reached"] for e in events if "limit_reached" in e]


def tool_names(specs: list[dict] | None) -> list[str]:
    return [s.get("name", s.get("type", "")) for s in (specs or [])]


# --- 上限値の解決 -------------------------------------------------------------


def test_resolve_max_doc_searches_uses_settings_by_default():
    get_settings.cache_clear()
    assert chat_mod.resolve_max_doc_searches() == AGENT_MAX_DOC_SEARCHES_DEFAULT


def test_resolve_max_doc_searches_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "3")
    get_settings.cache_clear()
    try:
        assert chat_mod.resolve_max_doc_searches() == 3
    finally:
        get_settings.cache_clear()


def test_resolve_max_doc_searches_rejects_above_ceiling(monkeypatch):
    """天井を超える設定はクランプせず拒否する（ホップ上限と同じ扱い）。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", str(AGENT_MAX_DOC_SEARCHES_CEILING + 1))
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="ceiling"):
            chat_mod.resolve_max_doc_searches()
    finally:
        get_settings.cache_clear()


def test_tool_results_accepts_hop_and_doc_search_ceilings(monkeypatch):
    """承認往復で送り返せる件数が「ホップ天井 + 検索天井」まで在る。

    検索をホップの予算から外した以上、1 ターンに積める結果はホップ天井だけでは
    足りない。ここが小さいと、検索を挟む承認往復が予算判定に届く前に 422 で詰まる。
    """
    import service.main as service_main

    def one_delta(*args, **kw):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_agent", one_delta)
    results = [{"call": {"type": "function_call", "call_id": f"c{i}"}, "output": "{}"}
               for i in range(AGENT_MAX_TOOL_HOPS_CEILING + AGENT_MAX_DOC_SEARCHES_CEILING)]
    res = client.post("/api/chat/stream", json=_agent_body(
        tool_results=results, max_tool_hops=AGENT_MAX_TOOL_HOPS_CEILING))
    assert res.status_code == 200


@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_resolve_max_doc_searches_rejects_bad_values(monkeypatch, bad):
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", bad)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="AGENT_MAX_DOC_SEARCHES"):
            chat_mod.resolve_max_doc_searches()
    finally:
        get_settings.cache_clear()


# --- 検索はホップを消費しない -------------------------------------------------


def test_doc_searches_do_not_consume_hops(monkeypatch, fake_adb_search,
                                          no_business_tool_side_effects):
    """検索を何回挟んでも、ホップの残りは業務の操作の分しか減らない。"""
    # 5 回検索 → そのあと業務ツールを呼び続ける。ホップ上限は 2
    fake, state = scripted_calls([["rag_search"]] * 5, tail="web_search")
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
    events = adb_agent(max_tool_hops=2)
    # 検索 5 往復 + 業務 2 ホップ + 最終回答 1 = 8。検索がホップを食っていたら 3 で終わる
    assert state["n"] == 8
    assert limits(events) == [{"reason": "max_tool_hops", "limit": 2}]
    assert len(fake_adb_search) == 5  # 検索は 5 回とも実行された


def test_hop_limit_counts_a_mixed_batch_once(monkeypatch, fake_adb_search,
                                             no_business_tool_side_effects):
    """同じ往復に検索と業務の操作が混ざったら、ホップは 1 回だけ減る。"""
    fake, state = scripted_calls([["rag_search", "web_search"]] * 3, tail=None)
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
    events = adb_agent(max_tool_hops=2)
    assert state["n"] == 3  # 2 ホップで打ち切り(3 回目の script は回らない) + 最終回答 1
    assert limits(events) == [{"reason": "max_tool_hops", "limit": 2}]


# --- 検索側の上限 -------------------------------------------------------------


def test_doc_search_limit_is_reported_with_its_own_reason(
    monkeypatch, fake_adb_search, no_business_tool_side_effects
):
    """検索の上限に達したら reason=max_doc_searches で知らせ、業務の操作は続けられる。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "2")
    get_settings.cache_clear()
    try:
        fake, state = scripted_calls([["rag_search"], ["rag_search"]], tail="web_search")
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(enabled_tools=("rag_search", "web_search"), max_tool_hops=2)
    finally:
        get_settings.cache_clear()
    assert limits(events) == [
        {"reason": "max_doc_searches", "limit": 2},   # 検索の予算が尽きた
        {"reason": "max_tool_hops", "limit": 2},      # そのあと業務の予算で止まった
    ]
    # 上限に達したあとの往復では検索ツールが外れている(業務ツールは残る)
    offered = offered_tools(state)
    assert "rag_search" in offered[0] and "web_search" in offered[0]
    assert "rag_search" not in offered[-1]
    assert "web_search" in offered[-1]
    # 画面に何も出ないのを避けるため本文にも出す(ADR-0025 §3 と同じ扱い)
    assert any("文書検索" in e.get("delta", "") for e in events)


def test_doc_search_limit_does_not_truncate_results(monkeypatch, fake_adb_search):
    """上限を跨いだ回の検索も**全部実行して結果を返す**(黙って減らさない)。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "1")
    get_settings.cache_clear()
    try:
        fake, _ = scripted_calls([["rag_search", "rag_search", "rag_search"]], tail=None)
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(max_tool_hops=4)
    finally:
        get_settings.cache_clear()
    results = [e["tool_result"] for e in events if "tool_result" in e]
    assert len(results) == 3            # 3 件とも実行される(切り落とさない)
    assert len(fake_adb_search) == 3
    for r in results:                   # 本文も切り詰めない
        assert ADB_HIT["text"] in r["preview"]
    assert limits(events) == [{"reason": "max_doc_searches", "limit": 1}]


def test_doc_search_limit_alone_does_not_hit_the_hop_limit(
    monkeypatch, fake_adb_search, no_business_tool_side_effects
):
    """検索だけで終わる要求は、ホップ上限には当たらない(検索の上限で止まる)。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "3")
    get_settings.cache_clear()
    try:
        fake, state = scripted_calls([["rag_search"]] * 3, tail=None)
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(max_tool_hops=2)
    finally:
        get_settings.cache_clear()
    assert limits(events) == [{"reason": "max_doc_searches", "limit": 3}]
    assert state["n"] == 4  # 検索 3 往復 + 検索ツールを外した最終の 1 往復


def test_doc_search_budget_carries_over_approval_round_trips(monkeypatch,
                                                             fake_adb_search):
    """承認往復で送り返された検索も数える(0 から数え直すと上限が効かない)。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "2")
    get_settings.cache_clear()
    try:
        fake, state = scripted_calls([], tail=None)
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        results = [{"call": {"type": "function_call", "name": "rag_search",
                             "call_id": f"c{i}"}, "output": "{}"} for i in range(2)]
        events = adb_agent(tool_results=results, max_tool_hops=3)
    finally:
        get_settings.cache_clear()
    assert limits(events) == [{"reason": "max_doc_searches", "limit": 2}]
    assert "rag_search" not in offered_tools(state)[0]  # 最初の往復から検索は渡さない


def test_doc_search_after_the_limit_is_refused_not_executed(
    monkeypatch, fake_adb_search, no_business_tool_side_effects
):
    """上限後にモデルが検索を呼んでも**実行しない**(理由を返す)。

    ツール一覧から外してもモデルが名前を出すことはある。実行してしまうと上限が
    上限でなくなる(ホップ上限のぶんだけ超過できる)。
    """
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "1")
    get_settings.cache_clear()
    try:
        fake, state = scripted_calls([], tail="rag_search")  # 呼び続けるモデル
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(max_tool_hops=2)
    finally:
        get_settings.cache_clear()
    assert len(fake_adb_search) == 1  # 上限までの 1 回だけが実行された
    previews = [e["tool_result"]["preview"] for e in events if "tool_result" in e]
    assert "上限" in previews[-1]     # 断った理由がモデルに届く(黙って空を返さない)
    # 上限後の呼び出しはホップを消費する = 止まることが保証される
    assert [lr["reason"] for lr in limits(events)] == ["max_doc_searches", "max_tool_hops"]


def test_cumulative_tool_results_cap_ignores_doc_searches(monkeypatch, fake_adb_search):
    """承認往復の累計上限(AGT-01d)も検索は数えない(検索は別枠)。"""
    fake, state = scripted_calls([], tail=None)
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
    results = [{"call": {"type": "function_call", "name": "rag_search",
                         "call_id": f"c{i}"}, "output": "{}"}
               for i in range(chat_mod.MIN_TOOL_RESULTS_CAP)]
    events = adb_agent(tool_results=results, max_tool_hops=5)
    assert limits(events) == []                          # 検索だけでは打ち切らない
    assert "rag_search" in tool_names(state["tools"][0])  # ツールも外れていない


# --- vector_store 経路(built-in)の回帰 ---------------------------------------


def test_vector_store_backend_is_unchanged(monkeypatch):
    """built-in の file_search はもともとホップを消費しない。挙動を変えない。"""
    state: dict = {"n": 0, "tools": []}

    class FakeResponses:
        def create(self, **kw):
            state["tools"].append(kw.get("tools"))
            state["n"] += 1
            # built-in の検索は function_call ではなく file_search_call として現れる
            return FakeStream([
                FakeEvent("response.output_item.added",
                          item=FakeItem(type="file_search_call")),
                FakeEvent("response.output_text.delta", delta="答え"),
            ])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        auto_tools=True, enabled_tools=["rag_search"], rag_store="vs_1",
    ))
    assert state["n"] == 1
    assert state["tools"][0] == [{"type": "file_search", "vector_store_ids": ["vs_1"]}]
    assert limits(events) == []
    assert any(e.get("tool_call", {}).get("name") == "rag_search" for e in events)


def test_vector_store_backend_ignores_doc_search_setting(monkeypatch,
                                                         no_business_tool_side_effects):
    """built-in 経路は検索の上限設定に影響されない(数える対象がそもそも無い)。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "1")
    get_settings.cache_clear()
    try:
        fake, state = scripted_calls([], tail="web_search")
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = list(chat_mod.stream_agent(
            "gpt-oss-120b", [{"role": "user", "content": "q"}],
            auto_tools=True, enabled_tools=["rag_search"], rag_store="vs_1",
            max_tool_hops=3,
        ))
    finally:
        get_settings.cache_clear()
    assert state["n"] == 4  # 3 ホップ + 最終回答
    assert limits(events) == [{"reason": "max_tool_hops", "limit": 3}]


# --- ルート境界 ---------------------------------------------------------------


def _agent_body(**kw):
    return {"model": "gpt-oss-120b", "agent": True, "auto_tools": True,
            "messages": [{"role": "user", "content": "x"}], **kw}


def test_bad_doc_search_setting_breaks_only_the_adb_agent_path(monkeypatch):
    """設定ミスは入口で断る。素のチャットも vector_store 経路も巻き添えにしない。"""
    import service.main as service_main
    import service.routes.chat as chat_route

    def one_delta(*args, **kw):
        yield {"delta": "ok"}

    monkeypatch.setattr(chat_route.rag_adb, "availability",
                        lambda: chat_route.rag_adb.READY)
    monkeypatch.setattr(service_main, "stream_chat", one_delta)
    monkeypatch.setattr(service_main, "stream_agent", one_delta)
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "abc")
    get_settings.cache_clear()
    try:
        plain = client.post("/api/chat/stream", json={
            "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}]})
        assert plain.status_code == 200
        vector_store = client.post("/api/chat/stream", json=_agent_body())
        assert vector_store.status_code == 200
        adb = client.post("/api/chat/stream", json=_agent_body(
            agent_rag_backend="adb", enabled_tools=["rag_search"]))
        assert adb.status_code == 400
        assert "AGENT_MAX_DOC_SEARCHES" in adb.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_doc_search_notice_is_json_serializable(monkeypatch, fake_adb_search):
    """SSE に載る形であること(通知が届かなければ「黙って打ち切り」と同じ)。"""
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "1")
    get_settings.cache_clear()
    try:
        fake, _ = scripted_calls([["rag_search"]], tail=None)
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(max_tool_hops=1)
    finally:
        get_settings.cache_clear()
    notice = next(e for e in events if "limit_reached" in e)
    assert json.loads(json.dumps(notice, ensure_ascii=False))["limit_reached"] == {
        "reason": "max_doc_searches", "limit": 1
    }
    assert "文書検索" in notice["notice"]


def test_both_limits_reached_in_one_batch_still_notifies_doc_search(
    monkeypatch, fake_adb_search, no_business_tool_side_effects
):
    """検索とホップの上限に**同時に**達しても検索側の通知が出る（review-6）。

    混在バッチ（検索 + 業務ツール）で両方の予算が尽きると、次の周回に入らずループを抜ける。
    ループ先頭のチェックだけだと `reason=max_doc_searches` が落ち、
    「検索の予算が尽きた」ことを応答から追えなくなる（どちらの設定を直せばよいか分からない）。
    """
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "1")
    get_settings.cache_clear()
    try:
        # 1 往復で検索と業務ツールを同時に呼ぶ = 検索 1/1・ホップ 1/1 が同時に尽きる
        fake, state = scripted_calls([["rag_search", "web_search"]])
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(enabled_tools=("rag_search", "web_search"), max_tool_hops=1)
    finally:
        get_settings.cache_clear()

    reasons = [e["limit_reached"]["reason"] for e in events if "limit_reached" in e]
    assert "max_doc_searches" in reasons, reasons
    assert "max_tool_hops" in reasons, reasons


def test_server_side_business_op_consumes_a_hop(
    monkeypatch, fake_adb_search, no_business_tool_side_effects
):
    """検索と**サーバー側で実行される業務の操作**が混ざってもホップが減る（review-7）。

    MCP / コード実行は `function_call` として返らないので、`call_dicts` だけを見ると
    「このバッチは検索だけ」に見えてホップが減らない。業務の操作が走ったのに予算が
    減らないと、検索の上限まで業務の操作を余分に実行できてしまう（上限の迂回）。
    """
    monkeypatch.setenv("AGENT_MAX_DOC_SEARCHES", "40")
    get_settings.cache_clear()
    try:
        # 検索だけの function_call に、OCI 側実行（コード実行）が同じ往復で混ざる状況
        fake, state = scripted_calls([], tail="rag_search")
        real_collect = chat_mod._collect_hop_events

        def collect_with_server_side(stream, calls, mcp_approvals, server_side=None):
            yield from real_collect(stream, calls, mcp_approvals, server_side)
            if server_side is not None and calls:
                server_side.append("code_interpreter_call")

        monkeypatch.setattr(chat_mod, "_collect_hop_events", collect_with_server_side)
        monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake)
        events = adb_agent(max_tool_hops=2)
    finally:
        get_settings.cache_clear()

    reasons = [e["limit_reached"]["reason"] for e in events if "limit_reached" in e]
    # 検索の上限(40)には遠いので、止まるならホップ側でなければならない
    assert "max_tool_hops" in reasons, reasons
