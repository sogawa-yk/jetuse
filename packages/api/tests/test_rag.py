"""RAG(RAG-01/02)のAPIテスト。rag層はfake、citations抽出は実関数。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.chat import _extract_citations
from service.main import app

client = TestClient(app)


class FakeRag:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.store_id: str | None = None

    def list_files(self, owner):
        return [dict(v) for v in self.files.values()]

    def refresh_statuses(self, owner, files):
        return files

    def add_file(self, owner, filename, content):
        fid = f"f{len(self.files) + 1}"
        self.files[fid] = {
            "id": fid, "filename": filename, "status": "processing",
            "bytes": len(content), "oci_file_id": f"file-{fid}",
        }
        self.store_id = self.store_id or "vs_fake"
        return self.files[fid]

    def delete_file(self, owner, file_id):
        return self.files.pop(file_id, None) is not None

    def get_store_id(self, owner):
        return self.store_id


@pytest.fixture(autouse=True)
def fake_rag(monkeypatch):
    fake = FakeRag()
    for name in ("list_files", "refresh_statuses", "add_file", "delete_file", "get_store_id"):
        monkeypatch.setattr(service_main.rag, name, getattr(fake, name))
    import service.routes.rag as rag_routes  # read 経路の移行ゲートは no-op に(DB 不要)
    monkeypatch.setattr(rag_routes, "owner_key_gate", lambda: None)
    # chat の RAG 読取(resolve_store_for_read / select_ai generate)が通す移行ゲートも no-op
    monkeypatch.setattr(service_main.rag, "owner_key_gate", lambda: None)
    import service.routes.chat as chat_routes
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)
    yield fake


def test_read_path_fails_closed_during_owner_key_migration(monkeypatch):
    """B001: 未分類の予約接頭辞行が残る移行中は read も 503(越境参照を防ぐ fail-closed)。"""
    import service.routes.rag as rag_routes
    from jetuse_core.owner_keys import OwnerKeyPreflightError

    def boom():
        raise OwnerKeyPreflightError("2 reserved-prefix owner rows need classification")

    monkeypatch.setattr(rag_routes, "owner_key_gate", boom)
    res = client.get("/api/rag/files")
    assert res.status_code == 503


def test_resolve_store_for_read_gates_then_resolves(monkeypatch):
    """review-12 B003: チャット/エージェントの RAG 読取は owner_key_gate を通してから
    Vector Store を解決する(write/list と同じ fail-closed 一貫性 = 越境参照防止)。"""
    from jetuse_core import rag
    from jetuse_core.owner_keys import OwnerKeyPreflightError

    order: list[str] = []
    monkeypatch.setattr(rag, "owner_key_gate", lambda: order.append("gate"))
    monkeypatch.setattr(rag, "get_store_id", lambda o: (order.append("resolve"), "vs_x")[1])
    assert rag.resolve_store_for_read("dev-user") == "vs_x"
    assert order == ["gate", "resolve"]  # ゲートが先(未通過なら解決させない)

    def boom():
        raise OwnerKeyPreflightError("pending")

    monkeypatch.setattr(rag, "owner_key_gate", boom)
    with pytest.raises(OwnerKeyPreflightError):
        rag.resolve_store_for_read("dev-user")  # 移行未完なら 503 契機で fail-closed


def test_select_ai_rag_read_gated_by_owner_key_migration(fake_rag, monkeypatch):
    """review-13 M007: select_ai/opensearch の RAG generate 経路も移行ゲートを通す
    (未分類 legacy 残存時は 503 = ensure_profile での越境資産作成をストリーム前に塞ぐ)。"""
    import service.routes.chat as chat_routes
    from jetuse_core.owner_keys import OwnerKeyPreflightError

    def boom():
        raise OwnerKeyPreflightError("pending")

    monkeypatch.setattr(chat_routes, "owner_key_gate", boom)
    res = client.post("/api/chat/stream",
                      json={"model": "gpt-oss-120b",
                            "messages": [{"role": "user", "content": "q"}],
                            "rag": True, "rag_backend": "select_ai"})
    assert res.status_code == 503


def test_chat_conversation_lookup_gated_by_owner_key(monkeypatch):
    """review-11 B004: conversation_id 照合の前に owner_key_gate を通す。未分類の
    予約接頭辞行が残る間は 503 = legacy owner 衝突での他人会話の参照/追記を塞ぐ。"""
    import service.routes.chat as chat_routes
    from jetuse_core.owner_keys import OwnerKeyPreflightError

    def boom():
        raise OwnerKeyPreflightError("pending")

    monkeypatch.setattr(chat_routes, "owner_key_gate", boom)
    monkeypatch.setattr(chat_routes.conv_repo, "get_conversation",
                        lambda *a, **k: pytest.fail("gate must block before lookup"))
    res = client.post("/api/chat/stream",
                      json={"model": "gpt-oss-120b",
                            "messages": [{"role": "user", "content": "q"}],
                            "conversation_id": "c-other"})
    assert res.status_code == 503


def test_resolve_os_namespace_prefers_settings_else_live(monkeypatch):
    """review-14 B002: PUT/削除/locator は同一解決(config 値優先、無ければ実 namespace)。"""
    from jetuse_core import rag

    class _C:
        def get_namespace(self):
            return SimpleNamespace(data="live-ns")

    monkeypatch.setattr(rag.get_settings(), "os_namespace", "cfg-ns")
    assert rag._resolve_os_namespace(_C()) == "cfg-ns"
    monkeypatch.setattr(rag.get_settings(), "os_namespace", "")
    assert rag._resolve_os_namespace(_C()) == "live-ns"


def test_assert_bucket_not_versioned_fail_closed_and_cached(monkeypatch):
    """review-14 B001: versioning!=Disabled は 503。Disabled は通過し以後キャッシュ。"""
    from jetuse_core import rag
    from jetuse_core.rag_ledger import UnmanagedFilesError

    rag._versioning_checked.clear()

    def cl(v):
        return SimpleNamespace(
            get_bucket=lambda ns, b: SimpleNamespace(data=SimpleNamespace(versioning=v)))

    with pytest.raises(UnmanagedFilesError):
        rag._assert_bucket_not_versioned(cl("Enabled"), "ns", "bkt")
    with pytest.raises(UnmanagedFilesError):
        rag._assert_bucket_not_versioned(cl("Suspended"), "ns", "bkt2")
    rag._assert_bucket_not_versioned(cl("Disabled"), "ns", "bkt3")  # ok
    # 同 key は以後キャッシュ = get_bucket を呼ばない(呼べば AssertionError)
    boom = SimpleNamespace(
        get_bucket=lambda ns, b: (_ for _ in ()).throw(AssertionError("should be cached")))
    rag._assert_bucket_not_versioned(boom, "ns", "bkt3")


def test_upload_list_delete(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("policy.md", b"# regulations", "text/markdown")},
    )
    assert res.status_code == 200
    fid = res.json()["id"]
    assert res.json()["status"] == "processing"
    assert any(f["id"] == fid for f in client.get("/api/rag/files").json()["files"])
    assert client.delete(f"/api/rag/files/{fid}").json() == {"deleted": True}
    assert client.delete(f"/api/rag/files/{fid}").status_code == 404


def test_upload_rejects_bad_files():
    res = client.post(
        "/api/rag/files", files={"file": ("doc.docx", b"x", "application/octet-stream")}
    )
    assert res.status_code == 422
    assert "docx" in res.json()["detail"]
    res2 = client.post("/api/rag/files", files={"file": ("a.exe", b"x", "x")})
    assert res2.status_code == 422
    res3 = client.post("/api/rag/files", files={"file": ("a.md", b"", "x")})
    assert res3.status_code == 422


def test_upload_returns_503_when_store_not_ready(monkeypatch):
    def not_ready(owner, filename, content):
        raise service_main.rag.StoreNotReadyError("dp propagation timeout")

    monkeypatch.setattr(service_main.rag, "add_file", not_ready)
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_rag_chat_requires_responses_model_and_store(fake_rag, monkeypatch):
    body = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "q"}], "rag": True}
    assert client.post("/api/chat/stream", json=body).status_code == 400  # chat系は不可

    body2 = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}], "rag": True}
    assert client.post("/api/chat/stream", json=body2).status_code == 400  # ストア未作成

    fake_rag.store_id = "vs_fake"
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["store"] = params.file_search_store
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    assert client.post("/api/chat/stream", json=body2).status_code == 200
    assert captured["store"] == "vs_fake"


def test_select_ai_backend_streams_single_delta(monkeypatch):
    def fake_generate(owner, prompt):
        return "回答本文です。", [{"file_id": "a.md", "filename": "a.md", "score": None}]

    monkeypatch.setattr(service_main.rag_select_ai, "generate", fake_generate)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "q"}],
            "rag": True,
            "rag_backend": "select_ai",
        },
    )
    assert res.status_code == 200
    assert '"delta": "回答本文です。"' in res.text
    assert '"citations"' in res.text
    assert res.text.rstrip().endswith("data: [DONE]")


def test_split_sources():
    from jetuse_core.rag_select_ai import split_sources

    ans = (
        "宿泊費の上限は12,000円です。\n\nSources:\n"
        "  - travel-policy.pdf (https://objectstorage.example/x)\n"
        "  - b36a17e0-a6f0-45fc-91e8-94dc88a15cbb_expense.md (https://objectstorage.example/y)\n"
    )
    body, cites = split_sources(ans)
    assert body == "宿泊費の上限は12,000円です。"
    # uuidプレフィックス({uuid}_name)は表示名から除去される
    assert [c["filename"] for c in cites] == ["travel-policy.pdf", "expense.md"]
    body2, cites2 = split_sources("Sourcesなしの回答")
    assert body2 == "Sourcesなしの回答" and cites2 == []


def test_extract_citations():
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="file_search_call",
                results=[
                    SimpleNamespace(file_id="f1", filename="policy.pdf", score=0.83),
                    SimpleNamespace(file_id="f1", filename="policy.pdf", score=0.51),
                    SimpleNamespace(file_id="f2", filename="rules.md", score=0.42),
                ],
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        annotations=[SimpleNamespace(file_id="f3", filename="extra.txt")]
                    )
                ],
            ),
        ]
    )
    cites = _extract_citations(response)
    assert [c["file_id"] for c in cites] == ["f1", "f2", "f3"]
    assert cites[0]["score"] == 0.83  # 同一ファイルは最大スコア


def test_attach_backend_status(monkeypatch):
    """3バックエンドの取り込み状況が各ファイルに付与される(ENH-05)。"""
    from jetuse_core import rag

    files = [
        {"id": "f1", "filename": "a.pdf", "status": "completed"},
        {"id": "f2", "filename": "b.pdf", "status": "processing"},
        {"id": "f3", "filename": "c.pdf", "status": "failed"},
    ]
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: True)
    monkeypatch.setattr(ros, "indexed_file_ids", lambda owner: {"f1", "f2"})

    out = rag.attach_backend_status("u", files)
    assert out[0]["backends"] == {"vector_store": "indexed", "select_ai": "indexed",
                                  "opensearch": "indexed"}
    assert out[1]["backends"] == {"vector_store": "pending", "select_ai": "pending",
                                  "opensearch": "indexed"}
    assert out[2]["backends"] == {"vector_store": "error", "select_ai": "pending",
                                  "opensearch": "pending"}


def test_resolve_citation_filenames(monkeypatch):
    """OCIが返す文字化けファイル名を、DBの元ファイル名へ解決する(石井FB #4)。"""
    from jetuse_core import rag

    monkeypatch.setattr(rag, "list_files", lambda owner: [
        {"id": "u1", "filename": "日本語の規程.pdf", "oci_file_id": "ocifile-1"},
        {"id": "u2", "filename": "手順書.md", "oci_file_id": "ocifile-2"},
    ])
    cites = [
        {"file_id": "ocifile-1", "filename": "garbled-mojibake", "score": 0.9},
        {"file_id": "u2", "filename": "garbled", "score": None},
        {"file_id": "unknown", "filename": "keep", "score": None},
    ]
    out = rag.resolve_citation_filenames("o", cites)
    assert out[0]["filename"] == "日本語の規程.pdf"
    assert out[1]["filename"] == "手順書.md"
    assert out[2]["filename"] == "keep"


def test_attach_backend_status_opensearch_disabled(monkeypatch):
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: set())
    monkeypatch.setattr(ros, "enabled", lambda: False)
    out = rag.attach_backend_status("u", [{"id": "f1", "filename": "a", "status": "completed"}])
    assert out[0]["backends"]["opensearch"] == "disabled"


