"""SPIKE-ADK: Google ADK を OCI Enterprise AI(OpenAI互換+IAM署名)で動かす実証。

論点: ADKのLiteLlmは静的kwargsしか渡せずOCIの「リクエスト毎IAM署名」に非対応。
→ 署名済みOpenAIクライアントを使う **カスタム BaseLlm** で接続する方式を検証する。
ReActのツール呼び出し(get_current_time)→最終回答までADK Runnerが回ることを確認する。

実行: /tmp/adk-spike/bin/python spikes/spike_adk_oci.py
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from oci_genai_auth import OciUserPrincipalAuth
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages/api"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from google.adk.agents import Agent  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

REGION = os.environ.get("OCI_REGION", "ap-osaka-1")
BASE_URL = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1"
COMPARTMENT = os.environ["COMPARTMENT_OCID"]
MODEL = "openai.gpt-oss-120b"

_client = OpenAI(
    api_key="OCI",
    base_url=BASE_URL,
    http_client=httpx.Client(
        auth=OciUserPrincipalAuth(),
        headers={"CompartmentId": COMPARTMENT},
        timeout=120,
    ),
)


def _to_openai_messages(llm_request) -> list[dict]:
    msgs: list[dict] = []
    cfg = llm_request.config
    if cfg and cfg.system_instruction:
        si = cfg.system_instruction
        text = si if isinstance(si, str) else " ".join(
            p.text for p in si.parts if getattr(p, "text", None)
        )
        msgs.append({"role": "system", "content": text})
    for content in llm_request.contents or []:
        for part in content.parts or []:
            if getattr(part, "function_call", None):
                fc = part.function_call
                msgs.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": fc.id or fc.name,
                        "type": "function",
                        "function": {"name": fc.name,
                                     "arguments": json.dumps(fc.args or {})},
                    }],
                })
            elif getattr(part, "function_response", None):
                fr = part.function_response
                msgs.append({
                    "role": "tool",
                    "tool_call_id": fr.id or fr.name,
                    "content": json.dumps(fr.response or {}),
                })
            elif getattr(part, "text", None):
                role = "assistant" if content.role == "model" else "user"
                msgs.append({"role": role, "content": part.text})
    return msgs


def _to_openai_tools(llm_request) -> list[dict] | None:
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
                "name": fd.name, "description": fd.description or "",
                "parameters": schema}})
    return out or None


class OciLlm(BaseLlm):
    """OCI互換chat completions(IAM署名)を使うADKモデル。"""

    async def generate_content_async(self, llm_request, stream: bool = False):
        messages = _to_openai_messages(llm_request)
        tools = _to_openai_tools(llm_request)
        kwargs = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        resp = await asyncio.to_thread(lambda: _client.chat.completions.create(**kwargs))
        msg = resp.choices[0].message
        parts = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
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


# --- ツール(ADKはPython関数のdocstring/型からスキーマ生成) ---
def get_current_time() -> dict:
    """現在のサーバ時刻(ISO8601)を返す。"""
    return {"now": "2026-06-15T14:00:00+09:00"}


async def main():
    agent = Agent(
        name="oci_react_spike",
        model=OciLlm(model=MODEL),
        instruction="あなたは有能なアシスタント。必要ならツールを使い、日本語で簡潔に答える。",
        tools=[get_current_time],
    )
    runner = InMemoryRunner(agent=agent, app_name="spike")
    uid, sid = "u1", "s1"
    await runner.session_service.create_session(
        app_name="spike", user_id=uid, session_id=sid)
    msg = types.Content(role="user", parts=[types.Part(text="今何時？ツールで確認して。")])
    saw_tool = False
    final = ""
    async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
        for part in (event.content.parts if event.content else []):
            if getattr(part, "function_call", None):
                saw_tool = True
                print(f"  [tool_call] {part.function_call.name}({part.function_call.args})")
            if getattr(part, "function_response", None):
                print(f"  [tool_result] {part.function_response.response}")
            if getattr(part, "text", None):
                final += part.text
    print("\n=== 結果 ===")
    print("tool使用:", saw_tool)
    print("最終回答:", final.strip()[:300])
    print("判定:", "PASS" if (saw_tool and final.strip()) else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
