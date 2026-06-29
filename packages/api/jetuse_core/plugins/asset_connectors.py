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

from collections.abc import Mapping
from typing import Any

from .connector import (
    ConnectorDefinition,
    validate_connector,
    validate_connector_composition,
)
from .connector_runtime import (
    ConnectorInvokeResult,
    McpCaller,
    SecretResolver,
    invoke_connector_action,
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


# --- 実 MCP 呼び出し（invoke 配線 / BE-06） --------------------------------
#
# CON-01 は配布表現の正規化、CON-02（connector_runtime）は invoke 実行層を担った。本節は
# 既存資産コネクタ（No.1-RAG / No.1-SQL-Assist）の **呼出元**（これまで存在しなかった）を提供する。
# transport=mcp なので connector_runtime が Responses API（type:"mcp"）経由で外部 MCP サーバーへ
# 到達する（既定 `_default_mcp_caller`＝実 MCP。テスト/mock E2E は `mcp_caller` を注入）。認可は必ず
# Platform ブローカー経由（rag.search / db.query ＋ connector.invoke）。資格情報は Vault
# （`vault_secret_resolver`）。実 MCP エンドポイント配備・実 Vault 束ねは人間ゲート（実資産接続）。


def vault_secret_resolver(secret_ocids: Mapping[str, str]) -> SecretResolver:
    """secretRef（論理参照名）→ 実トークンを **OCI Vault** から解決する resolver を作る。

    `secret_ocids` は secretRef → Vault secret OCID の対応（環境依存・人間ゲートで与える。実トークン
    値ではなく OCID 参照のみ）。返す resolver は invoke 時に OCID から実値を Vault 経由で読む
    （`mcp_servers._read_secret` を再利用＝唯一の Vault 読取経路）。未知 ref は fail-closed
    （`KeyError`→ connector_runtime が `ConnectorInvokeError` に正規化）。

    実 Vault への到達（実 OCID・実権限）は人間ゲート。単体/mock E2E では本 resolver を使わず、
    `secret_resolver` に mock を注入する（実 Vault を触らない）。
    """
    mapping = dict(secret_ocids)

    def _resolve(ref: str) -> str:
        ocid = mapping.get(ref)
        if not ocid:
            # 参照名は宣言の一部（非機密）。実値は出さない。
            raise KeyError(f"secretRef '{ref}' に対応する Vault secret OCID が未登録")
        from ..mcp_servers import _read_secret

        return _read_secret(ocid)

    return _resolve


#: invoke ヘルパの主入力（query/question）と追加 payload の長さ・総サイズ上限。
#: connector_runtime の MAX_PAYLOAD_FIELD_LEN は builtin 経路専用で mcp には効かないため、公開
#: ヘルパ側で **認可・secret 解決より前に** 境界を弾く（巨大入力/不正値を通さない。BOUNDARY-001）。
MAX_ASSET_FIELD_LEN = 40000
MAX_ASSET_PAYLOAD_BYTES = 200000


def _validated_payload(
    primary_key: str, primary_value: Any, extra: dict[str, Any]
) -> dict[str, Any]:
    """主入力＋追加 payload を **認可前に** 検証して payload dict を作る（fail-closed）。

    主入力は非空文字列・長さ上限。追加 payload は予約キー衝突禁止・JSON 化可能・全体サイズ上限。
    違反は `ConnectorInvokeError`（呼び出し側で予測可能なエラーになる）。
    """
    from .connector_runtime import ConnectorInvokeError

    if not isinstance(primary_value, str) or not primary_value.strip():
        raise ConnectorInvokeError(f"payload.{primary_key} は必須の非空文字列")
    if len(primary_value) > MAX_ASSET_FIELD_LEN:
        raise ConnectorInvokeError(f"payload.{primary_key} が長すぎる（>{MAX_ASSET_FIELD_LEN}）")
    if primary_key in extra:
        raise ConnectorInvokeError(f"payload.{primary_key} を追加引数で上書きできない")
    payload: dict[str, Any] = {primary_key: primary_value, **extra}
    try:
        from .manifest import _assert_json_value

        _assert_json_value(payload, "payload")
    except ValueError as e:
        raise ConnectorInvokeError(f"payload が JSON 化できない値を含む: {e}") from None
    import json as _json

    # 実バイト数で判定する（多バイト文字＝日本語/絵文字でも上限を正しく効かせる。BE06-006）。
    size_bytes = len(_json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size_bytes > MAX_ASSET_PAYLOAD_BYTES:
        raise ConnectorInvokeError(f"payload 全体が大きすぎる（>{MAX_ASSET_PAYLOAD_BYTES} bytes）")
    return payload


def invoke_no1_rag_search(
    endpoint: str,
    query: str,
    *,
    broker_token: str,
    tenant: str,
    secret_resolver: SecretResolver,
    resource: str = "",
    settings: Any = None,
    mcp_caller: McpCaller | None = None,
    secret_ref: str = NO1_RAG_SECRET_REF,
    **payload: Any,
) -> ConnectorInvokeResult:
    """No.1-RAG の `search` を実 MCP 経由で呼び出す（broker 認可つき・fail-closed）。

    `endpoint` は No.1-RAG MCP サーバーの HTTPS URL（環境依存・人間ゲート）。`broker_token` は
    `platform:connector.invoke` ＋ `platform:rag.search` を持つ短期トークン。`secret_resolver` は
    secretRef→実 API トークン（Vault 束ね＝`vault_secret_resolver` / テストは mock）。追加
    payload はキーワードで渡す（既定は query のみ）。戻り値・例外に実トークンは出ない。
    """
    definition = no1_rag_connector_definition(endpoint, secret_ref=secret_ref)
    return invoke_connector_action(
        definition,
        "search",
        _validated_payload("query", query, payload),
        broker_token=broker_token,
        tenant=tenant,
        resource=resource,
        settings=settings,
        secret_resolver=secret_resolver,
        mcp_caller=mcp_caller,
    )


def invoke_no1_sql_nl2sql(
    endpoint: str,
    question: str,
    *,
    broker_token: str,
    tenant: str,
    secret_resolver: SecretResolver,
    resource: str = "",
    settings: Any = None,
    mcp_caller: McpCaller | None = None,
    secret_ref: str = NO1_SQL_SECRET_REF,
    **payload: Any,
) -> ConnectorInvokeResult:
    """No.1-SQL-Assist の `nl2sql` を実 MCP 経由で呼び出す（broker 認可つき・fail-closed）。

    `broker_token` は `platform:connector.invoke` ＋ `platform:db.query` を持つ短期トークン。
    他は invoke_no1_rag_search と同契約（資格情報は Vault・実 MCP 配備は人間ゲート）。
    """
    definition = no1_sql_assist_connector_definition(endpoint, secret_ref=secret_ref)
    return invoke_connector_action(
        definition,
        "nl2sql",
        _validated_payload("question", question, payload),
        broker_token=broker_token,
        tenant=tenant,
        resource=resource,
        settings=settings,
        secret_resolver=secret_resolver,
        mcp_caller=mcp_caller,
    )
