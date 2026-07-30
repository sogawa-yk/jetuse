"""片付けの安全ガードの否定シナリオ（何も壊さずに止まることを確かめる）。

`teardown.py` は `DROP USER ... CASCADE` を打つ。名前だけを根拠にすると、
別タスク / 別人が同名で作り直したスキーマを消しうる。台帳との照合は
**USER_ID / 作成時刻 / この run 固有のマーカー**の 3 点で行い、**DROP の直前にも
もう一度**照合する。ここではその「止まるべき状況」を作って、
(a) 非ゼロ終了で止まる (b) リソースが無傷 の 2 つを毎回確かめる。

実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/guard_checks.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time

from common import HOME, MARKER_TABLE, banner, connect_admin, new_marker, require_schema

SCHEMA = require_schema()
ROOT = pathlib.Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"


def _run(script: str, ledger: pathlib.Path | None, *,
         pause: float = 0) -> subprocess.CompletedProcess:
    """台帳だけ差し替えて実行する（`RAGM02_HOME` は複製しない = 秘密を増やさない）。"""
    env = {**os.environ, "PYTHONPATH": "spikes/ragm02:packages/api"}
    if ledger is not None:
        env["RAGM02_LEDGER_PATH"] = str(ledger)
    if pause:
        env["RAGM02_TEST_PAUSE_SECONDS"] = str(pause)
    args = [".venv/bin/python", f"spikes/ragm02/{script}"]
    if script == "teardown.py":
        # **実削除を試させる**（それが否定シナリオの意味）。スキーマ名は run 固有で
        # この run が作ったものなので、仮にガードが壊れていても他タスクの資産には届かない。
        args.append("--yes")
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def run_teardown(ledger: pathlib.Path | None, *, pause: float = 0) -> subprocess.CompletedProcess:
    return _run("teardown.py", ledger, pause=pause)


def run_setup(ledger: pathlib.Path) -> subprocess.CompletedProcess:
    return _run("setup_schema.py", ledger)


_TEMP_LEDGERS: list[pathlib.Path] = []


def tampered_ledger(**changes) -> pathlib.Path:
    """**台帳だけ**を書き換えた写しを一時ファイルに作る（ウォレット・秘密は複製しない）。"""
    data = json.loads((HOME / "ledger.json").read_text())
    data.update(changes)
    fd, path = tempfile.mkstemp(prefix="ragm02-ledger-", suffix=".json")
    os.close(fd)
    dest = pathlib.Path(path)
    dest.write_text(json.dumps(data, indent=2))
    dest.chmod(0o600)
    _TEMP_LEDGERS.append(dest)
    return dest


def cleanup_temp_ledgers() -> None:
    for path in _TEMP_LEDGERS:
        path.unlink(missing_ok=True)
    print(f"  一時台帳を削除: {len(_TEMP_LEDGERS)} 件")


def intact() -> tuple[int, int]:
    admin = connect_admin()
    cur = admin.cursor()
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
    users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM all_objects WHERE owner = :u", u=SCHEMA)
    objs = cur.fetchone()[0]
    admin.close()
    return users, objs


def grants_of(schema: str) -> set[str]:
    """スキーマに付いている権限（既存再利用の否定シナリオで「変更していない」を示す）。"""
    admin = connect_admin()
    cur = admin.cursor()
    cur.execute("SELECT privilege FROM dba_sys_privs WHERE grantee = :u", u=schema)
    out = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT granted_role FROM dba_role_privs WHERE grantee = :u", u=schema)
    out |= {r[0] for r in cur.fetchall()}
    admin.close()
    return out


def check(label: str, proc: subprocess.CompletedProcess) -> dict:
    users, objs = intact()
    ok = proc.returncode != 0 and users == 1 and objs > 0
    print(f"  {label}: exit={proc.returncode} / user={users} / objects={objs}"
          f" -> {'PASS' if ok else 'FAIL'}")
    return {"label": label, "ok": ok, "exit": proc.returncode, "users": users,
            "objects": objs, "stderr": (proc.stderr or proc.stdout).strip().splitlines()[-1:]}


def swap_marker_during(pause: float) -> None:
    """teardown の 1 回目の照合後に、別人が作り直した状況（マーカーが変わる）を作る。"""
    def worker():
        time.sleep(pause / 2)
        admin = connect_admin()
        cur = admin.cursor()
        cur.execute(f"UPDATE {SCHEMA}.{MARKER_TABLE} SET marker = :m", m=new_marker())
        admin.commit()
        admin.close()
        print("  [race] DROP 直前にマーカーを差し替えた")

    threading.Thread(target=worker, daemon=True).start()


def restore_marker() -> None:
    marker = json.loads((HOME / "ledger.json").read_text())["marker"]
    admin = connect_admin()
    cur = admin.cursor()
    cur.execute(f"UPDATE {SCHEMA}.{MARKER_TABLE} SET marker = :m", m=marker)
    admin.commit()
    admin.close()
    print("  [race] マーカーを台帳の値へ戻した")


def main() -> None:
    banner("片付けガードの否定シナリオ")
    results = []
    results.append(check("G1 USER_ID が不一致（作り直された）",
                         run_teardown(tampered_ledger(user_id="999999"))))
    results.append(check("G2 作成時刻が不一致",
                         run_teardown(tampered_ledger(created="19990101000000"))))
    results.append(check("G3 同じ秒に作り直された相当（USER_ID・作成時刻は一致・マーカーだけ違う）",
                         run_teardown(tampered_ledger(marker=new_marker()))))
    results.append(check("G4 台帳が空（自分が作ったものではない）",
                         run_teardown(tampered_ledger(db="", schema="", user_id="",
                                                    created="", marker=""))))

    # G6: 既存スキーマの再利用経路（setup_schema）でも、最初の DDL の前に 3 点照合すること。
    # マーカーだけ違う台帳（= 同秒に作り直された状況）で setup を走らせ、
    # 非ゼロ終了かつ**権限が 1 つも変わっていない**ことを見る。
    before = grants_of(SCHEMA)
    proc = run_setup(tampered_ledger(marker=new_marker()))
    after = grants_of(SCHEMA)
    users, objs = intact()
    output = (proc.stderr or "") + (proc.stdout or "")
    # 停止理由が「人間ゲート」ではなく「マーカー不一致」であることまで見る
    stopped_for_ownership = "マーカーが不一致" in output
    ok6 = proc.returncode != 0 and before == after and users == 1 and stopped_for_ownership
    print(f"  G6 既存再利用でマーカー不一致: exit={proc.returncode} /"
          f" 権限の変化={'なし' if before == after else 'あり'} /"
          f" 停止理由=所有権照合:{stopped_for_ownership} -> {'PASS' if ok6 else 'FAIL'}")
    results.append({"label": "G6 既存再利用でマーカー不一致（setup_schema が何も変更せず中止）",
                    "ok": ok6, "exit": proc.returncode, "users": users, "objects": objs,
                    "stderr": (proc.stderr or proc.stdout).strip().splitlines()[-1:]})

    # G5: 正しい台帳で開始し、1 回目の照合後・DROP 直前にマーカーを差し替える（TOCTOU）
    swap_marker_during(6.0)
    results.append(check("G5 照合後・DROP 直前に作り直された（TOCTOU）",
                         run_teardown(None, pause=6.0)))
    restore_marker()  # 差し替えたマーカーを台帳の値へ戻す

    lines = "\n".join(
        f"| {r['label']} | {r['exit']} | {r['users']} | {r['objects']} |"
        f" {'PASS' if r['ok'] else 'FAIL'} |" for r in results)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "guard.md").write_text(f"""# 否定シナリオ — 片付けが「何も壊さずに止まる」こと

