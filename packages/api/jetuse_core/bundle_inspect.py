"""バンドル静的検査(specs/19 §4.3 S3(b)/S4・ADR-0023 の二層細則)。

信頼ツールチェーン(API 側)で生成物を検査する **fail-closed ゲート**。合格したバンドルだけが
ポインタ切替(S5)で公開される。返り値は違反リスト(空 = 合格)。

- **層1(生成 src・例外なし)**: 保護ファイル(client.js 等)以外の生成ソースに、絶対 URL・
  プロトコル相対・スコープ外 `/api/`・生 `fetch(`/`XMLHttpRequest`/`WebSocket(`/`EventSource(`・
  動的 `import(` があれば不合格(生成コードは client.js の関数だけを呼ぶ構造 — S3(a))。
- **層2(dist バンドル・全ファイル)**: 絶対 URL は**クリーン scaffold ビルド由来の baseline**
  (vendored React/w3.org 定数)に一致するもののみ可。生成コード由来 URL は baseline に無く不合格。
- **S4(全ファイル)**: 秘密パターン(`ocid1.`/`-----BEGIN`/JWT 風 `eyJ`/ジョブトークン `jetuse-jt-`)
  があれば不合格。F1(入力に秘密を渡さない)と対の出口検査。
"""
from __future__ import annotations

import re

# 層2 baseline: クリーン scaffold(Vite6 + React18.3.1・生成コードなし)ビルドに定数として
# 埋まる絶対 URL のみ(offline-build.log で実測。fetch/接続先ではない)。scaffold の React 版が
# 変わったら再導出する(バージョン tied — S3(b) 細則: 生成コードを一切含まないビルド由来のみ許容)。
BUNDLE_URL_BASELINE: frozenset[str] = frozenset({
    "https://reactjs.org/docs/error-decoder.html",
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/XML/1998/namespace",
})

# 層1 で生成ソースが直接使ってはならない通信 API(client.js だけが発行してよい — S3(a))。
_RAW_NET = ("fetch(", "XMLHttpRequest", "WebSocket(", "EventSource(", "import(")

_ABS_URL = re.compile(rb"https?://[^\s\"'`<>)]+")
# プロトコル相対: 引用符の直後に // + ホスト文字(`http://` や `// コメント` を誤検出しない)
_PROTO_REL = re.compile(rb"""["']//[a-zA-Z0-9.\-]""")
# スコープ外 API パス: **文字列リテラルが `/api/` で始まる**(ハードコードした絶対 API パス)。
# 生成コードは client.js の関数を呼ぶ契約ゆえ絶対 API パスを書かない(S3(a))。相対 import
# `'./api/client.js'` は引用符直後が `.` なので誤検出しない(codex 相当の false-positive 回避)。
_SCOPED_API = re.compile(rb"""["'`]/api/""")

# S4 秘密パターン(既知形式のみ — 誤検出を避けつつ出口で漏らさない)。エントロピー検査は
# dist(縮小 JS)がハッシュ/base64 チャンクだらけで偽陽性の山になるため採らない(既知形式で網羅)。
_SECRETS = (
    (rb"ocid1\.[a-z0-9]", "ocid"),
    (rb"-----BEGIN", "pem"),
    (rb"eyJ[A-Za-z0-9_-]{10,}", "jwt"),          # base64url JSON ヘッダ = JWT/Bearer 風
    (rb"jetuse-jt-", "job_token"),               # 生成側ジョブトークン接頭辞(ADR §5.5)
    (rb"[Bb]earer\s+[A-Za-z0-9._~+/-]{16,}", "bearer"),   # Authorization: Bearer <token>
    (rb"sk-[A-Za-z0-9]{20,}", "api_key"),        # OpenAI 系 API キー
    (rb"AKIA[0-9A-Z]{16}", "aws_key"),           # AWS アクセスキー ID
)


def _scan_secrets(name: str, content: bytes) -> list[dict]:
    out = []
    for pat, kind in _SECRETS:
        if re.search(pat, content):
            out.append({"file": name, "kind": f"secret:{kind}", "detail": kind})
    return out


def inspect_src(src_files: dict[str, bytes],
                protected: frozenset[str] = frozenset()) -> list[dict]:
    """層1: 生成ソース(保護ファイルを除く)を例外なしで検査。違反リストを返す。"""
    violations: list[dict] = []
    for name, content in src_files.items():
        if name in protected:
            continue
        if _ABS_URL.search(content):
            hit = _ABS_URL.search(content).group().decode(errors="replace")
            violations.append({"file": name, "kind": "absolute_url", "detail": hit[:120]})
        if _PROTO_REL.search(content):
            violations.append({"file": name, "kind": "protocol_relative", "detail": ""})
        if _SCOPED_API.search(content):
            violations.append({"file": name, "kind": "scoped_api_path",
                               "detail": "literal /api/ path (client.js 経由で構築 — S3a)"})
        for tok in _RAW_NET:
            if tok.encode() in content:
                violations.append({"file": name, "kind": "raw_network", "detail": tok})
        violations.extend(_scan_secrets(name, content))
    return violations


def inspect_dist(dist_files: dict[str, bytes]) -> list[dict]:
    """層2 + S4: dist 全ファイルの絶対 URL を baseline と照合し、秘密パターンを検査。

    照合は scheme+host+path(クエリ/フラグメント除去)で行う。React の縮小版は
    `error-decoder.html?invariant=NNN&args[]=...` のように **可変クエリ**を定数として埋め込む
    (invariant 番号がエラーごとに違う)ため、素の URL exact 一致では偽陽性になる。ホスト+パスは
    baseline に固定されるので分離先の指定にはならない(クエリは静的ドキュメント頁のアンカーのみ)。
    """
    violations: list[dict] = []
    for name, content in dist_files.items():
        for m in _ABS_URL.finditer(content):
            url = m.group().decode(errors="replace")
            base = url.split("?", 1)[0].split("#", 1)[0]
            if base not in BUNDLE_URL_BASELINE:
                violations.append({"file": name, "kind": "unlisted_absolute_url",
                                   "detail": url[:200]})
        violations.extend(_scan_secrets(name, content))
    return violations


def inspect(src_files: dict[str, bytes], dist_files: dict[str, bytes],
            protected: frozenset[str] = frozenset()) -> list[dict]:
    """完全検査(層1 + 層2 + S4)。空リスト = 合格 → ポインタ切替(S5)可。"""
    return inspect_src(src_files, protected) + inspect_dist(dist_files)
