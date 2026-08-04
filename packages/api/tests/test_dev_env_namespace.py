"""`ops/dev-env-up.sh` の OCIR namespace 解決を固定する。

**なぜテストが要るか**: スクリプトは「環境変数 > `.env` の `OCIR_NAMESPACE` >
`.env` の `OS_NAMESPACE`」の優先順を**コメントで宣言していた**が、`set -euo pipefail` 下で
`grep` の「一致なし」(rc=1) が pipefail によりパイプライン全体の失敗になり、
`NS="${OCIR_NAMESPACE:-$(_ns_from_env_file OCIR_NAMESPACE)}"` の代入で `set -e` が発火して
**スクリプトごと終了**していた。つまり `OS_NAMESPACE` フォールバックは到達不能な死にコードで、
`.env` に `OCIR_NAMESPACE` を書いていない環境では**配備が無言で rc=1** になった
（2026-08-04 の実害）。

宣言した優先順が実際に効くことを、スクリプトの該当ロジックを切り出して固定する。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "dev-env-up.sh"


def _extract_resolver() -> str:
    """スクリプト本体から namespace 解決部分だけを取り出す。

    本体をコピーせず**実物から抜き出す**ので、実装が変わればこのテストも追随する。
    """
    src = SCRIPT.read_text()
    # **順序と隣接まで見る。** 3行を個別に探して固定順で合成すると、本体で順序が入れ替わったり
    # 間に必要な処理が挟まったりしても気づけない（review-9 minor）。1つの正規表現で
    # 「定義 → OCIR 優先 → OS フォールバック」がこの順に並んでいることごと照合する。
    m = re.search(
        r"^(_ns_from_env_file\(\).*?)$"           # 定義
        r"(?:\n(?:#.*|\s*)?)*"                     # 間はコメント/空行のみ許す
        r"^(NS=\"\$\{OCIR_NAMESPACE.*?)$"          # 環境変数 > OCIR_NAMESPACE
        r"(?:\n(?:#.*|\s*)?)*"
        r"^(\[ -n \"\$NS\" \] \|\| NS=.*?)$",      # OS_NAMESPACE フォールバック
        src, re.MULTILINE)
    assert m, "namespace 解決の3行がこの順に隣接していない（実装が変わった可能性）"
    return "\n".join(m.groups())


def _run(tmp_path: pathlib.Path, env_content: str, env_var: str | None = None):
    (tmp_path / ".env").write_text(env_content)
    body = "set -euo pipefail\n" + _extract_resolver() + '\nprintf "%s" "$NS"\n'
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if env_var is not None:
        env["OCIR_NAMESPACE"] = env_var
    return subprocess.run(["/bin/bash", "-c", body], cwd=tmp_path,
                          capture_output=True, text=True, env=env)


def test_uses_ocir_namespace_from_env_file(tmp_path):
    r = _run(tmp_path, "OCIR_NAMESPACE=fromocir\nOS_NAMESPACE=fromos\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fromocir"


def test_falls_back_to_os_namespace(tmp_path):
    """`.env` に OCIR_NAMESPACE が無くても OS_NAMESPACE で解決できる（実害そのもの）。"""
    r = _run(tmp_path, "OS_NAMESPACE=fromos\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "fromos"


def test_environment_variable_wins(tmp_path):
    r = _run(tmp_path, "OCIR_NAMESPACE=fromocir\n", env_var="fromenv")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fromenv"


def test_empty_when_neither_present(tmp_path):
    """どちらも無ければ空。呼び出し側がその後で明示的に落とす。"""
    r = _run(tmp_path, "SOMETHING_ELSE=x\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == ""


def test_strips_quotes_and_cr(tmp_path):
    """`.env` が引用符付き・CRLF でも値だけ取れる。"""
    r = _run(tmp_path, 'OS_NAMESPACE="quoted"\r\n')
    assert r.returncode == 0, r.stderr
    assert r.stdout == "quoted"
