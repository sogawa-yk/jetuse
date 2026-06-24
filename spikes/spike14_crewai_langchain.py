"""SPIKE-14 (FW-03): CrewAI / LangChain(LCEL) のOCI互換性実機検証。

- CrewAI 1.14: litellm非依存(openai SDK直)。BaseInterceptor(on_outbound)でIAM署名を注入できるか
- LangChain: LCELチェーン(prompt | llm | parser)
- OL9のsqlite3が古いためpysqlite3-binaryで差し替え(chromadb要件)
実行: .venv/bin/python spikes/spike14_crewai_langchain.py
"""

__import__("pysqlite3")
import sys  # noqa: E402

sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")

from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import oci  # noqa: E402
import requests as req_lib  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages/api"))
from jetuse_core.settings import get_settings  # noqa: E402

MODEL_ID = "openai.gpt-oss-120b"
results = []


def record(name, ok, note):
    results.append((name, ok))
    print(f"[{'OK' if ok else 'NG'}] {name}: {str(note)[:140]}")


# --- CrewAI: interceptorでIAM署名+OCIヘッダを注入 ---
from crewai import LLM, Agent, Crew, Task  # noqa: E402
from crewai.llms.hooks.base import BaseInterceptor  # noqa: E402


class OciSigningInterceptor(BaseInterceptor[httpx.Request, httpx.Response]):
    """httpxリクエストをOCI IAM署名する(oci-genai-authと同じ方式)"""

    def __init__(self):
        self.signer = oci.signer.Signer.from_config(oci.config.from_file())
        s = get_settings()
        self.headers = {
            "CompartmentId": s.compartment_ocid,
            "OpenAi-Project": s.project_ocid,
        }

    def on_outbound(self, message: httpx.Request) -> httpx.Request:
        message.headers.update(self.headers)
        message.headers.pop("Authorization", None)
        content = message.read()
        r = req_lib.Request(
            method=message.method, url=str(message.url),
            headers=dict(message.headers), data=content,
        ).prepare()
        self.signer.do_request_sign(r)
        message.headers.update(r.headers)
        return message

    def on_inbound(self, message: httpx.Response) -> httpx.Response:
        return message


def test_crewai():
    s = get_settings()
    # モデル名がOpenAI既知リストに無いとlitellmへフォールバックするため
    # provider="openai" 明示でnative(openai SDK直)経路を強制する(実機確認)
    llm = LLM(
        model=MODEL_ID,
        provider="openai",
        api_base=s.inference_base_url,
        api_key="OCI",
        interceptor=OciSigningInterceptor(),
    )
    # ① 直接呼び出し
    try:
        r = llm.call("1+1は？数字のみで")
        record("①CrewAI LLM.call", "2" in str(r), r)
    except Exception as e:  # noqa: BLE001
        record("①CrewAI LLM.call", False, f"{type(e).__name__}: {e}")
        return
    # ② 2エージェントのCrew(researcher→writer のシーケンシャル)
    try:
        researcher = Agent(
            role="調査員", goal="お題の要点を3つ挙げる", backstory="簡潔な調査員",
            llm=llm, verbose=False,
        )
        writer = Agent(
            role="編集者", goal="要点を1文に要約する", backstory="簡潔な編集者",
            llm=llm, verbose=False,
        )
        t1 = Task(description="OCIの特徴の要点を3つ挙げる", expected_output="箇条書き3点", agent=researcher)
        t2 = Task(description="要点を日本語1文に要約", expected_output="1文", agent=writer)
        crew = Crew(agents=[researcher, writer], tasks=[t1, t2], verbose=False)
        out = crew.kickoff()
        record("②CrewAI Crew(2エージェント)", bool(str(out).strip()), str(out))
    except Exception as e:  # noqa: BLE001
        record("②CrewAI Crew(2エージェント)", False, f"{type(e).__name__}: {e}")


# --- LangChain: LCELチェーン ---
def test_langchain():
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from oci_genai_auth import OciUserPrincipalAuth

    s = get_settings()
    headers = {"CompartmentId": s.compartment_ocid, "OpenAi-Project": s.project_ocid}
    llm = ChatOpenAI(
        model=MODEL_ID, api_key="OCI", base_url=s.inference_base_url,
        http_client=httpx.Client(auth=OciUserPrincipalAuth(), headers=headers, timeout=120),
    )
    try:
        chain = (
            ChatPromptTemplate.from_template("{topic}を一言で説明して。日本語で。")
            | llm
            | StrOutputParser()
        )
        r = chain.invoke({"topic": "Oracle Cloud Infrastructure"})
        record("③LangChain LCELチェーン", bool(r), r)
    except Exception as e:  # noqa: BLE001
        record("③LangChain LCELチェーン", False, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    test_crewai()
    test_langchain()
    print("\n=== サマリ ===")
    for n, ok in results:
        print(("OK" if ok else "NG") + ":", n)
