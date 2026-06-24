"""SEC-02: モデレーション・監査・管理エンドポイントの単体テスト"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import moderation
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_moderation_parses_verdict(monkeypatch):
    monkeypatch.setattr(
        moderation, "complete_once",
        lambda *a, **k: '{"flag": true, "category": "violence"}',
    )
    assert moderation.check_input("x") == (True, "violence")


def test_moderation_passes_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(moderation, "complete_once", boom)
    flagged, category = moderation.check_input("x")
    assert flagged is False  # 可用性優先で通す


def test_admin_usage_requires_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "someone-else")
    res = client.get("/api/admin/usage")
    assert res.status_code == 403


def test_admin_usage_allows_listed_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "dev-user")
    from jetuse_core import audit

    monkeypatch.setattr(audit, "summarize", lambda days: {"days": days, "by_feature": []})
    res = client.get("/api/admin/usage?days=7")
    assert res.status_code == 200
    assert res.json()["days"] == 7
