"""既存資産（No.1-RAG / No.1-SQL-Assist）の L2 MCP コネクタ・オンボード定義（ASSET-01）。

`docs/enhance/202607-demo-platform-plan.md` §6 D9 / specs/16-platform.md §12 の `kind: connector`
（L2 MCP コネクタ）として、社内既存資産の **検索（No.1-RAG）／NL2SQL（No.1-SQL-Assist）**
パイプラインを JetUse の配布表現へ正規化する。これらは元々 Gradio UI を持つ独立アプリだが、
**UI は捨て**、検索 / NL2SQL の「パイプライン本体」だけを MCP 経由で JetUse から呼び出せる正規
コネクタとして束ねる（オンボード方式の整理は docs/verification/ASSET-01.md）。

設計方針（CON-01 の契約をそのまま踏襲する）:
  - **transport=mcp**: No.1-RAG / No.1-SQL-Assist は外部にデプロイされた資産であり、各々の MCP
    サーバー（HTTPS エンドポイント）を Responses API（type:"mcp"）経由で叩く。エンドポイントは
    デプロイ環境ごとに異なるため **builder の引数**（`endpoint`）で受ける（ハードコードしない）。
  - **認証は Vault OCID 参照のみ**: auth.kind=api_token・`secretRef`（論理参照名）だけを宣言し、
    **実 API トークンは一切持たない**（実値は install 時に Vault(OCID) へ束ねる＝人間ゲート）。
    CLAUDE.md「認証実値をコミットしない」と specs §12.2 の機密区分に従う。
  - **データ境界＝Platform スコープで表す**: 検索はテナント RAG データに触れるため action
    `search` が `platform:rag.search` を、NL2SQL はテナント DB に触れるため action `nl2sql` が
    `platform:db.query` を要求する。これにより invoke は Platform API ブローカー（PAPI-01）経由で
    必ず認可・監査される（コネクタ＝「DB 認証情報を持たずにテナントデータへ到達する唯一の正規
    経路」plan §4-3）。
  - 本モジュールは **配布表現（manifest／定義）の正規化** に責務を限定する。実 MCP 呼び出し・
    Vault 束ね・実エンドポイント配備は人間ゲート（実資産接続）。invoke 経路自体は CON-02 の
    `connector_runtime.invoke_connector_action`（mcp transport / mcp_caller 差し替え可）。
"""

from __future__ import annotations

from typing import Any

from .connector import (
    ConnectorDefinition,
    validate_connector,
    validate_connector_composition,
)
from .manifest import (
    PLATFORM_SCOPE_DB_QUERY,
    PLATFORM_SCOPE_RAG_SEARCH,
    PluginManifest,
    validate_manifest,
)

# --- 既存資産の安定キー（表示文言ではなくオンボード上の安定識別子） --------------

#: No.1-RAG 既存資産。
NO1_RAG_PROVIDER = "no1-rag"
NO1_RAG_PLUGIN_ID = "jetuse/no1-rag-connector"
#: install 時に Vault へ束ねる秘密の **論理参照名**（実 API トークンではない）。
NO1_RAG_SECRET_REF = "no1-rag-api-token"

#: No.1-SQL-Assist 既存資産。
NO1_SQL_PROVIDER = "no1-sql-assist"
NO1_SQL_PLUGIN_ID = "jetuse/no1-sql-assist-connector"
NO1_SQL_SECRET_REF = "no1-sql-assist-api-token"

#: 既存資産コネクタの版（オンボード初版）。
ASSET_CONNECTOR_VERSION = "1.0.0"


def _connector_manifest(
    *,
    plugin_id: str,
    name: str,
    description: str,
    definition: dict[str, Any],
    permissions: list[str],
    tags: list[str],
    icon: str,
) -> dict[str, Any]:
    """共通の connector manifest（kind=connector）骨格を組み立てる。"""
    return {
        "schemaVersion": "1",
        "id": plugin_id,
        "version": ASSET_CONNECTOR_VERSION,
        "kind": "connector",
        "name": name,
        "description": description,
        "publisher": "jetuse",
        "jetuse": {"minVersion": "0.3.0"},
        # action が要求する Platform スコープを漏れなく宣言する（合成バリデーション整合）。
        # 注: `platform:connector.invoke`（コネクタを呼ぶ権利そのもの）は **invoke 層が全コネクタに
        # 普遍的に強制する**スコープ（connector_runtime._required_scopes が常に先頭に付与）であり、
        # action 由来ではないため manifest.permissions には宣言しない（コア Slack コネクタ
        # slack_connector_builtin も permissions=[]。declare すると unused_permissions 警告）。
        # ここに入れるのは action が触れるデータドメインのスコープ（rag.search/db.query）のみ。
        "permissions": permissions,
        "contributes": {"connector": definition},
        "tags": tags,
        "icon": icon,
    }


# --- No.1-RAG ----------------------------------------------------------------