def test_list_all_external_files_fail_closed_on_has_more():
    """M004: has_more=True は不完全一覧 → fail-closed(UnmanagedFilesError=503)。
    部分一覧のまま台帳を消して孤児 File を残すのを防ぐ(OCI Files の after は前進しない)。"""
    from jetuse_core import rag
    from jetuse_core.rag_ledger import UnmanagedFilesError

    files = SimpleNamespace(list=lambda limit: SimpleNamespace(
        data=[SimpleNamespace(id="f1", filename="x/1.md")], has_more=True))
    with pytest.raises(UnmanagedFilesError):
        rag.list_all_external_files(SimpleNamespace(files=files))


def test_list_all_stores_fail_closed_on_has_more():
    """M004: CP 一覧も has_more=True なら fail-closed(孤児 store の取りこぼし防止)。"""
    from jetuse_core import rag
    from jetuse_core.rag_ledger import UnmanagedFilesError

    vs = SimpleNamespace(list=lambda limit: SimpleNamespace(
        data=[SimpleNamespace(id="vs1")], has_more=True))
    with pytest.raises(UnmanagedFilesError):
        rag._list_all_stores(SimpleNamespace(vector_stores=vs))


def test_reconcile_unmanaged_detection_excludes_only_pending_names():
    """M002: 登録済み rid と同名の別 File(API 再試行の重複)は未管理として検出する。
    exemption は外部 ID 未設定の pending rid の file_key 名だけに限る。"""
    from jetuse_core.rag_ledger import _is_pending_named

    pending = {"rid-pending"}
    assert _is_pending_named("abc123/rid-pending.md", pending)      # pending の File → 管理下
    assert not _is_pending_named("abc123/rid-confirmed.md", pending)  # 重複/登録済 → 未管理
    assert not _is_pending_named("plainname", pending)               # file_key 形式でない


