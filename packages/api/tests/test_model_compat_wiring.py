"""AGT-06: 吸収層がエージェントの実際の入力に効いているか。

`model_compat` 単体は test_model_compat.py で見る。ここで固めるのは
「`responses.create` に**実際に渡る** input が、モデルに合わせて整えられているか」。
吸収層を作っても呼び出し側に挿さっていなければ、実機でしか壊れが出ない
(gemini はまさにその形で 400 になっていた)。
"""

import json

import pytest
from fastapi.testclient import TestClient

import jetuse_core.chat as chat_mod
from jetuse_core.tools import ToolDef
from service.main import app

client = TestClient(app)


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


# このターン限りのツール。**組込の web_search を使わない** —— `stream_agent` は
# `execute_with` を関数内で import するため `chat_mod` への monkeypatch が効かず、
# 組込名を返すと実ハンドラ(DuckDuckGo)へ本当に到達する(review-2 m004)。
# 単体テストは外部へ出ない。ここで自前ハンドラのツールを渡して閉じる。
TOOL_CALLS: list[str] = []


def _local_tool():
    def handler(args: dict) -> str:
        TOOL_CALLS.append("local_echo")
        return '{"ok": true}'

    return ToolDef(
        name="local_echo", label="local_echo", description="テスト用。外部へ出ない",
        parameters={"type": "object", "properties": {"q": {"type": "string"}},
                    "required": ["q"]},
        handler=handler, requires_approval=False,
    )


def _recording_client(calls: list[dict], hops: int = 1):
    """`hops` 回だけ function_call を返し、その後は素の応答で終わるスタブ。

    毎回の `input` を記録する(= 吸収層を通った後の姿)。
    """

    class FakeResponses:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) <= hops:
                item = FakeItem(type="function_call", name="local_echo",
                                arguments='{"q": "x"}', call_id="c1", id="fc_1")
                return FakeStream([FakeEvent("response.output_item.done", item=item)])
            return FakeStream([FakeEvent("response.output_text.delta", delta="done")])

    class FakeClient:
        responses = FakeResponses()

    return FakeClient


def _run_agent(monkeypatch, model_key, calls, **kw):
    monkeypatch.setattr(chat_mod, "make_inference_client",
                        lambda **_: _recording_client(calls)())
    return list(chat_mod.stream_agent(
        model_key, [{"role": "user", "content": "問い"}],
        auto_tools=True, instructions="あなたは担当者", enabled_tools=[],
        http_tools=[_local_tool()], **kw,
    ))


def _items(call, type_):
    return [i for i in call["input"] if i.get("type") == type_]


def test_gemini_input_has_no_system_role_items(monkeypatch):
    """gemini は system ロールを 400 で拒否する。1 ホップ目から出してはいけない。"""
    calls: list[dict] = []
    _run_agent(monkeypatch, "gemini-2.5-flash", calls)
    assert calls, "responses.create が呼ばれていない"
    for call in calls:
        assert not [i for i in _items(call, "message") if i.get("role") == "system"]
    # 指示は消えずに user 側へ残っている(内容は変えない)
    first = calls[0]["input"][0]
    assert first["role"] == "user"
    assert first["content"][0]["text"] == "あなたは担当者"


def test_gemini_echoed_function_call_has_no_id(monkeypatch):
    """stream で id 付きの function_call を積み直すと 400 になる(実測)。"""
    calls: list[dict] = []
    _run_agent(monkeypatch, "gemini-2.5-flash", calls)
    echoed = [i for c in calls for i in _items(c, "function_call")]
    assert echoed, "ツール結果の積み直しが起きていない"
    for item in echoed:
        assert "id" not in item
        assert item["call_id"] == "c1", "call_id は結果の対応付けに要る"


def test_gpt_oss_input_is_left_as_is(monkeypatch):
    """差が無いモデルの挙動は変えない(回帰チェック)。"""
    calls: list[dict] = []
    _run_agent(monkeypatch, "gpt-oss-120b", calls)
    assert [i for i in _items(calls[0], "message") if i.get("role") == "system"]
    echoed = [i for c in calls for i in _items(c, "function_call")]
    assert echoed and all(i.get("id") == "fc_1" for i in echoed)


def test_force_answer_message_is_folded_for_gemini(monkeypatch):
    """打ち切り時の force-answer は入力の末尾に付く system アイテム。ここも畳む。"""
    calls: list[dict] = []
    _run_agent(monkeypatch, "gemini-2.5-flash", calls, max_tool_hops=1)
    last = calls[-1]["input"][-1]
    assert last["role"] == "user"
    assert not any(i.get("role") == "system" for i in calls[-1]["input"]
                   if i.get("type") == "message")


def test_agent_refuses_model_without_client_side_tools(monkeypatch):
    """Responses 系でもツールを渡せないモデルがある。api 属性だけでは足りない。"""
    events = list(chat_mod.stream_agent(
        "grok-4.20-multi-agent", [{"role": "user", "content": "x"}], auto_tools=True,
    ))
    assert len(events) == 1
    assert "client-side tools" in events[0]["error"]


@pytest.mark.parametrize("model_key", ["grok-4.20-multi-agent", "llama-3.3-70b"])
def test_route_rejects_agent_incapable_model_with_reason(model_key: str):
    """黙って壊れず、理由の分かる 400 で断る。"""
    res = client.post("/api/chat/stream", json={
        "model": model_key, "agent": True,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "エージェント" in detail and len(detail) > 20


def test_route_allows_agent_capable_grok(monkeypatch):
    """Grok 系がエージェントで通ること(断りの条件が広すぎないことの裏)。"""
    import service.main as service_main

    def fake_agent(model_key, messages, *a, **kw):
        yield {"delta": f"ok:{model_key}"}

    monkeypatch.setattr(service_main, "stream_agent", fake_agent)
    res = client.post("/api/chat/stream", json={
        "model": "grok-4.3", "agent": True,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert res.status_code == 200
    assert "ok:grok-4.3" in res.text


def test_models_endpoint_exposes_agent_capability():
    """UI が選ばせる前に弾けるよう、可否を一覧に出す。"""
    res = client.get("/api/chat/models")
    assert res.status_code == 200
    by_key = {m["key"]: m for m in res.json()["models"]}
    assert by_key["grok-4.3"]["agent"] is True
    assert by_key["grok-4.20-multi-agent"]["agent"] is False
    assert by_key["grok-4.20-multi-agent"]["agent_blocked_reason"]
    assert json.dumps(by_key, ensure_ascii=False)  # 直列化できること


def test_agent_tools_do_not_reach_the_network(monkeypatch):
    """このファイルのテストは外部へ出ない。

    `stream_agent` は `execute_with` を関数内 import するので、`chat_mod` 側の
    monkeypatch では止められない。組込の `web_search` を返すスタブにすると
    実際に DuckDuckGo を叩く(review-2 m004 で発覚)。自前ハンドラで閉じる。
    """
    TOOL_CALLS.clear()
    calls: list[dict] = []
    _run_agent(monkeypatch, "gpt-oss-120b", calls)
    assert TOOL_CALLS == ["local_echo"], "自前ハンドラ以外が実行された"
