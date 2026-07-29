"""開発者ごとのADBスキーマを共有ADBに作成する(再実行可)。

共有ADBに本人用のアプリスキーマ JETUSE_<DEV> と読取専用 JETUSE_<DEV>_Q を作り、
データセット機能(Select AI)に必要な権限・ネットワークACL・リソースプリンシパルを付与し、
最後にマイグレーションを本人スキーマへ適用する。

資格情報は ADB 自身の身分 OCI$RESOURCE_PRINCIPAL を使う。開発者の ~/.oci/config から
API キーを抜き出して DB へ焼き込む JETUSE_OCI_CRED は廃止した(ADR-0021)。

再実行時: 既に存在するユーザーのパスワードは**勝手に変えない**(配備済みの dev スタックが
接続できなくなるため)。パスワードを明示した場合だけ ALTER USER で合わせる。
権限・ACL・リソースプリンシパルは毎回そろえ直す(いずれも再適用して無害)。

採用方針 docs/guides/dev-environments.md / 設計 environments/app を参照。

実行: .venv/bin/python ops/setup-dev-schema.py --dev alice
前提: 共有ウォレット(ADB_WALLET_DIR・既定 /tmp/jetusedev_wallet)、.env の ADB_ADMIN_PASSWORD
"""

import argparse
import json
import os
import pathlib
import re
import secrets
import string
import subprocess
import sys

import _adb
import oracledb


def _gen_pw() -> str:
    # Oracleパスワード要件を満たす(英大小+数字+記号、先頭は英字)
    pool = string.ascii_letters + string.digits
    body = "".join(secrets.choice(pool) for _ in range(16))
    return "Gx" + body + "#7"


def _user_id(cur, user: str) -> int | None:
    """`DBA_USERS.USER_ID`。DROP して同名で作り直すと**必ず変わる**ので、同一性の証明に使える。"""
    cur.execute("SELECT user_id FROM dba_users WHERE username = :u", u=user)
    row = cur.fetchone()
    return row[0] if row else None


def _assert_receipt_writable(path: str) -> None:
    """receipt の出力先を **DDL の前に**検証する。

    親ディレクトリが無い・書けない・既存ファイルが壊れている、を後で踏むと
    「ユーザーだけできて作成証跡が残らない」＝呼び出し側が片付けられない状態になる。
    """
    if not path:
        return
    p = pathlib.Path(path)
    if not p.parent.is_dir():
        raise SystemExit(f"--receipt の親ディレクトリが無い: {p.parent}")
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"--receipt の既存ファイルが読めない/壊れている: {path}（{e}）") from e
        if not isinstance(data, list) or any(not isinstance(e, dict) for e in data):
            raise SystemExit(f"--receipt の既存ファイルが想定の形（dict の配列）でない: {path}")
    # 追記可否だけでなく **実際に書けるか**を試す（open できても write で落ちる環境がある）。
    try:
        probe = p.read_text() if p.exists() else None
        p.write_text(probe if probe is not None else "[]")
        if probe is None:
            p.unlink()
    except OSError as e:
        raise SystemExit(f"--receipt へ書き込めない: {path}（{e}）") from e


def _create_user_with_receipt(cur, user: str, pw: str) -> tuple[int | None, str | None]:
    """`CREATE USER` と識別子の取得を **1 回のサーバー往復**で行う。

    クライアント側で「作ってから読む」と、その往復の間に別セッションが DROP → 同名再作成を
    やった場合に別ユーザーの `USER_ID` を拾いうる。PL/SQL ブロックにまとめることで
    クライアント側の窓を無くす。

    **残る限界（既知・設計上）**: `CREATE USER` は暗黙コミットで、識別子は「オブジェクトが
    できた後」にしか読めない。よってコミット〜`SELECT` の間に DBA 権限を持つ別セッションが
    DROP → 同名再作成をした場合、その値を掴む可能性は原理的に消せない（クライアント側の
    どんな receipt 方式でも同じ）。緩和は 3 つ:
    ①呼び出し側は run ごとに一意な名前を使う ②`--require-new` は既存があれば何も作らずに中止
    ③receipt の `USER_ID` は破壊操作のたびに再照合するので、**この窓より後**の作り直しは必ず捕まる。
    """
    uid, created = cur.var(int), cur.var(str)
    cur.execute(
        """
        BEGIN
          EXECUTE IMMEDIATE 'CREATE USER ' || :u || ' IDENTIFIED BY "' || :p || '"';
          SELECT user_id, TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS')
            INTO :id, :cr FROM dba_users WHERE username = :u;
        END;""",
        u=user, p=pw, id=uid, cr=created,
    )
    return uid.getvalue(), created.getvalue()


