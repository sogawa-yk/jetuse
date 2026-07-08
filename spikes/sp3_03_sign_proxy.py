"""SP3-03 ADR 検証: IAM 署名注入プロキシ（specs/19 §4.4-2 / 比較 軸B B1）。

OpenCode（OpenAI 互換 API key 認証のみ）から OCI GenAI（IAM 署名必須）へ到達させる
薄いリバースプロキシ。サンドボックス側は base_url=このプロキシ + ダミー key で鍵レス（S2）。

  OpenCode --(plain HTTP)--> 127.0.0.1:8765/v1/chat/completions --(IAM署名+Compartment)--> 大阪 GenAI

**転送は chat.completions の完全一致 allowlist に限定**（codex-review R9-F001）。生成相は
非信頼で bash 実行可のため、任意パス透過だと Files/Responses/Conversations/Vector Stores 等を
API の広い OCI 権限で叩けて「他の箱・OCI へ横移動不可」の主張が崩れる。よって:
  - パスは chat/completions のみ（前方一致・`..`・別エンドポイントを構造で拒否）
  - method は POST のみ、Content-Type=application/json のみ（R10-F007）、model は allowlist 検証
  - 本文サイズ上限は **Content-Length 事前拒否 + ストリーム累積で強制**（R10-F002 — 非信頼側は
    Content-Length なし chunked で任意サイズを送れるため body() 全読み前に打ち切る）
  - OCI スコープ/署名関連ヘッダはクライアント入力から受けずサーバ側で固定
  - 応答は content-encoding をデコードして転送（上流が identity を無視しても壊れない — R9-F009）

使い方（環境依存値は .env 管理 — GENAI_BASE_URL / COMPARTMENT_OCID は必須。未設定は起動時 fail-fast）:
  GENAI_BASE_URL=... COMPARTMENT_OCID=... [GENAI_MODEL_ALLOWLIST=openai.gpt-oss-120b] \
    .venv/bin/uvicorn spikes.sp3_03_sign_proxy:app --port 8765
"""

import json
import os

import httpx
from oci_genai_auth import OciUserPrincipalAuth
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

UPSTREAM = os.environ["GENAI_BASE_URL"]
COMPARTMENT = os.environ["COMPARTMENT_OCID"]

_ALLOWED_PATH = "chat/completions"  # 生成相（OpenCode）が必要とする唯一のエンドポイント
# 既定はフル生成を実証した 120b のみ（R10-F006 — 20b/llama は疎通のみ。追加は env で明示）。
_ALLOWED_MODELS = {
    m.strip()
    for m in os.environ.get("GENAI_MODEL_ALLOWLIST", "openai.gpt-oss-120b").split(",")
    if m.strip()
}
_MAX_BODY = 256 * 1024  # 256KiB（プロンプト+プランで十分。過大本文は 413）
# クライアントから転送してよいのは通信メタのみ。OCI スコープ/署名/認可ヘッダは受けない。
_FORWARD_REQ_HEADERS = {"content-type", "accept"}

client = httpx.AsyncClient(auth=OciUserPrincipalAuth(), timeout=300.0)


def _reject_headers(path: str, method: str, content_type: str):
    """ヘッダのみで判定（本文を読む前）。違反なら (status, msg)。純粋関数（自己検査対象）。"""
    if method != "POST":
        return 405, "method_not_allowed"
    if path != _ALLOWED_PATH:
        return 403, "path_not_allowed"
    if content_type.split(";")[0].strip().lower() != "application/json":
        return 415, "unsupported_media_type"
    return None


def _no_dup(pairs):
    """重複キーを拒否（R13-F004 — パーサ差分攻撃: allowlist 検査した model と上流実行 model の不一致）。"""
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate_key")
        d[k] = v
    return d


def _validate_body(body: bytes):
    """本文を検証し (reject, canonical) を返す。reject=(status,msg) or None、canonical=転送本文 or None。

    非信頼入力なので不正 UTF-8・非 object・model 非文字列・重複キーでも 500 にせず 400/403 で閉じる
    （R11-F005 / R13-F004）。上流へは**検証済みオブジェクトを正規再シリアライズ**して転送し、生 body を
    渡さない（重複キーのパーサ差分で allowlist 迂回を防ぐ）。
    """
    try:
        data = json.loads(body, object_pairs_hook=_no_dup)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return (400, "invalid_json"), None
    if not isinstance(data, dict):
        return (400, "invalid_json"), None
    model = data.get("model")
    if not isinstance(model, str) or model not in _ALLOWED_MODELS:
        return (403, "model_not_allowed"), None
    return None, json.dumps(data).encode()


async def _read_capped(request: Request):
    """Content-Length 事前拒否 + ストリーム累積で上限強制。超過は None（body() 全読みを避ける）。"""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > _MAX_BODY:  # 結合前に判定（単一巨大 chunk を溜めない — R12-F005）
            return None  # chunked/Content-Length 欠落・虚偽もここで打ち切る
        buf += chunk
    return bytes(buf)


