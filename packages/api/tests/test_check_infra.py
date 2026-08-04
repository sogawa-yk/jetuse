"""`ops/check-infra.sh` の変更検出の境界条件を固定する。

**なぜテストが要るか**: この検査は「ループが緑なら CI も緑」を保つための入口で、
`infra_changed` が誤って「変更なし」を返すと、**未検査のまま緑になる**（一番まずい壊れ方）。
条件が複数あるので、実際にシェルを走らせて振る舞いを固定する。

実際の terraform は使わない（`PATH` から外して「terraform 未導入」の分岐だけを見る）。
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "check-infra.sh"


def _run(cwd: pathlib.Path, *, path: str = "/usr/bin:/bin:/usr/sbin:/sbin"):
    """terraform を PATH から外して実行する（未導入の分岐を見る）。"""
    # **コピーした方**を呼ぶ。スクリプトは `cd "$(dirname "$0")/.."` で自分の位置から
    # リポジトリ root へ移動するので、実リポジトリのパスを渡すと**本物のリポジトリを検査**
    # してしまう（一時リポジトリの状態を見ない）。
    return subprocess.run(
        ["bash", "ops/check-infra.sh"], cwd=cwd, capture_output=True, text=True,
        env={"PATH": path, "HOME": str(cwd), "INFRA_BASE_REF": "origin/main"},
    )


def _git(cwd: pathlib.Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """infra を持つ最小のリポジトリを作る。"""
    r = tmp_path / "repo"
    (r / "infra").mkdir(parents=True)
    (r / "ops").mkdir()
    (r / "infra" / "x.tf").write_text("# x\n", encoding="utf-8")
    (r / "ops" / "check-infra.sh").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    # 基準 ref を用意する。実リポジトリには必ず origin/main がある。
    # **基準がどれも無い場合は「変更あり」に倒す**のが仕様（判断できないなら検査する側へ）なので、
    # 「変更なしならスキップ」を確かめるにはこれが要る。
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    return r


def test_no_base_ref_is_treated_as_changed(tmp_path):
    """基準 ref が取れない（浅い clone 等）なら**検査する側へ倒す**。

    判断できないときに「変更なし」と決めつけると、未検査のまま緑になる。
    """
    r = _repo(tmp_path)
    _git(r, "update-ref", "-d", "refs/remotes/origin/main")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_skips_when_no_infra_change_and_no_terraform(tmp_path):
    """infra を触っていないなら、terraform が無くても止めない。"""
    r = _repo(tmp_path)
    res = _run(r)
    assert res.returncode == 0, res.stderr
    assert "スキップ" in res.stderr


def test_fails_when_infra_changed_and_no_terraform(tmp_path):
    """**未検査のまま緑にしない。** ここが緩むとこの入口を足した意味が無くなる。"""
    r = _repo(tmp_path)
    (r / "infra" / "x.tf").write_text("# changed\n", encoding="utf-8")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_fails_when_only_the_check_machinery_changed(tmp_path):
    """検査の仕組み自体を変えた場合も検知する（検査を壊す変更ほど検知したい）。"""
    r = _repo(tmp_path)
    (r / "Makefile").write_text("lint:\n\t@true\n", encoding="utf-8")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_untracked_infra_file_is_detected(tmp_path):
    """新規ファイル（未追跡）も変更とみなす。"""
    r = _repo(tmp_path)
    (r / "infra" / "new.tf").write_text("# new\n", encoding="utf-8")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_deleted_infra_file_is_detected(tmp_path):
    """削除も変更。`--cached` だけを見ると見落とす形。"""
    r = _repo(tmp_path)
    (r / "infra" / "x.tf").unlink()
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr
