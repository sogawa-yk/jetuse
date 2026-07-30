"""RAGM-02 の検証用リソースを片付ける（既定は dry-run。実削除は `--yes`）。

消すのは**台帳（`RAGM02_HOME/ledger.json`）と一致したスキーマだけ**。一致しなければ
何も消さずに中止する（並行タスク RAGM-01 や他人の同名スキーマを壊さないため。
SPIKE-M1 では台帳ゲートの無い teardown が検証スキーマを実際に消す事故を起こしている）。

スキーマ名は **run 固有**なので、消す対象は必ずこの run が作ったものである（固定名のときに
残っていた「照合と DROP の間に別主体が同名で作り直す」窓が構造的に無い）。それでも
台帳との 3 点照合を開始時と `DROP USER` 直前の 2 回行う（fail-closed）。
破壊操作なので明示フラグ `--yes` は要る（CLAUDE.md の運用規約）。

実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/teardown.py [--yes]
"""

import os
import sys
import time

import oracledb

from common import (
    adb,
    banner,
    connect_admin,
    ownership_mismatch,
    purge_local_secrets,
    read_marker,
    require_schema,
)

SCHEMA = require_schema()


def _acl_left(cur) -> int:
    cur.execute("SELECT COUNT(*) FROM dba_host_aces WHERE principal = :p", p=SCHEMA)
    return cur.fetchone()[0]


def _remove_acl(cur) -> None:
    """付与したのと同じホスト・権限で ACE を外す（`ops/_adb.append_acl` と対）。"""
    for host in adb.acl_hosts():
        try:
            cur.execute("BEGIN DBMS_NETWORK_ACL_ADMIN.REMOVE_HOST_ACE("
                        "host => :h, ace => xs$ace_type(privilege_list => "
                        "xs$name_list('http'), principal_name => :p, "
                        "principal_type => xs_acl.ptype_db), remove_empty_acl => TRUE); END;",
                        h=host, p=SCHEMA)
            print(f"  ACL 削除: {host}")
        except oracledb.DatabaseError as e:
            print(f"  ACL 削除失敗({host}):", str(e).splitlines()[0])


def main() -> None:
    apply = "--yes" in sys.argv
    admin = connect_admin()
    cur = admin.cursor()
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
    if cur.fetchone()[0] == 0:
        # ユーザーが無くても ACL の消し残しは点検する（前回 DROP 後に ACL 削除だけ失敗した
        # 場合、ここで早期 return すると再実行しても永久に残る）。
        left = _acl_left(cur)
        print(f"{SCHEMA} は存在しない。残存 ACL: {left} 件")
        if left and apply:
            _remove_acl(cur)
            admin.commit()
            left = _acl_left(cur)
            print(f"  ACL 削除後: {left} 件")
        admin.close()
        if left == 0 and apply:
            # DROP は済んでいるのにローカル資材だけ残った状態（前回の中断）からの回収
            print("  ローカルの認証資材を削除:", ", ".join(purge_local_secrets()) or "(無し)")
        sys.exit(0 if left == 0 else 1)
    reason = ownership_mismatch(admin, marker=read_marker(admin))
    if reason:
        sys.exit(f"台帳と一致しないため中止する（何も削除していない）: {reason}")
    print("  台帳と一致（USER_ID / 作成時刻 / マーカーの 3 点）")

    cur.execute("SELECT object_type, COUNT(*) FROM all_objects WHERE owner = :u "
                "GROUP BY object_type ORDER BY 1", u=SCHEMA)
    print("  削除対象のオブジェクト:", cur.fetchall())
    if not apply:
        print(f"\ndry-run。実削除は --yes を付けて実行する（DROP USER {SCHEMA} CASCADE）")
        return

    banner(f"DROP USER {SCHEMA} CASCADE")
    # テスト用の待ち（否定 E2E が「1 回目の照合後・DROP 直前に作り直された」状況を作るため。
    # 本番運用では設定しない）。
    pause = float(os.environ.get("RAGM02_TEST_PAUSE_SECONDS", "0"))
    if pause:
        print(f"  [test hook] {pause}s 待機してから DROP 直前の再照合を行う")
        time.sleep(pause)
    # **DROP の直前にもう一度**照合する。最初の照合から DROP までの間に別人が作り直すと
    # （同じ秒であっても USER_ID とマーカーが変わるので）ここで止まる。
    reason = ownership_mismatch(admin, marker=read_marker(admin))
    if reason:
        sys.exit(f"DROP 直前の再照合で不一致（何も削除していない）: {reason}")
    cur.execute(f"DROP USER {SCHEMA} CASCADE")
    # ACL は principal を消したときに一緒に落ちることが多い（実測）。残っているときだけ外す。
    if _acl_left(cur):
        _remove_acl(cur)
    else:
        print("  ACL: DROP USER で同時に消えた（残 0 件）")
    admin.commit()

    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
    left = cur.fetchone()[0]
    acl_left = _acl_left(cur)
    print(f"  削除後の再照会: ユーザー {left} 件 / ACL {acl_left} 件（どちらも 0 が期待値）")
    admin.close()
    if left == 0 and acl_left == 0:
        # DB 側が完全に片付いたときだけ、ローカルの認証資材（ウォレット・パスワード・台帳）も消す。
        # 失敗が残っているときは、回収のために保持する。
        print("  ローカルの認証資材を削除:", ", ".join(purge_local_secrets()) or "(無し)")
    # ACL が残っていたら成功にしない（消し残しは次回の所有判定を汚す）
    sys.exit(0 if left == 0 and acl_left == 0 else 1)


if __name__ == "__main__":
    main()
