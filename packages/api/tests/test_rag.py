"""RAG(RAG-01/02)のAPIテスト。rag層はfake、citations抽出は実関数。"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.chat import _extract_citations

# 差し替え前の実体（autouse の fake_rag が module 属性を上書きするので import 時に掴む）
from jetuse_core.rag import add_file as real_add_file
from service.main import app

client = TestClient(app)


class FakeRag:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.store_id: str | None = None
        self.last_attributes: dict | None = None

    def list_files(self, owner):
        return [dict(v) for v in self.files.values()]

    def refresh_statuses(self, owner, files):
        return files

    def add_file(self, owner, filename, content, attributes=None, ocr_engine=None):
        self.last_attributes = attributes
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
    def not_ready(owner, filename, content, attributes=None, ocr_engine=None):
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
    """各バックエンドの取り込み状況が各ファイルに付与される(ENH-05 / RAGM-02でadb追加)。"""
    from jetuse_core import rag

    files = [
        {"id": "f1", "filename": "a.pdf", "status": "completed"},
        {"id": "f2", "filename": "b.pdf", "status": "processing"},
        {"id": "f3", "filename": "c.pdf", "status": "failed"},
    ]
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: True)
    monkeypatch.setattr(ros, "indexed_file_ids", lambda owner: {"f1", "f2"})
    monkeypatch.setattr(radb, "enabled", lambda: True)
    monkeypatch.setattr(radb, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(radb, "errored_file_ids", lambda owner: set())  # DB へ触らせない

    out = rag.attach_backend_status("u", files)
    assert out[0]["backends"] == {"vector_store": "indexed", "select_ai": "indexed",
                                  "opensearch": "indexed", "adb": "indexed"}
    assert out[1]["backends"] == {"vector_store": "pending", "select_ai": "pending",
                                  "opensearch": "indexed", "adb": "pending"}
    assert out[2]["backends"] == {"vector_store": "error", "select_ai": "pending",
                                  "opensearch": "pending", "adb": "pending"}


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
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: set())
    monkeypatch.setattr(ros, "enabled", lambda: False)
    monkeypatch.setattr(radb, "enabled", lambda: False)  # DB へ触らせない
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
# --- RAGM-01: 構造化引用 / 版フィルタ / 属性付き取り込み ---


def _hit(**kw):
    """file_search_call.results の 1 件(SPIKE-M1 ①-c の実レスポンス形)。"""
    base = {
        "file_id": "f1", "filename": "c01__v2.0__current__spec.txt", "score": 0.85,
        "attributes": {
            "file": "在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
            "cells": "B12:F12", "kind": "spec", "current_version": "Y",
        },
        "text": "在庫照会API GET /v1/inventory は最大200件まで返却する。",
        "additional_properties": {"vector_store_id": "vs_x", "chunk_id": "0_ad585b8f"},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_extract_citations_returns_structured_source():
    """完了条件: file/version/sheet/cells が構造化された値として載る(本文埋め込みでない)。"""
    response = SimpleNamespace(
        output=[SimpleNamespace(type="file_search_call", results=[_hit()])]
    )
    (c,) = _extract_citations(response)
    assert c["source"] == {
        "file": "在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
        "cells": "B12:F12", "kind": "spec", "current_version": "Y",
    }
    assert c["text"].startswith("在庫照会API")
    assert c["chunk_id"] == "0_ad585b8f"


def test_extract_citations_backward_compatible_shape():
    """既存フロントは {file_id, filename, score} を読む。拡張は追加のみで壊さない。"""
    response = SimpleNamespace(
        output=[
            SimpleNamespace(type="file_search_call", results=[
                _hit(),
                # 属性なしの旧データ(拡張フィールドは付かない)
                _hit(file_id="f2", filename="old.txt", score=0.4,
                     attributes=None, text=None, additional_properties=None),
            ]),
            SimpleNamespace(type="message", content=[
                SimpleNamespace(annotations=[
                    SimpleNamespace(file_id="f3", filename="extra.txt")
                ])
            ]),
        ]
    )
    cites = _extract_citations(response)
    assert [c["file_id"] for c in cites] == ["f1", "f2", "f3"]
    for c in cites:
        assert set(c) >= {"file_id", "filename", "score"}
    assert "source" not in cites[1] and "text" not in cites[1]
    assert cites[0]["score"] == 0.85


def test_extract_citations_keeps_top_scoring_chunk_of_a_file():
    """属性はファイル単位(①-a)。同一ファイルの複数ヒットは最上位スコアのものを採る。"""
    response = SimpleNamespace(output=[SimpleNamespace(type="file_search_call", results=[
        _hit(score=0.3, text="低スコアのチャンク",
             additional_properties={"chunk_id": "9_low"}),
        _hit(score=0.9, text="高スコアのチャンク",
             additional_properties={"chunk_id": "1_high"}),
    ])])
    (c,) = _extract_citations(response)
    assert c["score"] == 0.9 and c["chunk_id"] == "1_high"


def test_citation_text_is_truncated():
    from jetuse_core.chat import CITATION_TEXT_CHARS

    response = SimpleNamespace(output=[SimpleNamespace(
        type="file_search_call", results=[_hit(text="あ" * (CITATION_TEXT_CHARS + 50))]
    )])
    (c,) = _extract_citations(response)
    assert len(c["text"]) == CITATION_TEXT_CHARS


def test_file_search_tool_carries_filters():
    from jetuse_core.chat import GenParams, _extra_responses_params
    from jetuse_core.models import MODELS

    f = {"type": "eq", "key": "current_version", "value": "Y"}
    extra = _extra_responses_params(
        MODELS["gpt-oss-120b"],
        GenParams(file_search_store="vs_x", file_search_filters=f),
    )
    assert extra["tools"] == [
        {"type": "file_search", "vector_store_ids": ["vs_x"], "filters": f}
    ]
    # 未指定なら filters キー自体を送らない(既存挙動の維持)
    extra2 = _extra_responses_params(
        MODELS["gpt-oss-120b"], GenParams(file_search_store="vs_x")
    )
    assert extra2["tools"] == [{"type": "file_search", "vector_store_ids": ["vs_x"]}]


def test_chat_stream_rejects_filters_in_agent_mode(fake_rag):
    """エージェント経路は別ディスパッチでフィルタを渡す口が無い。
    素通しすると黙って無視されるので 400 で断る(レビュー F-001)。"""
    fake_rag.store_id = "vs_fake"
    f = {"type": "eq", "key": "current_version", "value": "Y"}
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "agent": True, "rag_filters": f,
    })
    assert res.status_code == 400 and "agent mode" in res.json()["detail"]
    res2 = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "agent_id": "a1", "rag_filters": f,
    })
    assert res2.status_code == 400 and "agent mode" in res2.json()["detail"]


def test_upload_rejects_overlong_filename_with_422(monkeypatch):
    """長すぎるファイル名は 500 にせず 422(レビュー F-002)。

    SYNC-01: dev(SP2-02)のルート側ガードが 400 文字で先に弾くため、RAGM-01 が想定した
    属性側の 512 文字上限(自動補完する file)には到達しなくなった。**契約(422 であって
    500 でない・OCI を呼ばない)は保たれている**ので、より厳しい方の 400 を正とする。
    `_rag_call` の MetadataError→422 正規化そのものは次のテストで直接固定する。
    """
    calls = {"n": 0}

    def add_file(owner, filename, content, attributes=None, ocr_engine=None, lease=None):
        calls["n"] += 1
        raise AssertionError("検証を通ってはいけない")

    monkeypatch.setattr(service_main.rag, "add_file", add_file)
    res = client.post(
        "/api/rag/files", files={"file": ("a" * 520 + ".md", b"x", "text/markdown")}
    )
    assert res.status_code == 422 and "400" in res.json()["detail"]
    assert calls["n"] == 0  # OCI どころか add_file にも入らない


def test_rag_call_normalizes_metadata_error_to_422():
    """RAGM-01 レビュー F-002: 取り込み側が投げる MetadataError を 500 のまま漏らさない。

    ルート前段のガードが厚くなっても、この正規化が消えると「不正な属性は 422」の
    API 契約が破れる(防御的だが契約なので固定する)。
    """
    import asyncio

    from fastapi import HTTPException

    from jetuse_core import rag_metadata
    from service.routes import rag as rag_routes

    def boom():
        raise rag_metadata.MetadataError("attributes.cells が長すぎます(最大512文字)")

    try:
        asyncio.run(rag_routes._rag_call(boom))
        raise AssertionError("expected HTTPException")
    except HTTPException as e:
        assert e.status_code == 422 and "512" in e.detail


def test_chat_stream_passes_rag_filters(fake_rag, monkeypatch):
    fake_rag.store_id = "vs_fake"
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["filters"] = params.file_search_filters
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_filters": {"type": "eq", "key": "current_version", "value": "Y"},
    })
    assert res.status_code == 200
    assert captured["filters"] == {"type": "eq", "key": "current_version", "value": "Y"}


def test_chat_stream_rejects_unknown_filter_key(fake_rag):
    """SPIKE-M1 ①-b: 未知キーは上流では 0 件になるだけ。アプリが 422 で弾く。"""
    fake_rag.store_id = "vs_fake"
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_filters": {"type": "eq", "key": "current_verison", "value": "Y"},
    })
    assert res.status_code == 422
    assert "current_verison" in res.text


def test_chat_stream_rejects_filters_without_vector_store_backend(fake_rag):
    """効かない組み合わせを黙って無視しない(旧版が混ざる事故を防ぐ)。"""
    fake_rag.store_id = "vs_fake"
    f = {"type": "eq", "key": "current_version", "value": "Y"}
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "rag_backend": "select_ai", "rag_filters": f,
    })
    assert res.status_code == 400 and "select_ai" in res.json()["detail"]
    res2 = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag_filters": f,
    })
    assert res2.status_code == 400 and "rag=true" in res2.json()["detail"]


def test_upload_accepts_attributes_form_field(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.md", b"# spec", "text/markdown")},
        data={"attributes": '{"version": "2.0", "cells": "B12:F12", "sheet": ""}'},
    )
    assert res.status_code == 200
    # 空値はキーごと落ちる。file/sha256 は rag.add_file 側で補う
    assert fake_rag.last_attributes == {"version": "2.0", "cells": "B12:F12"}


def test_upload_rejects_bad_attributes():
    for bad in ('{"versoin": "2.0"}', "not json", '["file"]', '{"cells": "' + "x" * 513 + '"}'):
        res = client.post(
            "/api/rag/files",
            files={"file": ("spec.md", b"# spec", "text/markdown")},
            data={"attributes": bad},
        )
        assert res.status_code == 422, bad


def test_upload_without_attributes_stays_backward_compatible(fake_rag):
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 200
    assert fake_rag.last_attributes == {}


# --- xlsx の取り込み口と抽出口(PREP-01) ---------------------------------------


def _workbook() -> bytes:
    """架空の仕様書ブック(複数シート)。顧客データは使わない。"""
    from tests.test_extract_xlsx import SPEC, build

    return build(SPEC)


def test_upload_accepts_xlsx(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("サンプル仕様書.xlsx", _workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 200
    assert res.json()["filename"] == "サンプル仕様書.xlsx"


class _FakeLedger:
    """rag_ledger の呼び出し面だけ(SP2-02 の予約フロー)。実 DB を触らずに add_file を通す。"""

    def __init__(self):
        self.upload_ext: dict[str, str] = {}
        self.rid = "00000000-0000-4000-8000-0000000000ff"

    def upload_gate(self):
        pass

    def reserve(self, owner_key, filename, ext):
        return self.rid

    def set_upload_ext(self, rid, upload_ext):
        self.upload_ext[rid] = upload_ext

    def set_external(self, rid, ext_id):
        pass

    def release(self, rid):
        pass

    def confirm_in_tx(self, cur, rid):
        pass


def _stub_ledger_path(monkeypatch, rag_module) -> _FakeLedger:
    """本物の add_file を OCI も DB も無しで通すための最小スタブ(SYNC-02)。

    dev 側の取り込みは owner_key ゲート → 予約 → 原本 put → 登録の順で進むので、
    main 由来の抽出テストもこの経路を通す必要がある。
    """
    fake = _FakeLedger()
    monkeypatch.setattr(rag_module, "rag_ledger", fake)
    monkeypatch.setattr(rag_module, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag_module, "require_lease_for", lambda owner, lease: None)
    monkeypatch.setattr(rag_module, "ensure_store", lambda owner, lease=None: "vs_fake")
    monkeypatch.setattr(rag_module, "_put_original", lambda *a, **kw: None)
    monkeypatch.setattr(rag_module, "_insert_file_confirmed", lambda *a, **kw: None)
    return fake


def _use_real_add_file(monkeypatch):
    """xlsx の検証は `rag.add_file` の先頭(OCI を呼ぶ前)で走るので、本物を使って確かめる。"""
    monkeypatch.setattr(service_main.rag, "add_file", real_add_file)


def test_upload_rejects_broken_xlsx_with_422(monkeypatch):
    _use_real_add_file(monkeypatch)
    res = client.post("/api/rag/files", files={"file": ("broken.xlsx", b"not a zip", "x")})
    assert res.status_code == 422
    assert "xlsx" in res.json()["detail"]


def test_unsupported_type_message_lists_xlsx():
    res = client.post("/api/rag/files", files={"file": ("a.pptx", b"x", "x")})
    detail = res.json()["detail"]
    # 受け口(rag.ALLOWED_EXTENSIONS)の全形式が出る。PREP-03 で画像を足したので列挙も伸びる
    assert res.status_code == 422
    for ext in ("pdf", "txt", "md", "xlsx", "png", "jpg", "jpeg"):
        assert ext in detail, ext


def test_extract_returns_chunks_without_ingesting(fake_rag):
    res = client.post(
        "/api/extract", files={"file": ("サンプル仕様書.xlsx", _workbook(), "x")}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "サンプル仕様書.xlsx"
    assert body["chunk_count"] == len(body["chunks"]) == 2
    assert {c["cells"] for c in body["chunks"]} == {"B12:D13", "C5:E5"}
    assert [c["sheet"] for c in body["chunks"]] == ["API一覧", "制約"]
    # 取り込みは起きない(台帳にファイルが増えない)
    assert fake_rag.files == {}


def test_extract_works_for_text_files_too():
    res = client.post("/api/extract", files={"file": ("note.md", "# 見出し\n本文".encode(), "x")})
    assert res.status_code == 200
    assert res.json()["chunks"][0]["sheet"] == "本文"


def test_extract_rejects_limit_excess_with_422_naming_the_limit(monkeypatch):
    """上限超過は**切り詰めず**に 422。どの上限かが detail から分かる。"""
    from jetuse_core import extract_xlsx

    monkeypatch.setattr(extract_xlsx, "MAX_CHUNKS", 1)
    res = client.post("/api/extract", files={"file": ("spec.xlsx", _workbook(), "x")})
    assert res.status_code == 422
    assert "limit=chunks" in res.json()["detail"]


def test_extract_requires_supported_extension():
    assert client.post(
        "/api/extract", files={"file": ("a.docx", b"x", "x")}
    ).status_code == 422


def test_upload_accepts_xlsx_with_a_cell_over_the_chunk_char_limit():
    """1 セルが上限を超えるブックは**セルの中で分割して取り込む**(PREP-04)。

    以前はここで `422 limit=chunk_chars` を返していた。1 行 = 1 セルだと分ける場所が
    無く、ファイル全体が入らなかった(実案件で 8 冊中 2 冊)。
    """
    from jetuse_core import rag
    from tests.test_extract_xlsx import _giant_cell_text, build

    text = _giant_cell_text(13_000)
    content = build({"制約": [("A53", text)]})
    # 取り込み経路が投入するチャンクそのもの(`/api/extract` は保存しない = OCI を呼ばない)
    res = client.post("/api/extract", files={"file": ("spec.xlsx", content, "x")})
    assert res.status_code == 200
    chunks = res.json()["chunks"]
    assert len(chunks) > 1
    assert {(c["sheet"], c["cells"]) for c in chunks} == {("制約", "A53")}
    assert "".join(c["text"] for c in chunks) == text
    # マネージド側へ渡す本文も 422 にならず、断片が全部載る
    _, body, attrs = rag.prepare_upload("spec.xlsx", content)
    assert attrs == {"sheet": "制約", "cells": "A53"}
    assert all(c["text"] in body.decode("utf-8") for c in chunks)


# --- マネージド側へ渡す形(ファイル単位の属性)。実 OCI は E2E で確認する -------


def test_prepare_upload_converts_xlsx_to_text_with_file_level_attributes():
    """マネージド Vector Store は Office 形式を受け付けない(SPIKE-03)ので抽出テキストを渡す。

    そのとき属性は**ファイル単位**にしかできない(SPIKE-M1 ①-a)。チャンクごとの
    セル範囲を載せて「セル単位で返る」ように見せない。
    """
    from jetuse_core import rag as rag_module

    name, body, attrs = rag_module.prepare_upload("サンプル仕様書.xlsx", _workbook())
    assert name == "サンプル仕様書.xlsx.txt"
    assert "600 req/min" in body.decode()
    assert attrs == {"sheet": "(ブック全体: 2 シート)", "cells": "(ブック全体)"}


def test_prepare_upload_passes_through_non_xlsx():
    from jetuse_core import rag as rag_module

    assert rag_module.prepare_upload("a.md", b"# x") == ("a.md", b"# x", {})


def test_prepare_upload_rejects_empty_workbook():
    from jetuse_core import extract_xlsx
    from jetuse_core import rag as rag_module
    from tests.test_extract_xlsx import build

    with pytest.raises(extract_xlsx.EmptyWorkbook):
        rag_module.prepare_upload("empty.xlsx", build({"空": []}))


def test_upload_rejects_user_supplied_sheet_or_cells_for_xlsx(monkeypatch):
    """導出したファイル単位の属性を利用者指定で上書きさせない(能力差の偽装を防ぐ)。"""
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"sheet": "制約", "cells": "C5:E5", "version": "2.0"}'},
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "cells" in detail and "sheet" in detail and "adb" in detail


def test_upload_keeps_other_attributes_for_xlsx(monkeypatch):
    """`sheet` / `cells` 以外(版・分類)は従来どおり利用者指定が通る。"""
    from jetuse_core import rag as rag_module

    sent: dict = {}
    ledger = _stub_ledger_path(monkeypatch, rag_module)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"version": "2.0", "kind": "spec"}'},
    )
    assert res.status_code == 200
    # 送信名は不透明キー(SP2-02 `<owner hash>/<予約 id>.<拡張子>`)。変換したことは
    # **拡張子**に出る(SYNC-02: 原本は .xlsx のまま、送信は .txt)
    assert sent["upload_name"].endswith(".txt")
    assert ledger.upload_ext[ledger.rid] == "txt"
    assert sent["attributes"]["version"] == "2.0" and sent["attributes"]["kind"] == "spec"
    # 出典は**ファイル単位**(SPIKE-M1 ①-a)。チャンクごとのセル範囲は載らない
    assert sent["attributes"]["sheet"] == "(ブック全体: 2 シート)"
    assert sent["attributes"]["cells"] == "(ブック全体)"
    assert sent["attributes"]["file"] == "spec.xlsx"          # 台帳の表示名は元の xlsx


class _FakeDp:
    """Files API / Vector Store の最小スタブ(実 OCI 呼び出しは E2E で確認する)。"""

    def __init__(self, sent: dict):
        self.sent = sent
        outer = self

        class Files:
            def create(self, *, file, purpose):
                outer.sent["upload_name"] = file[0]
                outer.sent["upload_bytes"] = file[1]
                return SimpleNamespace(id="file-fake")

        class VsFiles:
            def create(self, *, vector_store_id, file_id, attributes):
                outer.sent["attributes"] = attributes

        self.files = Files()
        self.vector_stores = SimpleNamespace(files=VsFiles())


def test_kind_is_passed_to_the_adb_backend(monkeypatch):
    """同じ `kind` を両バックエンドへ入れる（分類の絞り込みがバックエンドで食い違わない）。"""
    from jetuse_core import rag as rag_module
    from jetuse_core import rag_adb

    sent: dict = {}
    seen: dict = {}
    _stub_ledger_path(monkeypatch, rag_module)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(rag_adb, "ingest",
                        lambda owner, fid, name, body, *, kind="doc", **kw:
                        seen.update(kind=kind))
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"kind": "spec"}'},
    )
    assert res.status_code == 200
    assert seen["kind"] == "spec" == sent["attributes"]["kind"]


def test_kind_longer_than_the_adb_column_is_rejected():
    """`kind` は ADB 側が VARCHAR2(32)。**バイト長**で見る（BYTE セマンティクス想定）。"""
    for bad in ("k" * 33, "分類" * 6):        # 33 バイト / 36 バイト
        res = client.post(
            "/api/rag/files",
            files={"file": ("a.md", b"x", "text/markdown")},
            data={"attributes": json.dumps({"kind": bad})},
        )
        assert res.status_code == 422 and "32" in res.json()["detail"], bad


def test_kind_rejects_non_string_scalars(fake_rag):
    """RAGM-04: `kind` は文字列のみ。数値・真偽は 422（黙って文字列化しない）。

    取り込みは 1 リクエストで両バックエンドへ入るので、ここで断ることが
    「同じ入力なら両バックエンドとも同じ応答」になる唯一の門。
    """
    for value in (0, False, 1.5):
        res = client.post(
            "/api/rag/files",
            files={"file": ("a.md", b"x", "text/markdown")},
            data={"attributes": json.dumps({"kind": value})},
        )
        assert res.status_code == 422, value
        assert "string" in res.json()["detail"]
        assert fake_rag.last_attributes is None  # OCI も ADB も呼ばない


@pytest.mark.parametrize("backend", ["vector_store", "adb", "select_ai", "opensearch"])
def test_chat_rejects_non_string_kind_filter_on_every_backend(fake_rag, backend):
    """RAGM-04: 同じ不正な `kind` フィルタは、選んだバックエンドに関わらず 422。

    バックエンド別の 400（絞り込み非対応）より**先に**型を弾く。ここが分かれると
    「同じ条件が選んだ先で違う応答になる」が検索側に残る。
    """
    fake_rag.store_id = "vs_fake"
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "rag_backend": backend,
        "rag_filters": {"type": "eq", "key": "kind", "value": 1},
    })
    assert res.status_code == 422, backend
    assert "string" in res.text


def test_long_filename_keeps_its_extension():
    """台帳へ収める切り詰めで拡張子を落とさない（形式で分岐する判定が誤らないため）。"""
    from jetuse_core import rag as rag_module

    name = "あ" * 200 + ".xlsx"                # 600 バイト超
    fitted = rag_module._fit(name)
    assert fitted.endswith(".xlsx") and len(fitted.encode()) <= 400
    assert rag_module.extract_xlsx.is_xlsx(fitted)


def test_extract_rejects_broken_pdf_with_422():
    res = client.post("/api/extract", files={"file": ("broken.pdf", b"%PDF-1.7 garbage", "x")})
    assert res.status_code == 422 and "PDF" in res.json()["detail"]


def test_select_ai_badge_treats_xlsx_like_any_other_format(monkeypatch):
    """xlsx を拡張子だけで `error` にしない（PREP-02 の実測で恒久 error を撤回した）。

    実測（`docs/verification/PREP-02.md`）: Select AI の索引は xlsx の原本を
    Oracle Text 経由でテキスト化して取り込み、検索でも引ける。したがって xlsx の状態は
    他形式と同じく「索引に在るか」だけで決まる（未反映なら同期待ちの `pending`）。
    """
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag as rag_module

    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: False)
    monkeypatch.setattr(radb, "enabled", lambda: True)
    monkeypatch.setattr(radb, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(radb, "errored_file_ids", lambda owner: set())
    files = [{"id": "f1", "filename": "spec.xlsx", "status": "completed"},
             {"id": "f2", "filename": "sheet2.xlsx", "status": "completed"},
             {"id": "f3", "filename": "policy.md", "status": "completed"}]
    out = rag_module.attach_backend_status("u", files)
    assert out[0]["backends"]["select_ai"] == "indexed"  # 索引に在る xlsx
    assert out[0]["backends"]["adb"] == "indexed"
    assert out[1]["backends"]["select_ai"] == "pending"  # まだ索引に無い xlsx = 同期待ち
    assert out[2]["backends"]["select_ai"] == "pending"  # md も同じ扱い


def test_opensearch_extracts_xlsx_instead_of_decoding_bytes():
    """xlsx をそのまま UTF-8 デコードして文字化け本文を投入しない。"""
    from jetuse_core import rag_opensearch

    text = rag_opensearch._extract_text("spec.xlsx", _workbook())
    assert "600 req/min" in text and "[制約 C5:E5]" in text
    assert "�" not in text


# --- PREP-03: スキャン PDF / 画像の結線 ----------------------------------------


@pytest.fixture
def scan(monkeypatch):
    """OCR をモックし、記憶をクリアした `extract_scan`(OCI は呼ばない)。"""
    from test_extract_scan import FakeOcr, build_pdf, build_png

    from jetuse_core import docunderstand, extract_scan

    fake = FakeOcr()
    monkeypatch.setattr(docunderstand, "ocr", fake)
    monkeypatch.setattr(docunderstand, "ocr_vlm", FakeOcr("VLM"))
    memos = (extract_scan._native, extract_scan._result, extract_scan._flags)
    for memo in memos:
        memo.clear()
    yield SimpleNamespace(ocr=fake, pdf=build_pdf, png=build_png())
    for memo in memos:
        memo.clear()


def test_extract_returns_page_numbers_for_a_scanned_pdf(scan):
    """層1(`POST /api/extract`)がスキャン PDF を通し、出典に頁が載る。"""
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")}
    )
    assert res.status_code == 200
    chunks = res.json()["chunks"]
    assert [c["sheet"] for c in chunks] == ["p.1", "p.2"]
    assert chunks[0]["text"] == "OCR1行目"


def test_extract_accepts_images(scan):
    res = client.post("/api/extract", files={"file": ("photo.png", scan.png, "image/png")})
    assert res.status_code == 200
    assert res.json()["chunks"][0]["sheet"] == "p.1"


def test_extract_does_not_ocr_a_pdf_that_has_a_text_layer(scan):
    """対照: 従来どおりテキスト層から取り、OCI を呼ばない(無駄な課金をしない)。"""
    res = client.post(
        "/api/extract",
        files={"file": ("born-digital.pdf", scan.pdf(["Rate limit 600 rpm"]), "x")},
    )
    assert res.status_code == 200
    assert "600 rpm" in res.json()["chunks"][0]["text"]
    assert scan.ocr.calls == []


def test_extract_rejects_an_unknown_ocr_engine_with_422(scan):
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")},
        data={"ocr_engine": "vlmm"},
    )
    assert res.status_code == 422 and "vlmm" in res.json()["detail"]
    assert scan.ocr.calls == []


def test_extract_lets_the_caller_choose_the_vlm_engine(scan):
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")},
        data={"ocr_engine": "vlm"},
    )
    assert res.status_code == 200
    assert res.json()["chunks"][0]["text"] == "VLM1行目"
    assert scan.ocr.calls == []          # DU は呼ばれない


def test_extract_rejects_over_the_page_limit_without_truncating(scan, monkeypatch):
    from jetuse_core import extract_scan

    monkeypatch.setattr(extract_scan, "MAX_OCR_PAGES", 1)
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf(["", ""]), "x")}
    )
    assert res.status_code == 422
    assert "limit=ocr_pages" in res.json()["detail"]
    assert scan.ocr.calls == []


def test_extract_reports_an_ocr_service_failure_as_503(scan, monkeypatch):
    """IAM 未整備は利用者の入力の問題ではない(422 にしない)。"""
    from jetuse_core import docunderstand

    def boom(content, **kw):
        raise docunderstand.OcrError("OCRサービスにアクセスできません(IAM未整備の可能性)")

    monkeypatch.setattr(docunderstand, "ocr", boom)
    res = client.post("/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")})
    assert res.status_code == 503 and "IAM" in res.json()["detail"]


def test_upload_accepts_images(scan, fake_rag):
    res = client.post("/api/rag/files", files={"file": ("photo.jpg", scan.png, "image/jpeg")})
    assert res.status_code == 200 and res.json()["filename"] == "photo.jpg"


def test_prepare_upload_converts_a_scan_to_text_with_the_page_range(scan):
    """マネージド Vector Store には OCR したテキストを渡す(属性はファイル単位)。"""
    from jetuse_core import rag as rag_module

    name, body, attrs = rag_module.prepare_upload("scan.pdf", scan.pdf(["", "", ""]))
    assert name == "scan.pdf.txt"
    assert body.decode().startswith("[p.1]\nOCR1行目")
    assert attrs == {"sheet": "p.1-p.3"}


def test_prepare_upload_passes_through_a_pdf_with_a_text_layer(scan):
    """テキスト層のある PDF は原本のまま渡す(従来どおり。変換も OCR もしない)。"""
    from jetuse_core import rag as rag_module

    pdf = scan.pdf(["Rate limit 600 rpm"])
    assert rag_module.prepare_upload("spec.pdf", pdf) == ("spec.pdf", pdf, {})
    assert scan.ocr.calls == []


def test_upload_ocrs_the_document_once_for_both_backends(scan, monkeypatch):
    """1 回のアップロードで OCR は 1 回(マネージド変換と ADB 取り込みで二重課金しない)。"""
    from jetuse_core import rag as rag_module
    from jetuse_core import rag_adb

    sent: dict = {}
    units: dict = {}
    _stub_ledger_path(monkeypatch, rag_module)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(
        rag_adb, "ingest",
        lambda o, fid, name, body, **kw: units.update(
            chunks=rag_adb.chunk_units(name, body, ocr_engine=kw.get("ocr_engine"))),
    )
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files", files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")}
    )
    assert res.status_code == 200
    assert len(scan.ocr.calls) == 1
    assert sent["upload_name"].endswith(".txt")   # 送信名は不透明キー + 送信用拡張子
    assert sent["attributes"]["sheet"] == "p.1-p.2"
    assert [c["sheet"] for c in units["chunks"]] == ["p.1", "p.2"]   # 出典は頁ごと


def test_opensearch_ocrs_images_instead_of_indexing_mojibake(scan):
    """画像を UTF-8 デコードして化けた本文を "indexed" にしない。"""
    from jetuse_core import rag_opensearch

    assert rag_opensearch._extract_text("photo.png", scan.png) == "OCR1行目"


def test_upload_propagates_the_chosen_engine_to_every_backend(scan, monkeypatch):
    """`ocr_engine=vlm` は取り込み口からも効き、**全バックエンドが同じエンジンで読む**。

    片方だけ既定の DU に落ちると、同じファイルの本文がバックエンドごとに違う経路で
    起こされ、検索結果が選んだバックエンドで変わる（RAGM-04 で `kind` について直したのと
    同じ種類のズレ）。
    """
    from test_extract_scan import FakeOcr

    from jetuse_core import docunderstand, rag_adb, rag_opensearch
    from jetuse_core import rag as rag_module

    vlm = FakeOcr("VLM")
    monkeypatch.setattr(docunderstand, "ocr_vlm", vlm)
    sent: dict = {}
    seen: dict = {}
    _stub_ledger_path(monkeypatch, rag_module)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(
        rag_adb, "ingest",
        lambda o, fid, name, body, **kw: seen.update(
            adb=[c["sheet"] for c in rag_adb.chunk_units(name, body,
                                                         ocr_engine=kw.get("ocr_engine"))]),
    )
    monkeypatch.setattr(rag_opensearch, "enabled", lambda: True)
    monkeypatch.setattr(
        rag_opensearch, "ingest",
        lambda o, fid, name, body, **kw: seen.update(
            opensearch=rag_opensearch._extract_text(name, body,
                                                    ocr_engine=kw.get("ocr_engine"))),
    )
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")},
        data={"ocr_engine": "vlm"},
    )
    assert res.status_code == 200
    assert scan.ocr.calls == []                 # 既定の DU は呼ばれない
    assert len(vlm.calls) == 1                  # VLM が 1 回だけ（経路ごとに呼ばない）
    assert sent["upload_bytes"].decode().startswith("[p.1]\nVLM1行目")
    assert seen["adb"] == ["p.1", "p.2"]
    assert seen["opensearch"] == "VLM1行目\nVLM2行目"


def test_conflicting_attributes_are_rejected_before_paying_for_ocr(scan, monkeypatch):
    """どうせ 422 になる要求のために OCR（課金）を先に走らせない（review-4 PREP03-002）。"""
    _use_real_add_file(monkeypatch)
    for name, body in (("photo.png", scan.png), ("scan.pdf", scan.pdf([""]))):
        res = client.post(
            "/api/rag/files", files={"file": (name, body, "application/octet-stream")},
            data={"attributes": json.dumps({"sheet": "p.9"})},
        )
        assert res.status_code == 422, name
        assert "sheet" in res.json()["detail"]
    assert scan.ocr.calls == []      # OCI を 1 回も呼んでいない
