"""版（public / internal）の取り違えを止める検査（`ops/where.sh`）。

**なぜ要るか**: どちらの版を触っているかを言い忘れたまま作業が進むと、共有物が
internal 側に着地して `main` へ届かない（2026-07 の実害）。CI の
`ops/check-branch-base.sh` は PR の base を見るので効くが、**ローカルでは base を
推測できないとして黙ってスキップ**していた（＝合格ではない）。`where.sh` はその穴を
merge-base による起点推定で埋める。

推定が成り立つ根拠は **Internal ⊇ Public**（ADR-0028）:
  merge-base(HEAD, public-dev) == merge-base(HEAD, internal-dev) → public 起点
  不一致（internal 側が新しい）                                   → internal 起点

このテストは**実際に git リポジトリを作って**両方の起点を再現し、判定させる。
静的検査だけだと「文字列は書いてあるが判定していない」を通してしまう。
"""

from __future__ import annotations

import itertools
import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
WHERE = ROOT / "ops" / "where.sh"
PATHS = ROOT / "ops" / "internal-only-paths.txt"


def _git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, **kw)


def _run_where(repo, script=None, path=None):
    """`where.sh` を repo で実行し (stdout+stderr, returncode) を返す。"""
    env = dict(os.environ)
    # .env を読ませない・oci を呼ばせない（判定部分だけを見る）。
    env["PATH"] = path or "/usr/bin:/bin:/usr/sbin:/sbin"
    p = subprocess.run(["bash", str(script or WHERE)], cwd=repo,
                       capture_output=True, text=True, env=env)
    return p.stdout + p.stderr, p.returncode


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """4ブランチ体制のミニチュアを作る。**Internal は Public を包含する**。"""
    d = tmp_path_factory.mktemp("repo")
    _git(d, "init", "-q", "-b", "public-dev")
    (d / "ops").mkdir()
    # 判定の正本をそのまま持ち込む（テスト用に書き換えない）。
    (d / "ops" / "internal-only-paths.txt").write_text(
        PATHS.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "ops" / "where.sh").write_text(WHERE.read_text(encoding="utf-8"),
                                        encoding="utf-8")
    (d / "docs").mkdir()
    (d / "docs" / "tips.md").write_text("shared\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base")

    # internal-dev = public-dev + 内部固有の追加コミット
    _git(d, "checkout", "-q", "-b", "internal-dev")
    internal_first = _internal_path()
    tgt = d / internal_first
    tgt.parent.mkdir(parents=True, exist_ok=True)
    tgt.write_text("internal only\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "internal")
    _git(d, "checkout", "-q", "public-dev")
    return d


def _internal_dir() -> str:
    """一覧のうち**ディレクトリ接頭辞**のもの（末尾 `/`）を返す。

    配下に任意の名前のファイルを置ける形でないと、非 ASCII 名の分類を試せない。
    """
    for line in PATHS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.endswith("/"):
            return line
    raise AssertionError("internal-only-paths.txt にディレクトリ接頭辞が無い")


def _internal_path() -> str:
    """`internal-only-paths.txt` の先頭のパターンを、実ファイルとして使える形で返す。"""
    for line in PATHS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            base = line.rstrip("/")
            # 一覧はディレクトリ接頭辞とファイル接頭辞が混在する。ファイルとして書けるようにする。
            return base if "." in pathlib.Path(base).name else base + "/x.sh"
    raise AssertionError("internal-only-paths.txt が空")


_SEQ = itertools.count()


def _branch_with(repo, base, path, content="probe\n"):
    """`base` から枝を切り、`path` を変更してコミットする。

    枝名は毎回一意にする（内容から導くとテスト間で衝突して git が 128 で落ちる）。
    """
    name = f"probe-{next(_SEQ)}"
    # **前のテストの汚れを持ち越さない。** repo は module スコープなので、作業ツリーを
    # 汚したテスト（一覧の書き換え等）の影響が次の判定に混ざる。
    _git(repo, "checkout", "-q", "--", ".")
    _git(repo, "clean", "-qfd")
    _git(repo, "checkout", "-q", "-b", name, base)
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f.read_text(encoding="utf-8") + content if f.exists() else content,
                 encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "probe")
    return name


# --- 起点の推定 --------------------------------------------------------------

def test_public_base_is_detected(repo):
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo)
    assert "public（公開版）" in out
    assert "internal（内部版）" not in out


def test_internal_base_is_detected(repo):
    """**これが本丸。** internal 起点を public と誤判定すると検査全体が無意味になる。"""
    _branch_with(repo, "internal-dev", "docs/tips.md")
    out, _ = _run_where(repo)
    assert "internal（内部版）" in out
    assert "public（公開版）" not in out


