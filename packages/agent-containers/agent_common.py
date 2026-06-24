"""3つのSDK別ホスト型ReActエージェント共通基盤(ADR-0009)。

- OCI Enterprise AI(OpenAI互換+IAM署名)クライアント生成(resource principal対応)
- 内蔵ツール(コンテナ内実行): web_search / web_fetch / get_current_time / rag_search
- invoke契約: アプリが {system_prompt, enabled_tools[], input, history?, rag_store_id?}
  をステートとして送り、汎用ReActエージェントが実行する(tools/promptは焼き込まない)。

各SDKランナー(run_openai/run_adk/run_langgraph)はこのモジュールのツール仕様・実装を共用する。
"""

import json
import os

import httpx
from openai import OpenAI
from pydantic import BaseModel

# web_fetch(SSRF対策込み)/ web_search(DuckDuckGo)/ get_current_time は jetuse_shared に一本化(P1b)。
# コンテナ側はここで jetuse_shared を呼ぶ薄い adapter にする(旧 inline コピーは廃止)。
from jetuse_shared import webtools as _wt

REGION = os.environ.get("OCI_REGION", "ap-osaka-1")
COMPARTMENT = os.environ.get("COMPARTMENT_OCID", "")
PROJECT_OCID = os.environ.get("PROJECT_OCID", "")
BASE_URL = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1"


def _signer():
    if os.environ.get("AUTH_MODE") == "resource_principal":
        from oci_genai_auth import OciResourcePrincipalAuth

        return OciResourcePrincipalAuth()
    from oci_genai_auth import OciUserPrincipalAuth

    return OciUserPrincipalAuth()


def _headers() -> dict:
    # OpenAi-Project(Enterprise AIプロジェクトOCID)が無いと誤誘導エラー(ADR-0007)のため常時付与
    h = {"CompartmentId": COMPARTMENT}
    if PROJECT_OCID:
        h["OpenAi-Project"] = PROJECT_OCID
    return h


def chat_client(timeout: float = 120.0) -> OpenAI:
    """chat completions(ReActのLLM)用(同期)。"""
    return OpenAI(
        api_key="OCI",
        base_url=BASE_URL,
        http_client=httpx.Client(auth=_signer(), headers=_headers(), timeout=timeout),
    )


def async_chat_client(timeout: float = 120.0):
    """chat completions(非同期)。Agents SDK/ADK用。"""
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key="OCI",
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(auth=_signer(), headers=_headers(), timeout=timeout),
    )


def _rag_client(timeout: float = 60.0) -> OpenAI:
    """Vector Store検索(DPホスト)。OpenAi-Projectヘッダ必須(FW-01b)。"""
    headers = {"CompartmentId": COMPARTMENT}
    if PROJECT_OCID:
        headers["OpenAi-Project"] = PROJECT_OCID
    return OpenAI(
        api_key="OCI",
        base_url=BASE_URL,
        http_client=httpx.Client(auth=_signer(), headers=headers, timeout=timeout),
    )


# ---- ツール実装(handler(args, ctx)->str)。ctxはrag_store_id等のステート ----
# web_fetch / web_search / get_current_time の実装は jetuse_shared に一本化(P1b)。
# コンテナ固有の User-Agent はデフォルトのままで挙動同等(ツール出力 text は 8000字上限=従来通り)。
def _h_web_search(args: dict, ctx: dict) -> str:
    return _wt.web_search_json(args["query"])


def _h_web_fetch(args: dict, ctx: dict) -> str:
    return _wt.web_fetch_json(args["url"])


def _h_get_current_time(args: dict, ctx: dict) -> str:
    return _wt.get_current_time_json()


def _h_query_database(args: dict, ctx: dict) -> str:
    import agent_db

    return json.dumps(agent_db.query_database(args["question"]), ensure_ascii=False)


def _h_rag_search(args: dict, ctx: dict) -> str:
    store_id = ctx.get("rag_store_id")
    if not store_id:
        return json.dumps({"results": [], "note": "文書が登録されていません"}, ensure_ascii=False)
    r = _rag_client().vector_stores.search(
        vector_store_id=store_id, query=args["query"], max_num_results=5)
    hits = []
    for item in getattr(r, "data", []) or []:
        chunks = []
        for c in getattr(item, "content", []) or []:
            t = getattr(c, "text", None)
            if t:
                chunks.append(t)
        hits.append({"filename": getattr(item, "filename", ""),
                     "score": getattr(item, "score", None),
                     "text": " ".join(chunks)[:1200]})
    return json.dumps({"results": hits}, ensure_ascii=False)


# 名前 -> (description, JSON Schema, handler)
TOOLS: dict[str, dict] = {
    "web_search": {
        "description": "Webを検索して上位結果(タイトル/URL/抜粋)を返す。最新情報や事実確認に使う",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "検索クエリ"}},
                       "required": ["query"]},
        "handler": _h_web_search,
    },
    "web_fetch": {
        "description": "指定URLのページ本文を取得する。web_searchで見つけたURLの内容を読むのに使う",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string", "description": "取得するURL"}},
                       "required": ["url"]},
        "handler": _h_web_fetch,
    },
    "get_current_time": {
        "description": "現在の日本時間(日付/時刻/曜日)を返す。「今日」「今週」等の質問の前に使う",
        "parameters": {"type": "object", "properties": {}},
        "handler": _h_get_current_time,
    },
    "rag_search": {
        "description": "アップロード済み文書から関連箇所を検索して回答の根拠にする",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "検索したい内容"}},
                       "required": ["query"]},
        "handler": _h_rag_search,
    },
    "query_database": {
        "description": "データベース(販売データ)に自然言語で質問しSQLを自動生成・実行して"
                       "結果を返す。売上・顧客・商品などの数値質問に使う。実行に30秒程度かかる",
        "parameters": {"type": "object",
                       "properties": {"question": {
                           "type": "string", "description": "データベースへの質問(日本語可)"}},
                       "required": ["question"]},
        "handler": _h_query_database,
    },
}

SUPPORTED_TOOLS = list(TOOLS.keys())


def run_tool(name: str, args: dict, ctx: dict) -> str:
    t = TOOLS.get(name)
    if not t:
        return json.dumps({"error": f"未対応のツール: {name}"}, ensure_ascii=False)
    try:
        return t["handler"](args, ctx)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"ツール実行失敗: {str(e)[:200]}"}, ensure_ascii=False)


# ---- invoke契約 ----
class InvokeRequest(BaseModel):
    system_prompt: str = ""
    enabled_tools: list[str] = []
    input: str
    history: list[dict] = []  # [{"role":"user"/"assistant","content":str}]
    rag_store_id: str | None = None
    model: str = "openai.gpt-oss-120b"
    max_turns: int = 12


class ToolCallTrace(BaseModel):
    name: str
    arguments: dict
    output_preview: str = ""


class InvokeResponse(BaseModel):
    output: str
    tool_trace: list[ToolCallTrace] = []
    sdk: str = ""
