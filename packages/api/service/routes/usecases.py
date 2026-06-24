"""ユースケースエンジン・プリセットルート(UC-01)。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from jetuse_core import presets as preset_repo
from jetuse_core import usecases as uc_repo
from jetuse_core.auth import AuthContext, require_user

from ..schemas import PresetCreate, UsecaseDefinition

router = APIRouter()


# --- ユースケースエンジン(UC-01) ---

@router.get("/api/usecases")
def list_usecases(user: Annotated[AuthContext, Depends(require_user)]):
    return {"usecases": uc_repo.list_usecases(user.subject)}


@router.post("/api/usecases")
def create_usecase(
    req: UsecaseDefinition, user: Annotated[AuthContext, Depends(require_user)]
):
    return uc_repo.create_usecase(user.subject, req.validated())


@router.get("/api/usecases/{uc_id}")
def get_usecase(uc_id: str, user: Annotated[AuthContext, Depends(require_user)]):
    uc = uc_repo.get_usecase(user.subject, uc_id)
    if not uc:
        raise HTTPException(status_code=404, detail="usecase not found")
    return {**uc, "mine": uc.get("owner_sub") == user.subject}


@router.put("/api/usecases/{uc_id}")
def update_usecase(
    uc_id: str,
    req: UsecaseDefinition,
    user: Annotated[AuthContext, Depends(require_user)],
):
    uc = uc_repo.update_usecase(user.subject, uc_id, req.validated())
    if not uc:
        raise HTTPException(status_code=404, detail="usecase not found")
    return uc


@router.delete("/api/usecases/{uc_id}")
def delete_usecase(uc_id: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not uc_repo.delete_usecase(user.subject, uc_id):
        raise HTTPException(status_code=404, detail="usecase not found")
    return {"deleted": True}


@router.get("/api/presets")
def list_presets(user: Annotated[AuthContext, Depends(require_user)]):
    return {"presets": preset_repo.list_presets(user.subject)}


@router.post("/api/presets")
def create_preset(
    req: PresetCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    return preset_repo.create_preset(user.subject, req.name, req.content)


@router.delete("/api/presets/{pid}")
def delete_preset(pid: str, user: Annotated[AuthContext, Depends(require_user)]):
    if not preset_repo.delete_preset(user.subject, pid):
        raise HTTPException(status_code=404, detail="preset not found")
    return {"deleted": True}
