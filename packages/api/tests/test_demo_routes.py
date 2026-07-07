"""デモスコープ能力ルート(SP1-03 / specs/17 §5)のテスト。

demos リポジトリと rag 層は fake、require_demo seam は実関数。
箱 = `demo_<id>` 名前空間(rag の owner キー)がユーザー単位と分離されることを検証する。
"""

import contextlib

import pytest
from fastapi.testclient import TestClient

import service.demo_context as demo_context
import service.main as service_main
from jetuse_core import demo_lease
from jetuse_core.models import DEFAULT_MODEL
from service.main import app

client = TestClient(app)

DEMOS = {
    "d1": {"id": "d1", "owner_sub": "dev-user", "name": "mine", "visibility": "private",
           "status": "ready"},
    "d2": {"id": "d2", "owner_sub": "dev-user", "name": "mine2", "visibility": "private",
           "status": "ready"},
    "theirs": {"id": "theirs", "owner_sub": "user-a", "name": "A's", "visibility": "private",
               "status": "ready"},
    "pub": {"id": "pub", "owner_sub": "user-a", "name": "shared", "visibility": "public",
            "status": "ready"},
}

CHAT_BODY = {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}]}


class NsFakeRag:
    """名前空間キー(owner引数)ごとにファイル・ストアを分離する fake。"""

    def __init__(self):
        self.files: dict[str, dict[str, dict]] = {}
        self.stores: dict[str, str] = {}

    def list_files(self, ns):
        return [dict(v) for v in self.files.get(ns, {}).values()]

    def refresh_statuses(self, ns, files):
        return files

    def add_file(self, ns, filename, content, lease=None):
        box = self.files.setdefault(ns, {})
        fid = f"{ns}-f{len(box) + 1}"
        box[fid] = {
            "id": fid, "filename": filename, "status": "processing",
            "bytes": len(content), "oci_file_id": f"file-{fid}",
        }
        self.stores.setdefault(ns, f"vs_{ns}")
        return box[fid]

    def delete_file(self, ns, file_id):
        return self.files.get(ns, {}).pop(file_id, None) is not None

    def get_store_id(self, ns):
        return self.stores.get(ns)


@pytest.fixture(autouse=True)
def fake_demos(monkeypatch):
    monkeypatch.setattr(demo_context.demos, "get_demo", DEMOS.get)


@pytest.fixture(autouse=True)
def fake_lease(monkeypatch):
    """demo mutation の排他リース(SP2-02)を DB なしで代替(契約検証は test_demo_lease)。"""

    @contextlib.contextmanager
    def fake_mutation(demo_id, **kw):
        yield demo_lease.DemoLease(demo_id=demo_id, _conn=None)

    monkeypatch.setattr(demo_lease, "mutation", fake_mutation)


@pytest.fixture(autouse=True)
def fake_rag(monkeypatch):
    fake = NsFakeRag()
    for name in ("list_files", "refresh_statuses", "add_file", "delete_file", "get_store_id"):
        monkeypatch.setattr(service_main.rag, name, getattr(fake, name))
    import service.routes.chat as chat_routes
    import service.routes.rag as rag_routes  # read 経路の移行ゲートは no-op に(DB 不要)
    monkeypatch.setattr(rag_routes, "owner_key_gate", lambda: None)
    # chat の RAG 読取(resolve_store_for_read)が通す移行ゲートも no-op(fake get_store_id を使う)
    monkeypatch.setattr(service_main.rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)  # 会話照合ゲート(B004)
    yield fake


def test_owner_chat_streams(monkeypatch):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "demo"}
        yield {"delta": "応答"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/demos/d1/chat", json=CHAT_BODY)
    assert res.status_code == 200
    body = res.text
    assert body.startswith('data: {"ka": 1}')
    assert '"delta": "demo"' in body
    assert body.rstrip().endswith("data: [DONE]")


def test_demo_chat_rag_uses_demo_namespace_store(monkeypatch):
    # デモの箱にだけ文書がある状態を作る
    client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("policy.md", b"# rules", "text/markdown")},
    )
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["store"] = params.file_search_store
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/demos/d1/chat", json={**CHAT_BODY, "rag": True})
    assert res.status_code == 200
    assert captured["store"] == "vs_demo_d1"
    # user 単位のストアは空のまま → 既存 /api/chat/stream の rag は 400(ストア未作成)
    assert client.post("/api/chat/stream", json={**CHAT_BODY, "rag": True}).status_code == 400


def test_owner_rag_upload_list_delete():
    res = client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("policy.md", b"# regulations", "text/markdown")},
    )
    assert res.status_code == 200
    fid = res.json()["id"]
    assert res.json()["status"] == "processing"
    listed = client.get("/api/demos/d1/rag/files").json()["files"]
    assert any(f["id"] == fid for f in listed)
    assert client.delete(f"/api/demos/d1/rag/files/{fid}").json() == {"deleted": True}
    assert client.delete(f"/api/demos/d1/rag/files/{fid}").status_code == 404


