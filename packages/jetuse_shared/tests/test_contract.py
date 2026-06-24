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
