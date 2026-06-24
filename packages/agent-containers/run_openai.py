"""OpenAI Agents SDK 版の汎用ReActランナー(ADR-0009)。

OCI互換chat completions(OpenAIChatCompletionsModel, ADR-0008)で、ステートとして渡された
system_prompt + enabled_tools を用いてReActを実行する。
"""

import asyncio
import json

from agents import Agent, FunctionTool, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from agents.exceptions import AgentsException

import agent_common as ac
from server import create_app

set_tracing_disabled(True)
SDK = "openai_agents"
DEFAULT_INSTRUCTIONS = "あなたは有能なアシスタント。必要に応じてツールを使い、日本語で簡潔に答える。"


def _tools(enabled, ctx, trace):
    out = []
    for name in enabled:
        spec = ac.TOOLS.get(name)
        if not spec:
            continue
        props = spec["parameters"].get("properties", {})

        async def invoke(_c, args_json, _n=name, _p=props):
            try:
                args = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError:
                args = {}
            args = {k: v for k, v in args.items() if k in _p} if isinstance(args, dict) else {}
            result = await asyncio.to_thread(ac.run_tool, _n, args, ctx)
            trace.append(ac.ToolCallTrace(name=_n, arguments=args, output_preview=result[:200]))
            return result

        out.append(FunctionTool(
            name=name, description=spec["description"],
            params_json_schema=spec["parameters"], on_invoke_tool=invoke,
            strict_json_schema=False))
    return out


async def run(req: ac.InvokeRequest) -> ac.InvokeResponse:
    ctx = {"rag_store_id": req.rag_store_id}
    trace: list[ac.ToolCallTrace] = []
    agent = Agent(
        name="jetuse-agent",
        instructions=req.system_prompt.strip() or DEFAULT_INSTRUCTIONS,
        tools=_tools(req.enabled_tools, ctx, trace),
        model=OpenAIChatCompletionsModel(model=req.model, openai_client=ac.async_chat_client()),
    )
    items = [{"role": m["role"], "content": m["content"]} for m in req.history]
    items.append({"role": "user", "content": req.input})
    try:
        result = await Runner.run(agent, items, max_turns=req.max_turns)
        output = result.final_output or ""
    except AgentsException as e:
        output = f"（実行が中断されました: {type(e).__name__}）"
    return ac.InvokeResponse(output=output, tool_trace=trace, sdk=SDK)


app = create_app(SDK, run)
