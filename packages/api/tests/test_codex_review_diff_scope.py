"""codex レビューの diff に**新規ファイルが載る**ことを固定する。

**なぜ要るか**（2026-08-20 に判明）: diff は `git diff HEAD` で作っていたが、これは
**追跡済みの変更しか出さない**。新規モジュール・新規テスト・新規 migration —— つまり
タスクの成果物の大半 —— が**レビュー対象から丸ごと落ちていた**。

VID-01 / VID-02 / VID-03 の初回レビューが毎回「完了対象として記載されたファイルが diff 外」で
FAIL しており、1 ラウンドずつ無駄になっていた。それは表に出た症状に過ぎず、**本当に悪いのは
エージェントが stage し忘れたまま通ったときに新規コードが未レビューで PASS しうる**こと。
レビューゲートの網に穴が空いていた。

`git add -N`（intent-to-add）で**内容を stage せず存在だけ**を index に伝えると `git diff HEAD`
に現れる。`.gitignore` 済みは対象外のままなので `.env` 等は載らない。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".claude" / "skills" / "codex-review" / "scripts" / "run_codex_review.sh"


def _diff_block() -> str:
    """スクリプトから diff 生成部（intent-to-add ＋ case）を取り出す。"""
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"(EXCL=\(.*?\nesac)", src, re.S)
    assert m, "diff 生成部を見つけられない（run_codex_review.sh の構造が変わった）"
    return m.group(1)


def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    def g(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@e"); g("config", "user.name", "t")
    (r / ".gitignore").write_text(".env\n", encoding="utf-8")
    (r / "existing.py").write_text("old = 1\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "base")
    return r


def _run(repo, scope="uncommitted") -> str:
    script = f'set -u\nSCOPE="{scope}"\n{_diff_block()}\nprintf "%s" "$DIFF"\n'
    p = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_new_file_appears_in_diff(tmp_path):
    """**本丸。** 未追跡の新規ファイルがレビュー対象に載ること。"""
    r = _repo(tmp_path)
    (r / "new_module.py").write_text("def analyze():\n    return 1\n", encoding="utf-8")
    out = _run(r)
    assert "new_module.py" in out, f"新規ファイルが diff に無い（レビューされない）:\n{out}"
    assert "def analyze" in out, "内容が載っていない"


def test_modified_file_still_appears(tmp_path):
    """既存の変更は従来どおり載ること（壊していない）。"""
    r = _repo(tmp_path)
    (r / "existing.py").write_text("old = 2\n", encoding="utf-8")
    out = _run(r)
    assert "existing.py" in out


def test_gitignored_file_is_not_leaked(tmp_path):
    """**`.gitignore` 済みは載せない。** `.env` を codex へ送らない。"""
    r = _repo(tmp_path)
    (r / ".env").write_text("ADB_ADMIN_PASSWORD=hunter2xyz\n", encoding="utf-8")
    (r / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    out = _run(r)
    assert "new_module.py" in out
    assert "ADB_ADMIN_PASSWORD" not in out, "gitignore 済みの秘匿値が diff に載っている"
    assert ".env" not in out


def test_dist_is_still_excluded(tmp_path):
    """build 生成物は除外のまま（巨大1行 diff で codex 入力上限を超える）。"""
    r = _repo(tmp_path)
    d = r / "packages" / "web" / "dist"
    d.mkdir(parents=True)
    (d / "bundle.js").write_text("var a=1;" * 200, encoding="utf-8")
    out = _run(r)
    assert "bundle.js" not in out, "dist が diff に載っている"


def test_staged_scope_is_untouched(tmp_path):
    """`staged` スコープでは intent-to-add しない（明示的に stage したものだけを見る契約）。"""
    r = _repo(tmp_path)
    (r / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    out = _run(r, scope="staged")
    assert "new_module.py" not in out


def test_detection_fails_without_intent_to_add(tmp_path):
    """**intent-to-add を外したら落ちること**を確かめる（テストが実効か）。"""
    r = _repo(tmp_path)
    (r / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    block = _diff_block()
    naive = re.sub(r'if \[ "\$SCOPE" != "staged" \]; then.*?\nfi\n', "", block, flags=re.S)
    assert naive != block, "変異を作れない（構造が変わった）"
    script = f'set -u\nSCOPE="uncommitted"\n{naive}\nprintf "%s" "$DIFF"\n'
    p = subprocess.run(["bash", "-c", script], cwd=r, capture_output=True, text=True)
    assert "new_module.py" not in p.stdout, "外しても載る = この経路を検証していない"


def test_bootstrap_initializes_terraform():
    """ステージ/タスク worktree で `make lint` が terraform の lock で落ちないこと。

    `.terraform/` は gitignore なので新しい worktree には無く、`ops/check-infra.sh` の
    `terraform init -lockfile=readonly` が "Provider dependency changes detected" で落ちる。
    """
    src = (ROOT / ".claude" / "loop" / "bootstrap-env.sh").read_text(encoding="utf-8")
    body = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    assert "terraform init -backend=false" in body, "bootstrap が terraform を初期化していない"
    # check-infra が見る全ディレクトリを網羅しているか
    infra = (ROOT / "ops" / "check-infra.sh").read_text(encoding="utf-8")
    dirs = set(re.findall(r"(infra/(?:terraform/(?:environments|modules)/[\w-]+|orm))", infra))
    for d in dirs:
        assert d in body, f"{d} が bootstrap の init 対象に無い（make lint がそこで落ちる）"
