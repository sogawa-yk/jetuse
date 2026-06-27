"""コネクタ実行(invoke)層 — L2 コネクタの actual body(CON-02)。

CON-01(`connector.py`/`connector_store.py`)はコネクタの **配布表現**(provider/transport/
actions/auth)の構造検証・合成バリデーション・インスタンス登録までを担った。本モジュールは
その続きとして **登録済みコネクタの action を実際に呼び出す実行経路** を提供する。これが
「Slack 等の SaaS を JetUse から呼び出す」L2 コネクタの実体である。

設計の柱:
  - **ブローカー認可を必ず通す(fail-closed)**: コネクタは「DB 認証情報を持たずに外部 SaaS/
    テナントデータへ到達する唯一の正規経路」(plan §4-3)の L2。だから invoke は必ず Platform API
    ブローカー(PAPI-01 / `platform_broker.authorize`)で **`platform:connector.invoke` ＋ action が
    宣言する Platform スコープ** を強制し、許可/拒否を `platform_broker_audit` に記録する。
    **認可は外部呼び出しより前**に行い、拒否時は Slack/MCP へ一切到達しない(外部副作用ゼロ)。
  - **実シークレットを持たない/出さない**: コネクタ定義が持つのは `secretRef`(参照名)のみ。
    実トークンは呼び出し時に **差し替え可能な `secret_resolver`** で解決する(install 時に Vault へ
    束ねる本実装は CON-03。本タスクでは実 Slack 認証を投入せず mock を注入する)。解決したトークンは
    戻り値・例外・監査・ログのいずれにも出さない。
  - **transport 別ディスパッチ**:
      builtin = コア同梱のインプロセス実行(Slack コア = `slack_connector_builtin.py`)。実 HTTP は
               差し替え可能な `http_caller` 経由(既定は実ネットワーク禁止の fail-closed。テスト/
               E2E は mock を注入)。
      mcp     = 外部 HTTPS MCP サーバー(Responses API type:"mcp")。`mcp_caller` 経由で
               `responses.create` を呼ぶ配管(呼び出し本体は差し替え可能＝単体は mock。実 MCP 接続は
               CON-03)。

新規 migration は作らない: invoke の認可監査は既存 `platform_broker_audit`(020)を再利用する
(むやみにリソースを増やさない)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import platform_broker as pb
from ..models import MODELS
from .connector import ConnectorAction, ConnectorDefinition

# --- 型 -------------------------------------------------------------------

#: secretRef(参照名)→ 実シークレット(トークン)の解決関数。install 時の Vault 束ね(CON-03)に
#: 差し替えられる継ぎ目。**実値はここで初めて現れ、外には出さない**。
SecretResolver = Callable[[str], str]

#: builtin transport の HTTP 呼び出し。(url, headers, json_body) -> レスポンス dict。
#: 既定は fail-closed(実ネットワーク禁止)。テスト/E2E は mock を注入する。
HttpCaller = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

#: mcp transport の呼び出し。(responses_mcp_tool_spec, action, payload) -> レスポンス dict。
#: 既定は Responses API(type:"mcp")への実呼び出し。テストは mock を注入する。
McpCaller = Callable[[dict[str, Any], str, dict[str, Any]], dict[str, Any]]

#: payload(channel/text など)の値の長さ上限。暴走/過大入力を境界で弾く。
MAX_PAYLOAD_FIELD_LEN = 40000

#: コネクタ invoke が常に要求する Platform スコープ(コネクタを呼ぶ権利そのもの)。
INVOKE_SCOPE = "platform:connector.invoke"

#: 既定 MCP 呼び出しに用いるモデル key。mcp transport は Responses API(type:"mcp")経由のため、
#: Responses 対応モデル(MODELS の api=="responses")が必要(chat 専用モデルは 404)。
MCP_DEFAULT_MODEL = "gpt-oss-120b"


# --- 例外 -----------------------------------------------------------------


class ConnectorInvokeError(ValueError):
    """invoke の構成不備(未知 action / payload 不正 / secret 未設定 / transport 応答異常)。"""


class ConnectorInvokeDenied(ConnectorInvokeError):
    """ブローカー認可で拒否された。`reason` は機械可読(broker の DENY 理由を引き継ぐ)。

    外部副作用が起きる前に送出される(Slack/MCP へは到達していない)。
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


# --- builtin ハンドラレジストリ -------------------------------------------

#: (provider, action) -> builtin ハンドラ。`slack_connector_builtin` が import 時に登録する。
#: ハンドラ: (req: InvokeRequest, http: HttpCaller) -> dict。トークンは req.token(外に出さない)。
_BUILTIN_HANDLERS: dict[tuple[str, str], Callable[[InvokeRequest, HttpCaller], dict[str, Any]]] = {}


