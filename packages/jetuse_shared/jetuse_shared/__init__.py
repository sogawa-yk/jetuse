"""jetuse_shared: JetUse の API と各SDKコンテナで二重管理していたセキュリティ要件ロジックを一本化。

範囲は意図的に最小(セキュリティ要件のみ):
- SSRFガード + web_fetch / extract_url
- web_search(DuckDuckGo HTML)
- get_current_time
- sanitize_sql(SELECT/WITHガード)

SemanticStore / pydantic Settings / os.environ 読取はランタイムごとに正しく異なるため
**ここには含めない**(各ランタイムが薄い adapter で注入する)。
"""

from .sqlguard import _BANNED, SqlRejectedError, sanitize_sql
from .webtools import (
    MAX_BYTES,
    MAX_PAGE_CHARS,
    MAX_TEXT_CHARS,
    SEARCH_RESULTS,
    SEARCH_TIMEOUT,
    TIMEOUT_SECONDS,
    FetchConfig,
    SsrfBlockedError,
    _DdgParser,
    assert_public_host,
    extract_url,
    get_current_time,
    get_current_time_json,
    web_fetch,
    web_fetch_json,
    web_search,
    web_search_json,
)

__all__ = [
    # webtools
    "SsrfBlockedError",
    "FetchConfig",
    "assert_public_host",
    "extract_url",
    "web_fetch",
    "web_fetch_json",
    "web_search",
    "web_search_json",
    "get_current_time",
    "get_current_time_json",
    "_DdgParser",
    "MAX_BYTES",
    "MAX_PAGE_CHARS",
    "MAX_TEXT_CHARS",
    "TIMEOUT_SECONDS",
    "SEARCH_RESULTS",
    "SEARCH_TIMEOUT",
    # sqlguard
    "sanitize_sql",
    "SqlRejectedError",
    "_BANNED",
]
