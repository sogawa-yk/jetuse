"""実 Platform API ルート(PAPI-03)の API テスト。

各エンドポイントの冒頭 authorize(JWT 検証 → scope 強制 → テナント一致 → 監査)と、通過後の
委譲(db.query 読取限定)/配管(connector.invoke / rag.search)を検証する。監査は best-effort で
実 DB を触るため、ユニットでは `record_broker_access` を捕捉して DB に依存させない(記録は E2E)。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import nl2sql, rag
from jetuse_core import platform_broker as pb
from jetuse_core.plugins import connector_store
from jetuse_core.settings import Settings, get_settings
from service.main import app

SECRET = "papi03-test-broker-secret-32bytes!"
TENANT = "ocid1.generativeaiproject.oc1.ap-osaka-1.aaaaaaaatenanta"
TENANT_B = "ocid1.generativeaiproject.oc1.ap-osaka-1.bbbbbbbbtenantb"
PLUGIN = "acme/faq-summarizer"

DB_QUERY = "platform:db.query"
RAG_SEARCH = "platform:rag.search"
CONNECTOR_INVOKE = "platform:connector.invoke"


def _settings(secret: str = SECRET, ttl: int = 300) -> Settings:
    return Settings(platform_broker_secret=secret, platform_token_ttl_seconds=ttl)


@pytest.fixture
def audit(monkeypatch):
    """authorize が残す監査行を捕捉する(実 DB を触らせない)。"""
    records: list[dict] = []
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _token(scopes, *, tenant=TENANT, plugin=PLUGIN, secret=SECRET) -> str:
    return pb.issue_broker_token(plugin, tenant, scopes, settings=_settings(secret=secret))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- db.query: 読取限定で既存エンジンへ委譲 ----------------------------------


def test_db_query_happy_path_delegates_and_audits_allow(client, audit, monkeypatch):
    captured = {}

    def fake_exec(sql):
        captured["sql"] = sql
        return {"columns": ["X"], "rows": [[1]], "row_count": 1, "truncated": False}

    monkeypatch.setattr(nl2sql, "execute_readonly", fake_exec)
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 AS x FROM dual"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 200, res.text
    assert res.json()["row_count"] == 1
    assert captured["sql"] == "SELECT 1 AS x FROM dual"
    allows = [r for r in audit if r["decision"] == "ALLOW"]
    assert len(allows) == 1
    assert allows[0]["scope"] == DB_QUERY
    assert allows[0]["tenant"] == TENANT


def test_db_query_rejects_write_400(client, audit):
    # sanitize_sql(execute_readonly 内)が非 SELECT を pool 取得前に弾く → 読取限定の強制。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "UPDATE t SET a = 1"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 400, res.text
    # authorize は通っている(ALLOW 監査が残る)。拒否は読取限定違反として委譲先で起きる。
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_db_query_scope_denied_403(client, audit):
    # rag.search だけのトークンで db.query を要求 → scope_denied → 403、DENY 監査。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 403, res.text
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "scope_denied"


def test_db_query_tenant_mismatch_403(client, audit):
    # トークン tenant=TENANT だが要求 tenant=TENANT_B → tenant_mismatch → 403。
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT_B, "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([DB_QUERY], tenant=TENANT)),
    )
    assert res.status_code == 403, res.text
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "tenant_mismatch"


def test_db_query_invalid_token_401(client, audit):
    bad = _token([DB_QUERY]) + "tampered"
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(bad),
    )
    assert res.status_code == 401, res.text
    assert any(r["decision"] == "DENY" for r in audit)


def test_db_query_missing_token_401(client, audit):
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
    )
    assert res.status_code == 401, res.text
    # 欠如(空トークン)も authorize を通って DENY 監査に残る(全アクセス監査)。
    assert any(r["decision"] == "DENY" for r in audit)


def test_db_query_wrong_secret_401(client, audit):
    # 別の鍵で署名されたトークンは検証で落ちる(fail-closed) → 401。
    forged = _token([DB_QUERY], secret="some-other-secret-32bytes-padding!")
    res = client.post(
        "/platform/db/query",
        json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
        headers=_auth(forged),
    )
    assert res.status_code == 401, res.text


def test_db_query_broker_unconfigured_503(audit):
    # 署名鍵未設定 = BrokerConfigError → 503。audit fixture で実 DB を触らせない。
    app.dependency_overrides[get_settings] = lambda: _settings(secret="")
    try:
        c = TestClient(app)
        res = c.post(
            "/platform/db/query",
            json={"tenant": TENANT, "sql": "SELECT 1 FROM dual"},
            headers=_auth("any.token.value"),
        )
        assert res.status_code == 503, res.text
    finally:
        app.dependency_overrides.clear()


def test_db_query_missing_tenant_422(client):
    res = client.post(
        "/platform/db/query",
        json={"tenant": "   ", "sql": "SELECT 1 FROM dual"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 422, res.text


# --- connector.invoke: 配管まで(実 MCP は CON-02/03) ------------------------


def _connector_record(cid, *, plugin_id=PLUGIN, actions=("post_message",)):
    return {
        "id": cid,
        "plugin_id": plugin_id,
        "definition": {"actions": [{"name": n} for n in actions]},
    }


def test_connector_invoke_plumbing_501(client, audit, monkeypatch):
    monkeypatch.setattr(
        connector_store, "get_connector", lambda cid: _connector_record(cid)
    )
    res = client.post(
        "/platform/connector/invoke",
        json={
            "tenant": TENANT,
            "connector_id": "conn-1",
            "action": "post_message",
            "params": {"text": "hi"},
        },
        headers=_auth(_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 501, res.text
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_connector_invoke_foreign_plugin_403(client, audit, monkeypatch):
    # コネクタが別プラグイン所属 → 認可トークンの sub と不一致で 403(プラグイン境界)。
    monkeypatch.setattr(
        connector_store,
        "get_connector",
        lambda cid: _connector_record(cid, plugin_id="other/owner"),
    )
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "post_message"},
        headers=_auth(_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 403, res.text


def test_connector_invoke_scope_denied_403(client, audit):
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "x"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 403, res.text
    assert any(r["decision"] == "DENY" and r["reason"] == "scope_denied" for r in audit)


def test_connector_invoke_unknown_connector_404(client, audit, monkeypatch):
    monkeypatch.setattr(connector_store, "get_connector", lambda cid: None)
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "missing", "action": "x"},
        headers=_auth(_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 404, res.text


def test_connector_invoke_unknown_action_404(client, audit, monkeypatch):
    monkeypatch.setattr(
        connector_store, "get_connector", lambda cid: _connector_record(cid)
    )
    res = client.post(
        "/platform/connector/invoke",
        json={"tenant": TENANT, "connector_id": "conn-1", "action": "delete_all"},
        headers=_auth(_token([CONNECTOR_INVOKE])),
    )
    assert res.status_code == 404, res.text


# --- rag.search: OCI Responses file_search 委譲 ------------------------------


def test_rag_search_happy_path_delegates_and_audits_allow(client, audit, monkeypatch):
    captured = {}

    def fake_search(owner, query, *, top_k=5):
        captured["owner"] = owner
        captured["query"] = query
        captured["top_k"] = top_k
        return {
            "hits": [
                {"file_id": "f1", "filename": "請求書.pdf", "score": 0.91, "text": "..."}
            ],
            "citations": [
                {"file_id": "f1", "filename": "請求書.pdf", "score": 0.91}
            ],
            "answer": "請求書の支払期日は月末です。",
            "store_present": True,
        }

    monkeypatch.setattr(rag, "search", fake_search)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "請求書", "top_k": 3},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tenant"] == TENANT
    assert body["hits"][0]["file_id"] == "f1"
    assert body["citations"][0]["filename"] == "請求書.pdf"
    assert body["answer"]
    # テナント境界: 本体は broker 検証済みテナントをストア所有者キーに使う(呼び出し元は渡さない)。
    assert captured["owner"] == TENANT
    assert captured["query"] == "請求書"
    assert captured["top_k"] == 3
    allows = [r for r in audit if r["decision"] == "ALLOW"]
    assert len(allows) == 1
    assert allows[0]["scope"] == RAG_SEARCH
    assert allows[0]["tenant"] == TENANT


def test_rag_search_empty_store_returns_empty_200(client, audit, monkeypatch):
    # テナントに取り込み済み文書(ストア)が無い場合は越境ではなくデータ未取込 → 空ヒットの 200。
    monkeypatch.setattr(
        rag,
        "search",
        lambda owner, query, *, top_k=5: {
            "hits": [],
            "citations": [],
            "answer": "",
            "store_present": False,
        },
    )
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 200, res.text
    assert res.json()["hits"] == []
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_rag_search_upstream_error_502(client, audit, monkeypatch):
    # 委譲先(OCI Responses file_search)の失敗は曖昧に 200 へ倒さず 502(fail-closed)。
    def boom(owner, query, *, top_k=5):
        raise rag.RagSearchError("genai timeout")

    monkeypatch.setattr(rag, "search", boom)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 502, res.text
    assert any(r["decision"] == "ALLOW" for r in audit)


def test_rag_search_upstream_error_hides_detail(client, audit, monkeypatch):
    # 502 のクライアント応答に上流例外文字列(vector_store_id 等を含みうる)を漏らさない。
    def boom(owner, query, *, top_k=5):
        raise rag.RagSearchError("vector_store_id=vs_kix_SECRET endpoint=https://internal")

    monkeypatch.setattr(rag, "search", boom)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 502, res.text
    assert "vs_kix_SECRET" not in res.text
    assert "internal" not in res.text


def test_rag_search_blank_query_422_no_delegation(client, audit, monkeypatch):
    # 空白のみの query は 422。rag.search に到達しない(課金 API を叩かない)。
    monkeypatch.setattr(rag, "search", lambda *a, **k: pytest.fail("must not delegate"))
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "   "},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 422, res.text


def test_rag_search_overlong_query_422_no_delegation(client, audit, monkeypatch):
    from service.routes.platform import MAX_RAG_QUERY_CHARS

    monkeypatch.setattr(rag, "search", lambda *a, **k: pytest.fail("must not delegate"))
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "あ" * (MAX_RAG_QUERY_CHARS + 1)},
        headers=_auth(_token([RAG_SEARCH])),
    )
    assert res.status_code == 422, res.text


def test_rag_search_scope_denied_403(client, audit):
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([DB_QUERY])),
    )
    assert res.status_code == 403, res.text


def test_rag_search_tenant_mismatch_403(client, audit, monkeypatch):
    # 別テナントのトークンで TENANT のストアを検索 → tenant_mismatch で 403(越境拒否 fail-closed)。
    # search に到達しないことも確認(認可で弾かれるため委譲が起きない)。
    called = {"n": 0}

    def fake_search(owner, query, *, top_k=5):
        called["n"] += 1
        return {"hits": [], "citations": [], "answer": "", "store_present": True}

    monkeypatch.setattr(rag, "search", fake_search)
    res = client.post(
        "/platform/rag/search",
        json={"tenant": TENANT, "query": "x"},
        headers=_auth(_token([RAG_SEARCH], tenant=TENANT_B)),
    )
    assert res.status_code == 403, res.text
    assert called["n"] == 0
    denies = [r for r in audit if r["decision"] == "DENY"]
    assert denies and denies[0]["reason"] == "tenant_mismatch"
