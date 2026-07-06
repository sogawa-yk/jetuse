"""Demo CRUD ルート(SP2-01 / specs/18 §2)の単体テスト。

demos リポジトリは in-memory fake、ルート・スキーマ・require_demo seam は実物。
DELETE は公開しない(specs/18 §2.1 — 後始末込みで SP2-02)。
"""

import pytest
from fastapi.testclient import TestClient

import jetuse_core.demos as demos_repo
from jetuse_core.nl2sql import DEFAULT_SELECT_AI_MODEL
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


class FakeDemos:
    """jetuse_core.demos と同じ契約の in-memory 実装(SQL の所有者強制を再現)。"""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.seq = 0

    def _now(self) -> str:
        self.seq += 1
        return f"2026-07-06T00:00:{self.seq:02d}"

    def create_demo(self, owner, name, description=None, visibility="private", config=None):
        d = {
            "id": f"d{len(self.rows) + 1}", "owner_sub": owner, "name": name[:200],
            "description": description, "visibility": visibility, "status": "ready",
            "config": dict(config or {}), "created_at": self._now(),
        }
        d["updated_at"] = d["created_at"]
        self.rows[d["id"]] = d
        return dict(d)

    def get_demo(self, demo_id):
        r = self.rows.get(demo_id)
        return dict(r) if r else None

    def list_demos(self, owner):
        mine = [dict(r) for r in self.rows.values() if r["owner_sub"] == owner]
        return sorted(mine, key=lambda r: r["updated_at"], reverse=True)

    def update_demo(self, owner, demo_id, fields):
        r = self.rows.get(demo_id)
        if not r or r["owner_sub"] != owner:
            return None
        r.update(fields)
        r["updated_at"] = self._now()
        return dict(r)


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    fake = FakeDemos()
    for name in ("create_demo", "get_demo", "list_demos", "update_demo"):
        monkeypatch.setattr(demos_repo, name, getattr(fake, name))
    yield fake


DEMO_OUT_KEYS = {"id", "name", "description", "visibility", "status", "config",
                 "created_at", "updated_at", "mine"}


