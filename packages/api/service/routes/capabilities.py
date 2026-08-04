"""能力カタログルート(SP1-01)。specs/17 §3「案1」= 自動 OpenAPI + 手書きディスクリプタ。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from jetuse_core.auth import AuthContext, require_user
from jetuse_core.capabilities import CAPABILITIES

router = APIRouter()


def build_catalog(spec_paths: dict) -> list[dict]:
    """カタログ生成の実体。GET /api/capabilities とビルダーのデモ設計(specs/19 §3.1)が
    同じ生成関数を共用する — 語彙・カタログ内容の単一真実源は CAPABILITIES 登録簿。"""
    capabilities = []
    for cap in CAPABILITIES:
        fragments: dict = {}
        for route in cap["routes"]:
            op = spec_paths.get(route["path"], {}).get(route["method"], {})
            fragments.setdefault(route["path"], {})[route["method"]] = {
                k: op[k] for k in ("requestBody", "responses") if k in op
            }
        capabilities.append({**cap, "openapi": fragments})
    return capabilities


@router.get("/api/capabilities")
async def list_capabilities(
    request: Request, user: Annotated[AuthContext, Depends(require_user)]
):
    """能力カタログ。出力形 {"capabilities": [{...descriptor, "openapi": {...}}]} は
    将来「案2」へ内部を差し替えても変えない安定契約(specs/17 §3)。"""
    return {"capabilities": build_catalog(request.app.openapi().get("paths", {}))}
