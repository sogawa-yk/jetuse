"""生成 SPA 用 app-session トークン(ADR-0023 §3.5)。

AUTH_REQUIRED=true では初回 HTML・相対アセットが Bearer を送れないため、認証済みの親(ビルダー UI)が
一回性コードを発行 → 配信ルートが HttpOnly Cookie へ交換する。コード/セッションとも HMAC 署名の
ステートレストークン(新テーブル不要・多プロセスで共有可)。秘密鍵は settings(空なら fail-closed)。

- コード: 短命(既定5分)。親の Bearer 面でのみ発行(生成 SPA からは発行不可)。
- セッション: 閲覧相当(既定60分)。exp を絶対期限とする(SP3-03 では refresh 無し = 親の再発行のみ)。
- ponytail: 真の単回失効(used-code ストア)は未実装。TTL 内の再利用は同一 subject への Cookie
  再発行に留まり権限昇格にならない。厳密単回化は SP3-05(実トークン E2E 側)で追加。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from .settings import get_settings

logger = logging.getLogger("jetuse.app_session")

CODE_TTL_S = 300      # 一回性コード(数分・親のみ発行)
SESSION_TTL_S = 3600  # Cookie セッション(閲覧相当・絶対期限)


def _secret() -> bytes | None:
    s = get_settings().app_session_secret.strip()
    return s.encode() if s else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict, secret: bytes) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verify(token: str, secret: bytes) -> dict | None:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expect = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):  # 定数時間比較(署名検証)
        return None
    try:
        return json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None


def issue_code(demo_id: str, subject: str) -> str:
    """親(Bearer)が一回性コードを発行。秘密鍵未設定は fail-closed(500 相当の RuntimeError)。"""
    secret = _secret()
    if secret is None:
        raise RuntimeError("app_session_secret is not configured (.env: APP_SESSION_SECRET)")
    return _sign({"t": "code", "d": demo_id, "s": subject,
                  "exp": int(time.time()) + CODE_TTL_S}, secret)


def verify_code(code: str, demo_id: str) -> str | None:
    """一回性コード → subject。無効/期限切れ/別 demo/鍵未設定は None(fail-closed)。"""
    secret = _secret()
    if secret is None:
        return None
    p = _verify(code, secret)
    if not p or p.get("t") != "code" or p.get("d") != demo_id or p.get("exp", 0) < time.time():
        return None
    s = p.get("s")
    return s if isinstance(s, str) and s else None


def issue_session(demo_id: str, subject: str) -> str:
    secret = _secret()
    if secret is None:
        raise RuntimeError("app_session_secret is not configured (.env: APP_SESSION_SECRET)")
    return _sign({"t": "sess", "d": demo_id, "s": subject,
                  "exp": int(time.time()) + SESSION_TTL_S}, secret)


def verify_session(token: str, demo_id: str) -> str | None:
    """Cookie セッション → subject。無効/期限切れ/別 demo/鍵未設定は None(fail-closed)。"""
    secret = _secret()
    if secret is None:
        return None
    p = _verify(token, secret)
    if not p or p.get("t") != "sess" or p.get("d") != demo_id or p.get("exp", 0) < time.time():
        return None
    s = p.get("s")
    return s if isinstance(s, str) and s else None


if __name__ == "__main__":  # smoke(HMAC の往復・期限・別 demo 拒否)
    import os
    os.environ["APP_SESSION_SECRET"] = "test-secret-xyz"
    get_settings.cache_clear()
    c = issue_code("d1", "alice")
    assert verify_code(c, "d1") == "alice"
    assert verify_code(c, "d2") is None          # 別 demo 拒否
    assert verify_code(c + "x", "d1") is None     # 改竄拒否
    tok = issue_session("d1", "alice")
    assert verify_session(tok, "d1") == "alice"
    assert verify_session("garbage", "d1") is None
    print("app_session smoke OK")