`teardown.py --yes` を、止まるべき状況で実行した。判定は
**(a) 非ゼロ終了 (b) スキーマとオブジェクトが無傷** の両方を満たすこと。

| ケース | exit | 残ったユーザー | 残ったオブジェクト | 判定 |
|---|---|---|---|---|
{lines}

いずれのケースも `teardown.py --yes`（実削除）を実際に試させている。スキーマ名は
**run 固有**（`{SCHEMA}`）でこの run が作ったものなので、ガードが壊れていても
他タスクの資産には届かない。

- 期待値: exit ≠ 0 / ユーザー 1 件 / オブジェクト > 0（= 1 件も消えていない）
- G3 は「**同じ秒に**同名スキーマが作り直された」状況。作成時刻（秒精度）だけでは
  見分けられないので、スキーマ内のマーカー表で判定している。
- G5 が示すのは「**最終照合より前**に作り直された場合に、最終照合で止まる」ことである。
  テスト用の待ち（`RAGM02_TEST_PAUSE_SECONDS`）で窓を作り、その間に別接続からマーカーを
  差し替えた。この再照合が無ければ、開始時の照合だけを根拠に削除していた。
  なお「最終照合と `DROP USER` の間に別主体が同名で作り直す」窓は、**スキーマ名を run 固有に
  したことで構造的に存在しない**（同名を作る他主体がいない）。ガードはその上の二重化である。
- G6 は破壊ではなく**変更**の側（`setup_schema.py` の既存再利用）。GRANT / ALTER USER / ACL /
  Resource Principal 付与の**前に**中止し、権限が 1 つも変わらないことを確認している。

停止時のメッセージ（最終行）:

```
{chr(10).join(r['stderr'][0] if r['stderr'] else '' for r in results)}
```
""")
    cleanup_temp_ledgers()
    print(f"\n  wrote {EVIDENCE / 'guard.md'}")
    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
