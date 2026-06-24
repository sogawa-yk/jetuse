"""service層 validator(P1c §5)。

route schema(`schemas.AgentDefinition` / `UsecaseDefinition`)の `validated()` から
呼ばれる純粋関数。route とは独立にユニットテストできるよう分離した。
検証セマンティクス(422 メッセージ等)は分離前と完全に同一を維持する。

注意: 引数 `defn` は schemas のモデルインスタンス。循環 import を避けるため
型注釈は `typing.Any`(schemas が validators を import するため)。
"""

from typing import Any

from fastapi import HTTPException

from jetuse_core import mcp_servers as mcp_repo
from jetuse_core import select_ai_agent
from jetuse_core import tools as tool_registry
from jetuse_core.models import MODELS


def validate_agent_definition(defn: Any, owner: str) -> dict:
    if defn.model not in MODELS:
        raise HTTPException(status_code=422, detail=f"unknown model: {defn.model}")
    # Select AI Agent はDB内ツール(sql/rag)を使う。コンテナ内蔵ツールの制約は適用しない
    if defn.framework == "select_ai":
        bad_sai = [t for t in defn.enabled_tools if t not in select_ai_agent.VALID_TOOLS]
        if bad_sai:
            raise HTTPException(
                status_code=422,
                detail=f"Select AI Agent unsupported tools: {bad_sai}",
            )
        return defn.model_dump()
    # ホスト型コンテナが内蔵するツールのみ(ADR-0009)。code_interpreter/MCPはコンテナ未対応。
    supported = {
        "web_search", "web_fetch", "get_current_time", "query_database",
        tool_registry.RAG_SEARCH,
    }
    bad = [t for t in defn.enabled_tools if t not in supported]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"hosted agent containers do not support tools: {bad}",
        )
    if defn.mcp_server_ids:
        raise HTTPException(
            status_code=422,
            detail="hosted agent containers do not support MCP servers yet",
        )
    if defn.mcp_server_ids:
        owned = {s["id"] for s in mcp_repo.get_servers(owner, defn.mcp_server_ids)}
        missing = [m for m in defn.mcp_server_ids if m not in owned]
        if missing:
            raise HTTPException(status_code=422, detail=f"unknown mcp servers: {missing}")
    return defn.model_dump()


def validate_usecase_definition(defn: Any) -> dict:
    names = [f.name for f in defn.fields]
    if len(names) != len(set(names)):
        raise HTTPException(status_code=422, detail="duplicate field names")
    for f in defn.fields:
        if f.type == "select" and not [o for o in (f.options or []) if o.strip()]:
            raise HTTPException(
                status_code=422, detail=f"select field '{f.name}' needs options"
            )
    if defn.model is not None and defn.model not in MODELS:
        raise HTTPException(status_code=422, detail=f"unknown model: {defn.model}")
    if defn.fields and not any(f"{{{{{n}}}}}" in defn.template for n in names):
        raise HTTPException(
            status_code=422, detail="template references no defined field"
        )
    return defn.model_dump()
