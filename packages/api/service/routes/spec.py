"""OpenAPI 仕様の公開ルート(API-01)。

**なぜ `/api/` 配下か**: FastAPI 既定の `/openapi.json` は API Gateway が CI へ通していない
(実測 2026-08-04: SPA の Object Storage バックエンドへ落ちて `ObjectNotFound`)。
キャッチオール `/api/{p*}` にそのまま乗る `/api/openapi.json` に置くことで、
**Terraform 変更なし**に外から取得できる(`runs/.../e2e/scenario-1-gateway-routing.md`)。

**なぜ FastAPI 既定の口を閉じるか**(main.py で `openapi_url=None` / `docs_url=None`):
仕様を返す経路をこの 1 本に限れば、認証が要る配備(`AUTH_REQUIRED=true`)で
**仕様だけ無認証で晒す経路が残らない**(fail-closed)。ここは他の `/api/*` と同じ
`require_user` を通す。公開配備で仕様を無認証にしてよいかの既定は ADR-0028(人間判断待ち)。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from jetuse_core.auth import AuthContext, require_user

from ..openapi_errors import error_responses_as_model

router = APIRouter()


@router.get(
    "/api/openapi.json",
    summary="この API の OpenAPI 仕様(機械可読)",
    # 401 の宣言は全 router 共通で入るが、ここだけ `model=` 形にして
    # `ErrorResponse` を components に登録する錨にする(理由は error_responses_as_model)。
    responses=error_responses_as_model(401),
)
async def openapi_spec(
    request: Request, user: Annotated[AuthContext, Depends(require_user)]
) -> dict:
    """OpenAPI 3.1 の仕様そのもの。クライアント生成器にそのまま食わせる想定(JetUse は
    SDK を配布しない — 契約が動いている間は生成のほうが安全)。

    用途・使いどころは `GET /api/capabilities`(能力登録簿)側が持つ。こちらは
    ワイヤ契約(path/method/スキーマ)の正本で、登録簿はここから断片を導出する。
    """
    return request.app.openapi()
