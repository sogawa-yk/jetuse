"""保存済みエージェントの実行ディスパッチ(P1c §5 ⚠補正)。

`/api/chat/stream` の agent 実行は3経路ある:
  1. select_ai → `select_ai_agent.run()`(早期return) ... `select_ai_stream_response`
  2. hosted   → `hosted_agent.invoke_agent()`(早期return) ... `hosted_agent_stream_response`
  3. アドホック native → `jetuse_core.chat.stream_agent()`(route内 produce で実行)

本モジュールは保存済みagentの主経路 (1)(2) を集約する。両者とも
`StreamingResponse` を返す。(3) は route の通常ストリームと永続化/監査が
密結合するため route 側(produce)に残す。SSEフレームは分離前とバイト等価。
"""

import asyncio
import json
import logging

from fastapi.responses import StreamingResponse

from jetuse_core import audit, genai, hosted_agent, rag, select_ai_agent
from jetuse_core import conversations as conv_repo
from jetuse_core import tools as tool_registry
from jetuse_core.auth import AuthContext
from jetuse_core.models import MODELS
from jetuse_core.owner_keys import user_owner_key

from .schemas import ChatRequest
from .sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")

# ホスト型コンテナが内蔵するツール(ADR-0009)
CONTAINER_TOOLS = {
    "web_search", "web_fetch", "get_current_time", "query_database",
    tool_registry.RAG_SEARCH,
}


def select_ai_stream_response(
    req: ChatRequest, user: AuthContext, agent_def: dict
) -> StreamingResponse:
    """ENH-04: Select AI Agent(ADB DBネイティブ)。RUN_TEAMをkeepalive付きで待ち1 deltaで返す"""
    sai_q = req.messages[-1].content
    sai_role = agent_def.get("instructions") or select_ai_agent.DEFAULT_ROLE
    aid = agent_def.get("id") or "default"
    sai_tools = agent_def.get("enabled_tools") or []

    async def select_ai_gen():
        yield KEEPALIVE_FRAME
        if req.conversation_id and req.persist_user:
            await asyncio.to_thread(
                conv_repo.append_message, req.conversation_id, "user", sai_q
            )
        # owner キーは必ず user_owner_key を通す(予約接頭辞 sub が demo owner と衝突し、
        # RAG tool から demo profile を越境参照するのを防ぐ — rag_search と同じ規則)
        fut = asyncio.ensure_future(asyncio.to_thread(
            lambda: select_ai_agent.run(
                user_owner_key(user.subject), aid, sai_q, role=sai_role, tools=sai_tools)))
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=KEEPALIVE_SECONDS)
                if not done:
                    yield KEEPALIVE_FRAME
                    continue
                out = fut.result()
                break
            yield f"data: {json.dumps({'delta': out}, ensure_ascii=False)}\n\n"
            if req.conversation_id and out:
                await asyncio.to_thread(
                    conv_repo.append_message, req.conversation_id, "assistant", out
                )
            await asyncio.to_thread(
                audit.log_event, user.subject, "agent", None, None, None, "ok", "select_ai"
            )
        except Exception as e:
            logger.exception("select_ai agent failed")
            err = {"error": f"Select AI Agent実行エラー: {str(e)[:200]}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            fut.cancel()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        select_ai_gen(), media_type="text/event-stream", headers=SSE_HEADERS
    )


def _error_stream(message: str) -> StreamingResponse:
    """理由を1フレームで返して閉じる SSE。UI は error をそのまま表示する。"""

    async def gen():
        yield f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


