"""エージェント定義CRUD・MCPサーバー・ツール実行ルート(AGT-01/02/03, ENH-04)。"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import agents as agents_repo
from jetuse_core import http_tools as http_tools_repo
from jetuse_core import mcp_servers as mcp_repo
from jetuse_core import select_ai_agent
from jetuse_core import tools as tool_registry
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.owner_keys import user_owner_key
from jetuse_core.webtools import SsrfBlockedError

from ..schemas import (
    AgentDefinition,
    HttpToolCreate,
    McpServerCreate,
    ToolExecuteRequest,
)

logger = logging.getLogger("jetuse.service")
router = APIRouter()


# --- エージェント(AGT-03) ---

@router.get("/api/agents")
def list_agents(user: Annotated[AuthContext, Depends(require_user)]):
    return {"agents": agents_repo.list_agents(user.subject)}


@router.get("/api/agents/projects")
async def list_agent_projects(user: Annotated[AuthContext, Depends(require_user)]):
    try:
        return {"projects": await asyncio.to_thread(agents_repo.list_projects)}
    except Exception as e:
        logger.exception("project list failed")
        raise HTTPException(status_code=502, detail=f"プロジェクト一覧の取得に失敗: {e}") from e


@router.post("/api/agents")
def create_agent(
    req: AgentDefinition, user: Annotated[AuthContext, Depends(require_user)]
):
    return agents_repo.create_agent(user.subject, req.validated(user.subject))


@router.get("/api/agents/{aid}")
def get_agent(aid: str, user: Annotated[AuthContext, Depends(require_user)]):
    a = agents_repo.get_agent(user.subject, aid)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    return a


@router.put("/api/agents/{aid}")
def update_agent(
    aid: str, req: AgentDefinition, user: Annotated[AuthContext, Depends(require_user)]
):
    a = agents_repo.update_agent(user.subject, aid, req.validated(user.subject))
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    return a


@router.delete("/api/agents/{aid}")
def delete_agent(aid: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not agents_repo.delete_agent(user.subject, aid):
        raise HTTPException(status_code=404, detail="agent not found")
    # Select AI Agent のDBオブジェクトを後始末(冪等。他種別では何もしない)。
    # owner キーは run() と同じ user_owner_key を通す(名前一致 = 確実に drop される)
    try:
        select_ai_agent.drop(user_owner_key(user.subject), aid)
    except Exception:
        logger.exception("select_ai drop failed (ignored)")
    return {"deleted": True}


@router.get("/api/agent/mcp-servers")
def list_mcp_servers(user: Annotated[AuthContext, Depends(require_user)]):
    return {"servers": mcp_repo.list_servers(user.subject)}


@router.post("/api/agent/mcp-servers")
def create_mcp_server(
    req: McpServerCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    if req.auth_token:
        # Vault書き込みは現行ポリシー(read)では不可。追加は人間作業(specs/11)
        raise HTTPException(
            status_code=501,
            detail="認証付きMCPサーバーの登録にはVault書き込み権限の追加が必要です"
            "（docs/setup/iam.md参照。現在は認証なしサーバーのみ登録できます）",
        )
    try:
        return mcp_repo.create_server(user.subject, req.label, req.url, None)
    except SsrfBlockedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/api/agent/mcp-servers/{sid}")
def delete_mcp_server(sid: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not mcp_repo.delete_server(user.subject, sid):
        raise HTTPException(status_code=404, detail="server not found")
    return {"deleted": True}


# --- 外部HTTPツール(TOOL-01) ---
# デモ側の素の HTTP エンドポイントをスキーマ付きで登録し、エージェント実行時に
# 組込ツールと同列に配線する口。MCP サーバー登録とは別経路として共存する。

@router.get("/api/agent/http-tools")
def list_http_tools(user: Annotated[AuthContext, Depends(require_user)]):
    return {"tools": http_tools_repo.list_tools(user.subject)}


@router.post("/api/agent/http-tools")
def create_http_tool(
    req: HttpToolCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        return http_tools_repo.create_tool(
            user.subject, req.name, req.description, req.parameters, req.url,
            method=req.method, auth_header=req.auth_header,
            auth_secret_ocid=req.auth_secret_ocid,
            headers=req.headers, idempotency_header=req.idempotency_header,
        )
    except (SsrfBlockedError, http_tools_repo.HttpToolDefError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/api/agent/http-tools/{tid}")
def delete_http_tool(tid: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not http_tools_repo.delete_tool(user.subject, tid):
        raise HTTPException(status_code=404, detail="tool not found")
    return {"deleted": True}


@router.get("/api/agent/tools")
async def agent_tools(user: Annotated[AuthContext, Depends(require_user)]):
    """ツール選択UI用の一覧(AGT-01b)"""
    return {"tools": tool_registry.list_tools()}


@router.get("/api/agent/select-ai-tools")
async def agent_select_ai_tools(user: Annotated[AuthContext, Depends(require_user)]):
    """Select AI Agent で選択可能なツール一覧(ENH-04)"""
    return {"tools": select_ai_agent.SELECT_AI_TOOLS}


@router.post("/api/agent/execute-tool")
async def agent_execute_tool(
    req: ToolExecuteRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    registry = tool_registry.TOOLS
    if req.name not in registry:
        # 承認フローで外部HTTPツール(TOOL-01)が承認された場合。owner 所有のものだけ解決する。
        # **名前での再解決は許さない**: 承認待ちの間に削除→同名で別 URL のツールを作られると、
        # 利用者が確認したのと違う HTTP 操作が走る。承認イベントが返した id を必須にする
        if not req.http_tool_id:
            raise HTTPException(
                status_code=400,
                detail=f"未知のツール: {req.name}"
                "（外部HTTPツールは承認イベントの http_tool_id を添えて実行してください）",
            )
        # 照会の失敗(DB 障害等)は握り潰さない — 存在しないツールと同じ 400 にすると、
        # 呼び出し側も監視もサービス障害を区別できなくなる(共通ハンドラが 503)
        rows = await asyncio.to_thread(
            http_tools_repo.get_tools, user.subject, [req.http_tool_id]
        )
        if not rows:
            # 削除済み・他人所有・不正 id。所有者強制の 0 行 = 404(登録の解決と同じ契約)
            raise HTTPException(status_code=404, detail="http tool not found")
        row = rows[0]
        if row["name"] != req.name:
            # 承認したツールが別物に差し替えられている
            raise HTTPException(
                status_code=409,
                detail="承認されたツールの定義が変更されています。やり直してください",
            )
        registry = {**registry, req.name: http_tools_repo.to_tooldef(row)}
    try:
        output = await asyncio.to_thread(
            tool_registry.execute_with, registry, req.name, req.arguments
        )
    except tool_registry.ToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log_with(logger, logging.INFO, "tool executed (approved)",
             tool=req.name, user=user.subject)
    return {"output": output}
