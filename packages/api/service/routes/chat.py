"""チャットストリーミングルート(CHAT-01)。SSE疎通・モデル一覧も同居。"""

import asyncio
import json
import logging
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from jetuse_core import agents as agents_repo
from jetuse_core import (
    audit,
    guardrails,
    moderation,
    rag,
    rag_adb,
    rag_opensearch,
    rag_select_ai,
)
from jetuse_core import conversations as conv_repo
from jetuse_core import http_tools as http_tools_repo
from jetuse_core import mcp_servers as mcp_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.chat import (
    GenParams,
    create_oci_conversation,
    resolve_max_doc_searches,
    resolve_max_tool_hops,
)
from jetuse_core.logging import log_with
from jetuse_core.model_compat import agent_refusal
from jetuse_core.models import DEFAULT_MODEL, MODELS, model_status
from jetuse_core.settings import get_settings

from .. import agent_dispatch
from ..schemas import ChatRequest
from ..sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")
router = APIRouter()

# Responses API を使わない RAG バックエンド(自前検索 → 単発生成)。値は (モジュール, 表示名)。
# generate は呼び出し時にモジュールから引く(差し替えを効かせるため参照を束縛しない)。
NON_RESPONSES_RAG_BACKENDS = {
    "select_ai": (rag_select_ai, "Select AI"),
    "opensearch": (rag_opensearch, "OpenSearch"),
    "adb": (rag_adb, "Oracle AI Database"),
}

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
    return await stream_chat_response(req, user, user.subject)


