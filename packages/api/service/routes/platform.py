"""実 Platform API ルート(PAPI-03 / ADR-0014 §13 / specs/16-platform.md §13.5)。

L2 コネクタ・L3 ホスト型アプリ・生成デモが **DB 認証情報を持たずに**テナントデータへ到達する
唯一の正規経路。各エンドポイントは冒頭で **broker トークン**(プラグインが提示する短期 JWT)を
`platform_broker.authorize(token, required_scope, tenant=...)` に通す:

    JWT 検証(fail-closed) → scope 強制 → テナント一致 → 監査(ALLOW/DENY)

を一括で行い、**通過した範囲でだけ**既存エンジンへ委譲する。OIDC のユーザトークン(`require_user`)
とは**別系統**であることに注意: Platform API の呼び出し元はエンドユーザではなくプラグインで、
提示するのは broker が発行した短期トークン(ADR-0014 §2)。検証鍵は常にブローカー側が持つ。

責務境界(本タスク=PAPI-03):
  - `db.query`: **読取限定**で `nl2sql.execute_readonly` へ委譲(非 SELECT は 400 に倒れる)。
  - `connector.invoke`: **配管まで**(authorize＋インストール済みコネクタ/action の存在検証まで。
    実 MCP 呼び出しは CON-02/03 → 501)。
  - `rag.search`: authorize 配管まで(本格ベクトル検索＝OCI Responses file_search 委譲は後続 → 501)。

セキュリティ姿勢: **fail-closed**。broker の拒否(`BrokerDenied`)・鍵未設定(`BrokerConfigError`)を
HTTP ステータスへ機械的に写像し、認可を曖昧なまま素通りさせない。
"""

import asyncio
import logging
from typing import Annotated, Any

import oracledb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from jetuse_core import nl2sql
from jetuse_core import platform_broker as pb
from jetuse_core.plugins import connector_store
from jetuse_core.plugins.manifest import (
    PLATFORM_SCOPE_CONNECTOR_INVOKE,
    PLATFORM_SCOPE_DB_QUERY,
    PLATFORM_SCOPE_RAG_SEARCH,
)
from jetuse_core.settings import Settings, get_settings

logger = logging.getLogger("jetuse.service")
router = APIRouter(prefix="/platform")

# broker トークンは OIDC ユーザトークンとは別系統。auto_error=False にして「トークン欠如」も
# 自前で 401 に倒す(authorize 入口の fail-closed と扱いを揃える)。
_broker_bearer = HTTPBearer(auto_error=False)

# BrokerDenied.reason → HTTP ステータスの写像。
#   - トークン自体が不正/偽造(検証で落ちる) → 401(提示し直すべき)。
#   - 認可不足(scope/テナント越境) → 403(本人は確かだが権限が無い)。
# ここに無い reason は安全側に 403 へ倒す(未知の拒否を 200 にしない)。
_UNAUTHORIZED_REASONS = frozenset(
    {
        "invalid_token",
        "missing_tenant",
        "missing_plugin",
        "missing_jti",
        "empty_scope",
        "unknown_scope",
    }
)
_FORBIDDEN_REASONS = frozenset({"scope_denied", "tenant_mismatch"})


def _authorize(
    creds: HTTPAuthorizationCredentials | None,
    required_scope: str,
    *,
    tenant: str,
    settings: Settings,
    resource: str = "",
) -> pb.BrokerContext:
    """broker トークンを検証・認可し、`BrokerContext` を返す。失敗は HTTP 例外に写像する。

    `platform_broker.authorize` が検証 → scope 強制 → テナント一致 → 監査(ALLOW/DENY)を行う。
    拒否(`BrokerDenied`)・鍵未設定(`BrokerConfigError`)はここで HTTP ステータスへ倒す
    (fail-closed: 認可の曖昧さを 200 で素通りさせない)。
    """
    # トークン欠如も「空トークン」として authorize に通す。verify_broker_token が空文字列を
    # invalid_token に倒し、DENY を `platform_broker_audit` に残す(「全アクセス ALLOW/DENY 監査」
    # を欠如ケースでも守る。検証以前に短絡しない)。
    token = creds.credentials if creds else ""
    try:
        return pb.authorize(
            token,
            required_scope,
            tenant=tenant,
            resource=resource,
            settings=settings,
        )
    except pb.BrokerConfigError as e:
        # ブローカー鍵未設定 = サービス側の設定不備。発行も検証もできない(503)。
        logger.error("platform api: broker unconfigured: %s", e)
        raise HTTPException(status_code=503, detail="platform broker unconfigured") from e
    except pb.BrokerDenied as e:
        if e.reason in _UNAUTHORIZED_REASONS:
            raise HTTPException(
                status_code=401,
                detail=f"invalid broker token: {e.reason}",
                headers={"WWW-Authenticate": "Bearer"},
            ) from e
        # scope_denied / tenant_mismatch / その他の拒否はすべて 403(認可不足)。
        raise HTTPException(status_code=403, detail=f"forbidden: {e.reason}") from e


def _require_tenant(tenant: str) -> str:
    """要求リソースのテナント(Project OCID)を受け取り、空を弾く(テナント境界の前提)。"""
    cleaned = (tenant or "").strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="tenant (Project OCID) is required")
    return cleaned


# --- db.query: 読取限定で既存エンジンへ委譲 ----------------------------------


class DbQueryRequest(BaseModel):
    tenant: str = Field(min_length=1, description="要求リソースのテナント(Project OCID)")
    sql: str = Field(min_length=1, description="読取専用 SQL(SELECT のみ)")