def test_crud_roundtrip_and_demo_out_shape():
    res = client.post("/api/demos", json={
        "name": "デモ", "description": "説明",
        "config": {"dbchat": {"model": DEFAULT_SELECT_AI_MODEL}, "opaque": [1, 2]},
    })
    assert res.status_code == 200
    body = res.json()
    assert set(body) == DEMO_OUT_KEYS  # owner_sub は返さない(§2.2)
    assert body["status"] == "ready" and body["mine"] is True
    assert body["visibility"] == "private"  # 既定
    assert body["config"]["opaque"] == [1, 2]  # 不透明キーは保存・返却のみ
    did = body["id"]

    listed = client.get("/api/demos").json()["demos"]
    assert [d["id"] for d in listed] == [did]
    assert set(listed[0]) == DEMO_OUT_KEYS

    got = client.get(f"/api/demos/{did}").json()
    assert got["name"] == "デモ" and got["mine"] is True

    patched = client.patch(f"/api/demos/{did}", json={"name": "改名"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "改名"
    assert patched.json()["description"] == "説明"  # 省略 = 変更しない


def test_delete_route_is_not_exposed(fake_repo):
    d = fake_repo.create_demo("dev-user", "x")
    assert client.delete(f"/api/demos/{d['id']}").status_code == 405


def test_list_is_own_only_and_updated_at_desc(fake_repo):
    a = fake_repo.create_demo("dev-user", "old")
    fake_repo.create_demo("other-user", "not-mine")
    b = fake_repo.create_demo("dev-user", "new")
    fake_repo.create_demo("other-user", "not-mine-pub", visibility="public")
    listed = client.get("/api/demos").json()["demos"]
    assert [d["id"] for d in listed] == [b["id"], a["id"]]  # 自分のみ・updated_at DESC


def test_cross_user_404_same_shape_as_missing(fake_repo):
    theirs = fake_repo.create_demo("other-user", "private-demo")
    r_theirs = client.get(f"/api/demos/{theirs['id']}")
    r_missing = client.get("/api/demos/no-such-id")
    assert r_theirs.status_code == r_missing.status_code == 404
    assert r_theirs.json() == r_missing.json() == {"detail": "demo not found"}  # 存在秘匿
    assert client.patch(
        f"/api/demos/{theirs['id']}", json={"name": "hijack"}
    ).status_code == 404


def test_public_demo_readable_but_not_writable(fake_repo):
    pub = fake_repo.create_demo("other-user", "shared", visibility="public")
    got = client.get(f"/api/demos/{pub['id']}")
    assert got.status_code == 200
    assert got.json()["mine"] is False
    assert client.patch(f"/api/demos/{pub['id']}", json={"name": "x"}).status_code == 404
    # 公開デモの横断一覧は SP4 まで作らない(一覧は自分のみ)
    assert client.get("/api/demos").json()["demos"] == []


def test_unauthenticated_is_401(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        assert client.get("/api/demos").status_code == 401
        assert client.post("/api/demos", json={"name": "x"}).status_code == 401
    finally:
        get_settings.cache_clear()


def test_status_and_owner_sub_are_not_patchable(fake_repo):
    d = fake_repo.create_demo("dev-user", "x")
    res = client.patch(f"/api/demos/{d['id']}",
                       json={"status": "failed", "owner_sub": "attacker", "id": "zz"})
    assert res.status_code == 200  # 入力スキーマ非包含 = 無視(空 PATCH と同じ)
    assert res.json()["status"] == "ready"
    assert fake_repo.rows[d["id"]]["owner_sub"] == "dev-user"
    # POST でも status は指定不可(即 ready)
    res = client.post("/api/demos", json={"name": "y", "status": "provisioning"})
    assert res.json()["status"] == "ready"


def test_deleting_demo_is_404(fake_repo):
    d = fake_repo.create_demo("dev-user", "x")
    fake_repo.rows[d["id"]]["status"] = "deleting"
    assert client.get(f"/api/demos/{d['id']}").status_code == 404
    assert client.patch(f"/api/demos/{d['id']}", json={"name": "y"}).status_code == 404


def test_config_over_1mb_is_422(fake_repo):
    big = {"blob": "x" * 1_048_577}
    assert client.post("/api/demos", json={"name": "x", "config": big}).status_code == 422
    d = fake_repo.create_demo("dev-user", "x")
    assert client.patch(f"/api/demos/{d['id']}", json={"config": big}).status_code == 422


def test_config_must_be_object():
    for bad in ([1, 2], "str", 5):
        assert client.post("/api/demos", json={"name": "x", "config": bad}).status_code == 422


def test_config_nan_infinity_is_422(fake_repo):
    """json.loads は NaN/Infinity リテラルを受理するが正規 JSON でない(review-1 M002)。
    クライアント側の json= は送信前に落ちるため、生ボディで非正規 JSON を送る。"""
    hdr = {"Content-Type": "application/json"}
    for lit in ("NaN", "Infinity", "-Infinity"):
        body = f'{{"name": "x", "config": {{"v": {lit}}}}}'
        assert client.post("/api/demos", content=body, headers=hdr).status_code == 422, lit
    d = fake_repo.create_demo("dev-user", "x")
    assert client.patch(f"/api/demos/{d['id']}", content='{"config": {"v": NaN}}',
                        headers=hdr).status_code == 422


def test_get_reapplies_authorization_on_refetch(fake_repo, monkeypatch):
    """require_demo 通過後の再取得に認可を再適用(TOCTOU — review-1 B002)。

    認可時 public だった行が応答用再取得までに private + 秘密 config になっても
    非所有者へ 200 で漏らさない。ready→deleting の競合も同様に 404。
    """
    pub = fake_repo.create_demo("other-user", "shared", visibility="public")

    real_get = fake_repo.get_demo
    calls = {"n": 0}

    def racing_get(demo_id):
        calls["n"] += 1
        d = real_get(demo_id)
        if d and calls["n"] >= 2:  # 1回目=require_demo(公開) / 2回目以降=応答用(私有化済み)
            d.update(visibility="private", config={"secret": "x"})
        return d

    monkeypatch.setattr(demos_repo, "get_demo", racing_get)
    assert client.get(f"/api/demos/{pub['id']}").status_code == 404

    calls["n"] = 0
    mine = fake_repo.create_demo("dev-user", "mine")

    def deleting_get(demo_id):
        calls["n"] += 1
        d = real_get(demo_id)
        if d and calls["n"] >= 2:  # 応答用再取得までに deleting へ遷移
            d.update(status="deleting")
        return d

    monkeypatch.setattr(demos_repo, "get_demo", deleting_get)
    assert client.get(f"/api/demos/{mine['id']}").status_code == 404


def test_empty_patch_returns_current_without_touching_updated_at(fake_repo):
    d = fake_repo.create_demo("dev-user", "x", description="d")
    res = client.patch(f"/api/demos/{d['id']}", json={})
    assert res.status_code == 200
    assert res.json()["name"] == "x"
    assert res.json()["updated_at"] == d["updated_at"]  # 変えない(§2.2)


def test_patch_explicit_null_semantics(fake_repo):
    d = fake_repo.create_demo("dev-user", "x", description="desc")
    # name / visibility / config の明示 null は 422(DB 上 NOT NULL — §2.2)
    for field in ("name", "visibility", "config"):
        res = client.patch(f"/api/demos/{d['id']}", json={field: None})
        assert res.status_code == 422, field
    # description の明示 null はクリア
    res = client.patch(f"/api/demos/{d['id']}", json={"description": None})
    assert res.status_code == 200
    assert res.json()["description"] is None


@pytest.mark.parametrize("bad_dbchat", [None, [], "text", 42])
def test_dbchat_must_be_object_on_post_and_patch(fake_repo, bad_dbchat):
    body = {"name": "x", "config": {"dbchat": bad_dbchat}}
    assert client.post("/api/demos", json=body).status_code == 422
    d = fake_repo.create_demo("dev-user", "x")
    res = client.patch(f"/api/demos/{d['id']}", json={"config": {"dbchat": bad_dbchat}})
    assert res.status_code == 422


@pytest.mark.parametrize("bad_model", [None, 123, ["a"], "no.such-model"])
def test_dbchat_model_invalid_is_422_on_post_and_patch(fake_repo, bad_model):
    cfg = {"dbchat": {"model": bad_model}}
    assert client.post("/api/demos", json={"name": "x", "config": cfg}).status_code == 422
    d = fake_repo.create_demo("dev-user", "x")
    assert client.patch(f"/api/demos/{d['id']}", json={"config": cfg}).status_code == 422


def test_dbchat_model_omitted_or_valid_is_accepted():
    # dbchat 自体の省略・model の省略は既定(§2.2)
    assert client.post("/api/demos", json={"name": "a"}).status_code == 200
    assert client.post(
        "/api/demos", json={"name": "b", "config": {"dbchat": {}}}
    ).status_code == 200
    res = client.post(
        "/api/demos",
        json={"name": "c", "config": {"dbchat": {"model": DEFAULT_SELECT_AI_MODEL}}},
    )
    assert res.status_code == 200
    assert res.json()["config"]["dbchat"]["model"] == DEFAULT_SELECT_AI_MODEL


def test_name_constraints():
    assert client.post("/api/demos", json={}).status_code == 422  # name 必須(POST)
    assert client.post("/api/demos", json={"name": ""}).status_code == 422
    assert client.post("/api/demos", json={"name": "x" * 201}).status_code == 422
    assert client.post("/api/demos", json={"name": "x", "description": "y" * 1001}
                       ).status_code == 422
