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
from pydantic import BaseModel, Field, field_validator

from jetuse_core import nl2sql
from jetuse_core import platform_broker as pb
from jetuse_core import platform_grants as pg
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.plugins import connector_store
from jetuse_core.plugins import store as plugin_store
from jetuse_core.plugins.manifest import (
    PLATFORM_SCOPE_CONNECTOR_INVOKE,
    PLATFORM_SCOPE_DB_QUERY,
    PLATFORM_SCOPE_RAG_SEARCH,
    PLATFORM_SCOPES,
    ManifestError,
    PluginManifest,
    validate_manifest,
)
from jetuse_core.settings import Settings, get_settings

from ..deps import is_admin

logger = logging.getLogger("jetuse.service")
router = APIRouter(prefix="/platform")

# scope 文字列 1 件の上限長。語彙(platform:*)の最長は 'platform:conversations.read'=27 文字。
# 余裕を見て 64 とし、approve 入力の各 scope 長を縛る(監査・検証前の早期境界 / fail-closed)。
MAX_SCOPE_ITEM_LEN = 64

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


# --- スコープ承認(PAPI-02 の到達経路): approve / revoke / list -----------------
#
# `platform_grants`(承認ポリシー＋永続化)へ REST から到達する唯一の経路。承認は **人間=SA の操作**
# (ADR-0014 §2 / specs/16-platform.md §13.3)であり、ここでは管理者(ADMIN_USERS)に限定する。
# 二重閉包(fail-closed): ルートが SA を強制 → `platform_grants.approve_scopes` が
# **manifest.permissions ∩ PLATFORM_SCOPES** に閉じる。範囲外スコープの承認は拒否し、誰が・いつ・
# どの scope を承認/失効したかを `platform_broker_audit`(append-only)へ刻む。


def _require_sa(user: AuthContext) -> str:
    """承認操作を管理者(SA)に限定し、承認者識別子(email 優先, 無ければ sub)を返す。

    fail-closed: 管理者でなければ 403。承認は人間操作であり、自動承認や非管理者承認を許さない。
    識別子は監査・グラント行の `approved_by` に刻むため、空白除去して非空を保証する。
    """
    if not is_admin(user):
        raise HTTPException(
            status_code=403,
            detail="scope approval is restricted to platform administrators (ADMIN_USERS)",
        )
    email = (user.claims or {}).get("email")
    actor = str(email or user.subject or "").strip()
    if not actor:
        raise HTTPException(status_code=422, detail="approver identity is required")
    return actor


def _audit_grant(
    decision: str,
    *,
    tenant: str,
    plugin_id: str,
    scopes: list[str],
    approved_by: str,
    reason: str = "",
) -> None:
    """承認/失効/拒否を `platform_broker_audit` に append-only で刻む(誰が・いつ・どの scope)。

    `created_at` が「いつ」、`resource_id` の approved_by が「誰が」、`scope` が「どの scope」、
    `decision`(APPROVE / REVOKE / DENY)が結果。scope 単位で 1 行刻むことで列幅(VARCHAR2(64))の
    切り詰めを避け、後から「あるスコープを誰が承認したか」を引けるようにする。ベストエフォート
    (record_broker_access が記録失敗を握り潰す)だが、越境試行(DENY)も必ず残す方針は broker と同じ。
    """
    # 空集合(失効でグラント行が読めなかった等)でも 1 行は残す。Oracle は '' を NULL 扱いにし
    # scope は NOT NULL なのでプレースホルダを置く(監査行の欠落を避ける)。
    rows = scopes or ["(all)"]
    for s in rows:
        pb.record_broker_access(
            plugin_id=plugin_id,
            tenant=tenant,
            scope=s or "(none)",
            decision=decision,
            reason=reason,
            resource=f"grant:approved_by={approved_by}",
        )


