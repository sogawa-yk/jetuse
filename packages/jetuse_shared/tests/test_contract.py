"""jetuse_shared 共通契約テスト。

API側 / コンテナ側の両方がこのロジックに委譲するため、ここがセキュリティ要件の正準テスト。
- SSRFガード: private/loopback/link-local/metadata を必ず拒否
- web_fetch: スキーム不正 / メタデータホストを拒否、text 上限の打ち切り
- web_search: DDG HTML パース、結果なし時の note
- get_current_time: 形状
- sanitize_sql: 危険文の拒否 / SELECT・WITH の通過
"""

import json

import pytest

import jetuse_shared as gs
from jetuse_shared import webtools
from jetuse_shared.sqlguard import SqlRejectedError, sanitize_sql
from jetuse_shared.webtools import (
    SsrfBlockedError,
    _DdgParser,
    assert_public_host,
    extract_url,
    get_current_time,
    web_fetch,
    web_search,
)


# ---------------- SSRFガード ----------------
@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "127.0.0.1", "10.0.0.1", "192.168.1.1", "localhost", "0.0.0.0"],
)
def test_assert_public_host_blocks_internal(host):
    with pytest.raises(SsrfBlockedError):
        assert_public_host(host)


def test_assert_public_host_blocks_ipv6_loopback():
    with pytest.raises(SsrfBlockedError):
        assert_public_host("::1")


def test_assert_public_host_unresolvable_raises_ssrf():
    with pytest.raises(SsrfBlockedError):
        assert_public_host("this-host-does-not-exist.invalid")


# ---------------- web_fetch ----------------
def test_web_fetch_rejects_non_http_scheme():
    with pytest.raises(SsrfBlockedError):
        web_fetch("ftp://example.com/x")


def test_web_fetch_rejects_metadata_host():
    with pytest.raises(SsrfBlockedError):
        web_fetch("http://169.254.169.254/opc/v2/")


def test_extract_url_rejects_private_host():
    with pytest.raises(SsrfBlockedError):
        extract_url("http://127.0.0.1:8080/")


def test_web_fetch_truncates_to_max_text_chars(monkeypatch):
    long_text = "x" * 50_000
    monkeypatch.setattr(
        webtools,
        "extract_url",
        lambda url, config=None: {"url": url, "title": "t", "text": long_text},
    )
    out = web_fetch("https://example.com/", max_text_chars=8000)
    assert len(out["text"]) == 8000
    assert out["title"] == "t"
    assert set(out) == {"title", "text", "url"}


def test_max_text_chars_canonical_value():
    # 統一の正準値(API/コンテナの実効値=8000)
    assert webtools.MAX_TEXT_CHARS == 8000
    assert webtools.MAX_PAGE_CHARS == 20000


# ---------------- web_search ----------------
_DDG_HTML = """
<div class="result">
  <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example   Title</a>
  <a class="result__snippet">A   snippet   here</a>
</div>
"""


def test_ddg_parser_extracts_and_decodes_redirect():
    p = _DdgParser()
    p.feed(_DDG_HTML)
    assert len(p.results) == 1
    assert p.results[0]["url"] == "https://example.com/page"
    assert "Example" in p.results[0]["title"]


def test_web_search_parses_results(monkeypatch):
    class _Resp:
        text = _DDG_HTML

        def raise_for_status(self):
            return None

    monkeypatch.setattr(webtools.httpx, "post", lambda *a, **k: _Resp())
    out = web_search("anything")
    assert out["results"][0]["url"] == "https://example.com/page"
    # 連続空白は1スペースに正規化される
    assert out["results"][0]["title"] == "Example Title"
    assert out["results"][0]["snippet"] == "A snippet here"


def test_web_search_empty_returns_note(monkeypatch):
    class _Resp:
        text = "<html></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(webtools.httpx, "post", lambda *a, **k: _Resp())
    out = web_search("anything")
    assert out["results"] == []
    assert out["note"] == "検索結果なし"


# ---------------- get_current_time ----------------
def test_get_current_time_shape():
    out = get_current_time()
    assert set(out) == {"iso", "japanese", "weekday", "timezone"}
    assert out["timezone"] == "Asia/Tokyo (JST)"
    assert out["weekday"] in ["月", "火", "水", "木", "金", "土", "日"]


