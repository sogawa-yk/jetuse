"""LangGraph製サンプルエージェント(AGT-04)。OCI Hosted Application題材。

リサーチャー→要約者の2ノードグラフ。LLMはOCI Enterprise AIのOpenAI互換
エンドポイント(gpt-oss-120b)をIAM署名で呼ぶ。

HTTP契約(本サンプル): POST /invoke {"input": str} -> {"output": str}
"""

import os
from typing import TypedDict

import httpx
from fastapi import FastAPI
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from oci_genai_auth import OciResourcePrincipalAuth, OciUserPrincipalAuth
from pydantic import BaseModel

REGION = os.environ.get("OCI_REGION", "ap-osaka-1")
COMPARTMENT = os.environ["COMPARTMENT_OCID"]
MODEL = "openai.gpt-oss-120b"


def _signer():
    if os.environ.get("AUTH_MODE") == "resource_principal":
        return OciResourcePrincipalAuth()
    return OciUserPrincipalAuth()


def make_llm(temperature: float = 0.3) -> ChatOpenAI:
    return ChatOpenAI(
        model=MODEL,
        api_key="OCI",
        base_url=f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1",
        http_client=httpx.Client(
            auth=_signer(), headers={"CompartmentId": COMPARTMENT}, timeout=120,
        ),
        temperature=temperature,
    )


class AgentState(TypedDict):
    question: str
    notes: str
    answer: str


def research(state: AgentState) -> dict:
    llm = make_llm()
    res = llm.invoke(
        f"次の質問について、知っている事実を箇条書きで5点まで挙げてください。\n質問: {state['question']}"
    )
    return {"notes": res.content}


def summarize(state: AgentState) -> dict:
    llm = make_llm(temperature=0.1)
    res = llm.invoke(
        "以下のメモに基づいて、質問に2〜3文で簡潔に答えてください。\n"
        f"質問: {state['question']}\nメモ:\n{state['notes']}"
    )
    return {"answer": res.content}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("research", research)
    g.add_node("summarize", summarize)
    g.set_entry_point("research")
    g.add_edge("research", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


GRAPH = build_graph()
app = FastAPI(title="jetuse-hosted-agent-sample")


class InvokeRequest(BaseModel):
    input: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/invoke")
def invoke(req: InvokeRequest):
    out = GRAPH.invoke({"question": req.input, "notes": "", "answer": ""})
    return {"output": out["answer"], "notes": out["notes"]}
