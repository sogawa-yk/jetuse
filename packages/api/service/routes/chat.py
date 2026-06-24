"""チャットストリーミングルート(CHAT-01)。SSE疎通・モデル一覧も同居。"""

import asyncio
import json
import logging
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from jetuse_core import agents as agents_repo
from jetuse_core import audit, guardrails, moderation, rag, rag_opensearch, rag_select_ai
from jetuse_core import conversations as conv_repo
from jetuse_core import mcp_servers as mcp_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.chat import GenParams, create_oci_conversation
from jetuse_core.logging import log_with
from jetuse_core.models import MODELS
from jetuse_core.settings import get_settings

from .. import agent_dispatch
from ..schemas import ChatRequest
from ..sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")
router = APIRouter()

# stream_chat / stream_agent は tests が `service.main` 上で monkeypatch するため、
# 呼び出し時に service.main 経由で解決する(lazy import で循環を回避)。


def _stream_chat(*args, **kwargs):
    from .. import main as svc_main
    return svc_main.stream_chat(*args, **kwargs)


def _stream_agent(*args, **kwargs):
    from .. import main as svc_main
    return svc_main.stream_agent(*args, **kwargs)


@router.get("/api/chat/ping")
async def chat_ping(
    user: Annotated[AuthContext, Depends(require_user)],
    events: int = 5,
    delay: float = 0.2,
):
    """SSE疎通デモ。keepaliveコメント送出はADR-0003の実装要件。"""

    async def gen():
        yield KEEPALIVE_FRAME
        for i in range(events):
            await asyncio.sleep(delay)
            yield f"data: {json.dumps({'i': i, 'user': user.subject})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api/chat/stream")
