"""SSRF対策込みのWebコンテンツ抽出・検索・現在時刻(API/コンテナ共有 — jetuse_shared)。

元実装: `jetuse_core/webtools.py`(extract_url/SSRFガード) と `jetuse_core/tools.py`(DuckDuckGo検索)、
および `agent-containers/agent_common.py` の移植コピーを一本化したもの。

設計:
- 設定(タイムアウト・最大バイト・最大文字数等)は引数 / 軽量 dataclass で受け取り、
  pydantic Settings にも os.environ にも依存しない。各ランタイムが値を注入する。
- 例外は jetuse_shared 固有の `SsrfBlockedError` を送出。呼び出し側は必要なら自分の例外型へ翻訳する。

セキュリティ要件(移植時に弱体化禁止):
- private / loopback / link-local(メタデータ 169.254.x)/ reserved / multicast / unspecified
  をすべて拒否する。
- リダイレクトは各ホップでホストを再検証する。
"""

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

# --- 既定値(API側の従来値を踏襲) ---
MAX_BYTES = 3_000_000
# ページ抽出の生テキスト上限(extract_url 戻り値 text に適用)。API従来値=20000。
MAX_PAGE_CHARS = 20_000
# web_fetch ツール出力の text 上限。API/コンテナとも従来は実効 8000(API は handler 側で [:8000]、
# コンテナは _extract_url 内で [:8000])。両者の観測挙動を保つため 8000 を正準値とする。
MAX_TEXT_CHARS = 8_000
TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5

SEARCH_RESULTS = 5
SEARCH_TIMEOUT = 15.0

SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer"}


class SsrfBlockedError(ValueError):
    """SSRFガードで拒否したURL/ホスト。"""


@dataclass(frozen=True)
class FetchConfig:
    """web_fetch / extract_url の挙動を各ランタイムが調整するための軽量設定。"""

    max_bytes: int = MAX_BYTES
    max_page_chars: int = MAX_PAGE_CHARS
    timeout_seconds: float = TIMEOUT_SECONDS
    max_redirects: int = MAX_REDIRECTS
    user_agent: str = "jetuse/0.1 (content extractor)"


def assert_public_host(host: str) -> None:
    """プライベート/リンクローカル(=メタデータ169.254.x)/ループバック等を拒否。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise SsrfBlockedError(f"DNS resolution failed: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise SsrfBlockedError(f"blocked address: {host} -> {ip}")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def extract_url(url: str, config: FetchConfig | None = None) -> dict:
    """URLを取得して本文テキストを抽出する。リダイレクト先も各ホップで検証。

    戻り値: {"url", "title", "text"}。text は config.max_page_chars で打ち切る。
    """
    cfg = config or FetchConfig()
    with httpx.Client(
        follow_redirects=False,
        timeout=cfg.timeout_seconds,
        headers={"User-Agent": cfg.user_agent},
    ) as client:
        for _ in range(cfg.max_redirects):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise SsrfBlockedError(f"unsupported url: {url}")
            assert_public_host(parsed.hostname)
            with client.stream("GET", url) as res:
                if res.is_redirect:
                    url = urljoin(url, res.headers["location"])
                    continue
                res.raise_for_status()
                body = b""
                for chunk in res.iter_bytes():
                    body += chunk
                    if len(body) >= cfg.max_bytes:
                        break
                charset = res.charset_encoding or "utf-8"
                break
        else:
            raise SsrfBlockedError("too many redirects")

    parser = _TextExtractor()
    parser.feed(body.decode(charset, errors="replace"))
    text = re.sub(r"\n{3,}", "\n\n", " ".join(parser.parts).replace(" \n ", "\n")).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    return {"url": url, "title": parser.title[:300], "text": text[: cfg.max_page_chars]}


def web_fetch(url: str, *, max_text_chars: int = MAX_TEXT_CHARS,
              config: FetchConfig | None = None) -> dict:
    """web_fetch ツールの本体: extract_url した上で text を max_text_chars に打ち切る。

    戻り値は {"title", "text", "url"}(JSON 化はしない=呼び出し側の責務)。
    """
    page = extract_url(url, config)
    return {
        "title": page["title"],
        "text": page["text"][:max_text_chars],
        "url": page["url"],
    }


# ---- web_search(DuckDuckGo HTML版) ----
class _DdgParser(HTMLParser):
    """DuckDuckGo HTML版(html.duckduckgo.com)の結果抽出。"""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._in_title = False
        self._in_snippet = False
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            href = a.get("href", "")
            # /l/?uddg=<encoded url> 形式のリダイレクトを展開
            if "uddg=" in href:
                q = parse_qs(urlparse(href).query)
                href = unquote(q.get("uddg", [href])[0])
            self._cur = {"title": "", "url": href, "snippet": ""}
            self._in_title = True
        elif tag == "a" and "result__snippet" in cls and self._cur is not None:
            self._in_snippet = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title and self._cur is not None:
            self._in_title = False
        elif tag == "a" and self._in_snippet and self._cur is not None:
            self._in_snippet = False
            self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._cur is None:
            return
        if self._in_title:
            self._cur["title"] += data
        elif self._in_snippet:
            self._cur["snippet"] += data


def web_search(query: str, *, max_results: int = SEARCH_RESULTS,
               timeout: float = SEARCH_TIMEOUT) -> dict:
    """DuckDuckGo HTML を検索して上位結果を返す。

    戻り値: {"results": [...]} あるいは {"results": [], "note": "検索結果なし"}。
    """
    res = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "kl": "jp-jp"},
        headers={"User-Agent": "Mozilla/5.0 (jetuse agent)"},
        timeout=timeout,
        follow_redirects=True,
    )
    res.raise_for_status()
    parser = _DdgParser()
    parser.feed(res.text)
    results = parser.results[:max_results]
    if not results:
        return {"results": [], "note": "検索結果なし"}
    for r in results:
        r["title"] = re.sub(r"\s+", " ", r["title"]).strip()[:200]
        r["snippet"] = re.sub(r"\s+", " ", r["snippet"]).strip()[:300]
    return {"results": results}


def get_current_time() -> dict:
    """現在の日本時間(日付・時刻・曜日)を返す。"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "japanese": now.strftime("%Y年%m月%d日 %H時%M分"),
        "weekday": ["月", "火", "水", "木", "金", "土", "日"][now.weekday()],
        "timezone": "Asia/Tokyo (JST)",
    }


# 便宜: JSON文字列を直接返すツール出力ヘルパ(呼び出し側で使ってもよい)
def web_search_json(query: str, **kwargs) -> str:
    return json.dumps(web_search(query, **kwargs), ensure_ascii=False)


def web_fetch_json(url: str, **kwargs) -> str:
    return json.dumps(web_fetch(url, **kwargs), ensure_ascii=False)


def get_current_time_json() -> str:
    return json.dumps(get_current_time(), ensure_ascii=False)
