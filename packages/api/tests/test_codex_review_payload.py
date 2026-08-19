"""codex レビューの入力ペイロードが**必ず UTF-8 として読める**ことを固定する。

**なぜ要るか**（2026-08-19 VID-01 で実際に起きた）: `run_codex_review.sh` は
`runs/<run-id>/e2e/` の証跡を全部 `tail -c` で流し込んでいた。E2E のスクリーンショット
（PNG）が1つ混ざるだけで codex が

    Failed to read prompt from stdin: input is not valid UTF-8 (invalid byte at offset 13347)

で rc=1 になり、**レビューが判定不能（verdict=ERROR）**になる。エージェントは PNG を
`e2e/` の外へ退避して回避した ——「証跡の置き場を仕組みの都合で歪めた」形であり、
UI タスクのように画像証跡が主役になる場面で必ず再発する。

このテストは**スクリプトの実際の抽出ロジックを走らせて**確かめる。文字列検査だけだと
「コメントには書いてあるが動いていない」を通してしまう。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".claude" / "skills" / "codex-review" / "scripts" / "run_codex_review.sh"


def _extract_attach_block(src: str) -> str:
    """スクリプトから証跡添付の while ループを取り出す。

    スクリプト全体は codex を実際に呼ぶので走らせられない。添付部分だけを切り出して
    同じ shell で評価する。切り出せなくなったら（＝構造が変わったら）テストは失敗する。
    """
    m = re.search(r"(find \"\$E2E_DIR\" -type f \| sort \| while read -r ef; do.*?\n    done)",
                  src, re.S)
    assert m, "証跡添付のループを見つけられない（run_codex_review.sh の構造が変わった）"
    return m.group(1)


def _run_attach(tmp_path, files: dict[str, bytes]) -> bytes:
    d = tmp_path / "e2e"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    block = _extract_attach_block(SCRIPT.read_text(encoding="utf-8"))
    script = f'set -u\nE2E_DIR="{d}"\n{block}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


def test_binary_evidence_does_not_break_utf8(tmp_path):
    """**本丸。** PNG が混ざってもペイロードは UTF-8 として読めること。"""
    out = _run_attach(tmp_path, {"scenario-1.txt": "登録 OK\n".encode(),
                                 "shot.png": PNG})
    out.decode("utf-8")  # ここで例外なら codex が rc=1 で落ちる状態に戻っている


def test_binary_evidence_is_still_reported(tmp_path):
    """**黙って落とさない。** 中身は流さないが、存在とサイズは伝える。

    そうしないと「証跡が無い」のか「添付できなかった」のかをレビュアーが区別できない。
    """
    out = _run_attach(tmp_path, {"shot.png": PNG}).decode("utf-8")
    assert "shot.png" in out
    assert "バイナリ" in out
    assert str(len(PNG)) in out, "サイズを示していない"


def test_text_evidence_is_still_attached(tmp_path):
    """テキスト証跡は従来どおり中身が入ること（バイナリ判定で巻き込まない）。"""
    out = _run_attach(tmp_path, {"scenario-1.txt": "雨天の場面を検出\n".encode()}).decode("utf-8")
    assert "雨天の場面を検出" in out


def test_empty_file_is_not_treated_as_binary(tmp_path):
    """空ファイルをバイナリ扱いしない（`grep -I` は空入力で非ゼロを返す）。"""
    out = _run_attach(tmp_path, {"empty.txt": b""}).decode("utf-8")
    assert "empty.txt" in out
    assert "バイナリ" not in out


def test_japanese_text_is_not_treated_as_binary(tmp_path):
    """日本語のテキスト証跡をバイナリと誤判定しないこと。"""
    body = ("シナリオ2: 一覧に出る\n" * 50).encode("utf-8")
    out = _run_attach(tmp_path, {"scenario-2.txt": body}).decode("utf-8")
    assert "バイナリ" not in out
    assert "シナリオ2" in out


def test_detection_fails_when_binary_guard_is_removed(tmp_path):
    """**ガードを外したら壊れること**を確かめる（テストが実効か）。"""
    src = SCRIPT.read_text(encoding="utf-8")
    block = _extract_attach_block(src)
    naive = re.sub(r'if LC_ALL=C grep -qI \. "\$ef".*?\n      else\n.*?\n      fi\n',
                   '        tail -c 8000 "$ef"\n        printf \'\\n\'\n', block, flags=re.S)
    assert naive != block, "変異を作れない（構造が変わった）"
    d = tmp_path / "e2e"; d.mkdir(parents=True, exist_ok=True)
    (d / "shot.png").write_bytes(PNG)
    r = subprocess.run(["bash", "-c", f'set -u\nE2E_DIR="{d}"\n{naive}\n'], capture_output=True)
    try:
        r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return  # 期待どおり壊れる
    raise AssertionError("ガードを外しても UTF-8 のまま = テストが判定を見ていない")
