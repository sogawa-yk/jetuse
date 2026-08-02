"""外部HTTPツールのレジストリと代理実行(TOOL-01)。

デモ側が持つ素の HTTP エンドポイントを、スキーマ付きでツールとして登録し、
エージェント実行時に組込ツール(`tools.TOOLS`)と同列に配線するための口。
MCP サーバー登録(`mcp_servers.py`)とは別経路として共存する(MCP はサーバー側で
OCI が実行、こちらは JetUse が自分で HTTP を叩く)。

既存の流儀に揃えている点(新方式を発明しない):
- 所有者強制は SQL の WHERE 句(0 行 = 呼び出し側で 404)。`mcp_servers` と同じ。
- 秘密は Vault に置き **OCID だけ**を保持する。読み出しは `mcp_servers._read_secret`。
- URL 検証は `mcp_servers.validate_url` と同じ「https 必須 + 公開ホストのみ」
  (`jetuse_shared.webtools.assert_public_host`)。

秘密の**認可**(TOOL-01 レビュー review-1 の blocker):
OCID を利用者が自由に書けると、「サービスの権限で読める秘密を、利用者が指定した外部 URL へ
送らせる」経路(confused deputy)になる。そこで登録時に Vault のメタデータ(値ではない)を引き、
**設定コンパートメント内**かつ **freeform タグ `jetuse_tool_owner` が登録者と一致**する秘密
だけを受け付ける。タグの無い秘密・他人のタグの秘密・別コンパートメントの秘密は登録できない。
照合先(`COMPARTMENT_OCID`)が未設定なら登録を断る(設定漏れで認可境界を失わない)。
**この認可は実行のたびに取り直す**(登録後にタグを外した = 権限剥奪がその場で効く)。
さらに、相手が認証ヘッダを応答へ反射しても秘密が外へ出ないよう、**送った値は応答から伏せる**。

SSRF に対する構え(fail-closed):
- 登録時と**実行時の両方**でホストを検証する(登録後に DNS が私有アドレスへ向く可能性)。
- リダイレクトは追わない。3xx は失敗として扱う(転送先が内部を向きうるため)。
- 応答サイズ上限・タイムアウトを設け、**リトライしない**。上限超過は黙って切り詰めず失敗にする。
"""

import base64
import ipaddress
import json
import logging
import re
import socket
import uuid
from typing import Any
from urllib.parse import quote, quote_plus, urlparse, urlunparse

import httpx
import oracledb

from .db import connect
from .mcp_servers import _read_secret
from .tools import CODE_INTERPRETER, RAG_SEARCH, TOOLS, ToolDef, ToolError
from .webtools import SsrfBlockedError, _assert_public_host

logger = logging.getLogger("jetuse.http_tools")

# 1 エージェントに渡せる外部ツール数の上限。多すぎるとモデルの選択精度が落ちるため
# 組込ツール(現状4種)と足しても十数件に収まる値にする。
MAX_TOOLS_PER_AGENT = 8

# 代理実行の既定。リトライはしない(業務APIの二重実行を避ける)。
TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 128_000
# 1 回の読み出し量。上限判定の粒度を細かく保つ
CHUNK_BYTES = 16_384
MAX_PROPERTIES = 20
USER_AGENT = "jetuse/0.1 (agent tool proxy)"
# 相手が認証ヘッダを応答へ反射したときの伏せ字
REDACTED = "<redacted>"

# 固定ヘッダ(TOOL-02)。相手が認証以外にも必須ヘッダを持つことは珍しくないので数個だけ許す。
# 増やしすぎるとリクエストの中身を利用者に握らせることになるため、上限は小さく保つ
MAX_EXTRA_HEADERS = 5
MAX_HEADER_VALUE_LENGTH = 200
# DB の `extra_headers`(CLOB)として読む上限。上限内の JSON しか parse しない。
# **登録を通った最大構成が必ず収まる**大きさにする(1 文字が JSON で最大 6 文字 `\uXXXX` に
# 膨らむ。ここを詰めすぎると、正しく登録できたツールが読み直しで壊れた扱いになる)
MAX_HEADERS_JSON_CHARS = MAX_EXTRA_HEADERS * (MAX_HEADER_VALUE_LENGTH * 6 + 512) + 64
# DB の値が壊れていたときの印(dict でない = 実行時検証で必ず弾かれる)
INVALID_HEADERS = "<invalid>"

