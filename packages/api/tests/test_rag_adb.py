"""ADB 自前索引バックエンド(RAGM-02)の単体テスト。

実 DB を要する経路(取り込み・検索 SQL)は実環境 E2E（runs/<run-id>/e2e/）で担保する。
ここでは DB に触れない純ロジック——チャンクごとの出典の付き方、フィルタの組み立て、
引用の後方互換——を固定する。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import rag_adb
from jetuse_core.settings import Settings
from service.main import app
from service.routes import chat as chat_routes

client = TestClient(app)


# --- チャンク化: 出典がチャンクごとに変わること（マネージド VS との差の核） ----


def test_chunk_units_gives_distinct_cells_per_chunk():
    body = "\n".join(f"{i}行目の本文です。" * 6 for i in range(1, 41)).encode()
    units = rag_adb.chunk_units("spec.md", body)
    assert len(units) > 1
    cells = [u["cells"] for u in units]
    assert len(set(cells)) == len(cells)  # 同一ファイルでもチャンクごとに違う
    assert all(c.startswith("L") and ":" in c for c in cells)
    assert all(u["sheet"] == "本文" for u in units)


def test_chunk_units_splits_overlong_single_line_without_losing_text():
    """上限を超える 1 行は文字オフセット付きで分割する。

    分割しないと埋め込み側の 2000 文字切り詰めと本文が食い違い、生成時のプロンプトも破裂する。
    """
    line = "あ" * (rag_adb.CHUNK_CHARS * 3 + 10)
    units = rag_adb.chunk_units("a.txt", line.encode())
    assert len(units) == 4
    assert "".join(u["text"] for u in units) == line  # 欠落も重複もしない
    assert all(len(u["text"]) <= rag_adb.CHUNK_CHARS for u in units)
    assert units[0]["cells"] == f"L1c1-{rag_adb.CHUNK_CHARS}"
    assert len({u["cells"] for u in units}) == 4


def test_chunk_units_mixes_normal_and_overlong_lines():
    long_line = "ら" * (rag_adb.CHUNK_CHARS + 5)
    units = rag_adb.chunk_units("a.txt", f"短い行\n{long_line}\n短い行2".encode())
    joined = "".join(u["text"] for u in units)
    assert "短い行" in joined and "短い行2" in joined
    assert joined.count("ら") == rag_adb.CHUNK_CHARS + 5  # 長い行も欠落・重複しない
    assert all(len(u["text"]) <= rag_adb.CHUNK_CHARS for u in units)


def test_chunk_units_covers_every_line():
    lines = [f"{i}: " + "文" * 200 for i in range(1, 21)]
    units = rag_adb.chunk_units("a.txt", "\n".join(lines).encode())
    joined = "\n".join(u["text"] for u in units)
    assert all(line in joined for line in lines)


def test_chunk_units_empty_content():
    assert rag_adb.chunk_units("a.txt", b"") == []
    assert rag_adb.chunk_units("a.txt", b"\n\n  \n") == []


# --- フィルタ: 許可キーのみ・値は必ずバインド --------------------------------


def test_build_where_binds_values_and_scopes_to_owner():
    where, binds = rag_adb.build_where({"current_version": "Y", "kind": "spec"})
    assert where.startswith("WHERE owner_sub = :owner")
    assert "current_version = :flt_current_version" in where
    assert "kind = :flt_kind" in where
    assert binds == {"flt_current_version": "Y", "flt_kind": "spec"}


def test_build_where_without_filters_still_scopes_to_owner():
    where, binds = rag_adb.build_where(None)
    assert where == "WHERE owner_sub = :owner"
    assert binds == {}


def test_build_where_bind_names_avoid_reserved_words():
    """`:file` のような予約語をバインド名にすると ORA-01745 になる（実機で踏んだ）。"""
    where, binds = rag_adb.build_where({"file": "a.md"})
    assert ":file" not in where
    assert "doc_file = :flt_file" in where
    assert binds == {"flt_file": "a.md"}


def test_build_where_rejects_unknown_key():
    # 誤字が「静かに 0 件」ではなくエラーになること（SPIKE-M1 ①-b の教訓）
    with pytest.raises(ValueError, match="unsupported filter key"):
        rag_adb.build_where({"currentversion": "Y"})


def test_build_where_rejects_non_string_and_injection_shaped_values():
    with pytest.raises(ValueError):
        rag_adb.build_where({"kind": 1})
    where, binds = rag_adb.build_where({"kind": "spec' OR '1'='1"})
    # 値は SQL に現れず、バインドとしてだけ渡る
    assert "OR" not in where
    assert binds["flt_kind"] == "spec' OR '1'='1"


def test_build_where_skips_none_value():
    where, binds = rag_adb.build_where({"kind": None})
    assert where == "WHERE owner_sub = :owner"
    assert binds == {}


# --- 引用: 既存契約を壊さずチャンク単位の出典を足す --------------------------


def _row(**over):
    row = {
        "chunk_id": "f1-3", "file_id": "f1", "chunk_no": 3, "doc_file": "仕様書.md",
        "doc_version": "2.0", "sheet_name": "本文", "cells": "L12:L30",
        "sha256_head": "abc123def456", "kind": "doc", "current_version": "Y",
        "attrs": '{"source":"upload"}', "body": "本文テキスト", "dist": 0.2,
    }
    return {**row, **over}


def test_citations_keep_backward_compatible_fields():
    cites = rag_adb.citations([rag_adb._hit(_row())])
    assert set(cites[0]) >= {"file_id", "filename", "score"}
    assert cites[0]["file_id"] == "f1"
    assert cites[0]["filename"] == "仕様書.md"
    assert cites[0]["score"] == pytest.approx(0.8)


def test_citations_carry_chunk_level_source():
    src = rag_adb.citations([rag_adb._hit(_row())])[0]["source"]
    assert src["cells"] == "L12:L30"
    assert src["sheet"] == "本文"
    assert src["version"] == "2.0"
    assert src["chunk_id"] == "f1-3"
    assert src["attributes"] == {"source": "upload"}


def test_hit_tolerates_broken_attributes_json():
    assert rag_adb._hit(_row(attrs="{壊れた"))["source"]["attributes"] == {}


def test_hit_without_distance_has_no_score():
    assert rag_adb._hit(_row(dist=None))["score"] is None


# --- 設定・可用性 -------------------------------------------------------------


def test_ingest_embeds_client_side_not_in_db(monkeypatch):
    """埋め込みはクライアント側で行う（DB 内埋め込みは ADR-0021 の資格情報方針と両立しない）。

    実測: `UTL_TO_EMBEDDING` は `OCI$RESOURCE_PRINCIPAL` では ORA-24247 になる
    （docs/verification/RAGM-02.md）。SQL に DB 内埋め込みを混ぜていないことを固定する。
    """
    seen: dict = {}

    class FakeCursor:
        rowcount = 0

        def execute(self, sql, **binds):
            seen.setdefault("sql", []).append(sql)
            seen.setdefault("binds", []).append(binds)

        def fetchone(self):
            return ("0.0",)  # 台帳行ロックの SELECT にも「行あり」で応える

        def fetchall(self):
            return []

    class FakeConn:
        call_timeout = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "connect", lambda: FakeConn())
    monkeypatch.setattr(rag_adb, "embed", lambda texts, input_type: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(rag_adb, "ensure_indexes", lambda: {})
    n = rag_adb.ingest("u", "f1", "a.md", ("行\n" * 400).encode())
    assert n >= 1
    assert not any("UTL_TO_EMBEDDING" in s for s in seen["sql"])
    assert any("embedding" in b for b in seen["binds"])
    # 版の採番は文書レジストリ行を作ってからロックする(初回同時取り込みも直列化する)
    assert any("INSERT INTO rag_adb_docs" in s for s in seen["sql"])
    assert any("FOR UPDATE" in s for s in seen["sql"])


def test_availability_is_absent_without_db_configured(monkeypatch):
    monkeypatch.setattr(rag_adb, "get_settings", lambda: Settings(_env_file=None, adb_dsn=""))
    assert rag_adb.availability() == rag_adb.ABSENT
    assert rag_adb.enabled() is False


def test_availability_is_not_cached_so_later_outages_are_visible(monkeypatch):
    """一度 READY を見たら覚える実装だと、その後の障害を検出できなくなる。"""
    calls = {"n": 0}

    class Cur:
        def execute(self, sql, **binds):
            calls["n"] += 1

        def fetchone(self):
            return (3,)  # 3 表そろっている

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "get_settings",
                        lambda: Settings(_env_file=None, adb_dsn="dsn"))
    monkeypatch.setattr(rag_adb, "connect", lambda: Conn())
    assert rag_adb.availability() == rag_adb.READY

    def down():
        raise RuntimeError("connection lost")

    monkeypatch.setattr(rag_adb, "connect", lambda: down())
    assert rag_adb.availability() == rag_adb.UNAVAILABLE  # 覚えていたら READY のままになる


def test_search_sql_uses_approximate_search_so_the_vector_index_applies():
    """スケール検証で計画を取ったのと同じ形（`FETCH APPROX FIRST`）であること。

    ここが厳密検索(`FETCH FIRST`)に戻ると、検証で確認した HNSW + PRE-FILTER が
    実経路では使われない＝検証が実装を裏づけなくなる。
    """
    where, _ = rag_adb.build_where({"current_version": "Y"})
    sql = rag_adb.search_sql(where, 5)
    assert "FETCH APPROX FIRST 5 ROWS ONLY" in sql
    assert "VECTOR_DISTANCE(embedding, (SELECT q FROM qvec), COSINE)" in sql
    assert rag_adb.TABLE in sql


def test_search_sql_can_target_another_table_for_verification():
    """検証スクリプトが**同じ SQL**を別表へ当てられること（測った SQL と動く SQL を一致させる）。"""
    sql = rag_adb.search_sql("WHERE owner_sub = :owner", 3, table="SCALE_CHUNKS")
    assert "FROM SCALE_CHUNKS" in sql


def test_delete_chunks_runs_in_caller_transaction():
    """削除は呼び出し側のトランザクションで実行する（台帳行と同時に消えること）。"""
    calls = []

    class Cur:
        rowcount = 3

        def execute(self, sql, **binds):
            calls.append((sql, binds))

        def fetchall(self):
            return [("仕様書.md",)] if "SELECT DISTINCT doc_file" in calls[-1][0] else []

        def fetchone(self):
            if "FROM user_tables" in calls[-1][0]:
                return (1,)  # チャンク表は在る
            if "SELECT doc_version" in calls[-1][0]:
                return ("1.0",)  # 文書レジストリのロック
            return (1,)  # 現行版が残っている = 昇格しない

    assert rag_adb.delete_chunks(Cur(), "u", "f1") == 3
    deletes = [(s, b) for s, b in calls if s.strip().startswith("DELETE FROM")]
    assert deletes  # 呼び出し側の cursor で DELETE を実行している
    assert all(b.get("o") == "u" and b.get("f") == "f1" for _, b in deletes)


# --- API 面: rag_backend='adb' が既存3経路と同じ枠組みで通ること ---------------


def test_adb_backend_streams_single_delta(monkeypatch):
    def fake_generate(owner, prompt):
        return "回答本文です。", [{
            "file_id": "f1", "filename": "仕様書.md", "score": 0.8,
            "source": {"sheet": "本文", "cells": "L12:L30"},
        }]

    # dev 統合(M007): 非Responses系 RAG 分岐は owner_key_gate(DB) を通すため no-op 化
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag_adb, "generate", fake_generate)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_backend": "adb",
    })
    assert res.status_code == 200
    assert '"delta": "回答本文です。"' in res.text
    assert '"cells": "L12:L30"' in res.text  # チャンク単位の出典が SSE に載る
    assert res.text.rstrip().endswith("data: [DONE]")


def test_unknown_rag_backend_is_rejected():
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_backend": "adbb",
    })
    assert res.status_code == 422


# --- 削除の原子性（台帳行と同じトランザクションでチャンクを消す） ----------------


class _RecordingConn:
    """`rag.delete_file` の締めのトランザクション順序を観察するためのダミー接続。

    SYNC-01: RAGM-02 の `_delete_row()` は SP2-02 が台帳行の削除を `delete_file` の
    締めの Tx(rag_files + rag_file_ledger を同一 Tx で確定)へ移したため統合された。
    「チャンクを台帳行と同一 Tx で消す」契約はそのまま引き継いでいる。
    """

    def __init__(self, chunk_delete_error: Exception | None = None,
                 chunk_table_missing: bool = False):
        self.events: list[str] = []
        self.chunk_delete_error = chunk_delete_error
        self.chunk_table_missing = chunk_table_missing
        outer = self

        class Cur:
            rowcount = 1

            def __init__(self):
                self.last = ""

            def execute(self, sql, **binds):
                self.last = sql
                if "DELETE FROM rag_files" in sql:
                    outer.events.append("delete_ledger")
                elif "DELETE FROM rag_file_ledger" in sql:
                    outer.events.append("delete_reservation")
                elif "DELETE FROM rag_adb_chunks" in sql:
                    if outer.chunk_delete_error:
                        raise outer.chunk_delete_error
                    outer.events.append("delete_chunks")
                elif "SELECT oci_file_id" in sql:
                    outer.events.append("select")
                elif "FOR UPDATE" in sql:
                    # 取り込み中(rag_adb._ingest が同じ行をロック)の完了待ち
                    outer.events.append("lock")

            def fetchone(self):
                if "FROM user_tables" in self.last:
                    return (0,) if outer.chunk_table_missing else (1,)
                if "SELECT COUNT(*)" in self.last:
                    return (1,)  # 現行版が残っている
                if "FOR UPDATE" in self.last:
                    return ("f1",)
                return ("oci-1", "a.md")

            def fetchall(self):
                return []

        self._cur = Cur()

    def cursor(self):
        return self._cur

    def commit(self):
        self.events.append("commit")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_delete_externals(monkeypatch, conn):
    """`rag.delete_file` の外部先行削除(DP File / 原本 / OpenSearch / Select AI)を潰し、
    締めのトランザクションだけを観察できるようにする。"""
    from jetuse_core import rag, rag_opensearch, rag_select_ai

    monkeypatch.setattr(rag, "connect", lambda: conn)
    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag, "rag_ledger", type("L", (), {
        "rows_for_owner_by_id": staticmethod(lambda fid: None),
    })())
    monkeypatch.setattr(rag, "get_store_id", lambda owner: None)
    monkeypatch.setattr(rag, "_dp_for", lambda loc=None: object())
    monkeypatch.setattr(rag, "delete_external_file", lambda oid, dp=None: None)
    monkeypatch.setattr(rag, "delete_original_exact", lambda *a, **kw: None)
    monkeypatch.setattr(rag, "_delete_original_legacy", lambda *a, **kw: None)
    monkeypatch.setattr(rag_opensearch, "enabled", lambda: False)
    monkeypatch.setattr(rag_select_ai, "sync_remove_file", lambda owner, fid: None)
    return rag


def test_delete_removes_chunks_in_the_same_transaction(monkeypatch):
    conn = _RecordingConn()
    rag = _stub_delete_externals(monkeypatch, conn)
    monkeypatch.setattr(rag_adb, "enabled", lambda: True)
    assert rag.delete_file("u", "f1") is True
    # 締めの Tx: 行ロック → チャンク削除 → 台帳行 → 予約 → commit（すべて同一 Tx）
    assert conn.events == [
        "select", "lock", "delete_chunks", "delete_ledger", "delete_reservation", "commit",
    ]


def test_delete_does_not_commit_when_chunk_delete_fails(monkeypatch):
    """チャンクを消せないなら台帳行も消さない（削除成功を装わない）。"""
    conn = _RecordingConn(chunk_delete_error=RuntimeError("boom"))
    rag = _stub_delete_externals(monkeypatch, conn)
    monkeypatch.setattr(rag_adb, "enabled", lambda: True)
    with pytest.raises(RuntimeError):
        rag.delete_file("u", "f1")
    assert "commit" not in conn.events
    assert "delete_ledger" not in conn.events


def test_delete_fails_when_chunk_table_errors_even_if_enabled_check_would_fail(monkeypatch):
    """可用性チェックの失敗を「削除成功」に変えないこと。

    `enabled()` は別接続なので、瞬断やプール枯渇を False に丸める。それを削除のスキップ条件に
    すると「API は削除成功なのにチャンクが残る」が起きる。可用性は見ずに必ず消しにいく。
    """
    conn = _RecordingConn(chunk_delete_error=RuntimeError("connection lost"))
    rag = _stub_delete_externals(monkeypatch, conn)
    monkeypatch.setattr(rag_adb, "enabled", lambda: False)  # 「無効」でもスキップしない
    with pytest.raises(RuntimeError):
        rag.delete_file("u", "f1")
    assert "commit" not in conn.events


def test_delete_tolerates_missing_chunk_table(monkeypatch):
    """017 未適用（チャンク表が無い）だけは「消すものが無い」として台帳行の削除を通す。"""
    conn = _RecordingConn(chunk_table_missing=True)
    rag = _stub_delete_externals(monkeypatch, conn)
    assert rag.delete_file("u", "f1") is True
    assert conn.events == [
        "select", "lock", "delete_ledger", "delete_reservation", "commit",
    ]


def test_delete_fails_on_partial_migration(monkeypatch):
    """チャンク表はあるのに 018/019 が無い（部分適用）なら、削除を成功にしない。

    ここで握り潰して commit すると、台帳行だけ消えてチャンクが残る＝削除済み文書が
    回答に混ざり続ける。
    """
    import oracledb

    class Missing(oracledb.DatabaseError):
        def __str__(self):
            return "ORA-00942: table or view does not exist"

    conn = _RecordingConn(chunk_delete_error=Missing())
    rag = _stub_delete_externals(monkeypatch, conn)
    with pytest.raises(oracledb.DatabaseError):
        rag.delete_file("u", "f1")
    assert "commit" not in conn.events


# --- アップロード経路の配線（add_file → rag_adb.ingest） -----------------------


class _FakeLedger:
    """SP2-02 の予約 ledger(rag_ledger)の呼び出し面だけを再現する。"""

    rid = "00000000-0000-4000-8000-0000000000ad"

    def upload_gate(self):
        pass

    def reserve(self, owner_key, filename, ext):
        return self.rid

    def set_external(self, rid, ext_id):
        pass

    def release(self, rid):
        pass

    def confirm_in_tx(self, cur, rid):
        pass


def _stub_upload(monkeypatch):
    """`rag.add_file` の外部依存（予約 ledger / Vector Store / Files API / DB）を潰す。"""
    from jetuse_core import rag

    monkeypatch.setattr(rag, "rag_ledger", _FakeLedger())
    monkeypatch.setattr(rag, "owner_key_gate", lambda: None)
    monkeypatch.setattr(rag, "ensure_store", lambda owner, lease=None: "vs-1")
    monkeypatch.setattr(rag, "_put_original", lambda *a, **kw: None)
    monkeypatch.setattr(rag, "_insert_file_confirmed", lambda *a: None)

    class DP:
        class files:
            @staticmethod
            def create(file=None, purpose=None):
                return type("F", (), {"id": "oci-1"})()

        class vector_stores:
            class files:
                @staticmethod
                def create(vector_store_id=None, file_id=None, attributes=None):
                    return None

    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: DP())
    return rag


def test_add_file_ingests_into_adb_when_ready(monkeypatch):
    rag = _stub_upload(monkeypatch)
    seen = {}
    seen_fid = {}
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(rag_adb, "ingest",
                        lambda o, fid, name, content, **kw: (
                            seen.update(owner=o, name=name), seen_fid.update(v=fid)))
    out = rag.add_file("u", "a.md", b"body")
    assert out["status"] == "processing"
    assert seen == {"owner": "u", "name": "a.md"}
    # 取り込みに渡す file_id は SP2-02 の予約 ID(= rag_files.id = 返り値の id)
    assert seen_fid["v"] == out["id"] == _FakeLedger.rid


def test_add_file_skips_adb_when_table_absent(monkeypatch):
    rag = _stub_upload(monkeypatch)
    called = []
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.ABSENT)
    monkeypatch.setattr(rag_adb, "ingest", lambda *a: called.append(a))
    monkeypatch.setattr(rag_adb, "mark_unavailable", lambda *a: called.append(a))
    rag.add_file("u", "a.md", b"body")
    assert called == []  # 未導入なら何も記録しない


def test_add_file_records_error_when_adb_unavailable(monkeypatch):
    """「表が無い」ではなく「今つながらない」ときは、そのファイルを error として残す。"""
    rag = _stub_upload(monkeypatch)
    marked = {}
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.UNAVAILABLE)
    monkeypatch.setattr(rag_adb, "ingest", lambda *a: pytest.fail("取り込みを試してはいけない"))
    monkeypatch.setattr(rag_adb, "mark_unavailable",
                        lambda o, fid, name: marked.update(owner=o, file=fid))
    assert rag.add_file("u", "a.md", b"body")["status"] == "processing"
    assert marked == {"owner": "u", "file": marked.get("file")}
    assert marked["file"]


def test_add_file_survives_adb_ingest_failure(monkeypatch):
    """ADB 取り込みが落ちてもアップロード自体は成功させる（他バックエンドは成立しているため）。"""
    rag = _stub_upload(monkeypatch)
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)

    def boom(*a):
        raise RuntimeError("adb down")

    monkeypatch.setattr(rag_adb, "ingest", boom)
    assert rag.add_file("u", "a.md", b"body")["status"] == "processing"


def test_doc_key_is_byte_bounded_and_collision_free():
    """`VARCHAR2(400)` は BYTE セマンティクスのことがあるのでバイト長で切る。

    先頭を切るだけだと「先頭が同じ別ファイル」が同一文書に統合され、片方が勝手に旧版化される。
    """
    short = "仕様書.md"
    assert rag_adb.doc_key(short) == short  # 収まるなら素通し
    a = "あ" * 300 + "_A.md"
    b = "あ" * 300 + "_B.md"
    ka, kb = rag_adb.doc_key(a), rag_adb.doc_key(b)
    assert len(ka.encode()) <= rag_adb.DOC_FILE_MAX
    assert len(kb.encode()) <= rag_adb.DOC_FILE_MAX
    assert ka != kb  # 先頭が同じでも別文書として扱われる
    assert rag_adb.doc_key(a) == ka  # 同じ入力は同じキー


def test_ingest_uses_same_doc_key_everywhere(monkeypatch):
    """長いファイル名でも、採番・旧版化・INSERT が同じ値を使うこと。"""
    seen: dict = {}

    class FakeCursor:
        rowcount = 0

        def execute(self, sql, **binds):
            seen.setdefault("binds", []).append(binds)

        def fetchone(self):
            return ("0.0",)  # 台帳行ロックの SELECT にも「行あり」で応える

        def fetchall(self):
            return []

    class FakeConn:
        call_timeout = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "connect", lambda: FakeConn())
    monkeypatch.setattr(rag_adb, "embed", lambda texts, input_type: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(rag_adb, "ensure_indexes", lambda: {})
    long_name = "あ" * 500 + ".md"
    rag_adb.ingest("u", "f1", long_name, b"line1\nline2")
    used = {b.get("f") or b.get("doc_file") or b.get("dk") for b in seen["binds"]
            if b.get("f") or b.get("doc_file") or b.get("dk")}
    # `:f` は台帳行ロック（file_id）でも使うので、それだけ除いて文書キーの一致を見る
    assert used - {"f1"} == {rag_adb.doc_key(long_name)}


def test_search_raises_typed_error_when_table_missing(monkeypatch):
    import oracledb

    class Missing(oracledb.DatabaseError):
        def __str__(self):
            return "ORA-00942: table or view does not exist"

    class Cur:
        description = []

        def execute(self, sql, **binds):
            raise Missing()

    class Conn:
        call_timeout = 0

        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "connect", lambda: Conn())
    monkeypatch.setattr(rag_adb, "embed", lambda texts, input_type: [[0.1] * 4])
    with pytest.raises(rag_adb.AdbBackendUnavailable):
        rag_adb.search("u", "q")


# --- 版の昇格・失敗状態（review-3 M-001 / M-003） -------------------------------


class _VersionCur:
    """`promote_latest_version` の分岐を観察する簡易カーソル。"""

    def __init__(self, current_count: int, versions: list[str]):
        self.current_count = current_count
        self.versions = versions
        self.updates: list[dict] = []
        self.last = ""

    def execute(self, sql, **binds):
        self.last = sql
        if sql.strip().startswith("UPDATE"):
            self.updates.append(binds)

    def fetchone(self):
        return (self.current_count,)

    def fetchall(self):
        return [(v,) for v in self.versions]


def test_promote_latest_version_restores_newest_remaining():
    """現行版を消したら、残っている最大版を現行へ戻す（旧版が永久に見えなくなるのを防ぐ）。"""
    cur = _VersionCur(current_count=0, versions=["1.0", "3.0", "2.0"])
    assert rag_adb.promote_latest_version(cur, "u", "a.md") == "3.0"
    assert cur.updates[-1]["v"] == "3.0"


def test_promote_latest_version_noop_when_current_exists():
    cur = _VersionCur(current_count=2, versions=["1.0", "2.0"])
    assert rag_adb.promote_latest_version(cur, "u", "a.md") is None
    assert cur.updates == []


def test_promote_latest_version_noop_when_nothing_left():
    cur = _VersionCur(current_count=0, versions=[])
    assert rag_adb.promote_latest_version(cur, "u", "a.md") is None
    assert cur.updates == []


def test_ingest_rejects_unsupported_format(monkeypatch):
    """DOCX / 画像などを UTF-8 として読んで「文字化けした本文」を indexed にしない。"""
    marked = {}
    monkeypatch.setattr(rag_adb, "_mark_failed", lambda o, d, f, msg: marked.update(msg=msg))
    assert rag_adb.ingest("u", "f1", "a.docx", b"PK\x03\x04binary") == 0
    assert "取り出せません" in marked["msg"]


def test_ingest_rejects_non_utf8_text(monkeypatch):
    marked = {}
    monkeypatch.setattr(rag_adb, "_mark_failed", lambda o, d, f, msg: marked.update(msg=msg))
    assert rag_adb.ingest("u", "f1", "a.txt", b"\xff\xfe\x00binary") == 0
    assert "UTF-8" in marked["msg"]


def test_ingest_records_error_when_no_text_extracted(monkeypatch):
    """0 チャンク（本文を取り出せない PDF 等）は成功にせず error として残す。"""
    marked = {}
    monkeypatch.setattr(rag_adb, "_mark_failed",
                        lambda o, d, f, msg: marked.update(owner=o, doc=d, file=f, msg=msg))
    assert rag_adb.ingest("u", "f1", "empty.txt", b"   \n\n  ") == 0
    assert marked["file"] == "f1"
    assert "0 チャンク" in marked["msg"]


def test_ingest_records_error_and_reraises_on_failure(monkeypatch):
    marked = {}
    monkeypatch.setattr(rag_adb, "_mark_failed", lambda o, d, f, msg: marked.update(msg=msg))

    def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(rag_adb, "_ingest", boom)
    with pytest.raises(RuntimeError):
        rag_adb.ingest("u", "f1", "a.md", b"line1\nline2")
    assert "db down" in marked["msg"]


def test_backend_badge_shows_error_for_failed_ingest(monkeypatch):
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: set())
    monkeypatch.setattr(ros, "enabled", lambda: False)
    monkeypatch.setattr(rag_adb, "enabled", lambda: True)
    monkeypatch.setattr(rag_adb, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(rag_adb, "errored_file_ids", lambda owner: {"f2"})
    out = rag.attach_backend_status("u", [
        {"id": "f1", "filename": "a.md", "status": "completed"},
        {"id": "f2", "filename": "b.pdf", "status": "completed"},
        {"id": "f3", "filename": "c.md", "status": "processing"},
    ])
    assert [f["backends"]["adb"] for f in out] == ["indexed", "error", "pending"]


def test_chunk_units_counts_newlines_toward_the_limit():
    """改行を数えないと保存本文が上限を超え、埋め込みだけが切り詰められる。"""
    units = rag_adb.chunk_units("a.txt", ("あ\n" * 2000).encode())
    assert units
    assert all(len(u["text"]) <= rag_adb.CHUNK_CHARS for u in units)


def test_chunk_units_overlap_never_exceeds_limit():
    line = "ん" * (rag_adb.CHUNK_CHARS - 10)
    units = rag_adb.chunk_units("a.txt", f"{line}\n{line}\n{line}".encode())
    assert all(len(u["text"]) <= rag_adb.CHUNK_CHARS for u in units)


def test_ingest_embeds_in_bounded_batches_outside_the_transaction(monkeypatch):
    """埋め込みは有界バッチで、かつ **DB 接続を取る前に**終わらせる。

    接続とロックを保持したまま外部 API を待つと、接続プール（最大 4）が枯渇して
    チャットや削除まで巻き添えで失敗する。
    """
    order = []
    batch_sizes = []

    class FakeCursor:
        rowcount = 0

        def execute(self, sql, **binds):
            pass

        def fetchone(self):
            return ("0.0",)  # 台帳行ロックの SELECT にも「行あり」で応える

        def fetchall(self):
            return []

    class FakeConn:
        call_timeout = 0

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_embed(texts, input_type):
        order.append("embed")
        batch_sizes.append(len(texts))
        return [[0.1] * 4 for _ in texts]

    def fake_connect():
        order.append("connect")
        return FakeConn()

    monkeypatch.setattr(rag_adb, "connect", fake_connect)
    monkeypatch.setattr(rag_adb, "embed", fake_embed)
    monkeypatch.setattr(rag_adb, "ensure_indexes", lambda: {})
    body = "\n".join("行" * 300 for _ in range(1500)).encode()
    n = rag_adb.ingest("u", "f1", "big.md", body)
    assert n > rag_adb.EMBED_BATCH  # バッチ境界を跨ぐ規模
    assert max(batch_sizes) <= rag_adb.EMBED_BATCH
    assert sum(batch_sizes) == n
    # 埋め込みがすべて終わってから接続する（接続中に外部 API を待たない）
    assert order.index("connect") > max(i for i, o in enumerate(order) if o == "embed")


def test_ingest_rejects_files_with_too_many_chunks(monkeypatch):
    """上限を超える巨大ファイルは取り込まず error として残す（メモリと時間の上限）。"""
    marked = {}
    monkeypatch.setattr(rag_adb, "_mark_failed", lambda o, d, f, msg: marked.update(msg=msg))
    monkeypatch.setattr(rag_adb, "chunk_units",
                        lambda name, content, **kw: [{"sheet": "本文", "cells": f"L{i}:L{i}",
                                                "text": "本文"}
                                               for i in range(rag_adb.MAX_CHUNKS + 1)])
    assert rag_adb.ingest("u", "f1", "huge.md", b"x") == 0
    assert "上限" in marked["msg"]


def test_lock_doc_reraises_non_unique_integrity_errors(monkeypatch):
    import oracledb

    class Other(oracledb.IntegrityError):
        def __str__(self):
            return "ORA-02290: check constraint violated"

    class Cur:
        def execute(self, sql, **binds):
            if "INSERT INTO rag_adb_docs" in sql:
                raise Other()

        def fetchone(self):
            return ("1.0",)

    with pytest.raises(oracledb.IntegrityError):
        rag_adb._lock_doc(Cur(), "u", "a.md")


def test_lock_doc_fails_when_row_missing_after_lock():
    class Cur:
        def execute(self, sql, **binds):
            pass

        def fetchone(self):
            return None

    with pytest.raises(RuntimeError, match="文書レジストリ"):
        rag_adb._lock_doc(Cur(), "u", "a.md")


def test_ingest_aborts_when_file_was_deleted_meanwhile(monkeypatch):
    """取り込み中に削除されたら、チャンクを 1 行も作らずに中止する。

    台帳行を `FOR UPDATE` で押さえてから作るので、削除が先行していれば行が無い。
    """
    inserted = []

    class Cur:
        rowcount = 0

        def execute(self, sql, **binds):
            if "INSERT INTO rag_adb_chunks" in sql:
                inserted.append(binds)
            self.last = sql

        def fetchone(self):
            return None  # 台帳行が無い = 既に削除された

        def fetchall(self):
            return []

    class Conn:
        call_timeout = 0

        def cursor(self):
            return Cur()

        def commit(self):
            pytest.fail("中止すべきなのに commit した")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "connect", lambda: Conn())
    monkeypatch.setattr(rag_adb, "embed", lambda texts, input_type: [[0.1] * 4 for _ in texts])
    monkeypatch.setattr(rag_adb, "ensure_indexes", lambda: {})
    monkeypatch.setattr(rag_adb, "_mark_failed",
                        lambda *a: pytest.fail("削除による中止は error 状態にしない"))
    assert rag_adb.ingest("u", "gone", "a.md", b"line1\nline2") == 0
    assert inserted == []


def _availability_with(found: int, monkeypatch):
    class Cur:
        def execute(self, sql, **binds):
            pass

        def fetchone(self):
            return (found,)

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rag_adb, "get_settings", lambda: Settings(_env_file=None, adb_dsn="dsn"))
    monkeypatch.setattr(rag_adb, "connect", lambda: Conn())
    return rag_adb.availability()


def test_partial_migration_is_not_ready(monkeypatch):
    """017 だけ適用された状態を READY にすると、取り込みも失敗記録もできず永久 pending になる。"""
    assert _availability_with(3, monkeypatch) == rag_adb.READY
    assert _availability_with(1, monkeypatch) == rag_adb.UNAVAILABLE  # 部分適用
    assert _availability_with(0, monkeypatch) == rag_adb.ABSENT       # 未導入


def test_chunk_units_aborts_on_oversized_text():
    """上限超えは全量を展開しきってから判定しない（展開爆発でメモリを食わない）。"""
    big = ("あ" * 1000 + "\n") * (rag_adb.MAX_EXTRACT_CHARS // 1000 + 10)
    with pytest.raises(rag_adb.TooLarge):
        rag_adb.chunk_units("a.md", big.encode())


def test_delete_locks_document_registry_before_promoting(monkeypatch):
    """削除・昇格も取り込みと同じロック順序（文書レジストリ → チャンク）に乗る。"""
    order = []

    class Cur:
        rowcount = 1

        def __init__(self):
            self.last = ""

        def execute(self, sql, **binds):
            self.last = sql
            if "SELECT DISTINCT doc_file" in sql:
                order.append("list_docs")
            elif "FOR UPDATE" in sql and "rag_adb_docs" in sql:
                order.append("lock_doc")
            elif sql.strip().startswith("DELETE FROM rag_adb_chunks"):
                order.append("delete_chunks")

        def fetchone(self):
            if "SELECT COUNT(*)" in self.last:
                return (1,)  # 現行版が残っている = 昇格不要
            return ("1.0",)

        def fetchall(self):
            return [("a.md",)] if "SELECT DISTINCT doc_file" in self.last else []

    rag_adb.delete_chunks(Cur(), "u", "f1")
    assert order.index("lock_doc") < order.index("delete_chunks")