def test_reconcile_locator_key_is_order_stable():
    """B002: locator の dict 順が違っても同一 project を1つに畳む(旧 project 走査の重複排除)。"""
    from jetuse_core.rag_ledger import _loc_key

    assert _loc_key({"region": "r", "project": "p"}) == _loc_key({"project": "p", "region": "r"})
    assert _loc_key(None) == _loc_key({})
    assert _loc_key({"region": "r1"}) != _loc_key({"region": "r2"})


class _RCur:
    def execute(self, *a, **kw): pass
    def fetchall(self): return []
    def fetchone(self): return (0,)  # grandfathered rag_files COUNT = 0(RAG_FILES 不在扱い)


class _RConn:
    def cursor(self): return _RCur()
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_reconcile_closes_gate_before_listing(monkeypatch):
    """B001: 起動ごとに gate をまず閉じ(前回の 'Y' を無効化)、突合完了後に判定して開く。
    閉じる操作は File 一覧取得より前(reconcile 進行中に旧 'Y' で upload を通さない)。"""
    from jetuse_core import rag_ledger as L

    log: list = []
    monkeypatch.setattr(L, "connect", lambda: _RConn())
    monkeypatch.setattr(L, "_ensure_ledger", lambda cur: None)
    monkeypatch.setattr(L, "_stale_pending", lambda cur: [])
    monkeypatch.setattr(L, "_set_gate", lambda cur, open_: log.append(("gate", open_)))
    monkeypatch.setattr(L, "current_locator", lambda: {"region": "r"})

    def list_fn(loc):
        log.append(("list", loc))
        return []

    L.reconcile(list_fn, lambda *a: None, lambda *a: None, lambda *a: None)
    assert log[0] == ("gate", False)                       # 最初に閉じる
    first_list = next(i for i, e in enumerate(log) if e[0] == "list")
    assert first_list > 0                                  # 閉じてから一覧
    assert log[-1] == ("gate", True)                       # 未管理ゼロなら再び開く


