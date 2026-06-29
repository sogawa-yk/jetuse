"""エージェントフレームワーク(AGT-01)のテスト。"""

import json

import pytest
from fastapi.testclient import TestClient

import jetuse_core.chat as chat_mod
import service.main as service_main
from jetuse_core.tools import TOOLS, ToolError, _validate_args, execute_tool, tool_specs
from service.main import app

client = TestClient(app)


def test_tool_specs_include_custom_and_builtin():
    specs = tool_specs()
    names = [s.get("name") for s in specs if s["type"] == "function"]
    assert "web_search" in names and "web_fetch" in names
    assert any(s["type"] == "code_interpreter" for s in specs)


def test_execute_tool_guards():
    with pytest.raises(ToolError):
        execute_tool("no_such_tool", "{}")
    with pytest.raises(ToolError):
        execute_tool("code_interpreter", "{}")  # built-inはサーバー実行不可
    with pytest.raises(ToolError):
        execute_tool("web_search", "[1,2]")  # オブジェクトでない
    with pytest.raises(ToolError):
        execute_tool("web_search", json.dumps({"q": "typo"}))  # 未知の引数
    with pytest.raises(ToolError):
        _validate_args(TOOLS["web_search"], {"query": 123})  # 型不正


def test_execute_tool_endpoint(monkeypatch):
    monkeypatch.setitem(
        TOOLS, "web_search",
        TOOLS["web_search"].__class__(**{
            **TOOLS["web_search"].__dict__,
            "handler": lambda args: json.dumps({"results": [{"title": "t"}]}),
        }),
    )
    res = client.post("/api/agent/execute-tool",
                      json={"name": "web_search", "arguments": '{"query": "oci"}'})
    assert res.status_code == 200
    assert "results" in res.json()["output"]
    res2 = client.post("/api/agent/execute-tool", json={"name": "nope", "arguments": "{}"})
    assert res2.status_code == 400


def test_agent_stream_approval_mode(monkeypatch):
    def fake_agent(model_key, messages, temperature=None, user="",
                   auto_tools=False, tool_results=None, params=None,
                   enabled_tools=None, mcp_servers=None,
                   instructions=None, project_ocid=None, rag_store=None):
        yield {"delta": "調べます。"}
        yield {"tool_call": {"name": "web_search", "label": "Web検索",
                             "arguments": '{"query": "x"}', "call_id": "c1",
                             "item": {"type": "function_call", "name": "web_search",
                                      "arguments": '{"query": "x"}', "call_id": "c1"},
                             "status": "pending_approval"}}

    monkeypatch.setattr(service_main, "stream_agent", fake_agent)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "agent": True,
        "messages": [{"role": "user", "content": "OCIの最新情報"}],
    })
    assert res.status_code == 200
    assert '"tool_call"' in res.text and '"pending_approval"' in res.text


def test_agent_rejects_chat_family_and_rag_combo():
    res = client.post("/api/chat/stream", json={
        "model": "llama-3.3-70b", "agent": True,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert res.status_code == 400
    res2 = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "agent": True, "rag": True,
        "messages": [{"role": "user", "content": "x"}],
    })
    assert res2.status_code == 400


