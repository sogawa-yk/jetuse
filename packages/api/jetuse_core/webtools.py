"""Webコンテンツ抽出(UC-02)。SSRF対策込み(specs/08)。

ロジック本体は共有パッケージ `jetuse_shared` に一本化済み(P1b)。本モジュールは
従来の公開API(`SsrfBlockedError`/`_assert_public_host`/`extract_url`/`MAX_*` 定数)を
再エクスポートする薄い adapter。

定数の対応:
- 旧 `jetuse_core.webtools.MAX_TEXT_CHARS`(=20000)は extract_url のページ抽出上限を指していた。
  これは jetuse_shared の `MAX_PAGE_CHARS` に相当するため、後方互換でその値を再エクスポートする。
"""

from jetuse_shared.webtools import (
    MAX_BYTES,
    MAX_REDIRECTS,
    TIMEOUT_SECONDS,
    SsrfBlockedError,
)
from jetuse_shared.webtools import MAX_PAGE_CHARS as MAX_TEXT_CHARS
from jetuse_shared.webtools import assert_public_host as _assert_public_host
from jetuse_shared.webtools import extract_url as extract_url

__all__ = [
    "SsrfBlockedError",
    "_assert_public_host",
    "extract_url",
    "MAX_BYTES",
    "MAX_TEXT_CHARS",
    "TIMEOUT_SECONDS",
    "MAX_REDIRECTS",
]
