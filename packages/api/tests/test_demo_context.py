"""DemoContext seam(SP1-02)の404/401マトリクス。demosリポジトリはfake、seamは実関数。

存在しない/他人のprivateは同じ404(存在秘匿)。認可判定はrequire_demoに集約(specs/17 §5)。
"""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import service.demo_context as demo_context
from jetuse_core.settings import get_settings
from service.demo_context import DemoContext, require_demo

app = FastAPI()


@app.get("/demos/{demo_id}/ctx")
def ctx_route(ctx: Annotated[DemoContext, Depends(require_demo)]):
    return {"demo_id": ctx.demo_id, "owner_sub": ctx.owner_sub, "namespace": ctx.namespace}


client = TestClient(app)

DEMOS = {
    "d1": {"id": "d1", "owner_sub": "dev-user", "name": "mine", "visibility": "private",
           "status": "ready"},
    "d2": {"id": "d2", "owner_sub": "other-user", "name": "theirs", "visibility": "private",
           "status": "ready"},
    "d3": {"id": "d3", "owner_sub": "other-user", "name": "shared", "visibility": "public",
           "status": "ready"},
}


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_owner_gets_context():
    res = client.get("/demos/d1/ctx")
    assert res.status_code == 200
    assert res.json() == {"demo_id": "d1", "owner_sub": "dev-user", "namespace": "demo_d1"}


def test_other_users_private_demo_is_404():
    assert client.get("/demos/d2/ctx").status_code == 404


def test_missing_demo_is_404():
    assert client.get("/demos/no-such/ctx").status_code == 404


def test_public_demo_passes_for_other_user():
    res = client.get("/demos/d3/ctx")
    assert res.status_code == 200
    assert res.json()["owner_sub"] == "other-user"


def test_unauthenticated_is_401(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    assert client.get("/demos/d1/ctx").status_code == 401
