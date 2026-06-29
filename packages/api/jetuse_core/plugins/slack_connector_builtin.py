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

import time
from functools import lru_cache
from typing import Any

from .connector import ConnectorDefinition, validate_connector
from .connector_runtime import (
    ConnectorInvokeError,
    ConnectorTransportError,
    HttpCaller,
    InvokeRequest,
    SecretResolutionError,
    _check_payload_field,
    register_builtin_action,
)
from .manifest import PluginManifest, validate_manifest

#: Slack `ok:false` のエラーコード分類(MAJ-002)。HTTP は呼出側ルートで写像される。
#: - 認証/scope/Bot 設定不備 = サーバー側(Vault/Bot)設定の問題 → SecretResolutionError(503)。
_SLACK_AUTH_CONFIG_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "missing_scope",
        "not_allowed_token_type",
        "ekm_access_denied",
        "team_access_not_granted",
        "org_login_required",
    }
)
#: - 上流の一時障害/スロットリング → ConnectorTransportError(502)。
_SLACK_UPSTREAM_ERRORS = frozenset(
    {
        "internal_error",
        "fatal_error",
        "service_unavailable",
        "ratelimited",
        "rate_limited",
        "request_timeout",
        "accesslimited",
    }
)
#: - **要求側の不備**(呼出側が修正できる)→ ConnectorInvokeError(400)。allowlist にし、未知コードは
#:   安全側(502)へ倒す(Slack の新規 auth/設定/一時障害コードを恒久 400 に潰さない。MAJ-002)。
_SLACK_REQUEST_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "msg_too_long",
        "no_text",
        "too_many_attachments",
        "cant_post_message",
        "cant_broadcast",
        "invalid_arguments",
        "invalid_arg_name",
        "invalid_array_arg",
        "invalid_channel",
        "messages_tab_disabled",
        "restricted_action",
        "restricted_action_read_only_channel",
        "user_not_in_channel",
        "name_taken",
    }
)


def _raise_slack_error(api: str, resp: dict[str, Any]) -> None:
    """Slack `ok:false` 応答(または不正応答)をエラーコード別に適切な例外型へ写像する(MAJ-002)。

    認証/scope/Bot 設定不備 → `SecretResolutionError`(503)、上流一時障害 → `ConnectorTransportError`
    (502)、要求側の不備(allowlist)→ `ConnectorInvokeError`(400)。**未知コードは安全側に 502**
    (新規 auth/設定/一時障害コードを恒久 400 に潰さない)。実トークンは含めない(err コードのみ)。
    """
    err = resp.get("error") if isinstance(resp, dict) else None
    code = str(err) if err else "invalid_response"
    msg = f"Slack {api} 失敗: {code}"
    if code in _SLACK_AUTH_CONFIG_ERRORS:
        # Bot トークン/scope の設定不備=サーバー側(Vault/Slack App)の問題。要求側の不備ではない。
        raise SecretResolutionError(msg)
    if code in _SLACK_REQUEST_ERRORS:
        # channel_not_found / msg_too_long 等 = 呼出側が修正できる要求不備。
        raise ConnectorInvokeError(msg)
    # 上流一時障害・**未知コード・不正/欠落応答**は安全側に上流障害(502)へ倒す。
    raise ConnectorTransportError(msg)

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
    # 1.1.0: permissions に platform:connector.invoke を追加した版(BLK-003)。(id,version)は版固定
    # スナップショットのため、旧 1.0.0 が install/publish 済みでも新権限契約を再取込できるよう版を
    # 繰り上げる(旧 manifest 起点では invoke を承認できない。MAJ-001 / ADR-0020)。
    "version": "1.1.0",
    "kind": "connector",
    "name": "Slack コネクタ",
    "description": "Slack へ通知/起動するコア同梱コネクタ(L2 MCP / builtin)。",
    "publisher": "jetuse",
    "jetuse": {"minVersion": "0.3.0"},
    # actions は Platform データに触れない(SaaS ブリッジのみ)が、コネクタを**呼ぶ権利そのもの**
    # である `platform:connector.invoke` を宣言する。これが無いと承認フロー(approve_scopes は
    # manifest.permissions に閉じる)で invoke スコープを付与できず、正規の issue_token 経由で
    # connector/invoke に提示できるトークンを発行できない(=実行経路が不達になる。BE03-BLK-003)。
    # action 固有の Platform データスコープは引き続き空(合成バリデーションは INVOKE を unused 扱い
    # しない=コネクタの呼出権として正当な宣言。ADR-0020)。
    "permissions": ["platform:connector.invoke"],
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
    # 成功は `ok is True` を厳密に要求(文字列 "false" 等の truthy 値を成功扱いしない。MAJ-001)。
    if not isinstance(resp, dict) or resp.get("ok") is not True:
        _raise_slack_error("chat.postMessage", resp if isinstance(resp, dict) else {})
    # 成功応答のスキーマ検証: channel/ts が**strip 後も非空の文字列**で揃っていなければ上流スキーマ
    # 不一致(502)。空文字だけでなく**空白のみ**(例 {"ok":true,"channel":"  ","ts":"\t"})も投稿成功に
    # 見せない(識別子は strip 後非空を要求。MIN-001/MIN-002)。
    out_channel, ts = resp.get("channel"), resp.get("ts")
    if (
        not isinstance(out_channel, str)
        or not out_channel.strip()
        or not isinstance(ts, str)
        or not ts.strip()
    ):
        raise ConnectorTransportError("Slack chat.postMessage 応答スキーマ不一致(channel/ts)")
    # 投稿の同定情報のみ返す(トークンは含めない)。
    return {"ok": True, "channel": out_channel, "ts": ts}


#: list_channels の列挙対象(public＋private)。types 未指定だと public のみで private が漏れる。
SLACK_LIST_TYPES = "public_channel,private_channel"
#: 1 ページ当たりの取得件数(Slack 推奨上限)。
SLACK_LIST_PAGE_LIMIT = 200
#: cursor ページングの安全上限(暴走防止)。超過時は `truncated: True` を明示する(無言切り捨て禁止)。
SLACK_LIST_MAX_PAGES = 50
#: 列挙全体の wall-clock 上限(秒)。遅延 Slack や cursor 異常で 50 ページ分の read timeout を積み上げ
#: ると API Gateway の 300s 上限を超え得るため、ページ間で全体期限を確認して打ち切る(MAJ-002)。
SLACK_LIST_DEADLINE_SECONDS = 120


@register_builtin_action("slack", "list_channels")
def _list_channels(req: InvokeRequest, http: HttpCaller) -> dict[str, Any]:
    """Slack `conversations.list` を列挙する。public＋private を cursor ページングで全件取得する。

    `types` を明示して private channel を含め、`next_cursor` が尽きるまで(安全上限内で)辿る。上限に
    達して未取得が残る場合は `truncated: True` を返す(無言の不完全列挙にしない。MAJ-002)。
    """
    if not req.token:  # pragma: no cover
        raise ConnectorInvokeError("Slack 操作には Bot トークンが必要")
    headers = {"Authorization": f"Bearer {req.token}", **_JSON_HEADERS}
    channels: list[dict[str, Any]] = []
    cursor = ""
    truncated = False
    start = time.monotonic()
    for page in range(SLACK_LIST_MAX_PAGES):
        # 全体 deadline は **次ページ要求を出す前** に確認する(返却後だけだと最終ページの読取で超過
        # し得る。1 ページ目は必ず実行)。超過時は未取得を残して打ち切り truncated を明示する。これで
        # 総時間は概ね deadline + 1 ページ分の read timeout(30s)に収まり API Gateway 300s 内に保つ。
        # 単一ページ内の trickle 応答の上限は httpx read timeout(非活動 30s)が担う。より厳密な
        # 単一要求デッドラインは SSRF/DoS ガードと併せ CON-03 で上乗せ。MAJ-002。
        if page > 0 and time.monotonic() - start >= SLACK_LIST_DEADLINE_SECONDS:
            truncated = True
            break
        body: dict[str, Any] = {"types": SLACK_LIST_TYPES, "limit": SLACK_LIST_PAGE_LIMIT}
        if cursor:
            body["cursor"] = cursor
        resp = http(SLACK_LIST_CHANNELS_URL, headers, body)
        if not isinstance(resp, dict) or resp.get("ok") is not True:
            _raise_slack_error("conversations.list", resp if isinstance(resp, dict) else {})
        raw_channels = resp.get("channels")
        if not isinstance(raw_channels, list):
            raise ConnectorTransportError("Slack conversations.list 応答スキーマ不一致(channels)")
        # 各要素も dict かつ id/name が **strip 後も非空の文字列**であることを厳密に検証する
        # (空文字だけでなく空白のみの id/name も拒否。黙って破棄せず、スキーマ不一致は上流障害=502
        # に倒す。MIN-001)。
        for c in raw_channels:
            if (
                not isinstance(c, dict)
                or not isinstance(c.get("id"), str)
                or not c["id"].strip()
                or not isinstance(c.get("name"), str)
                or not c["name"].strip()
            ):
                raise ConnectorTransportError(
                    "Slack conversations.list 応答スキーマ不一致(channel 要素)"
                )
            channels.append({"id": c["id"], "name": c["name"]})
        # ページング継続判定。response_metadata は欠落可・存在時は dict・next_cursor は文字列を要求
        # (破損上流を「完全な一覧(truncated:false)」に化けさせない。MIN-001)。
        meta = resp.get("response_metadata")
        if meta is None:
            cursor = ""
        elif not isinstance(meta, dict):
            raise ConnectorTransportError(
                "Slack conversations.list 応答スキーマ不一致(response_metadata)"
            )
        else:
            nxt = meta.get("next_cursor", "")
            if not isinstance(nxt, str):
                raise ConnectorTransportError(
                    "Slack conversations.list 応答スキーマ不一致(next_cursor)"
                )
            cursor = nxt
        if not cursor:
            break
    else:
        # for-else: break せず上限到達 = まだ next_cursor が残っている(未取得あり)。
        truncated = True
    return {"ok": True, "channels": channels, "truncated": truncated}
