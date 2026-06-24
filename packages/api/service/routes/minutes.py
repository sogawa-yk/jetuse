"""議事録(VOICE-01): バッチ文字起こし+LLM整形ルート。"""

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from jetuse_core import audit
from jetuse_core import minutes as minutes_repo
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.models import MODELS

from ..deps import require_speech
from ..schemas import MinutesGenerateRequest
from ..sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")
router = APIRouter()


def _stream_chat(*args, **kwargs):
    # tests が `service.main.stream_chat` を monkeypatch するため main 経由で解決。
    from .. import main as svc_main
    return svc_main.stream_chat(*args, **kwargs)


@router.get("/api/minutes")
async def list_minutes(user: Annotated[AuthContext, Depends(require_user)]):
    return {"jobs": await asyncio.to_thread(minutes_repo.list_jobs, user.subject)}


@router.post("/api/minutes")
async def create_minutes(
    file: UploadFile,
    user: Annotated[AuthContext, Depends(require_user)],
    language: str = "ja",
):
    import pathlib
    import re

    require_speech()
    name = pathlib.Path(file.filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in minutes_repo.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported audio type '{ext}'. allowed: mp3/wav/m4a/ogg/webm",
        )
    if not re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2})?", language):
        raise HTTPException(status_code=422, detail="invalid language code")
    content = await file.read()
    if len(content) > minutes_repo.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 100MB)")
    if not content:
        raise HTTPException(status_code=422, detail="empty file")
    try:
        return await asyncio.to_thread(
            minutes_repo.create_job, user.subject, name, content, language
        )
    except Exception as e:
        # IAM未整備(404 NotAuthorizedOrNotFound等)を機能未開放として案内(specs/12)
        logger.exception("minutes job create failed")
        raise HTTPException(
            status_code=503,
            detail=f"文字起こしジョブを作成できません(IAM未整備の可能性): {str(e)[:200]}",
        ) from e


@router.get("/api/minutes/{mid}")
async def get_minutes(
    mid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    rec = await asyncio.to_thread(minutes_repo.get_job, user.subject, mid)
    if not rec:
        raise HTTPException(status_code=404, detail="minutes job not found")
    return rec


@router.delete("/api/minutes/{mid}")
async def delete_minutes(
    mid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    if not await asyncio.to_thread(minutes_repo.delete_job, user.subject, mid):
        raise HTTPException(status_code=404, detail="minutes job not found")
    return {"deleted": True}


@router.post("/api/minutes/{mid}/generate")
async def generate_minutes(
    mid: str,
    req: MinutesGenerateRequest,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """トランスクリプトからテンプレート文書をSSEストリーミング生成"""
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")
    rec = await asyncio.to_thread(minutes_repo.get_job, user.subject, mid)
    if not rec:
        raise HTTPException(status_code=404, detail="minutes job not found")
    if rec["status"] != "completed" or not rec["transcript"]:
        raise HTTPException(status_code=409, detail="transcription not completed")
    messages = minutes_repo.build_generation_messages(
        rec["transcript"], req.template, rec["title"]
    )

    async def gen():
        yield KEEPALIVE_FRAME
        mg_usage: dict = {}
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)

        def produce():
            upstream = None
            try:
                upstream = _stream_chat(req.model, messages, user=user.subject)
                for ev in upstream:
                    asyncio.run_coroutine_threadsafe(q.put(ev), loop).result()
            except Exception as e:
                logger.exception("minutes generate failed")
                asyncio.run_coroutine_threadsafe(
                    q.put({"error": str(e)[:300]}), loop
                ).result()
            finally:
                if upstream is not None:
                    upstream.close()
                asyncio.run_coroutine_threadsafe(q.put(None), loop).result()

        producer = loop.run_in_executor(None, produce)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield KEEPALIVE_FRAME
                    continue
                if ev is None:
                    break
                if "usage" in ev:
                    mg_usage.update(ev["usage"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            await producer
            await asyncio.to_thread(
                audit.log_event, user.subject, "minutes", req.model,
                mg_usage.get("input_tokens"), mg_usage.get("output_tokens"),
                "ok", req.template,
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
