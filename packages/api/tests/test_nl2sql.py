"""NL2SQL(SQL-02)のガードとエンドポイントのテスト。"""

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core import datasets, nl2sql
from jetuse_core.nl2sql import SqlRejectedError, sanitize_sql
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)

_FAKE_SCHEMA = {
    "schema": "SH",
    "tables": [{"name": "SALES", "comment": "売上明細", "rows": 1, "columns": []}],
}


@pytest.fixture(autouse=True)
def dbchat_defaults(monkeypatch):
    """既定: SHサンプルは読める(sample_available=True) / SEMSTORE_OCIDは設定済み
    (=既存の"sql_search既定"経路のテストが引き続き成立するようにする)。
    unset/emptyにしたいテストは各テスト内で上書きする(PORT-02)。"""
    monkeypatch.setattr(service_main.nl2sql, "get_schema_info", lambda: dict(_FAKE_SCHEMA))
    monkeypatch.setenv("SEMSTORE_OCID", "ocid1.semanticstore.oc1..default")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_sanitize_accepts_select_and_with():
    assert sanitize_sql("SELECT 1 FROM dual;").startswith("SELECT")
    cte = sanitize_sql("  with t as (select 1 c from dual) select * from t")
    assert cte.lower().startswith("with")
    # コメント除去後に判定
    assert sanitize_sql("/* note */ -- c\nSELECT 1 FROM dual").startswith("SELECT")
    # 末尾コメント+セミコロンの単一SELECTは安全(除去後に単文)
    assert sanitize_sql("SELECT * FROM t WHERE 1=1; --").endswith("1=1")


def test_sanitize_rejects_non_select():
    for bad in (
        "DELETE FROM sh.sales",
        "UPDATE t SET a=1",
        "SELECT 1 FROM dual; DROP TABLE t",
        "BEGIN NULL; END;",
        "/* SELECT */ DROP TABLE t",
        "WITH t AS (SELECT 1 FROM dual) DELETE FROM x",
    ):
        with pytest.raises(SqlRejectedError):
            sanitize_sql(bad)


def test_execute_endpoint_guards(monkeypatch):
    res = client.post("/api/dbchat/execute", json={"sql": "DROP TABLE sh.sales"})
    assert res.status_code == 400

    def fake_exec(sql, owner_key=None):
        # SP2-02: execute は呼び出し元 owner でコンテキストを設定する(specs/18 §4.3)
        assert owner_key == "dev-user"
        return {"columns": ["C"], "rows": [["1"]], "row_count": 1, "truncated": False}

    monkeypatch.setattr(service_main.nl2sql, "execute_readonly", fake_exec)
    res = client.post("/api/dbchat/execute", json={"sql": "SELECT 1 FROM dual"})
    assert res.status_code == 200
    assert res.json()["columns"] == ["C"]


def test_nl2sql_generate_stream(monkeypatch):
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql", lambda q: "SELECT 1 FROM dual"
    )
    res = client.post("/api/chat/nl2sql", json={"question": "売上は？"})
    assert res.status_code == 200
    assert '"sql": "SELECT 1 FROM dual"' in res.text
    assert res.text.rstrip().endswith("data: [DONE]")


def test_nl2sql_generate_error(monkeypatch):
    def boom(q):
        raise RuntimeError("backend down")

    monkeypatch.setattr(service_main.nl2sql, "generate_sql", boom)
    res = client.post("/api/chat/nl2sql", json={"question": "x"})
    assert res.status_code == 200  # SSE内でエラーイベント
    assert '"error"' in res.text


def test_schema_endpoint(monkeypatch):
    fake = {"schema": "SH", "tables": [{"name": "SALES", "comment": "売上明細",
                                        "rows": 918843, "columns": []}]}
    monkeypatch.setattr(service_main.nl2sql, "get_schema_info", lambda: fake)
    res = client.get("/api/dbchat/schema")
    assert res.status_code == 200
    assert res.json()["tables"][0]["name"] == "SALES"


