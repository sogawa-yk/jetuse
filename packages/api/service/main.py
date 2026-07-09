"""CI用FastAPI(SSE系 — ADR-0003/0005)。起動: uvicorn service.main:app

ルートは service/routes/ 配下の APIRouter に分割(P1c §5)。本モジュールは
create_app() で router を include し `app` を公開する薄い組み立て層。

注意: 以下の jetuse_core モジュール群は本モジュールから直接は使わないものを含むが、
tests が `service.main.<module>` を monkeypatch する(routes 側と同一モジュール
オブジェクトを参照させる)ため import を維持する。
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

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
    builder,
    capabilities,
    chat,
    conversations,
    dbchat,
    demos,
    minutes,
    usecases,
    voice,
)
from .routes import rag as rag_routes

logger = logging.getLogger("jetuse.service")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """SP3-08(ADR-0023 §4): oci-ci runtime では reconcile を起動時 + 定期(5 分)で回す。

    API 再起動でウォッチが消えた provisioning・孤児 jetuse-builder-* CI・残置ジョブ
    オブジェクトを回収する。reconcile 自体は各段 best-effort(例外を伝播させない)。
    """
    task = None
    if get_settings().generation_runtime == "oci-ci":
        from jetuse_core import generation_runtime_ci

        async def _loop() -> None:
            while True:
                await asyncio.to_thread(generation_runtime_ci.reconcile)
                await asyncio.sleep(300)

        task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    configure(settings.log_level)
    app = FastAPI(title="JetUse OCI API", version="0.1.0", lifespan=_lifespan)

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

    # SP2-02(specs/18): fail-closed ゲート・リース・上限の型付き例外を HTTP に正規化
    def _json_handler(status: int, detail: str):
        async def handler(request: Request, exc: Exception):
            log_with(logger, logging.WARNING, "request rejected",
                     path=request.url.path, status=status,
                     error=str(exc).splitlines()[0][:200])
            return JSONResponse(status_code=status, content={"detail": detail})

        return handler

    from jetuse_core.demo_lease import (
        DemoGoneError,
        LeaseTimeoutError,
        LeaseUnavailableError,
    )
    from jetuse_core.owner_keys import OwnerKeyPreflightError
    from jetuse_core.rag_ledger import QuotaExceededError, UnmanagedFilesError
    from jetuse_core.vpd import DatasetsSecurityError

    app.add_exception_handler(
        DatasetsSecurityError,
        _json_handler(503, "datasets security boundary incomplete (fail-closed)"))
    app.add_exception_handler(
        OwnerKeyPreflightError,
        _json_handler(503, "owner key migration pending (fail-closed)"))
    app.add_exception_handler(
        UnmanagedFilesError,
        _json_handler(503, "unmanaged files detected (fail-closed)"))
    app.add_exception_handler(
        LeaseUnavailableError,
        _json_handler(503, "demo lease unavailable (fail-closed)"))
    app.add_exception_handler(
        LeaseTimeoutError, _json_handler(503, "demo busy, retry later"))
    app.add_exception_handler(
        DemoGoneError, _json_handler(404, "demo not found"))
    app.add_exception_handler(
        QuotaExceededError, _json_handler(422, "file quota exceeded"))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/health")
    async def api_health():
        # gateway は /api/* しか CI へルートしない — デプロイ smoke の契約(SP3-07)
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
    app.include_router(capabilities.router)
    app.include_router(demos.router)
    app.include_router(demos.crud_router)  # Demo CRUD(SP2-01 / specs/18 §2)
    app.include_router(builder.router)  # ビルダー・ヒアリング(SP3-01 / specs/19 §2)

    # 生成用署名プロキシ(SP3-07 配備像: API プロセス内 mount)。gateway は /api/* と SPA しか
    # ルートしないため公開されない。VCN 内(SP3-08 の生成 CI)からは :8000/gen-proxy/v1 で到達。
    from jetuse_core import sign_proxy

    app.mount("/gen-proxy", sign_proxy.app)

    return app


app = create_app()