def no1_rag_connector_definition_dict(
    endpoint: str, *, secret_ref: str = NO1_RAG_SECRET_REF
) -> dict[str, Any]:
    """No.1-RAG コネクタ定義（contributes["connector"]）の配布表現 dict を組み立てる。

    `endpoint` はデプロイ環境ごとに異なる No.1-RAG MCP サーバーの HTTPS URL（オンボード時に
    オペレータが与える。実値は .env / Vault・人間ゲート）。`secret_ref` は API トークンの **参照名**
    （実値ではない）。
    """
    return {
        "provider": NO1_RAG_PROVIDER,
        "transport": "mcp",
        "endpoint": endpoint,
        # 既存資産は API トークン認証。実トークンは持たず参照名のみ（install 時に Vault 束ね）。
        "auth": {"kind": "api_token", "secretRef": secret_ref},
        "actions": [
            {
                "name": "search",
                "title": "ドキュメント検索",
                "description": "No.1-RAG の検索パイプラインで関連ドキュメントを取得する。",
                # テナント RAG データに触れるため Platform スコープを要求する。
                "permissions": [PLATFORM_SCOPE_RAG_SEARCH],
            },
        ],
        "summary": "No.1-RAG（既存資産）の検索パイプラインを L2 MCP コネクタとして正規化。",
    }


def no1_rag_connector_definition(
    endpoint: str, *, secret_ref: str = NO1_RAG_SECRET_REF
) -> ConnectorDefinition:
    """検証済みの No.1-RAG コネクタ定義。"""
    return validate_connector(
        no1_rag_connector_definition_dict(endpoint, secret_ref=secret_ref)
    )


def no1_rag_connector_manifest(
    endpoint: str, *, secret_ref: str = NO1_RAG_SECRET_REF
) -> PluginManifest:
    """検証済みの No.1-RAG コネクタ manifest（kind=connector）。"""
    return validate_manifest(
        _connector_manifest(
            plugin_id=NO1_RAG_PLUGIN_ID,
            name="No.1-RAG コネクタ",
            description="既存資産 No.1-RAG の検索パイプライン（L2 MCP / 外部 MCP サーバー）。",
            definition=no1_rag_connector_definition_dict(endpoint, secret_ref=secret_ref),
            permissions=[PLATFORM_SCOPE_RAG_SEARCH],
            tags=["no1-rag", "connector", "rag", "search", "asset"],
            icon="🔎",
        )
    )


# --- No.1-SQL-Assist ---------------------------------------------------------


def no1_sql_assist_connector_definition_dict(
    endpoint: str, *, secret_ref: str = NO1_SQL_SECRET_REF
) -> dict[str, Any]:
    """No.1-SQL-Assist コネクタ定義（contributes["connector"]）の配布表現 dict を組み立てる。"""
    return {
        "provider": NO1_SQL_PROVIDER,
        "transport": "mcp",
        "endpoint": endpoint,
        "auth": {"kind": "api_token", "secretRef": secret_ref},
        "actions": [
            {
                "name": "nl2sql",
                "title": "自然言語→SQL",
                "description": "No.1-SQL-Assist の NL2SQL で自然言語からクエリを生成・実行する。",
                # テナント DB に触れるため Platform スコープを要求する。
                "permissions": [PLATFORM_SCOPE_DB_QUERY],
            },
        ],
        "summary": "No.1-SQL-Assist（既存資産）の NL2SQL を L2 MCP コネクタとして正規化。",
    }


def no1_sql_assist_connector_definition(
    endpoint: str, *, secret_ref: str = NO1_SQL_SECRET_REF
) -> ConnectorDefinition:
    """検証済みの No.1-SQL-Assist コネクタ定義。"""
    return validate_connector(
        no1_sql_assist_connector_definition_dict(endpoint, secret_ref=secret_ref)
    )


def no1_sql_assist_connector_manifest(
    endpoint: str, *, secret_ref: str = NO1_SQL_SECRET_REF
) -> PluginManifest:
    """検証済みの No.1-SQL-Assist コネクタ manifest（kind=connector）。"""
    return validate_manifest(
        _connector_manifest(
            plugin_id=NO1_SQL_PLUGIN_ID,
            name="No.1-SQL-Assist コネクタ",
            description="既存資産 No.1-SQL-Assist の NL2SQL（L2 MCP / 外部 MCP サーバー）。",
            definition=no1_sql_assist_connector_definition_dict(
                endpoint, secret_ref=secret_ref
            ),
            permissions=[PLATFORM_SCOPE_DB_QUERY],
            tags=["no1-sql-assist", "connector", "nl2sql", "database", "asset"],
            icon="🧮",
        )
    )


# --- オンボード妥当性チェック（合成バリデーション） ------------------------------


def asset_connector_manifests(rag_endpoint: str, sql_endpoint: str) -> list[PluginManifest]:
    """既存資産コネクタ（No.1-RAG / No.1-SQL-Assist）の検証済み manifest を返す。

    `rag_endpoint` / `sql_endpoint` は各資産の MCP サーバー HTTPS URL（オンボード時にオペレータが
    与える環境依存値。実値は人間ゲート）。返り値はオンボード対象の正準集合で、登録（register_connector）
    の入力にできる。各 manifest は合成バリデーションを通る（宣言整合）ことを本関数が保証する。
    """
    manifests = [
        no1_rag_connector_manifest(rag_endpoint),
        no1_sql_assist_connector_manifest(sql_endpoint),
    ]
    for m in manifests:
        report = validate_connector_composition(m)
        if not report.ok:  # pragma: no cover - 定義は宣言整合済み（保険）
            raise ValueError(
                f"asset connector {m.id} の合成バリデーション不整合: "
                f"{report.undeclared_permissions}"
            )
    return manifests