ALLOWED_METHODS = ("GET", "POST")
DEFAULT_AUTH_HEADER = "Authorization"
# この秘密を使ってよい所有者を示す Vault の freeform タグ(値 = AuthContext.subject)
SECRET_OWNER_TAG = "jetuse_tool_owner"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
# 引数名はツール名より緩い(相手の業務APIが camelCase や 1 文字を使うことがある)。
# ただしクエリ文字列 / JSON キーへそのまま載るので文字種は絞る
ARG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")
# 認証ヘッダ名に使わせないもの。宛先や本文の枠組みを決めるヘッダを利用者に握らせると、
# IP ピン留め(Host の上書き)やリクエストスマグリング(長さ・転送方式)の入口になる
FORBIDDEN_AUTH_HEADERS = frozenset({
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "expect", "upgrade", "te", "trailer", "keep-alive", "proxy-authorization",
    "proxy-connection",
})
# 固定ヘッダ/冪等キーに使わせないもの。上に加えて **認証と資格情報の経路**を塞ぐ
# (`authorization` / `cookie` を自由に書けると Vault 参照を迂回して秘密を平文で載せられる)
FORBIDDEN_EXTRA_HEADERS = FORBIDDEN_AUTH_HEADERS | frozenset({
    "authorization", "cookie", "set-cookie", "accept-encoding",
})
# `proxy-*` は前方一致で塞ぐ(個別列挙だと将来のヘッダを取りこぼす)
FORBIDDEN_HEADER_PREFIX = "proxy-"
# ヘッダ値は印字可能 ASCII のみ。CR/LF を混ぜられるとヘッダ/リクエストの偽装になる。
# 終端は `$` ではなく `\Z`(`$` は**末尾の LF の直前にも**一致するので "value\n" が通る)
HEADER_VALUE_RE = re.compile(r"\A[\x20-\x7e]+\Z")
# 固定ヘッダ/冪等キーのヘッダ名は RFC 9110 の token(tchar)。認証ヘッダ名の `HEADER_RE` より
# 広い(相手の API が `X_Trace` や `api.version` のような名前を要求することがある)。
# 区切り文字(`:` `,` 空白等)と制御文字は入らないので、これ自体がインジェクション対策になる
HEADER_NAME_RE = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,63}\Z")
ALLOWED_PARAM_TYPES = ("string", "number", "integer", "boolean", "object", "array")
SCALAR_PARAM_TYPES = ("string", "number", "integer", "boolean")
CONTAINER_PARAM_TYPES = ("object", "array")
# 入れ子の上限(TOOL-03・ADR-0024)。実在する業務 API の最深構成
# 「配列の中に配列」= root(1) → 配列(2) → 要素オブジェクト(3) → 配列(4) → 要素オブジェクト(5)
# を通し、そこへ 1 段だけ余裕を持たせた値。スカラの葉は段数に数えない。
MAX_SCHEMA_DEPTH = 6
# スキーマ全体のノード数(型宣言の総数)の上限。深さだけでは MAX_PROPERTIES の掛け算で
# 20^6 まで広がるので、実際の広がりはこちらで抑える。実在する業務 API の最大構成が
# 30 ノード程度なので、その 3 倍強を上限にする
MAX_SCHEMA_NODES = 100

# 組込ツールと同名を登録させない(モデルから見て同じ名前空間)
RESERVED_NAMES = frozenset(TOOLS) | {CODE_INTERPRETER, RAG_SEARCH}


class HttpToolDefError(ValueError):
    """ツール定義が受け入れられない(登録時の 400 相当)。"""


class HttpToolCallError(ToolError):
    """代理実行が失敗した。モデルには「失敗」として伝わる(切り詰めではない)。"""


def _uid() -> str:
    return str(uuid.uuid4())


# --- 検証 ---------------------------------------------------------------

def _assert_port(parsed) -> int | None:
    """ポートを検証して返す。壊れた値を保存させない(実行時に ValueError になる)。"""
    try:
        port = parsed.port
    except ValueError as e:
        raise SsrfBlockedError("URLのポート指定が不正です") from e
    if port is not None and not (1 <= port <= 65535):
        raise SsrfBlockedError("URLのポート指定が不正です")
    return port


