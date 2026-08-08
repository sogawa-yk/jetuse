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
- ブランチが 4 長期ブランチ（`main` / `public-dev` / `internal-dev` / `internal-stable`）に入っているか
- PR が出ているか
- 最後に動いた日からの経過
- **安定枝への未リリース分**（`public-dev`→`main` / `internal-dev`→`internal-stable`）

最後の1つは worktree 走査では拾えない。開発枝へ merge した時点で「取り込み済み」になり、
そこから安定枝へ出す作業は**誰の worktree にも現れない**ため（`_release_lag` のコメント参照）。

`ER-*.md` の `ticket:` に書かれたブランチは **意図的に止めているもの**として扱う
（例: 認証待ちの OpenAPI）。放置と区別しないと、毎回同じものが出て無視されるようになる。
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOOPS = ROOT.parent / f"{ROOT.name}-loops"
STALE_DAYS = 2  # これ以上動いていなければ「放置」として目立たせる


def _git_out(*args: str, cwd: pathlib.Path | None = None) -> str | None:
    """git の出力。**失敗は `None`**（成功して出力が空、の `""` と区別する）。

    `_git()` は失敗を `""` に潰すので、`rev-list --count` が壊れたリポジトリで失敗しても
    `int("" or 0)` = 0 になり「差分なし」として未リリース行が消える（review-5 major）。
    数える系の呼び出しはこちらを使う。git を起動できない場合の `OSError` もここで受ける。
    """
    try:
        r = subprocess.run(["git", *args], cwd=cwd or ROOT,
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _git(*args: str, cwd: pathlib.Path | None = None) -> str:
    out = _git_out(*args, cwd=cwd)
    return "" if out is None else out


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


def _open_pr_records() -> list[dict] | None:
    """open PR を head/base 付きで返す。**取得できなければ `None`**（空 dict と区別する）。

    `{}` を返すと「PR は無い」と読めてしまい、`gh` が落ちているだけの状態を
    「未リリース」「未出荷」と断定してしまう。区別できないと利用者は判断できない。
    `--limit` を指定しないと既定 30 件で頭打ちになり、release PR が結果から抜けて
    「PR が出ていない」と誤表示する（review-1 minor）。**上限に達したら `None`**
    ＝取りこぼした可能性がある以上「PR は無い」と言えない（review-2 minor）。

    `gh` が無い環境では `FileNotFoundError`、応答しなければ `TimeoutExpired` が飛ぶ。
    どちらも**呼び出し元まで伝播すればレポート生成ごと落ちる**（review-2 major）。
    """
    limit = 200
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", str(limit),
             "--json", "number,headRefName,baseRefName,isCrossRepository"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        recs = json.loads(r.stdout)
    except Exception:
        return None
    # **形まで確かめる。** `null` や object が返ると `len()` / 内包表記で例外になり、
    # 「取得不能は None」という約束を越えて呼び出し元まで飛ぶ（review-7 minor）。
    # 要る鍵まで揃っているか見る。`[{}]` を通すと `scan()` の内包表記が `KeyError` で落ち、
    # 「取得不能は None」の約束を越えて呼び出し元まで飛ぶ（review-8 major）。
    # `isCrossRepository` も必須。欠けると `not p.get(...)` が True になり、
    # **fork の PR を自リポジトリの release PR と誤認する**（review-9 major）。
    # **存在だけでなく型まで見る。** `headRefName: []` は list を辞書キーにして `TypeError`、
    # `isCrossRepository: "false"` は真値として扱われ fork 判定が反転する（review-10 major）。
    def _ok(x: object) -> bool:
        if not isinstance(x, dict):
            return False
        # `True` は `int` の実体でもあるので、number に bool が来たら弾く。
        if not isinstance(x.get("number"), int) or isinstance(x.get("number"), bool):
            return False
        if not all(isinstance(x.get(k), str) for k in ("headRefName", "baseRefName")):
            return False
        return isinstance(x.get("isCrossRepository"), bool)

    if not isinstance(recs, list) or not all(_ok(x) for x in recs):
        return None
    return None if len(recs) >= limit else recs


def _by_head(recs: list[dict] | None) -> dict[str, int]:
    """head ブランチ → PR 番号。鍵が欠けたレコードは黙って飛ばす（例外にしない）。"""
    return {x["headRefName"]: x["number"] for x in recs or []
            if "headRefName" in x and "number" in x}


def _open_prs() -> dict[str, int]:
    """head ブランチ → PR 番号。取得できなければ空（worktree 側の従来の扱いを保つ）。"""
    recs = _open_pr_records()
    return _by_head(recs)


# 4ブランチ体制(ADR-0028)の長期ブランチ。取り込み判定の基準はこれだけ。
LONG_BRANCHES = ("main", "public-dev", "internal-dev", "internal-stable")


# 返しうる状態と表示順。**module 定数にしておく。** レポート側(`ops/er.py` の `STALL_JA`)が
# すべてに日本語表示を持つことをテストで照合できる＝状態を足したとき生の英単語が表に出ない。
RANK = {"review": 0, "unreleased": 1, "unshipped": 2, "unknown": 3,
        "pr": 4, "parked": 5, "clean": 6}


# 安定枝 ← 開発枝。release PR を出すまで、ここに溜まった分は誰にも届いていない。
RELEASE_PAIRS = (("main", "public-dev"), ("internal-stable", "internal-dev"))


def _fetch_origin() -> bool:
    """判定前に `origin` を取り込む。成否を返す。

    判定材料はローカルの `origin/*` remote-tracking ref だけなので、**古いと嘘をつく**。
    remote で release 済みなのに「未リリース」と出したり、逆に新しい未リリース commit を
    見落としたりする（review-1 major）。失敗しても止めず、**古い可能性を利用者に見せる**。

    60 秒を超えれば `TimeoutExpired`、git を起動できなければ `OSError` が飛ぶ。
    これを捕まえないと「失敗しても止めない」という約束が守られず、**レポート生成ごと落ちる**
    （review-2 major）。オフラインでの実行は普通に起こる。

    **明示 refspec で取り込む。** 引数なしの `git fetch origin` は `remote.origin.fetch` に
    従うため、refspec から外れた長期ブランチが**更新されないまま成功する**。除外前に作られた
    古い `origin/<branch>` が残っていれば、それを最新と信じて判定してしまう（review-5 blocker）。
    """
    refs = sorted({b for pair in RELEASE_PAIRS for b in pair})
    spec = [f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in refs]
    try:
        r = subprocess.run(["git", "fetch", "--quiet", "--no-tags", "origin", *spec],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _release_lag(prs: list[dict] | None, *, fetched: bool = True) -> list[dict]:
    """**長期ブランチのリリース遅れ**を返す。

    worktree 走査では見つからない。開発枝へ merge した時点で「取り込み済み」になり、
    そこから先（安定枝へ出す）が誰の worktree にも現れないため。
    実害: 2026-08-06、`public-dev` が `main` より 16 commit 先行していたが、
    「やりかけを取りこぼさない」はずの一覧に**一行も出なかった**。

    `prs` は open PR のレコード列。**`None` は「GitHub を確認できなかった」**で、
    「PR が無い」（空リスト）とは区別する。
    """
    today = datetime.date.today()
    stale = "" if fetched else "（**origin を取得できておらず、古い可能性がある**）"
    # 履歴が浅いと `rev-list --count` も `log --reverse` も**完全な履歴ではない**（review-4 major）。
    shallow_raw = _git_out("rev-parse", "--is-shallow-repository")
    out: list[dict] = []

    def _cannot_tell(stable: str, dev: str, why: str) -> dict:
        return {"name": f"{dev} → {stable}", "branch": dev, "status": "unknown",
                "why": why, "days": None, "dirty": False}

    if shallow_raw is None:
        # git 自体を実行できない。ここで返さないと、以降の `_git` が全部 "" になり
        # 「ref が手元に無い」という**誤った理由**で unknown を出す（review-5 major）。
        return [_cannot_tell(s, d, "`git` を実行できず、リリース遅れを判定できない")
                for s, d in RELEASE_PAIRS]
    shallow = shallow_raw == "true"

    for stable, dev in RELEASE_PAIRS:
        missing = [b for b in (stable, dev)
                   if not _git("rev-parse", "--verify", "--quiet", f"origin/{b}")]
        if missing:
            # **黙って飛ばさない。** single-branch clone や fetch refspec の絞り込みでは
            # `git fetch` しても ref が増えず、未リリースが恒久的に一覧から消える
            # ＝この ER が直したかった漏れそのもの（review-3 major）。
            out.append(_cannot_tell(
                stable, dev,
                f"`{'` / `'.join('origin/' + b for b in missing)}` が手元に無く、"
                "リリース遅れを判定できない"
                "（single-branch clone か fetch refspec の絞り込みを疑う）"))
            continue
        if shallow:
            out.append(_cannot_tell(
                stable, dev,
                f"履歴が浅く（shallow clone）、`{stable}`↔`{dev}` の差を数えられない。"
                "`git fetch --unshallow` してから確認する"))
            continue
        rng = f"origin/{stable}..origin/{dev}"
        raw = _git_out("rev-list", "--count", rng)
        if raw is None or not raw.isdigit():
            # 数えられなかったことを 0（＝遅れ無し）に潰さない（review-5 major）。
            out.append(_cannot_tell(
                stable, dev, f"`git rev-list --count {rng}` が失敗し、差を数えられない"))
            continue
        n = int(raw)
        if n == 0:
            # **「0 件」と言えるのは取り込めたときだけ。** 古い ref のまま 0 を見て黙ると、
            # remote で進んだ未リリース分が**行ごと消える**（review-4 blocker）。
            if not fetched:
                out.append(_cannot_tell(
                    stable, dev,
                    f"手元の `origin/{stable}`↔`origin/{dev}` に差は無いが、"
                    "**origin を取得できておらず、リリース遅れが無いとは言えない**"))
            continue
        # **待たせている長さ = 一番古い未リリース commit からの日数。** 直近の commit を見ると、
        # 溜め続けているほど「昨日動いた」に見えてしまい、遅れが隠れる。
        oldest = _git("log", "--format=%cI", "--reverse", rng).splitlines()
        days = None
        if oldest:
            with contextlib.suppress(ValueError):
                days = (today - datetime.datetime.fromisoformat(oldest[0]).date()).days
        # **head だけでなく base も一致を見る。** `public-dev` を head とする別 base の PR
        # （例: internal-dev 宛の同期 PR）を release PR と誤認すると、要注意一覧から
        # 未リリースが消える（review-1 major）。実際 #155 がその形だった。
        # fork 側に同名の `public-dev` があって stable 宛の PR が開かれていると、
        # 自リポジトリの release PR と取り違える。`isCrossRepository` で弾く（review-7 major）。
        rel = next((p for p in prs or []
                    if p.get("headRefName") == dev and p.get("baseRefName") == stable
                    and not p.get("isCrossRepository")), None)
        if prs is None:
            status = "unknown"
            why = f"`{dev}` が `{stable}` より {n} commit 先行。PR の有無を確認できなかった{stale}"
        elif rel:
            status, why = "pr", f"release PR #{rel['number']} が出ている（{n} commit）{stale}"
        else:
            status = "unreleased"
            why = (f"`{dev}` が `{stable}` より {n} commit 先行。release PR が出ていない"
                   + ("（merge すると公開配信物が差し替わる）" if stable == "main" else "") + stale)
        out.append({"name": f"{dev} → {stable}", "branch": dev, "status": status,
                    "why": why, "days": days, "dirty": False})
    return out


def scan() -> list[dict]:
    # **判定材料はローカルの origin/* だけ。** 先に取り込まないと古い状態で断定する。
    fetched = _fetch_origin()
    pr_records = _open_pr_records()
    prs = _by_head(pr_records)
    intent = _intentional()
    # 4ブランチ体制(ADR-0028)。どれかに入っていれば「取り込み済み」とみなす。
    # main / internal-stable も見るのは、release 枝へ hotfix が直接入る経路があるため。
    # 手元に無い参照は黙って飛ばす（浅い clone や移行途中で欠けうる）。
    existing_refs = {
        b for b in (f"origin/{x}" for x in LONG_BRANCHES)
        if _git("rev-parse", "--verify", "--quiet", b)
    }
    merged_long: set[str] = set()
    for _ref in existing_refs:
        merged_long |= set(_git("branch", "-r", "--merged", _ref).split())
    today = datetime.date.today()

    items: list[dict] = _release_lag(pr_records, fetched=fetched)
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

        # **「長期ブランチと違うか」ではなく「どこにも入っていないコミットがあるか」で見る。**
        # 古い worktree は長期ブランチが先へ進んだ分だけ差分が出るが、それは未出荷ではない。
        # 4ブランチ体制(ADR-0028)では main だけを基準にすると、public-dev / internal-dev へ
        # 入れただけの作業が「未出荷」に見え続ける（main はリリース時にしか進まないため）。
        # **存在する参照だけを渡す。** 1つでも欠けると git log 全体が失敗し、_git は失敗時に
        # 空文字を返すので ahead=[] となり、未出荷の作業が「取り込み済み」として消える。
        # 浅い clone・single-branch fetch・移行途中で実際に起こりうる（review-11）。
        _not = [a for b in LONG_BRANCHES if f"origin/{b}" in existing_refs
                for a in ("--not", f"origin/{b}")]
        ahead = [x for x in _git("log", "--oneline", "HEAD", *_not, cwd=wt).splitlines() if x]
        # 未コミットの**コード**変更（STATE.md や証跡は数えない）
        code_dirty = bool(_git("status", "--porcelain", "--",
                               "packages", "infra", "ops", cwd=wt).strip())
        merged = f"origin/{branch}" in merged_long
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

    items.sort(key=lambda x: (RANK.get(x["status"], 9), -(x["days"] or 0)))
    return items


def needs_attention(items: list[dict]) -> list[dict]:
    """**放っておくと欠陥が本番に残る**ものだけを返す。"""
    return [i for i in items
            if i["status"] in ("review", "unreleased", "unshipped", "unknown")]


if __name__ == "__main__":
    for i in scan():
        d = f"{i['days']}日前" if i["days"] is not None else "不明"
        print(f"{i['name']:<14} {i['status']:<10} {d:<7} {i['why']}")
