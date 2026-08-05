"""`ops/stalled.py` が**安定枝への未リリース分**を検出することを固定する。

**なぜテストが要るか（実害）**: 2026-08-06、`public-dev` が `main` より 16 commit 先行し、
`internal-dev` も `internal-stable` より 10 commit 先行していたが、
「やりかけを取りこぼさない」ためのはずの一覧に**一行も出なかった**。
worktree 走査は「4長期ブランチのどれかに入っていれば取り込み済み」と見なすので、
開発枝へ merge した瞬間に消える。**そこから安定枝へ出す作業は誰の worktree にも現れない。**

実リポジトリの状態に依存しないよう、使い捨ての git リポジトリを作って検査する。
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]


def _load(root: pathlib.Path):
    """`ops/stalled.py` を、ROOT を差し替えた状態で読み込む。"""
    spec = importlib.util.spec_from_file_location("stalled_mod", REPO / "ops" / "stalled.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    mod.LOOPS = root.parent / f"{root.name}-loops"  # 実在しない＝worktree 由来の項目は出ない
    return mod


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """`origin/*` の4長期ブランチを持つ使い捨てリポジトリ。

    remote を立てずに `refs/remotes/origin/<b>` を直接作る。`_git` は
    `origin/<b>` を rev-parse するだけなので、これで十分かつ速い。
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("1")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    for b in ("main", "public-dev", "internal-dev", "internal-stable"):
        _git(root, "update-ref", f"refs/remotes/origin/{b}", "HEAD")
    return root


