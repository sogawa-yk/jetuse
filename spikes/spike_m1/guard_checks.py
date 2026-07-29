"""安全ガードが**実際に止まること**を実機で確かめる否定シナリオ。

「壊さない設計にした」と書くだけでは証跡にならないので、わざと危ない状況を作って
スクリプトが中止することを実行結果で示す。

  G1: 接続先が想定の ADB でないとき、DDL を打つ前に中止するか
  G2: 台帳に無い同名スキーマがあるとき、setup_schema が ALTER/GRANT を打たずに中止するか
  G3: 台帳が空のとき、teardown が名前一致だけで OCI リソース**および DB スキーマ**を
      消しに行かないか（初版はここが DB 側だけ門番されておらず、この確認自体が
      本物のスキーマを削除した。その反省で DB 側も台帳ゲートに入れた）

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/guard_checks.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

from common import banner

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv/bin/python")
ENV_BASE = {**os.environ, "PYTHONPATH": str(ROOT / "spikes/spike_m1")}


def run(script: str, env: dict, args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(ROOT / "spikes/spike_m1" / script), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)


def main() -> None:
    failures = []

    banner("G1 想定外の接続先なら DDL 前に中止するか")
    # 期待する DB 名をわざと別物にして、ガードが働くかを見る
    env = {**ENV_BASE, "SPIKE_EXPECT_DB_NAME_OVERRIDE": "1"}
    # 固定パスへ書くと同名ファイルを壊す。一時ディレクトリに置いて PYTHONPATH で解決させる。
    with tempfile.TemporaryDirectory() as td:
        probe = pathlib.Path(td) / "guard_probe.py"
        probe.write_text(
            "import common\n"
            "common.EXPECT_DB_NAME = 'SOMEONE_ELSES_DB'\n"
            "common.connect_admin()\n"
            "print('!!! ガードをすり抜けた')\n"
        )
        r = subprocess.run([PY, str(probe)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300, check=False)
    print(f"  exit={r.returncode}")
    print("  stdout/stderr:", (r.stdout + r.stderr).strip().splitlines()[-1:])
    if r.returncode == 0 or "想定外の接続先" not in (r.stdout + r.stderr):
        failures.append("G1: 想定外の接続先でも止まらなかった")

    banner("G2 台帳に無い同名スキーマなら setup_schema は中止するか")
    # スキーマが存在しない状態でこれを走らせると setup_schema が**本物を作ってしまう**
    # （一時台帳に記録されるので追跡不能になる）。存在するときだけ実行する。
    from common import SCHEMA, connect_admin
    admin = connect_admin()
    c = admin.cursor()
    c.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
    schema_exists = c.fetchone()[0] > 0
    admin.close()
    if not schema_exists:
        print(f"  SKIP: {SCHEMA} が存在しないため実行しない"
              "（実行すると本物のスキーマを作ってしまう）")
        failures.append(f"G2: {SCHEMA} 不在のため未検証（片付け後に走らせない運用が前提）")
    with tempfile.TemporaryDirectory() as td:
        empty = pathlib.Path(td) / "registry.json"
        empty.write_text(json.dumps({}))
        if schema_exists:
            r = run("setup_schema.py", {**ENV_BASE, "SPIKE_REGISTRY_PATH": str(empty)})
            print(f"  exit={r.returncode}")
            out = (r.stdout + r.stderr).strip()
            print("  " + "\n  ".join(out.splitlines()[-3:]))
            if r.returncode == 0 or "台帳に無い" not in out:
                failures.append("G2: 台帳に無い既存スキーマを黙って再利用した")

    banner("G3 台帳が空なら teardown は OCI リソースを消しに行かないか")
    with tempfile.TemporaryDirectory() as td:
        empty = pathlib.Path(td) / "registry.json"
        empty.write_text(json.dumps({}))
        # --yes 付き（実削除モード）でも、台帳が空なら消してはならない
        r = run("teardown.py", {**ENV_BASE, "SPIKE_REGISTRY_PATH": str(empty)}, ("--yes",))
    out = (r.stdout + r.stderr)
    print(f"  exit={r.returncode}")
    for line in out.splitlines():
        if "名前一致では消さない" in line or "delete " in line:
            print("  " + line.strip())
    skipped = out.count("名前一致では消さない")
    # bucket / vector_store / genai_project / DB オブジェクト / DB スキーマ の 5 箇所
    if skipped < 5:
        failures.append(f"G3: 台帳が空なのにスキップされなかった（skip {skipped}/5）")
    for danger in ("delete bucket", "delete vector store", "delete GenAI", "DROP USER"):
        if danger in out:
            failures.append(f"G3: 台帳が空なのに実削除に進んだ（{danger}）")

    banner("判定")
    if failures:
        for f in failures:
            print("  FAIL", f)
        sys.exit(1)
    print("  PASS: G1/G2/G3 いずれもガードが働き、危険な操作の手前で止まった")


if __name__ == "__main__":
    main()
