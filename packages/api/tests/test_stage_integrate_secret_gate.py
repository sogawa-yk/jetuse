"""ステージ統合の秘匿値ゲート（`integrate_task.sh`）が**実際に staged 差分を見る**ことを固定する。

**なぜ要るか**（2026-08-19 VID-01 で判明）: ゲートは

    git diff --cached -U0 | grep -aErqi '(...|password\\s*=\\s*[^ ]{6,})'

と書かれていたが、**BSD grep（macOS）は引数の無い `-r` で stdin ではなく cwd を再帰検索する**。
つまりパイプで渡した staged 差分を一度も見ておらず、worktree 全体を走査していた。
結果 `infra/terraform/.../main.tf` の `admin_password = var.adb_admin_password` に当たって
**常に発火**し、統合が毎回止まっていた。「安全網が働いている」ように見えて、
実際には**検査したい対象を検査していなかった**。

このテストはスクリプトから実際のパイプラインを取り出して両方向を確かめる:
- 秘匿値を含む staged 差分 → 止まる（検知漏れが無い）
- 秘匿値を含まない staged 差分 → 通る（worktree に紛らわしい行があっても誤検知しない）
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".claude" / "skills" / "stage-runner" / "scripts" / "integrate_task.sh"


def _gate_pipeline() -> str:
    """スクリプトから秘匿値検査の1行を取り出す。構造が変わったら失敗する。"""
    m = re.search(r"^\s*if (git diff --cached -U0 \| grep [^\n]+); then$",
                  SCRIPT.read_text(encoding="utf-8"), re.M)
    assert m, "秘匿値ゲートの行を見つけられない（integrate_task.sh の構造が変わった）"
    return m.group(1)


def _repo(tmp_path, *, worktree_files: dict[str, str], staged: dict[str, str]):
    r = tmp_path / "r"
    r.mkdir()
    def g(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@e"); g("config", "user.name", "t")
    for name, body in worktree_files.items():
        (r / name).write_text(body, encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "base")
    for name, body in staged.items():
        (r / name).write_text(body, encoding="utf-8")
    g("add", "-A")
    return r


def _run_gate(repo) -> int:
    """ゲートの終了値を返す。0 = 秘匿値を検出（＝統合を止める）。"""
    return subprocess.run(["bash", "-c", _gate_pipeline()], cwd=repo,
                          capture_output=True).returncode


# worktree に「紛らわしいが正当な」行を置く。これがリポジトリの実態
# （infra/terraform の変数参照）で、以前はこれに当たって常に止まっていた。
DECOY = {"infra.tf": "admin_password = var.adb_admin_password\n"}


def test_clean_diff_passes_even_with_decoy_in_worktree(tmp_path):
    """**本丸。** staged 差分が無害なら、worktree に紛らわしい行があっても通ること。"""
    r = _repo(tmp_path, worktree_files=DECOY, staged={"clean.py": "x = 1\n"})
    assert _run_gate(r) != 0, "staged 差分ではなく worktree を見ている（-r が戻っている）"


def test_secret_in_staged_diff_is_detected(tmp_path):
    """検知漏れが無いこと。"""
    cases = [("leak.env", "ADB_ADMIN_PASSWORD=hunter2xyz\n"),
             ("w.env", "ADB_WALLET_PASSWORD=abcdefgh\n"),
             ("k.pem", "-----BEGIN RSA PRIVATE KEY-----\n"),
             ("a.cfg", "aws_secret_access_key = AKIAxxxxxxxx\n"),
             ("p.cfg", "password = supersecret\n")]
    for i, (name, body) in enumerate(cases):
        d = tmp_path / f"case{i}"
        d.mkdir()
        r = _repo(d, worktree_files={"a.txt": "x\n"}, staged={name: body})
        assert _run_gate(r) == 0, f"{name} を検出できていない"


def test_gate_does_not_use_recursive_grep(tmp_path):
    """`-r` を禁じる。BSD grep は引数無しの `-r` で stdin を無視して cwd を読む。"""
    line = _gate_pipeline()
    m = re.search(r"grep\s+(-[A-Za-z]+)", line)
    assert m, line
    assert "r" not in m.group(1), f"grep に -r が付いている: {line}"


def test_gate_does_not_use_non_posix_escapes(tmp_path):
    r"""`\s` は POSIX ERE で未定義。環境によって挙動が変わる検査を安全網にしない。"""
    assert r"\s" not in _gate_pipeline()
