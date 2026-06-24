"""チャットストリーミング統合層(CHAT-01)。

2系統API(Responses=gpt-oss/llama、Chat Completions=Gemini — SPIKE-01実証)を
単一のイベント列 {"delta"} / {"usage"} / {"error"} に正規化する。
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from openai import APIConnectionError, OpenAI

from .genai import make_inference_client
from .logging import log_with
from .models import MODELS, ModelDef

logger = logging.getLogger("jetuse.chat")

ChatEvent = dict[str, Any]

ReasoningEffort = Literal["low", "medium", "high"]

# RAG(RAG-02): ツール使用を強制しないとモデルが一般論で回答する(SPIKE-03b: 7/10→10/10)
RAG_INSTRUCTIONS = (
    "質問には必ずfile_searchツールでアップロード済み文書を検索し、"
    "その検索結果のみに基づいて回答してください。一般論で答えてはいけません。"
    "検索結果に該当がない場合は「アップロードされた文書には該当する情報がありません」と答えてください。"
    "回答には根拠となる文書名を含めてください。"
)


@dataclass(frozen=True)
class GenParams:
    """生成パラメータ(CHAT-04b)。Noneは「APIに渡さない=モデル既定」"""

    top_p: float | None = None
    max_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None  # 推論モデルのみ有効
    file_search_store: str | None = None  # RAG(RAG-02)。Responses系のみ


def _to_responses_input(messages: list[dict]) -> list[dict]:
    """OCIのResponses実装は {role, content:str} を拒否する(実機確定)。
    受理されるのは type=message + 型付きcontentパーツの形式のみ。
    アシスタント履歴も input_text にする(output_textはgpt-ossが400で拒否 — 2026-06-10実機)。"""
    return [
        {
            "type": "message",
            "role": m["role"],
            "content": [{"type": "input_text", "text": m["content"]}],
        }
        for m in messages
    ]


def create_oci_conversation(metadata: dict[str, str], project_ocid: str | None = None) -> str:
    """OCI Conversations(短期メモリ — CHAT-06)を作成してIDを返す。

    short_term_memory_optimization は履歴圧縮フラグ。プロジェクトのSTM condenser
    設定有効時は既定true(jetuse-dev-projectで確認)だが、明示trueで固定する(CHAT-06b:
    実測42%削減・圧縮後も記憶保持OK)。
    """
    client = make_inference_client(with_project=True, project_ocid=project_ocid)
    return client.conversations.create(
        metadata={"short_term_memory_optimization": "true", **metadata}
    ).id


def delete_oci_conversation(oci_conversation_id: str) -> None:
    """OCI Conversations側の削除(CHAT-09)。会話削除時の同期に使う。"""
    client = make_inference_client(with_project=True)
    client.conversations.delete(oci_conversation_id)


def _extra_responses_params(model: ModelDef, params: "GenParams") -> dict:
    """Responses系の追加パラメータ(CHAT-04b)。未指定はAPIに渡さない"""
    out: dict = {}
    if params.top_p is not None:
        out["top_p"] = params.top_p
    if params.max_tokens is not None:
        out["max_output_tokens"] = params.max_tokens
    if params.reasoning_effort and model.reasoning:
        out["reasoning"] = {"effort": params.reasoning_effort}
    if params.file_search_store:
        out["tools"] = [
            {"type": "file_search", "vector_store_ids": [params.file_search_store]}
        ]
        out["include"] = ["file_search_call.results"]
        out["instructions"] = RAG_INSTRUCTIONS
    return out


def _extract_citations(response: Any) -> list[dict]:
    """file_search_call.results + message annotations から引用元を抽出(RAG-02)"""
    by_file: dict[str, dict] = {}
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", "") == "file_search_call":
            for r in getattr(item, "results", None) or []:
                fid = getattr(r, "file_id", "")
                score = getattr(r, "score", None)
                cur = by_file.get(fid)
                if not cur or (score or 0) > (cur.get("score") or 0):
                    by_file[fid] = {
                        "file_id": fid,
                        "filename": getattr(r, "filename", ""),
                        "score": round(score, 3) if score is not None else None,
                    }
        elif getattr(item, "type", "") == "message":
            for part in getattr(item, "content", None) or []:
                for a in getattr(part, "annotations", None) or []:
                    fid = getattr(a, "file_id", "")
                    if fid and fid not in by_file:
                        by_file[fid] = {
                            "file_id": fid,
                            "filename": getattr(a, "filename", ""),
                            "score": None,
                        }
    return sorted(by_file.values(), key=lambda c: -(c["score"] or 0))


def _stream_responses(
    client: OpenAI,
    model: ModelDef,
    messages: list[dict],
    temperature: float,
    oci_conversation_id: str | None = None,
    params: "GenParams | None" = None,
) -> Iterator[ChatEvent]:
    extra = _extra_responses_params(model, params or GenParams())
    if oci_conversation_id:
        # 短期メモリ(CHAT-06): 履歴はサーバー側のConversationが保持するため
        # 最新のユーザー発話(+システム)だけを送る。storeはConversation側に任せる
        sendable = [m for m in messages if m["role"] == "system"] + messages[-1:]
        stream = client.responses.create(
            model=model.oci_id,
            conversation=oci_conversation_id,
            input=_to_responses_input(sendable),
            temperature=temperature,
            stream=True,
            **extra,
        )
    else:
        stream = client.responses.create(
            model=model.oci_id,
            input=_to_responses_input(messages),
            temperature=temperature,
            stream=True,
            # 既定はサーバー側に保存される(store=true相当 — 実機確定)。
            # 履歴の正はADB(ADR-0002)であり、意図しない蓄積を避けるため明示的に無効化
            store=False,
            **extra,
        )
    try:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield {"delta": event.delta}
            elif etype == "response.completed":
                citations = _extract_citations(event.response)
                if citations:
                    yield {"citations": citations}
                usage = getattr(event.response, "usage", None)
                if usage:
                    yield {
                        "usage": {
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                        }
                    }
    finally:
        # ジェネレータclose(クライアント切断 — CHAT-08)で上流HTTPストリームを打ち切る
        stream.close()


def _stream_chat_completions(
    client: OpenAI,
    model: ModelDef,
    messages: list[dict],
    temperature: float,
    params: "GenParams | None" = None,
) -> Iterator[ChatEvent]:
    p = params or GenParams()
    extra: dict = {}
    if p.top_p is not None:
        extra["top_p"] = p.top_p
    if p.max_tokens is not None:
        # 思考型モデル(Gemini)の実用下限でクランプ(小さい値は空応答/ハングの実機挙動)
        extra["max_tokens"] = max(p.max_tokens, model.min_max_tokens)
    # reasoning effortはChat Completions系に存在しないため無視(CHAT-04b)
    stream = client.chat.completions.create(
        model=model.oci_id,
        messages=messages,
        temperature=temperature,
        stream=True,
        # 末尾チャンクでusageを受け取る(SEC-02監査。OCI互換で動作確認済み 2026-06-13)
        stream_options={"include_usage": True},
        **extra,
    )
    usage = None
    try:
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield {"delta": delta.content}
    finally:
        stream.close()  # CHAT-08: 切断時に上流を打ち切る
    if usage:
        yield {
            "usage": {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
            }
        }


def complete_once(model_key: str, messages: list[dict], max_chars: int = 200) -> str:
    """非ストリーミングの単発補完(タイトル生成等の内部用途 — CHAT-05)。"""
    model = MODELS[model_key]
    client = make_inference_client(with_project=model.api == "responses")
    if model.api == "responses":
        r = client.responses.create(
            model=model.oci_id, input=_to_responses_input(messages), store=False
        )
        return (r.output_text or "")[:max_chars]
    r = client.chat.completions.create(model=model.oci_id, messages=messages)
    return (r.choices[0].message.content or "")[:max_chars]


MAX_TOOL_HOPS = 5

# ツール使用回数の上限に達したときの最終回答強制プロンプト(AGT-01d)
_FORCE_ANSWER_TEXT = (
    "ツール使用回数の上限に達しました。これまでに得た情報だけで"
    "最終的な回答をまとめてください。"
)


def _force_answer_message() -> dict:
    """ツール無しで最終回答を促すsystemメッセージアイテム(AGT-01d)。"""
    return {
        "type": "message", "role": "system",
        "content": [{"type": "input_text", "text": _FORCE_ANSWER_TEXT}],
    }


def _build_agent_input(
    messages: list[dict],
    instructions: str | None,
    tool_results: list[dict] | None,
) -> list[dict]:
    """エージェントのResponses入力を構築(履歴+人格+承認/ツール結果の往復)。"""
    base_input = _to_responses_input(messages)
    if instructions:
        # エージェントの人格(AGT-03)はsystemメッセージとして先頭付与
        base_input = _to_responses_input([{"role": "system", "content": instructions}]) + base_input
    for tr in tool_results or []:
        call = tr["call"]
        base_input.append(call)
        if call.get("type") == "mcp_approval_request":
            # MCP承認(AGT-02): approve/denyを応答アイテムで返す
            base_input.append({
                "type": "mcp_approval_response",
                "approval_request_id": call.get("id"),
                "approve": tr["output"] == "approve",
            })
        else:
            base_input.append({
                "type": "function_call_output",
                "call_id": call.get("call_id"),
                "output": tr["output"],
            })
    return base_input


def _build_agent_tools(
    enabled_tools: list[str] | None,
    mcp_servers: list[dict] | None,
    auto_tools: bool,
    rag_store: str | None,
) -> list[dict]:
    """このターンで使用可能なツール仕様を構築(custom + MCP + rag_search/file_search)。"""
    from .mcp_servers import mcp_tool_spec
    from .tools import RAG_SEARCH, tool_specs

    custom_enabled = [t for t in (enabled_tools or []) if t != RAG_SEARCH]
    all_tools = tool_specs(custom_enabled if enabled_tools is not None else None) + [
        mcp_tool_spec(srv, auto_tools) for srv in (mcp_servers or [])
    ]
    if enabled_tools and RAG_SEARCH in enabled_tools and rag_store:
        # rag_searchの実体はfile_search built-in(ユーザーのVector Store) — AGT-01c
        all_tools.append({"type": "file_search", "vector_store_ids": [rag_store]})
    return all_tools


def _collect_hop_events(
    stream: Any, calls: list[Any], mcp_approvals: list[Any]
) -> Iterator[ChatEvent]:
    """1ホップのResponseストリームを消費。delta/tool_call/citations/usageを
    passthroughで yield し、function_call / mcp_approval_request を渡された
    リストへ収集する(呼び出し側で承認/実行を判断)。"""
    try:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield {"delta": event.delta}
            elif etype == "response.output_item.added":
                itype = getattr(event.item, "type", "")
                if itype == "code_interpreter_call":
                    # built-in: OCI側サンドボックスで実行される(承認対象外・通知のみ)
                    yield {"tool_call": {
                        "name": "code_interpreter", "label": "コード実行",
                        "builtin": True, "status": "running",
                    }}
                elif itype == "file_search_call":
                    yield {"tool_call": {
                        "name": "rag_search", "label": "文書検索",
                        "builtin": True, "status": "running",
                    }}
                elif itype == "mcp_call":
                    # MCPはサーバーサイド実行(通知のみ — AGT-02)
                    label = (f"MCP: {getattr(event.item, 'server_label', '')}/"
                             f"{getattr(event.item, 'name', '')}")
                    yield {"tool_call": {
                        "name": getattr(event.item, "name", "mcp"),
                        "label": label, "builtin": True, "status": "running",
                    }}
            elif etype == "response.output_item.done":
                item = event.item
                itype = getattr(item, "type", "")
                if itype == "function_call":
                    calls.append(item)
                elif itype == "mcp_approval_request":
                    mcp_approvals.append(item)
            elif etype == "response.completed":
                citations = _extract_citations(event.response)
                if citations:
                    yield {"citations": citations}
                usage = getattr(event.response, "usage", None)
                if usage:
                    yield {"usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    }}
    finally:
        stream.close()


def _emit_mcp_approvals(mcp_approvals: list[Any]) -> Iterator[ChatEvent]:
    """MCP承認要求(AGT-02): UIへ通知(承認モードのみ発生)。"""
    for ap in mcp_approvals:
        ad = ap.model_dump(exclude_none=True)
        yield {"tool_call": {
            "kind": "mcp",
            "name": ad.get("name", "mcp"),
            "label": f"MCP: {ad.get('server_label', '')}/{ad.get('name', '')}",
            "arguments": ad.get("arguments", "{}"),
            "call_id": ad.get("id"),
            "item": ad,
            "status": "pending_approval",
        }}


def _emit_pending_approval(call_dicts: list[dict], tools: dict) -> Iterator[ChatEvent]:
    """function_callバッチをUIへ承認待ちとして通知(混在バッチは全件承認制)。"""
    for cd in call_dicts:
        tool = tools.get(cd["name"])
        yield {"tool_call": {
            "name": cd["name"],
            "label": tool.label if tool else cd["name"],
            "arguments": cd.get("arguments", "{}"),
            "call_id": cd.get("call_id"),
            "item": cd,
            "status": "pending_approval",
        }}


def _run_tool_calls(
    call_dicts: list[dict],
    base_input: list[dict],
    user: str,
    tools: dict,
    execute_tool: Any,
    tool_error: type[Exception],
) -> Iterator[ChatEvent]:
    """function_callを実行し結果を base_input へ追記(自動実行ホップ)。"""
    for cd in call_dicts:
        tool = tools.get(cd["name"])
        yield {"tool_call": {
            "name": cd["name"],
            "label": tool.label if tool else cd["name"],
            "arguments": cd.get("arguments", "{}"),
            "call_id": cd.get("call_id"),
            "status": "running",
        }}
        try:
            output = execute_tool(cd["name"], cd.get("arguments", "{}"))
        except tool_error as e:
            output = json.dumps({"error": str(e)}, ensure_ascii=False)
        log_with(logger, logging.INFO, "agent_tool_executed",
                 tool=cd["name"], user=user, output_chars=len(output))
        yield {"tool_result": {
            "call_id": cd.get("call_id"), "name": cd["name"],
            "preview": output[:500],
        }}
        base_input.append(cd)
        base_input.append({
            "type": "function_call_output",
            "call_id": cd.get("call_id"),
            "output": output,
        })


def stream_agent(
    model_key: str,
    messages: list[dict],
    temperature: float | None = None,
    user: str = "",
    auto_tools: bool = False,
    tool_results: list[dict] | None = None,
    params: GenParams | None = None,
    enabled_tools: list[str] | None = None,
    mcp_servers: list[dict] | None = None,
    instructions: str | None = None,
    project_ocid: str | None = None,
    rag_store: str | None = None,
) -> Iterator[ChatEvent]:
    """エージェントモード(AGT-01)。ツール付きResponses呼び出しをループする。

    - ステートレス(全履歴再送)。Responses系モデルのみ
    - auto_tools=False: function_callを {"tool_call"} イベントで通知してストリーム終了
      (UIが承認後、tool_results付きで再呼び出しして継続する)
    - auto_tools=True: サーバー側で実行し最大MAX_TOOL_HOPSホップまで自動継続
    """
    from .tools import TOOLS, ToolError, execute_tool

    model = MODELS[model_key]
    if model.api != "responses":
        yield {"error": "エージェントモードはResponses系モデルのみ対応です"}
        return
    temp = model.default_temperature if temperature is None else temperature
    client = make_inference_client(with_project=True, project_ocid=project_ocid)
    base_input = _build_agent_input(messages, instructions, tool_results)
    extra = _extra_responses_params(model, params or GenParams())
    # ターン内ツール総数の安全弁(AGT-01d): 累積16件以上はツールを外し最終回答を強制
    force_answer = len(tool_results or []) >= 16
    all_tools = _build_agent_tools(enabled_tools, mcp_servers, auto_tools, rag_store)
    if force_answer:
        all_tools = []
        base_input.append(_force_answer_message())

    for _hop in range(MAX_TOOL_HOPS):
        stream = client.responses.create(
            model=model.oci_id,
            input=base_input,
            temperature=temp,
            tools=all_tools,
            stream=True,
            store=False,
            **extra,
        )
        calls: list[Any] = []
        mcp_approvals: list[Any] = []
        yield from _collect_hop_events(stream, calls, mcp_approvals)

        if mcp_approvals:
            # MCP承認要求(AGT-02): UIへ通知してストリーム終了(承認モードのみ発生)
            yield from _emit_mcp_approvals(mcp_approvals)
            return

        if not calls:
            log_with(logger, logging.INFO, "agent_done", model=model_key, user=user)
            return

        call_dicts = [
            {k: v for k, v in c.model_dump(exclude_none=True).items()
             if k in ("type", "name", "arguments", "call_id", "id")}
            for c in calls
        ]
        needs_approval = [
            cd for cd in call_dicts
            if not (TOOLS.get(cd["name"]) and not TOOLS[cd["name"]].requires_approval)
        ]
        if not auto_tools and needs_approval:
            # 混在バッチは全件承認制(ステートレス継続で安全側の結果が失われるのを防ぐ)
            yield from _emit_pending_approval(call_dicts, TOOLS)
            return  # UIの承認待ち
        # 全件が承認不要(requires_approval=False)の場合は承認モードでも自動実行して継続

        yield from _run_tool_calls(call_dicts, base_input, user, TOOLS, execute_tool, ToolError)

    # ホップ上限: エラーではなくツールなしで最終回答を強制する(AGT-01d)
    yield {"delta": ""}
    final_input = base_input + [_force_answer_message()]
    stream = client.responses.create(
        model=model.oci_id, input=final_input, temperature=temp,
        stream=True, store=False, **extra,
    )
    try:
        for event in stream:
            if getattr(event, "type", "") == "response.output_text.delta":
                yield {"delta": event.delta}
    finally:
        stream.close()


def stream_chat(
    model_key: str,
    messages: list[dict],
    temperature: float | None = None,
    user: str = "",
    oci_conversation_id: str | None = None,
    params: GenParams | None = None,
    project_ocid: str | None = None,
) -> Iterator[ChatEvent]:
    """正規化済みチャットイベントを返す。接続確立失敗は1回リトライ。

    oci_conversation_id はResponses系モデルのみ有効(短期メモリ — CHAT-06)。
    params は系統ごとに対応するものだけAPIへ渡す(CHAT-04b)。
    """
    model = MODELS[model_key]
    temp = model.default_temperature if temperature is None else temperature

    def fn(client: OpenAI) -> Iterator[ChatEvent]:
        if model.api == "responses":
            return _stream_responses(
                client, model, messages, temp, oci_conversation_id, params
            )
        return _stream_chat_completions(client, model, messages, temp, params)

    for attempt in (1, 2):
        yielded = False
        try:
            # Responses APIは OpenAi-Project ヘッダ必須(実機確定 — specs/00 未文書仕様2)
            client = make_inference_client(
                with_project=model.api == "responses", project_ocid=project_ocid
            )
            out_tokens = 0
            for ev in fn(client):
                yielded = True
                if "usage" in ev:
                    out_tokens = ev["usage"].get("output_tokens", 0)
                yield ev
            log_with(
                logger, logging.INFO, "chat_done",
                model=model_key, user=user, output_tokens=out_tokens,
            )
            return
        except APIConnectionError as e:
            # ストリーミング開始前の接続失敗のみリトライ対象
            if attempt == 2:
                log_with(logger, logging.ERROR, "chat_failed", model=model_key, error=str(e))
                yield {"error": f"connection failed: {e}"}
                return
            log_with(logger, logging.WARNING, "chat_retry", model=model_key)
        except json.JSONDecodeError:
            # OCIは一時エラーを非JSON(単引用符dict等)でSSEに流すことがあり、
            # SDKの解析がJSONDecodeErrorで落ちる(2026-06-11 RAGで実発生)。
            # 何も出力していなければ1回リトライ、途中なら平易なメッセージで通知
            logger.exception("upstream stream parse failed (model=%s)", model_key)
            if not yielded and attempt == 1:
                log_with(logger, logging.WARNING, "chat_retry_parse", model=model_key)
                continue
            yield {
                "error": "上流応答の解析に失敗しました（一時的なエラーの可能性）。"
                "再生成をお試しください"
            }
            return
        except Exception as e:  # ストリーミング途中の失敗はイベントで通知
            log_with(logger, logging.ERROR, "chat_failed", model=model_key, error=str(e))
            logger.exception("chat_failed traceback (model=%s)", model_key)
            yield {"error": str(e)}
            return