def test_gate_passes_boot_generation():
    """B001: gate は 'Y' かつ今回起動が開けた場合のみ通す。前回起動の 'Y'(boot 不一致)は閉じる。
    current_boot_id 空(単一プロセス)なら boot 照合はスキップ = 従来挙動。"""
    from jetuse_core.rag_ledger import _gate_passes

    assert _gate_passes("Y", None, "") is True         # boot 追跡なし: 値のみ
    assert _gate_passes("N", None, "") is False
    assert _gate_passes("Y", "boot-1", "boot-1") is True   # 今回起動が開けた
    assert _gate_passes("Y", "boot-0", "boot-1") is False  # 前回起動の stale 'Y'
    assert _gate_passes("Y", None, "boot-1") is False      # boot 未記録(旧列)
    assert _gate_passes("N", "boot-1", "boot-1") is False


def test_total_file_count_includes_grandfathered_rag_files():
    """B001: 上限判定の総数は ledger 行 + 既存 rag_files(ledger 未登録)。既存 File を ledger へ
    backfill せず grandfather として数える(推測 locator の delete 孤児化を避けつつ枠は正確に)。"""
    from jetuse_core.rag_ledger import _total_file_count

    class Cur:
        _q = ""

        def execute(self, sql, **b):
            self._q = sql

        def fetchone(self):
            if "user_tables" in self._q:
                return (1,)          # RAG_FILES 存在
            if "FROM rag_file_ledger" in self._q and "rag_files" not in self._q:
                return (3,)          # ledger 行 = 3
            if "FROM rag_files rf" in self._q:
                return (2,)          # ledger 未登録の既存 rag_files = 2
            return (0,)

    assert _total_file_count(Cur()) == 5  # 3 + 2

    class CurNoTable:
        _q = ""

        def execute(self, sql, **b):
            self._q = sql

        def fetchone(self):
            if "user_tables" in self._q:
                return (0,)          # RAG_FILES 不在(最小構成)
            return (4,)              # ledger のみ

    assert _total_file_count(CurNoTable()) == 4


