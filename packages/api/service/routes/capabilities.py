"""能力カタログルート(SP1-01)。specs/17 §3「案1」= 自動 OpenAPI + 手書きディスクリプタ。

**OpenAPI との役割分担(API-01。二重管理にしない)**: ワイヤ契約(path/method/スキーマ/
パラメータの説明)の正本は OpenAPI(`GET /api/openapi.json`)で、ここはそこから
`requestBody`/`responses` を**導出**する。登録簿が独自に持つのは「用途」側の情報
(capability / summary / when_to_use / example / demo_safe / 能力差)だけ。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from jetuse_core.auth import AuthContext, require_user
from jetuse_core.capabilities import CAPABILITIES

router = APIRouter()


@router.get("/api/capabilities")
async def list_capabilities(
    request: Request, user: Annotated[AuthContext, Depends(require_user)]
):
    """能力カタログ。出力形 {"capabilities": [{...descriptor, "openapi": {...}}]} は
    将来「案2」へ内部を差し替えても変えない安定契約(specs/17 §3)。"""
    spec_paths = request.app.openapi().get("paths", {})
    capabilities = []
    for cap in CAPABILITIES:
        fragments: dict = {}
        for route in cap["routes"]:
            op = spec_paths.get(route["path"], {}).get(route["method"], {})
            fragments.setdefault(route["path"], {})[route["method"]] = {
                k: op[k] for k in ("requestBody", "responses") if k in op
            }
        capabilities.append({**cap, "openapi": fragments})
    return {"capabilities": capabilities}