async def proxy(request: Request):
    ct = request.headers.get("content-type", "")
    bad = _reject_headers(request.path_params["path"], request.method, ct)
    if bad:
        return JSONResponse({"error": bad[1]}, status_code=bad[0])
    body = await _read_capped(request)
    if body is None:
        return JSONResponse({"error": "body_too_large"}, status_code=413)
    bad, canonical = _validate_body(body)
    if bad:
        return JSONResponse({"error": bad[1]}, status_code=bad[0])
    # クライアントヘッダは通信メタだけ通す。Compartment/署名はサーバ側で固定（クライアント入力から独立）。
    # content-length は canonical 本文で再計算させる（生 body と長さが変わるため転送しない）。
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in _FORWARD_REQ_HEADERS and k.lower() != "content-length"
    }
    headers["content-type"] = "application/json"
    headers["CompartmentId"] = COMPARTMENT
    headers["opc-compartment-id"] = COMPARTMENT
    headers["accept-encoding"] = "identity"
    req = client.build_request(
        "POST", f"{UPSTREAM}/{_ALLOWED_PATH}", headers=headers, content=canonical
    )
    resp = await client.send(req, stream=True)
    return StreamingResponse(
        resp.aiter_bytes(),  # content-encoding をデコード済みで転送（R9-F009）
        status_code=resp.status_code,
        headers={"content-type": resp.headers.get("content-type", "application/json")},
        background=BackgroundTask(resp.aclose),
    )


app = Starlette(routes=[Route("/v1/{path:path}", proxy, methods=["GET", "POST"])])


if __name__ == "__main__":
    import asyncio

    # 負の契約の自己検査（ネットワーク不要）: allowlist 逸脱を確実に拒否する
    assert _reject_headers("files", "POST", "application/json")[0] == 403, "Files は拒否"
    assert _reject_headers("responses", "POST", "application/json")[0] == 403
    assert _reject_headers("chat/completions", "GET", "application/json")[0] == 405, "GET 拒否"
    assert _reject_headers("chat/completions", "POST", "text/plain")[0] == 415, "非JSON 拒否"
    assert _reject_headers("chat/completions", "POST", "application/json; charset=utf-8") is None
    assert _validate_body(b"not-json")[0][0] == 400
    assert _validate_body(b'{"model":"evil"}')[0][0] == 403
    _rej, _canon = _validate_body(b'{"model":"openai.gpt-oss-120b"}')
    assert _rej is None and b"openai.gpt-oss-120b" in _canon  # canonical 再シリアライズ
    # R11-F005: 不正 UTF-8・非 object・model 非文字列でも 500 にしない
    assert _validate_body(b"\xff\xfe")[0][0] == 400, "不正 UTF-8 は 400"
    assert _validate_body(b"[]")[0][0] == 400, "非 object は 400"
    assert _validate_body(b'{"model":[]}')[0][0] == 403, "model 非文字列は 403"
    assert _validate_body(b"{}")[0][0] == 403, "model 欠落は 403"
    # R13-F004: 重複キーはパーサ差分回避のため 400（allowlist model と上流実行 model の不一致を防ぐ）
    assert _validate_body(b'{"model":"openai.gpt-oss-120b","model":"evil"}')[0][0] == 400

    # R11-F007: _read_capped を chunked / Content-Length 欠落・虚偽・境界で検査
    class _FakeReq:
        def __init__(self, headers, chunks):
            self.headers = headers
            self._chunks = chunks

        async def stream(self):
            for c in self._chunks:
                yield c

    async def _check_capped():
        # CL 無し・chunked・上限内
        r = await _read_capped(_FakeReq({}, [b"a" * 100, b"b" * 100]))
        assert r is not None and len(r) == 200
        # CL 無し・stream 累積で超過 → 打ち切り None
        assert await _read_capped(_FakeReq({}, [b"a" * _MAX_BODY, b"b"])) is None
        # 単一巨大 chunk → 結合前に打ち切り None（R12-F005）
        assert await _read_capped(_FakeReq({}, [b"a" * (_MAX_BODY + 1)])) is None
        # CL を小さく偽装しても stream 累積で超過を捕捉
        assert await _read_capped(_FakeReq({"content-length": "10"}, [b"a" * (_MAX_BODY + 1)])) is None
        # CL 申告が上限超 → stream 前に拒否
        assert await _read_capped(_FakeReq({"content-length": str(_MAX_BODY + 1)}, [b"x"])) is None
        # 境界ちょうど → OK
        r = await _read_capped(_FakeReq({}, [b"a" * _MAX_BODY]))
        assert r is not None and len(r) == _MAX_BODY

    asyncio.run(_check_capped())
    print("sign-proxy self-check OK (headers/body/_read_capped); default models =", sorted(_ALLOWED_MODELS))
