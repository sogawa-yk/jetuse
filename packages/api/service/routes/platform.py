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
  - `connector.invoke`: authorize＋コネクタ/action 存在検証＋所属境界の後、`connector_runtime.
    invoke_connector_action` へ委譲して**実 invoke**する(builtin=実 HTTP/httpx・secret=Vault 解決。
    BE-03)。実 Slack 認証・実 Vault 読取 IAM は人間ゲート(mock E2E で投稿フローを検証)。
  - `rag.search`: authorize 後、broker 検証済みテナントをストア所有者キーにして本体がベクトル
    ストアを解決し、**OCI Responses file_search** へ委譲(ヒット＋引用＋根拠付き回答を返す)。

セキュリティ姿勢: **fail-closed**。broker の拒否(`BrokerDenied`)・鍵未設定(`BrokerConfigError`)を
HTTP ステータスへ機械的に写像し、認可を曖昧なまま素通りさせない。
"""

import asyncio
import hashlib
import logging
import re
from typing import Annotated, Any

import oracledb
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from jetuse_core import nl2sql, rag
from jetuse_core import platform_broker as pb
from jetuse_core import platform_grants as pg
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.plugins import connector_runtime, connector_store
from jetuse_core.plugins import store as plugin_store
from jetuse_core.plugins.connector_runtime import (
    ConnectorInvokeDenied,
    ConnectorInvokeError,
    ConnectorTransportError,
    SecretResolutionError,
    invoke_connector_action,
)
from jetuse_core.plugins.manifest import (
    PLATFORM_SCOPE_CONNECTOR_INVOKE,
    PLATFORM_SCOPE_DB_QUERY,
    PLATFORM_SCOPE_RAG_SEARCH,
    PLATFORM_SCOPES,
    ManifestError,
    PluginManifest,
    validate_manifest,
)
from jetuse_core.plugins.slack_connector_builtin import (
    SLACK_CONNECTOR_ID,
    slack_connector_definition,
    slack_connector_manifest,
)
from jetuse_core.settings import Settings, get_settings

from ..deps import is_admin

logger = logging.getLogger("jetuse.service")
router = APIRouter(prefix="/platform")

# scope 文字列 1 件の上限長。語彙(platform:*)の最長は 'platform:conversations.read'=27 文字。
# 余裕を見て 64 とし、approve 入力の各 scope 長を縛る(監査・検証前の早期境界 / fail-closed)。
MAX_SCOPE_ITEM_LEN = 64

# rag.search の query 上限長。サンプルアプリ等の入力上限(ai_runtime.MAX_INPUT_CHARS=8000)と揃え、
# 課金される推論 API への過大入力を入口で弾く(BE-04 review BE04-005)。
MAX_RAG_QUERY_CHARS = 8000

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


# テナント(Project OCID)識別子の上限長。登録簿 platform_rag_stores.tenant は VARCHAR2(255) なので
# それを超える入力は Oracle エラーではなく 422 で入口拒否する(BE-04 review BE04-R5-007)。
MAX_TENANT_LEN = 255
# テナント = **Generative AI Project OCID**(ADR-0019)。型を `generativeaiproject` に限定し、
# realm・region・固有部まで含む最低限の形を強制する。これにより tenancy OCID や `ocid1.foo.` 等の
# 誤った型・途中で切れた OCID を入口で弾き、無効値を永続化後 OpenAi-Project に渡して 502 に
# なる境界不備を塞ぐ(BE04-003)。形式: ocid1.generativeaiproject.<realm>.<region>.<unique>
_OCID_RE = re.compile(r"^ocid1\.generativeaiproject\.[a-z0-9]+\.[a-z0-9-]+\.[a-z0-9]+$")


def _require_tenant(tenant: str) -> str:
    """要求リソースのテナント(Project OCID)を受け取り、空/過長/非 OCID を弾く(テナント境界の前提)。

    検索・登録・GET で共有する単一の validator。strip 後に非空・255 文字以内・`ocid1.<type>.` 形式を
    強制する(BE04-R5-007: 過長で Oracle エラーにせず、無効な Project 値を永続化しない)。
    """
    cleaned = (tenant or "").strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="tenant (Project OCID) is required")
    if len(cleaned) > MAX_TENANT_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"tenant (Project OCID) must be at most {MAX_TENANT_LEN} chars",
        )
    if not _OCID_RE.match(cleaned):
        raise HTTPException(
            status_code=422,
            detail="tenant must be a Generative AI Project OCID "
            "(ocid1.generativeaiproject.<realm>.<region>.<id>)",
        )
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


# --- connector.invoke: 実 invoke(BE-03 / builtin=実 HTTP・secret=Vault 解決) ----


def _connector_secret_resolver(
    settings: Settings, *, tenant: str, plugin_id: str, connector_id: str
) -> connector_runtime.SecretResolver:
    """invoke へ渡す secret 解決器を作る(差し替え seam)。

    既定は Vault 解決器で、解決を **テナント＋plugin＋コネクタ instance に束縛**する(`secretRef`→
    `settings.connector_secret_ocids[f"{tenant}/{plugin_id}/{connector_id}/{secretRef}"]`)。これにより
    (a) 別プラグインが同名 `secretRef` を宣言しても他人の秘密を引けない(confused-deputy 防止)、
    (b) 別テナントが同一コア plugin のトークンで他テナントの SaaS 資格情報を共有/越境できない、
    (c) **同一テナント内に同一 plugin の Slack 接続が複数あっても instance ごとに別 secret を解決**
    して取り違えを防ぐ(BLK-001)。実 Vault 読取 IAM・実 Slack Bot トークン投入は人間ゲートのため、
    単体/mock E2E は本関数を monkeypatch して mock 解決器を注入する。
    """
    return connector_runtime.make_vault_secret_resolver(
        settings, tenant=tenant, plugin_id=plugin_id, connector_id=connector_id
    )


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
    """コネクタ action 呼び出し(scope `platform:connector.invoke`)。**実 invoke**(BE-03)。

    手順: **broker 認可**(`pb.authorize`=検証 → scope → テナント一致 → ALLOW/DENY 監査。リソース取得
    より**前**に行い、未認可からの列挙・無監査の早期終了を防ぐ。MAJ-001) → コネクタ取得(404) →
    **コア builtin Slack 限定**(MCP/3rd-party 実行は後続 → 501) → action 存在検証(404) →
    `connector_runtime.invoke_connector_action` へ委譲。invoke 層も **必ず broker 認可**を行う
    (多層防御。同一スコープの ALLOW 監査が route と invoke で各1回出る=二重監査は許容)。builtin は
    実 HTTP(`live_http_caller`)で SaaS を呼ぶ。secret は **テナント＋呼出 plugin＋connector に束縛**
    した Vault 解決(`{tenant}/{plugin_id}/{connector_id}/{secretRef}`→OCID→実トークン)。

    **呼出主体の境界**: コア Slack は単一プラグイン(`jetuse/slack-connector`)が所有する共有
    capability で、呼ぶのは connector.invoke を承認された **別の L3 デモ**(トークン sub=デモ自身の
    plugin_id ≠ Slack)である。よって「connector 所有 plugin == トークン sub」を要求すると正規の
    デモ呼出が必ず 403 になり主要経路が到達不能になる(review BLK-001)。呼出ごとの境界は
    (a) tenant 認可、(b) コア限定、(c) **secret 束縛**`{tenant}/{呼出plugin}/{connector}/{ref}` が
    担う。未プロビジョンの (tenant, 呼出 plugin, connector) は secret 解決不能で 503 となり Slack へ
    到達しない(fail-closed)。connector 行は秘密を持たない(secret_ref のみ)。

    fail-closed の HTTP 写像: 鍵未設定→503、認可拒否(検証系→401 / scope・tenant→403)、
    **未登録 connector / 未知 action→404**(リソース不在。取得・存在検証で返す)、版固定不整合→**409**
    (再インストール要求。MAJ-001)、secret 解決不能・依存(Vault/IAM)障害→**503**、外部 SaaS 到達/応答
    障害→**502**、payload 不正・SaaS 論理エラー等のクライアント要求不備→**400**。**実シークレットは
    戻り値・監査・例外に出さない**(runtime redact)。実 Slack 認証・実 Vault IAM 付与は人間ゲート。
    """
    tenant = _require_tenant(req.tenant)
    token = creds.credentials if creds else ""
    # 認可(authz)をリソース取得より前に行う: JWT 検証 → scope(connector.invoke)→ テナント一致 →
    # ALLOW/DENY 監査。未認可トークンに 404/403/422/501 の差を見せて connector を列挙させない＋
    # 無効/scope 不足/tenant 不一致の試行を必ず監査に残す(MAJ-001)。委譲先も再認可する(多層防御)。
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
    # 呼出主体の所有チェックは**しない**: コア Slack は共有 capability で、所有 plugin
    # (`jetuse/slack-connector`)と呼出デモの sub は必ず異なる(=所有一致を要求すると正規呼出が
    # 必ず 403 で主要経路が到達不能。review BLK-001)。呼出ごとの境界は tenant 認可＋コア限定＋
    # secret 束縛`{tenant}/{呼出plugin}/{connector}/{ref}`が担う(未プロビジョンは 503 で
    # Slack 不達)。connector 行は tenant 列が無く物理 tenant 束縛は INFRA(ADR-0020 R1)。
    # BE-03 の実行対象はコア同梱 builtin Slack コネクタに**限定**する(fail-closed)。コア以外
    # (MCP / 3rd-party builtin)は実行時 SSRF ガード等が未実装の非ゴールのため 501 で経路を開かない。
    if record.get("plugin_id") != SLACK_CONNECTOR_ID:
        raise HTTPException(
            status_code=501,
            detail=(
                "only the core builtin Slack connector is executable in BE-03; "
                "MCP/third-party connector execution is a follow-up"
            ),
        )
    # コアは**コード同梱**が正本。DB 由来の定義(改竄/破損し得る)を
    # 実行・スコープ算出・builtin handler 選択に使わず、**カノニカルな
    # `slack_connector_definition()` を信頼源**にする。これで同 ID を名乗る取込物・
    # 破損行が別 handler や別スコープへ到達するのを防ぐ(review MAJ-001 / BLK-001)。
    # DB 行は install 済み＋所属＋**版固定整合**の確認にのみ用いる(下記)。
    connector_def = slack_connector_definition()
    # **版固定スナップショット整合(MAJ-001)**: plugin_id だけでなく、保存行の
    # (source_version, provider, transport) がサポート対象のカノニカル版と一致することを要求する。
    # コアは (id, version) で版固定のため、旧 1.0.0 install 行や provider/transport 不整合の取込物に
    # 対して**現行カノニカル定義(1.1.0 / slack / builtin)を暗黙実行しない**。不一致は再インストール
    # 要求として 409 で拒否する(古い権限契約・別 provider/transport の行が新挙動で動くのを防ぐ。
    # 実行・スコープ算出・handler 選択はカノニカル定義を信頼源にする方針と整合)。
    supported_version = slack_connector_manifest().version
    if (
        record.get("source_version") != supported_version
        or record.get("provider") != connector_def.provider
        or record.get("transport") != connector_def.transport
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "connector instance does not match the supported core Slack connector snapshot "
                f"(supported: version={supported_version}, provider={connector_def.provider}, "
                f"transport={connector_def.transport}); reinstall required"
            ),
        )
    actions = {a.name for a in connector_def.actions}
    if req.action not in actions:
        raise HTTPException(
            status_code=404,
            detail=f"action '{req.action}' is not defined on connector {req.connector_id}",
        )
    secret_resolver = _connector_secret_resolver(
        settings, tenant=tenant, plugin_id=ctx.plugin_id, connector_id=req.connector_id
    )
    try:
        result = await asyncio.to_thread(
            invoke_connector_action,
            connector_def,
            req.action,
            req.params,
            broker_token=token,
            tenant=tenant,
            resource=f"connector.invoke:{req.connector_id}",
            settings=settings,
            secret_resolver=secret_resolver,
            http_caller=connector_runtime.live_http_caller,
            mcp_caller=None,
        )
    except ConnectorInvokeDenied as e:
        # 認可拒否。reason を HTTP に写像(検証系→401 / scope_denied・tenant_mismatch 等→403)。
        if e.reason in _UNAUTHORIZED_REASONS:
            raise HTTPException(
                status_code=401,
                detail=f"invalid broker token: {e.reason}",
                headers={"WWW-Authenticate": "Bearer"},
            ) from e
        raise HTTPException(status_code=403, detail=f"forbidden: {e.reason}") from e
    except SecretResolutionError as e:
        # secret 未マップ(設定不備)・Vault/IAM 障害=サーバー/依存側の問題。クライアント不備では
        # ないので恒久的 400 に潰さず 503(設定/依存)に倒す(監視・再試行判断を誤らせない。MAJ-004)。
        # 実シークレット/OCID は runtime 側で redact 済み(例外文に出ない)。
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ConnectorTransportError as e:
        # 外部 SaaS への到達/応答障害=上流側の問題 → 502(Bad Gateway)。本文は echo しない。
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ConnectorInvokeError as e:
        # payload 不正・未知 action・SaaS 論理エラー(channel_not_found 等)=クライアント不備 → 400。
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": result.ok,
        "provider": result.provider,
        "action": result.action,
        "transport": result.transport,
        "output": result.output,
        "jti": result.jti,
    }


# --- rag.search: OCI Responses file_search 委譲(テナント境界＝本体がストア解決) ----


class RagSearchRequest(BaseModel):
    tenant: str = Field(min_length=1, description="要求リソースのテナント(Project OCID)")
    # query は課金される推論 API へ渡るため、上限長(既存 MAX_INPUT_CHARS と揃える)と空白のみ拒否で
    # 境界を縛る(過大入力での 502・過剰課金・ワーカー長時間占有を防ぐ。BE-04 review BE04-005)。
    query: str = Field(min_length=1, max_length=MAX_RAG_QUERY_CHARS)
    top_k: int = Field(default=5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _non_blank_query(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("query は空白のみにできない")
        return cleaned


@router.post("/rag/search")
async def platform_rag_search(
    req: RagSearchRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_broker_bearer)],
) -> dict[str, Any]:
    """テナント文書のセマンティック検索(scope `platform:rag.search`)。

    authorize(JWT 検証 → scope → テナント → 監査)通過後、**broker 検証済みテナント(Project OCID)**
    をストア所有者キーにして本体がベクトルストアを解決し、OCI Responses の file_search へ委譲する。
    呼び出し元(プラグイン)はストア id を渡さない/受け取らないため、別テナントのストアには構造的に
    到達できない(秘密=vector_store_id は本体のみ保持。越境はそもそも authorize の tenant 一致で
    弾かれ、加えてストア解決もテナント鍵に閉じる＝二重の境界)。ヒット＋引用＋根拠付き回答を返す。

    fail-closed: 委譲先(GenAI)の失敗は 200 へ倒さず 502(上流エラー)に写像する。テナントに取り込み
    済み文書(ストア)が無い場合は空ヒットの 200 を返す(越境ではなくデータ未取込)。
    """
    tenant = _require_tenant(req.tenant)
    _authorize(
        creds,
        PLATFORM_SCOPE_RAG_SEARCH,
        tenant=tenant,
        settings=settings,
        resource="rag.search",
    )
    try:
        result = await asyncio.to_thread(rag.search, tenant, req.query, top_k=req.top_k)
    except rag.RagSearchError as e:
        # 検索委譲(OCI Responses file_search)の失敗。認可は通っているが上流が応答しない → 502。
        # 上流例外の文字列には内部エンドポイント/リクエスト情報/vector_store_id 等が混じりうるため、
        # クライアントには**固定の汎用メッセージ**だけを返し、詳細はサーバログにのみ残す
        # (「ストア id は本体のみ保持」の境界を破らない。BE-04 review BE04-004)。
        logger.error("platform rag.search upstream error (tenant=%s): %s", tenant, e)
        raise HTTPException(
            status_code=502, detail="rag search upstream error"
        ) from e
    return {"tenant": tenant, "query": req.query, **result}


# --- rag ストア登録(テナント→ベクトルストアの紐付け / SA 限定) -----------------
#
# テナント所有ストアを登録簿 platform_rag_stores へ登録する唯一の REST 経路(ADR-0019 承認済)。
# rag.search はこの登録簿だけを正本にストアを解決するため、登録経路が無いと検索は常に空になる
# (BE-04 review BE04-001)。テナントとストアの紐付けは越境に直結する管理操作なので、承認(grants)と
# 同じく **管理者(ADMIN_USERS)限定**にする(プラグイン提示の broker トークンでは登録できない)。
# 文書のストアへの取込パイプライン自体は別タスク/INFRA(ADR-0019 未解決事項 §1)。


class RagStoreRegisterRequest(BaseModel):
    tenant: str = Field(min_length=1, description="登録対象テナント(Project OCID)")
    vector_store_id: str = Field(
        min_length=1, max_length=128, description="テナントが所有するベクトルストア id"
    )

    @field_validator("vector_store_id")
    @classmethod
    def _non_blank_store_id(cls, v: str) -> str:
        # strip 後の非空を保証(空白のみは 422。Oracle の '' = NULL → NOT NULL 503 を防ぐ)。
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("vector_store_id は空白のみにできない")
        return cleaned


def _store_fingerprint(vector_store_id: str) -> str:
    """監査に残すストア id の短いハッシュ(秘密=store id 実値を監査表に平文で残さない)。"""
    return hashlib.sha256(vector_store_id.encode("utf-8")).hexdigest()[:16]


def _audit_store_register(
    decision: str, *, tenant: str, actor: str, vector_store_id: str, reason: str = ""
) -> None:
    """テナント↔ストアの登録(成功 REGISTER / 拒否 REG_DENY)を append-only 監査へ刻む。

    高権限の管理操作(テナント境界を左右する)なので、誰が(actor)・いつ(created_at)・どのテナントへ・
    どのストア(平文でなく fingerprint)を紐付けたかを必ず残す(BE-04 review BE04-R5-004)。
    ベストエフォート(record_broker_access が記録失敗を握り潰す)だが broker 監査と同じ表に集約する。
    """
    pb.record_broker_access(
        plugin_id="platform:rag.store",
        tenant=tenant,
        scope="rag.store",
        decision=decision,
        reason=reason,
        resource=f"register:by={actor};store_fp={_store_fingerprint(vector_store_id)}",
    )


@router.put("/rag/stores")
async def platform_register_rag_store(
    req: RagStoreRegisterRequest,
    user: Annotated[AuthContext, Depends(require_user)],
) -> dict[str, Any]:
    """テナント→ベクトルストアの所有を登録簿へ upsert する(SA 限定 / ADR-0019)。

    DB 更新の前にテナント(Project)でストアの存在・所属を検証し、別 Project/不存在は登録しない
    (誤登録→恒久 502・越境を防ぐ。BE-04 review BE04-R5-003)。誰が登録/拒否されたかは監査へ刻む
    (BE04-R5-004)。
    """
    actor = _require_sa(user)
    tenant = _require_tenant(req.tenant)
    store_id = req.vector_store_id.strip()
    try:
        await asyncio.to_thread(rag.register_tenant_store, tenant, store_id)
    except rag.StoreVerificationError as e:
        # ストアが実在しない(404 / id 不一致)= 利用者の入力不正。DB は更新されない。監査に残し 400。
        _audit_store_register(
            "REG_DENY", tenant=tenant, actor=actor, vector_store_id=store_id, reason=str(e)
        )
        logger.warning("rag store register denied (tenant=%s, by=%s): %s", tenant, actor, e)
        raise HTTPException(
            status_code=400, detail="vector store is not accessible (not found or unreachable)"
        ) from e
    except rag.StoreUpstreamError as e:
        # 検証先(CP)の一過性障害(接続/タイムアウト/認証/5xx)。入力不正と区別し 502(再試行可能)。
        # BE04-008: 恒久的な入力エラー 400 に倒さない。DB は更新されない。高権限操作なので、
        # 上流障害で登録に至らなかった試行も監査に残す(誰が・どのテナント/ストアか。BE04-012)。
        _audit_store_register(
            "REG_ERR", tenant=tenant, actor=actor, vector_store_id=store_id, reason=str(e)
        )
        logger.error("rag store register upstream error (tenant=%s, by=%s): %s", tenant, actor, e)
        raise HTTPException(
            status_code=502, detail="vector store verification upstream error"
        ) from e
    except rag.StoreConflictError as e:
        # 別テナントへ既登録のストア(UNIQUE 違反)。越境防止のため 409 で拒否し監査(BE04-001)。
        # decision は監査表の VARCHAR2(8) 上限に収める(REG_DENY と並ぶ 8 文字)。
        _audit_store_register(
            "REG_CONF", tenant=tenant, actor=actor, vector_store_id=store_id, reason=str(e)
        )
        logger.warning("rag store register conflict (tenant=%s, by=%s): %s", tenant, actor, e)
        raise HTTPException(
            status_code=409, detail="vector store is already registered to another tenant"
        ) from e
    _audit_store_register("REGISTER", tenant=tenant, actor=actor, vector_store_id=store_id)
    return {"registered": True, "tenant": tenant}


@router.get("/rag/stores")
async def platform_get_rag_store(
    user: Annotated[AuthContext, Depends(require_user)],
    tenant: str,
) -> dict[str, Any]:
    """テナントに登録済みのストア有無を返す(SA 限定。store id 実値は返さない=秘密保持)。"""
    _require_sa(user)
    t = _require_tenant(tenant)
    store_id = await asyncio.to_thread(rag.get_tenant_store_id, t)
    return {"tenant": t, "registered": bool(store_id)}


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