def test_chart_suggest_endpoint(monkeypatch):
    spec = {"type": "bar", "x": "CHANNEL_DESC", "y": ["TOTAL_SALES"],
            "title": "チャネル別売上", "reason": "カテゴリ比較"}
    monkeypatch.setattr(service_main.nl2sql, "suggest_chart", lambda q, c, r: spec)
    res = client.post("/api/dbchat/chart", json={
        "question": "売上", "columns": ["CHANNEL_DESC", "TOTAL_SALES"],
        "rows": [["Direct", "100"]],
    })
    assert res.status_code == 200
    assert res.json()["type"] == "bar"


def test_suggest_chart_validates_columns(monkeypatch):
    import jetuse_core.nl2sql as mod

    monkeypatch.setattr(
        mod, "complete_once",
        lambda *a, **k: '{"type":"bar","x":"NO_SUCH","y":["TOTAL"],"title":"t","reason":"r"}',
        raising=False,
    )
    # complete_onceはchatモジュールからの遅延importなのでそちらを差し替え
    import jetuse_core.chat as chat_mod
    monkeypatch.setattr(
        chat_mod, "complete_once",
        lambda *a, **k: '{"type":"bar","x":"NO_SUCH","y":["TOTAL"],"title":"t","reason":"r"}',
    )
    out = mod.suggest_chart("q", ["A", "B"], [["1", "2"]])
    assert out["type"] == "none"


def test_nl2sql_backend_dispatch(monkeypatch):
    called = {}
    monkeypatch.setattr(service_main.nl2sql, "generate_sql",
                        lambda q: called.setdefault("b", "ss") or "SELECT 1 FROM dual")
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai",
                        lambda q, **k: called.setdefault("b", "sai") or "SELECT 2 FROM dual")
    res = client.post("/api/chat/nl2sql", json={"question": "x", "backend": "select_ai"})
    assert res.status_code == 200
    assert called["b"] == "sai"


def test_resolve_select_ai_model_fallback():
    # 既知モデルはそのまま、未知/未指定は既定にフォールバック(feedback 20260620 #3)
    assert nl2sql.resolve_select_ai_model(nl2sql.DEFAULT_SELECT_AI_MODEL) == \
        nl2sql.DEFAULT_SELECT_AI_MODEL
    assert nl2sql.resolve_select_ai_model("no.such-model") == nl2sql.DEFAULT_SELECT_AI_MODEL
    assert nl2sql.resolve_select_ai_model(None) == nl2sql.DEFAULT_SELECT_AI_MODEL
    valid = {m["key"] for m in nl2sql.SELECT_AI_MODELS}
    assert nl2sql.DEFAULT_SELECT_AI_MODEL in valid


def test_select_ai_models_endpoint():
    res = client.get("/api/dbchat/select-ai-models")
    assert res.status_code == 200
    body = res.json()
    assert body["default"] == nl2sql.DEFAULT_SELECT_AI_MODEL
    assert any(m["key"] == nl2sql.DEFAULT_SELECT_AI_MODEL for m in body["models"])
    assert all("label" in m and "key" in m for m in body["models"])


def test_nl2sql_model_passed_through(monkeypatch):
    # 選択モデルが generate_sql_select_ai まで伝搬すること(feedback 20260620 #3)
    seen = {}
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql_select_ai",
        lambda q, **k: seen.update(k) or "SELECT 1 FROM dual",
    )
    res = client.post("/api/chat/nl2sql",
                      json={"question": "x", "backend": "select_ai",
                            "model": "cohere.command-a-03-2025"})
    assert res.status_code == 200
    assert seen.get("model") == "cohere.command-a-03-2025"


def test_seed_datasets_endpoint(monkeypatch):
    # サンプル投入ルートが datasets.seed_samples を呼び結果を返す(feedback 20260620 #12)
    monkeypatch.setattr(
        datasets, "seed_samples",
        lambda owner, model=None: {"datasets": [{"id": "1"}], "ready": True, "skipped": 0},
    )
    res = client.post("/api/db/datasets/seed", json={})
    assert res.status_code == 200
    assert res.json()["ready"] is True


