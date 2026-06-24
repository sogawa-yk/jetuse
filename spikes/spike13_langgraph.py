"""SPIKE-13 (FW-02): LangGraphのOCI互換性実機検証。

検証項目: ①ChatOpenAI基本呼び出し ②create_react_agent(ツール) ③astream_eventsストリーミング
④分岐・並列ノードのカスタムグラフ
実行: .venv/bin/python spikes/spike13_langgraph.py
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages/api"))
from oci_genai_auth import OciUserPrincipalAuth  # noqa: E402

from jetuse_core.settings import get_settings  # noqa: E402

from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402

MODEL = "openai.gpt-oss-120b"
results = []


def record(name, ok, note):
    results.append((name, ok))
    print(f"[{'OK' if ok else 'NG'}] {name}: {str(note)[:140]}")


def make_llm():
    s = get_settings()
    client = httpx.Client(
        auth=OciUserPrincipalAuth(),
        headers={"CompartmentId": s.compartment_ocid, "OpenAi-Project": s.project_ocid},
        timeout=120,
    )
    aclient = httpx.AsyncClient(
        auth=OciUserPrincipalAuth(),
        headers={"CompartmentId": s.compartment_ocid, "OpenAi-Project": s.project_ocid},
        timeout=120,
    )
    return ChatOpenAI(
        model=MODEL, api_key="OCI", base_url=s.inference_base_url,
        http_client=client, http_async_client=aclient,
    )


@tool
def get_weather(city: str) -> str:
    """指定都市の天気を返す(ダミー)"""
    return f"{city}は晴れ、22度です。"


async def main():
    llm = make_llm()

    # ① 基本
    try:
        r = await llm.ainvoke("OCIの正式名称を一言で。日本語で。")
        record("①ChatOpenAI基本", bool(r.content), r.content)
    except Exception as e:  # noqa: BLE001
        record("①ChatOpenAI基本", False, e)

    # ② ReActエージェント(ツール)
    try:
        agent = create_react_agent(llm, [get_weather])
        r = await agent.ainvoke({"messages": [("user", "大阪の天気は？日本語で")]})
        out = r["messages"][-1].content
        record("②create_react_agent", "晴" in out or "22" in out, out)
    except Exception as e:  # noqa: BLE001
        record("②create_react_agent", False, e)

    # ③ ストリーミング(astream_events)
    try:
        agent = create_react_agent(llm, [get_weather])
        deltas, tool_starts = 0, 0
        async for ev in agent.astream_events(
            {"messages": [("user", "東京の天気は？")]}, version="v2"
        ):
            if ev["event"] == "on_chat_model_stream":
                if ev["data"]["chunk"].content:
                    deltas += 1
            elif ev["event"] == "on_tool_start":
                tool_starts += 1
        record("③astream_events", deltas > 1 and tool_starts >= 1,
               f"delta {deltas}回 / tool_start {tool_starts}回")
    except Exception as e:  # noqa: BLE001
        record("③astream_events", False, e)

    # ④ 分岐+並列のカスタムグラフ: 質問を2観点(技術/ビジネス)へ並列展開→統合
    try:
        class S(TypedDict):
            q: str
            tech: str
            biz: str
            answer: str

        async def tech_node(s: S):
            r = await llm.ainvoke(f"技術観点で一文: {s['q']}")
            return {"tech": r.content}

        async def biz_node(s: S):
            r = await llm.ainvoke(f"ビジネス観点で一文: {s['q']}")
            return {"biz": r.content}

        async def merge_node(s: S):
            r = await llm.ainvoke(
                f"次の2観点を統合して2文で答える。技術: {s['tech']} / ビジネス: {s['biz']}"
            )
            return {"answer": r.content}

        g = StateGraph(S)
        g.add_node("tech", tech_node)
        g.add_node("biz", biz_node)
        g.add_node("merge", merge_node)
        g.add_edge(START, "tech")  # STARTから2本 = 並列実行
        g.add_edge(START, "biz")
        g.add_edge("tech", "merge")
        g.add_edge("biz", "merge")
        g.add_edge("merge", END)
        graph = g.compile()
        import time
        t0 = time.time()
        r = await graph.ainvoke({"q": "生成AIの社内導入"})
        record("④並列グラフ", bool(r.get("answer")),
               f"{time.time()-t0:.1f}s / {r.get('answer','')[:80]}")
    except Exception as e:  # noqa: BLE001
        record("④並列グラフ", False, e)

    print("\n=== サマリ ===")
    for n, ok in results:
        print(("OK" if ok else "NG") + ":", n)


if __name__ == "__main__":
    asyncio.run(main())
