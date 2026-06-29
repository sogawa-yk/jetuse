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
import time
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
    """invoke の構成不備(未知 action / payload 不正 等)。HTTP では既定 400(クライアント要求の不備)。

    サブクラスで「サーバー設定/依存サービス障害(`SecretResolutionError`→503)」と「外部 SaaS への
    到達/応答障害(`ConnectorTransportError`→502)」を区別し、ルートが副作用不確定なサーバー障害を
    恒久的 400 に潰さない(監視・再試行判断を誤らせない。CON02/BE03 review MAJ-004)。
    """


class ConnectorTransportError(ConnectorInvokeError):
    """外部 SaaS/MCP への到達・応答が壊れた(ネットワーク失敗 / 非2xx / 非JSON 応答)。

    呼出要求の不備ではなく**上流(SaaS)側の障害**。HTTP では 502 に倒す。副作用(投稿等)の成否は
    不確定なので呼び出し側で安易に自動再送しない(冪等性はコネクタ/呼出側の責務)。
    """


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
    """既定 HttpCaller。実ネットワークは張らない(明示注入しない限り外部に出ない fail-closed)。

    `invoke_connector_action` の `http_caller` 既定はこれ。実 HTTP は呼び出し側(ルート/E2E)が
    `live_http_caller` を明示注入したときだけ張る。テストは mock を注入する。
    """
    raise ConnectorInvokeError(
        "http_caller が未設定。builtin コネクタの実 SaaS 呼び出しは http_caller の注入が必要"
        "(既定は fail-closed。実 HTTP は live_http_caller を明示注入する)"
    )


#: builtin transport の実 HTTP 既定 timeout(秒)。connect は短く、read は SaaS の遅延に余裕。
#: API Gateway(readTimeout 最大300秒)を超えないよう read を抑える。リトライは張らない
#: (副作用のある POST=投稿を勝手に再送しない。fail-closed で呼び出し側に返す)。
_LIVE_HTTP_TIMEOUT = (5.0, 30.0)

#: 1 要求あたりの **絶対 wall-clock 上限(秒)**。httpx の read timeout は「無通信(inactivity)時間」
#: なので、read 未満の間隔でデータを trickle する応答は単一要求で無期限に延び得る(MAJ-002)。これを
#: 塞ぐため応答ボディを**ストリーム読み**し、チャンク受信ごとに絶対期限を確認して超過でソケットを
#: 中断する(read timeout 任せにしない真の総時間上限)。list_channels のページ間 deadline(120s)と
#: 併せ、最悪でも `120s + (wall + connect)` 程度に総 wall-clock を抑え API Gateway 300s 内に保つ。
_LIVE_HTTP_WALL_DEADLINE = 60.0

#: 応答ボディの最大バイト数(暴走/メモリ枯渇防止)。Slack の投稿/一覧応答には十分大きい。超過は
#: 上流障害(502)に倒す。trickle 攻撃はサイズ・時間の両面で打ち切る。
_LIVE_HTTP_MAX_BYTES = 8 * 1024 * 1024