async def stream_chat_response(  # noqa: ANN202
    req: ChatRequest, user: AuthContext, rag_ns: str
):
    """チャットSSE本体(user単位/デモスコープ共有 — SP1-03/specs/17 §5)。

    rag_ns はRAG文書の名前空間キー: user単位ルートは user.subject、デモスコープは
    DemoContext.namespace。監査・会話・エージェント/MCP解決は実ユーザーのまま。
    """
    # モデル関連の事前検証は agent_id 指定時は行わない: 実行時に req.model ではなく
    # agent_def["model"] を使う(既存test_chat_with_agent_applies_instructionsが示すとおり
    # 呼び出し側モデルは定義側で上書きされる仕様)ため、req.modelがMODELS未登録でも
    # 正当なエージェント実行を拒否してはいけない(レビュー指摘: unknown-model 400の回帰)。
    # 同様に非Responses系バックエンドのRAG(下の分岐がreq.modelを一切使わず終端)も対象外。
    bypasses_model_check = bool(req.agent_id) or (
        req.rag and req.rag_backend in NON_RESPONSES_RAG_BACKENDS
    )
    if not bypasses_model_check and req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")

    # モデル可用性(PORT-02): 直前の呼び出しで利用不可と判明したモデルは、既定モデルなら
    # 同系統(chat-family)へフォールバックし、それ以外は生エラーでなくヒント付きで即座に返す。
    # bypasses_model_check時はreq.modelが未登録キーでもmodel_status()は安全に(True, None)を
    # 返す(dict.getベース)ため、下のif節がそのまま素通りする。
    model_ok, model_hint = model_status(req.model)
    fallback_notice: str | None = None
    if not model_ok and not bypasses_model_check:
        # responses-family必須の機能(agent/rag/画像/保存済み会話メモリ)ではchat-familyへの
        # フォールバックは機能欠落(later checksが400にする、または会話メモリのサイレント
        # 無効化)につながるため行わない。素のchatリクエストに限定する。
        needs_responses_family = bool(
            req.agent or req.agent_id or req.rag or req.images or req.conversation_id
        )
        fallback_key = None
        if req.model == DEFAULT_MODEL and not needs_responses_family:
            fallback_key = next(
                (k for k in MODELS
                 if k != req.model and MODELS[k].api == "chat" and model_status(k)[0]),
                None,
            )
        if fallback_key:
            fallback_notice = (
                f"既定モデル {req.model} は利用できません({model_hint})。"
                f"{fallback_key} に自動フォールバックしました"
            )
            req = req.model_copy(update={"model": fallback_key})
        else:
            hint = f"モデル {req.model} はこのリージョン/テナンシでは利用できません"
            if model_hint:
                hint += f"({model_hint})"

            async def unavailable_gen():
                yield KEEPALIVE_FRAME
                yield f"data: {json.dumps({'error': hint}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                unavailable_gen(), media_type="text/event-stream", headers=SSE_HEADERS
            )

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

    # RAGM-01: 絞り込みは vector_store バックエンドの file_search でしか効かない。
    # 黙って無視すると「版フィルタを掛けたのに旧版が混ざる」ため、明示的に断る。
    if req.rag_filters is not None:
        if not req.rag:
            raise HTTPException(status_code=400, detail="rag_filters requires rag=true")
        if req.rag_backend != "vector_store":
            raise HTTPException(
                status_code=400,
                detail=f"rag_filters is not supported on the {req.rag_backend} backend",
            )
        if req.agent or req.agent_id:
            # エージェント経路(AGT-01/03)は別ディスパッチで、絞り込みを渡す口が無い。
            # 素通しすると黙って無視される(レビュー F-001)ので、ここで断る。
            raise HTTPException(
                status_code=400, detail="rag_filters is not supported in agent mode"
            )

    # AGT-04: エージェント専用のパラメータを黙って無視しない（無視すると「上限を上げたのに
    # 変わらない」「adb を選んだのに出典が粗い」が起きる）。**保存済みエージェント
    # (agent_id) は別ディスパッチ**（Select AI / ホスト型コンテナ）でこのループを通らないので、
    # 受理せず断る。**dispatch より前に**弾く（後ろに置くと agent_id 経路で素通りする）。
    if req.max_tool_hops is not None or req.agent_rag_backend != "vector_store":
        param = "max_tool_hops" if req.max_tool_hops is not None else "agent_rag_backend"
        if req.agent_id:
            raise HTTPException(
                status_code=400,
                detail=f"{param} is not supported for saved agents (agent_id)",
            )
        if not req.agent:
            raise HTTPException(status_code=400, detail=f"{param} requires agent mode")

    # agent と rag は併用できない。**RAG ディスパッチより前に**弾く（後ろに置くと
    # 非Responses系バックエンドでは agent 指定が黙って無視されたまま RAG が走る）。
    if (req.agent or req.agent_id) and req.rag:
        raise HTTPException(status_code=400, detail="agent and rag cannot be combined")

    # Select AI / OpenSearch / ADB自前索引: 非ストリーミングGENERATEを単発deltaで返す
    if req.rag and req.rag_backend in NON_RESPONSES_RAG_BACKENDS:
        prompt = req.messages[-1].content
        _rag_mod, _rag_label = NON_RESPONSES_RAG_BACKENDS[req.rag_backend]

        async def sa_gen():
            yield KEEPALIVE_FRAME
            # generate はモジュール属性として毎回引く(テスト/差し替えが効くように)
            task = asyncio.create_task(
                asyncio.to_thread(_rag_mod.generate, rag_ns, prompt)
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
                    cites = rag.resolve_citation_filenames(rag_ns, cites)
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
        if req.agent or req.rag or req.agent_id:
            # PORT-02: agent_id時はreq.modelがMODELS未登録キーでありうるため、
            # MODELS[req.model]参照(vision判定)より先にこちらを判定する
            # (レビュー指摘: agent_id+images+未知modelがKeyErrorで500していた)。
            raise HTTPException(
                status_code=422, detail="images cannot be combined with agent/rag"
            )
        if not MODELS[req.model].vision:
            raise HTTPException(
                status_code=422, detail="selected model does not support images"
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

    # 外部HTTPツール(TOOL-01): owner所有のものだけ解決してエージェントループへ配線する。
    # 1件でも解決できなければ 404 で止める。黙って外すと、業務APIを参照しないまま
    # もっともらしい回答を返してしまう(削除済み・他人所有・不正idのいずれも同じ)
    http_tool_defs: list = []
    if req.agent and req.http_tool_ids:
        wanted = list(dict.fromkeys(req.http_tool_ids))
        rows = await asyncio.to_thread(http_tools_repo.get_tools, user.subject, wanted)
        if len(rows) != len(wanted):
            missing = sorted(set(wanted) - {r["id"] for r in rows})
            raise HTTPException(
                status_code=404, detail=f"http tool not found: {', '.join(missing)}"
            )
        http_tool_defs = [http_tools_repo.to_tooldef(r) for r in rows]

    mcp_defs: list[dict] = []
    if req.agent and req.mcp_server_ids:
        # owner所有のサーバーのみ解決(AGT-02)
        mcp_defs = await asyncio.to_thread(
            mcp_repo.get_servers, user.subject, req.mcp_server_ids
        )
    # AGT-04: ホップ上限は**エージェントループを使う要求だけ**で解決する。無条件に呼ぶと、
    # 設定値が天井超えのときに素のチャットまで 400 になる（壊すのは当該機能だけに閉じる）。
    # 設定値が不正でもここで断る（クランプしない — ADR-0025）
    max_tool_hops: int | None = None
    if req.agent:
        try:
            max_tool_hops = resolve_max_tool_hops(req.max_tool_hops)
            if req.agent_rag_backend == "adb":
                # AGT-05: 文書検索の上限も入口で検証する（設定が壊れていると
                # ストリームの途中で落ち、打ち切りとの区別がつかなくなる）。
                # 数えるのは adb 経路の function tool だけなので、そこに限る
                resolve_max_doc_searches()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # rag_searchツール(AGT-01c): 有効ならユーザーのVector Storeを解決。
    # adb バックエンド(AGT-04)は Vector Store を使わず rag_adb を直接引く
    agent_rag_store: str | None = None
    eff_tools = agent_def["enabled_tools"] if agent_def else (req.enabled_tools or [])
    if req.agent_rag_backend != "vector_store" and "rag_search" not in eff_tools:
        # バックエンドだけ指定しても rag_search が無効なら文書検索は行われない。
        # 受理すると「adb を選んだのに出典が粗い(そもそも検索していない)」になる
        raise HTTPException(
            status_code=400,
            detail="agent_rag_backend requires rag_search in enabled_tools",
        )
    if (req.agent or agent_def) and eff_tools and "rag_search" in eff_tools:
        if req.agent_rag_backend == "adb":
            # 使えないまま素通しすると、検索が毎回失敗したまま回答だけ返る。
            # 「表が無い(未導入)」と「今つながらない」を区別して理由ごと断る
            state = await asyncio.to_thread(rag_adb.availability)
            if state != rag_adb.READY:
                raise HTTPException(
                    status_code=400,
                    detail=f"adb rag backend is not available ({state})",
                )
        else:
            agent_rag_store = await asyncio.to_thread(rag.get_store_id, rag_ns)

    if agent_def and agent_def["mcp_server_ids"] and agent_def["mine"]:
        # 共有エージェントのMCP(所有者の私有資源)は実行ユーザーには適用しない(specs/11)
        mcp_defs += await asyncio.to_thread(
            mcp_repo.get_servers, user.subject, agent_def["mcp_server_ids"]
        )
    if req.agent:
        # AGT-06: Responses 系かどうかだけでは足りない(ツールを渡せない Responses 系がある)。
        # 断る理由は登録簿が持ち、そのまま利用者に見える文言で返す
        refusal = agent_refusal(req.model)
        if refusal:
            raise HTTPException(status_code=400, detail=refusal)

    rag_store: str | None = None
    if req.rag:
        if MODELS[req.model].api != "responses":
            raise HTTPException(
                status_code=400, detail="rag requires a responses-family model"
            )
        rag_store = await asyncio.to_thread(rag.get_store_id, rag_ns)
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
                file_search_filters=req.rag_filters,  # RAGM-01(検証済み構造のみ)
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
                    http_tools=http_tool_defs,
                    rag_backend=req.agent_rag_backend,  # AGT-04
                    rag_owner=rag_ns,
                    max_tool_hops=max_tool_hops,
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
                    # エージェントは**ホップごとに** usage を流す(AGT-04 で最終回答の
                    # 1 往復も含めた)。上書きすると最後の 1 往復しか記録されず、
                    # 会話の使用量も監査ログもターン全体を大幅に過少計上する
                    usage = {
                        k: (usage or {}).get(k, 0) + v for k, v in ev["usage"].items()
                    }
                if "citations" in ev:  # 日本語ファイル名の文字化け対策(元名へ解決)
                    ev["citations"] = rag.resolve_citation_filenames(
                        rag_ns, ev["citations"]
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
        if fallback_notice:
            yield f"data: {json.dumps({'notice': fallback_notice}, ensure_ascii=False)}\n\n"
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


def _model_entry(k: str, m) -> dict:
    ok, hint = model_status(k)
    entry = {
        "key": k,
        "label": m.label,
        "default_temperature": m.default_temperature,
        "api": m.api,
        "reasoning": m.reasoning,  # UIの出し分け用(CHAT-04b)
        "min_max_tokens": m.min_max_tokens,
        "vision": m.vision,  # 画像添付UIの出し分け(MM-01)
        "multi_image": m.multi_image,  # 複数画像可否(ENH-09)
        "available": ok,  # PORT-02: リージョン/テナンシで利用不可と判明したものはfalse
        # AGT-06: エージェントモードの可否。UIが選ばせる前に出し分けられるようにする
        # (選ばせてから 400 で断るより、選べないほうがよい)
        "agent": m.agent,
    }
    if m.agent_blocked_reason:
        entry["agent_blocked_reason"] = m.agent_blocked_reason
    if not ok and hint:
        entry["unavailable_reason"] = hint
    return entry


@router.get("/api/chat/models")
async def list_models(user: Annotated[AuthContext, Depends(require_user)]):
    return {"models": [_model_entry(k, m) for k, m in MODELS.items()]}
