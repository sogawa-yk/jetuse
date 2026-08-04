"""`ops/check-branch-base.sh`（起点判定）の分岐を固定する。

**なぜテストが要るか**: この検査は「共有物が Internal 側に着地して Public へ届かない」
再発（2026-07 の `docs/verification/` 整理）を機械で止めるためのもの。誤って通す方向に
壊れると、規律だけの状態へ静かに戻る。実際レビューで2回、通してはいけない経路が見つかった:

  - `docs/verification/*` を丸ごと中立扱いにして、再発防止の対象そのものが素通りしていた
  - 一覧ファイルを PR で削除すれば検査ごと迂回できた（fail-open）

条件が多いので、実際に一時 git リポジトリを作ってシェルを走らせ、振る舞いを固定する。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "check-branch-base.sh"
PATHS = REPO / "ops" / "internal-only-paths.txt"


def _git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run(cwd: pathlib.Path, *args: str, env_base: str | None = None):
    """一時リポジトリ内の**コピーした方**を呼ぶ。

    スクリプトは `git rev-parse --show-toplevel` で呼び出し元の worktree へ移動するので、
    cwd さえ一時リポジトリなら実リポジトリは見ない。
    """
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(cwd)}
    if env_base is not None:
        env["BRANCH_BASE"] = env_base
    return subprocess.run(
        ["bash", "ops/check-branch-base.sh", *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


def _repo(tmp_path: pathlib.Path, *, with_paths_file: bool = True) -> pathlib.Path:
    """internal-dev を base に持つ一時リポジトリを作り、feature ブランチへ移る。"""
    r = tmp_path / "r"
    (r / "ops").mkdir(parents=True)
    (r / "docs" / "verification" / "demo-platform").mkdir(parents=True)
    (r / "runs" / "x").mkdir(parents=True)
    _git(r.parent, "init", "-q", "-b", "internal-dev", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")

    shutil.copy(SCRIPT, r / "ops" / "check-branch-base.sh")
    if with_paths_file:
        shutil.copy(PATHS, r / "ops" / "internal-only-paths.txt")
    (r / "docs" / "shared.md").write_text("base\n")
    (r / "docs" / "verification" / "AGT-01.md").write_text("base\n")
    (r / "docs" / "verification" / "demo-platform" / "SP1-01.md").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "feature")
    return r


def _commit(r: pathlib.Path, rel: str, body: str = "changed\n") -> None:
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", f"touch {rel}")


def test_base_unspecified_skips_without_failing(tmp_path):
    """ローカル `make lint` を壊さないため、base 未指定はスキップ（合格ではない）。"""
    r = _repo(tmp_path)
    _commit(r, "docs/shared.md")
    res = _run(r)
    assert res.returncode == 0
    assert "SKIP" in res.stdout


def test_public_dev_base_is_out_of_scope(tmp_path):
    """internal-dev 宛以外は検査しない。"""
    r = _repo(tmp_path)
    _commit(r, "docs/shared.md")
    res = _run(r, "public-dev")
    assert res.returncode == 0
    assert "検査対象外" in res.stdout


def test_shared_only_fails(tmp_path):
    """共有物だけの internal-dev 宛 PR は落とす（中核の受け入れ条件）。"""
    r = _repo(tmp_path)
    _commit(r, "docs/shared.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 1
    assert "FAIL" in res.stderr


def test_verification_reorg_is_shared_not_neutral(tmp_path):
    """2026-07 の再発事例そのもの。`docs/verification/` は中立ではなく共有物。"""
    r = _repo(tmp_path)
    _commit(r, "docs/verification/AGT-01.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "docs/verification/AGT-01.md" in res.stderr


def test_internal_only_passes(tmp_path):
    """内部固有パスだけなら通す。"""
    r = _repo(tmp_path)
    _commit(r, "packages/web/src/pages/demobuilder/x.tsx")
    res = _run(r, "internal-dev")
    assert res.returncode == 0
    assert "OK" in res.stdout


def test_internal_only_wins_over_neutral_prefix(tmp_path):
    """一覧に載る `docs/verification/demo-platform/` は中立扱いに負けない。"""
    r = _repo(tmp_path)
    _commit(r, "docs/verification/demo-platform/SP1-01.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 0
    assert "内部固有 1 件" in res.stdout


def test_mixed_warns_but_passes(tmp_path):
    """混在は落とさない。分割を強制すると実務が回らないため。ただし共有部分は示す。"""
    r = _repo(tmp_path)
    _commit(r, "packages/web/src/pages/demobuilder/x.tsx")
    _commit(r, "docs/shared.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 0
    assert "WARN" in res.stderr
    assert "docs/shared.md" in res.stderr


def test_runs_only_is_neutral(tmp_path):
    """ループの実行履歴だけの PR は判定材料が無いので通す。"""
    r = _repo(tmp_path)
    _commit(r, "runs/x/turn-1.json", "{}\n")
    res = _run(r, "internal-dev")
    assert res.returncode == 0


def test_non_ascii_path_is_classified(tmp_path):
    """git は非 ASCII パスをクォートする。中立判定を素通りさせない。"""
    r = _repo(tmp_path)
    _commit(r, "runs/x/レポート.md", "x\n")
    res = _run(r, "internal-dev")
    assert res.returncode == 0
    assert "共有物 0 件" in res.stdout


def test_deleting_paths_file_cannot_bypass(tmp_path):
    """一覧ファイルを PR で消しても、base 側を読むので迂回できない。"""
    r = _repo(tmp_path)
    (r / "ops" / "internal-only-paths.txt").unlink()
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "drop paths file")
    _commit(r, "docs/shared.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 1
    assert "FAIL" in res.stderr


def test_bootstrap_sync_pr_is_not_blocked(tmp_path):
    """base にまだ一覧が無い移行前は通す。

    一覧を internal-dev へ運ぶ最初の同期 PR は「共有物のみ」の差分になるため、HEAD 側の
    一覧で判定すると自分自身を落として移行が始められない（review-6 F001）。
    """
    r = _repo(tmp_path, with_paths_file=False)
    shutil.copy(PATHS, r / "ops" / "internal-only-paths.txt")   # この PR が一覧を持ち込む
    _commit(r, "docs/shared.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "SKIP" in res.stderr


def test_ci_fails_closed_when_base_unresolvable(tmp_path):
    """CI で base を解決できないときは緑にしない（required check の fail-open 防止）。"""
    r = _repo(tmp_path)
    _commit(r, "docs/shared.md")
    res = subprocess.run(
        ["bash", "ops/check-branch-base.sh"], cwd=r, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(r),
             "GITHUB_BASE_REF": "internal-dev"},
    )
    # 一時リポジトリには internal-dev ブランチが存在する（_repo が作る）ので解決できる。
    # 解決できない状況を作るため、base ブランチを消してから再実行する。
    _git(r, "branch", "-D", "internal-dev")
    res = subprocess.run(
        ["bash", "ops/check-branch-base.sh"], cwd=r, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(r),
             "GITHUB_BASE_REF": "internal-dev"},
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_local_run_sees_uncommitted_and_untracked(tmp_path):
    """コミット前チェックとして案内している以上、未コミット・未追跡も見る。"""
    r = _repo(tmp_path)
    (r / "docs" / "shared.md").write_text("uncommitted\n")       # 未コミット
    (r / "docs" / "brand-new.md").write_text("untracked\n")      # 未追跡
    res = _run(r, "internal-dev")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "docs/shared.md" in res.stderr
    assert "docs/brand-new.md" in res.stderr


def test_ci_run_ignores_worktree(tmp_path):
    """CI では PR の確定差分だけを見る（GITHUB_BASE_REF 経由）。"""
    r = _repo(tmp_path)
    (r / "docs" / "shared.md").write_text("uncommitted only\n")
    # 引数も BRANCH_BASE も無く、GITHUB_BASE_REF だけがある状態＝CI の呼ばれ方
    res = subprocess.run(
        ["bash", "ops/check-branch-base.sh"], cwd=r, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(r),
             "GITHUB_BASE_REF": "internal-dev"},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "変更なし" in res.stdout


def test_internal_migration_band_is_recognized(tmp_path):
    """ADR-0028 の 5xx_ 帯は Internal 固有として拾う。"""
    r = _repo(tmp_path)
    _commit(r, "packages/api/jetuse_core/migrations/501_builder_x.sql", "-- x\n")
    res = _run(r, "internal-dev")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "内部固有 1 件" in res.stdout


def test_pr_cannot_widen_the_rules_to_bypass(tmp_path):
    """PR 自身が一覧へ接頭辞を足して自分の共有変更を Internal 扱いにはできない。

    分類は base 側の一覧だけで行う。ここを PR 側と union にすると、共有ファイルの接頭辞を
    一覧に足すだけで検査を迂回できた（review-5 F002）。
    """
    r = _repo(tmp_path)
    paths = r / "ops" / "internal-only-paths.txt"
    paths.write_text(paths.read_text() + "\ndocs/shared.md\nops/internal-only-paths.txt\n")
    _commit(r, "docs/shared.md")
    res = _run(r, "internal-dev")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "docs/shared.md" in res.stderr


def test_sync_pr_from_public_dev_is_exempt(tmp_path):
    """正規の同期 PR（public-dev を merge しただけ）は落とさない。

    同期ブランチは internal-dev から切って public-dev を merge するので、base 比の差分は
    Public 側の共有物そのものになる。ここを落とすと Internal ⊇ Public を維持できない
    （review-7 F001）。ブランチ名ではなく「独自の非 merge コミットが無い」ことで判別する。
    """
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "internal-dev")
    _git(r, "checkout", "-qb", "public-dev")
    _commit(r, "docs/shared.md", "public side change\n")       # Public 側の共有物変更
    _git(r, "checkout", "-q", "internal-dev")
    _git(r, "checkout", "-qb", "refactor/sync-public-internal")
    _git(r, "merge", "--no-ff", "--no-edit", "-q", "public-dev")
    res = _run(r, "internal-dev")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "同期 PR" in res.stdout


def test_feature_branch_cut_from_public_dev_still_fails(tmp_path):
    """public-dev から切っていても、独自コミットを持つ共有物-only PR は落とす。"""
    r = _repo(tmp_path)
    _git(r, "checkout", "-q", "internal-dev")
    _git(r, "checkout", "-qb", "public-dev")
    _git(r, "checkout", "-qb", "feat/misrouted")
    _commit(r, "docs/shared.md", "own commit\n")               # ブランチ独自のコミット
    res = _run(r, "internal-dev")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "FAIL" in res.stderr