def validate_url(url: str) -> None:
    """https 必須 + 公開ホストのみ(`mcp_servers.validate_url` と同じ流儀)。"""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise SsrfBlockedError("ツールのURLはhttpsである必要があります")
    if p.username or p.password:
        raise SsrfBlockedError("URLに認証情報を含めることはできません(秘密はVaultへ)")
    _assert_port(p)
    _assert_public_host(p.hostname)


def _count_node(counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_SCHEMA_NODES:
        raise HttpToolDefError(f"引数スキーマの要素数は{MAX_SCHEMA_NODES}個までです")


def _clean_node(spec: Any, path: str, depth: int, counter: list[int]) -> dict:
    """1 ノード(スカラ / object / array)を検証し、検証できるキーだけの写しを返す。

    `depth` はこのノードが container だった場合に占める段数(root object = 1)。
    スカラの葉は段数に数えない(業務 API の「配列の中に配列 = 2 段」を素直に数えるため)。
    """
    _count_node(counter)
    if not isinstance(spec, dict) or spec.get("type") not in ALLOWED_PARAM_TYPES:
        raise HttpToolDefError(
            f"引数 {path} の type は {'/'.join(ALLOWED_PARAM_TYPES)} のいずれかです"
        )
    t = spec["type"]
    if t in CONTAINER_PARAM_TYPES and depth > MAX_SCHEMA_DEPTH:
        raise HttpToolDefError(
            f"引数スキーマの入れ子は{MAX_SCHEMA_DEPTH}段までです: {path}"
        )
    # 検証できるキーだけを残す。未対応の JSON Schema キーワード(enum・pattern 等)を
    # そのまま通すと「モデルには制約に見えるが実行前検証は素通し」になる
    clean: dict[str, Any] = {"type": t}
    desc = spec.get("description")
    if isinstance(desc, str) and desc:
        clean["description"] = desc[:300]
    if t == "object":
        # 自由形式の object は受理しない(何が来ても検証できない)。root と違い省略も不可
        if not isinstance(spec.get("properties"), dict):
            raise HttpToolDefError(
                f"object の引数 {path} には properties が必要です(自由形式は受理しません)"
            )
        clean.update(_clean_object(spec, path, depth, counter))
    elif t == "array":
        items = spec.get("items")
        if not isinstance(items, dict):
            raise HttpToolDefError(
                f"array の引数 {path} には items(単一スキーマ)が必要です"
                "(タプル形式の items は受理しません)"
            )
        clean["items"] = _clean_node(items, f"{path}[]", depth + 1, counter)
    return clean


def _clean_object(spec: dict, path: str, depth: int, counter: list[int]) -> dict:
    """object ノードの properties / required を検証して返す(各階層で同じ強さで効かせる)。"""
    props = spec.get("properties")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise HttpToolDefError(
            f"{path or 'parameters'}.properties はオブジェクトである必要があります"
        )
    if len(props) > MAX_PROPERTIES:
        raise HttpToolDefError(f"{path or 'parameters'} の引数は{MAX_PROPERTIES}個までです")
    clean: dict[str, dict] = {}
    for key, sub in props.items():
        if not isinstance(key, str) or not ARG_NAME_RE.match(key):
            raise HttpToolDefError(f"引数名が不正です: {key}")
        clean[key] = _clean_node(sub, f"{path}.{key}" if path else key, depth + 1, counter)
    required = spec.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(r, str) or r not in clean for r in required
    ):
        raise HttpToolDefError("required は properties に存在するキーの配列である必要があります")
    return {"properties": clean, "required": list(required)}


def validate_parameters(parameters: Any) -> dict:
    """引数スキーマ(JSON Schema のサブセット)を検証して返す。

    モデルに渡す仕様であり、`tools._validate_args` が実行前検証に使う。表現力より
    「検証しきれる形だけ通す」を優先する(未知の構成を素通しさせない = fail-closed)。
    入れ子オブジェクトと配列を受理するが(TOOL-03)、**実行時に同じ強さで検証できる形**
    ——`properties` を持つ object と、単一スキーマの `items` を持つ array——に限る。
    """
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise HttpToolDefError("parameters は type=object の JSON Schema である必要があります")
    counter = [1]  # root 自身を 1 ノードとして数える
    return {"type": "object", **_clean_object(parameters, "", 1, counter)}


