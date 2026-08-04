"""プラン title/description 直接編集(SP3-05 / specs/19 §7②)の単体テスト。

編集は title/description のみ(JSON 自由編集の API は作らない — §11)。反映後に §3.3 の
validate_plan で再検証し、save_plan の楽観ロック(demo_id IS NULL + transcript 長)で保存する。
リポジトリは design テストと同じ in-memory fake。
"""

import pytest
from fastapi.testclient import TestClient
from test_builder_design import SPEC_PLAN, FakeRepo
from test_builder_sessions import FULL_REQ

import jetuse_core.builder_sessions as repo
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    fake = FakeRepo()
    for name in ("create_session", "get_session", "save_hearing_turn", "save_plan"):
        monkeypatch.setattr(repo, name, getattr(fake, name))
    yield fake


def _designed_sid(fake_repo):
    sid = client.post("/api/builder/sessions").json()["id"]
    r = fake_repo.rows[sid]
    r["requirements"] = dict(FULL_REQ)
    r["sufficient"] = True
    r["transcript"] = [
        {"role": "user", "content": "製造業のデモ"}, {"role": "assistant", "content": "了解"}]
    r["plan"] = dict(SPEC_PLAN)
    r["status"] = "designed"
    return sid


def test_patch_title_and_description(fake_repo):
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan",
                       json={"title": "新タイトル", "description": "新しい説明"})
    assert res.status_code == 200
    body = res.json()
    assert body["plan"]["title"] == "新タイトル"
    assert body["plan"]["description"] == "新しい説明"
    assert body["status"] == "designed"
    # 他フィールドは不変(title/description 以外は編集させない)
    assert body["plan"]["capabilities"] == SPEC_PLAN["capabilities"]
    assert body["plan"]["screens"] == SPEC_PLAN["screens"]


def test_patch_title_only_keeps_description(fake_repo):
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "T2"})
    assert res.status_code == 200
    assert res.json()["plan"]["title"] == "T2"
    assert res.json()["plan"]["description"] == SPEC_PLAN["description"]


def test_empty_patch_returns_current_session(fake_repo):
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={})
    assert res.status_code == 200
    assert res.json()["plan"] == SPEC_PLAN


def test_patch_without_plan_is_409(fake_repo):
    sid = client.post("/api/builder/sessions").json()["id"]
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "T"})
    assert res.status_code == 409


def test_patch_after_demo_attached_is_409(fake_repo):
    sid = _designed_sid(fake_repo)
    fake_repo.rows[sid]["demo_id"] = "d1"  # 生成開始後 = 読み取り専用
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "T"})
    assert res.status_code == 409


def test_cross_user_is_404(fake_repo, monkeypatch):
    sid = _designed_sid(fake_repo)
    fake_repo.rows[sid]["owner_sub"] = "other-user"  # 越境 = 存在秘匿
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "T"})
    assert res.status_code == 404


def test_title_over_200_chars_is_422(fake_repo):
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "あ" * 201})
    assert res.status_code == 422


def test_empty_title_is_422(fake_repo):
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": ""})
    assert res.status_code == 422


def test_unknown_field_is_422(fake_repo):
    """title/description 以外は受けない(プラン JSON 自由編集の禁止 — specs/19 §11)。"""
    sid = _designed_sid(fake_repo)
    res = client.patch(f"/api/builder/sessions/{sid}/plan",
                       json={"title": "T", "capabilities": ["chat"]})
    assert res.status_code == 422


def test_concurrent_generate_between_read_and_save_is_409(fake_repo, monkeypatch):
    """読み取り後に生成開始(demo_id)が割り込んだら楽観ロックで 409(save_plan 0 行)。"""
    sid = _designed_sid(fake_repo)
    real_save = fake_repo.save_plan

    def racy_save(owner, s, plan, expected_len):
        fake_repo.rows[s]["demo_id"] = "d1"
        return real_save(owner, s, plan, expected_len)

    monkeypatch.setattr(repo, "save_plan", racy_save)
    res = client.patch(f"/api/builder/sessions/{sid}/plan", json={"title": "T"})
    assert res.status_code == 409


def test_unauthenticated_is_401(monkeypatch):
    from jetuse_core.settings import get_settings
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        res = client.patch("/api/builder/sessions/x/plan", json={"title": "T"})
        assert res.status_code == 401
    finally:
        get_settings.cache_clear()
