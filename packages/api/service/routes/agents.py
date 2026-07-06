"""エージェント定義CRUD・MCPサーバー・ツール実行ルート(AGT-01/02/03, ENH-04)。"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import agents as agents_repo
from jetuse_core import mcp_servers as mcp_repo
from jetuse_core import select_ai_agent
from jetuse_core import tools as tool_registry
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.owner_keys import user_owner_key
from jetuse_core.webtools import SsrfBlockedError

from ..schemas import AgentDefinition, McpServerCreate, ToolExecuteRequest

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
    try:
        output = await asyncio.to_thread(
            tool_registry.execute_tool, req.name, req.arguments
        )
    except tool_registry.ToolError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    log_with(logger, logging.INFO, "tool executed (approved)",
             tool=req.name, user=user.subject)
    return {"output": output}
