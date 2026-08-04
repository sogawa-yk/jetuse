"""バンドル静的検査(specs/19 §4.3 S3(b)/S4・ADR-0023)の単体テスト。

純関数 — DB もネットワークも不要。層1(生成 src・例外なし)/ 層2(dist・baseline)/ S4(秘密)。
"""

from jetuse_core import bundle_inspect as bi


def _kinds(violations):
    return {v["kind"] for v in violations}


# --- 層1: 生成ソース(例外なし) ---


def test_src_clean_passes():
    src = {"App.jsx": b"import {chat} from './api/client.js'; export default function App(){}"}
    assert bi.inspect_src(src) == []


def test_src_absolute_url_flagged():
    src = {"Home.jsx": b"const u = 'https://evil.example.com/x';"}
    assert "absolute_url" in _kinds(bi.inspect_src(src))


def test_src_protocol_relative_flagged():
    src = {"Home.jsx": b"const u = '//evil.example.com/x';"}
    assert "protocol_relative" in _kinds(bi.inspect_src(src))


def test_src_literal_api_path_flagged():
    src = {"Home.jsx": b"fetchThing('/api/demos/1/chat')"}
    assert "scoped_api_path" in _kinds(bi.inspect_src(src))


def test_src_raw_network_each_token_flagged():
    for tok in (b"fetch(", b"XMLHttpRequest", b"WebSocket(", b"EventSource(", b"import("):
        v = bi.inspect_src({"x.js": b"x = " + tok + b"'y')"})
        assert "raw_network" in _kinds(v), tok


def test_src_protected_file_skipped():
    # client.js は絶対 URL や fetch を持つが保護対象なので層1 では検査しない
    src = {"api/client.js": b"await fetch('https://x/y'); //ok", "App.jsx": b"export default 1"}
    assert bi.inspect_src(src, protected=frozenset({"api/client.js"})) == []


def test_src_secret_patterns_flagged():
    cases = {
        "a.js": b"const x='ocid1.tenancy.oc1..aaa'",
        "b.js": b"-----BEGIN PRIVATE KEY-----",
        "c.js": b"const t='eyJhbGciOiJIUzI1Ni4iodummytoken'",
        "d.js": b"const j='jetuse-jt-abc'",
    }
    for name, content in cases.items():
        v = bi.inspect_src({name: content})
        assert any(k.startswith("secret:") for k in _kinds(v)), name


# --- 層2: dist(baseline 照合)+ S4 ---


def test_dist_baseline_urls_allowed():
    # クリーン scaffold ビルドに埋まる vendored 定数 URL は許容(空白区切りで各 URL を独立に照合)
    content = b" ".join(u.encode() for u in bi.BUNDLE_URL_BASELINE)
    assert bi.inspect_dist({"assets/index.js": content}) == []


def test_src_relative_client_import_not_flagged():
    # 生成ファイルは全て `./api/client.js` を import する — /api/ を含むが誤検出してはならない(回帰)
    src = {"screens/Home.jsx": b"import { chat, ragSearch } from '../api/client.js';"}
    assert bi.inspect_src(src) == []


def test_dist_react_error_decoder_with_query_allowed():
    # React 縮小版は可変クエリ付き error-decoder URL を埋め込む(実 build で観測 — 回帰)
    url = b"https://reactjs.org/docs/error-decoder.html?invariant=130&args[]=undefined"
    assert bi.inspect_dist({"assets/index.js": url}) == []


def test_dist_unlisted_url_flagged():
    v = bi.inspect_dist({"assets/index.js": b"fetch('https://evil.example.com/x')"})
    assert "unlisted_absolute_url" in _kinds(v)


def test_dist_secret_flagged():
    v = bi.inspect_dist({"assets/index.js": b"var k='ocid1.key.oc1..zzz'"})
    assert any(k.startswith("secret:") for k in _kinds(v))


def test_inspect_combines_layers():
    src = {"App.jsx": b"await fetch('/x')"}
    dist = {"assets/index.js": b"'https://evil/x'"}
    v = bi.inspect(src, dist)
    assert "raw_network" in _kinds(v) and "unlisted_absolute_url" in _kinds(v)


def test_inspect_clean_bundle_passes():
    src = {"App.jsx": b"import {chat} from './api/client.js'"}
    dist = {"index.html": b"<!doctype html>", "assets/index.js": b"console.log(1)"}
    assert bi.inspect(src, dist, protected=frozenset({"api/client.js"})) == []
