"""RAGM-02 の検証用スキーマ（**run 固有名** `JETUSE_RAGM02_<乱数>`）を共有 loop ADB に作る。

ADB は増やさない（並行タスク RAGM-01 と共有し、スキーマだけで隔離する）。
権限は ops/setup-dev-schema.py と同じ構成 + ベクタ系（`DBMS_VECTOR_CHAIN`）。
DB 内の資格情報は `OCI$RESOURCE_PRINCIPAL` に統一する（ADR-0021。API キーを DB へ焼かない）。

スキーマ名を **run ごとに変える**（オーケストレータ承認 2026-07-30）ので、
「作成／照合と実行の間に別主体が同名で作り直す」窓が構造的に存在しない。
それでも二重に守る: (a) 既に同名があれば**何もせず中止** (b) 作成直後に
**自分が生成したパスワードでログインできること**を、権限を 1 つも付ける前に確かめる。

実行: .venv/bin/python spikes/ragm02/setup_schema.py
片付け: .venv/bin/python spikes/ragm02/teardown.py --yes
"""

import re
import sys

import oracledb

import common
from common import (
    adb,
    banner,
    connect_admin,
    ledger,
    new_marker,
    ownership_mismatch,
    read_marker,
    record_owned,
    secret,
    user_receipt,
    write_marker,
)


def _password_matches(pw: str) -> bool:
    """CREATE SESSION を付けずにログインを試し、**パスワードが合っているか**だけを判定する。

    - `ORA-01045`（ユーザーに CREATE SESSION が無い）= 資格情報は正しい → True
    - `ORA-01017`（invalid username/password）= 別主体が作り直した → False
    どちらでもないエラーは判断できないので False（fail-closed）。
    """
    try:
        adb.connect(common.SCHEMA, pw).close()
        return True  # 既に CREATE SESSION がある場合（再実行時など）
    except oracledb.DatabaseError as e:
        msg = str(e)
        if "ORA-01045" in msg:
            return True
        if "ORA-01017" not in msg:
            print(f"  想定外のログイン結果: {msg.splitlines()[0]}")
        return False


def _redact(sql: str) -> str:
    return re.sub(r'IDENTIFIED BY ".*"', 'IDENTIFIED BY "***"', sql)


def _try(cur: oracledb.Cursor, sql: str, *, ignore: tuple[str, ...] = ()) -> None:
    shown = _redact(sql)
    try:
        cur.execute(sql)
        print(f"  ok: {shown[:78]}")
    except oracledb.DatabaseError as e:
        msg = str(e)
        if any(code in msg for code in ignore):
            print(f"  skip: {shown[:60]} ({msg.splitlines()[0]})")
            return
        raise


def reverify() -> None:
    """中断した実行の後始末: **パスワード一致を取り直して**台帳を検証済みに戻す。

    DDL は一切打たない。USER_ID / 作成時刻が台帳と一致し、かつ生成済みパスワードで
    ログインできたときだけ `verified` を立てる（別主体が作り直していれば必ず落ちる）。
    """
    pw = secret("schema_password")
    admin = connect_admin()
    actual = user_receipt(admin)
    recorded = ledger()
    for key in ("db", "schema", "user_id", "created"):
        if recorded.get(key) != actual.get(key):
            sys.exit(f"台帳と実物が違う（{key}: {recorded.get(key)} / {actual.get(key)}）。中止。")
    if not _password_matches(pw):
        sys.exit("生成済みパスワードでログインできない（別主体の同名ユーザー）。中止。")
    record_owned(actual, read_marker(admin), verified=True)
    print("  再検証: パスワード一致と USER_ID / 作成時刻の一致を確認し、台帳を検証済みにした")
    admin.close()


def main() -> None:
    if "--reverify" in sys.argv:
        reverify()
        return
    # この run のスキーマ名（無ければ採番する）。再実行では同じ名前を使う。
    schema = common.SCHEMA or common.new_schema_name()
    pw = secret("schema_password", generate=True)
    banner(f"ADMIN: ensure {schema}")
    admin = connect_admin()  # 接続先ガード（承認済みコンパートメントの loop ADB か）を内部で通す
    cur = admin.cursor()
    cur.execute("SELECT banner_full FROM v$version")
    print("db:", cur.fetchone()[0])

    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=schema)
    exists = cur.fetchone()[0] > 0
    if exists:
        # run 固有名なので、既にあるのは「自分の前回実行」のはず。それでも黙って
        # GRANT / ALTER / ACL を打たない。**最初の DDL より前に** USER_ID・作成時刻・
        # マーカーの 3 点とパスワード一致を確かめ、1 つでも違えば何も変更せず終了する。
        reason = ownership_mismatch(admin, marker=read_marker(admin))
        if not reason and not _password_matches(pw):
            reason = "生成済みパスワードでログインできない（別主体の同名ユーザー）"
        if reason:
            sys.exit(
                f"ユーザー {schema} は既に存在するが台帳と一致しない（{reason}）。"
                " 別用途 / 別タスクのスキーマを書き換える恐れがあるため中止する"
                "（何も変更していない。先に teardown するか、台帳を確認すること）。"
            )
        print(f"  既存の {schema} を再利用（USER_ID / 作成時刻 / マーカーの 3 点が一致）")
    else:
        adb.assert_password(schema, pw)
        _try(cur, f'CREATE USER {schema} IDENTIFIED BY "{pw}"')
        admin.commit()
        # **CREATE USER は暗黙コミット**。ここから先で落ちると「台帳に無い自分のスキーマ」が
        # 残って再実行も片付けも止まるので、真っ先に所有を記録する（マーカーは表を作れる
        # ようになってから追記。空マーカー同士は一致とみなす）。
        record_owned(user_receipt(admin), "", verified=False)
        # **権限を 1 つも付けないまま**、パスワードの一致だけで同一性を確かめる。
        # CREATE SESSION が無いので、パスワードが合っていれば ORA-01045（権限不足）、
        # 別主体が作り直していれば ORA-01017（資格情報が違う）になる。
        # この 2 つを区別すれば、他人のスキーマには GRANT を 1 つも打たずに済む。
        if not _password_matches(pw):
            sys.exit(f"作成直後の {schema} が自分のパスワードと一致しない"
                     "（別主体に作り直された可能性）。権限を一切付けずに中止する"
                     "（台帳は未検証のまま残るので、次回も所有物として扱わない）。")
        # 本人確認できたのでここで初めて「検証済み」にする
        record_owned(user_receipt(admin), "", verified=True)
        print("  パスワード一致を確認（権限は未付与）→ 台帳を検証済みに")

    _try(cur, f"GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO {schema}")
    _try(cur, f"ALTER USER {schema} QUOTA UNLIMITED ON DATA")
    for pkg in ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_PIPELINE", "DBMS_VECTOR_CHAIN"):
        _try(cur, f"GRANT EXECUTE ON {pkg} TO {schema}", ignore=("ORA-04043",))
    adb.append_acl(cur, schema)
    adb.enable_resource_principal(cur, schema)
    admin.commit()

    if not exists:
        # 所有の証拠（USER_ID・作成時刻・この run 固有のマーカー）。マーカー表を作るには
        # RESOURCE と割当が要るので、権限付与のあとに書く。
        conn = adb.connect(schema, pw)
        marker = new_marker()
        write_marker(conn, marker)
        conn.close()
        record_owned(user_receipt(admin), marker)
        print("  台帳に記録: USER_ID / 作成時刻 / マーカー")

    admin.close()
    print("\ndone")


if __name__ == "__main__":
    main()
