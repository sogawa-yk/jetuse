"""`.claude/hooks/ensure_task_branch.sh` の base 解決を固定する。

**なぜテストが要るか**: このフックは共有チェックアウト運用でタスクブランチを作る。
以前は base が見つからないと**現在地から黙って分岐**していたため、Internal 系ブランチの上で
共有物の作業を始めても正常終了し、ADR-0028 の起点保証が静かに破れた（review-4 F001）。
「base が無いなら止まる」は保証の要なので、シェルを実際に走らせて固定する。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
HOOK = REPO / ".claude" / "hooks" / "ensure_task_branch.sh"


def _git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run(cwd: pathlib.Path, task: str, *, base: str | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(cwd)}
    if base is not None:
        env["BASE_BRANCH"] = base
    return subprocess.run(
        ["bash", ".claude/hooks/ensure_task_branch.sh", task],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


def _repo(tmp_path: pathlib.Path, branches: tuple[str, ...]) -> pathlib.Path:
    """指定したブランチを持つ一時リポジトリを作り、最初のブランチ上に居る状態で返す。"""
    r = tmp_path / "r"
    (r / ".claude" / "hooks").mkdir(parents=True)
    _git(r.parent, "init", "-q", "-b", branches[0], str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    shutil.copy(HOOK, r / ".claude" / "hooks" / "ensure_task_branch.sh")
    (r / "f.txt").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    for b in branches[1:]:
        _git(r, "branch", b)
    return r


def _head_of(r: pathlib.Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=r, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_defaults_to_public_dev(tmp_path):
    """BASE_BRANCH 未指定なら public-dev から分岐する。"""
    r = _repo(tmp_path, ("internal-dev", "public-dev"))
    _git(r, "commit", "-q", "--allow-empty", "-m", "internal only")  # 両者を分岐させる
    res = _run(r, "T-1")
    assert res.returncode == 0, res.stderr
    assert _head_of(r, "feat/T-1") == _head_of(r, "public-dev")


def test_base_branch_override_uses_internal_dev(tmp_path):
    """内部固有タスクは BASE_BRANCH=internal-dev で上書きできる。"""
    r = _repo(tmp_path, ("internal-dev", "public-dev"))
    _git(r, "commit", "-q", "--allow-empty", "-m", "internal only")
    res = _run(r, "T-2", base="internal-dev")
    assert res.returncode == 0, res.stderr
    assert _head_of(r, "feat/T-2") == _head_of(r, "internal-dev")


def test_missing_base_fails_instead_of_branching_from_here(tmp_path):
    """base が無いなら**現在地から作らず**止まる。起点保証の要。"""
    r = _repo(tmp_path, ("internal-dev",))  # public-dev が無い
    res = _run(r, "T-3")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "ERROR" in res.stderr
    # ブランチが作られていないこと
    out = subprocess.run(["git", "branch", "--list", "feat/T-3"],
                         cwd=r, capture_output=True, text=True).stdout
    assert out.strip() == ""