def test_json_helpers_roundtrip():
    assert json.loads(gs.get_current_time_json())["timezone"] == "Asia/Tokyo (JST)"


# ---------------- sanitize_sql ----------------
def test_sanitize_accepts_select_and_with():
    assert sanitize_sql("SELECT 1 FROM dual;").startswith("SELECT")
    assert sanitize_sql(
        "  with t as (select 1 c from dual) select * from t"
    ).lower().startswith("with")
    assert sanitize_sql("/* note */ -- c\nSELECT 1 FROM dual").startswith("SELECT")
    assert sanitize_sql("SELECT * FROM t WHERE 1=1; --").endswith("1=1")


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM sh.sales",
        "UPDATE t SET a=1",
        "SELECT 1 FROM dual; DROP TABLE t",
        "BEGIN NULL; END;",
        "/* SELECT */ DROP TABLE t",
        "WITH t AS (SELECT 1 FROM dual) DELETE FROM x",
        "INSERT INTO t VALUES (1)",
        "GRANT SELECT ON t TO u",
    ],
)
def test_sanitize_rejects_dangerous(bad):
    with pytest.raises(SqlRejectedError):
        sanitize_sql(bad)


def test_sqlrejected_is_valueerror():
    # コンテナ側の except Exception / 旧 ValueError catch との後方互換
    assert issubclass(SqlRejectedError, ValueError)
    assert issubclass(SsrfBlockedError, ValueError)


# ---------------- strip_code_fences / charting(SQL-03 / SBA-B 共有) ----------------
from jetuse_shared.charting import CHART_TYPES, propose_chart  # noqa: E402
from jetuse_shared.sqlguard import strip_code_fences  # noqa: E402


def test_strip_code_fences_removes_sql_fence():
    assert strip_code_fences("```sql\nSELECT 1 FROM dual\n```") == "SELECT 1 FROM dual"
    assert strip_code_fences("```\nSELECT 2\n```") == "SELECT 2"
    assert strip_code_fences("SELECT 3") == "SELECT 3"
    assert strip_code_fences("") == ""


def test_propose_chart_validates_columns():
    spec = propose_chart(
        lambda p: '{"type":"bar","x":"w","y":["q"],"title":"t","reason":"r"}',
        "倉庫別",
        ["w", "q"],
        [["A", "1"], ["B", "2"]],
    )
    assert spec["type"] == "bar"
    assert spec["x"] == "w" and spec["y"] == ["q"]
    assert spec["type"] in CHART_TYPES


def test_propose_chart_none_when_x_not_in_columns():
    spec = propose_chart(
        lambda p: '{"type":"bar","x":"missing","y":["q"]}',
        "q",
        ["w", "q"],
        [["A", "1"]],
    )
    assert spec["type"] == "none"


def test_propose_chart_none_when_no_rows():
    called = {"n": 0}

    def gen(p):
        called["n"] += 1
        return "{}"

    spec = propose_chart(gen, "q", ["w"], [])
    assert spec["type"] == "none"
    assert called["n"] == 0  # データが無ければ LLM を呼ばない


def test_propose_chart_none_on_unparseable():
    spec = propose_chart(lambda p: "no json here", "q", ["w", "q"], [["A", "1"]])
    assert spec["type"] == "none"


# ---------------- referenced_tables / assert_tables_allowed(NL2SQL スキーマ境界) ----------------
from jetuse_shared.sqlguard import (  # noqa: E402
    assert_tables_allowed,
    cte_names,
    referenced_tables,
)


def test_referenced_tables_simple_and_join():
    assert referenced_tables("SELECT * FROM inventory") == ["inventory"]
    refs = referenced_tables(
        "SELECT * FROM inventory i JOIN orders o ON i.product_code=o.product_code"
    )
    assert [r.lower() for r in refs] == ["inventory", "orders"]


def test_referenced_tables_comma_list_and_alias():
    refs = referenced_tables("SELECT * FROM inventory a, orders b WHERE a.x=b.x")
    assert [r.lower() for r in refs] == ["inventory", "orders"]


