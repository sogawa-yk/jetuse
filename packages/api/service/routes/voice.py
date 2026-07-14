"""音声・翻訳・OCR・URL抽出ルート(VOICE-02/03, ENH-07/10, UC-02)。"""

import asyncio
import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from jetuse_core import audit, docunderstand, stt_realtime, translate, tts
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.logging import log_with
from jetuse_core.webtools import SsrfBlockedError, extract_url

from ..schemas import ExtractUrlRequest, SttSessionCreate, TranslateRequest, TtsRequest
from ..sse import KEEPALIVE_FRAME, KEEPALIVE_SECONDS, SSE_HEADERS

logger = logging.getLogger("jetuse.service")
router = APIRouter()


# --- リアルタイム文字起こし(VOICE-02): 音声=チャンクPOST / 結果=SSE中継 ---

@router.post("/api/stt/sessions")
async def create_stt_session(
    req: SttSessionCreate, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        rec = await stt_realtime.create_session(user.subject, req.language)
        await asyncio.to_thread(
            audit.log_event, user.subject, "stt", None, None, None, "ok", req.language
        )
        return rec
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("stt session create failed")
        raise HTTPException(
            status_code=503,
            detail=f"リアルタイムセッションを開始できません(IAM未整備の可能性): {str(e)[:200]}",
        ) from e


@router.post("/api/stt/sessions/{sid}/audio")
async def stt_audio(
    sid: str, request: Request, user: Annotated[AuthContext, Depends(require_user)]
):
    chunk = await request.body()
    if not chunk:
        raise HTTPException(status_code=422, detail="empty chunk")
    if len(chunk) > stt_realtime.MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="chunk too large (max 64KB)")
    if not await stt_realtime.send_audio(user.subject, sid, chunk):
        raise HTTPException(status_code=404, detail="session not found or closed")
    return {"ok": True}


@router.get("/api/stt/sessions/{sid}/events")
async def stt_events(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    sess = stt_realtime.get_session(user.subject, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")

    async def gen():
        yield KEEPALIVE_FRAME
        while True:
            try:
                ev = await asyncio.wait_for(sess.queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                if sess.closed:
                    break
                if sess.idle_seconds > stt_realtime.SESSION_IDLE_SECONDS:
                    await stt_realtime.close_session(user.subject, sid)
                    break
                yield KEEPALIVE_FRAME
                continue
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("closed"):
                break
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.delete("/api/stt/sessions/{sid}")
async def delete_stt_session(
    sid: str, user: Annotated[AuthContext, Depends(require_user)]
):
    if not await stt_realtime.close_session(user.subject, sid):
        raise HTTPException(status_code=404, detail="session not found")
    return {"closed": True}


# --- TTS(VOICE-03): Phoenixクロスリージョン合成 ---

@router.post("/api/tts")
async def synthesize_tts(
    req: TtsRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    if req.voice not in tts.VOICES:
        raise HTTPException(
            status_code=422, detail=f"unknown voice (allowed: {', '.join(tts.VOICES)})"
        )
    try:
        audio = await asyncio.to_thread(tts.synthesize, req.text, req.voice)
        await asyncio.to_thread(
            audit.log_event, user.subject, "tts", None, len(req.text), None,
            "ok", req.voice,
        )
    except tts.TtsError as e:
        logger.warning("tts synthesize degraded: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("tts synthesize failed")
        raise HTTPException(
            status_code=503, detail=f"音声合成に失敗しました: {str(e)[:200]}"
        ) from e
    return Response(content=audio, media_type="audio/mpeg")


# --- 翻訳(ENH-10): リアルタイム文字起こしの確定テキストを逐次翻訳 ---

@router.get("/api/translate/options")
async def translate_options(user: Annotated[AuthContext, Depends(require_user)]):
    return {"languages": translate.LANGUAGES,
            "backends": [{"name": "llm", "label": "Enterprise AI (LLM)"},
                         {"name": "oci_language", "label": "OCI Language"}]}


@router.post("/api/translate")
async def do_translate(
    req: TranslateRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        out = await asyncio.to_thread(
            translate.translate, req.text, req.target,
            source=req.source, backend=req.backend,
        )
        return {"translated": out}
    except Exception as e:
        logger.exception("translate failed")
        raise HTTPException(
            status_code=502, detail=f"翻訳に失敗しました: {str(e)[:200]}"
        ) from e


# --- OCR / ドキュメント理解(ENH-07): OCI Document Understanding 同期API ---

@router.get("/api/ocr/options")
async def ocr_options(user: Annotated[AuthContext, Depends(require_user)]):
    return {"languages": docunderstand.LANGUAGES,
            "max_bytes": docunderstand.MAX_BYTES,
            "max_pages": docunderstand.MAX_SYNC_PAGES,
            "engines": docunderstand.ENGINES,
            "vlm_models": docunderstand.VLM_MODELS}


@router.post("/api/ocr")
async def do_ocr(
    user: Annotated[AuthContext, Depends(require_user)],
    file: UploadFile,
    engine: str = "document_understanding",
    language: str = "JPN",
    tables: bool = True,
    key_values: bool = False,
    model: str = docunderstand.DEFAULT_VLM_MODEL,
):
    import pathlib

    name = pathlib.Path(file.filename or "doc").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".pdf"):
        raise HTTPException(
            status_code=422, detail="対応形式: PNG/JPEG/TIFF/PDF"
        )
    content = await file.read()
    try:
        if engine == "vlm":
            result = await asyncio.to_thread(
                docunderstand.ocr_vlm, content,
                model=model, tables=tables, language=language,
            )
        else:
            result = await asyncio.to_thread(
                docunderstand.ocr, content,
                language=language, tables=tables, key_values=key_values,
            )
    except docunderstand.OcrError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("ocr failed")
        raise HTTPException(status_code=502, detail=f"OCRに失敗: {str(e)[:200]}") from e
    log_with(logger, logging.INFO, "ocr done",
             user=user.subject, pages=result["page_count"], engine=engine, name=name)
    return result


# --- ツール(UC-02): Webコンテンツ抽出。SSRF対策はwebtools側 ---

@router.post("/api/tools/extract-url")
async def extract_url_endpoint(
    req: ExtractUrlRequest, user: Annotated[AuthContext, Depends(require_user)]
):
    try:
        result = await asyncio.to_thread(extract_url, req.url)
    except SsrfBlockedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"fetch failed: {e}") from e
    if not result["text"]:
        raise HTTPException(status_code=422, detail="no text content extracted")
    return result