def register_builtin_action(
    provider: str, action: str
) -> Callable[
    [Callable[[InvokeRequest, HttpCaller], dict[str, Any]]],
    Callable[[InvokeRequest, HttpCaller], dict[str, Any]],
]:
    """builtin コネクタの (provider, action) にインプロセスハンドラを登録するデコレータ。"""

    def deco(
        fn: Callable[[InvokeRequest, HttpCaller], dict[str, Any]],
    ) -> Callable[[InvokeRequest, HttpCaller], dict[str, Any]]:
        key = (provider, action)
        if key in _BUILTIN_HANDLERS:  # pragma: no cover - 重複登録は実装ミス
            raise RuntimeError(f"builtin handler 重複登録: {key}")
        _BUILTIN_HANDLERS[key] = fn
        return fn

    return deco


@dataclass(frozen=True)
class InvokeRequest:
    """builtin ハンドラへ渡す要求。`token` は解決済み実シークレット(**外へ出さない**)。"""

    provider: str
    action: str
    payload: dict[str, Any]
    #: auth.kind!=none のとき解決済みトークン。none のとき None。
    token: str | None = field(default=None, repr=False)  # repr に秘密を出さない


@dataclass(frozen=True)
class ConnectorInvokeResult:
    """invoke 結果。**実シークレットは含まない**。"""

    provider: str
    action: str
    transport: str
    ok: bool
    #: transport 応答(builtin=ハンドラ戻り値 / mcp=mcp_caller 戻り値)。秘密を含まない。
    output: dict[str, Any]
    #: 認可に用いた短期トークンの jti(監査 `platform_broker_audit` との突合用)。
    jti: str


# --- 既定の(fail-closed)transport 実装 -----------------------------------


