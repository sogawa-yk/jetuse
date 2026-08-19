"""映像の登録・一覧・詳細・削除・再生 URL(VID-01 / specs/20 §2)。

分析・検索・場面編集は後続タスク。ここは映像を預かって取り出せるところまで。
"""

import asyncio
import logging
import pathlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from jetuse_core import video as video_repo
from jetuse_core.auth import AuthContext, require_user

from ..deps import require_video

logger = logging.getLogger("jetuse.service")
router = APIRouter()

_ALLOWED_HINT = "/".join(sorted(e.lstrip(".") for e in video_repo.ALLOWED_EXTENSIONS))


def _parse_captured_at(raw: str | None) -> datetime | None:
    """撮影日時。**読めない値を黙って NULL にしない**(specs/20 §1)。

    NULL は「不明」を表す値であって、入力ミスの受け皿ではない。読めなければ 422 で
    返し、利用者に直させる。オフセット付き("Z" / "+09:00")は UTC へ寄せる
    (保存先が TIMESTAMP = タイムゾーン無しのため。`video.to_utc_naive`)。
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail=f"captured_at must be ISO-8601: {raw[:40]}"
        ) from e
    return video_repo.to_utc_naive(parsed)


def _parse_duration_ms(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="duration_ms must be an integer") from e
    if value < 0:
        raise HTTPException(status_code=422, detail="duration_ms must not be negative")
    return value


async def _check_upload(file: UploadFile) -> tuple[str, int]:
    """拡張子・サイズ・空を検査し、本文は**読まずに**名前とサイズだけ返す。

    本文はストリームのまま Object Storage へ渡す(映像 1 本をメモリに載せない)。
    """
    name = pathlib.Path(file.filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in video_repo.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported video type '{ext}'. allowed: {_ALLOWED_HINT}",
        )
    size = file.size
    if size is None:  # 稀に size を持たないクライアント。末尾へ seek して測る
        size = await asyncio.to_thread(file.file.seek, 0, 2)
        await asyncio.to_thread(file.file.seek, 0)
    if size > video_repo.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 500MB)")
    if not size:
        raise HTTPException(status_code=422, detail="empty file")
    return name, size


@router.post("/api/video/assets")
async def create_video_asset(
    file: UploadFile,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
    title: Annotated[str | None, Form()] = None,
    collection: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
    rights: Annotated[str | None, Form()] = None,
    captured_at: Annotated[str | None, Form()] = None,
    duration_ms: Annotated[str | None, Form()] = None,
):
    """1 リクエスト 1 本。複数件は画面側が順に投げる(specs/20 §2)。"""
    captured = _parse_captured_at(captured_at)
    duration = _parse_duration_ms(duration_ms)
    name, size = await _check_upload(file)
    await asyncio.to_thread(file.file.seek, 0)
    return await asyncio.to_thread(
        lambda: video_repo.create_asset(
            user.subject, name, file.file, size,
            title=title, collection=collection, category=category, rights=rights,
            captured_at=captured, duration_ms=duration,
        )
    )


@router.get("/api/video/assets")
async def list_video_assets(
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
    limit: int = 50,
    offset: int = 0,
):
    assets = await asyncio.to_thread(video_repo.list_assets, user.subject, limit, offset)
    return {"assets": assets}


@router.get("/api/video/assets/{asset_id}")
async def get_video_asset(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    asset = await asyncio.to_thread(video_repo.get_asset, user.subject, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="video asset not found")
    return asset


@router.get("/api/video/assets/{asset_id}/playback")
async def get_video_playback(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """期限付きの再生 URL(PAR)。API は映像を中継しない。"""
    res = await asyncio.to_thread(video_repo.playback_url, user.subject, asset_id)
    if res is None:
        raise HTTPException(status_code=404, detail="video asset not found")
    return res


@router.delete("/api/video/assets/{asset_id}")
async def delete_video_asset(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    if not await asyncio.to_thread(video_repo.delete_asset, user.subject, asset_id):
        raise HTTPException(status_code=404, detail="video asset not found")
    return {"deleted": True}