def test_stream_agent_auto_mode_loops(monkeypatch):
    """auto_tools: function_call→実行→継続→最終回答のループ"""

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

    hops = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            hops["n"] += 1
            if hops["n"] == 1:
                call = FakeItem(type="function_call", name="web_search",
                                arguments='{"query": "oci"}', call_id="c1", id=None)
                return FakeStream([FakeEvent("response.output_item.done", item=call)])
            return FakeStream([FakeEvent("response.output_text.delta", delta="答え")])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    import jetuse_core.tools as tools_mod
    monkeypatch.setattr(tools_mod, "execute_tool",
                        lambda name, args: '{"results": []}')

    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}], auto_tools=True,
    ))
    kinds = [next(iter(e)) for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds and "delta" in kinds
    assert hops["n"] == 2


def test_tool_specs_filtering():
    only_search = tool_specs(["web_search"])
    assert [s.get("name") for s in only_search if s["type"] == "function"] == ["web_search"]
    assert not any(s["type"] == "code_interpreter" for s in only_search)
    with_ci = tool_specs(["code_interpreter"])
    assert any(s["type"] == "code_interpreter" for s in with_ci)
    assert not any(s["type"] == "function" for s in with_ci)


def test_tools_list_endpoint():
    res = client.get("/api/agent/tools")
    assert res.status_code == 200
    names = [tl["name"] for tl in res.json()["tools"]]
    assert "web_search" in names and "code_interpreter" in names


class FakeMcpRepo:
    def __init__(self):
        self.store = {}

    def list_servers(self, owner):
        return [{"id": k, **v, "has_auth": False} for k, v in self.store.items()]

    def get_servers(self, owner, ids):
        return [{"id": k, **v, "auth_secret_ocid": None}
                for k, v in self.store.items() if k in ids]

    def create_server(self, owner, label, url, auth_secret_ocid=None, *, auth_token=None):
        sid = f"m{len(self.store) + 1}"
        # 認証付きは実トークンを保存しない(Vault 束ねの代理: has_auth のみ)。
        # 本番と同じく空/空白トークンは認証なし扱いに揃える(BE08-R3-005)。
        self.store[sid] = {"label": label, "url": url}
        has_auth = bool((auth_token and auth_token.strip()) or auth_secret_ocid)
        return {"id": sid, "label": label, "url": url, "has_auth": has_auth}

    def delete_server(self, owner, sid):
        return self.store.pop(sid, None) is not None


@pytest.fixture()
def fake_mcp(monkeypatch):
    fake = FakeMcpRepo()
    for n in ("list_servers", "get_servers", "create_server", "delete_server"):
        monkeypatch.setattr(service_main.mcp_repo, n, getattr(fake, n))
    return fake


def test_mcp_server_crud(fake_mcp):
    res = client.post("/api/agent/mcp-servers",
                      json={"label": "deepwiki", "url": "https://mcp.deepwiki.com/mcp"})
    assert res.status_code == 200
    sid = res.json()["id"]
    assert any(s["id"] == sid for s in client.get("/api/agent/mcp-servers").json()["servers"])
    assert client.delete(f"/api/agent/mcp-servers/{sid}").json() == {"deleted": True}


def test_mcp_server_with_token_registers_via_vault(fake_mcp):
    # BE-08: 認証付き登録は 501 ではなく成功し、has_auth=True(実トークンは Vault 束ね)。
    res = client.post("/api/agent/mcp-servers", json={
        "label": "x", "url": "https://example.com/mcp", "auth_token": "secret"})
    assert res.status_code == 200
    assert res.json()["has_auth"] is True


def test_mcp_server_auth_fail_closed_when_vault_unconfigured(monkeypatch):
    """BE-08: Vault 未設定なら認証付き登録は 503 で fail-closed(実値は書かない)。"""
    import jetuse_core.mcp_servers as mcp_servers
    from jetuse_core.settings import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(
        mcp_servers, "get_settings",
        lambda: Settings(vault_ocid="", vault_key_ocid="", compartment_ocid=""),
    )
    # validate_url は通す(SSRF ガードは別経路)。connect は呼ばれない想定だが念のため監視。
    called = {"connect": False}

    def _no_connect(*a, **k):
        called["connect"] = True
        raise AssertionError("Vault 書込前に DB へ触れてはならない")

    monkeypatch.setattr(mcp_servers, "connect", _no_connect)
    res = client.post("/api/agent/mcp-servers", json={
        "label": "x", "url": "https://example.com/mcp", "auth_token": "secret"})
    assert res.status_code == 503
    assert called["connect"] is False
    get_settings.cache_clear()


def test_mcp_server_auth_ssrf_before_vault(monkeypatch):
    """BE-08: 不正 URL は Vault 書込の前に fail-closed(_write_secret を呼ばない)。"""
    import jetuse_core.mcp_servers as mcp_servers

    def _boom(*a, **k):
        raise AssertionError("不正 URL なのに Vault 書込が呼ばれた")

    monkeypatch.setattr(mcp_servers, "_write_secret", _boom)
    res = client.post("/api/agent/mcp-servers", json={
        "label": "x", "url": "http://example.com/mcp", "auth_token": "secret"})
    assert res.status_code == 400  # https でない → SsrfBlockedError


def test_mcp_url_validation():
    from jetuse_core.mcp_servers import validate_url
    from jetuse_core.webtools import SsrfBlockedError
    with pytest.raises(SsrfBlockedError):
        validate_url("http://example.com/mcp")  # httpsでない
    with pytest.raises(SsrfBlockedError):
        validate_url("https://169.254.169.254/mcp")  # メタデータ


def test_stream_agent_mcp_approval_continuation(monkeypatch):
    """mcp_approval_requestのtool_results継続でmcp_approval_responseが構築される"""
    captured = {}

    class FakeStream:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class FakeResponses:
        def create(self, **kw):
            captured["input"] = kw["input"]
            return FakeStream()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    approval_item = {"type": "mcp_approval_request", "id": "mcpr_1",
                     "name": "read_wiki", "server_label": "deepwiki"}
    list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "q"}],
        tool_results=[{"call": approval_item, "output": "approve"}],
    ))
    types = [i.get("type") for i in captured["input"]]
    assert "mcp_approval_request" in types and "mcp_approval_response" in types
    resp = next(i for i in captured["input"] if i.get("type") == "mcp_approval_response")
    assert resp["approve"] is True and resp["approval_request_id"] == "mcpr_1"