def test_long_lived_branches_report_themselves(repo):
    for br, want in [("public-dev", "public（公開版）"),
                     ("internal-dev", "internal（内部版）")]:
        _git(repo, "checkout", "-q", br)
        out, _ = _run_where(repo)
        assert want in out, f"{br} で {want} が出ない: {out}"


# --- 起点と変更内容の突き合わせ ------------------------------------------------

def test_shared_change_on_internal_base_is_flagged(repo):
    """共有物だけを internal 起点で触ると警告する（`main` へ届かない形）。"""
    _branch_with(repo, "internal-dev", "docs/tips.md")
    out, _ = _run_where(repo)
    assert "!!" in out
    assert "main へ届きません" in out


def test_internal_change_on_public_base_is_flagged(repo):
    """内部固有パスを public 起点で触ると警告する（Public に存在しないもの）。"""
    _branch_with(repo, "public-dev", _internal_path())
    out, _ = _run_where(repo)
    assert "!!" in out
    assert "internal-dev 起点にしてください" in out


def test_correct_combinations_are_not_flagged(repo):
    """正しい組み合わせでは警告を出さない（狼少年にしない）。"""
    for base, path in [("public-dev", "docs/tips.md"),
                       ("internal-dev", _internal_path())]:
        _branch_with(repo, base, path)
        out, _ = _run_where(repo)
        assert "!!" not in out, f"{base}/{path} で誤検知: {out}"
        assert "起点と変更内容は合っています" in out


def test_mixed_change_is_noted(repo):
    """内部固有と共有物の混在は、落とさないが注意を出す（共有部分は main へ届かない）。"""
    _branch_with(repo, "internal-dev", _internal_path())
    (repo / "docs" / "tips.md").write_text("mixed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "shared too")
    out, _ = _run_where(repo)
    assert "混在" in out
    assert "!!" not in out


# --- 判定が本当に効いているか（変異） -------------------------------------------

def test_detection_fails_when_mergebase_logic_is_broken(repo, tmp_path):
    """**推定を壊したらテストが落ちること**を確かめる。

    比較を定数 true に潰すと internal 起点が public と判定されるはず。
    そうならないなら、上のテストは判定ではなく別の何かを見ている。
    """
    broken = tmp_path / "broken.sh"
    src = WHERE.read_text(encoding="utf-8")
    # 「祖先ではない（exit 1）= internal 起点」を public にすり替える
    mutated = src.replace(
        '      1) VERSION="internal（内部版）"; BASE="internal-dev"',
        '      1) VERSION="public（公開版）"; BASE="public-dev"')
    assert mutated != src, "変異対象の行が見つからない（where.sh の構造が変わった）"
    broken.write_text(mutated, encoding="utf-8")
    _branch_with(repo, "internal-dev", "docs/tips.md")
    out, _ = _run_where(repo, script=broken)
    assert "public（公開版）" in out, "変異させても internal のまま = 判定していない"


def test_internal_paths_file_is_the_single_source(repo):
    """判定は `ops/internal-only-paths.txt` を読む（別に一覧を持たない）。"""
    src = WHERE.read_text(encoding="utf-8")
    assert "ops/internal-only-paths.txt" in src
    # ハードコードされた内部固有パスが無いこと
    pats = [ln.strip() for ln in PATHS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    body = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    body = body.replace('PATHS="ops/internal-only-paths.txt"', "")
    for p in pats:
        assert p not in body, f"{p} が where.sh に直接書かれている（二重管理）"


# --- 移植性 -------------------------------------------------------------------

def test_no_bash4_only_features():
    src = WHERE.read_text(encoding="utf-8")
    body = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    for feat in ("mapfile", "readarray", "declare -A", "${!"):
        assert feat not in body, f"{feat} は bash 3.2（macOS 既定）に無い"


def test_no_multibyte_directly_after_variable():
    """`$VAR）` の形を禁じる。全角の先頭バイトが変数名に食われて unbound variable になる。

    2回踏んだ（`$STATE）` / `$WHY）`）。手で見るのをやめて機械で見る。
    """
    bad = []
    for f in sorted((ROOT / "ops").glob("*.sh")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line):
                bad.append(f"{f.name}:{i}")
    assert not bad, f"変数展開の直後に多バイト文字がある（{{}} で囲むこと）: {bad}"


def test_where_is_wired_into_make():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^where:.*##", mk, re.M), "make where が無い"
    assert "ops/where.sh" in mk


# --- 「確認できない」を「問題なし」に丸めない（fail-closed） -------------------

def test_uncommitted_changes_are_counted(repo):
    """**未コミットの変更を見る。** コミット前に気づかせるのが目的なので、
    HEAD までしか見ないと肝心の作業中の変更が映らない
    （`check-branch-base.sh` が同じ理由で一度直されている）。
    """
    _branch_with(repo, "internal-dev", _internal_path())
    out_before, _ = _run_where(repo)
    # コミットせずに共有物を足す → 混在になるはず
    (repo / "docs" / "tips.md").write_text("uncommitted\n", encoding="utf-8")
    out_after, _ = _run_where(repo)
    assert "起点と変更内容は合っています" in out_before, out_before
    assert "混在" in out_after, out_after


def test_untracked_files_are_counted(repo):
    _branch_with(repo, "internal-dev", _internal_path())
    (repo / "docs" / "brand-new.md").write_text("new\n", encoding="utf-8")
    out, _ = _run_where(repo)
    assert "混在" in out, out


def test_classification_failure_is_reported_not_silent(repo, tmp_path):
    """python3 が無ければ分類できない。**黙って飛ばさず「できなかった」と言う。**"""
    stub = tmp_path / "nopy"
    stub.mkdir()
    (stub / "python3").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    (stub / "python3").chmod(0o755)
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo, path=f"{stub}:/usr/bin:/bin")
    assert "分類" in out and "できませんでした" in out, out
    # 「合っています」と言い切ってはいけない（判定していないため）
    assert "起点と変更内容は合っています" not in out, out


def test_adb_state_unresolvable_is_reported(repo):
    """`.env` が無ければ ADB は確認できない。**空欄で済ませない。**"""
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo)   # 一時リポジトリには .env が無い
    assert "確認できない" in out, out


