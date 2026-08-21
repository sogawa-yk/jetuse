"""ループの権限層が **apply を打ちうる入口をすべて塞いでいる**ことを固定する。

**なぜ要るか**（2026-08-21 に実際に起きた）: ループは
`--disallowedTools "Bash(terraform apply:*)"` で配備を止める設計だった。しかし配備は
`ops/` のスクリプト越しに打つため**コマンド名が一致せず素通りする**。VID-07 のループが
`ops/orm-stack.sh public-dev apply --apply` を実行し、**IAM ポリシーを人間の承認前に
適用した**（`loop-config.yml` の `hard_gates` にある `iam_identity` を越えた）。
ORM は API 経由なので `terraform` という文字列すら現れない。

「止まっているはず」が止まっていなかった。**ops に新しい配備経路が増えたときに
deny へ足し忘れれば同じことが起きる**ので、`ops/` の実態と deny 一覧を機械で照合する。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
LAUNCHERS = [ROOT / ".claude" / "loop" / "start-loop.sh",
             ROOT / ".claude" / "loop" / "start-stage.sh"]
OPS = ROOT / "ops"

# apply / destroy を実際に起こす呼び出し。ここに挙げたものを含む ops スクリプトは
# すべて deny されていなければならない。
APPLY_CALLS = re.compile(
    r"terraform\s+(apply|destroy)|resource-manager\s+job\s+create-(apply|destroy)-job")


def _deny_text(p: pathlib.Path) -> str:
    """`--disallowedTools` に続く**実際の引数行**を集める。

    **コメントを除いてから探す。** 解説文にも `--disallowedTools` と書いてあるため、
    素朴に検索するとコメント側に当たり、deny を外しても素通りする
    （最初の実装がこれで、変異させてもテストが落ちなかった）。
    """
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if not ln.lstrip().startswith("#")]
    src = "\n".join(lines)
    m = re.search(r"--disallowedTools(.*?)(?:\nfi\b|\nelse\b|\Z)", src, re.S)
    assert m, f"{p.name} に --disallowedTools が無い"
    body = m.group(1)
    # 引数は行継続（\）で続く。継続が切れたところで終わり。
    out = []
    for ln in body.splitlines():
        out.append(ln)
        if ln.strip() and not ln.rstrip().endswith("\\"):
            break
    return "\n".join(out)


def _deploying_ops() -> set[str]:
    """apply / destroy を打ちうる ops スクリプト名。"""
    out = set()
    for f in sorted(OPS.glob("*.sh")):
        body = f.read_text(encoding="utf-8", errors="replace")
        # コメント行は除く（説明文に terraform apply と書いてあるだけの場合がある）
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        if APPLY_CALLS.search(code):
            out.add(f.name)
    return out


def test_every_deploying_ops_script_is_denied():
    """**本丸。** apply を打ちうる ops スクリプトが漏れなく deny されている。"""
    scripts = _deploying_ops()
    assert scripts, "apply を打つ ops が1つも見つからない（検出の仕方が壊れている）"
    for p in LAUNCHERS:
        deny = _deny_text(p)
        missing = [s for s in scripts if f"ops/{s}" not in deny]
        assert not missing, f"{p.name} の deny に無い: {missing}（ops に増えたら足すこと）"


def test_orm_api_path_is_denied():
    """**ORM は terraform の文字列が現れない。** API を直接叩く経路も塞ぐ。"""
    for p in LAUNCHERS:
        deny = _deny_text(p)
        for c in ("create-apply-job", "create-destroy-job"):
            assert c in deny, f"{p.name} が {c} を塞いでいない"


def test_terraform_itself_is_still_denied():
    """従来の直接実行も引き続き塞ぐ（緩めていない）。"""
    for p in LAUNCHERS:
        deny = _deny_text(p)
        assert "terraform apply" in deny and "terraform destroy" in deny


def test_human_gates_for_git_are_still_denied():
    """コミット/PR/push のゲートを巻き添えで緩めていないこと。"""
    loop = _deny_text(LAUNCHERS[0])
    for c in ("git commit", "git push", "gh pr create", "gh pr merge"):
        assert c in loop, f"start-loop.sh が {c} を塞いでいない"
    # stage-runner は統合のため commit/merge を許す（設計どおり）。push と PR は塞ぐ。
    stage = _deny_text(LAUNCHERS[1])
    for c in ("git push", "gh pr create", "gh pr merge"):
        assert c in stage, f"start-stage.sh が {c} を塞いでいない"


def test_detection_would_catch_a_new_script(tmp_path):
    """**照合が実際に効くか。** 新しい配備スクリプトを増やすと検出されること。"""
    body = 'terraform apply -auto-approve\n'
    f = tmp_path / "deploy-new.sh"
    f.write_text(body, encoding="utf-8")
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert APPLY_CALLS.search(code), "新しい apply 経路を検出できない"
    # コメントだけの言及は拾わない（誤検知しない）
    only_comment = "# terraform apply は人間ゲート\necho hi\n"
    code2 = "\n".join(ln for ln in only_comment.splitlines() if not ln.lstrip().startswith("#"))
    assert not APPLY_CALLS.search(code2), "コメントの言及を誤検出している"