class FakeAgentsRepo:
    def __init__(self):
        self.store = {}

    def list_agents(self, owner):
        return [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "instructions"},
                 "mine": v["owner_sub"] == owner}
                for k, v in self.store.items()
                if v["owner_sub"] == owner or v["visibility"] == "public"]

    def get_agent(self, owner, aid):
        v = self.store.get(aid)
        if not v or (v["owner_sub"] != owner and v["visibility"] != "public"):
            return None
        return {**v, "id": aid, "mine": v["owner_sub"] == owner}

    def create_agent(self, owner, data):
        aid = f"a{len(self.store) + 1}"
        self.store[aid] = {**data, "owner_sub": owner}
        return {**data, "id": aid, "mine": True}

    def update_agent(self, owner, aid, data):
        v = self.store.get(aid)
        if not v or v["owner_sub"] != owner:
            return None
        self.store[aid] = {**data, "owner_sub": owner}
        return {**data, "id": aid, "mine": True}

    def delete_agent(self, owner, aid):
        v = self.store.get(aid)
        if not v or v["owner_sub"] != owner:
            return False
        del self.store[aid]
        return True


@pytest.fixture()
def fake_agents(monkeypatch):
    fake = FakeAgentsRepo()
    for n in ("list_agents", "get_agent", "create_agent", "update_agent", "delete_agent"):
        monkeypatch.setattr(service_main.agents_repo, n, getattr(fake, n))
    return fake


AGENT_DEF = {
    "name": "規程アシスタント", "instructions": "丁寧に答える",
    "model": "gpt-oss-120b", "enabled_tools": ["web_search"],
}


def test_agent_crud_and_validation(fake_agents):
    res = client.post("/api/agents", json=AGENT_DEF)
    assert res.status_code == 200
    aid = res.json()["id"]
    assert client.get(f"/api/agents/{aid}").json()["mine"] is True
    bad = {**AGENT_DEF, "enabled_tools": ["no_such_tool"]}
    assert client.post("/api/agents", json=bad).status_code == 422
    # ADR-0009 hostedルーティング: ツールはコンテナ内でそのSDKのモデルが実行するため、
    # 旧「ツール付きはResponses系モデルのみ」制約は撤廃。llama+toolsも受理される(200)。
    ok2 = {**AGENT_DEF, "model": "llama-3.3-70b"}
    assert client.post("/api/agents", json=ok2).status_code == 200
    assert client.delete(f"/api/agents/{aid}").json() == {"deleted": True}


