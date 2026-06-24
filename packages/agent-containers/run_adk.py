"""Google ADK 版の汎用ReActランナー(ADR-0009 / SPIKE-ADK準拠)。

ADKのLiteLlmはOCIのリクエスト毎IAM署名に非対応のため、署名済みクライアントを使う
カスタム BaseLlm で接続する(SPIKE-ADK)。
"""

import asyncio
import json

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

import agent_common as ac
from server import create_app

SDK = "adk"
DEFAULT_INSTRUCTIONS = "あなたは有能なアシスタント。必要に応じてツールを使い、日本語で簡潔に答える。"


def _to_messages(llm_request):
    msgs = []
    cfg = llm_request.config
    if cfg and cfg.system_instruction:
        si = cfg.system_instruction
        text = si if isinstance(si, str) else " ".join(
            p.text for p in si.parts if getattr(p, "text", None))
        msgs.append({"role": "system", "content": text})
    for content in llm_request.contents or []:
        for part in content.parts or []:
            if getattr(part, "function_call", None):
                fc = part.function_call
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": fc.id or fc.name, "type": "function",
                    "function": {"name": fc.name, "arguments": json.dumps(fc.args or {})}}]})
            elif getattr(part, "function_response", None):
                fr = part.function_response
                msgs.append({"role": "tool", "tool_call_id": fr.id or fr.name,
                             "content": json.dumps(fr.response or {})})
            elif getattr(part, "text", None):
                msgs.append({"role": "assistant" if content.role == "model" else "user",
                             "content": part.text})
    return msgs


def _to_tools(llm_request):
    cfg = llm_request.config
    if not cfg or not cfg.tools:
        return None
    out = []
    for tool in cfg.tools:
        for fd in getattr(tool, "function_declarations", None) or []:
            schema = {"type": "object", "properties": {}}
            if fd.parameters:
                schema = fd.parameters.model_dump(exclude_none=True) \
                    if hasattr(fd.parameters, "model_dump") else dict(fd.parameters)
            out.append({"type": "function", "function": {
                "name": fd.name, "description": fd.description or "", "parameters": schema}})
    return out or None


class OciLlm(BaseLlm):
    async def generate_content_async(self, llm_request, stream: bool = False):
        messages = _to_messages(llm_request)
        tools = _to_tools(llm_request)
        kwargs = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        client = ac.chat_client()
        resp = await asyncio.to_thread(lambda: client.chat.completions.create(**kwargs))
        msg = resp.choices[0].message
        parts = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            parts.append(types.Part(function_call=types.FunctionCall(
                id=tc.id, name=tc.function.name, args=args)))
        if msg.content:
            parts.append(types.Part(text=msg.content))
        if not parts:
            parts.append(types.Part(text=""))
        yield LlmResponse(content=types.Content(role="model", parts=parts))


def _build_tools(enabled, ctx, trace):
    """enabled名 -> ADK用python関数(シグネチャからスキーマ生成)。run_toolへ委譲。"""
    tools = []

    def web_search(query: str) -> dict:
        """Webを検索して上位結果(タイトル/URL/抜粋)を返す。"""
        out = ac.run_tool("web_search", {"query": query}, ctx)
        trace.append(ac.ToolCallTrace(name="web_search", arguments={"query": query},
                                      output_preview=out[:200]))
        return json.loads(out)

    def web_fetch(url: str) -> dict:
        """指定URLのページ本文を取得する。"""
        out = ac.run_tool("web_fetch", {"url": url}, ctx)
        trace.append(ac.ToolCallTrace(name="web_fetch", arguments={"url": url},
                                      output_preview=out[:200]))
        return json.loads(out)

    def get_current_time() -> dict:
        """現在の日本時間(日付/時刻/曜日)を返す。"""
        out = ac.run_tool("get_current_time", {}, ctx)
        trace.append(ac.ToolCallTrace(name="get_current_time", arguments={},
                                      output_preview=out[:200]))
        return json.loads(out)

    def rag_search(query: str) -> dict:
        """アップロード済み文書から関連箇所を検索する。"""
        out = ac.run_tool("rag_search", {"query": query}, ctx)
        trace.append(ac.ToolCallTrace(name="rag_search", arguments={"query": query},
                                      output_preview=out[:200]))
        return json.loads(out)

    def query_database(question: str) -> dict:
        """データベース(販売データ)に自然言語で質問しSQLを生成・実行して結果を返す。"""
        out = ac.run_tool("query_database", {"question": question}, ctx)
        trace.append(ac.ToolCallTrace(name="query_database", arguments={"question": question},
                                      output_preview=out[:200]))
        return json.loads(out)

    available = {"web_search": web_search, "web_fetch": web_fetch,
                 "get_current_time": get_current_time, "rag_search": rag_search,
                 "query_database": query_database}
    for name in enabled:
        if name in available:
            tools.append(available[name])
    return tools


async def run(req: ac.InvokeRequest) -> ac.InvokeResponse:
    ctx = {"rag_store_id": req.rag_store_id}
    trace: list[ac.ToolCallTrace] = []
    agent = Agent(
        name="jetuse_agent", model=OciLlm(model=req.model),
        instruction=req.system_prompt.strip() or DEFAULT_INSTRUCTIONS,
        tools=_build_tools(req.enabled_tools, ctx, trace))
    runner = InMemoryRunner(agent=agent, app_name="jetuse")
    await runner.session_service.create_session(app_name="jetuse", user_id="u", session_id="s")
    # 履歴は簡易にプロンプトへ前置(ADKのsession復元は使わずステートレス運用)
    prefix = ""
    for m in req.history:
        prefix += f"{m['role']}: {m['content']}\n"
    text = (prefix + "user: " + req.input) if prefix else req.input
    msg = types.Content(role="user", parts=[types.Part(text=text)])
    output = ""
    async for event in runner.run_async(user_id="u", session_id="s", new_message=msg):
        for part in (event.content.parts if event.content else []):
            if getattr(part, "text", None):
                output += part.text
    return ac.InvokeResponse(output=output.strip(), tool_trace=trace, sdk=SDK)


app = create_app(SDK, run)