def _denied_http_caller(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    """既定 HttpCaller。実ネットワークは張らない(本タスクは実 SaaS 接続を投入しない)。

    テスト/E2E は mock を注入する。実 SaaS 接続(実トークン+Vault 束ね)は CON-03。
    """
    raise ConnectorInvokeError(
        "http_caller が未設定。builtin コネクタの実 SaaS 呼び出しは http_caller の注入が必要"
        "(本タスクは実 Slack 接続を投入しない。実接続は CON-03)"
    )


def _resolve_responses_model(model_key: str) -> str:
    """model key を MODELS で解決し、Responses 対応(api=="responses")を検証して oci_id を返す。

    mcp transport は Responses API(type:"mcp")でしか動かないため、chat 専用モデルや未登録 key は
    fail-closed(`ConnectorInvokeError`)で弾く(実呼び出し前に構成ミスを検出する)。
    """
    model = MODELS.get(model_key)
    if model is None:
        raise ConnectorInvokeError(f"未知のモデル key '{model_key}'(MODELS 未登録)")
    if model.api != "responses":
        raise ConnectorInvokeError(
            f"mcp transport は Responses 対応モデルが必要(model '{model_key}' は api={model.api})"
        )
    return model.oci_id


def _default_mcp_caller(
    spec: dict[str, Any], action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """既定 McpCaller。Responses API(type:"mcp")で MCP ツールを起動する配管。

    実 MCP サーバーへの到達(エンドポイント配備+実認証)は CON-03。単体テストは mock を注入し、
    本関数自身はネットワーク呼び出しの形を持つだけ(実エンドポイント未配備の E2E では使わない)。
    model は **Responses 対応モデル**を MODELS で解決して oci_id を渡す(chat 専用既定だと 404 になる
    のを防ぐ。CON02-MAJ-002)。
    """
    from ..genai import make_inference_client

    oci_id = _resolve_responses_model(MCP_DEFAULT_MODEL)
    client = make_inference_client(with_project=True)
    instruction = (
        f"MCP ツール '{action}' を次の引数で1回だけ実行してください: "
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    resp = client.responses.create(
        model=oci_id,
        input=instruction,
        tools=[spec],
        store=False,
    )
    return {"mcp": True, "output_text": getattr(resp, "output_text", "")}


# --- 認可(fail-closed) ---------------------------------------------------


def _required_scopes(action: ConnectorAction) -> list[str]:
    """この action の invoke が要求する Platform スコープ。

    コネクタを呼ぶ権利そのもの(`platform:connector.invoke`)＋ action が宣言する Platform スコープ
    (例: search_messages の `platform:conversations.read`)。順序を固定して監査再現性を持たせる。
    """
    scopes = [INVOKE_SCOPE]
    for sc in action.permissions:
        if sc not in scopes:
            scopes.append(sc)
    return scopes


def _authorize_all(
    broker_token: str,
    scopes: list[str],
    *,
    tenant: str,
    resource: str,
    settings: Any,
) -> pb.BrokerContext:
    """必要スコープを順にブローカー強制する。1つでも拒否なら `ConnectorInvokeDenied`。

    `platform_broker.authorize` が各スコープの許可/拒否を監査に残す。**外部呼び出しより前**に
    全スコープを通すことで、拒否時に Slack/MCP へ到達しないことを保証する(fail-closed)。
    """
    ctx: pb.BrokerContext | None = None
    for scope in scopes:
        try:
            ctx = pb.authorize(
                broker_token, scope, tenant=tenant, resource=resource, settings=settings
            )
        except pb.BrokerDenied as d:
            raise ConnectorInvokeDenied(d.reason, str(d)) from d
        except pb.BrokerConfigError as e:
            # 鍵未設定も「呼べない」に倒す(fail-closed)。authorize 側で DENY 監査済み。
            raise ConnectorInvokeDenied("broker_unconfigured", str(e)) from e
    assert ctx is not None  # scopes は最低 INVOKE_SCOPE を含むため必ず設定される
    return ctx


# --- 秘密解決(実値はここで初めて現れ、外に出さない) -----------------------


def _resolve_secret(
    definition: ConnectorDefinition, secret_resolver: SecretResolver | None
) -> str | None:
    """auth.kind!=none のとき secretRef を実トークンへ解決する。none のとき None。

    secret_resolver 未設定(かつ秘密が必要)は fail-closed(`ConnectorInvokeError`)。実値はこの関数の
    外へは戻り値経由(InvokeRequest.token / mcp ヘッダ)でしか渡さず、監査・ログ・例外文には出さない。
    """
    auth = definition.auth
    if auth.kind == "none":
        return None
    if secret_resolver is None:
        raise ConnectorInvokeError(
            f"auth.kind={auth.kind} のコネクタは secret_resolver の注入が必要"
            "(secretRef を実トークンへ解決する。実 Vault 束ねは CON-03)"
        )
    ref = auth.secret_ref
    if not ref:  # pragma: no cover - 定義検証で kind!=none は secretRef 必須
        raise ConnectorInvokeError("auth.secretRef が無いのに認証が必要")
    token = secret_resolver(ref)
    if not token or not str(token).strip():
        # 参照名は宣言の一部(非機密)なので例外文に出してよい。実値は出さない。
        raise ConnectorInvokeError(f"secret_resolver が secretRef '{ref}' を解決できなかった")
    return str(token)


#: 実シークレットを redact するときの置換文字列。
_REDACTED = "***redacted***"


def _redact_secret(obj: Any, secret: str | None) -> Any:
    """obj 内に出現する `secret` 文字列を再帰的に伏字へ置換する(str/dict/list を走査)。

    transport(MCP サーバー / 差し替え caller)が Authorization ヘッダや spec を echo した場合でも、
    解決済みトークンが戻り値・例外文字列に残らないようにするための最終防壁。secret が None/空のとき
    は何もしない(走査コストを払わない)。
    """
    if not secret:
        return obj
    if isinstance(obj, str):
        return obj.replace(secret, _REDACTED)
    if isinstance(obj, dict):
        # キーにもトークンが混入し得るため key/value 双方を redact する。
        return {_redact_secret(k, secret): _redact_secret(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_secret(v, secret) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_redact_secret(v, secret) for v in obj)
    return obj


def _call_transport(thunk: Callable[[], Any], token: str | None) -> Any:
    """transport 呼び出しを実行し、例外に混入し得るトークンを redact して再送出する。

    ハンドラ/caller の例外メッセージ・**例外連鎖(__cause__/__context__)**のいずれにトークンが
    含まれても外へ漏らさない。連鎖は明示的に断ち(`from None` ＋ __context__ 消去)、
    メッセージ(args)は redact する。secret 非漏洩を優先する。
    """
    err: ConnectorInvokeError
    try:
        return thunk()
    except ConnectorInvokeError as e:
        # 既知の構成エラーは型を保ちつつ args を redact する。
        if token:
            e.args = tuple(_redact_secret(a, token) for a in e.args)
        err = e
    except Exception as e:
        # 元例外(str/連鎖)にトークンが含まれ得るため、新規例外へ redact 済みメッセージだけを移す。
        err = ConnectorInvokeError(f"transport 呼び出しに失敗: {_redact_secret(str(e), token)}")
    # **except ブロックの外**で連鎖を消去してから raise する。except 内で raise すると Python が
    # 処理中の元例外を __context__ に再設定してしまい(トークンを含む元例外が連鎖経由で漏れる)、
    # 事前の消去が無効化されるため(`raise ... from None` は表示抑止のみで属性は残る)。
    err.__cause__ = None
    err.__context__ = None
    raise err


# --- 公開 API: invoke -----------------------------------------------------


def invoke_connector_action(
    definition: ConnectorDefinition,
    action: str,
    payload: dict[str, Any],
    *,
    broker_token: str,
    tenant: str,
    resource: str = "",
    settings: Any = None,
    secret_resolver: SecretResolver | None = None,
    http_caller: HttpCaller | None = None,
    mcp_caller: McpCaller | None = None,
) -> ConnectorInvokeResult:
    """登録済みコネクタ定義の action を実行する。

    手順(順序が安全契約):
      1. action が定義に存在するか検証(未知 action は `ConnectorInvokeError`)。
      2. **ブローカー認可**: `platform:connector.invoke` ＋ action.permissions を `platform_broker`
         で強制(許可/拒否を監査に記録)。拒否なら `ConnectorInvokeDenied`(**外部呼び出し前**)。
      3. **秘密解決**: auth.kind!=none のとき secret_resolver(secretRef) で実トークン取得。
      4. **transport 別ディスパッチ**: builtin=インプロセスハンドラ / mcp=Responses type:"mcp"。

    `resource` は監査の resource_id に入る(E2E のマーカー突合用)。戻り値・例外・監査に
    **実シークレットは出さない**。
    """
    if not broker_token or not broker_token.strip():
        # トークンが無ければ認可不能 = 呼べない(fail-closed)。authorize を通らない経路でも
        # 「invoke は許可/拒否を必ず監査する」契約を守るため、DENY を明示記録してから拒否する
        # (Authorization 欠落の試行が監査から消えないようにする。CON02-MAJ-001)。
        pb.record_broker_access(
            plugin_id="?",
            tenant=tenant,
            scope=INVOKE_SCOPE,
            decision="DENY",
            reason="missing_token",
            resource=resource,
        )
        raise ConnectorInvokeDenied(
            "missing_token", "broker_token が空。コネクタは認可なしに呼べない"
        )
    if not tenant or not tenant.strip():
        raise ConnectorInvokeError("tenant(Project OCID)は必須")
    if not isinstance(payload, dict):
        raise ConnectorInvokeError("payload は dict でなければならない")

    # 1. action 解決(未知 action はここで弾く)。
    act = next((a for a in definition.actions if a.name == action), None)
    if act is None:
        names = sorted(a.name for a in definition.actions)
        raise ConnectorInvokeError(f"未知の action '{action}'(定義の action: {names})")

    settings = settings or pb.get_settings()

    # 2. 認可(fail-closed。外部呼び出しより前)。
    ctx = _authorize_all(
        broker_token,
        _required_scopes(act),
        tenant=tenant,
        resource=resource,
        settings=settings,
    )

    # 3. 秘密解決(実値はここから先のディスパッチにしか渡さない)。
    token = _resolve_secret(definition, secret_resolver)

    # 4. ディスパッチ。
    if definition.transport == "builtin":
        handler = _BUILTIN_HANDLERS.get((definition.provider, action))
        if handler is None:
            raise ConnectorInvokeError(
                f"builtin コネクタ {definition.provider}/{action} のハンドラが未登録"
            )
        req = InvokeRequest(
            provider=definition.provider, action=action, payload=payload, token=token
        )
        output = _call_transport(
            lambda: handler(req, http_caller or _denied_http_caller), token
        )
    else:  # transport == "mcp"
        spec: dict[str, Any] = {
            "type": "mcp",
            "server_label": definition.provider,
            "server_url": definition.endpoint,
            "require_approval": "never",
        }
        if token:
            spec["headers"] = {"Authorization": f"Bearer {token}"}
        caller = mcp_caller or _default_mcp_caller
        output = _call_transport(lambda: caller(spec, action, payload), token)

    if not isinstance(output, dict):  # pragma: no cover - transport 契約違反
        raise ConnectorInvokeError("transport 応答は dict でなければならない")

    # secret 非漏洩契約の最終強制: transport が spec/header を echo してトークンを混入させても、
    # 戻り値・例外にトークンを残さない(CON02 review-2 MAJ)。token があるときだけ走査・redact する。
    output = _redact_secret(output, token)

    return ConnectorInvokeResult(
        provider=definition.provider,
        action=action,
        transport=definition.transport,
        ok=bool(output.get("ok", True)),
        output=output,
        jti=ctx.jti,
    )


def _check_payload_field(payload: dict[str, Any], key: str, *, required: bool) -> str:
    """payload の文字列フィールドを取り出して検証する(builtin ハンドラ共通)。"""
    val = payload.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        if required:
            raise ConnectorInvokeError(f"payload.{key} は必須")
        return ""
    if not isinstance(val, str):
        raise ConnectorInvokeError(f"payload.{key} は文字列でなければならない")
    if len(val) > MAX_PAYLOAD_FIELD_LEN:
        raise ConnectorInvokeError(f"payload.{key} が長すぎる(>{MAX_PAYLOAD_FIELD_LEN})")
    return val