def assert_query_serializable(schema: dict) -> None:
    """GET ツールのスキーマがクエリ文字列にできる形か確かめる(TOOL-03)。

    GET にはボディが無く、入れ子・配列をクエリ文字列へ載せる標準の書き方は無い
    (`a[0].b=` / `a=<JSON>` 等は相手の実装次第)。JetUse が勝手な符号化を決めると
    「送ったつもりの形と相手が読む形が違う」になるので、**登録時に断る**。
    """
    for key, spec in (schema.get("properties") or {}).items():
        if spec.get("type") in CONTAINER_PARAM_TYPES:
            raise HttpToolDefError(
                f"GET ツールの引数に入れ子・配列は使えません(クエリ文字列に載せられません): {key}"
            )


def _assert_extra_header_name(name: Any, auth_lower: str) -> None:
    low = name.lower() if isinstance(name, str) else ""
    if not isinstance(name, str) or not HEADER_NAME_RE.match(name):
        raise HttpToolDefError(f"ヘッダ名が不正です: {name}")
    if low in FORBIDDEN_EXTRA_HEADERS or low.startswith(FORBIDDEN_HEADER_PREFIX):
        raise HttpToolDefError(f"このヘッダは指定できません: {name}")
    if low == auth_lower:
        # 認証ヘッダを固定ヘッダ側から書けると、Vault 参照を平文で上書きする経路になる
        raise HttpToolDefError(f"認証ヘッダと同じ名前は指定できません: {name}")


def validate_extra_headers(
    headers: Any, idempotency_header: Any, auth_header: str | None
) -> tuple[dict[str, str], str | None]:
    """固定ヘッダと冪等キーのヘッダ名を検証し、(固定ヘッダ, 冪等キー名) を返す(TOOL-02)。

    **登録時と実行時の両方**で通す。登録時だけだと、後から DB を直接書き換えられたときに
    禁止ヘッダや CR/LF が素通りする(秘密の認可を実行時に取り直すのと同じ理由)。
    """
    auth_lower = (auth_header or DEFAULT_AUTH_HEADER).lower()
    clean: dict[str, str] = {}
    if headers is not None:
        # 空の list / False / 0 を「未指定」として素通りさせない(DB を書き換えられた場合に
        # 型の違いが黙って無視されると、fail-closed の契約が崩れる)
        if not isinstance(headers, dict):
            raise HttpToolDefError("headers はヘッダ名と値のオブジェクトである必要があります")
        if len(headers) > MAX_EXTRA_HEADERS:
            raise HttpToolDefError(f"固定ヘッダは{MAX_EXTRA_HEADERS}個までです")
        for name, value in headers.items():
            _assert_extra_header_name(name, auth_lower)
            if name.lower() in {k.lower() for k in clean}:
                # 大小違いの重複を許すと、どちらが送られるかが実装依存になる
                raise HttpToolDefError(f"同じヘッダ名が重複しています: {name}")
            if not isinstance(value, str) or not HEADER_VALUE_RE.match(value):
                raise HttpToolDefError(
                    f"ヘッダ値は CR/LF を含まない印字可能 ASCII である必要があります: {name}"
                )
            if len(value) > MAX_HEADER_VALUE_LENGTH:
                raise HttpToolDefError(
                    f"ヘッダ値は{MAX_HEADER_VALUE_LENGTH}文字までです: {name}"
                )
            clean[name] = value
    if not idempotency_header:
        return clean, None
    _assert_extra_header_name(idempotency_header, auth_lower)
    if idempotency_header.lower() in {k.lower() for k in clean}:
        # 固定値と毎回変わる値が同じヘッダ名で競合する。どちらの意図か決められない
        raise HttpToolDefError(
            f"冪等キーのヘッダ名が固定ヘッダと重複しています: {idempotency_header}"
        )
    return clean, idempotency_header