def test_execute_readonly_gates_owner_key_before_db(monkeypatch):
    """review-11 B003: owner_key 付き execute_readonly は登録簿/VPD/DB より前に
    owner_key_gate を通す(route だけでなく Fn 経路も直接呼ぶ共有チョークポイント)。"""
    from jetuse_core import owner_keys, vpd
    from jetuse_core.owner_keys import OwnerKeyPreflightError

    monkeypatch.setattr(vpd, "integrity_gate", lambda: None)

    def boom():
        raise OwnerKeyPreflightError("pending")

    monkeypatch.setattr(owner_keys, "owner_key_gate", boom)
    monkeypatch.setattr(nl2sql, "_get_query_pool",
                        lambda: pytest.fail("gate must block before DB"))
    with pytest.raises(OwnerKeyPreflightError):
        nl2sql.execute_readonly("SELECT 1 FROM dual", owner_key="demo_abc")


def test_execute_readonly_ownerless_skips_gate(monkeypatch):
    """owner なしモード(agent/SH 固定照会)は owner_key_gate 非対象(owner 非依存)。"""
    from jetuse_core import owner_keys, vpd

    calls: list[int] = []
    monkeypatch.setattr(vpd, "integrity_gate", lambda: None)
    monkeypatch.setattr(owner_keys, "owner_key_gate", lambda: calls.append(1))
    monkeypatch.setattr(nl2sql, "enforce_sql_boundary", lambda *a, **k: None)
    monkeypatch.setattr(nl2sql, "_get_query_pool",
                        lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        nl2sql.execute_readonly("SELECT 1 FROM dual", owner_key=None)
    assert calls == []  # owner なしはゲートを呼ばない


# --- PORT-02: dbchat既定切替(SEMSTORE_OCID空→select_ai) ---


def test_generate_sql_raises_hinted_error_when_semstore_unset(monkeypatch):
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as ei:
        nl2sql.generate_sql("売上は？")
    assert "SEMSTORE_OCID" in str(ei.value)
    assert "Select AI" in str(ei.value)


def test_sample_target_defaults_to_select_ai_when_semstore_unset(monkeypatch):
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    get_settings.cache_clear()
    called = {}
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql",
        lambda q: called.setdefault("b", "ss") or "SELECT 1 FROM dual",
    )
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql_select_ai",
        lambda q, **k: called.setdefault("b", "sai") or "SELECT 2 FROM dual",
    )
    res = client.post("/api/chat/nl2sql", json={"question": "売上は？"})
    assert res.status_code == 200
    assert called["b"] == "sai"


def test_explicit_sql_search_also_switches_to_select_ai_when_semstore_unset(monkeypatch):
    """PORT-02: web UIは常にbackendを明示送信し既定値は"sql_search"のため、「未指定」と
    「明示sql_search」をワイヤ上で区別できない(対象areaはpackages/apiのためUI変更は
    このタスクでは行わない — schemas.Nl2SqlRequestのponytailコメント参照)。よって
    backend="sql_search"は実UIの既定操作(=通常の「サンプルに質問する」)そのものであり、
    ここを弾くとSEMSTORE_OCID未設定環境でdbchatの既定挙動が実UIから一切到達不能になる
    (レビューでblocker指摘)。実UI到達性を優先しselect_aiへ自動切替する。"""
    monkeypatch.delenv("SEMSTORE_OCID", raising=False)
    get_settings.cache_clear()
    called = {}
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql_select_ai",
        lambda q, **k: called.setdefault("b", "sai") or "SELECT 1 FROM dual",
    )
    res = client.post(
        "/api/chat/nl2sql", json={"question": "売上は？", "backend": "sql_search"}
    )
    assert res.status_code == 200
    assert called["b"] == "sai"