def _write_receipt(path: str, user: str, user_id: int | None, created_at: str | None,
                   created: bool) -> None:
    """作成の receipt を **CREATE の直後に**追記する（`--receipt` 指定時のみ）。

    後続（GRANT / ACL / リソースプリンシパル有効化 / migrate）が失敗しても、
    呼び出し側はこの receipt だけで「自分が作ったユーザー」を確定でき、安全に片付けられる。
    `USER_ID` を含めるので、後から同名で作り直されても照合で気づける。
    """
    if not path:
        return
    entry = {"user": user, "user_id": user_id, "created_at": created_at,
             "created_by_this_run": created}
    p = pathlib.Path(path)
    raw = p.read_text() if p.exists() else ""
    data = json.loads(raw) if raw.strip() else []
    data = [e for e in data if isinstance(e, dict) and e.get("user") != user] + [entry]
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"  receipt: {user}（user_id={user_id} / 作成={created}）")


def _ensure_user(cur, user: str, pw: str, explicit: bool, require_new: bool = False,
                 receipt: str = "") -> bool:
    """ユーザーを用意し、「このパスワードで入れると分かっているか」を返す。

    既存ユーザーのパスワードは明示指定(--app-password / --query-password)のときだけ上書きする。
    無指定の再実行で自動生成パスワードを流し込むと、そのスキーマで動いている dev スタックが
    黙って接続不能になる。

    `require_new` は「新規作成のはずだ」と分かっている呼び出し（自動検証など）向け。
    既に在ったら **ALTER せずに中止**する＝他人が同名で作ったものを掴んで書き換えない。
    """
    _adb.assert_password(user, pw)   # 呼び出し前に検証済みだが、単体で使われても崩れないように
    cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=user)
    exists = cur.fetchone()[0]
    if require_new and exists:
        raise SystemExit(
            f"{user} は既に存在する（--require-new 指定）。新規作成のつもりだったので"
            " 既存ユーザーには一切触れずに中止する。"
        )
    if exists == 0:
        uid, created_at = _create_user_with_receipt(cur, user, pw)
        # **CREATE の直後**に receipt を残す。ここから先で落ちても呼び出し側が片付けられる。
        _write_receipt(receipt, user, uid, created_at, created=True)
        print(f"  created {user}")
        return True
    # 既存＝自分は作っていない
    cur.execute("SELECT TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') FROM dba_users"
                " WHERE username = :u", u=user)
    row = cur.fetchone()
    _write_receipt(receipt, user, _user_id(cur, user), row[0] if row else None, created=False)
    if not explicit:
        print(f"  {user} は既存。パスワードは変更しない(変えるなら明示指定する)")
        return False
    try:
        cur.execute(f'ALTER USER {user} IDENTIFIED BY "{pw}"')
        print(f"  {user} は既存。指定パスワードに更新")
    except oracledb.DatabaseError as e:
        # ORA-28007: 既定プロファイルはパスワードの再利用を拒否する(FIX-58 の知見)。
        # ただしこれは「履歴にある」であって「現在値」とは限らないので、実ログインで裏取りする。
        # 推測のまま known=True を返すと、配備用に案内したパスワードで接続できない。
        if getattr(e.args[0], "code", None) != 28007:
            raise
        if not _adb.verify_login(user, pw):
            raise SystemExit(
                f"{user}: 指定パスワードは再利用できず(ORA-28007)、そのパスワードでログインも"
                " できない。別のパスワードを指定するか、現行パスワードを指定して再実行する。"
            ) from e
        print(f"  {user} は既存。指定パスワードは現行値(ORA-28007・実ログインで確認)")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True, help="開発者識別子(英小文字)。例: alice")
    ap.add_argument("--app-password", default="", help="JETUSE_<DEV>のパスワード(未指定で自動生成)")
    ap.add_argument("--query-password", default="", help="JETUSE_<DEV>_Qのパスワード(未指定で自動生成)")
    ap.add_argument("--skip-migrate", action="store_true", help="マイグレーション実行を省略")
    ap.add_argument("--require-new", action="store_true",
                    help="新規作成のはずのとき用。既存ユーザーがあれば一切触れずに中止する")
    ap.add_argument("--receipt", default="",
                    help="作成 receipt(JSON)の出力先。CREATE 直後に user_id 付きで追記する"
                         "（自動検証が『自分が作ったもの』を後続失敗時にも確定するために使う）")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9]{1,12}", args.dev):
        raise SystemExit("--dev は英小文字+数字、先頭英字、2〜13文字")

    app_user = _adb.assert_schema(f"JETUSE_{args.dev.upper()}")
    qry_user = _adb.assert_schema(f"{app_user}_Q")
    app_pw = args.app_password or _gen_pw()
    qry_pw = args.query_password or _gen_pw()
    # 両方まとめて DDL の前に検証する。片方だけ先に使うと、app を作った後に
    # query のパスワードで落ちて「片方だけできた」状態が残る。
    _adb.assert_password(app_user, app_pw)
    _adb.assert_password(qry_user, qry_pw)
    _assert_receipt_writable(args.receipt)   # DDL の前に出力先を確かめる
    def announce_generated_passwords() -> None:
        """自動生成したパスワードを **DDL の前** に出す（途中で落ちても失われないように）。

        ただしプリフライトより後に呼ぶ。中止するときに「設定していない値」を案内すると、
        使えない認証情報を渡すことになる。呼び出し側が渡した値は出さない
        （実行ログが証跡や CI に残るため）。
        """
        for label, pw, generated in (("ADB_PASSWORD       (JETUSE app) ", app_pw,
                                      not args.app_password),
                                     ("ADB_QUERY_PASSWORD (JETUSE read)", qry_pw,
                                      not args.query_password)):
            if generated:
                print(f"  {label} = {pw}   ← これから設定する自動生成値（控えること）")

    print(f"== ADMIN: ensure {app_user} / {qry_user} ==")
    admin = _adb.connect("ADMIN", _adb.env("ADB_ADMIN_PASSWORD"))
    try:
        cur = admin.cursor()
        _adb.assert_target(admin)  # DDL の前に接続先 ADB を同定する（fail-closed）
        # プリフライト: 「app が既存でパスワード未指定、かつ migrate を求められている」なら
        # 何も作らずにここで止める。先に進むと読取専用ユーザーだけ作って落ち、
        # その自動生成パスワードが出力されないまま失われる。
        if args.require_new:
            # CLI の契約は「既存があれば一切触れずに中止」。app を作ってから query の検査で
            # 中止すると部分的なリソースが残るので、**2 ユーザーまとめて**先に見る。
            cur.execute("SELECT username FROM dba_users WHERE username IN (:a, :q)",
                        a=app_user, q=qry_user)
            found = [r[0] for r in cur.fetchall()]
            if found:
                raise SystemExit(
                    f"{', '.join(found)} が既に存在する（--require-new 指定）。"
                    " 新規作成のつもりだったので既存ユーザーには一切触れずに中止する。"
                )
        cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=app_user)
        if cur.fetchone()[0] and not args.app_password and not args.skip_migrate:
            raise SystemExit(
                f"{app_user} は既存だがパスワードが分からないため migrate を実行できない。"
                f" --app-password '<{app_user} の現行パスワード>' を付けて再実行するか、"
                " 意図的に飛ばすなら --skip-migrate を明示すること（何も変更していない）。"
            )
        announce_generated_passwords()
        # アプリスキーマ(CREATE TABLE/VIEW + データセットのSelect AI実行に必要なDBMS_CLOUD系)
        app_pw_known = _ensure_user(cur, app_user, app_pw, bool(args.app_password),
                                    require_new=args.require_new, receipt=args.receipt)
        admin.commit()   # receipt と DB の状態を合わせる（以降で落ちても作成は確定済み）
        cur.execute(f"GRANT CREATE SESSION, RESOURCE, CREATE VIEW TO {app_user}")
        cur.execute(f"ALTER USER {app_user} QUOTA UNLIMITED ON DATA")
        _adb.grant_cloud_packages(cur, app_user)
        _adb.append_acl(cur, app_user)
        # 読取専用ユーザー(CREATE SESSIONのみ。datasetsが個別表にSELECTを付与する)
        qry_pw_known = _ensure_user(cur, qry_user, qry_pw, bool(args.query_password),
                                    require_new=args.require_new, receipt=args.receipt)
        cur.execute(f"GRANT CREATE SESSION TO {qry_user}")
        # DBMS_CLOUD / DBMS_CLOUD_AI が使う資格情報(ADR-0021)。API キーは焼き込まない。
        _adb.enable_resource_principal(cur, app_user)
        admin.commit()
    finally:
        admin.close()

    # 自動生成したパスワードは **migrate より前に**出す。後に置くと migrate が失敗したときに
    # 作成済みユーザーのパスワードが分からなくなり「再実行できる」が嘘になる。
    # 呼び出し側が渡した値は出さない（実行ログが証跡や CI に残るため）。
    print("\n== 認証情報（infra/terraform/environments/app/<dev>.tfvars へ） ==")
    print(f"  adb_user       = \"{app_user}\"")
    print(f"  adb_query_user = \"{qry_user}\"")
    for label, user, pw, known, generated in (
        ("ADB_PASSWORD       (JETUSE app) ", app_user, app_pw, app_pw_known,
         not args.app_password),
        ("ADB_QUERY_PASSWORD (JETUSE read)", qry_user, qry_pw, qry_pw_known,
         not args.query_password),
    ):
        if not known:
            print(f"  {label} = <{user} の既存パスワードを流用>")
        elif generated:
            print(f"  {label} = {pw}")
        else:
            print(f"  {label} = <実行時に指定した値>")

    if not args.skip_migrate and not app_pw_known:
        # プリフライトで弾いているので通常ここには来ない（防御的な二重化）。
        raise SystemExit(
            f"{app_user} のパスワードが分からないため migrate を実行できない。"
            " --app-password を付けて再実行するか --skip-migrate を明示すること。"
        )
    if not args.skip_migrate:
        print(f"== migrate -> schema {app_user} ==")
        env = {
            **os.environ,
            "ADB_USER": app_user, "ADB_PASSWORD": app_pw,
            "ADB_DSN": _adb.dsn(),
            "ADB_WALLET_DIR": _adb.wallet_dir(),
            "ADB_WALLET_PASSWORD": _adb.wallet_password(),
        }
        r = subprocess.run(
            [sys.executable, "-m", "jetuse_core.migrate"],
            cwd=str(_adb.ROOT / "packages/api"), env=env, check=False,
        )
        if r.returncode != 0:
            raise SystemExit("migration failed")

    print("\n== done ==")
    print("次の手順: 上記の値を <dev>.tfvars の adb_user / adb_query_user /"
          " api_environment(ADB_PASSWORD, ADB_QUERY_PASSWORD) に設定")
    print("その後: ops/dev-env-up.sh", args.dev)


if __name__ == "__main__":
    main()
