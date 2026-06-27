"""コア同梱 Slack コネクタ(CON-02)。

`kind: connector` の最初の実体。Slack を JetUse から呼び出すための **L2 MCP コネクタ** を
**builtin transport**(コア同梱・インプロセス実行)で提供する。CON-01 の `connector.py` の配布
スキーマ・合成バリデーションを満たす定義(`slack_connector_definition`/`slack_connector_manifest`)と、
`connector_runtime` の builtin ハンドラ((slack, post_message) / (slack, list_channels))を持つ。

設計:
  - **実シークレットを持たない**: auth=oauth2 / `secretRef="slack-bot-token"`(参照名)。実 Bot
    トークンは invoke 時に `secret_resolver` が Vault から解決して `InvokeRequest.token` に載る
    (本タスクは実 Slack 認証を投入しないため、テスト/E2E は mock を注入。実 Vault 束ねは
    install 時=CON-03)。
  - **実 HTTP は差し替え可能**: ハンドラは Slack Web API への要求(URL/ヘッダ/JSON)を組み立てる
    だけで、実送信は `connector_runtime` から渡る `http_caller` に委ねる(既定は実ネットワーク禁止の
    fail-closed)。これにより「投稿フロー」を実 Slack なしで mock 検証でき、実接続を CON-03 へ素直に
    差し替えられる。
  - **トークンを出さない**: ハンドラ戻り値・例外に実トークンを含めない(Authorization のみに載せる)。

コア同梱方針は usecases_builtin / sample_app_builtin と同じ(DB に置かずコード同梱)。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .connector import ConnectorDefinition, validate_connector
from .connector_runtime import (
    ConnectorInvokeError,
    HttpCaller,
    InvokeRequest,
    _check_payload_field,
    register_builtin_action,
)
from .manifest import PluginManifest, validate_manifest

#: コア Slack コネクタの固定 ID / secretRef 参照名(install 時に束ねる Vault 秘密の論理名)。
SLACK_CONNECTOR_ID = "jetuse/slack-connector"
SLACK_SECRET_REF = "slack-bot-token"

#: Slack Web API エンドポイント(builtin ハンドラが要求を組み立てる先。実送信は http_caller)。
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_LIST_CHANNELS_URL = "https://slack.com/api/conversations.list"

_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


_DEFINITION: dict[str, Any] = {
    "provider": "slack",
    "transport": "builtin",
    # auth は実値を持たない: secretRef は「束ねるべき秘密の参照名」(非機密)。
    # provider scope(Slack 側): post_message=chat:write / list_channels(conversations.list の
    # public/private 列挙)=channels:read+groups:read。実装済み action が必要とする scope を漏れなく
    # 宣言する(CON-03 で Bot token を束ねた際に missing_scope にならないようにする。CON02-MAJ-003)。
    "auth": {
        "kind": "oauth2",
        "secretRef": SLACK_SECRET_REF,
        "scopes": ["chat:write", "channels:read", "groups:read"],
    },
    "actions": [
        {
            "name": "post_message",
            "title": "メッセージ投稿",
            "description": "指定チャンネルへメッセージを投稿する(chat.postMessage)。",
            # Slack のみを叩くブリッジ操作で Platform データに触れないため permissions は空。
            # 呼ぶ権利そのもの(platform:connector.invoke)は invoke 層が常に強制する。
            "permissions": [],
        },
        {
            "name": "list_channels",
            "title": "チャンネル一覧",
            "description": "投稿先チャンネルを列挙する(conversations.list)。",
            "permissions": [],
        },
    ],
    "summary": "Slack コネクタ(コア)。chat.postMessage / conversations.list を builtin で提供。",
}

_MANIFEST: dict[str, Any] = {
    "schemaVersion": "1",
    "id": SLACK_CONNECTOR_ID,
    "version": "1.0.0",
    "kind": "connector",
    "name": "Slack コネクタ",
    "description": "Slack へ通知/起動するコア同梱コネクタ(L2 MCP / builtin)。",
    "publisher": "jetuse",
    "jetuse": {"minVersion": "0.3.0"},
    # actions が Platform データに触れない(SaaS ブリッジのみ)ため宣言スコープは空。
    "permissions": [],
    "contributes": {"connector": _DEFINITION},
    "tags": ["slack", "connector", "notification"],
    "icon": "💬",
}


@lru_cache(maxsize=1)
def slack_connector_manifest() -> PluginManifest:
    """検証済みのコア Slack コネクタ manifest(kind=connector)。"""
    return validate_manifest(_MANIFEST)


@lru_cache(maxsize=1)
def slack_connector_definition() -> ConnectorDefinition:
    """検証済みのコア Slack コネクタ定義(contributes["connector"])。"""
    return validate_connector(_DEFINITION)


# --- builtin ハンドラ ------------------------------------------------------


@register_builtin_action("slack", "post_message")
def _post_message(req: InvokeRequest, http: HttpCaller) -> dict[str, Any]:
    """Slack `chat.postMessage` を呼ぶ。実トークンは Authorization のみに載せ戻り値に出さない。"""
    channel = _check_payload_field(req.payload, "channel", required=True)
    text = _check_payload_field(req.payload, "text", required=True)
    if not req.token:  # pragma: no cover - oauth2 は secret 必須(runtime が保証)
        raise ConnectorInvokeError("Slack 投稿には Bot トークンが必要")
    resp = http(
        SLACK_POST_MESSAGE_URL,
        {"Authorization": f"Bearer {req.token}", **_JSON_HEADERS},
        {"channel": channel, "text": text},
    )
    if not isinstance(resp, dict) or not resp.get("ok"):
        err = resp.get("error") if isinstance(resp, dict) else "invalid_response"
        raise ConnectorInvokeError(f"Slack chat.postMessage 失敗: {err}")
    # 投稿の同定情報のみ返す(トークンは含めない)。
    return {"ok": True, "channel": resp.get("channel"), "ts": resp.get("ts")}


@register_builtin_action("slack", "list_channels")
def _list_channels(req: InvokeRequest, http: HttpCaller) -> dict[str, Any]:
    """Slack `conversations.list` を呼んで投稿先候補を列挙する。"""
    if not req.token:  # pragma: no cover
        raise ConnectorInvokeError("Slack 操作には Bot トークンが必要")
    resp = http(
        SLACK_LIST_CHANNELS_URL,
        {"Authorization": f"Bearer {req.token}", **_JSON_HEADERS},
        {},
    )
    if not isinstance(resp, dict) or not resp.get("ok"):
        err = resp.get("error") if isinstance(resp, dict) else "invalid_response"
        raise ConnectorInvokeError(f"Slack conversations.list 失敗: {err}")
    channels = [
        {"id": c.get("id"), "name": c.get("name")}
        for c in (resp.get("channels") or [])
        if isinstance(c, dict)
    ]
    return {"ok": True, "channels": channels}