def test_chat_with_agent_applies_instructions(fake_agents, monkeypatch):
    """ADR-0009: 保存済みagentはhostedコンテナへルーティングされ、instructionsは
    system_promptとして、モデルは定義側の値(MODELS経由のoci_id)としてstateに載る。
    呼び出し側モデル(llama-3.3-70b)は定義側(gpt-oss-120b)で上書きされる。"""
    res = client.post("/api/agents", json={**AGENT_DEF, "enabled_tools": []})
    aid = res.json()["id"]
    captured = {}

    def fake_invoke(sdk, state):
        captured["sdk"] = sdk
        captured["state"] = state
        return {"output": "ok", "tool_trace": []}

    monkeypatch.setattr(service_main.hosted_agent, "invoke_agent", fake_invoke)
    r = client.post("/api/chat/stream", json={
        "model": "llama-3.3-70b",  # エージェント定義(gpt-oss)で上書きされる
        "agent_id": aid,
        "messages": [{"role": "user", "content": "q"}],
    })
    assert r.status_code == 200
    assert "ok" in r.text
    # 定義側モデル(gpt-oss-120b)のoci_idがstateに反映される
    from jetuse_core.models import MODELS
    assert captured["state"]["model"] == MODELS["gpt-oss-120b"].oci_id
    assert "丁寧" in captured["state"]["system_prompt"]


def test_chat_with_unknown_agent_404(fake_agents):
    r = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "agent_id": "nope",
        "messages": [{"role": "user", "content": "q"}],
    })
    assert r.status_code == 404


def test_stream_agent_safe_tools_auto_execute_in_approval_mode(monkeypatch):
    """requires_approval=Falseのツールは承認モードでも自動実行され、ループ継続する"""

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

    hops = {"n": 0}

    class FakeResponses:
        def create(self, **kw):
            hops["n"] += 1
            if hops["n"] == 1:
                call = FakeItem(type="function_call", name="get_current_time",
                                arguments="{}", call_id="c1", id=None)
                return FakeStream([FakeEvent("response.output_item.done", item=call)])
            return FakeStream([FakeEvent("response.output_text.delta", delta="いま")])

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: FakeClient())
    events = list(chat_mod.stream_agent(
        "gpt-oss-120b", [{"role": "user", "content": "今何時?"}], auto_tools=False,
    ))
    kinds = [next(iter(e)) for e in events]
    assert "tool_result" in kinds and "delta" in kinds  # 承認なしで完走
    assert not any(
        e.get("tool_call", {}).get("status") == "pending_approval" for e in events
    )
    assert hops["n"] == 2


def test_agent_accepts_all_listed_tools(fake_agents):
    """ADR-0009: hostedコンテナが内蔵するツール(code_interpreter以外)はエージェント定義で
    受理される(回帰: rag_search 422)。code_interpreterはコンテナ未対応のため除外して検証。"""
    from jetuse_core.tools import list_tools

    # code_interpreterはhostedコンテナ非対応(validated() main.py:168-177)
    container_names = [tl["name"] for tl in list_tools() if tl["name"] != "code_interpreter"]
    res = client.post("/api/agents", json={
        "name": "全ツール", "instructions": "x", "model": "gpt-oss-120b",
        "enabled_tools": container_names,
    })
    assert res.status_code == 200, res.text
    # code_interpreterは明示的に拒否される
    bad = client.post("/api/agents", json={
        "name": "ci", "instructions": "x", "model": "gpt-oss-120b",
        "enabled_tools": ["code_interpreter"],
    })
    assert bad.status_code == 422
    assert "code_interpreter" in bad.json()["detail"]
