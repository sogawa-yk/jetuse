"""LangGraph 版の汎用ReActランナー(ADR-0009 / FW-02準拠)。"""

import asyncio
import json
import warnings

import httpx

import agent_common as ac
from server import create_app

SDK = "langgraph"
DEFAULT_INSTRUCTIONS = "あなたは有能なアシスタント。必要に応じてツールを使い、日本語で簡潔に答える。"


def _llm(model: str):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model, api_key="OCI", base_url=ac.BASE_URL, temperature=0.2,
        http_client=httpx.Client(auth=ac._signer(), headers=ac._headers(), timeout=120),
        http_async_client=httpx.AsyncClient(
            auth=ac._signer(), headers=ac._headers(), timeout=120),
    )


def _tools(enabled, ctx, trace):
    from langchain_core.tools import StructuredTool

    out = []
    for name in enabled:
        spec = ac.TOOLS.get(name)
        if not spec:
            continue
        props = spec["parameters"].get("properties", {})

        def make(_n, _p):
            async def invoke(**kwargs):
                args = {k: v for k, v in kwargs.items() if k in _p}
                result = await asyncio.to_thread(ac.run_tool, _n, args, ctx)
                trace.append(
                    ac.ToolCallTrace(name=_n, arguments=args, output_preview=result[:200]))
                return result

            return invoke

        out.append(StructuredTool(
            name=name, description=spec["description"],
            args_schema=spec["parameters"], coroutine=make(name, props)))
    return out


async def run(req: ac.InvokeRequest) -> ac.InvokeResponse:
    ctx = {"rag_store_id": req.rag_store_id}
    trace: list[ac.ToolCallTrace] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(
        _llm(req.model), _tools(req.enabled_tools, ctx, trace),
        prompt=req.system_prompt.strip() or DEFAULT_INSTRUCTIONS)
    msgs = [(m["role"], m["content"]) for m in req.history]
    msgs.append(("user", req.input))
    try:
        result = await agent.ainvoke(
            {"messages": msgs}, config={"recursion_limit": req.max_turns * 2})
        last = result["messages"][-1]
        output = last.content if isinstance(last.content, str) else json.dumps(last.content)
    except Exception as e:  # noqa: BLE001
        output = f"（実行が中断されました: {type(e).__name__}）"
    return ac.InvokeResponse(output=output, tool_trace=trace, sdk=SDK)


app = create_app(SDK, run)