def test_env_file_is_not_sourced(repo):
    """`.env` を source しない（このリポジトリの慣習。任意コードを実行しない）。"""
    src = WHERE.read_text(encoding="utf-8")
    body = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    for pat in (". ./.env", "source .env", "set -a"):
        assert pat not in body, f"{pat} で .env を読み込んでいる"


# --- Codex 指摘（review 2026-08-19）の再現 --------------------------------------

def test_public_branch_is_not_misjudged_when_internal_lags(tmp_path_factory):
    """**blocker の再現。** internal-dev が public-dev に追いついていない状態で、
    public-dev の先端から切った枝を Internal と誤判定しないこと。

    同期（public-dev → internal-dev）は人間ゲートなので、**この状態が普通**である。
    単純な merge-base 等値比較だと mb(pub)=先端 / mb(int)=古い同期点 で食い違い、
    Public の作業が Internal 扱いになって配備先まで間違える。
    """
    d = tmp_path_factory.mktemp("lag")
    _git(d, "init", "-q", "-b", "public-dev")
    (d / "ops").mkdir()
    (d / "ops" / "internal-only-paths.txt").write_text(
        PATHS.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "ops" / "where.sh").write_text(WHERE.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "docs").mkdir()
    (d / "docs" / "tips.md").write_text("P1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "P1")

    # internal-dev は P1 までしか同期していない（＋内部固有コミット I1）
    _git(d, "checkout", "-q", "-b", "internal-dev")
    ip = d / _internal_path()
    ip.parent.mkdir(parents=True, exist_ok=True)
    ip.write_text("I1\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "I1")

    # public-dev だけが先へ進む（P2）。まだ internal へ同期していない。
    _git(d, "checkout", "-q", "public-dev")
    (d / "docs" / "tips.md").write_text("P2\n", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "P2")

    # P2 から枝を切る = ごく普通の Public 作業
    _git(d, "checkout", "-q", "-b", "feature", "public-dev")
    (d / "docs" / "tips.md").write_text("work\n", encoding="utf-8")
    out, _ = _run_where(d)
    assert "public（公開版）" in out, f"同期遅延で Public を Internal と誤判定した:\n{out}"
    assert "!!" not in out, out


def test_rules_are_read_from_base_not_worktree(repo):
    """**major の再現。** 枝が `internal-only-paths.txt` に共有パスを足して
    「整合」を偽装できないこと。分類規則は base 側から読む。
    """
    _branch_with(repo, "internal-dev", "docs/tips.md")   # 共有物のみ = 誤起点
    out_before, _ = _run_where(repo)
    assert "!!" in out_before, out_before
    # 枝の中で一覧に docs/ を足して迂回を試みる
    f = repo / "ops" / "internal-only-paths.txt"
    f.write_text(f.read_text(encoding="utf-8") + "docs/\n", encoding="utf-8")
    out_after, _ = _run_where(repo)
    assert "!!" in out_after, f"作業ツリーの一覧で迂回できてしまう:\n{out_after}"


def test_non_ascii_path_is_classified(repo):
    """**major の再現。** 非 ASCII のファイル名でも先頭一致の分類が効くこと。

    既定の `git diff --name-only` は非 ASCII を "..." で括って \\nnn 展開するため、
    内部固有パス配下でも一致せず shared 扱いになる。
    """
    _branch_with(repo, "internal-dev", f"{_internal_dir()}検証レポート.md")
    out, _ = _run_where(repo)
    assert "共有物      0 件" in out, f"非 ASCII 名が共有物と誤分類された:\n{out}"
    assert "!!" not in out, out


def test_rules_are_unioned_across_both_long_lived_branches(repo):
    """**新しい内部固有パスは先に internal-dev の一覧へ入る**（2段階運用）。

    public 起点のときに public-dev の一覧だけを読むと、登録済みの内部固有パスを
    共有物と誤分類して「合っています」と言ってしまう。両方の長期ブランチの一覧を
    合併して読む（どちらも枝からは書き換えられないので安全）。
    """
    # internal-dev の一覧にだけ新しい内部固有パスを足す
    _git(repo, "checkout", "-q", "internal-dev")
    f = repo / "ops" / "internal-only-paths.txt"
    f.write_text(f.read_text(encoding="utf-8") + "packages/api/internal_new/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "register path")
    # public 起点でそのパスを触る → 内部固有として数えられるべき
    _branch_with(repo, "public-dev", "packages/api/internal_new/x.py")
    out, _ = _run_where(repo)
    assert "内部固有   1 件" in out, f"internal-dev 側の一覧が効いていない:\n{out}"
    assert "!!" in out, out


def test_git_failures_are_reported_not_silent(repo, tmp_path):
    """**git が失敗したら黙らない。** 分類節ごと省くと、取り違えているのに何も出ない
    画面と区別が付かない（fail-closed）。

    落とすのは `git show`。分類段でしか使わないので、起点の推定は成功したまま
    「一覧を読めなかった」経路だけを通せる（merge-base を落とすと起点推定の側で
    先に別の理由が立ってしまい、この経路を検証したことにならない）。
    """
    stub = tmp_path / "nogit"
    stub.mkdir()
    (stub / "git").write_text(
        '#!/bin/sh\n'
        'for a in "$@"; do [ "$a" = "show" ] && exit 1; done\n'
        'exec /usr/bin/git "$@"\n', encoding="utf-8")
    (stub / "git").chmod(0o755)
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo, path=f"{stub}:/usr/bin:/bin")
    assert "できませんでした" in out, out
    assert "読めない" in out, out
    # 判定していないのに「合っています」と言わない
    assert "起点と変更内容は合っています" not in out, out
    # 「差分なし」に丸めない（作業ツリーには変更がある）
    assert "差分なし" not in out and "なし（起点と同じ）" not in out, out


def _git_stub(tmp_path, name, body):
    """`git` を差し替えるスタブを作り、その PATH 前置文字列を返す。"""
    d = tmp_path / name
    d.mkdir()
    (d / "git").write_text("#!/bin/sh\n" + body + 'exec /usr/bin/git "$@"\n', encoding="utf-8")
    (d / "git").chmod(0o755)
    return f"{d}:/usr/bin:/bin"


def test_partial_rule_read_failure_stops_classification(repo, tmp_path):
    """**片方の一覧だけ読めた状態で分類を続けない。**

    union が欠けたまま「合っています」と言えてしまう（internal-dev にだけ登録済みの
    内部固有パスが共有物に化ける）。両方読めたときだけ分類する。
    """
    # public-dev からの読み出しだけ失敗させる
    path = _git_stub(tmp_path, "halfshow",
                     'if [ "$1" = "show" ]; then case "$2" in *public-dev*) exit 1;; esac; fi\n')
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo, path=path)
    assert "できませんでした" in out, out
    assert "起点と変更内容は合っています" not in out, out


def test_ancestor_check_abnormal_exit_is_not_called_internal(repo, tmp_path):
    """**`--is-ancestor` の異常終了を「祖先ではない」と同じ扱いにしない。**

    0=祖先 / 1=祖先でない / それ以外=異常。まとめて else に入れると、答えを出せなかった
    場合を internal と言い切る（fail-closed 違反。配備先まで間違える）。
    """
    path = _git_stub(tmp_path, "badanc",
                     'for a in "$@"; do [ "$a" = "--is-ancestor" ] && exit 3; done\n')
    _branch_with(repo, "public-dev", "docs/tips.md")
    out, _ = _run_where(repo, path=path)
    assert "internal（内部版）" not in out, f"異常終了を internal と誤判定した:\n{out}"
    assert "判定できない" in out, out