def _advance(root: pathlib.Path, branch: str, n: int, days_ago: int | None = None) -> None:
    """`origin/<branch>` を n commit だけ進める。`days_ago` で commit 日を過去にできる。"""
    _git(root, "checkout", "-q", "-B", f"tmp-{branch}", f"origin/{branch}")
    env = None
    if days_ago is not None:
        when = (datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    for i in range(n):
        p = root / f"{branch}-{days_ago}-{i}.txt"
        p.write_text(str(i))
        _git(root, "add", "-A")
        subprocess.run(["git", "commit", "-qm", f"{branch} {i}"], cwd=root, check=True,
                       capture_output=True, text=True, env=env)
    _git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")


def test_no_lag_reports_nothing(repo):
    """安定枝と開発枝が揃っていれば何も出ない（毎回出ると無視されるようになる）。"""
    assert _load(repo)._release_lag([]) == []


def test_public_lag_is_reported(repo):
    _advance(repo, "public-dev", 3)
    items = _load(repo)._release_lag([])
    assert len(items) == 1
    it = items[0]
    assert it["status"] == "unreleased"
    assert it["name"] == "public-dev → main"
    assert "3 commit" in it["why"]
    # main への merge は公開配信物を差し替える。その注意を落とさない。
    assert "公開配信物" in it["why"]


def test_internal_lag_is_reported_without_public_warning(repo):
    _advance(repo, "internal-dev", 2)
    items = _load(repo)._release_lag([])
    assert [i["name"] for i in items] == ["internal-dev → internal-stable"]
    assert "公開配信物" not in items[0]["why"]


def test_both_pairs_reported(repo):
    _advance(repo, "public-dev", 1)
    _advance(repo, "internal-dev", 1)
    assert len(_load(repo)._release_lag([])) == 2


def test_open_release_pr_downgrades_to_pr(repo):
    """release PR が出ているなら「要注意」ではない。出しっぱなしを催促しても仕方ない。"""
    _advance(repo, "public-dev", 4)
    items = _load(repo)._release_lag(
        [{"number": 42, "headRefName": "public-dev", "baseRefName": "main"}])
    assert items[0]["status"] == "pr"
    assert "#42" in items[0]["why"]


def test_days_counts_from_the_oldest_unreleased_commit(repo):
    """**待たせている長さ**を出す。直近 commit を見ると溜めるほど新しく見えて遅れが隠れる。

    commit 日を作り分けて **oldest と latest を区別できる**ようにする。同日に積むと
    実装が最新 commit を選んでいても期待値 0 で通ってしまい、仕様を固定できない。
    """
    _advance(repo, "public-dev", 1, days_ago=30)   # 30日前に溜め始め
    _advance(repo, "public-dev", 1, days_ago=1)    # 昨日も足した
    it = _load(repo)._release_lag([])[0]
    # commit 日は UTC、`today` はローカル日付なので境界で ±1 ずれる。
    # 固定したいのは「30 と 1 のどちらを見ているか」なので、そこだけを見る。
    assert abs(it["days"] - 30) <= 1, f"最新 commit(1日前)を見ている: days={it['days']}"


def test_missing_branch_is_surfaced_not_dropped(repo):
    """安定枝が無い環境で落ちない。ただし**黙って消さない**。

    single-branch clone や fetch refspec の絞り込みでは `git fetch` しても ref が増えず、
    黙ってスキップすると未リリースが恒久的に一覧から消える＝この検出が直したかった漏れそのもの。
    """
    _git(repo, "update-ref", "-d", "refs/remotes/origin/internal-stable")
    _advance(repo, "internal-dev", 1)
    items = _load(repo)._release_lag([])
    assert [i["status"] for i in items] == ["unknown"]
    assert "origin/internal-stable" in items[0]["why"]
    assert items[0]["days"] is None


def test_scan_includes_release_lag_without_worktrees(repo):
    """worktree ディレクトリが無くても未リリース分は返る（早期 return に潰されない）。"""
    _advance(repo, "public-dev", 1)
    mod = _load(repo)
    mod._open_pr_records = lambda: []
    mod._fetch_origin = lambda: True
    assert not mod.LOOPS.exists()
    assert [i["status"] for i in mod.scan()] == ["unreleased"]


def test_needs_attention_includes_unreleased(repo):
    """レポートの「要注意」側に入る。畳まれると気づけない。"""
    mod = _load(repo)
    items = [{"name": "x", "status": "unreleased", "why": "", "days": 1}]
    assert mod.needs_attention(items) == items


def test_pr_to_a_different_base_is_not_a_release_pr(repo):
    """`public-dev` を head とする**別 base** の PR を release PR と誤認しない。

    実例: #155 は `public-dev` を運ぶ `internal-dev` 宛の同期 PR だった。head だけで
    判定すると、これがあるだけで `main` への未リリースが要注意一覧から消える。
    """
    _advance(repo, "public-dev", 5)
    prs = [{"number": 155, "headRefName": "public-dev", "baseRefName": "internal-dev"}]
    items = _load(repo)._release_lag(prs)
    assert items[0]["status"] == "unreleased", "別 base の PR で未リリースが隠れた"


def test_unavailable_github_is_not_reported_as_no_pr(repo):
    """`gh` を確認できなかった状態を「PR が無い」と断定しない。"""
    _advance(repo, "public-dev", 2)
    it = _load(repo)._release_lag(None)[0]
    assert it["status"] == "unknown"
    assert "確認できなかった" in it["why"]


def test_stale_refs_are_flagged(repo):
    """fetch できていないなら、その旨を文面に出す（古い判定を黙って見せない）。"""
    _advance(repo, "public-dev", 1)
    it = _load(repo)._release_lag([], fetched=False)[0]
    assert "古い可能性がある" in it["why"]
    assert _load(repo)._release_lag([], fetched=True)[0]["why"].endswith("差し替わる）")


def test_open_pr_records_asks_for_base_and_a_high_limit(repo, monkeypatch):
    """`gh pr list` の引数を固定する。base 無し/既定 30 件だと誤判定に戻る。"""
    import subprocess as sp
    mod = _load(repo)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._open_pr_records() == []
    assert "baseRefName" in " ".join(seen["cmd"])
    limit = int(seen["cmd"][seen["cmd"].index("--limit") + 1])
    assert limit >= 100, f"--limit {limit} では open PR を取りこぼす"


def test_open_pr_records_returns_none_on_failure(repo, monkeypatch):
    import subprocess as sp
    mod = _load(repo)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    assert mod._open_pr_records() is None


@pytest.mark.parametrize("boom", [
    pytest.param(FileNotFoundError("gh"), id="not-installed"),
    pytest.param(subprocess.TimeoutExpired("gh", 60), id="timeout"),
])
def test_gh_exceptions_do_not_escape(repo, boom, monkeypatch):
    """`gh` が無い／応答しない環境で**レポート生成ごと落ちない**。

    例外が伝播すると `ops/er.py report` が失敗し、一覧が一切出なくなる。
    オフライン実行は普通に起こるので、`None`（＝確認できなかった）へ落とす。
    """
    mod = _load(repo)

    def raiser(*a, **kw):
        raise boom

    monkeypatch.setattr(mod.subprocess, "run", raiser)
    assert mod._open_pr_records() is None


@pytest.mark.parametrize("boom", [
    pytest.param(FileNotFoundError("git"), id="not-installed"),
    pytest.param(subprocess.TimeoutExpired("git", 60), id="timeout"),
])
def test_fetch_exceptions_do_not_escape(repo, boom, monkeypatch):
    """`git fetch` が落ちても「古いかもしれない」に落とすだけで、例外は出さない。"""
    mod = _load(repo)

    def raiser(*a, **kw):
        raise boom

    monkeypatch.setattr(mod.subprocess, "run", raiser)
    assert mod._fetch_origin() is False


def test_hitting_the_pr_limit_is_treated_as_unknown(repo, monkeypatch):
    """上限に達したら「PR は無い」と言えない。取りこぼした可能性があるため。"""
    import subprocess as sp
    mod = _load(repo)
    # **形は正しくしておく。** 鍵が欠けたレコードだと形の検証で先に None になり、
    # 「上限に達したから None」を検査したつもりが素通りする（review-12 minor）。
    full = json.dumps([{"number": i, "headRefName": f"b{i}", "baseRefName": "main",
                        "isCrossRepository": False} for i in range(200)])
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=full, stderr=""))
    assert mod._open_pr_records() is None