def test_referenced_tables_subquery_inner_caught():
    refs = referenced_tables(
        "SELECT * FROM (SELECT product_code FROM orders) t JOIN inventory i ON 1=1"
    )
    assert "orders" in [r.lower() for r in refs]
    assert "inventory" in [r.lower() for r in refs]


def test_referenced_tables_schema_qualified():
    assert referenced_tables("SELECT * FROM SYS.DBA_USERS") == ["SYS.DBA_USERS"]


def test_cte_names():
    sql = "WITH t AS (SELECT 1 FROM inventory), u AS (SELECT 2 FROM orders) SELECT * FROM t, u"
    assert cte_names(sql) == {"T", "U"}


def test_assert_tables_allowed_passes_known():
    assert_tables_allowed(
        "SELECT warehouse, SUM(quantity) FROM inventory GROUP BY warehouse",
        {"INVENTORY", "ORDERS"},
    )


def test_assert_tables_allowed_allows_cte_and_dual():
    assert_tables_allowed(
        "WITH s AS (SELECT amount FROM orders) SELECT SUM(amount) FROM s",
        {"INVENTORY", "ORDERS"},
    )
    assert_tables_allowed("SELECT SYSDATE FROM dual", {"INVENTORY"})


def test_assert_tables_allowed_rejects_unknown_table():
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed("SELECT * FROM secret_table", {"INVENTORY", "ORDERS"})


def test_assert_tables_allowed_no_dual_rejects_dual():
    """allow_dual=False: DUAL の暗黙許可を切る(sample-app 専用 execute 経路)。"""
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed("SELECT USER FROM dual", {"INVENTORY"}, allow_dual=False)
    # 既定(allow_dual=True)では従来どおり DUAL を許可。
    assert_tables_allowed("SELECT USER FROM dual", {"INVENTORY"})


def test_assert_tables_allowed_require_table_rejects_non_dataset():
    """require_table=True: 業務テーブルを最低1つ参照しない SQL を拒否する。"""
    # FROM 無しのスカラ/関数照会(dataset テーブル不参照)は拒否。
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed(
            "SELECT SYS_CONTEXT('USERENV','SESSION_USER')",
            {"INVENTORY", "ORDERS"},
            allow_dual=False,
            require_table=True,
        )
    # CTE のみ(業務テーブルへ到達しない構成)も拒否。
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed(
            "WITH s AS (SELECT 1 c FROM dual) SELECT c FROM s",
            {"INVENTORY", "ORDERS"},
            require_table=True,
        )
    # 業務テーブルを参照していれば通る。
    assert_tables_allowed(
        "SELECT product_code FROM inventory FETCH FIRST 1 ROWS ONLY",
        {"INVENTORY", "ORDERS"},
        allow_dual=False,
        require_table=True,
    )


def test_assert_tables_allowed_require_table_blocks_cte_shadowing():
    """require_table=True: 許可テーブルと同名の CTE で実テーブル参照を偽装する回避を拒否する。"""
    # CTE INVENTORY が実テーブルを shadow → 実テーブル不参照とみなし require_table で拒否。
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed(
            "WITH INVENTORY AS (SELECT SYS_CONTEXT('USERENV','SESSION_USER') AS U) "
            "SELECT * FROM INVENTORY",
            {"INVENTORY", "ORDERS"},
            allow_dual=False,
            require_table=True,
        )
    # 同名 CTE があっても、別の実業務テーブル(ORDERS)を実参照していれば通る。
    assert_tables_allowed(
        "WITH INVENTORY AS (SELECT 1 c FROM dual) "
        "SELECT o.amount FROM ORDERS o, INVENTORY i",
        {"INVENTORY", "ORDERS"},
        require_table=True,
    )


def test_assert_tables_allowed_rejects_schema_qualified():
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed("SELECT * FROM SYS.DBA_USERS", {"INVENTORY"})
    # 自テーブルでもスキーマ修飾は拒否(別スキーマ同名テーブルの取り違え防止)。
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed("SELECT * FROM OTHER.INVENTORY", {"INVENTORY"})


def test_assert_tables_allowed_string_literal_cannot_bypass():
    """B2 回帰: 文字列リテラル内の `, x AS (` を CTE と誤認して許可しない。"""
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed(
            "SELECT * FROM secret_table WHERE note = ', secret_table AS ('",
            {"INVENTORY", "ORDERS"},
        )


