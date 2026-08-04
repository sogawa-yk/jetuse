"""`ops/check-no-real-ocid.sh`（実 OCID の混入検査）の分岐を固定する。

**なぜテストが要るか**: このリポジトリは public で、この検査が実 OCID の新規混入を止める
最後の関門になる。**誤って「検出なし」を返す壊れ方が一番まずい**（静かに無効化される）。
実際その壊れ方を2回作った:

  - `git grep` のオプションをパターンの後ろに置き、git が revision と解釈して失敗
    → `|| true` で握り潰され、検出なしで素通りしていた
  - allowlist 突合を `$(grep ... || cat ...)` で書き、全件が受容済みのとき grep が
    exit 1 を返して `cat` にフォールバックし、**全件を新規混入として報告**していた

一時 git リポジトリで実際にシェルを走らせて振る舞いを固定する。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "check-no-real-ocid.sh"

BODY = "a" * 45  # 完全長 OCID の本体（40 文字以上あれば実値とみなす）


def _git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run(cwd: pathlib.Path, *args: str):
    return subprocess.run(
        ["bash", "ops/check-no-real-ocid.sh", *args],
        cwd=cwd, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(cwd)},
    )


def _repo(tmp_path: pathlib.Path, allow: str | None = None) -> pathlib.Path:
    r = tmp_path / "r"
    (r / "ops").mkdir(parents=True)
    _git(r.parent, "init", "-q", "-b", "main", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    shutil.copy(SCRIPT, r / "ops" / "check-no-real-ocid.sh")
    (r / "ops" / "allowed-public-ocids.txt").write_text(
        "# 受容済み\n" + (allow or "") + "\n")
    (r / "clean.md").write_text("ocid1.tenancy.oc1..MASKED\n")  # マスク済みは無視される
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def test_masked_only_passes(tmp_path):
    """マスク済みしか無ければ通す。"""
    res = _run(_repo(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout


def test_tracked_real_ocid_fails(tmp_path):
    r = _repo(tmp_path)
    (r / "leak.md").write_text(f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "leak")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "FAIL" in res.stderr


def test_staged_only_is_detected(tmp_path):
    """stage してから作業ツリーだけマスクしても抜けられない。"""
    r = _repo(tmp_path)
    (r / "leak.md").write_text(f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}\n")
    _git(r, "add", "leak.md")
    (r / "leak.md").write_text("ocid1.ormjob.oc1.ap-osaka-1.MASKED\n")  # 作業ツリーは綺麗
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_untracked_needs_all_flag(tmp_path):
    """未追跡は --all のときだけ見る（既定を重くしないため）。"""
    r = _repo(tmp_path)
    (r / "leak.md").write_text(f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}\n")
    assert _run(r).returncode == 0
    assert _run(r, "--all").returncode == 1


def test_non_oc1_realm_is_detected(tmp_path):
    """realm を oc1 に固定しない（OC2/OC3/OC4 も実値）。"""
    r = _repo(tmp_path)
    (r / "leak.md").write_text(f"ocid1.ormjob.oc2.us-langley-1.{BODY}\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "leak")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_allowlisted_ocid_passes(tmp_path):
    """受容済みは通す。**全件が受容済みでも通ること**（grep の exit 1 で壊れやすい）。"""
    ocid = f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}"
    r = _repo(tmp_path, allow=ocid)
    (r / "leak.md").write_text(ocid + "\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "accepted")
    res = _run(r)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "受容済み" in res.stdout


def test_tenancy_cannot_be_allowlisted(tmp_path):
    """tenancy は allowlist に書いても拒否する（運用規律に頼らない）。"""
    ocid = f"ocid1.tenancy.oc1..{BODY}"
    r = _repo(tmp_path, allow=ocid)
    (r / "leak.md").write_text(ocid + "\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "try to accept tenancy")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "受容しません" in res.stderr


def test_compartment_cannot_be_allowlisted(tmp_path):
    """compartment も同様（サポート詐称・cross-tenancy ポリシーの標的化に使われうる）。"""
    ocid = f"ocid1.compartment.oc1..{BODY}"
    r = _repo(tmp_path, allow=ocid)
    (r / "leak.md").write_text(ocid + "\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "try to accept compartment")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr


def test_commented_out_tenancy_in_allowlist_is_rejected(tmp_path):
    """コメント化・字下げして allowlist に紛れ込ませても拒否する。

    allowlist はスキャン対象外なので、`^` 固定の判定だと `# ocid1.tenancy...` を書くだけで
    検査を丸ごと迂回できた（review-17 blocker）。
    """
    ocid = f"ocid1.tenancy.oc1..{BODY}"
    r = _repo(tmp_path, allow=f"   # メモ: {ocid}")
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "受容しません" in res.stderr


def test_allowlist_entry_with_surrounding_whitespace_works(tmp_path):
    """受容エントリの前後空白・行末コメントは許容する。"""
    ocid = f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}"
    r = _repo(tmp_path, allow=f"  {ocid}   # PORT-01 の E2E 証跡")
    (r / "leak.md").write_text(ocid + "\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "accepted")
    res = _run(r)
    assert res.returncode == 0, res.stdout + res.stderr


def test_failure_reports_source_file_for_staged_only(tmp_path):
    """作業ツリーがマスク済みでも、出所（index）のファイル名を示す。"""
    r = _repo(tmp_path)
    (r / "leak.md").write_text(f"ocid1.ormjob.oc1.ap-osaka-1.{BODY}\n")
    _git(r, "add", "leak.md")
    (r / "leak.md").write_text("ocid1.ormjob.oc1.ap-osaka-1.MASKED\n")
    res = _run(r)
    assert res.returncode == 1
    assert "leak.md" in res.stderr


def test_shorter_unique_id_tenancy_still_rejected(tmp_path):
    """禁止種別の閾値は一般検出と揃える。

    PAT が30文字で「実値」と判定するのに NEVER_ALLOW が40文字だと、その隙間の
    tenancy / compartment を allowlist に足して通せた（review-19 major）。
    """
    ocid = "ocid1.tenancy.oc1.." + "b" * 32
    r = _repo(tmp_path, allow=ocid)
    res = _run(r)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "受容しません" in res.stderr


def test_multiple_hits_are_all_reported(tmp_path):
    """複数検出時に途中で打ち切らない（set -e とパイプで止まりやすい）。"""
    r = _repo(tmp_path)
    (r / "a.md").write_text(f"ocid1.ormjob.oc1.ap-osaka-1.{'a' * 45}\n")
    (r / "b.md").write_text(f"ocid1.ormstack.oc1.ap-osaka-1.{'b' * 45}\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "two leaks")
    res = _run(r)
    assert res.returncode == 1
    assert "2 件" in res.stderr
    assert "マスクしてください" in res.stderr   # 末尾の案内まで到達している


def test_git_grep_error_fails_closed(tmp_path):
    """git grep がエラー終了したら落とす（fail-open にしない）。

    `|| true` で終了コードを一律に握り潰すと、オプション非対応や index 破損でも
    空の結果になり `[ocid] OK` を返す。過去2回この壊れ方を作った（review-21 major）。
    ここでは PATH 上に「必ず rc=2 で落ちる git」を置いて、grep が異常終了する状況を作る。
    """
    r = _repo(tmp_path)
    fake = r / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\n"
                              'case " $* " in *" grep "*) echo "fatal: boom" >&2; exit 2 ;; esac\n'
                              'exec /usr/bin/git "$@"\n')
    (fake / "git").chmod(0o755)
    res = subprocess.run(
        ["bash", "ops/check-no-real-ocid.sh"], cwd=r, capture_output=True, text=True,
        env={"PATH": f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(r)},
    )
    assert res.returncode != 0, res.stdout + res.stderr
    assert "OK" not in res.stdout

