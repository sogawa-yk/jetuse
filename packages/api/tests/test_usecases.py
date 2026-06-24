"""ユースケースエンジン(UC-01)のAPIテスト。リポジトリはfake、SSRFガードは実関数。"""

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.usecases_builtin import BUILTIN_USECASES
from jetuse_core.webtools import SsrfBlockedError, _assert_public_host
from service.main import app

client = TestClient(app)

VALID_DEF = {
    "name": "テスト要約",
    "description": "test",
    "fields": [{"name": "text", "label": "本文", "type": "textarea", "required": True}],
    "template": "要約して: {{text}}",
}


class FakeUcRepo:
    def __init__(self):
        self.store: dict[str, dict] = {}

    def list_usecases(self, owner):
        return [
            {"id": u["id"], "name": u["name"], "builtin": True} for u in BUILTIN_USECASES
        ] + [
            {"id": k, "name": v["name"], "builtin": False}
            for k, v in self.store.items()
            if v["owner"] == owner or v.get("visibility") == "public"
        ]

    def get_usecase(self, owner, uc_id):
        for u in BUILTIN_USECASES:
            if u["id"] == uc_id:
                return {**u, "owner_sub": None}
        v = self.store.get(uc_id)
        if not v or (v["owner"] != owner and v.get("visibility") != "public"):
            return None
        return {**v, "id": uc_id, "builtin": False}

    def create_usecase(self, owner, definition):
        uc_id = f"u{len(self.store) + 1}"
        self.store[uc_id] = {**definition, "owner": owner}
        return {**definition, "id": uc_id}

    def update_usecase(self, owner, uc_id, definition):
        v = self.store.get(uc_id)
        if not v or v["owner"] != owner:
            return None
        self.store[uc_id] = {**definition, "owner": owner}
        return {**definition, "id": uc_id}

    def delete_usecase(self, owner, uc_id):
        v = self.store.get(uc_id)
        if not v or v["owner"] != owner:
            return False
        del self.store[uc_id]
        return True


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    repo = FakeUcRepo()
    for name in (
        "list_usecases", "get_usecase", "create_usecase", "update_usecase",
        "delete_usecase",
    ):
        monkeypatch.setattr(service_main.uc_repo, name, getattr(repo, name))
    yield repo


def test_usecase_crud(fake_repo):
    res = client.post("/api/usecases", json=VALID_DEF)
    assert res.status_code == 200
    uc_id = res.json()["id"]
    assert any(u["id"] == uc_id for u in client.get("/api/usecases").json()["usecases"])
    assert client.get(f"/api/usecases/{uc_id}").status_code == 200
    upd = {**VALID_DEF, "name": "改名"}
    assert client.put(f"/api/usecases/{uc_id}", json=upd).json()["name"] == "改名"
    assert client.delete(f"/api/usecases/{uc_id}").json() == {"deleted": True}
    assert client.get(f"/api/usecases/{uc_id}").status_code == 404


def test_builtins_listed_and_gettable():
    ucs = client.get("/api/usecases").json()["usecases"]
    assert any(u["id"] == "builtin-summarize" for u in ucs)
    got = client.get("/api/usecases/builtin-summarize").json()
    assert got["builtin"] is True
    assert "{{text}}" in got["template"]


def test_validation_rejects_bad_definitions():
    bad_dup = {
        **VALID_DEF,
        "fields": [
            {"name": "a", "label": "A"},
            {"name": "a", "label": "A2"},
        ],
        "template": "{{a}}",
    }
    assert client.post("/api/usecases", json=bad_dup).status_code == 422
    bad_novar = {**VALID_DEF, "template": "変数を使わないテンプレート"}
    assert client.post("/api/usecases", json=bad_novar).status_code == 422
    bad_model = {**VALID_DEF, "model": "no-such-model"}
    assert client.post("/api/usecases", json=bad_model).status_code == 422
    bad_fieldname = {
        **VALID_DEF,
        "fields": [{"name": "1abc", "label": "x"}],
        "template": "{{1abc}}",
    }
    assert client.post("/api/usecases", json=bad_fieldname).status_code == 422
    bad_select = {
        **VALID_DEF,
        "fields": [{"name": "s", "label": "S", "type": "select", "options": [" "]}],
        "template": "{{s}}",
    }
    assert client.post("/api/usecases", json=bad_select).status_code == 422


def test_others_private_usecase_hidden(fake_repo):
    fake_repo.store["x1"] = {**VALID_DEF, "owner": "someone-else", "visibility": "private"}
    assert client.get("/api/usecases/x1").status_code == 404
    assert client.put("/api/usecases/x1", json=VALID_DEF).status_code == 404
    assert client.delete("/api/usecases/x1").status_code == 404


def test_ssrf_guard_blocks_internal_addresses():
    for host in ("169.254.169.254", "127.0.0.1", "10.0.0.1", "192.168.1.1", "localhost"):
        with pytest.raises(SsrfBlockedError):
            _assert_public_host(host)


def test_extract_url_rejects_metadata_endpoint():
    res = client.post("/api/tools/extract-url", json={"url": "http://169.254.169.254/opc/v2/"})
    assert res.status_code == 400
    res2 = client.post("/api/tools/extract-url", json={"url": "ftp://example.com/x"})
    assert res2.status_code == 400
