"""サーバ管理 config キー(specs/19 §5.3)と生成 SPA バンドル配信(§5.2)の単体テスト。

demos / bundles / demo_context seam はモック。CSP・トラバーサル防止・UUID 検証・保存温存を検査。
"""

import pytest
from fastapi.testclient import TestClient

import service.demo_context as demo_context
import service.routes.demos as droute
from service.main import app

client = TestClient(app)

BUNDLE = "12345678-1234-1234-1234-123456789abc"


# --- サーバ管理 config キー(§5.3) ---


@pytest.mark.parametrize("key", ["plan", "frontend", "generation"])
def test_post_rejects_server_managed_config_key(key):
    res = client.post("/api/demos", json={"name": "x", "config": {key: {"a": 1}}})
    assert res.status_code == 422
    assert key in res.json()["detail"]


@pytest.fixture
def owned_demo(monkeypatch):
    """require_demo_owner が通る所有デモ + 現行 config(サーバ管理キー入り)。"""
    demo = {"id": "d1", "owner_sub": "dev-user", "name": "n", "description": None,
            "visibility": "private", "status": "ready", "created_at": "t0",
            "config": {"frontend": {"bundle": BUNDLE}, "dbchat": {}}}
    monkeypatch.setattr(demo_context.demos, "get_demo", lambda i: dict(demo))
    monkeypatch.setattr(droute.demos, "get_demo", lambda i: dict(demo))
    captured = {}

    def update_demo(owner, demo_id, fields):
        captured["fields"] = fields
        return {**demo, **fields, "updated_at": "t"}

    monkeypatch.setattr(droute.demos, "update_demo", update_demo)
    return captured


def test_patch_rejects_server_managed_config_key(owned_demo):
    res = client.patch("/api/demos/d1", json={"config": {"generation": {"x": 1}}})
    assert res.status_code == 422
    assert "generation" in res.json()["detail"]


def test_patch_preserves_existing_server_key(owned_demo):
    # ユーザは config を全置換するが frontend(サーバ管理)は温存される(生成物ポインタを消さない)。
    # 検証を確実に通す値(theme)で 200 を強制 → 温存アサートを無条件に実行(422 で握り潰さない)。
    res = client.patch("/api/demos/d1", json={"config": {"theme": "dark"}})
    assert res.status_code == 200
    assert owned_demo["fields"]["config"].get("frontend") == {"bundle": BUNDLE}


# --- バンドル配信 /app(§5.2) ---


def _ready_demo(monkeypatch, config):
    demo = {"id": "d1", "owner_sub": "dev-user", "name": "n", "visibility": "private",
            "status": "ready", "config": config}
    monkeypatch.setattr(demo_context.demos, "get_demo", lambda i: dict(demo))
    monkeypatch.setattr(droute.demos, "get_demo", lambda i: dict(demo))


def test_serve_index_html_with_csp_headers(monkeypatch):
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    monkeypatch.setattr(droute.bundles, "get_object",
                        lambda ns, b, rel: b"<!doctype html>" if rel == "index.html" else None)
    res = client.get("/api/demos/d1/app/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_serve_asset_content_type(monkeypatch):
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    monkeypatch.setattr(droute.bundles, "get_object",
                        lambda ns, b, rel: b"console.log(1)" if rel == "assets/x.js" else None)
    res = client.get("/api/demos/d1/app/assets/x.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("path", ["../../etc/passwd", "a/../../b", "..", "back\\slash"])
def test_safe_rel_rejects_traversal(path):
    # ガード本体を直接検査(TestClient/httpx が ../ をクライアント側で正規化して
    # ルートに届かない問題を回避 — 検査対象は _safe_rel そのもの)。
    assert droute._safe_rel(path) is None


def test_safe_rel_absolute_and_fallback():
    assert droute._safe_rel("/etc/passwd") is None       # 絶対パス拒否
    assert droute._safe_rel("dashboard") == "index.html"  # 拡張子なし = SPA エントリ
    assert droute._safe_rel("assets/x.js") == "assets/x.js"


def test_serve_traversal_is_404(monkeypatch):
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    called = []
    monkeypatch.setattr(droute.bundles, "get_object",
                        lambda ns, b, rel: called.append(rel) or b"x")
    # %2e%2e = エンコード済みの `..`。httpx はドットセグメントとして正規化しないため
    # ルートまで到達し、_safe_rel が実際に弾くことを結合レベルでも確認する。
    res = client.get("/api/demos/d1/app/%2e%2e/%2e%2e/etc/passwd")
    assert res.status_code == 404
    assert not called          # トラバーサルはオブジェクト取得前に弾く


def test_serve_app_no_slash_redirects(monkeypatch):
    # 末尾スラッシュ無しの /app は /app/ へ 308(相対 ./assets/... がブラウザで正しく解決される)
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    res = client.get("/api/demos/d1/app", follow_redirects=False)
    assert res.status_code == 308
    assert res.headers["location"].endswith("/api/demos/d1/app/")


def test_serve_not_published_is_404(monkeypatch):
    _ready_demo(monkeypatch, {"dbchat": {}})   # frontend 未設定
    res = client.get("/api/demos/d1/app/")
    assert res.status_code == 404


def test_serve_invalid_bundle_uuid_is_404(monkeypatch):
    _ready_demo(monkeypatch, {"frontend": {"bundle": "not-a-uuid"}})
    res = client.get("/api/demos/d1/app/")
    assert res.status_code == 404


def test_serve_missing_object_is_404(monkeypatch):
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    monkeypatch.setattr(droute.bundles, "get_object", lambda ns, b, rel: None)
    assert client.get("/api/demos/d1/app/missing.js").status_code == 404


def test_serve_spa_fallback_to_index(monkeypatch):
    # 拡張子なしルート(SPA サブルート)は index.html を返す(単一エントリ)
    _ready_demo(monkeypatch, {"frontend": {"bundle": BUNDLE}})
    seen = []
    monkeypatch.setattr(droute.bundles, "get_object",
                        lambda ns, b, rel: seen.append(rel) or b"<html>")
    res = client.get("/api/demos/d1/app/dashboard")
    assert res.status_code == 200
    assert seen == ["index.html"]