def test_sample_target_uses_semantic_store_when_semstore_set(monkeypatch):
    # dbchat_defaults フィクスチャがSEMSTORE_OCIDを設定済み → 従来どおりsql_search経路
    called = {}
    monkeypatch.setattr(
        service_main.nl2sql, "generate_sql",
        lambda q: called.setdefault("b", "ss") or "SELECT 1 FROM dual",
    )
    res = client.post("/api/chat/nl2sql", json={"question": "売上は？"})
    assert res.status_code == 200
    assert called["b"] == "ss"


def test_sh_sample_status_available_when_tables_present():
    assert nl2sql.sh_sample_status() == {"available": True}


def test_sh_sample_status_unavailable_when_no_tables(monkeypatch):
    monkeypatch.setattr(
        service_main.nl2sql, "get_schema_info",
        lambda: {"schema": "SH", "tables": []},
    )
    status = nl2sql.sh_sample_status()
    assert status["available"] is False
    assert "SH" in status["reason"]


def test_sample_target_unavailable_returns_hint_without_generating(monkeypatch):
    monkeypatch.setattr(
        service_main.nl2sql, "get_schema_info",
        lambda: {"schema": "SH", "tables": []},
    )

    def boom(*a, **kw):
        raise AssertionError("SQL生成を呼んではいけない(SH未整備の時点で打ち切る)")

    monkeypatch.setattr(service_main.nl2sql, "generate_sql", boom)
    monkeypatch.setattr(service_main.nl2sql, "generate_sql_select_ai", boom)
    res = client.post("/api/chat/nl2sql", json={"question": "売上は？"})
    assert res.status_code == 200
    assert "SH" in res.text
    assert '"error"' in res.text


def test_sample_target_precheck_crash_returns_sse_error_not_500(monkeypatch):
    # PORT-02 レビュー指摘F-003: sh_sample_status()自体が例外を投げても、SSE契約を破る
    # 生500にせずSSEの{"error":...}へ正規化する。
    def boom():
        raise RuntimeError("DPY-4000: unable to find wallet")

    monkeypatch.setattr(service_main.nl2sql, "sh_sample_status", boom)
    res = client.post("/api/chat/nl2sql", json={"question": "売上は？"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert '"error"' in res.text
    assert res.text.rstrip().endswith("data: [DONE]")


def test_schema_endpoint_reports_sample_available():
    res = client.get("/api/dbchat/schema")
    assert res.status_code == 200
    body = res.json()
    assert body["sample_available"] is True
    assert "sample_unavailable_reason" not in body


def test_schema_endpoint_reports_sample_unavailable_reason(monkeypatch):
    monkeypatch.setattr(
        service_main.nl2sql, "get_schema_info",
        lambda: {"schema": "SH", "tables": []},
    )
    res = client.get("/api/dbchat/schema")
    body = res.json()
    assert body["sample_available"] is False
    assert body["sample_unavailable_reason"]


# --- PORT-02: Select AI可視化(create_profileのヒント付きエラー) ---


def test_create_profile_wraps_database_error_with_hint():
    import oracledb as oracledb_mod

    class FakeErr:
        code = 20000
        message = "ORA-20000"

    class FakeCursor:
        def execute(self, sql, **kw):
            if "CREATE_PROFILE" in sql:
                raise oracledb_mod.DatabaseError(FakeErr())

    with pytest.raises(RuntimeError) as ei:
        nl2sql.create_profile(FakeCursor(), "PROF", "meta.llama-3.3-70b-instruct", [])
    assert "generative-ai-family" in str(ei.value)
    assert "/api/health" in str(ei.value)


def test_sample_data_csv_valid():
    # 同梱サンプルCSVがヘッダ+データ行を持ち、列名がASCIIであること(feedback 20260620 #12)
    from jetuse_core.sample_data import SAMPLE_DATASETS

    assert len(SAMPLE_DATASETS) >= 2
    for display_name, csv_text in SAMPLE_DATASETS:
        assert display_name
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        assert len(lines) >= 3  # ヘッダ + 2行以上
        header = lines[0].split(",")
        assert all(h.isascii() and h == h.lower() for h in header)
        # 全行の列数がヘッダと一致
        assert all(len(ln.split(",")) == len(header) for ln in lines)