async def hosted_agent_stream_response(
    req: ChatRequest, user: AuthContext, agent_def: dict
) -> StreamingResponse:
    """AGT-MULTI(ADR-0009): SDK別ホスト型ReActコンテナで実行。

    tools/promptはステートとしてpush(コンテナは汎用ReAct、焼き込まない)。
    非ストリーミングinvokeを keepalive 付きで待ち、tool_trace+本文をdeltaで流す。
    """
    sdk = hosted_agent.normalize_sdk(agent_def.get("framework"))
    # 未配備の判定を最初に行う。RAG store 参照や project 自動作成といった副作用を
    # 走らせてから落ちると、「認証無効 / 対象外リージョン / SDK 未配備」という本当の理由が
    # 別のエラーに隠れる(PORT-03)。
    avail = hosted_agent.availability()
    if not avail["sdks"].get(sdk, False):
        return _error_stream(avail["reason"] or "このSDKのエージェントは配備されていません")

    last_user = req.messages[-1].content
    history = [
        {"role": m.role, "content": m.content}
        for m in req.messages[:-1] if m.role != "system"
    ]
    enabled = [t for t in (agent_def.get("enabled_tools") or []) if t in CONTAINER_TOOLS]
    rag_store_id = None
    if tool_registry.RAG_SEARCH in enabled:
        # 資源キーは通常 RAG ルートと同じ owner キーヘルパーを必ず通す(specs/18 §3.2.1 —
        # sub='demo_<uuid>' が demo namespace の store と衝突するのを防ぐ)。
        rag_store_id = await asyncio.to_thread(
            rag.resolve_store_for_read, user_owner_key(user.subject))
    model_key = agent_def.get("model")
    # コンテナ側の env の PROJECT_OCID は公開スタックでは空(Terraform に解決結果を戻せない)。
    # OpenAi-Project が無いと 3SDK の LLM 呼び出しと rag_search が揃って失敗するため、
    # **API 側で解決した値を state に載せて渡す**(PORT-03)。
    # エージェントに Project が割り当てられていればそれを優先する(SPIKE-05: エージェント単位で
    # 会話・記憶を分離する契約。自動解決で上書きすると分離が壊れる)。
    project_ocid = (agent_def.get("project_ocid") or "").strip()
    if not project_ocid:
        try:
            project_ocid = await asyncio.to_thread(genai.resolve_project_ocid)
        except genai.ProjectResolutionError as e:
            # 空の OpenAi-Project で invoke しても後段で別のHTTPエラーになるだけで、
            # ProjectResolutionError が持つ復旧手順が利用者に届かない(FIX-47 と同じ方針)。
            return _error_stream(f"エージェントを実行できません: {e}")
    state = {
        "system_prompt": agent_def.get("instructions") or "",
        "enabled_tools": enabled,
        "input": last_user,
        "history": history,
        "rag_store_id": rag_store_id,
        "project_ocid": project_ocid,
        "model": MODELS[model_key].oci_id if model_key in MODELS else "openai.gpt-oss-120b",
    }

    async def agent_gen():
        yield KEEPALIVE_FRAME
        if req.conversation_id and req.persist_user:
            await asyncio.to_thread(
                conv_repo.append_message, req.conversation_id, "user", last_user
            )
        fut = asyncio.ensure_future(
            asyncio.to_thread(hosted_agent.invoke_agent, sdk, state)
        )
        try:
            while True:
                done, _ = await asyncio.wait({fut}, timeout=KEEPALIVE_SECONDS)
                if not done:
                    yield KEEPALIVE_FRAME
                    continue
                data = fut.result()
                break
            for tc in data.get("tool_trace") or []:
                note = f"> 🛠 {tc.get('name')} 実行\n\n"
                yield f"data: {json.dumps({'delta': note}, ensure_ascii=False)}\n\n"
            out = data.get("output") or ""
            yield f"data: {json.dumps({'delta': out}, ensure_ascii=False)}\n\n"
            if req.conversation_id and out:
                await asyncio.to_thread(
                    conv_repo.append_message, req.conversation_id, "assistant", out
                )
            await asyncio.to_thread(
                audit.log_event, user.subject, "agent", model_key,
                None, None, "ok", sdk,
            )
        except hosted_agent.HostedAgentNotConfigured as e:
            # 例外メッセージ自体が「なぜ使えないか・どうすれば使えるか」を含む(PORT-03)。
            # 内部の欠落キー名は hosted_agent 側でログにだけ出す。
            err = {"error": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("hosted agent invoke failed")
            err = {"error": f"エージェント実行エラー: {str(e)[:200]}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            fut.cancel()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        agent_gen(), media_type="text/event-stream", headers=SSE_HEADERS
    )


def is_select_ai_agent(agent_def: dict | None) -> bool:
    """保存済みagentがSelect AI(DBネイティブ)経路かどうか。"""
    return bool(agent_def and agent_def.get("framework") == "select_ai")