def _load_installed_manifest(plugin_id: str, version: str | None) -> PluginManifest:
    """承認対象プラグインの**インストール済み** manifest を取り出す(承認スコープの正本)。

    version 指定があればその版固定スナップショットを、無ければ**最新版 1 件**を採る。インストール
    されていなければ 404。fail-closed: 採った 1 件が壊れている/読めない場合は 422 に倒し、**古い
    正常版へ暗黙にフォールバックしない**(最新で削除された権限を旧 manifest 基準で再承認させない。
    BE-05 review F-003)。
    """
    if version:
        rec = plugin_store.find_install(plugin_id, version)
    else:
        # list_installs は新しい順。先頭(最新版)だけを承認の正本にする(旧版へ降りない)。
        installs = plugin_store.list_installs(plugin_id)
        rec = installs[0] if installs else None
    if rec is None:
        ref = f"{plugin_id}@{version}" if version else plugin_id
        raise HTTPException(status_code=404, detail=f"installed plugin not found: {ref}")
    md = rec.get("manifest")
    if not md or rec.get("manifest_error"):
        # 最新(または指定版)の manifest が壊れている/欠落 → 旧版に降りず 422。
        raise HTTPException(
            status_code=422,
            detail="installed manifest is unreadable; cannot approve scopes",
        )
    try:
        return validate_manifest(md)
    except ManifestError as e:
        raise HTTPException(
            status_code=422, detail=f"stored manifest is invalid: {e}"
        ) from e


class GrantApproveRequest(BaseModel):
    tenant: str = Field(min_length=1, description="承認対象テナント(Project OCID)")
    plugin_id: str = Field(min_length=1, description="承認するプラグイン id")
    # scopes は platform 語彙の部分集合。件数を語彙サイズで上限し、各要素長も縛る。これにより
    # 拒否時に req.scopes を 1 件 1 行で同期監査する経路が**無制限な DB 書込**にならない(BE-05
    # review F: 監査 DoS の予防)。語彙外スコープは下流 validate_grant_scopes が fail-closed で弾く。
    scopes: list[str] = Field(
        min_length=1,
        max_length=len(PLATFORM_SCOPES),
        description="承認する platform スコープ(manifest 宣言の範囲内のみ)",
    )
    version: str | None = Field(
        default=None,
        max_length=MAX_SCOPE_ITEM_LEN,
        description="照合する manifest version(省略時は最新インストール)",
    )

    @field_validator("scopes")
    @classmethod
    def _bound_scope_items(cls, v: list[str]) -> list[str]:
        # 各 scope 文字列長を縛る(語彙は最長 'platform:conversations.read'=27。余裕を見て上限)。
        if any(len(s) > MAX_SCOPE_ITEM_LEN for s in v):
            raise ValueError(f"scope は各 {MAX_SCOPE_ITEM_LEN} 文字以内でなければならない")
        return v


class GrantRevokeRequest(BaseModel):
    tenant: str = Field(min_length=1, description="失効対象テナント(Project OCID)")
    plugin_id: str = Field(min_length=1, description="失効するプラグイン id")


