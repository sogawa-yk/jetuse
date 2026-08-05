"""`ops/er.py` が「止まっている作業」をどう見せるかを固定する。

**なぜテストが要るか**: 検出（`ops/stalled.py`）を直しても、レポート側の分類と表示が
追随していなければ**人の目には届かない**。`unreleased` が「要注意」ではなく下段の表へ
落ちれば、`public-dev → main` の未リリースは畳まれて気づかれない
（[[ER-0021]] が直したかった漏れが、表示側で復活する）。

`stalled.scan()` を差し替え、レポートの行だけを検査する。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]


def _load_er():
    spec = importlib.util.spec_from_file_location("er_mod_rows", REPO / "ops" / "er.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def er(monkeypatch):
    """`_stalled_rows()` が import する `stalled` を差し替えた `er` モジュール。"""
    mod = _load_er()
    fake = type(sys)("stalled")
    monkeypatch.setitem(sys.modules, "stalled", fake)
    return mod, fake


WARN = ("review", "unreleased", "unshipped", "unknown")


@pytest.mark.parametrize("status", WARN)
def test_attention_statuses_go_to_the_warning_table(er, status):
    """放っておくと問題になるものは**上段（要注意）**に出す。"""
    mod, fake = er
    fake.scan = lambda: [{"name": "public-dev → main", "status": status,
                          "why": "先行している", "days": 3}]
    warn, rest = mod._stalled_rows()
    assert len(warn) == 1 and not rest, f"{status} が要注意側に出ていない"
    assert "public-dev → main" in warn[0]
    assert "3日前" in warn[0]


@pytest.mark.parametrize("status", ["pr", "parked", "clean"])
def test_handled_statuses_go_to_the_lower_table(er, status):
    """把握済みのものは下段へ。毎回上段に出すと本当に見るべきものが埋もれる。"""
    mod, fake = er
    fake.scan = lambda: [{"name": "x", "status": status, "why": "把握済み", "days": 1}]
    warn, rest = mod._stalled_rows()
    assert not warn and len(rest) == 1


def test_unreleased_is_shown_in_japanese(er):
    """`unreleased` に日本語表示がある（生の英単語が表に出ない）。"""
    mod, fake = er
    assert mod.STALL_JA.get("unreleased") == "リリースされていない"
    fake.scan = lambda: [{"name": "public-dev → main", "status": "unreleased",
                          "why": "16 commit 先行", "days": 2}]
    warn, _ = mod._stalled_rows()
    assert "リリースされていない" in warn[0]
    assert "unreleased" not in warn[0]


def test_every_status_stalled_can_emit_has_a_label(er):
    """`stalled.py` が返しうる状態はすべて日本語表示を持つ。追加時の取りこぼしを防ぐ。"""
    mod, _ = er
    spec = importlib.util.spec_from_file_location("stalled_real", REPO / "ops" / "stalled.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    assert st.RANK, "stalled.RANK が無い（状態一覧の正本が失われた）"
    for status in st.RANK:
        assert status in mod.STALL_JA, f"{status} の日本語表示が無い"


def test_missing_days_does_not_break_the_row(er):
    """日数が取れなくても行は出す（判定できないものこそ消してはいけない）。"""
    mod, fake = er
    fake.scan = lambda: [{"name": "public-dev → main", "status": "unknown",
                          "why": "判定できない", "days": None}]
    warn, _ = mod._stalled_rows()
    assert "不明" in warn[0]


def test_scan_failure_does_not_break_the_report(er):
    """`scan()` が落ちてもレポート生成は続く（一覧ごと消えない）。"""
    mod, fake = er

    def boom():
        raise RuntimeError("boom")

    fake.scan = boom
    warn, rest = mod._stalled_rows()
    assert warn and "確認できませんでした" in warn[0]
