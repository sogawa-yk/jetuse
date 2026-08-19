"""共有基盤を ORM で扱う道具の契約を固定する（ADR-0031）。

**なぜテストが要るか**: この経路の失敗はどれも「静かに間違ったことをする」形で出た。

- 共有 state を **umask 任せのファイル**へ落とすと、ADB のパスワードを含む state が
  他ユーザーから読める。落ちないので気づけない。
- スタック検索の失敗を「スタックが無い」と読むと、**同名スタックをもう1本作り**、
  apply で空 state から既存資源を作り直そうとする。
- `enable_dynamic_group` を渡し忘れると、**権限の無い環境が黙って建つ**
  （2026-08-08: public-dev の初回 plan が IAM 0 件だった）。

シェルを実行して確かめられるものは実行し、それ以外は記述を検査する
（実 apply は数十分かかるうえ課金が伴うため）。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

OPS = pathlib.Path(__file__).resolve().parents[3] / "ops"
ORM = OPS / "orm-stack.sh"
ADB = OPS / "start-adb-if-stopped.sh"
UP = OPS / "dev-env-up.sh"


def _body(path: pathlib.Path) -> list[str]:
    """コメントを除いた実行行。説明文の中の記述を検査対象にしない。"""
    return [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]


# --- 秘密の置き場 --------------------------------------------------------------


def test_shared_state_file_is_the_one_mktemp_made():
    """`$(mktemp ...).tfstate` と後置しない。

    後置すると書き込み先は **mktemp 管理外の別ファイル**になり、権限が umask 任せになる。
    共有 state には ADB のパスワードが入りうる。
    """
    text = "\n".join(_body(UP))
    assert not re.search(r'\$\(mktemp[^)]*\)\.\w+', text), \
        "mktemp の戻り値に拡張子を後置している（権限が umask 任せの別ファイルになる）"


def test_shared_state_is_locked_down_and_removed():
    text = "\n".join(_body(UP))
    assert "chmod 600" in text, "共有 state の権限を絞っていない"
    assert re.search(r"trap\s+'rm -f \"\$SHARED_STATE\"'", text), \
        "共有 state を終了時に消していない"


def test_mktemp_default_permissions_would_not_be_enough(tmp_path):
    """**なぜ chmod が要るか**を実測で示す。

    mktemp 自身は 0600 を作るが、名前を後置して作り直したファイルは umask 次第。
    ここでは後置した場合に 0600 とは限らないことを確かめる（環境の umask に依存するため
    「0600 でない」ことは断定せず、mktemp 本体との差だけを見る）。
    """
    r = subprocess.run(
        ["bash", "-c",
         # **1ファイルにつき1つの format**。`%Lp %Lp` を2ファイルへ適用すると
         # 先頭2値とも最初のファイルの mode になり、比較が成立しない。
         'umask 022; f=$(mktemp -t t-XXXXXX); g="${f}.suffix"; : > "$g"; '
         'stat -f "%Lp" "$f" 2>/dev/null || stat -c "%a" "$f"; '
         'stat -f "%Lp" "$g" 2>/dev/null || stat -c "%a" "$g"; rm -f "$f" "$g"'],
        capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    made, suffixed = r.stdout.split()
    assert made == "600", f"mktemp が 0600 で作っていない（前提が崩れた）: {made}"
    # umask 022 を明示したので、後置ファイルは 0644 になる = mktemp の保護を継承しない。
    assert suffixed != "600", (
        f"後置ファイルが 0600 になった（前提が変わった）: {suffixed}")


# --- スタック検索の fail-closed ------------------------------------------------


def test_find_stack_fails_instead_of_reporting_empty():
    """CLI の失敗を「スタックが無い」に潰さない。

    潰すと同名スタックを作り、`apply` で**空 state から既存資源を作り直そうとする**。
    """
    src = ORM.read_text()
    m = re.search(r"find_stack\(\) \{.*?\n\}", src, re.S)
    assert m, "find_stack が見つからない"
    fn = m.group(0)
    assert "|| true" not in fn, "CLI エラーを握り潰している"
    assert "return 1" in fn, "失敗を戻り値で伝えていない"
    assert "len(ids) > 1" in fn, "同名スタックの重複を検出していない"


def _run_find_stack(oci_stub: str, tmp_path) -> subprocess.CompletedProcess:
    """`find_stack` だけを切り出し、`oci` をスタブに差し替えて動かす。

    実 CLI を叩くと、未導入・未認証・通信断のどれでも非ゼロになり、
    **エラー処理を通ったことを保証できない**（外部通信で遅くもなる）。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "oci"
    stub.write_text("#!/usr/bin/env bash\n" + oci_stub + "\n")
    stub.chmod(0o755)
    fn = re.search(r"find_stack\(\) \{.*?\n\}", ORM.read_text(), re.S).group(0)
    return subprocess.run(
        ["bash", "-c",
         # find_stack が使うのは STACK_REGION。REGION だけ与えると `set -u` が先に落ち、
         # **CLI のエラー処理を検査せずに** 期待どおりの非ゼロになってしまう。
         'set -euo pipefail\nCOMPARTMENT=c\nSTACK_REGION=r\nSTACK_NAME=nope\n'
         + fn + '\nfind_stack\n'],
        capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)})