def assert_secret_usable(owner: str, secret_ocid: str) -> None:
    """登録者がその Vault 秘密を使ってよいかを **Vault のメタデータ**で確かめる(値は読まない)。

    これが無いと「サービスの OCI 権限で読める任意の秘密を、利用者が指定した外部 URL へ
    送らせる」経路になる。fail-closed: 参照できない・タグが無い・タグが他人・別コンパートメント
    のいずれでも登録を断る。
    """
    import oci

    from .oci_auth import sdk_signer_args
    from .settings import get_settings

    s = get_settings()
    if not s.compartment_ocid:
        # 照合先が無いまま通すと「タグさえ合えば別コンパートメントの秘密も使える」になる
        raise HttpToolDefError(
            "COMPARTMENT_OCID が未設定のため秘密付きツールは登録できません(設定漏れ)"
        )
    args = sdk_signer_args(s.oci_region)
    args["config"] = {**args.get("config", {}), "region": s.oci_region}
    try:
        meta = oci.vault.VaultsClient(**args).get_secret(secret_ocid).data
    except Exception as e:
        logger.warning("secret metadata lookup failed: %s", type(e).__name__)
        raise HttpToolDefError(
            "指定された Vault の秘密を参照できません(OCID・リージョン・権限を確認してください)"
        ) from e
    if meta.compartment_id != s.compartment_ocid:
        raise HttpToolDefError("この秘密は本アプリのコンパートメントにありません")
    if (meta.freeform_tags or {}).get(SECRET_OWNER_TAG) != owner:
        raise HttpToolDefError(
            f"この秘密の利用が許可されていません(Vault の freeform タグ "
            f"{SECRET_OWNER_TAG} に利用者を設定してください)"
        )


def validate_definition(
    name: str, url: str, method: str, auth_header: str | None
) -> tuple[str, str]:
    """名前・メソッド・認証ヘッダ名を検証し、正規化した (method, auth_header) を返す。"""
    if not NAME_RE.match(name or ""):
        raise HttpToolDefError(
            "ツール名は英小文字で始まる 3〜48 文字の [a-z0-9_] である必要があります"
        )
    if name in RESERVED_NAMES:
        raise HttpToolDefError(f"組込ツールと同じ名前は使えません: {name}")
    m = (method or "GET").upper()
    if m not in ALLOWED_METHODS:
        raise HttpToolDefError(f"メソッドは {'/'.join(ALLOWED_METHODS)} のみです")
    h = auth_header or DEFAULT_AUTH_HEADER
    if not HEADER_RE.match(h):
        raise HttpToolDefError(f"認証ヘッダ名が不正です: {h}")
    if h.lower() in FORBIDDEN_AUTH_HEADERS:
        raise HttpToolDefError(f"このヘッダは認証ヘッダに使えません: {h}")
    validate_url(url)
    return m, h


# --- レジストリ(所有者強制は SQL の WHERE 句) ---------------------------

def _load_headers(raw: Any) -> Any:
    """DB の `extra_headers` を読む。**壊れていても例外を投げない**。

    ここで `JSONDecodeError` を上げると、壊れた 1 行で一覧 API 全体が 500 になる
    (DB を直接触られた場合の実行時拒否が、拒否ではなく障害になる)。壊れていたら
    dict でない印を返し、`validate_extra_headers` の実行時検証で必ず弾かれるようにする。
    """
    if not raw:
        return None
    if len(raw) > MAX_HEADERS_JSON_CHARS:
        # 巨大な CLOB を parse しに行かない(壊れた行 1 つでメモリを使い切らせない)
        logger.warning("http tool extra_headers too large: %d chars", len(raw))
        return INVALID_HEADERS
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("http tool has unparsable extra_headers")
        return INVALID_HEADERS
    # 列が「未設定」なら DB は NULL(上で弾いている)。JSON の null が入っているのは
    # 直接書き換えられた印なので、未設定として扱わず壊れた値として拒否する
    return INVALID_HEADERS if parsed is None else parsed


def _row_to_tool(r) -> dict[str, Any]:
    return {
        "id": r[0],
        "name": r[1],
        "description": r[2],
        "parameters": json.loads(r[3]),
        "url": r[4],
        "method": r[5],
        "auth_header": r[6],
        "auth_secret_ocid": r[7],
        # 実行時に秘密の認可を取り直すために持つ(API 応答には出さない)
        "owner_sub": r[8],
        # TOOL-02。列を足す前に登録されたツールは両方 NULL(挙動は従来どおり)
        "headers": _load_headers(r[9]),
        "idempotency_header": r[10],
    }


def _public(tool: dict[str, Any]) -> dict[str, Any]:
    """API 応答用。Vault の OCID・秘密は返さない(has_auth のみ — mcp_servers と同じ)。

    固定ヘッダは**名前だけ**返す(値は返さない)。値は DB に平文で載るため、一覧・履歴・
    スクリーンショットへ広げる面を狭くしておく(ADR-0023。秘密は従来どおり Vault 参照)。
    """
    headers = tool.get("headers")
    return {
        "id": tool["id"], "name": tool["name"], "description": tool["description"],
        "parameters": tool["parameters"], "url": tool["url"], "method": tool["method"],
        "auth_header": tool["auth_header"], "has_auth": tool["auth_secret_ocid"] is not None,
        "header_names": list(headers) if isinstance(headers, dict) else [],
        # DB の値が壊れている(直接書き換えられた等)ことを黙って隠さない。実行は必ず失敗する
        "headers_invalid": headers is not None and not isinstance(headers, dict),
        "idempotency_header": tool.get("idempotency_header"),
    }