def test_demo_upload_same_validation_as_user_route():
    res = client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("doc.docx", b"x", "application/octet-stream")},
    )
    assert res.status_code == 422
    assert "docx" in res.json()["detail"]
    assert client.post(
        "/api/demos/d1/rag/files", files={"file": ("a.md", b"", "x")}
    ).status_code == 422


def test_demo_upload_returns_503_when_store_not_ready(monkeypatch):
    def not_ready(ns, filename, content, lease=None):
        raise service_main.rag.StoreNotReadyError("dp propagation timeout")

    monkeypatch.setattr(service_main.rag, "add_file", not_ready)
    res = client.post(
        "/api/demos/d1/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    )
    assert res.status_code == 503


def test_cross_user_demo_is_404_for_chat_and_rag():
    # dev-user が user-a の private デモへアクセス(存在秘匿 = 404)
    assert client.post("/api/demos/theirs/chat", json=CHAT_BODY).status_code == 404
    assert client.get("/api/demos/theirs/rag/files").status_code == 404
    assert client.post(
        "/api/demos/theirs/rag/files",
        files={"file": ("a.md", b"x", "text/markdown")},
    ).status_code == 404
    assert client.delete("/api/demos/theirs/rag/files/f1").status_code == 404


def test_demo_boxes_are_isolated(fake_rag):
    client.post(
        "/api/demos/d1/rag/files",
        files={"file": ("only-in-d1.md", b"x", "text/markdown")},
    )
    assert client.get("/api/demos/d2/rag/files").json()["files"] == []
    d1_files = client.get("/api/demos/d1/rag/files").json()["files"]
    assert [f["filename"] for f in d1_files] == ["only-in-d1.md"]
    # user 単位ルートの箱にも現れない
    assert client.get("/api/rag/files").json()["files"] == []


def test_public_demo_non_owner_can_read_and_chat_but_not_write(monkeypatch):
    """公開デモは非所有者も閲覧・実行(chat/GET)可。書き込み(POST/DELETE)は所有者のみ(REV-002)。"""

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    # dev-user は "pub"(user-a 所有・public)の非所有者
    assert client.post("/api/demos/pub/chat", json=CHAT_BODY).status_code == 200
    assert client.get("/api/demos/pub/rag/files").status_code == 200
    assert client.post(
        "/api/demos/pub/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    ).status_code == 404
    assert client.delete("/api/demos/pub/rag/files/f1").status_code == 404


def test_owner_can_write_own_public_demo(monkeypatch):
    monkeypatch.setitem(DEMOS, "mypub",
                        {"id": "mypub", "owner_sub": "dev-user", "name": "p",
                         "visibility": "public", "status": "ready"})
    assert client.post(
        "/api/demos/mypub/rag/files", files={"file": ("a.md", b"x", "text/markdown")}
    ).status_code == 200


# --- 会話のデモ紐付け(SP2-03 / specs/18 §4.2) ---


class FakeConvRepo:
    """demo_id の scope 契約を実装と同形で再現する fake(SQL 契約は実 ADB E2E で検証)。"""

    def __init__(self):
        self.convs: dict[str, dict] = {}

    def create_conversation(self, owner, model, title, demo_id=None):
        cid = f"c{len(self.convs) + 1}"
        self.convs[cid] = {"id": cid, "owner": owner, "model": model,
                           "demo_id": demo_id, "title": title or "新しい会話",
                           "messages": []}
        return {"id": cid, "title": title, "model": model}

    def get_conversation(self, owner, cid, demo_id=None):
        c = self.convs.get(cid)
        if not c or c["owner"] != owner or c.get("demo_id") != demo_id:
            return None
        return {**{k: c[k] for k in ("id", "title", "model", "messages")},
                "oci_conversation_id": c.get("oci_conversation_id")}

    def append_message(self, cid, role, content):
        self.convs[cid]["messages"].append({"role": role, "content": content})

    def set_oci_conversation(self, owner, cid, oci):
        self.convs[cid]["oci_conversation_id"] = oci

    def log_usage(self, owner, cid, model, input_tokens, output_tokens):
        pass


@pytest.fixture()
def fake_conv(monkeypatch):
    repo = FakeConvRepo()
    import service.routes.demos as demo_routes
    for name in ("create_conversation", "get_conversation", "append_message",
                 "set_oci_conversation", "log_usage"):
        monkeypatch.setattr(service_main.conv_repo, name, getattr(repo, name))
    monkeypatch.setattr(demo_routes, "owner_key_gate", lambda: None)
    yield repo


def _fake_stream(monkeypatch, capture: dict | None = None):
    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        if capture is not None:
            capture["oci_conversation_id"] = oci_conversation_id
            capture["messages"] = messages
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)


def test_demo_conversation_create_and_chat_continuation(fake_conv, monkeypatch):
    """作成 → conversation_id で chat 継続(specs/18 §4.2)。履歴は箱の会話に保存される。"""
    _fake_stream(monkeypatch)
    res = client.post("/api/demos/d1/conversations",
                      json={"model": DEFAULT_MODEL, "title": "デモ会話"})
    assert res.status_code == 200
    cid = res.json()["id"]
    assert fake_conv.convs[cid]["demo_id"] == "d1"

    res = client.post("/api/demos/d1/chat",
                      json={**CHAT_BODY, "conversation_id": cid})
    assert res.status_code == 200
    assert '"delta": "ok"' in res.text
    msgs = fake_conv.convs[cid]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_demo_conversation_unknown_model_400(fake_conv):
    res = client.post("/api/demos/d1/conversations", json={"model": "nope"})
    assert res.status_code == 400


def test_demo_conversation_create_404_for_others_private(fake_conv):
    res = client.post("/api/demos/theirs/conversations", json={"model": DEFAULT_MODEL})
    assert res.status_code == 404


def test_demo_conversation_create_allowed_for_public_non_owner(fake_conv):
    """公開デモで chat を実行できる者は会話も持てる(require_demo — specs/18 §4.2)。"""
    res = client.post("/api/demos/pub/conversations", json={"model": DEFAULT_MODEL})
    assert res.status_code == 200
    assert fake_conv.convs[res.json()["id"]]["demo_id"] == "pub"


def test_demo_chat_rejects_cross_box_conversations(fake_conv, monkeypatch):
    """両方向の持ち込み・他デモ・他人の会話はすべて 404(specs/18 §4.2)。"""
    _fake_stream(monkeypatch)
    user_cid = fake_conv.create_conversation("dev-user", DEFAULT_MODEL, None)["id"]
    d1_cid = fake_conv.create_conversation("dev-user", DEFAULT_MODEL, None, "d1")["id"]
    other_cid = fake_conv.create_conversation("user-a", DEFAULT_MODEL, None, "d1")["id"]

    # user 会話 → demo chat は 404
    assert client.post("/api/demos/d1/chat",
                       json={**CHAT_BODY, "conversation_id": user_cid}).status_code == 404
    # demo 会話 → user chat は 404
    assert client.post("/api/chat/stream",
                       json={**CHAT_BODY, "conversation_id": d1_cid}).status_code == 404
    # 他デモへの持ち込みは 404
    assert client.post("/api/demos/d2/chat",
                       json={**CHAT_BODY, "conversation_id": d1_cid}).status_code == 404
    # 他人の demo 会話は 404(owner_sub 不一致)
    assert client.post("/api/demos/d1/chat",
                       json={**CHAT_BODY, "conversation_id": other_cid}).status_code == 404


def test_demo_chat_does_not_create_oci_conversation(fake_conv, monkeypatch):
    """demo 会話は OCI Conversation を作らない(specs/18 §4.2 — LTM のデモ間混線を構造排除)。
    user 単位 chat では従来どおり作られる(回帰なし)。"""
    created: list[dict] = []

    def fake_create(meta):
        created.append(meta)
        return "oc-new"

    monkeypatch.setattr(service_main, "create_oci_conversation", fake_create,
                        raising=False)
    import service.routes.chat as chat_routes
    monkeypatch.setattr(chat_routes, "create_oci_conversation", fake_create)

    cap: dict = {}
    _fake_stream(monkeypatch, cap)
    # gpt-oss-120b は responses 系 + persist_user 既定 true → user 経路なら作成される条件
    cid = client.post("/api/demos/d1/conversations",
                      json={"model": "gpt-oss-120b"}).json()["id"]
    res = client.post("/api/demos/d1/chat",
                      json={"model": "gpt-oss-120b", "conversation_id": cid,
                            "messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200
    assert created == []  # demo では呼ばれない
    assert cap["oci_conversation_id"] is None  # ステートレス(全履歴再送の契約)

    # user 経路の回帰なし: 同条件で作成される
    ucid = fake_conv.create_conversation("dev-user", "gpt-oss-120b", None)["id"]
    res = client.post("/api/chat/stream",
                      json={"model": "gpt-oss-120b", "conversation_id": ucid,
                            "messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200
    assert len(created) == 1
    assert created[0]["memory_subject_id"] == "dev-user"


def test_demo_conversation_reserved_prefix_sub_roundtrip(fake_conv, monkeypatch):
    """予約接頭辞 sub(demo_/sub_)は owner_key で単射エスケープされ往復が成立(specs/18 §4.2)。"""
    from jetuse_core.auth import AuthContext, require_user
    from service.main import app as _app

    _fake_stream(monkeypatch)
    monkeypatch.setitem(DEMOS, "pubx",
                        {"id": "pubx", "owner_sub": "user-a", "name": "p",
                         "visibility": "public", "status": "ready"})
    _app.dependency_overrides[require_user] = lambda: AuthContext(subject="demo_evil")
    try:
        cid = client.post("/api/demos/pubx/conversations",
                          json={"model": DEFAULT_MODEL}).json()["id"]
        # 資源キー列はエスケープ済み(raw sub を owner に直渡ししない)
        assert fake_conv.convs[cid]["owner"] == "sub_demo_evil"
        res = client.post("/api/demos/pubx/chat",
                          json={**CHAT_BODY, "conversation_id": cid})
        assert res.status_code == 200
    finally:
        _app.dependency_overrides.pop(require_user, None)


def _mock_select_ai(monkeypatch, answer="回答本文"):
    import service.routes.chat as chat_routes
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(chat_routes.rag_select_ai, "generate",
                        lambda ns, prompt: (answer, []))
    monkeypatch.setattr(chat_routes.rag_select_ai, "ensure_profile",
                        lambda ns, lease=None: "prof")


def test_demo_rag_select_ai_persists_to_box_conversation(fake_conv, monkeypatch):
    """review-2 M002: RAG select_ai 経路でも conversation_id 指定時に user/assistant を
    箱の会話へ保存する。"""
    _mock_select_ai(monkeypatch)
    cid = client.post("/api/demos/d1/conversations",
                      json={"model": DEFAULT_MODEL}).json()["id"]
    res = client.post("/api/demos/d1/chat",
                      json={"model": DEFAULT_MODEL, "rag": True,
                            "rag_backend": "select_ai", "conversation_id": cid,
                            "messages": [{"role": "user", "content": "質問A"}]})
    assert res.status_code == 200
    msgs = fake_conv.convs[cid]["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == \
        [("user", "質問A"), ("assistant", "回答本文")]


def test_demo_rag_select_ai_persist_user_false_only_assistant(fake_conv, monkeypatch):
    _mock_select_ai(monkeypatch)
    cid = client.post("/api/demos/d1/conversations",
                      json={"model": DEFAULT_MODEL}).json()["id"]
    res = client.post("/api/demos/d1/chat",
                      json={"model": DEFAULT_MODEL, "rag": True,
                            "rag_backend": "select_ai", "conversation_id": cid,
                            "persist_user": False,
                            "messages": [{"role": "user", "content": "再生成"}]})
    assert res.status_code == 200
    assert [m["role"] for m in fake_conv.convs[cid]["messages"]] == ["assistant"]


def test_demo_rag_select_ai_rejects_foreign_conversation(fake_conv, monkeypatch):
    """RAG select_ai 経路でも他人/他デモの conversation_id は 404(早期 return 前の検証)。"""
    _mock_select_ai(monkeypatch)
    other = fake_conv.create_conversation("user-a", DEFAULT_MODEL, None, "d1")["id"]
    res = client.post("/api/demos/d1/chat",
                      json={"model": DEFAULT_MODEL, "rag": True,
                            "rag_backend": "select_ai", "conversation_id": other,
                            "messages": [{"role": "user", "content": "越境"}]})
    assert res.status_code == 404
    # 保存もされない
    assert fake_conv.convs[other]["messages"] == []


def test_capabilities_list_demo_scoped_routes():
    from jetuse_core.capabilities import CAPABILITIES

    by_name = {c["capability"]: c for c in CAPABILITIES}
    chat_paths = {r["path"] for r in by_name["chat"]["routes"]}
    rag_paths = {r["path"] for r in by_name["rag.search"]["routes"]}
    dbchat_paths = {r["path"] for r in by_name["dbchat"]["routes"]}
    assert "/api/demos/{demo_id}/chat" in chat_paths
    assert "/api/demos/{demo_id}/conversations" in chat_paths  # SP2-03 §4.2
    assert "/api/demos/{demo_id}/rag/files" in rag_paths
    # SP2-03 §4.3: dbchat 縦切り
    assert "/api/demos/{demo_id}/dbchat/nl2sql" in dbchat_paths
    assert "/api/demos/{demo_id}/dbchat/execute" in dbchat_paths
    assert "/api/demos/{demo_id}/dbchat/schema" in dbchat_paths
    assert "/api/demos/{demo_id}/db/datasets" in dbchat_paths
    assert "/api/demos/{demo_id}/db/datasets/{ds_id}/preview" in dbchat_paths
