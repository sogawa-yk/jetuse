"""GitHub Actions の設定が OSS 前提の水準を保つことを固定する。

**なぜ要るか**: ワークフローは普段誰も読み返さない。緩めても気づかれないまま残り、
外部からの PR を受け付け始めたときに効いてくる。ここで機械的に見張る。

見ているのは3つ:

1. **アクションを SHA でピン留めしているか。** タグ（`@v4`）は動く参照で、
   タグの付け替えで**別のコードが実行されうる**。OSS はフォークからの PR が
   任意のコードを持ち込むため、ここが踏み台になりやすい。
2. **既定の権限が読み取りだけか。** 既定のままだと `GITHUB_TOKEN` が書き込み可の
   リポジトリがあり、ワークフローを経由してリポジトリを書き換えられる。
3. **検査だけのワークフローが secrets を要求していないか。** 要求するとフォークからの
   PR で必ず落ち、貢献の入口が塞がる。
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
WF = ROOT / ".github" / "workflows"

# 検査だけを行う（=フォーク PR でも完走すべき）ワークフロー。
CHECK_ONLY = {"ci.yml", "no-real-ocid.yml"}


def _workflows() -> list[pathlib.Path]:
    fs = sorted(WF.glob("*.yml")) + sorted(WF.glob("*.yaml"))
    assert fs, "ワークフローが1つも見つからない（配置が変わった？）"
    return fs


@pytest.mark.parametrize("wf", _workflows(), ids=lambda p: p.name)
def test_actions_are_pinned_to_a_commit(wf: pathlib.Path):
    """`uses:` は 40 桁の commit SHA で固定する（タグは動く参照）。"""
    bad = []
    for m in re.finditer(r"^\s*(?:- )?uses:\s*(\S+)", wf.read_text(encoding="utf-8"), re.M):
        ref = m.group(1)
        if ref.startswith("./"):      # ローカルの composite action はリポジトリ内なので対象外
            continue
        if "@" not in ref:
            bad.append(ref)
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", ref.split("@", 1)[1]):
            bad.append(ref)
    assert not bad, f"SHA でピン留めされていない: {bad}"


@pytest.mark.parametrize("wf", _workflows(), ids=lambda p: p.name)
def test_workflow_declares_permissions(wf: pathlib.Path):
    """`permissions` を明示する（既定に委ねない）。"""
    assert re.search(r"^permissions:", wf.read_text(encoding="utf-8"), re.M), \
        f"{wf.name} が permissions を宣言していない"


@pytest.mark.parametrize("name", sorted(CHECK_ONLY))
def test_check_only_workflows_are_read_only(name: str):
    """検査だけのワークフローは `contents: read` に留める。"""
    src = (WF / name).read_text(encoding="utf-8")
    m = re.search(r"^permissions:\n((?:\s+\S+:\s*\S+\n)+)", src, re.M)
    assert m, f"{name} のトップレベル permissions を読めない"
    body = m.group(1)
    assert "contents: read" in body, f"{name} が contents: read でない"
    for w in ("write", "write-all"):
        assert w not in body, f"{name} が書き込み権限を持っている: {body.strip()}"


@pytest.mark.parametrize("name", sorted(CHECK_ONLY))
def test_check_only_workflows_do_not_need_secrets(name: str):
    """**フォークからの PR で落ちないこと。** secrets はフォークでは空になる。

    `GITHUB_TOKEN` だけは例外（フォークでも読み取り権限で供給される）。
    """
    src = (WF / name).read_text(encoding="utf-8")
    used = {m.group(1) for m in re.finditer(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", src)}
    assert used <= {"GITHUB_TOKEN"}, f"{name} がフォークで供給されない secrets を使う: {used}"


def test_pull_request_target_is_not_used():
    """`pull_request_target` を使わない。

    フォークの内容を**書き込み権限つきの文脈**で実行できてしまう入口で、
    OSS で最も事故が起きやすい。使うなら理由と防御を伴う設計レビューが要る。
    """
    for wf in _workflows():
        assert "pull_request_target" not in wf.read_text(encoding="utf-8"), \
            f"{wf.name} が pull_request_target を使っている"


def test_dist_is_not_tracked():
    """ビルド成果物を追跡しない。

    以前は `packages/web/dist` を追跡し「コミット済みと一致するか」を CI で見ていたが、
    **ビルド出力は環境で一致しない**（macOS と CI(Linux) でチャンクのハッシュが変わる）。
    ソースを1文字も違えていなくても落ちるため、外部の貢献者は詰む。
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "packages/web/dist"],
                         cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert not out, f"dist が追跡されている:\n{out[:300]}"


def test_dependabot_covers_actions():
    """SHA ピン留めは**更新が止まる**ので、Dependabot で追随させる。"""
    p = ROOT / ".github" / "dependabot.yml"
    assert p.exists(), "dependabot.yml が無い（ピン留めしたまま腐る）"
    src = p.read_text(encoding="utf-8")
    for eco in ("github-actions", "pip", "npm"):
        assert eco in src, f"dependabot が {eco} を見ていない"
