"""NL2SQL(SQL-02)のガードとエンドポイントのテスト。"""

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core import datasets, nl2sql
from jetuse_core.nl2sql import SqlRejectedError, sanitize_sql
from service.main import app

client = TestClient(app)


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
