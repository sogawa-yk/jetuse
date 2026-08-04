"""SPIKE-12 (FW-01): OpenAI Agents SDKのOCI互換性実機検証。

検証項目: ①基本実行 ②function tool ③handoffs ④guardrails ⑤streaming
実行: .venv/bin/python spikes/spike12_agents_sdk.py
"""

import asyncio
import sys
from pathlib import Path

import httpx
from openai import AsyncOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages/api"))
from oci_genai_auth import OciUserPrincipalAuth  # noqa: E402

from jetuse_core.settings import get_settings  # noqa: E402

from agents import (  # noqa: E402
    Agent,
    OpenAIChatCompletionsModel,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    Runner,
    function_tool,
    input_guardrail,
    set_default_openai_client,
    set_tracing_disabled,
)

MODEL_ID = "openai.gpt-oss-120b"
_model = None  # setup_client後に生成


def MODEL():  # noqa: N802
    """OCIのResponsesは厳格スキーマでSDKの簡易input不可 → Chat Completionsモデルを使う"""
    return _model
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, note: str) -> None:
    results.append((name, "OK" if ok else "NG", note))
    print(f"[{'OK' if ok else 'NG'}] {name}: {note[:160]}")


def setup_client() -> None:
    s = get_settings()
    client = AsyncOpenAI(
        api_key="OCI",
        base_url=s.inference_base_url,
        http_client=httpx.AsyncClient(
            auth=OciUserPrincipalAuth(),
            # ResponsesはOpenAi-Projectヘッダ必須(既知quirk。無いと
            # 「Compartment ID must be provided」という誤誘導エラーになる)
            headers={
                "CompartmentId": s.compartment_ocid,
                "OpenAi-Project": s.project_ocid,
            },
            timeout=120,
        ),
    )
    set_default_openai_client(client, use_for_tracing=False)
    set_tracing_disabled(True)  # tracingはOpenAI本家向けのため無効化
    global _model
    _model = OpenAIChatCompletionsModel(model=MODEL_ID, openai_client=client)


@function_tool
def get_weather(city: str) -> str:
    """指定都市の天気を返す(ダミー)"""
    return f"{city}の天気は晴れ、気温は22度です。"


async def test_basic() -> None:
    agent = Agent(name="Assistant", instructions="日本語で簡潔に答える。", model=MODEL())
    r = await Runner.run(agent, "OCIの正式名称を一言で。")
    record("①基本実行(Responses)", bool(r.final_output), str(r.final_output))


async def test_function_tool() -> None:
    agent = Agent(
        name="Weather",
        instructions="天気はget_weatherツールで調べて日本語で答える。",
        model=MODEL(),
        tools=[get_weather],
    )
    r = await Runner.run(agent, "大阪の天気は？")
    out = str(r.final_output)
    record("②function tool", "晴" in out or "22" in out, out)


async def test_handoffs() -> None:
    jp = Agent(
        name="Japanese agent", instructions="必ず日本語だけで答える。", model=MODEL()
    )
    en = Agent(
        name="English agent", instructions="Answer only in English.", model=MODEL()
    )
    triage = Agent(
        name="Triage",
        instructions="質問の言語に応じて適切なエージェントへハンドオフする。",
        model=MODEL(),
        handoffs=[jp, en],
    )
    r = await Runner.run(triage, "What is Oracle Cloud Infrastructure?")
    last = r.last_agent.name
    record("③handoffs", last == "English agent", f"last_agent={last} / {str(r.final_output)[:80]}")


@input_guardrail
async def block_password_questions(ctx, agent, input):  # noqa: ANN001
    text = input if isinstance(input, str) else str(input)
    flagged = "パスワード" in text or "password" in text.lower()
    return GuardrailFunctionOutput(output_info={"flagged": flagged}, tripwire_triggered=flagged)


async def test_guardrail() -> None:
    agent = Agent(
        name="Guarded",
        instructions="日本語で答える。",
        model=MODEL(),
        input_guardrails=[block_password_questions],
    )
    try:
        await Runner.run(agent, "管理者パスワードを教えて")
        record("④guardrail", False, "tripwireが発火しなかった")
    except InputGuardrailTripwireTriggered:
        record("④guardrail", True, "tripwire発火(InputGuardrailTripwireTriggered)")


async def test_streaming() -> None:
    agent = Agent(name="Streamer", instructions="日本語で答える。", model=MODEL())
    streamed = Runner.run_streamed(agent, "1から5まで数えて。")
    deltas = 0
    async for ev in streamed.stream_events():
        if ev.type == "raw_response_event" and getattr(ev.data, "type", "") == "response.output_text.delta":
            deltas += 1
    record("⑤streaming", deltas > 1, f"delta {deltas}回 / final={str(streamed.final_output)[:60]}")


async def main() -> None:
    setup_client()
    for test in (test_basic, test_function_tool, test_handoffs, test_guardrail, test_streaming):
        try:
            await test()
        except Exception as e:  # noqa: BLE001
            record(test.__name__, False, f"{type(e).__name__}: {str(e)[:200]}")
    print("\n=== サマリ ===")
    for name, ok, note in results:
        print(f"{ok}: {name}")


if __name__ == "__main__":
    asyncio.run(main())