@router.post("/db/query")
async def platform_db_query(
    req: DbQueryRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_broker_bearer)],
) -> dict[str, Any]:
    """テナントデータへの**読取限定**クエリ(scope `platform:db.query`)。

    authorize 通過後、`nl2sql.execute_readonly` へ委譲する。同関数は `sanitize_sql` で非 SELECT
    (INSERT/UPDATE/DELETE/DDL・DB リンク・危険関数)を拒否するため、**書込は到達しない**
    (read-only の強制点はエンジン側に一元化し、ルートで二重実装しない)。

    注: テナント→物理スキーマの本格ルーティング(per-tenant の DB 隔離実体)は INFRA 範囲(本タスクの
    非ゴール)。本ルートは broker のテナント境界(token.tenant == 要求 tenant)を強制し、読取は
    JETUSE_QUERY(読取専用)の解決スキーマに対して行う。`current_schema` 固定による物理隔離は
    テナント→スキーマ登録簿の導入時(INFRA)に上乗せする。
    """
    tenant = _require_tenant(req.tenant)
    _authorize(
        creds,
        PLATFORM_SCOPE_DB_QUERY,
        tenant=tenant,
        settings=settings,
        resource="db.query",
    )
    try:
        return await asyncio.to_thread(nl2sql.execute_readonly, req.sql)
    except nl2sql.SqlRejectedError as e:
        # 非 SELECT・隔離跨ぎ等は読取限定違反として 400(書込は実行されない)。
        raise HTTPException(status_code=400, detail=str(e)) from e
    except oracledb.DatabaseError as e:
        # 存在しない表・権限不足・構文エラー等はクエリ起因の 400。接続系(DPY-)だけは
        # グローバル handler の 503(DB 障害)へ委ねる(/api/dbchat/execute と同方針)。
        msg = str(e).splitlines()[0][:300]
        if "DPY-" in msg:
            raise
        raise HTTPException(status_code=400, detail=f"SQL実行エラー: {msg}") from e


# --- connector.invoke: 配管まで(実 MCP 呼び出しは CON-02/03) ------------------


class ConnectorInvokeRequest(BaseModel):
    tenant: str = Field(min_length=1, description="要求リソースのテナント(Project OCID)")
    connector_id: str = Field(min_length=1, description="インストール済みコネクタの instance id")
    action: str = Field(min_length=1, description="コネクタが公開する action 名")
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/connector/invoke")
async def platform_connector_invoke(
    req: ConnectorInvokeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_broker_bearer)],
) -> dict[str, Any]:
    """コネクタ action 呼び出し(scope `platform:connector.invoke`)。**配管まで**。

    authorize(JWT 検証 → scope → テナント → 監査)とインストール済みコネクタ/action の存在検証
    までを本実装と同一に行う。**実 MCP 呼び出し(Responses API type:"mcp")は CON-02/03** なので、
    存在検証を通っても実行は 501 に倒す(認可・配管は通り、実体だけが未実装であることを明示する)。
    """
    tenant = _require_tenant(req.tenant)
    ctx = _authorize(
        creds,
        PLATFORM_SCOPE_CONNECTOR_INVOKE,
        tenant=tenant,
        settings=settings,
        resource=f"connector.invoke:{req.connector_id}",
    )
    record = await asyncio.to_thread(connector_store.get_connector, req.connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="connector not found")
    # コネクタはそれを宣言したプラグインに紐づく。トークンの sub(plugin_id)と一致しない instance を
    # 別プラグインが叩けないようにする(connector_instances は tenant 列を持たないため、認可された
    # プラグインへの所属を境界にする。tenant 単位の物理隔離は INFRA で上乗せ)。
    if record.get("plugin_id") != ctx.plugin_id:
        raise HTTPException(
            status_code=403,
            detail="connector does not belong to the authorized plugin",
        )
    definition = record.get("definition") or {}
    actions = {a.get("name") for a in definition.get("actions", []) if isinstance(a, dict)}
    if req.action not in actions:
        raise HTTPException(
            status_code=404,
            detail=f"action '{req.action}' is not defined on connector {req.connector_id}",
        )
    # 認可・配管は通った。実 MCP 呼び出しは後続(CON-02/03)。
    raise HTTPException(
        status_code=501,
        detail="connector.invoke is wired (authorized); MCP execution lands in CON-02/03",
    )


# --- rag.search: authorize 配管まで(本格ベクトル検索は後続) ------------------


class RagSearchRequest(BaseModel):
    tenant: str = Field(min_length=1, description="要求リソースのテナント(Project OCID)")
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)


@router.post("/rag/search")
async def platform_rag_search(
    req: RagSearchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_broker_bearer)],
) -> dict[str, Any]:
    """テナント文書のセマンティック検索(scope `platform:rag.search`)。**配管まで**。

    authorize(JWT 検証 → scope → テナント → 監査)を本実装と同一に行う。本格ベクトル検索は
    OCI Responses の file_search 委譲(非決定的・課金あり)で後続実装するため、authorize 通過後は
    501 に倒す(認可・配管は通り、検索実体だけが未実装であることを明示する)。
    """
    tenant = _require_tenant(req.tenant)
    _authorize(
        creds,
        PLATFORM_SCOPE_RAG_SEARCH,
        tenant=tenant,
        settings=settings,
        resource="rag.search",
    )
    raise HTTPException(
        status_code=501,
        detail="rag.search is wired (authorized) but vector search delegation is a follow-up",
    )
