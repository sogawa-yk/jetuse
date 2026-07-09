"""生成用 IAM 署名注入プロキシ(SP3-06 — spikes/sp3_03_sign_proxy.py の後継。ADR-0023 §2)。

OpenCode(OpenAI 互換・API key 認証のみ)から OCI GenAI(IAM 署名必須)へ到達させる薄い
リバースプロキシ。SP3-03 spike の防御をそのまま維持し、生成モデルレジストリ(gen_models)による
ルーティングへ多目的化する:

  body.model(OCI id) → {リージョンエンドポイント, auth プロファイル, compartment, api 種別}

- **許可パスは chat/completions と responses の 2 つの完全一致のみ**(他は 403 —
  Files/Vector Stores 等への横移動を構造で拒否。SP3-03 R9-F001 の規律)。
- モデルの api 種別と要求パスが一致しない場合も 403(codex 系を chat/completions へ流せない)。
- 共有テナンシ(gpt-5 系)の auth プロファイル/compartment は .env。未設定なら該当モデルは
  403(fail-closed — 設定されるまで存在しないのと同じ)。
- 維持する既存防御: POST/JSON のみ・本文 256KiB 上限(Content-Length 事前拒否 + ストリーム
  累積)・重複キー拒否 + 正規再シリアライズ(パーサ差分)・ヘッダ固定(OCI スコープ/署名系は
  クライアントから受けない)・SSE 素通し・content-encoding デコード転送。

起動(環境依存値は .env — jetuse_core.settings が読む。リポジトリ root から):
  .venv/bin/uvicorn jetuse_core.sign_proxy:app --app-dir packages/api --port 8766
"""

import json
import logging

import httpx
from oci_genai_auth import OciUserPrincipalAuth
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .gen_models import GEN_MODELS_BY_OCI_ID, GenModelDef, inference_base_url
from .genai import _signer
from .settings import get_settings

logger = logging.getLogger("jetuse.sign_proxy")

# 許可パス(完全一致)→ そのパスを話せる api 種別
_PATH_API = {"chat/completions": "chat", "responses": "responses"}
_MAX_BODY = 256 * 1024  # 256KiB(プロンプト+プランで十分。過大本文は 413)
# クライアントから転送してよいのは通信メタのみ。OCI スコープ/署名/認可ヘッダは受けない。
_FORWARD_REQ_HEADERS = {"content-type", "accept"}

# auth プロファイル('' = 既定 = 自テナンシ)ごとの httpx クライアント(遅延生成・使い回し)
_clients: dict[str, httpx.AsyncClient] = {}


def _auth_for(profile: str):
    """共有テナンシ = ユーザープリンシパル(プロファイル)。自テナンシ = genai と同一の選択
    (AUTH_MODE=resource_principal なら RP — 配備コンテナに DEFAULT プロファイル不要。SP3-07)。"""
    return OciUserPrincipalAuth(profile_name=profile) if profile else _signer()


def _client(profile: str) -> httpx.AsyncClient:
    if profile not in _clients:
        _clients[profile] = httpx.AsyncClient(auth=_auth_for(profile), timeout=300.0)
    return _clients[profile]


def _route(model: GenModelDef) -> tuple[str, str, str] | None:
    """モデル → (base_url, compartment, auth プロファイル)。未設定は None(fail-closed)。"""
    s = get_settings()
    if model.shared:
        if not (s.gen_shared_profile and s.gen_shared_compartment_ocid):
            return None
        return (inference_base_url(model.region),
                s.gen_shared_compartment_ocid, s.gen_shared_profile)
    if not s.compartment_ocid:
        return None
    return (inference_base_url(model.region), s.compartment_ocid, "")


def _reject_headers(path: str, method: str, content_type: str):
    """ヘッダのみで判定(本文を読む前)。違反なら (status, msg)。純粋関数(契約テスト対象)。"""
    if method != "POST":
        return 405, "method_not_allowed"
    if path not in _PATH_API:
        return 403, "path_not_allowed"
    if content_type.split(";")[0].strip().lower() != "application/json":
        return 415, "unsupported_media_type"
    return None


def _no_dup(pairs):
    """重複キーを拒否(R13-F004 — allowlist 検査した model と上流実行 model の不一致を防ぐ)。"""
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate_key")
        d[k] = v
    return d