def live_http_caller(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    """実 HTTP(httpx)で SaaS Web API(Slack 等)を呼ぶ HttpCaller(BE-03)。

    builtin ハンドラが組み立てた (url, headers, json_body) を **JSON POST** し、JSON 応答を dict で
    返す。Slack Web API は**論理エラーでも HTTP 200** + `{"ok": false, "error": ...}` を返すため、
    2xx の応答 dict はそのままハンドラへ返し、`ok` 判定はハンドラに委ねる(契約は
    `slack_connector_builtin`)。

    **絶対 wall-clock 期限(MAJ-002)**: 応答ボディを **raw**(content-decoding 前)でストリーム読みし、
    **各ソケット読取ごと**に `_LIVE_HTTP_WALL_DEADLINE` の絶対期限とサイズ上限を確認する。期限/上限
    超過で読取を打ち切り、コンテキストマネージャ離脱でソケットを閉じて**実ソケット処理を中断**する。
    raw を使うのは、content デコーダ(gzip 等)が入力をバッファして decode 済みチャンクを yield しない
    trickle 応答でも、各 raw チャンク受信ごとに必ず期限確認へ到達させるため(decode 任せだと期限確認
    を素通りし得る)。圧縮を避けるため `Accept-Encoding: identity` を強制し、ループ終了(EOF)後にも
    期限を再確認する(最終チャンク後に EOF が遅延しても期限を守る)。これにより read timeout
    (無通信時間)を潜り抜ける trickle/遅延 EOF でも単一要求の総時間が縛られる。

    fail-closed(いずれも `ConnectorTransportError`=上流障害 → 502):
      - ネットワーク/タイムアウト/接続失敗。
      - **非 2xx ステータス**(JSON 本文が偶然 `{"ok": true}` でも成功扱いにしない。MIN-001)。
      - wall-clock 期限超過 / 応答が大きすぎる。
      - 応答が JSON でない / dict でない。
    実トークン(Authorization)は戻り値・例外に出さない(例外文は status のみ。ヘッダ・本文・URL を
    含めない。runtime 側の `_redact_secret` が最終防壁)。
    """
    import httpx

    connect, read = _LIVE_HTTP_TIMEOUT
    deadline = time.monotonic() + _LIVE_HTTP_WALL_DEADLINE
    # 圧縮を避けて raw=decode 済みを一致させ、デコーダのバッファリングで期限確認を素通りさせない
    # (caller の Accept-Encoding より identity を優先=決定的に縛る)。
    req_headers = {**headers, "Accept-Encoding": "identity"}
    # 例外メッセージ(status/型名のみ)を組み立て、**except の外**でチェーンを断って raise する。
    # httpx.RequestError は `.request`(Authorization ヘッダ・URL・本文を保持)を __cause__ 経由で
    # 露出させ得るため、本関数を直接呼んだ場合でも token/URL/本文が連鎖に残らないようにする
    # (通常 invoke 経路は `_call_transport` も連鎖を断つが、直接呼出に依存しない。BE03-MAJ-003)。
    msg: str | None = None
    data: Any = None
    try:
        with httpx.Client(timeout=httpx.Timeout(read, connect=connect)) as client:
            with client.stream("POST", url, headers=req_headers, json=body) as resp:
                if not (200 <= resp.status_code < 300):
                    # 非 2xx(3xx 含む)は本文を読まず上流障害として倒す(本文の echo はしない)。
                    msg = f"SaaS API が非 2xx を返した (status={resp.status_code})"
                else:
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_raw():
                        # 絶対期限を**各 raw ソケット読取ごと**に確認し、超過時点で読取を抜ける
                        # (with 離脱でソケットを閉じる=中断)。trickle 応答を read timeout に頼らず
                        # 縛る。
                        if time.monotonic() > deadline:
                            msg = "SaaS API 応答が wall-clock 期限を超過"
                            break
                        total += len(chunk)
                        if total > _LIVE_HTTP_MAX_BYTES:
                            msg = f"SaaS API 応答が大きすぎる (> {_LIVE_HTTP_MAX_BYTES} bytes)"
                            break
                        chunks.append(chunk)
                    # EOF で正常終了した場合も期限を再確認する(最終チャンク後に EOF が遅延しても、
                    # 期限超過なら成功扱いにしない=絶対期限を守る)。
                    if msg is None and time.monotonic() > deadline:
                        msg = "SaaS API 応答が wall-clock 期限を超過"
                    if msg is None:
                        try:
                            data = json.loads(b"".join(chunks))
                        except ValueError:
                            msg = f"SaaS API 応答が JSON でない (status={resp.status_code})"
                        else:
                            if not isinstance(data, dict):
                                msg = (
                                    "SaaS API 応答 JSON が dict でない "
                                    f"(status={resp.status_code})"
                                )
    except httpx.HTTPError as e:
        # URL・本文・ヘッダ・連鎖を例外文/属性に残さない(トークン混入回避)。型名だけ。
        msg = f"SaaS API への HTTP 呼び出しに失敗: {type(e).__name__}"
    if msg is not None:
        err = ConnectorTransportError(msg)
        err.__cause__ = None
        err.__context__ = None
        raise err
    return data


# --- Vault 秘密解決(secretRef → OCID → 実トークン。実値はコード/DB に置かない) -----


class SecretResolutionError(ConnectorInvokeError):
    """secretRef を実トークンへ解決できない(参照名未マップ / Vault 読取不可)。fail-closed。

    `ConnectorInvokeError` のサブクラスなので invoke の構成不備として扱われ、外部呼び出しは起きない
    (秘密解決は transport ディスパッチより前)。例外文に実トークン/OCID 実値は出さない。
    """


#: secretRef→OCID 対応表の合成キー区切り。鍵は `<tenant>/<plugin_id>/<connector_id>/<secretRef>`。
#: テナント＋呼出プラグイン＋**コネクタ instance** に束縛し、(a) 別プラグインの同名 secretRef 横取り
#: (confused-deputy)、(b) 別テナントでの資格情報共有/越境、(c) **同一テナント内の別 Slack 接続への
#: 誤送信/取り違え**(同一 plugin に複数 instance がある場合)を防ぐ(BLK-001)。
_SECRET_KEY_SEP = "/"


def _read_vault_secret(secret_ocid: str) -> str:
    """Vault データプレーン(SecretsClient.get_secret_bundle)で secret OCID の実値を読む(BE-03)。

    既存 `mcp_servers._read_secret` と同じデータプレーン読取。timeout を明示し retry を張らずに
    (API Gateway 60秒以内)、初期化/呼出失敗はすべて `SecretResolutionError` へ畳む(fail-closed)。
    OCI 例外は **secret OCID/endpoint/リクエスト情報を含み得る**ため、型名だけを露出し、連鎖
    (__cause__/__context__)も明示的に断つ(traceback/収集基盤へ実 OCID を残さない。BE03-MAJ-003)。
    実 Vault 読取 IAM は人間ゲート。
    """
    import base64
    import os

    import oci

    err: SecretResolutionError
    try:
        kwargs = {
            "timeout": (5, 15),
            "retry_strategy": oci.retry.NoneRetryStrategy(),
        }
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            client = oci.secrets.SecretsClient({}, signer=signer, **kwargs)
        else:
            client = oci.secrets.SecretsClient(oci.config.from_file(), **kwargs)
        bundle = client.get_secret_bundle(secret_ocid).data
        return base64.b64decode(bundle.secret_bundle_content.content).decode()
    except Exception as e:  # 署名子/設定/権限欠如/障害をすべて fail-closed へ畳む
        err = SecretResolutionError(f"Vault からの secret 読取に失敗: {type(e).__name__}")
    # except の外で連鎖を断ってから raise(元 OCI 例外を __cause__/__context__ に残さない。
    # `_call_transport` と同じ理由: except 内 raise は __context__ に元例外を再設定してしまう)。
    err.__cause__ = None
    err.__context__ = None
    raise err


def make_vault_secret_resolver(
    settings: Any, *, tenant: str, plugin_id: str, connector_id: str
) -> SecretResolver:
    """`secretRef`(論理参照名)→ Vault secret OCID → 実トークン の解決器を作る(BE-03)。

    対応表は `settings.connector_secret_ocids`(.env で JSON 注入。**実 OCID/トークンはコード/DB に
    置かない**)。鍵は **`<tenant>/<plugin_id>/<connector_id>/<secretRef>`** の合成キーで、解決を
    **テナント＋呼出プラグイン＋コネクタ instance** に束縛する:
      - 別プラグインが同名 `secretRef` を宣言しても他人の秘密を引けない(confused-deputy 防止)。
      - 別テナントが同一 plugin のトークンで他テナントの SaaS 資格情報を共有/越境できない。
      - **同一テナント内に同一 plugin の Slack 接続が複数あっても、instance ごとに別 secret を解決**
        し、別ワークスペースへの誤送信/取り違えを防ぐ(BLK-001)。未マップ鍵は fail-closed。
    未マップなら `SecretResolutionError`(→503)。Vault 復号値は**空/空白を拒否**して正規化
    (空値=サーバー側 secret 設定不備。base 400 に潰さない。MIN-002)。実トークンは戻り値だけに返り、
    以降は `InvokeRequest.token`/Authorization ヘッダにしか乗らない(監査/ログ/例外に非出力)。
    実 Vault 読取 IAM・実 Slack Bot トークン投入は人間ゲート(テストは mock 注入)。
    """
    mapping: dict[str, str] = dict(getattr(settings, "connector_secret_ocids", {}) or {})
    tid = (tenant or "").strip()
    pid = (plugin_id or "").strip()
    cid = (connector_id or "").strip()

    def _resolve(ref: str) -> str:
        if not tid or not pid or not cid:
            raise SecretResolutionError(
                "secret 解決には tenant＋plugin_id＋connector_id 束縛が必要(fail-closed)"
            )
        key = _SECRET_KEY_SEP.join((tid, pid, cid, ref))
        ocid = mapping.get(key)
        if not ocid or not str(ocid).strip():
            # 参照名・合成キー(宣言の一部・非機密)は出してよい。OCID 実値は出さない。
            raise SecretResolutionError(
                f"secretRef '{ref}' (key '{key}') に対応する Vault secret OCID が未設定"
                "(settings.connector_secret_ocids に .env で注入する)"
            )
        token = _read_vault_secret(str(ocid).strip())
        # Vault 復号値が空/空白 = サーバー側 secret 設定不備。base 400 でなく 503 へ(MIN-002)。
        normalized = (token or "").strip()
        if not normalized:
            raise SecretResolutionError(
                f"secretRef '{ref}' の Vault secret が空/空白(設定不備)"
            )
        # Bearer 不正値(内部空白/制御文字/非 ASCII)は設定不備として 503 に倒す。
        # httpx に渡して 502/400 へ化けるのを防ぎ、secret 不備=503 の契約を保つ
        # (MIN-001)。実値は例外に出さない(ref のみ)。
        bad = any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in normalized)
        if bad or not normalized.isascii():
            raise SecretResolutionError(
                f"secretRef '{ref}' の Vault secret が不正(空白/制御文字/非ASCII を含む。設定不備)"
            )
        return normalized

    return _resolve


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
    **実シークレットは出さない**。**外部副作用(秘密解決・transport)の前に必ず broker 認可を通す**
    (認可スキップのバイパスを持たない=偽造 context で外部副作用を起こせない安全契約。MAJ-002)。
    ただし spec §12.6 の順序契約に従い **未知 action・非 dict payload・空 token は認可より前**に
    弾く(外部に触れないローカル構成検証。空トークンは DENY を監査記録してから拒否)。これらの早期
    終了では外部副作用も action 固有スコープの ALLOW 監査も無い。HTTP ルートは取得前 authorize で
    connector.invoke を先に強制するため未認可からの列挙はルート層で塞がれる(多層防御。直接呼出時の
    早期 ValueError は構成不備の通知で副作用なし。review-2 MIN-003)。呼び出し側が別途認可済みでも
    二重監査は許容する(多層防御 > 単一監査の節約)。
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

    # 2. 認可(fail-closed。外部呼び出しより前)。必須スコープ(INVOKE ＋ action.permissions)を
    #    本関数で必ず全認可・監査する(バイパスなし)。
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