_SELECT = (
    "SELECT id, name, description, parameters, url, method, auth_header, "
    "auth_secret_ocid, owner_sub, extra_headers, idempotency_header FROM http_tools"
)


def list_tools(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"{_SELECT} WHERE owner_sub = :o ORDER BY created_at", o=owner
        )
        return [_public(_row_to_tool(r)) for r in cur.fetchall()]


def get_tools(owner: str, ids: list[str]) -> list[dict[str, Any]]:
    """所有者のツールを id で解決する(他人の id は 0 行になる)。"""
    if not ids:
        return []
    binds = {f"id{i}": v for i, v in enumerate(ids[:MAX_TOOLS_PER_AGENT])}
    placeholders = ", ".join(f":id{i}" for i in range(len(binds)))
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"{_SELECT} WHERE owner_sub = :o AND id IN ({placeholders})",
            o=owner, **binds,
        )
        return [_row_to_tool(r) for r in cur.fetchall()]


def create_tool(
    owner: str,
    name: str,
    description: str,
    parameters: dict,
    url: str,
    method: str = "GET",
    auth_header: str | None = None,
    auth_secret_ocid: str | None = None,
    headers: dict[str, str] | None = None,
    idempotency_header: str | None = None,
) -> dict[str, Any]:
    m, h = validate_definition(name, url, method, auth_header)
    schema = validate_parameters(parameters)
    if m == "GET":
        assert_query_serializable(schema)
    extra, idem = validate_extra_headers(headers, idempotency_header, h)
    if auth_secret_ocid:
        assert_secret_usable(owner, auth_secret_ocid)
    tid = _uid()
    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO http_tools(
                  id, owner_sub, name, description, parameters, url, method,
                  auth_header, auth_secret_ocid, extra_headers, idempotency_header)
                VALUES (:id, :o, :n, :d, :p, :u, :m, :h, :a, :x, :i)
                """,
                id=tid, o=owner, n=name, d=description[:1000],
                p=json.dumps(schema, ensure_ascii=False), u=url[:1000], m=m,
                h=h, a=auth_secret_ocid,
                x=json.dumps(extra, ensure_ascii=False) if extra else None, i=idem,
            )
            conn.commit()
    except oracledb.IntegrityError as e:
        raise HttpToolDefError(f"同じ名前のツールが既に登録されています: {name}") from e
    return _public({
        "id": tid, "name": name, "description": description, "parameters": schema,
        "url": url, "method": m, "auth_header": h, "auth_secret_ocid": auth_secret_ocid,
        "headers": extra, "idempotency_header": idem,
    })


def delete_tool(owner: str, tid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM http_tools WHERE id = :id AND owner_sub = :o", id=tid, o=owner
        )
        conn.commit()
        return cur.rowcount > 0


# --- 代理実行 -----------------------------------------------------------

def _query_params(args: dict) -> dict[str, str]:
    """GET のクエリ文字列へ。値はスカラのみ(登録時の `assert_query_serializable` で担保)。

    DB を直接書き換えられて入れ子が入り込んだ場合に `str(dict)` を送らない
    (Python の repr が相手へ飛ぶ = 黙って壊れた値を送る)。ここでも fail-closed。
    """
    out: dict[str, str] = {}
    for k, v in args.items():
        if isinstance(v, (dict, list)):
            raise HttpToolCallError(
                f"ツール実行に失敗しました: GET の引数に入れ子・配列は使えません({k})"
            )
        out[k] = "true" if v is True else "false" if v is False else str(v)
    return out


def _auth_headers(tool: dict) -> tuple[dict[str, str], str]:
    """Vault から秘密を読んでヘッダに載せる。戻り値は (ヘッダ, 秘密の実値)。

    **実行のたびに認可を取り直す**(登録後にタグを外した = 権限剥奪が効くように)。
    秘密の実値は呼び出し側が応答の伏せ字に使う。ログにも応答にも出さない。
    """
    ocid = tool.get("auth_secret_ocid")
    if not ocid:
        return {}, ""
    assert_secret_usable(tool.get("owner_sub") or "", ocid)
    value = _read_secret(ocid)
    return {tool.get("auth_header") or DEFAULT_AUTH_HEADER: value}, value


def _pin_target(url: str) -> tuple[str, str]:
    """名前解決を1回だけ行い、**検証したその IP へ接続する** URL を作る。

    「検証時に公開 IP・接続時に内部 IP」を返す DNS リバインディングを塞ぐ。戻り値は
    (IP に差し替えた URL, 元のホスト:ポート)。呼び出し側は Host ヘッダと SNI に後者を使う
    ので、TLS の証明書検証は本来のホスト名に対して行われる(実測: OCI Object Storage /
    postman-echo とも 通常どおり検証が通る)。

    判定規則は共有の `assert_public_host` をそのまま使う(IP リテラルを渡す)。解決された
    アドレスが**1つでも**内部を指していれば拒否する。
    """
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise SsrfBlockedError("ツールのURLはhttpsである必要があります")
    if p.username or p.password:
        raise SsrfBlockedError("URLに認証情報を含めることはできません(秘密はVaultへ)")
    port = _assert_port(p)
    try:
        infos = socket.getaddrinfo(p.hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SsrfBlockedError(f"DNS resolution failed: {p.hostname}") from e
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    if not addrs:
        raise SsrfBlockedError(f"DNS resolution failed: {p.hostname}")
    for a in addrs:
        _assert_public_host(a)
    # IPv4 を優先(到達性が広い)。無ければ解決順の先頭
    chosen = next(
        (a for a in addrs if isinstance(ipaddress.ip_address(a), ipaddress.IPv4Address)),
        addrs[0],
    )
    literal = f"[{chosen}]" if ":" in chosen else chosen
    netloc = f"{literal}:{port}" if port else literal
    return urlunparse(p._replace(netloc=netloc)), p.netloc


def _redaction_forms(secret: str) -> list[str]:
    """応答本文に現れうる秘密の表現(生 / JSON エスケープ / URL エンコード / Base64 / hex)。

    完全な保証ではない(敵対的な相手は任意に変形できる)。現実の反射
    ——エコー・デバッグ用エンドポイントがヘッダをそのまま返す——を確実に潰すのが目的。
    """
    if len(secret) < 4:
        return [secret]
    forms = {
        secret,
        json.dumps(secret)[1:-1],
        quote(secret, safe=""),
        quote_plus(secret),
        base64.b64encode(secret.encode()).decode(),
        secret.encode().hex(),
    }
    return sorted((f for f in forms if len(f) >= 4), key=len, reverse=True)


def _too_large() -> str:
    return ("ツール実行に失敗しました: 応答が上限"
            f"({MAX_RESPONSE_BYTES}バイト)を超えました")


def _client() -> httpx.Client:
    """代理実行用クライアント。リトライなし・リダイレクト追随なし(テストの差し替え点)。"""
    return httpx.Client(follow_redirects=False, timeout=TIMEOUT_SECONDS)


def call_tool(tool: dict, args: dict) -> str:
    """登録済みツールをサーバー側で代理実行する。失敗は HttpToolCallError。

    リトライしない / リダイレクトを追わない / 上限超過は切り詰めずに失敗させる。
    """
    # 実行時の再検証(登録後の DNS 変化に対する fail-closed)と Vault 読み出し。
    # ここの失敗も「ツール実行の失敗」として同じ境界で返す(呼び出し側の扱いを分岐させない)
    try:
        target_url, host_header = _pin_target(tool["url"])
    except SsrfBlockedError as e:
        raise HttpToolCallError(f"ツール実行に失敗しました: 到達を許可しない宛先です({e})") from e
    headers = {
        "User-Agent": USER_AGENT, "Accept": "application/json, */*",
        # 圧縮を要求しない。上限は展開後のバイト数で測るので、圧縮爆弾(小さい応答が
        # 巨大に展開される)を上限判定の前に読み込む余地を減らす
        "Accept-Encoding": "identity",
    }
    # 固定ヘッダと冪等キー(TOOL-02)。**実行時にも検証する**(登録後に DB を書き換えられて
    # も禁止ヘッダ・CR/LF を素通りさせない)。組み立て順は 固定 → 冪等 → 認証 → Host で、
    # 後から入るものが必ず勝つ = 固定ヘッダで認証・宛先を上書きできない
    try:
        extra, idem = validate_extra_headers(
            tool.get("headers"), tool.get("idempotency_header"), tool.get("auth_header")
        )
    except HttpToolDefError as e:
        raise HttpToolCallError(
            f"ツール実行に失敗しました: ヘッダ定義が不正です({e})"
        ) from e
    headers.update(extra)
    if idem:
        # **呼び出しごとに JetUse が発行する**(モデルに作らせない = 使い回させない。ADR-0023)
        headers[idem] = _uid()
    try:
        auth, secret_value = _auth_headers(tool)
    except HttpToolDefError as e:
        raise HttpToolCallError(f"ツール実行に失敗しました: {e}") from e
    except Exception as e:
        logger.warning("secret read failed: %s", type(e).__name__)
        raise HttpToolCallError(
            "ツール実行に失敗しました: Vault の秘密を読み出せませんでした"
        ) from e
    headers.update(auth)
    # Host は認証ヘッダの**後**に置く。登録時にも禁止しているが、ここで固定しておけば
    # 万一の抜けでも IP ピン留めが指す origin を利用者に動かされない
    headers["Host"] = host_header
    method = tool["method"]
    # GET は引数をクエリ文字列へ、POST は JSON ボディへ(URL のパス・ホストは動かせない)
    payload: dict[str, Any] = (
        {"params": _query_params(args)} if method == "GET" else {"json": args}
    )
    try:
        with _client() as client:
            with client.stream(
                method, target_url, headers=headers,
                extensions={"sni_hostname": urlparse(tool["url"]).hostname},
                **payload,
            ) as res:
                # Location の有無に関わらず 3xx はすべて失敗にする(`is_redirect` は
                # Location 付きしか真にならず、300/304/307 等が素通りする)
                if 300 <= res.status_code < 400:
                    raise HttpToolCallError(
                        "ツール実行に失敗しました: リダイレクト応答は許可していません"
                    )
                declared = res.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    # 相手が長さを申告しているなら、1 バイトも読まずに断る
                    raise HttpToolCallError(_too_large())
                body = b""
                for chunk in res.iter_bytes(chunk_size=CHUNK_BYTES):
                    # **足す前に**測る(足してから測ると 1 チャンク分だけ上限を越えて持つ)
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise HttpToolCallError(_too_large())
                    body += chunk
                status = res.status_code
                charset = res.charset_encoding or "utf-8"
    except httpx.TimeoutException as e:
        raise HttpToolCallError(
            f"ツール実行に失敗しました: {TIMEOUT_SECONDS}秒でタイムアウトしました"
        ) from e
    except httpx.HTTPError as e:
        # 例外の詳細は秘密を含みうる経路ではないが、モデルへは種別だけ返す
        logger.warning("http tool transport error: %s", type(e).__name__)
        raise HttpToolCallError(
            f"ツール実行に失敗しました: 通信エラー({type(e).__name__})"
        ) from e
    text = body.decode(charset, errors="replace")
    for form in (_redaction_forms(secret_value) if secret_value else ()):
        # 相手が認証ヘッダを応答へ反射することがある(エコー系・デバッグ用エンドポイント)。
        # そのまま返すとモデル・UI・会話履歴へ秘密が流れるので、送った値を伏せる
        text = text.replace(form, REDACTED)
    if status >= 400:
        raise HttpToolCallError(f"ツール実行に失敗しました: HTTP {status} {text[:200]}")
    return json.dumps({"status": status, "body": text}, ensure_ascii=False)


def to_tooldef(tool: dict) -> ToolDef:
    """登録行を組込ツールと同じ `ToolDef` にする(chat.py が同列に扱えるように)。"""
    return ToolDef(
        name=tool["name"],
        label=f"外部API: {tool['name']}",
        description=tool["description"],
        parameters=tool["parameters"],
        # 承認往復で「承認したその1件」を id で名指しできるようにする(名前だけだと、
        # 承認待ちの間に同名で別 URL のツールを作り直されると別物が実行される)
        tool_id=tool["id"],
        handler=lambda args, _t=tool: call_tool(_t, args),
        # 外部の業務APIは副作用を持ちうる。既定は承認必須(auto_tools=True で自動実行)
        requires_approval=True,
    )
