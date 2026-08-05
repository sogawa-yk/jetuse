"""`ops/er.py` の Markdown→HTML 変換が、どんな入力でも**必ず終わる**ことを固定する。

**なぜテストが要るか**: `_md()` の段落分岐は継続条件だけでループを回しており、
「`|` で始まるのに表として解釈されない行」（次行が `|---|` 区切りでない＝表の行を
折り返して書いた場合など）で継続条件が即 false になり、`buf` が空のまま `i` が
進まず、外側の `while i < len(lines)` が**永久に回り続けた**。
実害: 2026-08-05、`docs/enhance/ER-0012-*.md` の折り返した表により
`ops/er.py report` が10分以上応答せず、Obsidian 側の ER 一覧が更新できなくなった。

変換の見た目ではなく「**停止すること**」を主眼に固定する。
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import signal

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]


def _load_er():
    spec = importlib.util.spec_from_file_location("er_mod", REPO / "ops" / "er.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


er = _load_er()


@contextlib.contextmanager
def time_limit(seconds: int = 5):
    """**回帰時に落ちる。固まらない。** 素の assert だと再発で CI が無応答になる。"""
    def _boom(signum, frame):
        raise AssertionError(f"{seconds} 秒で終わらなかった（無限ループの再発）")

    old = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# 実害そのもの（折り返した表）に加え、同じ「継続条件が即 false」を踏む形を並べる。
HANG_CANDIDATES = [
    pytest.param("| 主張 | 根拠 |\n| 続きを折り返した行\nさらに続き |\n", id="wrapped-table-row"),
    pytest.param("| 区切りの無い表 | 二列目 |\n本文\n", id="table-without-separator"),
    pytest.param("|\n", id="bare-pipe"),
    pytest.param("| 最後の行が pipe |", id="trailing-pipe-eof"),
]


@pytest.mark.parametrize("text", HANG_CANDIDATES)
def test_md_terminates(text):
    """停止すること。ハングしていたケースは pytest-timeout ではなく到達で判定する。"""
    with time_limit():
        assert isinstance(er._md(text), str)


@pytest.mark.parametrize("text", HANG_CANDIDATES)
def test_md_keeps_the_content(text):
    """停止するだけでなく、行の中身を落とさない（`i` を素通りさせて解決しない）。"""
    with time_limit():
        html = er._md(text)
    for token in ("主張", "続きを折り返した行", "区切りの無い表", "最後の行が pipe"):
        if token in text:
            assert token in html, f"{token!r} が出力から消えている"


def test_all_real_er_files_convert():
    """実データ全件が変換できる。ER を書くときに同じ形を書いても気づける。"""
    files = sorted((REPO / "docs" / "enhance").glob("ER-*.md"))
    assert files, "ER ファイルが見つからない"
    with time_limit(20):
        for f in files:
            assert isinstance(er._md(f.read_text(encoding="utf-8")), str), f.name


def test_well_formed_table_still_renders():
    """正常な表は従来どおり <table> になる（停止優先で表を壊していない）。"""
    html = er._md("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in html and "<th>A</th>" in html and "<td>1</td>" in html