def test_referenced_tables_ignores_from_inside_literal():
    """文字列リテラル内の FROM はテーブル参照として抽出しない。"""
    refs = referenced_tables("SELECT * FROM inventory WHERE note = 'x FROM evil'")
    assert [r.lower() for r in refs] == ["inventory"]


def test_cte_names_ignores_literal_as_paren():
    assert cte_names("SELECT * FROM t WHERE x = ', fake AS ('") == set()


def test_assert_tables_allowed_qquote_cannot_bypass():
    """B2 回帰: Oracle q-quote 内の `, x AS (` を CTE と誤認して許可しない。"""
    for probe in (
        "SELECT * FROM secret_table WHERE n = q'[, secret_table AS (]'",
        "SELECT * FROM secret_table WHERE n = q'{, secret_table AS (}'",
        "SELECT * FROM secret_table WHERE n = q'!, secret_table AS (!'",
    ):
        with pytest.raises(SqlRejectedError):
            assert_tables_allowed(probe, {"INVENTORY", "ORDERS"})


def test_referenced_tables_ignores_from_inside_qquote():
    refs = referenced_tables("SELECT * FROM inventory WHERE n = q'[x FROM evil]'")
    assert [r.lower() for r in refs] == ["inventory"]


def test_assert_tables_allowed_rejects_dblink():
    """M1 回帰: DB link 記法(name@dblink)は許可テーブル名でも拒否。"""
    for bad in (
        "SELECT * FROM INVENTORY@REMOTE",
        'SELECT * FROM "INVENTORY"@REMOTE',
        "SELECT * FROM inventory@db.example.com",
    ):
        with pytest.raises(SqlRejectedError):
            assert_tables_allowed(bad, {"INVENTORY", "ORDERS"})


def test_referenced_tables_captures_dblink():
    refs = referenced_tables("SELECT * FROM INVENTORY@REMOTE")
    assert refs == ["INVENTORY@REMOTE"]


def test_assert_tables_allowed_quoted_identifier_case():
    """M2 回帰: 引用識別子はケースを区別する。"""
    # unquoted は大文字フォールドで一致。
    assert_tables_allowed("SELECT * FROM inventory", {"INVENTORY"})
    assert_tables_allowed("SELECT * FROM Inventory", {"INVENTORY"})
    # 大文字引用は実質同一 → 許可。
    assert_tables_allowed('SELECT * FROM "INVENTORY"', {"INVENTORY"})
    # 小文字/混在引用は別オブジェクト → 拒否。
    for bad in ('SELECT * FROM "inventory"', 'SELECT * FROM "Inventory"'):
        with pytest.raises(SqlRejectedError):
            assert_tables_allowed(bad, {"INVENTORY"})


def test_assert_tables_allowed_nested_cte_cannot_bypass():
    """B1 回帰: ネスト WITH で実テーブル名と同名の CTE を定義しても外側参照を許可しない。"""
    attack = (
        "SELECT * FROM SALES WHERE EXISTS ("
        "SELECT 1 FROM (WITH SALES AS (SELECT 1 FROM DUAL) SELECT * FROM SALES))"
    )
    with pytest.raises(SqlRejectedError):
        assert_tables_allowed(attack, {"INVENTORY", "ORDERS"})


def test_cte_names_only_top_level():
    # トップレベル WITH は収集。
    top = "WITH a AS (SELECT 1 FROM dual), b AS (SELECT 2 FROM dual) SELECT * FROM a, b"
    assert cte_names(top) == {"A", "B"}
    # 先頭が SELECT(ネスト WITH のみ)は収集しない。
    nested = "SELECT * FROM SALES WHERE x IN (WITH s AS (SELECT 1 FROM dual) SELECT * FROM s)"
    assert cte_names(nested) == set()
    # 列リスト付き CTE も名前を取れる。
    assert cte_names("WITH t (x, y) AS (SELECT 1, 2 FROM dual) SELECT * FROM t") == {"T"}


def test_top_level_cte_still_allowed():
    assert_tables_allowed(
        "WITH s AS (SELECT amount FROM orders) SELECT SUM(amount) FROM s",
        {"INVENTORY", "ORDERS"},
    )
