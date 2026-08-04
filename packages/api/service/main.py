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
    http_tools as http_tools_repo,
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

from .openapi_errors import error_responses
from .routes import (
    admin,
    agents,
    capabilities,
    chat,
    conversations,
    dbchat,
    demos,
    health,
    minutes,
    spec,
    usecases,
    voice,
)
from .routes import rag as rag_routes

logger = logging.getLogger("jetuse.service")

# API-01: 外部(デモ作成者)が最初に読む文章。SDK は配布しないので、**この仕様が入口**。
API_DESCRIPTION = """\
JetUse の HTTP API。**SDK は配布しない**ので、この仕様からクライアントを生成して使う
(契約がまだ動いているため、古い SDK が動いてしまう状態を作らない方針)。

**取得**: `GET /api/openapi.json`。認証が要る配備(`AUTH_REQUIRED=true`)では、仕様の取得にも
他の `/api/*` と同じ Bearer 認証が要る(仕様だけ無認証で晒さない)。

**認証トークンの入手**: この API に**トークンを発行する口は無い**(推測でエンドポイントを
組み立てないこと)。`AUTH_REQUIRED=true` の配備では OIDC(OCI IAM Identity Domain)が発行した
Bearer トークンを `authorization: Bearer <token>` で送る。発行元・client・scope は配備ごとに
異なるので**配備した側から受け取る**。`AUTH_REQUIRED=false` の配備(開発用)では認証は不要で、
すべての呼び出しが単一の開発ユーザーとして扱われる。

**能力から入る**: 「何ができるか / いつ使うか / 入力例」は `GET /api/capabilities`(能力登録簿)が
正本。登録簿は各能力のルートの `requestBody` / `responses` を**この OpenAPI から導出**して返す
(同じことを二重に持たない)。**ワイヤ契約の正はこの仕様・用途の説明の正は登録簿**。

**つまずきやすい組み合わせ**は該当ルート/パラメータの説明に書いてある。特に
`POST /api/chat/stream` の説明(排他・依存・似た名前のパラメータ)を先に読むこと。
各パラメータには「いつ使うか」も書いてある(型だけでは使い分けが決まらないため)。
リクエストの**例**は `ChatRequest` の `examples` にある。

**エラーからの自己修正**(コーディングエージェント向け): エラー応答は基本 `{"detail": "<理由>"}` の
形で、`detail` はそのまま直し方の手掛かりになる文になっている。**`422` だけ `detail` が 2 通り**:
スキーマ検証で落ちた場合は項目ごとの**配列**(`loc` / `msg` / `type`)、ルート側の検証で落ちた場合は
他と同じ**文字列**。`422` を扱うコードは**両方を受けられるように書くこと**。コードの意味は
固定で、`400`=組み合わせが不正(直して再送) / `401`=認証が必要(トークンを付ける。
`AUTH_REQUIRED=true` の配備だけ) / `404`=資源が無い(他人所有も 404) /
`409`=状態が合わない(前段をやり直す) / `413`=サイズ超過(分割する) /
`422`=入力の検証エラー(`loc` を見る) / `502`=上流(OCI 側)がエラーを返した(設定を確認し、
一時的な失敗なら再試行) / `503`=一時的な障害(同じ内容で再試行してよい)。
各ルートの `responses` に「このルートでは具体的にどういうときに返るか」がある。

**SSE の読み方**: ストリーミング系(`/api/chat/stream` 等)は `text/event-stream` で
`data: {...}` を逐次返し、`data: [DONE]` で終端する。フレームは `{"delta": "..."}`(本文) /
`{"citations": [...]}`(出典) / `{"error": "..."}`(途中失敗) / `{"ka": 1}`(キープアライブ)。
**HTTP は 200 のまま本文中でエラーになる場合がある**ので `error` フレームも見ること。
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure(settings.log_level)
    # openapi_url/docs_url を既定(`/openapi.json` `/docs`)から外すのは API-01。
    # 仕様を返す口を routes/spec.py の 1 本(認証あり)に限るため = fail-closed。
    app = FastAPI(
        title="JetUse OCI API",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

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
    # 401 は**全 router に共通で宣言する**（`/healthz` を除く全ルートが require_user を通る。
    # 宣言が漏れると `AUTH_REQUIRED=true` の配備で生成クライアントが型のない例外になる
    # — review-9 F-002）。
    auth = error_responses(401)
    app.include_router(chat.router, responses=auth)
    app.include_router(admin.router, responses=auth)
    app.include_router(conversations.router, responses=auth)
    app.include_router(agents.router, responses=auth)
    app.include_router(dbchat.router, responses=auth)
    app.include_router(rag_routes.router, responses=auth)
    app.include_router(minutes.router, responses=auth)
    app.include_router(voice.router, responses=auth)
    app.include_router(usecases.router, responses=auth)
    app.include_router(capabilities.router, responses=auth)
    app.include_router(spec.router, responses=auth)
    app.include_router(demos.router, responses=auth)
    app.include_router(health.router, responses=auth)

    return app


app = create_app()