def test_offline_scan_still_reports_lag(repo):
    """fetch も gh も落ちている状態で、未リリースが `unknown` として残る（消えない）。"""
    _advance(repo, "public-dev", 3)
    mod = _load(repo)
    mod._fetch_origin = lambda: False
    mod._open_pr_records = lambda: None
    it = mod.scan()[0]
    assert it["status"] == "unknown"
    assert "古い可能性がある" in it["why"]


def test_no_lag_while_offline_is_not_reported_as_no_lag(repo):
    """**「差は無い」と言えるのは取り込めたときだけ。**

    前回 fetch 時点で stable/dev が同一 → その後 remote の dev だけ進む → 今回オフライン、
    という並びで、古い ref の 0 件を黙って信じると未リリースが**行ごと消える**。
    """
    items = _load(repo)._release_lag([], fetched=False)
    assert [i["status"] for i in items] == ["unknown", "unknown"]
    assert "無いとは言えない" in items[0]["why"]


def test_no_lag_when_fetched_stays_silent(repo):
    """取り込めていて本当に差が無いなら黙る（毎回出ると無視されるようになる）。"""
    assert _load(repo)._release_lag([], fetched=True) == []


def test_shallow_clone_is_not_trusted(repo):
    """履歴が浅ければ `rev-list --count` を完全な履歴として扱わない。"""
    _advance(repo, "public-dev", 3)
    mod = _load(repo)
    real = mod._git_out

    def fake(*args, **kw):
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return "true"
        return real(*args, **kw)

    mod._git_out = fake
    items = mod._release_lag([])
    assert {i["status"] for i in items} == {"unknown"}
    assert "shallow" in items[0]["why"]


def test_fetch_uses_explicit_refspecs(repo, monkeypatch):
    """**明示 refspec で取り込む。**

    引数なしの `git fetch origin` は `remote.origin.fetch` に従うため、refspec から外れた
    長期ブランチが更新されないまま成功する。古い ref を最新と信じて判定してしまう。
    """
    import subprocess as sp
    mod = _load(repo)
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    assert mod._fetch_origin() is True
    joined = " ".join(seen["cmd"])
    for b in ("main", "public-dev", "internal-dev", "internal-stable"):
        assert f"+refs/heads/{b}:refs/remotes/origin/{b}" in joined, f"{b} の refspec が無い"


def test_rev_list_failure_is_not_counted_as_zero(repo):
    """数えられなかったことを 0（＝遅れ無し）に潰さない。"""
    mod = _load(repo)
    real = mod._git_out

    def fake(*args, **kw):
        if args[:1] == ("rev-list",):
            return None
        return real(*args, **kw)

    mod._git_out = fake
    items = mod._release_lag([])
    assert {i["status"] for i in items} == {"unknown"}
    assert "数えられない" in items[0]["why"]


def test_git_unavailable_reports_the_real_reason(repo):
    """git を起動できないときに「ref が手元に無い」と誤った理由を出さない。"""
    mod = _load(repo)
    mod._git_out = lambda *a, **kw: None
    items = mod._release_lag([])
    assert [i["status"] for i in items] == ["unknown", "unknown"]
    assert "`git` を実行できず" in items[0]["why"]


def test_git_out_distinguishes_failure_from_empty_output(repo):
    """失敗は `None`、成功して空なら `""`。潰すと 0 件と失敗が同じになる。"""
    mod = _load(repo)
    assert mod._git_out("rev-list", "--count", "origin/main..origin/public-dev") == "0"
    assert mod._git_out("rev-parse", "--verify", "--quiet", "origin/nonexistent") is None
    assert mod._git("rev-parse", "--verify", "--quiet", "origin/nonexistent") == ""


