"""Reference Implementation Catalog の読取ルート(EXB-02)。

実装方針 §7.1 の最小版。静的 Descriptor を in-process ローダーから返すだけ
(サービス化・永続化はしない)。Builder/コーディングエージェントが利用可能な実装を発見する。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core.auth import AuthContext, require_user
from jetuse_platform.reference_descriptors import catalog

router = APIRouter()


@router.get("/api/v1/catalog/capabilities")
def list_capabilities(user: Annotated[AuthContext, Depends(require_user)]):
    return {"capabilities": catalog.list_capabilities()}


@router.get("/api/v1/catalog/capabilities/{capability_id}/versions/{version}")
def get_capability(
    capability_id: str,
    version: str,
    user: Annotated[AuthContext, Depends(require_user)],
):
    try:
        return catalog.get_capability(capability_id, version)
    except KeyError:
        raise HTTPException(status_code=404, detail="capability not found") from None
