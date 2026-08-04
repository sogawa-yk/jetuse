"""ready ゲート(specs/19 §8.1 — SP3-01)の単体テスト。

デモスコープ能力ルートは status='ready' のみ通し、provisioning/failed/deleting は
存在秘匿と同じ 404。demos CRUD メタは従来どおり(所有者は非 ready でも status を見られる)。
SP2 までの挙動不変は既存スイート(test_demo_routes ほか — 全デモ ready)が回帰として担保する。
"""

import pytest
from fastapi.testclient import TestClient

import service.demo_context as demo_context
import service.main as service_main
from service.main import app

client = TestClient(app)


def _demo(demo_id, status, visibility="private"):
    return {
        "id": demo_id, "owner_sub": "dev-user", "name": f"demo-{status}",
        "description": None, "visibility": visibility, "status": status,
        "config": {}, "created_at": "2026-07-07T00:00:00",
        "updated_at": "2026-07-07T00:00:00",
    }


DEMOS = {
    "prov": _demo("prov", "provisioning"),
    "fail": _demo("fail", "failed"),
    "del": _demo("del", "deleting"),
    "ok": _demo("ok", "ready"),
    "pub-prov": _demo("pub-prov", "provisioning", visibility="public"),
}


@pytest.fixture(autouse=True)
def fake_demos(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)


# 能力ルートの代表(閲覧系 / 所有者書き込み系 / SSE 系)。ゲートは router 共通依存なので
# 全ルート同挙動 — 代表で依存の配線を検証する。
CAPABILITY_CALLS = [
    ("GET", "/rag/files", None),
    ("GET", "/dbchat/schema", None),
    ("GET", "/db/datasets", None),
    ("POST", "/chat", {"model": "gpt-oss-120b",
                       "messages": [{"role": "user", "content": "hi"}]}),
    ("POST", "/conversations", {"model": "gpt-oss-120b"}),
]


@pytest.mark.parametrize("status_id", ["prov", "fail", "del", "pub-prov"])
@pytest.mark.parametrize("method,path,body", CAPABILITY_CALLS)
def test_non_ready_capability_routes_are_404(status_id, method, path, body):
    res = client.request(method, f"/api/demos/{status_id}{path}",
                         json=body if body else None)
    assert res.status_code == 404
    assert res.json()["detail"] == "demo not found"  # 存在秘匿と同形


def test_ready_demo_passes_gate(monkeypatch):
    """ready はゲートを通過して従来どおり動く(SP2 挙動不変)。"""
    import service.routes.rag as rag_routes

    monkeypatch.setattr(rag_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(service_main.rag, "list_files", lambda ns: [])
    monkeypatch.setattr(service_main.rag, "refresh_statuses", lambda ns, files: files)
    res = client.get("/api/demos/ok/rag/files")
    assert res.status_code == 200


def test_crud_meta_still_visible_for_owner():
    """CRUD メタは従来どおり: 所有者は provisioning/failed でも status を見られる
    (進行表示・再生成・破棄に必要 — specs/19 §8.1)。deleting は既存どおり 404。"""
    for demo_id, status in (("prov", "provisioning"), ("fail", "failed")):
        res = client.get(f"/api/demos/{demo_id}")
        assert res.status_code == 200
        assert res.json()["status"] == status
    assert client.get("/api/demos/del").status_code == 404
