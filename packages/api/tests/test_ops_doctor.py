"""前提チェック（`ops/doctor.sh`）が陳腐化しないことを固定する。

**なぜテストが要るか**: 前提の欠落はこれまで**実行の途中で**露見していた。
とくに `codex` はレビューゲートそのもので、無いとループは実装まで進んでから
レビューで落ちる（`onboarding.md` に記載すら無かった）。

doctor を足しても、**新しい外部コマンドを ops に足したときに doctor へ書き忘れれば
同じことが起きる**。だから「ops が使うもの ⊆ doctor が見るもの」を機械で照合する。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
OPS = ROOT / "ops"
DOCTOR = OPS / "doctor.sh"

# シェルの構文・組み込み・どの POSIX 環境にもあるもの。「導入が要る依存」ではない。
UBIQUITOUS = {
    # 構文
    "do", "done", "then", "else", "elif", "fi", "esac", "case", "while", "for",
    "if", "in", "function", "break", "continue", "return", "exit", "shift", "until",
    # 組み込み
    "echo", "printf", "read", "test", "set", "unset", "local", "export", "eval",
    "exec", "trap", "cd", "pwd", "true", "false", "source", "shopt", "command",
    "wait", "kill", "type", "hash", "umask", "times",
    # どこにでもある coreutils 相当
    "awk", "sed", "grep", "cat", "cut", "tr", "head", "tail", "sort", "uniq", "wc",
    "find", "xargs", "mkdir", "rm", "cp", "mv", "chmod", "ls", "date", "sleep",
    "env", "basename", "dirname", "mktemp", "git", "bash", "sh", "seq", "tee",
    "diff", "comm", "join", "paste", "fold", "expr", "id", "stat",
}


def _strip_embedded(body: str) -> str:
    """埋め込みの非シェルを落とす。

    `python3 -c '...'` や `<<'PY' ... PY` の中身は Python であってシェルではない。
    残すと `print` / `raise` を「コマンド」と誤認する。
    """
    # heredoc の本体を落とす。引用符の有無どちらも。**中身は出力する文章であって
    # コマンドではない**（案内文の「base を…」を base コマンドと誤認していた）。
    body = re.sub(r"<<-?'(\w+)'.*?\n.*?^\s*\1\s*$", "", body, flags=re.S | re.M)
    body = re.sub(r"<<-?\s*(\w+)\b.*?\n.*?^\s*\1\s*$", "", body, flags=re.S | re.M)
    # python3 -c '...' / "..." の本体を落とす
    body = re.sub(r"python3?\s+-c\s+'[^']*'", "python3 -c", body, flags=re.S)
    body = re.sub(r'python3?\s+-c\s+"[^"]*"', "python3 -c", body, flags=re.S)
    return body


def _shell_bodies() -> dict[str, str]:
    """コメントと埋め込み非シェルを除いた実行部分。"""
    out = {}
    # **走査対象は `ops/` だけ。** `.claude/skills/*/scripts/` も見にいったが、
    # レビュー指示の長い散文（heredoc）から `blocker` / `major` のような語を
    # コマンドとして拾ってしまい、精度が保てなかった。
    # そこから来る唯一の依存は `codex` で、それは
    # `test_hard_requirements_are_treated_as_hard` が名指しで押さえている。
    for f in sorted(OPS.glob("*.sh")):
        body = "\n".join(
            ln for ln in f.read_text().splitlines() if not ln.lstrip().startswith("#"))
        out[f.name] = _strip_embedded(body)
    return out


_FN = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*\(\)\s*\{", re.M)
_LABEL = re.compile(r"^\s*[\w*?|\[\]\".$/{}-]+\)")
# コマンドの前に付きうる修飾。**ここを飛ばさないと本体を見落とす**
# （`if ! kubectl ...` は `if` を拾って終わり、`VAR=x kubectl` は代入で終わっていた）。
_MODIFIERS = {"if", "while", "until", "then", "elif", "do", "!", "time", "sudo",
              "exec", "command", "nohup", "env", "builtin", "eval"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 簡単コマンドの区切り。ここで割って、各断片の先頭語を見る。
_SPLIT = re.compile(r"\|\||&&|\||;|\$\(|\n|`")


def _all_functions() -> set[str]:
    """`ops/*.sh` 全体で定義された関数。**他ファイルのものも含む**。

    `_region.sh` は source されるので、`jetuse_ocir_host` は呼び出し側の
    ファイルだけ見ていると「未知の外部コマンド」に見える。
    """
    return {n for b in _shell_bodies().values() for n in _FN.findall(b)}


def _commands_used() -> set[str]:
    """`ops/*.sh` が呼ぶ**外部コマンド**。

    **`shutil.which` で絞らない。** 手元に入っていないツール（新しく使い始めたもの）
    こそ doctor に足りていないので、`which` で消すと検査の意味が無くなる。
    代わりに、コマンドでないものを構造で落とす。

    - 構文・組み込み・どこにでもあるもの（`UBIQUITOUS`）
    - `ops/*.sh` のどこかで定義された関数
    - `case` の分岐ラベル行（`plan|apply|import|state)`）
    - 変数代入（`rc=1`）
    - 埋め込み Python の中身（`_strip_embedded` で除去済み）
    """
    fns = _all_functions()
    used: set[str] = set()
    for name, body in _shell_bodies().items():
        if name == "doctor.sh":
            continue          # doctor 自身は対象外（見る側）
        # **行ごとに処理する。** `'[^']*'` は改行をまたいで対になりうるので、
        # ファイル全体に一度に当てると、離れた行のアポストロフィと組んで
        # 広い範囲を巻き込み構造が壊れる（誤検出の主因だった）。
        cleaned = []
        for ln in body.splitlines():
            if _LABEL.match(ln):
                continue                      # case の分岐ラベル行
            # クォートの中は走査しない（`grep -E "a|b"` の `|` を区切りと誤読するため）。
            # 位置を保つためプレースホルダに置き換える（空白に潰すと次語が行頭に見える）。
            ln = re.sub(r"'[^'\n]*'", "_Q_", ln)
            ln = re.sub(r'"(?:[^"\\\n]|\\.)*"', "_Q_", ln)
            ln = re.sub(r"\s#.*$", "", ln)     # 行末コメント（クォートは潰し済み）
            # **算術展開はコマンド置換ではない。** `$((found + 1))` を `$(` で割ると
            # `found` が先頭語＝コマンドに見える。先に潰す。
            ln = re.sub(r"\$\(\([^)]*\)\)", "_A_", ln)
            cleaned.append(ln)
        scannable = "\n".join(cleaned)
        for seg in _SPLIT.split(scannable):
            for word in seg.split():
                w = word.strip("()&{}")
                if not w or _ASSIGN.match(w):
                    continue                  # 代入は飛ばして本体を探す
                if w in _MODIFIERS:
                    continue                  # 修飾も飛ばす
                if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,15}", w):
                    break                     # 引数やオプション = ここで打ち切り
                if w not in UBIQUITOUS and w not in fns:
                    used.add(w)
                break                         # 先頭の1語だけがコマンド
    return used


def test_doctor_exists_and_is_executable():
    assert DOCTOR.exists(), "ops/doctor.sh が無い"
    assert os.access(DOCTOR, os.X_OK), "実行権限が無い"


def test_make_exposes_doctor():
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^doctor:", mk, re.M), "make doctor が入口に出ていない"
    assert "doctor" in re.search(r"^\.PHONY:.*", mk, re.M).group(0), ".PHONY に無い"


def test_every_external_command_used_by_ops_is_checked():
    """**ops が使うもの ⊆ doctor が見るもの。**

    ここが破れると、足りない前提に実行の途中で気づくことになる。
    新しいコマンドを使い始めたら doctor にも足す。
    """
    doctor = DOCTOR.read_text()
    used = _commands_used()
    # doctor 側は `has <cmd>` / `command -v <cmd>` / for ループの列挙で見ている。
    checked = set(re.findall(r"\bhas\s+([a-z][a-z0-9_.-]+)", doctor))
    checked |= set(re.findall(r"command -v\s+\"?([a-z][a-z0-9_.-]+)", doctor))
    for m in re.finditer(r"for c in ([a-z0-9 _.-]+); do", doctor):
        checked |= set(m.group(1).split())

    missing = sorted(c for c in used if c not in checked)
    assert not missing, (
        "ops が使うのに doctor が見ていないコマンド: " + ", ".join(missing) +
        "\n  → ops/doctor.sh に追加するか、自明なら UBIQUITOUS へ入れる")


@pytest.mark.parametrize("cmd", ["codex", "oci", "terraform", "python3", "node"])
def test_hard_requirements_are_treated_as_hard(cmd):
    """**レビューゲートと実環境の前提は必須扱い。** warn では見落とす。"""
    doctor = DOCTOR.read_text()
    body = doctor[doctor.index("== 必須"):doctor.index("== あると良い")]
    assert cmd in body, f"{cmd} が必須の節に無い"


def test_codex_is_explained_not_just_listed():
    """`codex` が何のために要るかを書く。名前だけでは導入判断ができない。"""
    doctor = DOCTOR.read_text()
    assert "レビュー" in doctor, "codex の役割が書かれていない"


# 手元に無いツールの代役。**doctor が版を見る項目は、通る値を返させる**
# （terraform 1.7+ / node 22+）。これが無いと CI で検査そのものが skip され、
# doctor が壊れていても気づけない。
_FAKE = {
    "terraform": '#!/usr/bin/env bash\n'
                 'if [ "$1" = version ] && [ "$2" = -json ]; then\n'
                 '  echo \'{"terraform_version":"1.9.0"}\'\n'
                 'else echo "Terraform v1.9.0"; fi\n',
    "node": '#!/usr/bin/env bash\necho v22.12.0\n',
    "npm": '#!/usr/bin/env bash\necho 10.9.0\n',
    "codex": '#!/usr/bin/env bash\necho codex-cli 0.144.0\n',
    "oci": '#!/usr/bin/env bash\necho 3.71.4\n',
}


def _version_ok(cmd: str, path: str) -> bool:
    """doctor が版を見る項目について、この実物が要件を満たすか。"""
    try:
        if cmd == "terraform":
            out = subprocess.run([path, "version", "-json"], capture_output=True,
                                 text=True, timeout=60).stdout
            v = json.loads(out)["terraform_version"].split(".")
            return (int(v[0]), int(v[1])) >= (1, 7)
        if cmd == "node":
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=60).stdout
            return int(out.strip().lstrip("v").split(".")[0]) >= 22
    except Exception:
        return False
    return True


def _run_doctor(tmp_path, missing: list[str]):
    """指定コマンドだけを PATH から隠して doctor を動かす。

    **実物への shim を作る。** `command -v` はシェル組み込みなので
    `subprocess.run(["command", "-v", ...])` では解決できず、以前は全部が
    「exit 0 するだけの偽物」になっていた（doctor は通るが中身を見ていない）。
    `shutil.which` で実体を引く。
    """
    # **実リポジトリの .env / ~/.oci/config に依存しない。** どちらも gitignore 対象で、
    # クリーンな checkout（CI）には無い。依存すると「codex を隠したから落ちた」のか
    # 「.env が無いから落ちた」のか区別できず、CI で必ず落ちる。
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        (repo / "ops").symlink_to(OPS)
        # doctor が必須と見なす鍵をすべて入れる（`ops/doctor.sh` の REQUIRED と揃える）。
        (repo / ".env").write_text(
            "COMPARTMENT_OCID=ocid1.compartment.oc1..t\n"
            "TENANCY_OCID=ocid1.tenancy.oc1..t\n"
            "ADB_ADMIN_PASSWORD=dummy\n"
            "ADB_OCID=ocid1.autonomousdatabase.oc1..t\n"
            "INTERNAL_DEV_COMPARTMENT_OCID=ocid1.compartment.oc1..i\n"
            "PUBLIC_DEV_COMPARTMENT_OCID=ocid1.compartment.oc1..p\n")
        oci_dir = tmp_path / "home" / ".oci"
        oci_dir.mkdir(parents=True)
        # doctor はプロファイルの**中身**まで見る（空の [DEFAULT] や、
        # key_file が指す先が無いものを通さないため）。実在する鍵ファイルを置く。
        key = oci_dir / "key.pem"
        key.write_text("dummy")
        (oci_dir / "config").write_text(
            "[DEFAULT]\nregion=us-chicago-1\ntenancy=ocid1.tenancy.oc1..t\n"
            f"user=ocid1.user.oc1..u\nfingerprint=aa:bb\nkey_file={key}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shimmed, faked = [], []
    for c in ("oci", "terraform", "python3", "node", "npm", "docker", "podman", "gh",
              "zip", "unzip", "tar", "jq", "curl", "codex"):
        if c in missing:
            continue
        real = shutil.which(c)
        # **版が要件を満たさないホストでは実物を使わない。** Terraform 1.6 や Node 20 が
        # 入っている環境で正常系が落ちると、doctor の欠陥と区別が付かない。
        if real is not None and c in _FAKE and not _version_ok(c, real):
            real = None
        if real is not None:
            (bin_dir / c).write_text(f'#!/usr/bin/env bash\nexec "{real}" "$@"\n')
            shimmed.append(c)
        else:
            # **手元に無くても偽物を置く。** 置かないと CI（ツールが揃わない環境）で
            # 検査そのものが skip され、doctor が壊れていても気づけない。
            # 版まで見る項目があるので、**doctor が通る値**を返す偽物にする。
            (bin_dir / c).write_text(_FAKE.get(c, '#!/usr/bin/env bash\nexit 0\n'))
            faked.append(c)
        (bin_dir / c).chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["HOME"] = str(tmp_path / "home")
    env.pop("AUTH_MODE", None)          # .env 側の値だけを見せる
    r = subprocess.run(["bash", str(repo / "ops" / "doctor.sh")],
                       capture_output=True, text=True, cwd=repo, env=env, timeout=300)
    return r, shimmed, faked


def test_missing_codex_fails_loudly(tmp_path):
    """**codex が無ければ非ゼロで止まる。** 「気づかず進む」を許さない。"""
    r, shimmed, faked = _run_doctor(tmp_path, ["codex"])
    assert r.returncode != 0, f"codex が無いのに成功した:\n{r.stdout}"
    # **codex 以外が原因で落ちていないこと**を確かめる。shim が偽物だと
    # 別の必須が NG になり、「codex を検出できた」の根拠にならない。
    ng = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("NG")]
    assert len(ng) == 1, f"codex 以外も NG になっている（shim が効いていない）:\n{r.stdout}"
    assert "codex" in ng[0], ng[0]


def test_doctor_passes_when_everything_is_present(tmp_path):
    """**揃っていれば通る。** 常に失敗する doctor は検査になっていない
    （`if has codex` を潰しても「落ちる」テストだけでは気づけない）。"""
    r, shimmed, faked = _run_doctor(tmp_path, [])
    assert r.returncode == 0, f"揃っているのに落ちた:\n{r.stdout}\n{r.stderr}"


def test_required_env_keys_match_the_fixture(tmp_path):
    """**doctor の必須鍵とテストの合成 `.env` を揃える。**

    ずれると正常系が「必須が欠けている」で落ち、doctor の欠陥と区別が付かない
    （2026-08-19: 必須を増やしたとき実際にこれで落ちた）。
    """
    block = re.search(r"REQUIRED = \[(.*?)\]", DOCTOR.read_text(), re.S).group(1)
    required = set(re.findall(r'"([A-Z][A-Z0-9_]+)"', block))
    _run_doctor(tmp_path, [])          # fixture を作らせる
    env = (tmp_path / "repo" / ".env").read_text()
    missing = sorted(k for k in required if f"{k}=" not in env)
    assert not missing, f"合成 .env に足りない鍵: {missing}"


# --- 個々の分岐を実行して確かめる -----------------------------------------------


def _doctor_with_oci_config(tmp_path, config_text: str, extra_env=None):
    """`~/.oci/config` の中身だけを差し替えて doctor を動かし、oci config の行を返す。"""
    r, shimmed, faked = _run_doctor(tmp_path, [])          # fixture を作らせる
    (tmp_path / "home" / ".oci" / "config").write_text(
        config_text.replace("{KEY}", str(tmp_path / "home" / ".oci" / "key.pem")))
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}:/usr/bin:/bin"
    env["HOME"] = str(tmp_path / "home")
    env.pop("AUTH_MODE", None)
    env.update(extra_env or {})
    out = subprocess.run(["bash", str(tmp_path / "repo" / "ops" / "doctor.sh")],
                         capture_output=True, text=True,
                         cwd=tmp_path / "repo", env=env, timeout=300)
    line = next((ln for ln in out.stdout.splitlines() if "oci config" in ln), "")
    return out, line


BASE_CFG = ("[DEFAULT]\nregion=us-chicago-1\ntenancy=t\nuser=u\n"
            "fingerprint=f\nkey_file={KEY}\n")


def test_empty_profile_is_rejected(tmp_path):
    """空の `[DEFAULT]` を通さない（実行時まで認証失敗に気づけない）。"""
    _, line = _doctor_with_oci_config(tmp_path, "[DEFAULT]\n")
    assert "NG" in line and "tenancy" in line, line


def test_missing_key_file_is_rejected(tmp_path):
    """`key_file` が指す先が無いものを通さない。"""
    _, line = _doctor_with_oci_config(tmp_path, BASE_CFG.replace("{KEY}", "/nope/none.pem"))
    assert "NG" in line and "key_file" in line, line


def test_missing_security_token_file_is_rejected(tmp_path):
    """セッション認証でも**トークンの実体**を見る。"""
    cfg = "[DEFAULT]\nregion=r\ntenancy=t\nkey_file={KEY}\nsecurity_token_file=/nope/x.tok\n"
    _, line = _doctor_with_oci_config(tmp_path, cfg)
    assert "NG" in line and "security_token_file" in line, line


def test_percent_in_config_does_not_break_parsing(tmp_path):
    """`%` を含む値で補間エラーにしない（`RawConfigParser`）。

    **名前付きプロファイルで試す。** `[DEFAULT]` は `defaults()` が生の値を返すので
    補間が走らず、素の `ConfigParser` でも通ってしまい検査にならない。
    """
    r, _, _ = _run_doctor(tmp_path, [])
    env_path = tmp_path / "repo" / ".env"
    env_path.write_text(env_path.read_text() + "OCI_PROFILE=WORK\n")
    cfg = ("[DEFAULT]\nregion=r\n\n[WORK]\nregion=r\ntenancy=t\nuser=u\n"
           "fingerprint=f\nkey_file={KEY}\npassphrase=100%x\n")
    _, line = _doctor_with_oci_config(tmp_path, cfg)
    assert "OK" in line, line


@pytest.mark.parametrize("value,expect_ok", [
    ("config_file", True),
    ("config_file # local", True),          # 行末コメントは値に含めない
    ("instance_principal", True),
    ("resource_principal", True),
    ("typo_mode", False),                   # 未知の値は通さない
])
def test_auth_mode_values(tmp_path, value, expect_ok):
    """`AUTH_MODE` の扱い。一律「不要」に倒すと typo が素通りし、
    一律必須にすると instance_principal で動く正しい環境が落ちる。"""
    r, _, _ = _run_doctor(tmp_path, [])
    env_path = tmp_path / "repo" / ".env"
    env_path.write_text(env_path.read_text() + f"AUTH_MODE={value}\n")
    _, line = _doctor_with_oci_config(tmp_path, BASE_CFG)
    assert ("OK" in line) is expect_ok, f"AUTH_MODE={value}: {line}"