@router.get("/grants")
async def platform_list_grants(
    user: Annotated[AuthContext, Depends(require_user)],
    tenant: str | None = None,
    plugin_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """承認グラントを新しい順に一覧する(承認 UI のレビュー用。SA 限定)。"""
    _require_sa(user)
    grants = await asyncio.to_thread(pg.list_grants, tenant, plugin_id, status=status)
    return {"grants": grants}


@router.get("/grants/candidates")
async def platform_grant_candidates(
    user: Annotated[AuthContext, Depends(require_user)],
) -> dict[str, Any]:
    """承認可能な候補(インストール済みプラグイン × 宣言済み platform スコープ)を返す。

    承認 UI が「プロジェクト×scope のレビュー→承認」フォームを組むための入力。plugin_id ごとに
    最新版 1 件へ畳み、`permissions` のうち PLATFORM_SCOPES に属するものだけを承認可能スコープとして
    出す(語彙外は承認経路に乗せない)。platform スコープを宣言しないプラグインは候補から外す。
    """
    _require_sa(user)
    installs = await asyncio.to_thread(plugin_store.list_installs, None)
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for r in installs:  # 新しい順 = 最初に来た版が最新
        pid = r.get("plugin_id")
        if not pid or pid in seen:
            continue
        # 最新版だけを候補判定の対象にする。最新が壊れている/scope 未宣言ならこの plugin は候補外。
        # 旧版の宣言スコープへ降りない(approve 側の fail-closed と揃える。BE-05 review F-003)。
        seen.add(pid)
        md = r.get("manifest")
        # JSON として読めても形が manifest とは限らない(壊れた CLOB が list/str で返る等)。
        # dict/list でなければ候補外(fail-closed。md.get で 500 にしない)。
        if not isinstance(md, dict) or r.get("manifest_error"):
            continue
        perms = md.get("permissions")
        if not isinstance(perms, list):
            continue
        declared = sorted(s for s in perms if s in PLATFORM_SCOPES)
        if not declared:
            continue
        candidates.append(
            {
                "plugin_id": pid,
                "version": r.get("version"),
                "name": md.get("name"),
                "declared_scopes": declared,
            }
        )
    return {"candidates": candidates}


@router.post("/grants")
async def platform_approve_grant(
    req: GrantApproveRequest,
    user: Annotated[AuthContext, Depends(require_user)],
) -> dict[str, Any]:
    """manifest 宣言の範囲内でテナントにスコープを承認する(SA 限定 / fail-closed)。

    承認スコープは `platform_grants.approve_scopes` が **manifest.permissions ∩ PLATFORM_SCOPES** に
    閉じる(二重閉包)。範囲外・未知・非宣言スコープは 422 で拒否し、その拒否も監査に残す(DENY)。
    成功時はグラント記録を返し、承認(APPROVE)を監査に刻む。
    """
    approved_by = _require_sa(user)
    tenant = _require_tenant(req.tenant)
    manifest = await asyncio.to_thread(
        _load_installed_manifest, req.plugin_id, req.version
    )
    try:
        grant = await asyncio.to_thread(
            pg.approve_scopes,
            manifest,
            tenant=tenant,
            scopes=req.scopes,
            approved_by=approved_by,
        )
    except pg.GrantError as e:
        # 範囲外/未知/非宣言スコープは fail-closed で拒否。越境試行として監査に残す。
        _audit_grant(
            "DENY",
            tenant=tenant,
            plugin_id=req.plugin_id,
            scopes=list(req.scopes),
            approved_by=approved_by,
            reason=str(e)[:200],
        )
        raise HTTPException(status_code=422, detail=str(e)) from e
    _audit_grant(
        "APPROVE",
        tenant=tenant,
        plugin_id=req.plugin_id,
        scopes=grant["scopes"],
        approved_by=approved_by,
    )
    return grant


@router.delete("/grants")
async def platform_revoke_grant(
    req: GrantRevokeRequest,
    user: Annotated[AuthContext, Depends(require_user)],
) -> dict[str, Any]:
    """承認を失効させる(SA 限定)。失効した ACTIVE グラントが無ければ 404。

    失効対象のスコープを監査に残すため、失効前にグラントを読む。失効後の issue_token は
    `grant_revoked` で発行を拒否する(行は残り監査追跡可能)。
    """
    approved_by = _require_sa(user)
    tenant = _require_tenant(req.tenant)
    # 失効と「失効した scope の取得」を 1 トランザクションに閉じる(再承認との競合で監査がずれない)。
    revoked_scopes = await asyncio.to_thread(
        pg.revoke_grant_capture, tenant, req.plugin_id
    )
    if revoked_scopes is None:
        raise HTTPException(status_code=404, detail="no active grant to revoke")
    _audit_grant(
        "REVOKE",
        tenant=tenant,
        plugin_id=req.plugin_id,
        scopes=revoked_scopes,
        approved_by=approved_by,
    )
    return {"revoked": True, "tenant": tenant, "plugin_id": req.plugin_id}