def test_upload_gate_noop_without_total_limit(monkeypatch):
    """B002: RAG_FILES_TOTAL_LIMIT 未設定(既定 None = Public 互換)なら gate は no-op で DB を
    触らない(reconcile を回さない既定デプロイで全 upload が 503 になるのを防ぐ)。"""
    from jetuse_core import rag_ledger as L

    monkeypatch.setattr(L, "get_settings",
                        lambda: SimpleNamespace(rag_files_total_limit=None, app_boot_id=""))
    monkeypatch.setattr(L, "connect",
                        lambda: (_ for _ in ()).throw(AssertionError("DB を触るべきでない")))
    L.upload_gate()  # 例外なく即 return


def test_reconcile_excludes_grandfathered_from_unmanaged(monkeypatch):
    """B001/B003: 既存 rag_files の File(ledger 未登録)は未管理と誤判定せず gate を開いたまま
    にする(backfill しない=推測 locator による delete 孤児化を避けつつ 503 恒久化も防ぐ)。"""
    from jetuse_core import rag_ledger as L

    gate = {}

    class _Cur:
        _q = ""

        def execute(self, sql, **kw):
            self._q = sql

        def fetchall(self):
            if "oci_file_id FROM rag_files" in self._q:
                return [("file-existing",)]   # grandfathered ext id
            return []                          # ledger は空(raw / confirmed)

        def fetchone(self):
            if "user_tables" in self._q:
                return (1,)                    # RAG_FILES 存在
            return (0,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(L, "connect", lambda: _Conn())
    monkeypatch.setattr(L, "_ensure_ledger", lambda cur: None)
    monkeypatch.setattr(L, "_stale_pending", lambda cur: [])
    monkeypatch.setattr(L, "_set_gate", lambda cur, open_: gate.__setitem__("open", open_))
    monkeypatch.setattr(L, "current_locator", lambda: {"region": "r"})
    # 現在 project の File 一覧に「既存 rag_files の File」だけが在る → 未管理ゼロ = gate 開く
    summary = L.reconcile(lambda loc=None: [{"id": "file-existing", "filename": "x"}],
                          lambda *a: None, lambda *a: None, lambda *a: None)
    assert summary["unmanaged"] == 0
    assert gate["open"] is True  # 既存 File を未管理扱いして閉じない


def test_current_locator_persists_opensearch_endpoint(monkeypatch):
    """B004: OpenSearch 有効時は取り込み時の endpoint を locator に write-ahead。
    無効時は付けない(現在設定に依らず保存 endpoint で個別 DELETE できるようにするため)。"""
    from jetuse_core import rag_ledger as L

    monkeypatch.setattr(L, "get_settings", lambda: SimpleNamespace(
        oci_region="r", compartment_ocid="c", project_ocid="p",
        os_namespace="ns", rag_bucket="b", opensearch_endpoint="http://os:9200"))
    assert L.current_locator()["opensearch_endpoint"] == "http://os:9200"

    monkeypatch.setattr(L, "get_settings", lambda: SimpleNamespace(
        oci_region="r", compartment_ocid="c", project_ocid="p",
        os_namespace="ns", rag_bucket="b", opensearch_endpoint=""))
    assert "opensearch_endpoint" not in L.current_locator()