def _validate_body(body: bytes, path: str):
    """本文を検証し (reject, canonical, route) を返す。

    非信頼入力なので不正 UTF-8・非 object・model 非文字列・重複キーでも 500 にせず 400/403 で
    閉じる。上流へは検証済みオブジェクトの正規再シリアライズを転送(生 body を渡さない)。
    """
    try:
        data = json.loads(body, object_pairs_hook=_no_dup)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return (400, "invalid_json"), None, None
    if not isinstance(data, dict):
        return (400, "invalid_json"), None, None
    model = data.get("model")
    d = GEN_MODELS_BY_OCI_ID.get(model) if isinstance(model, str) else None
    if d is None:
        return (403, "model_not_allowed"), None, None
    if _PATH_API[path] != d.api:
        return (403, "model_api_mismatch"), None, None
    if d.api == "responses":
        # 共有テナンシへ永続状態を作らせない(書き込み系リソース禁止 — tasks/SP3-06 /
        # review-2 B001)。明示 store:true と保存済み状態を参照するパラメータは拒否し、
        # canonical では常に store=false を強制する(上流既定に依存しない fail-closed)。
        if data.get("store") is True or "conversation" in data or "previous_response_id" in data:
            return (403, "persistence_not_allowed"), None, None
        # 本上流(共有テナンシ)は ZDR: store 未指定でも実効 store=false(create 応答の echo で
        # 実機確認)= サーバ側に永続項目は作られない。canonical で明示 false に固定するのは
        # その契約の見える化(防御の重ね掛け)。
        data["store"] = False
        # ZDR ゆえ過去 turn の item は id 参照で解決できない(「Items are not persisted for
        # Zero Data Retention organizations」で 404 — E2E 実測)。@ai-sdk/openai はフローに
        # よって {"type":"item_reference","id":"rs_..."} を echo し、生成が非決定に失敗する
        # (プロキシ実測で形状特定)。encrypted_content の往復も本上流では成立しない(E2E 実測)
        # ため、サーバ保存参照を前提とする item(item_reference / reasoning)は input から
        # 落として無状態化する(function call/output・message は内容完結なので残す)。
        if isinstance(data.get("input"), list):
            data["input"] = [
                it for it in data["input"]
                if not (isinstance(it, dict)
                        and it.get("type") in ("item_reference", "reasoning"))
            ]
    route = _route(d)
    if route is None:
        return (403, "model_not_configured"), None, None
    return None, json.dumps(data).encode(), route


async def _read_capped(request: Request):
    """Content-Length 事前拒否 + ストリーム累積で上限強制。超過は None(body() 全読みを避ける)。"""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > _MAX_BODY:  # 結合前に判定(単一巨大 chunk を溜めない)
            return None
        buf += chunk
    return bytes(buf)


async def proxy(request: Request):
    path = request.path_params["path"]
    ct = request.headers.get("content-type", "")
    bad = _reject_headers(path, request.method, ct)
    if bad:
        return JSONResponse({"error": bad[1]}, status_code=bad[0])
    body = await _read_capped(request)
    if body is None:
        return JSONResponse({"error": "body_too_large"}, status_code=413)
    bad, canonical, route = _validate_body(body, path)
    if bad:
        return JSONResponse({"error": bad[1]}, status_code=bad[0])
    base_url, compartment, profile = route
    # クライアントヘッダは通信メタ(_FORWARD_REQ_HEADERS)だけ通す。Compartment/署名はサーバ側で
    # 固定。content-length は転送対象外 = canonical 本文で再計算される。
    headers = {
        k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ_HEADERS
    }
    headers["content-type"] = "application/json"
    headers["CompartmentId"] = compartment
    headers["opc-compartment-id"] = compartment
    headers["accept-encoding"] = "identity"
    client = _client(profile)
    req = client.build_request("POST", f"{base_url}/{path}", headers=headers, content=canonical)
    try:
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        return JSONResponse({"error": "upstream_timeout"}, status_code=504)
    except httpx.RequestError:  # 上流未到達をプロキシ障害(500)と区別する(review-2 m001)
        logger.warning("upstream request failed: %s", base_url, exc_info=True)
        return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
    return StreamingResponse(
        resp.aiter_bytes(),  # content-encoding をデコード済みで転送(R9-F009)
        status_code=resp.status_code,
        headers={"content-type": resp.headers.get("content-type", "application/json")},
        background=BackgroundTask(resp.aclose),
    )


app = Starlette(routes=[Route("/v1/{path:path}", proxy, methods=["GET", "POST"])])
