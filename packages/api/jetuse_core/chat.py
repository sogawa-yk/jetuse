"""チャットストリーミング統合層(CHAT-01)。

2系統API(Responses=gpt-oss/llama、Chat Completions=Gemini — SPIKE-01実証)を
単一のイベント列 {"delta"} / {"usage"} / {"error"} に正規化する。
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from openai import APIConnectionError, APIStatusError, OpenAI

from .genai import make_inference_client
from .logging import log_with
from .models import MODELS, ModelDef, mark_unavailable
from .settings import (
    AGENT_MAX_TOOL_HOPS_CEILING,
    AGENT_MAX_TOOL_HOPS_DEFAULT,
    get_settings,
)

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
    # RAGM-01: file_searchのメタデータ絞り込み(例: current_version='Y'で旧版を外す)。
    # 検証済み(rag_metadata.validate_filters)の構造だけを渡す — 未知キーは静かに0件になる
    file_search_filters: dict | None = None


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
            file_search_tool(params.file_search_store, params.file_search_filters)
        ]
        out["include"] = ["file_search_call.results"]
        out["instructions"] = RAG_INSTRUCTIONS
    return out


def file_search_tool(store_id: str, filters: dict | None = None) -> dict:
    """file_search ツール仕様(RAGM-01: メタデータ絞り込みを任意で載せる)。

    filters は `rag_metadata.validate_filters` を通った構造のみ(未知キーは上流でエラーに
    ならず 0 件になるため、境界で弾いてからここへ来る — SPIKE-M1 ①-b)。
    """
    tool: dict = {"type": "file_search", "vector_store_ids": [store_id]}
    if filters:
        tool["filters"] = filters
    return tool


# 引用に載せる本文抜粋の上限(SSEペイロードを膨らませないための切り詰め)
CITATION_TEXT_CHARS = 500


def _as_dict(value: Any) -> dict:
    """SDKの属性値(dict / pydanticモデル)を素のdictへ。取れなければ空dict。"""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:  # noqa: BLE001 - 引用の付加情報。落とさず捨てる
            return {}
    return {}


def _structured_citation(r: Any) -> dict:
    """file_search_call の 1 ヒットを構造化引用にする(RAGM-01)。

    既存の `{file_id, filename, score}` は後方互換のため必ず含め、拡張分
    (`source` = 取り込み時の attributes / `text` = 該当箇所の本文 / `chunk_id`)を足す。
    属性はファイル単位なので(SPIKE-M1 ①-a)、同一ファイルの複数ヒットからは
    最上位スコアのものを採る。
    """
    score = getattr(r, "score", None)
    cite: dict = {
        "file_id": getattr(r, "file_id", ""),
        "filename": getattr(r, "filename", ""),
        "score": round(score, 3) if score is not None else None,
    }
    source = {k: v for k, v in _as_dict(getattr(r, "attributes", None)).items() if v != ""}
    if source:
        cite["source"] = source
    text = getattr(r, "text", None)
    if text:
        cite["text"] = str(text)[:CITATION_TEXT_CHARS]
    chunk_id = _as_dict(getattr(r, "additional_properties", None)).get("chunk_id")
    if chunk_id:
        cite["chunk_id"] = chunk_id
    return cite


def _extract_citations(response: Any) -> list[dict]:
    """file_search_call.results + message annotations から引用元を抽出(RAG-02/RAGM-01)"""
    by_file: dict[str, dict] = {}
    # 比較は丸め前のスコアで行う(丸めた値で比べると 0.8504 と 0.8501 が同値になり、
    # 最上位でないチャンクの text/chunk_id を引用してしまう — レビュー F-005)
    raw_scores: dict[str, float] = {}
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", "") == "file_search_call":
            for r in getattr(item, "results", None) or []:
                fid = getattr(r, "file_id", "")
                score = getattr(r, "score", None) or 0
                if fid not in by_file or score > raw_scores.get(fid, 0):
                    by_file[fid] = _structured_citation(r)
                    raw_scores[fid] = score
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


def _configured_max_tool_hops() -> int:
    """設定(env `AGENT_MAX_TOOL_HOPS`)の値。空なら既定値。整数でなければ拒否。

    `Settings` 側でレンジ検証も型変換もしないのは意図的(settings.py のコメント)。
    ここで解釈すれば、設定ミスで壊れるのはエージェント実行だけになる。
    """
    raw = (get_settings().agent_max_tool_hops or "").strip()
    if not raw:
        return AGENT_MAX_TOOL_HOPS_DEFAULT
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"AGENT_MAX_TOOL_HOPS must be an integer: {raw!r}") from e


def resolve_max_tool_hops(requested: int | None = None) -> int:
    """このターンのホップ上限を決める(AGT-04)。要求 > 設定(env)の順。

    1 ホップ = モデル 1 往復。天井(`AGENT_MAX_TOOL_HOPS_CEILING`)を超える値は
    **クランプせず拒否**する — 黙って下げると「上げたのに効かない」が起き、
    利用者は上限に当たった理由を設定値から追えなくなる。既定値と天井の根拠は ADR-0025。
    """
    value = requested if requested is not None else _configured_max_tool_hops()
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"max_tool_hops must be an integer >= 1: {value!r}")
    if value > AGENT_MAX_TOOL_HOPS_CEILING:
        raise ValueError(
            f"max_tool_hops exceeds the ceiling "
            f"({AGENT_MAX_TOOL_HOPS_CEILING}): {value}"
        )
    return value


# 承認往復をまたいだツール結果の累計上限(AGT-01d)。ホップ上限より下だと、上限を上げても
# 承認モードだけ先に打ち切られるため、ホップ上限を上げたときは連動して上がる(AGT-04)。
MIN_TOOL_RESULTS_CAP = 16

# ツール使用回数の上限に達したときの最終回答強制プロンプト(AGT-01d)
_FORCE_ANSWER_TEXT = (
    "ツール使用回数の上限に達しました。これまでに得た情報だけで"
    "最終的な回答をまとめてください。"
)

# 上限に当たったことを利用者へ伝える文面(AGT-04)。**黙って打ち切らない**:
# 途中で力尽きたのか答え切ったのかが外から区別できないと、失敗の説明ができない。
_LIMIT_TEXTS = {
    "max_tool_hops": "ツール使用の上限({limit} ホップ)に達したため、"
                     "ここまでに得た情報で回答します（手続きは未完了の可能性があります）。",
    "max_tool_results": "ツール実行の累計が上限({limit} 件)に達したため、"
                        "ここまでに得た情報で回答します（手続きは未完了の可能性があります）。",
}


def _limit_reached_events(reason: str, limit: int) -> Iterator[ChatEvent]:
    """上限到達を通知する。構造化イベントと、現行 UI にそのまま出る本文の両方を出す。

    `notice` は機械可読(将来 UI が専用表示にできる)。ただし現行のチャット UI は
    `delta` / `tool_call` / `error` しか描かないので、それだけだと画面には何も出ない。
    デモで「なぜ途中で止まったか」を出せることが要件なので、既存の警告表記に合わせた
    本文も流す(会話履歴にも残り、後から打ち切りだったと分かる)。
    """
    text = _LIMIT_TEXTS[reason].format(limit=limit)
    yield {"notice": text, "limit_reached": {"reason": reason, "limit": limit}}
    yield {"delta": f"\n\n> ⚠ {text}\n\n"}


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


def _function_spec(tool: Any) -> dict:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _build_agent_tools(
    enabled_tools: list[str] | None,
    mcp_servers: list[dict] | None,
    auto_tools: bool,
    rag_store: str | None,
    http_tools: list[Any] | None = None,
    rag_tool: Any | None = None,
) -> list[dict]:
    """このターンで使用可能なツール仕様を構築(custom + MCP + 外部HTTP + rag_search)。

    http_tools は外部HTTPツール(TOOL-01)の `ToolDef`。呼び出し側が owner 所有の
    ものだけを解決済みで渡す = 明示選択なので enabled_tools では絞らない。
    rag_tool は adb バックエンドの `rag_search`(AGT-04)。指定時は file_search built-in
    ではなくこちらを載せる(**増やすだけ**で built-in 経路は置き換えない)。
    """
    from .mcp_servers import mcp_tool_spec
    from .tools import RAG_SEARCH, tool_specs

    custom_enabled = [t for t in (enabled_tools or []) if t != RAG_SEARCH]
    all_tools = tool_specs(custom_enabled if enabled_tools is not None else None) + [
        _function_spec(t) for t in (http_tools or [])
    ] + [
        mcp_tool_spec(srv, auto_tools) for srv in (mcp_servers or [])
    ]
    if rag_tool is not None:
        # adb バックエンド(AGT-04): チャンク単位の出典(シート名・セル範囲)を結果に載せる
        all_tools.append(_function_spec(rag_tool))
    elif enabled_tools and RAG_SEARCH in enabled_tools and rag_store:
        # rag_searchの実体はfile_search built-in(ユーザーのVector Store) — AGT-01c
        # 絞り込み(RAGM-01)はこの経路では未対応。ルート側が rag_filters を 400 で断る
        all_tools.append(file_search_tool(rag_store))
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
        event = {
            "name": cd["name"],
            "label": tool.label if tool else cd["name"],
            "arguments": cd.get("arguments", "{}"),
            "call_id": cd.get("call_id"),
            "item": cd,
            "status": "pending_approval",
        }
        if tool and tool.tool_id:
            # 外部HTTPツール(TOOL-01): 承認したその1件を id で名指しできるようにする。
            # 名前だけで再解決すると、承認待ちの間に同名で別 URL のツールを作り直された
            # 場合に「利用者が確認したのと違う HTTP 操作」が実行されうる
            event["http_tool_id"] = tool.tool_id
        yield {"tool_call": event}


def _rag_search_citations(output: str) -> Iterator[ChatEvent]:
    """adb バックエンドの `rag_search` の結果を引用イベントにする(AGT-04)。

    file_search built-in は `response.completed` から引用を取れる(`_extract_citations`)が、
    function tool の結果はサーバー側で実行するこちらにしか無い。出さないと、
    せっかくのチャンク単位の出典が UI の出典欄に出ないままになる。
    """
    try:
        results = json.loads(output).get("results")
    except (ValueError, AttributeError):
        return
    if not isinstance(results, list):
        return
    cites = [
        {
            "file_id": r.get("file_id", ""),
            "filename": r.get("filename", ""),
            "score": r.get("score"),
            "source": r.get("source"),
            "text": str(r.get("text") or "")[:CITATION_TEXT_CHARS],
        }
        for r in results if isinstance(r, dict)
    ]
    if cites:
        yield {"citations": cites}


def _run_tool_calls(
    call_dicts: list[dict],
    base_input: list[dict],
    user: str,
    tools: dict,
    execute_tool: Any,
    tool_error: type[Exception],
) -> Iterator[ChatEvent]:
    """function_callを実行し結果を base_input へ追記(自動実行ホップ)。"""
    from .tools import RAG_SEARCH

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
        if cd["name"] == RAG_SEARCH:
            yield from _rag_search_citations(output)
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
    http_tools: list[Any] | None = None,
    rag_backend: str = "vector_store",
    rag_owner: str = "",
    max_tool_hops: int | None = None,
) -> Iterator[ChatEvent]:
    """エージェントモード(AGT-01)。ツール付きResponses呼び出しをループする。

    - ステートレス(全履歴再送)。Responses系モデルのみ
    - auto_tools=False: function_callを {"tool_call"} イベントで通知してストリーム終了
      (UIが承認後、tool_results付きで再呼び出しして継続する)
    - auto_tools=True: サーバー側で実行し、ホップ上限(AGT-04: 設定 or 要求。既定は
      `AGENT_MAX_TOOL_HOPS_DEFAULT`)まで自動継続する
    - http_tools: owner 所有の外部HTTPツール(TOOL-01)の ToolDef。組込ツールと同じ
      レジストリに載せ、JetUse がサーバー側で代理実行する
    - rag_backend='adb': 文書検索を Vector Store の file_search built-in ではなく
      `rag_adb` の検索(チャンク単位の出典)で行う。既定は現行どおり vector_store
    """
    from .tools import RAG_SEARCH, TOOLS, ToolError, adb_rag_search_tool, execute_with

    max_hops = resolve_max_tool_hops(max_tool_hops)
    # adb バックエンドの rag_search は built-in ではなく **function tool**(AGT-04)。
    # 読み取り専用なので承認は要らない(要ると検索のたびにストリームが止まる)
    rag_tool = (
        adb_rag_search_tool(rag_owner)
        if rag_backend == "adb" and enabled_tools and RAG_SEARCH in enabled_tools
        else None
    )
    # そのターン限りのレジストリ。組込を後勝ちにして外部ツールの名前衝突を無害化する
    # (登録時にも予約名を弾いているが、実行段でも組込が上書きされないようにする)
    registry = {**{t.name: t for t in (http_tools or [])}, **TOOLS}
    if rag_tool is not None:
        registry[rag_tool.name] = rag_tool
    model = MODELS[model_key]
    if model.api != "responses":
        yield {"error": "エージェントモードはResponses系モデルのみ対応です"}
        return
    temp = model.default_temperature if temperature is None else temperature
    client = make_inference_client(with_project=True, project_ocid=project_ocid)
    base_input = _build_agent_input(messages, instructions, tool_results)
    extra = _extra_responses_params(model, params or GenParams())
    # ターン内ツール総数の安全弁(AGT-01d): 累積がこの数に達したらツールを外し最終回答を強制。
    # ホップ上限を上げても承認モードだけ先に打ち切られないよう連動させる(AGT-04)
    results_cap = max(MIN_TOOL_RESULTS_CAP, max_hops)
    force_answer = len(tool_results or []) >= results_cap
    all_tools = _build_agent_tools(
        enabled_tools, mcp_servers, auto_tools, rag_store, http_tools, rag_tool
    )
    if force_answer:
        all_tools = []
        base_input.append(_force_answer_message())
        yield from _limit_reached_events("max_tool_results", results_cap)

    for _hop in range(max_hops):
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
            if not (registry.get(cd["name"]) and not registry[cd["name"]].requires_approval)
        ]
        if not auto_tools and needs_approval:
            # 混在バッチは全件承認制(ステートレス継続で安全側の結果が失われるのを防ぐ)
            yield from _emit_pending_approval(call_dicts, registry)
            return  # UIの承認待ち
        # 全件が承認不要(requires_approval=False)の場合は承認モードでも自動実行して継続

        yield from _run_tool_calls(
            call_dicts, base_input, user, registry,
            lambda n, a: execute_with(registry, n, a), ToolError,
        )

    # ホップ上限: エラーではなくツールなしで最終回答を強制する(AGT-01d)。
    # **黙って打ち切らない**(AGT-04): 上限に当たったことを通知してから最終回答へ移る。
    # この最終回答は**ツールを外した 1 往復**で、上限とは別に加算される
    # (= 1 ターンの最大往復数は max_hops + 1)。usage も必ず出す — 出さないと
    # 打ち切ったターンだけコストが記録から漏れる
    log_with(logger, logging.INFO, "agent_hop_limit_reached",
             model=model_key, user=user, max_hops=max_hops)
    yield from _limit_reached_events("max_tool_hops", max_hops)
    final_input = base_input + [_force_answer_message()]
    stream = client.responses.create(
        model=model.oci_id, input=final_input, temperature=temp,
        stream=True, store=False, **extra,
    )
    try:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield {"delta": event.delta}
            elif etype == "response.completed":
                usage = getattr(event.response, "usage", None)
                if usage:
                    yield {"usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    }}
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
        except APIStatusError as e:
            # 404/403/401はモデル未提供/未認可(リージョン/テナンシ差)を示しうる(PORT-02)。
            # ただしRAG(stale vector store)/短期メモリ(stale conversation)/エージェント固有
            # project_ocid絡みの呼び出しはモデル以外が原因の404/403もあるため、プロセス全体を
            # 汚す mark_unavailable はそれらが関与しない素のチャット呼び出しに限定する
            # (レビュー指摘F-001)。
            p = params or GenParams()
            model_only_call = (
                not p.file_search_store and not oci_conversation_id and not project_ocid
            )
            if e.status_code in (401, 403, 404) and model_only_call:
                hint = f"HTTP {e.status_code}"
                mark_unavailable(model_key, hint)
                log_with(
                    logger, logging.WARNING, "chat_model_unavailable",
                    model=model_key, status=e.status_code,
                )
                yield {
                    "error": f"モデル {model_key} はこのリージョン/テナンシでは利用できません"
                    f"({hint})"
                }
                return
            log_with(logger, logging.ERROR, "chat_failed", model=model_key, error=str(e))
            yield {"error": str(e)}
            return
        except Exception as e:  # ストリーミング途中の失敗はイベントで通知
            log_with(logger, logging.ERROR, "chat_failed", model=model_key, error=str(e))
            logger.exception("chat_failed traceback (model=%s)", model_key)
            yield {"error": str(e)}
            return