async def chat_stream(  # noqa: ANN202
    req: ChatRequest,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """チャットストリーミング(CHAT-01)。LLMの2系統APIを正規化したSSEを返す。"""
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")

    # 監査の機能ラベル(SEC-02)
    audit_feature = (
        req.source or ("agent" if (req.agent or req.agent_id) else "rag" if req.rag else "chat")
    )

    # 入力モデレーション(SEC-02。MODERATION_ENABLED=trueのとき)
    if (
        get_settings().moderation_enabled
        and req.messages[-1].role == "user"
        and not req.sdk_state  # 承認往復の再開はチェック済み
    ):
        flagged, category = await asyncio.to_thread(
            moderation.check_input, req.messages[-1].content
        )
        if flagged:
            await asyncio.to_thread(
                audit.log_event, user.subject, "moderation_block",
                status="blocked", meta=category,
            )

            async def blocked_gen():
                yield KEEPALIVE_FRAME
                msg = {"error": "入力内容が利用ポリシーに抵触するため処理できません"}
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                blocked_gen(), media_type="text/event-stream", headers=SSE_HEADERS
            )

    # プロンプトインジェクション検知(GAP-01。OCIマネージドApplyGuardrails)
    if (
        get_settings().prompt_injection_guard_enabled
        and req.messages[-1].role == "user"
        and not req.sdk_state
    ):
        pi_flagged, pi_score = await asyncio.to_thread(
            guardrails.check_prompt_injection, req.messages[-1].content
        )
        if pi_flagged:
            await asyncio.to_thread(
                audit.log_event, user.subject, "prompt_injection_block",
                status="blocked", meta=f"score={pi_score}",
            )

            async def pi_blocked_gen():
                yield KEEPALIVE_FRAME
                msg = {
                    "error": "プロンプトインジェクションの可能性があるため処理を中断しました"
                }
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                pi_blocked_gen(), media_type="text/event-stream", headers=SSE_HEADERS
            )

    # Select AI / OpenSearch バックエンド: 非ストリーミングGENERATEを単発deltaで返す
    if req.rag and req.rag_backend in ("select_ai", "opensearch"):
        prompt = req.messages[-1].content
        _rag_gen = (rag_opensearch.generate if req.rag_backend == "opensearch"
                    else rag_select_ai.generate)
        _rag_label = "OpenSearch" if req.rag_backend == "opensearch" else "Select AI"

        async def sa_gen():
            yield KEEPALIVE_FRAME
            task = asyncio.create_task(
                asyncio.to_thread(_rag_gen, user.subject, prompt)
            )
            try:
                while True:
                    try:
                        body, cites = await asyncio.wait_for(
                            asyncio.shield(task), timeout=KEEPALIVE_SECONDS
                        )
                        break
                    except TimeoutError:
                        yield KEEPALIVE_FRAME  # 初回は索引構築で数分かかりうる
                yield f"data: {json.dumps({'delta': body}, ensure_ascii=False)}\n\n"
                if cites:
                    cites = rag.resolve_citation_filenames(user.subject, cites)
                    yield (
                        f"data: {json.dumps({'citations': cites}, ensure_ascii=False)}\n\n"
                    )
            except Exception as e:
                logger.exception("%s rag failed", _rag_label)
                err = {"error": f"{_rag_label} RAGの実行に失敗しました: {str(e)[:200]}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sa_gen(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    if req.agent and req.rag:
        raise HTTPException(status_code=400, detail="agent and rag cannot be combined")

    # 画像入力(MM-01): visionモデル必須・agent/rag併用不可・最終メッセージはuser
    if req.images:
        if not MODELS[req.model].vision:
            raise HTTPException(
                status_code=422, detail="selected model does not support images"
            )
        if req.agent or req.rag or req.agent_id:
            raise HTTPException(
                status_code=422, detail="images cannot be combined with agent/rag"
            )
        if req.messages[-1].role != "user":
            raise HTTPException(status_code=422, detail="last message must be user")
        for u in req.images:
            if not u.startswith("data:image/"):
                raise HTTPException(status_code=422, detail="images must be data URIs")
            if len(u) > 2 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="image too large (max 2MB)")
        if sum(len(u) for u in req.images) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="images too large (max 10MB total)")

    # エージェント定義の適用(AGT-03)
    agent_def: dict | None = None
    if req.agent_id:
        agent_def = await asyncio.to_thread(
            agents_repo.get_agent, user.subject, req.agent_id
        )
        if not agent_def:
            raise HTTPException(status_code=404, detail="agent not found")

    # 保存済みagentの実行経路 (1)(2) は agent_dispatch へ集約(P1c §5)。
    # ENH-04: Select AI Agent(ADB DBネイティブ)
    if agent_dispatch.is_select_ai_agent(agent_def):
        return agent_dispatch.select_ai_stream_response(req, user, agent_def)

    # AGT-MULTI(ADR-0009): その他はSDK別ホスト型ReActコンテナで実行
    if agent_def:
        return await agent_dispatch.hosted_agent_stream_response(req, user, agent_def)

    mcp_defs: list[dict] = []
    if req.agent and req.mcp_server_ids:
        # owner所有のサーバーのみ解決(AGT-02)
        mcp_defs = await asyncio.to_thread(
            mcp_repo.get_servers, user.subject, req.mcp_server_ids
        )
    # rag_searchツール(AGT-01c): 有効ならユーザーのVector Storeを解決
    agent_rag_store: str | None = None
    eff_tools = agent_def["enabled_tools"] if agent_def else (req.enabled_tools or [])
    if (req.agent or agent_def) and eff_tools and "rag_search" in eff_tools:
        agent_rag_store = await asyncio.to_thread(rag.get_store_id, user.subject)

    if agent_def and agent_def["mcp_server_ids"] and agent_def["mine"]:
        # 共有エージェントのMCP(所有者の私有資源)は実行ユーザーには適用しない(specs/11)
        mcp_defs += await asyncio.to_thread(
            mcp_repo.get_servers, user.subject, agent_def["mcp_server_ids"]
        )
    if req.agent and MODELS[req.model].api != "responses":
        raise HTTPException(
            status_code=400, detail="agent mode requires a responses-family model"
        )

    rag_store: str | None = None
    if req.rag:
        if MODELS[req.model].api != "responses":
            raise HTTPException(
                status_code=400, detail="rag requires a responses-family model"
            )
        rag_store = await asyncio.to_thread(rag.get_store_id, user.subject)
        if not rag_store:
            raise HTTPException(status_code=400, detail="no documents uploaded")

    oci_conv: str | None = None
    if req.conversation_id and not req.agent_id:
        conv = await asyncio.to_thread(
            conv_repo.get_conversation, user.subject, req.conversation_id
        )
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
        # 短期メモリ(CHAT-06): Responses系のみ。再生成時(persist_user=false)は
        # Conversation側のアイテム重複を避けるためステートレスにフォールバック
        if MODELS[req.model].api == "responses" and req.persist_user:
            oci_conv = conv.get("oci_conversation_id")
            if not oci_conv:
                try:
                    # memory_subject_id=JWT sub: 同一ユーザーの全会話で長期メモリを共有
                    # (AGT-05。プロジェクトのLTM有効化が前提 — jetuse-dev-project)
                    oci_conv = await asyncio.to_thread(
                        create_oci_conversation,
                        {
                            "jetuse_cid": req.conversation_id,
                            "memory_subject_id": user.subject,
                        },
                    )
                    await asyncio.to_thread(
                        conv_repo.set_oci_conversation,
                        user.subject, req.conversation_id, oci_conv,
                    )
                except Exception:
                    logger.exception("oci conversation create failed (fallback stateless)")
                    oci_conv = None

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    cancel = threading.Event()  # クライアント切断→上流打ち切り(CHAT-08)

    def persist(role: str, content: str) -> None:
        """永続化失敗でチャットは止めない(ログのみ)"""
        try:
            conv_repo.append_message(req.conversation_id, role, content)
        except Exception:
            logger.exception("persist failed")

    def put_event(ev) -> bool:
        """キューへ投入。キャンセル検知でFalse(満杯キューでのデッドロック防止)"""
        fut = asyncio.run_coroutine_threadsafe(queue.put(ev), loop)
        while True:
            try:
                fut.result(timeout=1.0)
                return True
            except TimeoutError:
                if cancel.is_set():
                    fut.cancel()
                    return False

    def produce() -> None:
        parts: list[str] = []
        usage: dict | None = None
        cancelled = False
        upstream = None
        try:
            gen_params = GenParams(
                top_p=req.top_p,
                max_tokens=req.max_tokens,
                reasoning_effort=req.reasoning_effort,
                file_search_store=rag_store,
            )
            eff_model = agent_def["model"] if agent_def else req.model
            use_agent_loop = req.agent or bool(
                agent_def and (agent_def["enabled_tools"] or mcp_defs)
            )
            if use_agent_loop:
                # エージェントモード(AGT-01/03): ステートレス・短期メモリ非統合
                upstream = _stream_agent(
                    eff_model,
                    [m.model_dump() for m in req.messages],
                    req.temperature,
                    user=user.subject,
                    auto_tools=(
                        agent_def["auto_tools"] if agent_def else req.auto_tools
                    ),
                    tool_results=req.tool_results,
                    params=gen_params,
                    enabled_tools=(
                        agent_def["enabled_tools"] if agent_def else req.enabled_tools
                    ),
                    mcp_servers=mcp_defs,
                    instructions=agent_def["instructions"] if agent_def else None,
                    project_ocid=agent_def.get("project_ocid") if agent_def else None,
                    rag_store=agent_rag_store,
                )
            elif agent_def:
                # ツールなしエージェント: instructionsをsystemとして付与(AGT-03)
                agent_msgs = [
                    {"role": "system", "content": agent_def["instructions"]}
                ] + [m.model_dump() for m in req.messages]
                upstream = _stream_chat(
                    eff_model,
                    agent_msgs,
                    req.temperature,
                    user=user.subject,
                    oci_conversation_id=None,  # Project分離のため既定会話は使わない
                    params=gen_params,
                    project_ocid=agent_def.get("project_ocid"),
                )
            else:
                plain_msgs = [m.model_dump() for m in req.messages]
                if req.images:
                    # 最終userメッセージをcontent partsへ(MM-01。chat系は素通しで届く)
                    plain_msgs[-1] = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": plain_msgs[-1]["content"]},
                            *[
                                {"type": "image_url", "image_url": {"url": u}}
                                for u in req.images
                            ],
                        ],
                    }
                upstream = _stream_chat(
                    req.model,
                    plain_msgs,
                    req.temperature,
                    user=user.subject,
                    oci_conversation_id=oci_conv,
                    params=gen_params,
                )
            if req.conversation_id and req.persist_user:
                persist("user", req.messages[-1].content)
            for ev in upstream:
                if "delta" in ev:
                    parts.append(ev["delta"])
                if "usage" in ev:
                    usage = ev["usage"]
                if "citations" in ev:  # 日本語ファイル名の文字化け対策(元名へ解決)
                    ev["citations"] = rag.resolve_citation_filenames(
                        user.subject, ev["citations"]
                    )
                if cancel.is_set() or not put_event(ev):
                    cancelled = True
                    log_with(
                        logger, logging.INFO, "upstream cancelled",
                        model=req.model, user=user.subject, partial_chars=len("".join(parts)),
                    )
                    break
        except Exception as e:
            # 同期例外でもSSEをkeepaliveのまま放置しない(終端は必ずfinallyで送る)
            logger.exception("produce failed")
            put_event({"error": str(e)})
        finally:
            if upstream is not None:
                upstream.close()  # 上流LLMストリームを打ち切る(CHAT-08)
            if req.conversation_id and parts:
                persist("assistant", "".join(parts))
            if req.conversation_id and usage:
                try:
                    conv_repo.log_usage(
                        user.subject, req.conversation_id, req.model,
                        usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    )
                except Exception:
                    logger.exception("usage log failed")
            # 監査ログ(SEC-02): 会話の有無に関わらず記録
            audit.log_event(
                user.subject, audit_feature, model=req.model,
                input_tokens=(usage or {}).get("input_tokens"),
                output_tokens=(usage or {}).get("output_tokens"),
                status="cancelled" if cancelled else "ok",
                meta=req.agent_id,
            )
            if not cancelled:
                put_event(None)

    async def gen():
        yield KEEPALIVE_FRAME
        producer = loop.run_in_executor(None, produce)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                if ev is None:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            # クライアント切断時: キャンセルを伝搬してから部分応答の永続化完了を待つ
            cancel.set()
            await producer

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/api/chat/models")
async def list_models(user: Annotated[AuthContext, Depends(require_user)]):
    return {
        "models": [
            {
                "key": k,
                "label": m.label,
                "default_temperature": m.default_temperature,
                "api": m.api,
                "reasoning": m.reasoning,  # UIの出し分け用(CHAT-04b)
                "min_max_tokens": m.min_max_tokens,
                "vision": m.vision,  # 画像添付UIの出し分け(MM-01)
                "multi_image": m.multi_image,  # 複数画像可否(ENH-09)
            }
            for k, m in MODELS.items()
        ]
    }
