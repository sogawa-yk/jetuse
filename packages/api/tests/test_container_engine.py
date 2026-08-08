"""`ops/_container.sh` のエンジン解決を固定する。

**なぜテストが要るか**: 配備スクリプトが `podman` を直書きしていたため、docker しか無い
開発機では `podman: command not found` で配備が始まらなかった（2026-08-04 の実害。
CLAUDE.md は「podman 5.6」を確定事実として載せていたが、実機と乖離していた）。

解決の優先順と失敗時の挙動を固定する。PATH を差し替えて「どちらが在るか」を作り分ける。
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
LIB = REPO / "ops" / "_container.sh"


def _fake(tmp_path: pathlib.Path, *names: str) -> pathlib.Path:
    """指定した名前だけを持つ PATH ディレクトリを作る。"""
    d = tmp_path / ("bin-" + "-".join(names or ("none",)))
    d.mkdir()
    for n in names:
        p = d / n
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    return d


def _run(path_dir: pathlib.Path, env_engine: str | None = None):
    # **PATH は偽物ディレクトリだけにする。** /usr/bin 等を足すと、ホストに入っている
    # podman/docker が探索対象に残り「どちらも無い」ケースを作れない（review-7）。
    # `command -v` も `printf` も bash の builtin なので、これで十分動く。
    env = {"PATH": str(path_dir), "HOME": str(path_dir)}
    if env_engine is not None:
        env["JETUSE_CONTAINER_ENGINE"] = env_engine
    # bash 自体は絶対パスで起動する（子の PATH を偽物だけにしたので探索できない）
    return subprocess.run(
        ["/bin/bash", "-c", f'. "{LIB}"; jetuse_container_engine'],
        capture_output=True, text=True, env=env,
    )


def test_prefers_podman_when_both_present(tmp_path):
    r = _run(_fake(tmp_path, "podman", "docker"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == "podman"


def test_falls_back_to_docker(tmp_path):
    """podman が無くても docker があれば配備できる（今回の実害そのもの）。"""
    r = _run(_fake(tmp_path, "docker"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == "docker"


def test_uses_podman_when_only_podman(tmp_path):
    r = _run(_fake(tmp_path, "podman"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == "podman"


def test_fails_closed_when_neither_present(tmp_path):
    """どちらも無ければ**黙って進まない**。空文字を返すと後続が謎のエラーになる。"""
    r = _run(_fake(tmp_path))
    assert r.returncode != 0
    assert r.stdout == ""
    assert "見つからない" in r.stderr


def test_explicit_override_wins(tmp_path):
    """両方あっても JETUSE_CONTAINER_ENGINE の指定が優先される。"""
    r = _run(_fake(tmp_path, "podman", "docker"), env_engine="docker")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "docker"


def test_explicit_override_missing_fails(tmp_path):
    """明示指定が PATH に無いなら落とす（別のエンジンへ勝手に切り替えない）。"""
    r = _run(_fake(tmp_path, "docker"), env_engine="podman")
    assert r.returncode != 0
    assert r.stdout == ""
    assert "PATH に無い" in r.stderr
