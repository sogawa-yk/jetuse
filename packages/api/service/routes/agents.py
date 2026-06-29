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
from jetuse_core.plugins import loader as contrib_loader
from jetuse_core.webtools import SsrfBlockedError

from ..schemas import (
    AgentDefinition,
    McpServerCreate,
    PluginPublishRequest,
    ToolExecuteRequest,
)
from .plugin_publish import publish_entity

logger = logging.getLogger("jetuse.service")
router = APIRouter()


# --- エージェント(AGT-03) ---

@router.get("/api/agents")
def list_agents(user: Annotated[AuthContext, Depends(require_user)]):
    # ユーザー + インストール済みを合算し、出所バッジ・名前衝突解決(PLG-07)。
    return {"agents": contrib_loader.list_agents(user.subject)}


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
    # 出所バッジ(plugin名/版)を付与(PLG-07)。通常定義は追加 I/O なし。
    contrib_loader.enrich_one(a)
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
    # Select AI Agent のDBオブジェクトを後始末(冪等。他種別では何もしない)
    try:
        select_ai_agent.drop(user.subject, aid)
    except Exception:
        logger.exception("select_ai drop failed (ignored)")
    return {"deleted": True}


@router.post("/api/agents/{aid}/publish")
def publish_agent(
    aid: str,
    req: PluginPublishRequest,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """エージェント定義を manifest 化・署名してマーケット(中央レジストリ)へ公開する(PLG-05)。"""
    a = agents_repo.get_agent(user.subject, aid)
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    return publish_entity(kind="agent", definition=a, entity_id=aid, version=req.version)


@router.get("/api/agent/mcp-servers")
def list_mcp_servers(user: Annotated[AuthContext, Depends(require_user)]):
    return {"servers": mcp_repo.list_servers(user.subject)}


@router.post("/api/agent/mcp-servers")
def create_mcp_server(
    req: McpServerCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    # 認証付き(auth_token あり)は実トークンを Vault へ束ね、DB には OCID 参照のみ保持(BE-08)。
    # SSRF/URL ガードは Vault 書込の前に効く(fail-closed)。Vault 未設定/権限欠如は 503(人間ゲート)。
    try:
        return mcp_repo.create_server(
            user.subject, req.label, req.url, auth_token=req.auth_token
        )
    except (SsrfBlockedError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except mcp_repo.VaultWriteError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.delete("/api/agent/mcp-servers/{sid}")
def delete_mcp_server(sid: str, user: Annotated[AuthContext, Depends(require_user)]):
    # 自管理 secret の Vault 削除予約が DB 行削除より前に走る。予約失敗時は行を残し 503(再試行可)。
    try:
        deleted = mcp_repo.delete_server(user.subject, sid)
    except mcp_repo.VaultWriteError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not deleted:
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
