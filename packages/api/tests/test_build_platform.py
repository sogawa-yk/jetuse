"""配備スクリプトが build 時に platform を固定していることを固定する。

**なぜテストが要るか（2026-08-04 の実害）**: `ops/dev-env-up.sh` が `--platform` を
指定していなかったため、Apple Silicon 上の build が **arm64** イメージを作り、
Container Instance（x86 shape）が受け付けずに apply が落ちた。

    Error Message: work request did not succeed ...
    A container image provided is not compatible with the processor architecture
    of the shape selected for the container instance.

しかも Container Instance は image_url 変更で**置換**されるため、**旧インスタンスは
先に削除済み**だった。つまり「失敗しても元に戻る」ではなく、**環境が落ちたまま復旧できない**。
だから「落ちたら直せばいい」では済まず、指定が消えていないことを機械で見張る。

シェルの実行ではなく**スクリプトの記述**を検査する（実 build は数十分かかるため）。
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS = [
    REPO / "ops" / "dev-env-up.sh",
    REPO / "ops" / "deploy-hosted-agent.sh",
]


def _build_lines(path: pathlib.Path) -> list[str]:
    """コメントを除いた実行行のうち、コンテナ build を呼ぶ行。"""
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        if re.search(r'(^|[\s"])(\$CE|\$\{CE\}|podman|docker)"?\s+build\b', s):
            out.append(s)
    return out


def test_every_script_has_a_build_line():
    """検査対象が消えていないこと（スクリプト側の改名で空振りしないため）。"""
    for p in SCRIPTS:
        assert _build_lines(p), f"{p.name} に build 行が見つからない"


def test_build_pins_platform():
    """すべての build 行が --platform を渡している。"""
    for p in SCRIPTS:
        for line in _build_lines(p):
            assert "--platform" in line, (
                f"{p.name} の build が platform を固定していない: {line}\n"
                "Apple Silicon で arm64 イメージができ、Container Instance が弾く。"
            )


def test_platform_value_comes_from_the_override_variable():
    """`--platform` に渡す値が **`JETUSE_BUILD_PLATFORM` 由来**であること。

    「build 行に --platform がある」と「ファイルのどこかに既定値がある」を別々に見るだけだと、
    build が無関係な値を渡していても未使用の既定値が残っていれば通ってしまう（review-9 minor）。
    渡している実体を辿る。
    """
    for p in SCRIPTS:
        src = p.read_text()
        for line in _build_lines(p):
            m = re.search(r'--platform\s+"?\$\{?(\w+)', line)
            assert m, f"{p.name}: --platform の値が変数から来ていない: {line}"
            var = m.group(1)
            if var == "JETUSE_BUILD_PLATFORM":
                assert "JETUSE_BUILD_PLATFORM:-linux/amd64" in line, (
                    f"{p.name}: 既定が linux/amd64 でない: {line}")
            else:
                # 中間変数を経由する場合は、その代入が既定値を持つこと
                assert re.search(
                    rf'^{var}="\$\{{JETUSE_BUILD_PLATFORM:-linux/amd64\}}"', src, re.MULTILINE), (
                    f"{p.name}: {var} が JETUSE_BUILD_PLATFORM:-linux/amd64 から来ていない")


def test_no_bare_container_engine_in_scripts():
    """`podman` / `docker` の直書きが復活していないこと（_container.sh 経由に統一）。"""
    for p in SCRIPTS:
        for line in _build_lines(p):
            assert not re.search(r"^(podman|docker)\s", line), (
                f"{p.name} でエンジンを直書きしている: {line}\n"
                "ops/_container.sh の jetuse_container_engine を使うこと。"
            )