def test_fork_pr_is_not_mistaken_for_a_release_pr(repo):
    """fork 側の同名ブランチからの PR を自リポジトリの release PR と取り違えない。"""
    _advance(repo, "public-dev", 3)
    fork = [{"number": 7, "headRefName": "public-dev", "baseRefName": "main",
             "isCrossRepository": True}]
    assert _load(repo)._release_lag(fork)[0]["status"] == "unreleased"
    own = [{"number": 8, "headRefName": "public-dev", "baseRefName": "main",
            "isCrossRepository": False}]
    assert _load(repo)._release_lag(own)[0]["status"] == "pr"


@pytest.mark.parametrize("payload", [
    pytest.param("null", id="null"),
    pytest.param('{"number": 1}', id="object-not-list"),
    pytest.param('[1, 2, 3]', id="list-of-scalars"),
])
def test_malformed_gh_json_is_treated_as_unavailable(repo, monkeypatch, payload):
    """`gh` が想定外の JSON を返しても例外を投げず `None`（＝確認できなかった）にする。"""
    import subprocess as sp
    mod = _load(repo)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert mod._open_pr_records() is None


def test_open_pr_records_requests_cross_repository_flag(repo, monkeypatch):
    """`isCrossRepository` を要求する。落とすと fork の PR を見分けられない。"""
    import subprocess as sp
    mod = _load(repo)
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    mod._open_pr_records()
    assert "isCrossRepository" in " ".join(seen["cmd"])


@pytest.mark.parametrize("payload", [
    pytest.param('[{}]', id="empty-record"),
    pytest.param('[{"headRefName": "public-dev"}]', id="missing-number"),
    pytest.param('[{"number": 1, "headRefName": "public-dev"}]', id="missing-base"),
])
def test_records_missing_keys_are_treated_as_unavailable(repo, monkeypatch, payload):
    """鍵が欠けたレコードを正常値として返さない（`scan()` が KeyError で落ちる）。"""
    import subprocess as sp
    mod = _load(repo)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert mod._open_pr_records() is None


def test_by_head_skips_incomplete_records(repo):
    """呼び出し側も鍵の欠落で落ちない（防御の二段目）。"""
    mod = _load(repo)
    assert mod._by_head([{"number": 1, "headRefName": "a"}, {}, {"headRefName": "b"}]) == {"a": 1}
    assert mod._by_head(None) == {}


def test_missing_cross_repository_flag_is_treated_as_unavailable(repo, monkeypatch):
    """`isCrossRepository` が欠けたら受け取らない。

    欠けたまま通すと `not p.get("isCrossRepository")` が True になり、
    **fork の PR を自リポジトリの release PR と誤認**して未リリースが隠れる。
    """
    import subprocess as sp
    mod = _load(repo)
    payload = '[{"number": 1, "headRefName": "public-dev", "baseRefName": "main"}]'
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert mod._open_pr_records() is None


@pytest.mark.parametrize("payload", [
    pytest.param('[{"number": 1, "headRefName": [], "baseRefName": "main",'
                 ' "isCrossRepository": false}]', id="head-is-list"),
    pytest.param('[{"number": 1, "headRefName": "public-dev", "baseRefName": "main",'
                 ' "isCrossRepository": "false"}]', id="flag-is-string"),
    pytest.param('[{"number": "1", "headRefName": "public-dev", "baseRefName": "main",'
                 ' "isCrossRepository": false}]', id="number-is-string"),
    pytest.param('[{"number": true, "headRefName": "public-dev", "baseRefName": "main",'
                 ' "isCrossRepository": false}]', id="number-is-bool"),
])
def test_wrong_types_are_treated_as_unavailable(repo, monkeypatch, payload):
    """存在だけでなく**型**まで見る。

    `headRefName: []` は list を辞書キーにして `TypeError`、
    `isCrossRepository: "false"` は真値として扱われ fork 判定が反転する。
    """
    import subprocess as sp
    mod = _load(repo)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert mod._open_pr_records() is None


def test_well_formed_record_is_accepted(repo, monkeypatch):
    """正しい形は通す（厳しくしすぎて常に None にしていないことを固定する）。"""
    import subprocess as sp
    mod = _load(repo)
    payload = ('[{"number": 9, "headRefName": "public-dev", "baseRefName": "main",'
               ' "isCrossRepository": false}]')
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=payload, stderr=""))
    assert mod._open_pr_records() == [{"number": 9, "headRefName": "public-dev",
                                       "baseRefName": "main", "isCrossRepository": False}]


def test_just_below_the_pr_limit_is_accepted(repo, monkeypatch):
    """上限未満なら通す。`None` の理由が**件数上限**であることを示す対の検査。"""
    import subprocess as sp
    mod = _load(repo)
    recs = [{"number": i, "headRefName": f"b{i}", "baseRefName": "main",
             "isCrossRepository": False} for i in range(199)]
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout=json.dumps(recs),
                                                              stderr=""))
    assert mod._open_pr_records() == recs