def test_find_stack_fails_when_the_cli_errors(tmp_path):
    """CLI が非ゼロで返ったら、空ではなく失敗として伝える。"""
    r = _run_find_stack('echo "ServiceError: NotAuthenticated" >&2; exit 1', tmp_path)
    assert r.returncode != 0, f"CLI 失敗を握り潰した: {r.stdout!r}"
    assert r.stdout.strip() == ""
    assert "unbound variable" not in r.stderr, f"変数未定義で落ちている: {r.stderr}"
    assert "スタック検索に失敗" in r.stderr


def test_find_stack_fails_on_unparsable_json(tmp_path):
    """CLI が成功しても、解釈できない出力は「0 件」と読まない。"""
    r = _run_find_stack('echo "WARNING: something"; echo "not json"', tmp_path)
    assert r.returncode != 0, f"壊れた出力を 0 件として通した: {r.stdout!r}"
    assert r.stdout.strip() == ""


def test_find_stack_returns_empty_for_a_genuine_zero(tmp_path):
    """本当に 0 件なら、空を返して成功する（新規作成へ進める）。"""
    r = _run_find_stack('echo "[]"', tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_find_stack_rejects_duplicates(tmp_path):
    """同名スタックが複数あるのは異常。止める。"""
    r = _run_find_stack('echo \'["a","b"]\'', tmp_path)
    assert r.returncode != 0
    assert "同名スタック" in r.stderr


def test_find_stack_returns_the_single_id(tmp_path):
    r = _run_find_stack('echo \'["ocid1.ormstack.oc1..only"]\'', tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ocid1.ormstack.oc1..only"


# --- 環境ごとに決まっている値 --------------------------------------------------


@pytest.mark.parametrize("var", [
    "enable_dynamic_group",      # 既定 false。渡さないと権限の無い環境が建つ
    "enable_runtime_policy",
    "existing_dynamic_group",    # 共用する DG 名。空だとポリシーが誰も指さない
    "region",                    # スタックの所在ではなく資源を作るリージョン
    "compartment_ocid",
    "prefix",
])
def test_stack_variables_are_always_explicit(var):
    """既定に落ちると壊れる変数は、毎回明示して渡す。"""
    assert f'"{var}"' in ORM.read_text(), f"{var} をスタック変数に渡していない"


def test_schema_declares_every_variable_the_script_sends():
    """`schema.yaml` に無い変数は ORM のコンソールで型が付かない（password が平文表示になる）。"""
    schema = (pathlib.Path(__file__).resolve().parents[3]
              / "infra/terraform/environments/dev/schema.yaml").read_text()
    sent = set(re.findall(r'^\s*"(\w+)":', ORM.read_text(), re.M))
    sent -= {"schemas"}
    missing = sorted(v for v in sent if f"  {v}:" not in schema)
    assert not missing, f"schema.yaml に宣言が無い変数: {missing}"


def test_adb_password_is_masked_in_schema():
    schema = (pathlib.Path(__file__).resolve().parents[3]
              / "infra/terraform/environments/dev/schema.yaml").read_text()
    m = re.search(r"  adb_admin_password:\s*\n\s*type: (\w+)", schema)
    assert m and m.group(1) == "password", "ADB パスワードがコンソールで平文表示になる"


# --- ADB 起動ヘルパ ------------------------------------------------------------


def test_adb_helper_resolves_both_environments():
    """internal-dev と public-dev の両方を引ける。

    以前は常に `COMPARTMENT_OCID`(=internal-dev)を見ていたため、
    `orm-stack.sh public-dev` が案内しても public-dev の ADB は永久に見つからなかった。
    """
    text = ADB.read_text()
    assert "PUBLIC_DEV_COMPARTMENT_OCID" in text, "public-dev のコンパートメントを引けない"
    assert "INTERNAL_DEV_COMPARTMENT_OCID" in text
    assert "jetuse-pubdev-adb" in text, "public-dev の ADB 名を知らない"


def test_adb_helper_rejects_unknown_env():
    r = subprocess.run([str(ADB), "bogus-env"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "未知の env" in (r.stdout + r.stderr)


def test_orm_preflight_points_at_the_right_environment():
    """プリフライトの案内が env を渡している（渡さないと既定=internal-dev を起動しに行く）。"""
    assert "start-adb-if-stopped.sh ${ENV_NAME}" in ORM.read_text(), \
        "ADB 起動の案内が環境を伝えていない"


# --- 移植性 --------------------------------------------------------------------


def test_no_bash4_only_features():
    """**macOS 既定の bash は 3.2。** 連想配列は使えない。

    `declare -A` を置くと 3.2 では配列添字が算術評価され、
    `internal: unbound variable` で落ちる（2026-08-08 実測）。
    `date -Is` も GNU 専用で BSD date にはない。
    """
    for path in OPS.glob("*.sh"):
        body = "\n".join(_body(path))
        assert "declare -A" not in body, f"{path.name}: 連想配列は bash 3.2 で動かない"
        assert "date -Is" not in body, f"{path.name}: date -Is は GNU 専用（macOS で落ちる）"


def test_ops_scripts_parse_under_bash_3_2():
    """`ops/*.sh` が /bin/bash（macOS では 3.2）で構文解析できる。"""
    for path in sorted(OPS.glob("*.sh")):
        r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, f"{path.name}: {r.stderr}"


def test_no_global_override_can_repoint_the_dynamic_group():
    """**環境をまたぐ override を持たない。**

    以前は `JETUSE_SHARED_DYNAMIC_GROUP` でどの環境の DG 名も差し替えられた。
    public-dev に `jetuse-internal-dg` を指せば、internal-dev の principal に
    public-dev の権限が付き、閉じたはずの境界がまた開く。
    """
    body = "\n".join(_body(ORM))
    assert "JETUSE_SHARED_DYNAMIC_GROUP" not in body, \
        "DG 名を環境をまたいで差し替えられる抜け道が残っている"


def test_stack_region_and_target_region_are_separate():
    """**スタックの所在**と**資源を作る先**を同じ変数にしない。

    同じにすると、配備先を変えた瞬間に既存スタックを別リージョンで探して見失い、
    同名スタックをもう1本作る。
    """
    body = "\n".join(_body(ORM))
    assert "STACK_REGION=" in body, "スタック所在リージョンが分離されていない"
    # resource-manager の API 呼び出しは所在リージョンを使う。
    for line in body.splitlines():
        if "oci resource-manager" in line and "--region" in line:
            assert '--region "$STACK_REGION"' in line, \
                f"ORM API が配備先リージョンを使っている: {line.strip()}"


def test_adb_helper_does_not_fold_stderr_into_the_value():
    """`2>&1` で案内文を値に混ぜない。

    CLI は該当 0 件のとき「Query returned empty result」を stderr に出す。値に混ぜると
    **「見つかった」と誤って数える**（2026-08-09 実測: 誤って「複数リージョンにある」と判定）。
    """
    body = "\n".join(_body(ADB))
    assert "--raw-output 2>&1" not in body, "stderr を検索結果に混ぜている"
    assert 'ERRF' in body, "stderr を別に受けていない"


def test_dynamic_group_is_never_shared_across_environments():
    """**環境ごとに別の DG を指す。** 共用すると権限境界が崩れる。

    ポリシーは動的グループ全体に権限を与えるので、複数コンパートメントを含む DG を
    使うと internal-dev の Container Instance が public-dev の ADB に届く
    （Codex review-1 の blocker）。
    """
    src = ORM.read_text()
    names = re.findall(r"^\s*DG_NAME=(\S+)", src, re.M)
    assert len(names) >= 2, "環境ごとの DG 名が定義されていない"
    assert len(set(names)) == len(names), f"環境間で同じ DG を共用している: {names}"


def test_apply_uses_the_reviewed_plan():
    """**確認した plan を適用する。** `AUTO_APPROVED` はその場で作り直して即適用する。

    人が読んだ内容と、実際に適用される内容が食い違いうる（構成・変数・実資源が
    その間に動いていれば別物になる）。
    """
    body = "\n".join(_body(ORM))
    # 危険なのは**フラグの組み合わせ**。語そのものは注意書きや警告文に出てよい。
    assert "--execution-plan-strategy AUTO_APPROVED" not in body, \
        "plan を作り直して即適用している"
    assert "--execution-plan-strategy FROM_PLAN_JOB_ID" in body, "確認済み plan を指定していない"
    assert "--execution-plan-job-id" in body


def test_apply_always_plans_first():
    """apply の前に必ず plan を回す（適用対象の plan が存在しないと apply できない）。"""
    body = ORM.read_text()
    m = re.search(r"apply\|import\)(.*?);;", body, re.S)
    assert m, "apply/import の分岐が見つからない"
    assert "run_job plan" in m.group(1), "apply の前に plan を回していない"


def test_import_is_guarded_as_a_one_shot():
    """import は移行専用。二度目と、前提未指定を拒む。"""
    body = "\n".join(_body(ORM))
    assert "移行は済んでいる" in ORM.read_text(), "既に移行済みでも再 import できてしまう"
    assert 'PAR_SET" != "1"' in body, "SPA_PAR_EXPIRY 未指定でも import できてしまう"


def test_apply_refuses_a_destructive_plan():
    """**受け入れ条件は `0 to destroy`（ADR-0031）。**

    `--apply` は plan を読む前に指定するので、「何が起きるか知らないまま適用する」形に
    なりうる。資源が消える計画は明示の上書きが無い限り通さない。
    """
    body = "\n".join(_body(ORM))
    assert "LAST_DESTROY_COUNT" in body, "destroy 件数を apply の判断に使っていない"
    assert "ORM_ALLOW_DESTROY" in body, "破棄を明示的に許可する経路が無い"


def test_adb_helper_does_not_call_transitional_states_ok():
    """AVAILABLE 以外を「問題なし」に丸めない（呼び出し側が起動済みと誤認する）。"""
    body = "\n".join(_body(ADB))
    assert "STARTING" in body, "遷移中の状態を扱っていない"
    assert 'AVAILABLE)' in body, "AVAILABLE だけを正常として扱っていない"


def test_job_log_failure_is_not_swallowed():
    """ログを取れなかったら判定しない。

    握り潰すと destroy 件数が 0 に見え、資源が消える plan でも `--apply` のゲートを
    素通りする。
    """
    src = ORM.read_text()
    fn = re.search(r"run_job\(\) \{.*?\n\}", src, re.S).group(0)
    assert "get-job-logs" in fn
    assert "> \"$log\" 2>/dev/null || true" not in fn, "ログ取得の失敗を握り潰している"
    assert "判定行が無い" in fn, "空ログ・形式変更を検出していない"


def test_import_addresses_quote_index_keys_safely():
    """`for_each` のキーに引用符が入っても壊れた address を作らない。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("orm_imports", OPS / "orm-import-blocks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = {"resources": [{
        "mode": "managed", "type": "terraform_data", "name": "x",
        "provider": 'provider["terraform.io/builtin/terraform"]',
        "module": "module.m",
        "instances": [{"index_key": 'a"b\\c', "attributes": {"id": "1"}}],
    }]}
    # terraform_data は SKIP 対象なので、別の型で試す。
    state["resources"][0]["type"] = "oci_objectstorage_bucket"
    state["resources"][0]["provider"] = 'provider["registry.terraform.io/oracle/oci"]'
    body, _ = mod.blocks(state)
    line = next(x for x in body if x.startswith("  to ="))
    assert '\\"' in line or '\\\\' in line, f"エスケープされていない: {line}"


def test_job_logs_are_written_with_locked_down_permissions():
    """**plan/apply のログには PAR の access_uri が載る。** umask 任せにしない。"""
    fn = re.search(r"run_job\(\) \{.*?\n\}", ORM.read_text(), re.S).group(0)
    assert 'chmod 600 "$log"' in fn, "ジョブログの権限を絞っていない"
    assert fn.index('chmod 600 "$log"') < fn.index("get-job-logs"), \
        "書き込んだ後に chmod している（その間は読める）"


def test_import_has_a_recovery_path():
    """途中で失敗して一部だけ取り込まれたときに、進む手がある。"""
    body = "\n".join(_body(ORM))
    assert "ORM_FORCE_IMPORT" in body, "部分 import から回復できない"


def test_local_state_is_retired_after_migration():
    """移行後にローカル state を残さない（同じ資源を2つの state が持つ）。"""
    body = "\n".join(_body(ORM))
    assert "migrated-to-orm" in body, "ローカル state を退避していない"


def test_unknown_action_is_rejected_before_touching_the_stack():
    """打ち間違い1つで既存スタックの構成を書き換えない。"""
    src = ORM.read_text()
    i_validate = src.index('未知の action')
    i_touch = src.index("resource-manager stack update")
    assert i_validate < i_touch, "action の検証がスタック更新より後にある"
    r = subprocess.run([str(ORM), "internal-dev", "typo"], capture_output=True, text=True,
                       timeout=60, cwd=ORM.parents[1])
    assert r.returncode == 2, r.stdout + r.stderr


def test_import_verifies_convergence_before_retiring_local_state():
    """完了条件（imports.tf 抜きで No changes）を確かめてから旧 state を畳む。"""
    body = ORM.read_text()
    i_check = body.index("収束確認")
    i_retire = body.index("migrated-to-orm")
    assert i_check < i_retire, "収束を確かめる前に state を退避している"
    assert "No changes" in body[i_check:i_retire], "No changes を確認していない"


# --- 証跡の秘匿 ----------------------------------------------------------------


RUNS = pathlib.Path(__file__).resolve().parents[3] / "runs"

# **PAR の access_uri は期限内なら認証情報として使える。** OCID より危険なのに、
# `ops/check-no-real-ocid.sh` は OCID しか見ない（2026-08-09 に証跡へ実際に混入した）。
SECRETS = [
    (r"/p/[A-Za-z0-9%_.-]{16,}", "Object Storage PAR の access_uri"),
    (r"idcs-[0-9a-f]{16,}", "Identity Domain の URL"),
    (r"[a-z0-9]{20,}\.apigateway\.[a-z0-9-]+\.oci\.customer-oci\.com", "API Gateway のホスト名"),
]


def test_committed_evidence_has_no_credentials():
    """`runs/` にコミットする証跡へ秘匿値を持ち込まない。"""
    tracked = subprocess.run(["git", "ls-files", "runs/"], capture_output=True, text=True,
                             cwd=RUNS.parent).stdout.split()
    bad = []
    for rel in tracked:
        f = RUNS.parent / rel
        if not f.is_file():
            continue
        try:
            body = f.read_text(errors="replace")
        except OSError:
            continue
        for pat, what in SECRETS:
            for m in re.finditer(pat, body):
                # 説明文の中で語として触れているだけのものは対象外。
                if "秘匿値" in body[max(0, m.start() - 80):m.start()]:
                    continue
                bad.append(f"{rel}: {what}")
                break
    assert not bad, "証跡に認証情報が含まれている:\n  " + "\n  ".join(sorted(set(bad)))


# --- 安全機構を実際に動かす -----------------------------------------------------


OCI_STUB = r'''#!/usr/bin/env bash
# `oci` の最小スタブ。呼ばれたサブコマンドを $CALLS へ追記し、必要な JSON だけ返す。
echo "$*" >> "$CALLS"
case "$1 $2 $3" in
  "resource-manager stack list")   echo '["ocid1.ormstack.oc1..stub"]' ;;
  "resource-manager stack get")    echo '{"prefix":"jetuse-dev"}' ;;
  "resource-manager stack update") echo '{}' ;;
  "resource-manager job create-plan-job")  echo '{"id":"ocid1.ormjob.oc1..planjob0001"}' ;;
  "resource-manager job create-apply-job") echo '{"id":"ocid1.ormjob.oc1..applyjob001"}' ;;
  "resource-manager job get")      echo 'SUCCEEDED' ;;
  "resource-manager job get-job-logs") cat "$FAKE_LOG" ;;
  "db autonomous-database list")   echo '["AVAILABLE"]' ;;
  *) echo '{}' ;;
esac
'''


def _run_orm(tmp_path, plan_log: str, *args, env_extra=None):
    """`ops/orm-stack.sh` を `oci` スタブ付きで動かし、呼ばれた API を記録する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "oci"
    stub.write_text(OCI_STUB)
    stub.chmod(0o755)
    calls = tmp_path / "calls.txt"
    calls.touch()
    fake_log = tmp_path / "plan.log"
    fake_log.write_text(plan_log)

    # **実 `.env` を読ませない。** 読むと実環境の値が混ざり、テストの前提が環境依存になる。
    # 空の `.env` を持つ作業ディレクトリを作って、そこでスクリプトを走らせる。
    repo_src = ORM.parents[1]
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        for name in ("ops", "infra"):
            (repo / name).symlink_to(repo_src / name)
        (repo / ".env").write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "CALLS": str(calls),
        "FAKE_LOG": str(fake_log),
        "ORM_LOG_DIR": str(tmp_path),
        # .env を読ませない代わりに必要な値を渡す
        "INTERNAL_DEV_COMPARTMENT_OCID": "ocid1.compartment.oc1..stub",
        "TENANCY_OCID": "ocid1.tenancy.oc1..stub",
        "ADB_ADMIN_PASSWORD": "stub",
        "SPA_PAR_EXPIRY": "",
    }
    env.update(env_extra or {})
    r = subprocess.run([str(repo / "ops" / "orm-stack.sh"), *args],
                       capture_output=True, text=True, timeout=180, cwd=repo, env=env)
    return r, calls.read_text()


DESTRUCTIVE = ('"Plan: 0 to add, 0 to change, 1 to destroy."\n'
               '"  # module.adb[0].oci_database_autonomous_database.this will be destroyed"\n')
CLEAN = '"No changes. Your infrastructure matches the configuration."\n'


def test_destructive_plan_is_not_applied(tmp_path):
    """**資源が消える plan は適用しない。** 文字列の存在ではなく、apply が呼ばれないことを見る。"""
    r, calls = _run_orm(tmp_path, DESTRUCTIVE, "internal-dev", "apply", "--apply")
    assert r.returncode != 0, f"破壊的な plan を通した:\n{r.stdout}"
    assert "create-apply-job" not in calls, f"apply が呼ばれた:\n{calls}"
    assert "destroy / replace が 1 件" in r.stderr


def test_destructive_plan_can_be_applied_with_an_explicit_override(tmp_path):
    """意図した破棄は明示すれば通る（塞ぎっぱなしにはしない）。"""
    r, calls = _run_orm(tmp_path, DESTRUCTIVE, "internal-dev", "apply", "--apply",
                        env_extra={"ORM_ALLOW_DESTROY": "1"})
    assert "create-apply-job" in calls, f"明示しても適用されない:\n{r.stdout}{r.stderr}"


def test_clean_plan_applies(tmp_path):
    """差分の無い plan は普通に適用できる（ゲートが常時閉じていない）。"""
    r, calls = _run_orm(tmp_path, CLEAN, "internal-dev", "apply", "--apply")
    assert "create-apply-job" in calls, f"clean な plan が適用されない:\n{r.stdout}{r.stderr}"
    assert "--execution-plan-strategy FROM_PLAN_JOB_ID" in calls, "確認済み plan を使っていない"


ADB_STUB = r'''#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$1 $2 $3" in
  "db autonomous-database list")
    if [ "${STUB_FAIL_REGION:-}" != "" ] && echo "$*" | grep -q "$STUB_FAIL_REGION"; then
      echo "ServiceError: unreachable" >&2; exit 1
    fi
    if echo "$*" | grep -q "us-chicago-1"; then cat "$STUB_ROWS"; else echo ""; fi
    ;;
  "db autonomous-database start") echo '{}' ;;
  *) echo '{}' ;;
esac
'''


def _run_adb(tmp_path, rows: str, *args, env_extra=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "oci").write_text(ADB_STUB)
    (bin_dir / "oci").chmod(0o755)
    calls = tmp_path / "calls.txt"
    calls.touch()
    rows_f = tmp_path / "rows.json"
    rows_f.write_text(rows)
    repo_src = ADB.parents[1]
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        for name in ("ops", "infra"):
            (repo / name).symlink_to(repo_src / name)
        (repo / ".env").write_text("")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "HOME": str(tmp_path), "CALLS": str(calls), "STUB_ROWS": str(rows_f),
           "INTERNAL_DEV_COMPARTMENT_OCID": "ocid1.compartment.oc1..stub",
           "ADB_REGIONS": "us-chicago-1 ap-osaka-1"}
    env.update(env_extra or {})
    r = subprocess.run([str(repo / "ops" / "start-adb-if-stopped.sh"), *args],
                       capture_output=True, text=True, timeout=120, cwd=repo, env=env)
    return r, calls.read_text()


def test_adb_helper_starts_only_a_stopped_one(tmp_path):
    r, calls = _run_adb(tmp_path, '[{"id":"ocid1.a","state":"STOPPED"}]', "internal-dev")
    assert r.returncode == 0, r.stderr
    assert "autonomous-database start" in calls, "STOPPED なのに起動していない"


def test_adb_helper_leaves_available_alone(tmp_path):
    r, calls = _run_adb(tmp_path, '[{"id":"ocid1.a","state":"AVAILABLE"}]', "internal-dev")
    assert r.returncode == 0, r.stderr
    assert "autonomous-database start" not in calls, "AVAILABLE を起動している"


def test_adb_helper_does_not_touch_transitional_states(tmp_path):
    """遷移中は触らない。呼び出し側が「起動した」と誤認しないよう非ゼロで返す。"""
    r, calls = _run_adb(tmp_path, '[{"id":"ocid1.a","state":"STARTING"}]', "internal-dev")
    assert r.returncode != 0, "遷移中を成功扱いにしている"
    assert "autonomous-database start" not in calls


def test_adb_helper_stops_on_duplicates_in_one_region(tmp_path):
    r, calls = _run_adb(tmp_path,
                        '[{"id":"ocid1.a","state":"STOPPED"},{"id":"ocid1.b","state":"STOPPED"}]',
                        "internal-dev")
    assert r.returncode != 0, "同一リージョンの重複を通している"
    assert "autonomous-database start" not in calls, "重複なのに起動した"


def test_adb_helper_does_not_start_when_a_region_lookup_fails(tmp_path):
    """**一部リージョンを確認できなければ何も起動しない。**"""
    r, calls = _run_adb(tmp_path, '[{"id":"ocid1.a","state":"STOPPED"}]', "internal-dev",
                        env_extra={"STUB_FAIL_REGION": "ap-osaka-1"})
    assert "autonomous-database start" not in calls, "確認漏れがあるのに起動した"


def test_adb_helper_fails_when_nothing_is_found(tmp_path):
    r, _ = _run_adb(tmp_path, "", "internal-dev")
    assert r.returncode != 0, "見つからないのに成功している"
