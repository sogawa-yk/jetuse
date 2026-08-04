#!/usr/bin/env python3
"""止まっている作業を洗い出す。

**なぜ要るか（実害）**: 2026-08-04 に、PORT-03 が「レビュー未完了」のまま **6 日間**
放置されていたことが分かった。しかも再レビューを回したら **マージ済みコードから blocker が
2 件**出た（他人の資産を消しうるもの）。**「完了していないループ」を取りこぼすと、
欠陥が本番に入ったままになる。**

ER が「思いついたことを取りこぼさない」ための仕組みなら、こちらは
**「やりかけを取りこぼさない」**ための仕組み。同じレポートに並べて出す。

判定に使うのは**リポジトリの実際の状態だけ**（人が更新する台帳を作らない。必ずずれるため）:

- worktree の `STATE.md` の `review_verdict`
- ブランチが `main` / `dev` に入っているか
- PR が出ているか
- 最後に動いた日からの経過

`ER-*.md` の `ticket:` に書かれたブランチは **意図的に止めているもの**として扱う
（例: 認証待ちの OpenAPI）。放置と区別しないと、毎回同じものが出て無視されるようになる。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOOPS = ROOT.parent / f"{ROOT.name}-loops"
STALE_DAYS = 2  # これ以上動いていなければ「放置」として目立たせる


def _git(*args: str, cwd: pathlib.Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd or ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _intentional() -> dict[str, str]:
    """ER が参照しているブランチ＝意図的に止めているもの。"""
    out: dict[str, str] = {}
    d = ROOT / "docs" / "enhance"
    for p in d.glob("ER-*.md") if d.exists() else []:
        text = p.read_text(encoding="utf-8")
        er = re.search(r"^id:\s*(\S+)", text, re.M)
        for b in re.findall(r"`(feat/[\w.-]+|fix/[\w.-]+|chore/[\w.-]+)`", text):
            if er:
                out[b] = er.group(1)
    return out


def _open_prs() -> dict[str, int]:
    r = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "number,headRefName"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    try:
        return {x["headRefName"]: x["number"] for x in json.loads(r.stdout)}
    except Exception:
        return {}


def scan() -> list[dict]:
    prs = _open_prs()
    intent = _intentional()
    merged_main = set(_git("branch", "-r", "--merged", "origin/main").split())
    merged_dev = set(_git("branch", "-r", "--merged", "origin/dev").split())
    today = datetime.date.today()

    items: list[dict] = []
    if not LOOPS.exists():
        return items
    for wt in sorted(LOOPS.iterdir()):
        state = wt / "STATE.md"
        if not (wt / ".git").exists() and not state.exists():
            continue
        text = state.read_text(encoding="utf-8", errors="ignore") if state.exists() else ""
        verdict = ""
        m = re.search(r"review_verdict:\s*\**`?(\w+)", text)
        if m:
            verdict = m.group(1)
        branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt)
        dirty = bool(_git("status", "--porcelain", cwd=wt).strip())
        # 最後に動いた日 = worktree 内で一番新しい変更（.git と証跡は除く）
        newest = 0.0
        for p in wt.rglob("*"):
            if p.is_file() and not any(x in p.parts for x in (".git", ".venv", "runs", "node_modules")):
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    pass
        days = (today - datetime.date.fromtimestamp(newest)).days if newest else None

        # **「main と違うか」ではなく「main に入っていないコミットがあるか」で見る。**
        # 古い worktree は main が先へ進んだ分だけ差分が出るが、それは未出荷ではない。
        ahead = [x for x in _git("log", "--oneline", "origin/main..HEAD", cwd=wt).splitlines() if x]
        # 未コミットの**コード**変更（STATE.md や証跡は数えない）
        code_dirty = bool(_git("status", "--porcelain", "--",
                               "packages", "infra", "ops", cwd=wt).strip())
        merged = (f"origin/{branch}" in merged_main) or (f"origin/{branch}" in merged_dev)
        if not merged and not ahead and not code_dirty:
            merged = True  # ブランチ名が違っても、出すべきものは残っていない
        # **意図的に止めているものを最初に見る。** ER に理由が書いてある＝把握済みなので、
        # 「未出荷」として毎回出すと、本当に見るべきものが埋もれる
        if branch in intent:
            status, why = "parked", f"{intent[branch]} で意図的に止めている"
        elif branch in prs:
            status, why = "pr", f"PR #{prs[branch]} が出ている"
        elif merged:
            status, why = "clean", "出すべきものは残っていない（worktree は撤去してよい）"
        elif verdict and verdict.upper() != "PASS":
            status, why = "review", f"レビュー未完了（{verdict}）"
        elif verdict.upper() == "PASS":
            n = len(ahead) + (1 if code_dirty else 0)
            status = "unshipped"
            why = f"レビューは通ったが PR が出ていない（未出荷 {n} 件）"
        else:
            status, why = "unknown", "状態が読めない"

        items.append({"name": wt.name, "branch": branch, "status": status,
                      "why": why, "days": days, "dirty": code_dirty})

    rank = {"review": 0, "unshipped": 1, "unknown": 2, "pr": 3, "parked": 4, "clean": 5}
    items.sort(key=lambda x: (rank.get(x["status"], 9), -(x["days"] or 0)))
    return items


def needs_attention(items: list[dict]) -> list[dict]:
    """**放っておくと欠陥が本番に残る**ものだけを返す。"""
    return [i for i in items if i["status"] in ("review", "unshipped", "unknown")]


if __name__ == "__main__":
    for i in scan():
        d = f"{i['days']}日前" if i["days"] is not None else "不明"
        print(f"{i['name']:<14} {i['status']:<10} {d:<7} {i['why']}")
