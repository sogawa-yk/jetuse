"""CI用FastAPI(SSE系 — ADR-0003/0005)。起動: uvicorn service.main:app

ルートは service/routes/ 配下の APIRouter に分割(P1c §5)。本モジュールは
create_app() で router を include し `app` を公開する薄い組み立て層。

注意: 以下の jetuse_core モジュール群は本モジュールから直接は使わないものを含むが、
tests が `service.main.<module>` を monkeypatch する(routes 側と同一モジュール
オブジェクトを参照させる)ため import を維持する。
"""

import logging
import time

import oracledb
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# tests の monkeypatch アンカー(service.main.<module>)。routes と同一モジュール。
from jetuse_core import (  # noqa: F401
    agents as agents_repo,
)
from jetuse_core import (  # noqa: F401
    audit,
    datasets,
    docunderstand,
    guardrails,
    hosted_agent,
    moderation,
    nl2sql,
    rag,
    rag_opensearch,
    rag_select_ai,
    select_ai_agent,
    stt_realtime,
    translate,
    tts,
)
from jetuse_core import (  # noqa: F401
    conversations as conv_repo,
)
from jetuse_core import (  # noqa: F401
    mcp_servers as mcp_repo,
)
from jetuse_core import (  # noqa: F401
    minutes as minutes_repo,
)
from jetuse_core import (  # noqa: F401
    presets as preset_repo,
)
from jetuse_core import (  # noqa: F401
    tools as tool_registry,
)
from jetuse_core import (  # noqa: F401
    usecases as uc_repo,
)

# tests が `service.main.<fn>` を直接 monkeypatch する LLM/会話関数。routes は
# 呼び出し時に `service.main` 経由で解決するため、ここに名前を保持する必要がある。
from jetuse_core.chat import (  # noqa: F401
    delete_oci_conversation,
    stream_agent,
    stream_chat,
)
from jetuse_core.logging import configure, log_with
from jetuse_core.settings import get_settings

from .routes import (
    admin,
    agents,
    catalog,
    chat,
    conversations,
    dbchat,
    minutes,
    usecases,
    voice,
)
from .routes import rag as rag_routes

logger = logging.getLogger("jetuse.service")


def create_app() -> FastAPI:
    settings = get_settings()
    configure(settings.log_level)
    app = FastAPI(title="JetUse OCI API", version="0.1.0")

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        log_with(
            logger,
            logging.INFO,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response

    @app.exception_handler(oracledb.Error)
    async def db_unavailable(request: Request, exc: oracledb.Error):
        """DB停止・タイムアウトはハングさせず503即時返却(CHAT-07)"""
        log_with(logger, logging.ERROR, "database unavailable",
                 path=request.url.path, error=str(exc).splitlines()[0][:200])
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # route 群(P1c §5)。path/method/status は分割前と同一。
    app.include_router(chat.router)
    app.include_router(admin.router)
    app.include_router(conversations.router)
    app.include_router(agents.router)
    app.include_router(dbchat.router)
    app.include_router(rag_routes.router)
    app.include_router(minutes.router)
    app.include_router(voice.router)
    app.include_router(usecases.router)
    app.include_router(catalog.router)

    return app


app = create_app()
