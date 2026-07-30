"""RP-01 実環境 E2E ドライバ（共有 loop ADB / ap-osaka-1）。

tasks/RP-01.md の E2E シナリオ 1〜4 を実 ADB に対して流し、証跡を同ディレクトリへ残す。
ADB は増やさず、共有 loop ADB の中に検証用スキーマ（run 固有名 `JETUSE_RP01xxxx`）を
作って隔離する。作る資源はすべて run 固有名なので、他の実行や他人の資源と名前で衝突しない。

    .venv/bin/python -u runs/<run-id>/e2e/driver.py {1|2|3|4|guard|teardown}

接続とセットアップは `ops/_adb.py`（本タスクの成果物）をそのまま使う＝検証が本番経路を通る。

fail-closed の三重ゲート:
1. `_adb.assert_target()` … SQL 接続が `.env` の ADB_OCID と**同一 ADB**か
   （DB_NAME のインスタンス固有トークンと ADB の接続文字列を突き合わせる）。
2. `assert_loop_adb()` … その ADB が**承認済みの共有 loop ADB**か。承認済みの根
   （`.env` の COMPARTMENT_OCID）の直下から `dev` の OCID を引き、ADB のコンパートメントと
   **OCID で完全一致**することを見る（名前はテナンシをまたいで一意でないので根拠にしない）。
3. `verify_owned()` … 触ろうとしているスキーマが**この run が作ったもの**か。
   台帳（created-resources.json）にユーザーの作成時刻と**この run 固有のマーカー**（乱数トークンを
   入れた表）を記録し、**破壊操作の前に ADMIN 接続で先に照合する**。一致しなければ何も消さない。
   `guard` サブコマンドがこの否定側（台帳が古いと消さない）を実機で確認する。
"""

import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

import oracledb

oracledb.defaults.fetch_lobs = False  # CLOB を str で受ける（jetuse_core.db と同じ）

ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
LEDGER = HERE / "created-resources.json"

# 触ってよい対象（これ以外は作らない・消さない）。
# 名前は **run ごとに一意** にする（`rp01<乱数4>`）。固定名だと「事前に無い」→「作る」の間に
# 同名を作られたとき、他人のものを自分の所有物として台帳に取り込みうる（TOCTOU）。
# 一意名なら衝突自体が起こらず、それでも既存なら中止する。
RUN_TAG_KEY = "run_tag"
RP_CRED = "OCI$RESOURCE_PRINCIPAL"

EXPECT_ADB_NAME = "jetuse-loop-adb"
EXPECT_DB_NAME = "JETUSELOOP2"
EXPECT_COMPARTMENT = "dev"
EXPECT_PARENT_COMPARTMENT = "jetuse"

# 無期限待ち(0)は使わない。応答が返らないと片付けまで到達できなくなる。
CALL_TIMEOUT_MS = 600_000
INDEX_BUILD_TIMEOUT_MS = 900_000

LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"


def docs() -> dict[str, str]:
    """検証用文書。オブジェクト名も run 固有パス（`rag/<run tag>/`）に置く。"""
    tag = run_tag()
    return {
        f"rag/{tag}/doc-a.txt": (
            "JetUse 検証用ドキュメント A。\n"
            "リソースプリンシパルの検証では、ADB 自身の身分で Object Storage を読む。\n"
            "この文書の合言葉は BLUEHERON である。\n"
        ),
        f"rag/{tag}/doc-b.txt": (
            "JetUse 検証用ドキュメント B。\n"
            "開発者の API キーを DB へ焼き込む方式は RP-01 で廃止された。\n"
            "この文書の合言葉は REDFALCON である。\n"
        ),
    }


sys.path.insert(0, str(ROOT / "ops"))
import _adb


def run_tag() -> str:
    """この run の識別子（`rp01xxxx`）。台帳に永続化し、全シナリオで共有する。"""
    data = ledger()
    tag = data.get(RUN_TAG_KEY)
    if not tag:
        tag = "rp01" + secrets.token_hex(2)
        record(RUN_TAG_KEY, tag)
    return tag


def names() -> dict[str, str]:
    """この run が作る資源の名前（すべて run 固有）。"""
    tag = run_tag()
    return {
        "dev": tag,
        "schema": f"JETUSE_{tag.upper()}",
        "qry_schema": f"JETUSE_{tag.upper()}_Q",
        "marker": "RP01_RUN_MARKER",
        "bucket": f"jetuse-spike-{tag}-rag",
        "bad_cred": f"JETUSE_SPIKE_{tag.upper()}_BADCRED",
        "profile": f"JETUSE_SPIKE_{tag.upper()}_PROF",
        "index": f"JETUSE_SPIKE_{tag.upper()}_IDX",
    }


def env(name: str, default: str = "") -> str:
    return _adb.env(name, default)


def oci_args() -> dict:
    from jetuse_core.oci_auth import sdk_signer_args
    args = sdk_signer_args(env("OCI_REGION"))
    args.setdefault("config", {})
    args["config"] = {**args["config"], "region": env("OCI_REGION")}
    return args


# ------------------------------------------------------------------ 接続ゲート
def wallet_dir() -> str:
    """ウォレットの展開先は ADB ごとに分ける（別 ADB のウォレットを掴まないため）。

    ディレクトリ名には **ADB_OCID そのものではなくハッシュ**を使う。パスは証跡ログに載るので、
    OCID の一部でも書き出すと「OCID をコミットしない」規約に反する。
    """
    tag = hashlib.sha256(env("ADB_OCID").encode()).hexdigest()[:12]
    return f"/tmp/rp01_wallet_{tag}"


def _write_secret(path: pathlib.Path, text: str) -> None:
    """秘密ファイルは所有者だけが読める形で作る（同一ホストの他ユーザーに見せない）。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def ensure_wallet() -> None:
    """mTLS ウォレットを ADB から生成して展開し、ops が読む環境変数を設定する（read-only 操作）。"""
    import io
    import zipfile

    import oci
    dest = pathlib.Path(wallet_dir())
    pw_file = dest / ".pw"
    if not ((dest / "tnsnames.ora").exists() and pw_file.exists()):
        pw = "Wx" + secrets.token_hex(8) + "#7"
        r = oci.database.DatabaseClient(**oci_args()).generate_autonomous_database_wallet(
            env("ADB_OCID"),
            oci.database.models.GenerateAutonomousDatabaseWalletDetails(
                generate_type="SINGLE", password=pw),
        )
        dest.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(dest, 0o700)
        zipfile.ZipFile(io.BytesIO(r.data.content)).extractall(dest)
        for p in dest.iterdir():
            os.chmod(p, 0o600)
        _write_secret(pw_file, pw)
        print("  ウォレット生成: <WALLET_DIR>（0700・秘密ファイルは 0600）")
    os.environ["ADB_WALLET_DIR"] = str(dest)
    os.environ["ADB_WALLET_PASSWORD"] = pw_file.read_text()
    os.environ["ADB_DSN"] = f"{EXPECT_DB_NAME.lower()}_low"


def purge_wallet(keep_schema_passwords: bool = False) -> None:
    """認証資材を消す（検証が終わったら /tmp に残さない）。

    ウォレットは ADB から作り直せるので常に消す。ただし**検証用スキーマのパスワードは
    作り直せない**（DB 側の実パスワード）ので、片付けが未完了のときだけ残す。
    消してしまうと残ったスキーマへ接続できず、二度と片付けられなくなる。
    """
    import shutil
    dest = pathlib.Path(wallet_dir())
    if not dest.exists():
        return
    if not keep_schema_passwords:
        shutil.rmtree(dest)
        print("  ウォレットとパスワードファイルを削除")
        return
    for p in dest.iterdir():
        if not p.name.startswith(".pw_"):
            p.unlink()
    print("  ウォレットを削除（片付けが未完了のためスキーマのパスワードだけ残す）")


_loop_adb: dict[str, str] = {}


def approved_compartment_ocid(idc) -> str:
    """検証が触ってよいコンパートメントの OCID を、**承認済みの根から**解決する。

    `.env` の `COMPARTMENT_OCID`（承認済みの根）の直下から名前が `dev` の子を引く。
    ADB 側から逆算すると「対象が正しいか」の判定に対象自身を使うことになる（循環）ので、
    根と期待名だけから求める。
    """
    import oci
    root = env("COMPARTMENT_OCID")
    if not root:
        sys.exit(".env の COMPARTMENT_OCID が未設定。承認済みの根を決められないため中止。")
    children = oci.pagination.list_call_get_all_results(
        idc.list_compartments, root, lifecycle_state="ACTIVE").data
    match = [c for c in children if c.name == EXPECT_COMPARTMENT]
    if len(match) != 1:
        sys.exit(f"承認済みの根の直下に {EXPECT_COMPARTMENT} が {len(match)} 個。中止。")
    return match[0].id


def assert_loop_adb() -> dict[str, str]:
    """`.env` の ADB_OCID が承認済みの共有 loop ADB を指していることを確認する。

    認可の根拠は **コンパートメント OCID の完全一致**（名前はテナンシをまたいで一意でない）。
    確認できた OCID は `ADB_COMPARTMENT_OCID` として ops サブプロセスへ渡し、
    ops 側の `_adb.assert_target()` も同じ値で照合する。
    """
    import oci
    if _loop_adb:
        return _loop_adb
    args = oci_args()
    idc = oci.identity.IdentityClient(**args)
    approved = approved_compartment_ocid(idc)
    adb = oci.database.DatabaseClient(**args).get_autonomous_database(env("ADB_OCID")).data
    if adb.compartment_id != approved:
        sys.exit("ADB が承認済みコンパートメント（承認済みの根の直下 "
                 f"{EXPECT_COMPARTMENT}）に無い。中止。")
    if adb.display_name != EXPECT_ADB_NAME:
        sys.exit(f"想定外の ADB {adb.display_name}（想定 {EXPECT_ADB_NAME}）。中止。")
    os.environ["ADB_COMPARTMENT_OCID"] = approved
    _loop_adb.update(compartment_ocid=approved, adb_name=adb.display_name)
    print(f"  対象 ADB: {adb.display_name} /"
          f" {EXPECT_PARENT_COMPARTMENT}:{EXPECT_COMPARTMENT}（OCID 一致）/"
          f" {adb.lifecycle_state}")
    return _loop_adb


def schema_pw(name: str) -> str:
    """検証用スキーマのパスワード。リポジトリ外(ウォレット置き場)に 0600 で置く。"""
    ensure_wallet()
    path = pathlib.Path(wallet_dir()) / f".pw_{name.lower()}"
    if not path.exists():
        _write_secret(path, "Gx" + secrets.token_hex(8) + "Ab#7")
    return path.read_text()


def connect(user: str, password: str) -> oracledb.Connection:
    """ops と同じ経路で接続し、ゲート 1・2（同一 ADB か・承認済み ADB か）を通す。"""
    assert_loop_adb()
    ensure_wallet()
    conn = _adb.connect(user, password)
    conn.call_timeout = CALL_TIMEOUT_MS
    try:
        _adb.assert_target(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def admin() -> oracledb.Connection:
    return connect("ADMIN", env("ADB_ADMIN_PASSWORD"))


def _mask(args: tuple[str, ...]) -> list[str]:
    """証跡ログへ出す表示用にパスワード引数を伏せる（runs/ はコミット対象）。"""
    out, hide = [], False
    for a in args:
        out.append("<PASSWORD>" if hide else a)
        hide = a.endswith("-password")
    return out


def run_ops(script: str, *args: str) -> None:
    ensure_wallet()
    print(f"\n$ .venv/bin/python ops/{script} {' '.join(_mask(args))}", flush=True)
    r = subprocess.run([sys.executable, f"ops/{script}", *args],
                       cwd=str(ROOT), env=dict(os.environ), check=False)
    if r.returncode != 0:
        sys.exit(f"{script} が exit {r.returncode} で失敗。中止。")


# ------------------------------------------------------------------ 所有権の台帳
def ledger() -> dict:
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}


def write_ledger(data: dict) -> None:
    LEDGER.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def record(key: str, value) -> None:
    data = ledger()
    data[key] = value
    write_ledger(data)


def forget(key: str) -> None:
    """削除できたものは台帳から落とす（stale 台帳で同名の別リソースを掴まないため）。"""
    data = ledger()
    if data.pop(key, None) is not None:
        write_ledger(data)


def user_created_at(cur, user: str) -> str | None:
    cur.execute("SELECT TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') FROM dba_users"
                " WHERE username = :u", u=user)
    row = cur.fetchone()
    return row[0] if row else None


def read_marker(cur) -> str | None:
    n = names()
    try:
        cur.execute(f"SELECT token FROM {n['schema']}.{n['marker']}")
    except oracledb.DatabaseError:
        return None
    row = cur.fetchone()
    return row[0] if row else None


def db_now() -> str:
    """DB 側の現在時刻。receipt が無いときの下限としてだけ使う（補助）。"""
    conn = admin()
    try:
        cur = conn.cursor()
        cur.execute("SELECT TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') FROM dual")
        return cur.fetchone()[0]
    finally:
        conn.close()


def receipt_path() -> str:
    """setup が CREATE 直後に書く receipt の場所（リポジトリ外・実行のたびに作り直す）。"""
    return str(pathlib.Path(wallet_dir()) / "setup-receipt.json")


def read_receipts() -> dict[str, dict]:
    p = pathlib.Path(receipt_path())
    if not p.exists():
        return {}
    return {e["user"]: e for e in json.loads(p.read_text())}


def user_id(cur, user: str) -> int | None:
    """`DBA_USERS.USER_ID`。DROP → 同名で再作成すると必ず変わるので、同一性の証明に使う。"""
    cur.execute("SELECT user_id FROM dba_users WHERE username = :u", u=user)
    row = cur.fetchone()
    return row[0] if row else None


def capture_ownership() -> None:
    """**setup が CREATE 直後に書いた receipt** をもとに所有権を台帳へ入れる。

    「いま存在するユーザー」を推測で取り込まない（他プロセスが作ったものを掴まないため）。
    receipt に `created_by_this_run=false` があれば、それは他者のものなので中止する。
    setup が途中で失敗していても receipt さえあれば安全に台帳化でき、teardown で消せる。
    """
    n = names()
    receipts = read_receipts()
    if not receipts:
        print("  receipt が無い（ユーザーは 1 つも作られていない）")
        return
    conn = admin()
    try:
        cur = conn.cursor()
        data = ledger()
        for key, user in (("schema", n["schema"]), ("query_schema", n["qry_schema"])):
            r = receipts.get(user)
            if not r:
                continue
            now = user_id(cur, user)
            if key in data:
                # 既に所有済み（2 回目の実行など）。同一性だけ確かめる。
                if now is not None and data[key].get("user_id") not in (None, now):
                    sys.exit(f"{user} の USER_ID が台帳と違う（{data[key]['user_id']} → {now}）。"
                             " 作り直されている＝別のユーザーなので触らずに中止。")
                continue
            if not r.get("created_by_this_run"):
                sys.exit(f"{user} は receipt 上『この run が作ったものではない』（実行前から存在）。"
                         " 他プロセスが作った可能性があるため触らずに中止。")
            if now is None:
                print(f"  {user} は receipt にあるが現在は存在しない（削除済み）")
                continue
            if now != r.get("user_id"):
                sys.exit(f"{user} の USER_ID が receipt と違う（{r.get('user_id')} → {now}）。"
                         " CREATE 後に作り直されている＝別のユーザーなので触らずに中止。")
            data[key] = {"name": user, "created": r.get("created_at"), "user_id": now}
            print(f"  台帳に記録: {user}（user_id={now} / 作成時刻 {r.get('created_at')}）")
        write_ledger(data)
        # マーカー表は user_id に対する二重の裏取り（DROP/再作成でも消えるので追加の証拠になる）
        if "schema" in data and not data["schema"].get("marker"):
            token = read_marker(cur)
            if token is None:
                try:
                    cur.execute(f"CREATE TABLE {n['schema']}.{n['marker']} (token VARCHAR2(64))")
                except oracledb.DatabaseError as e:
                    if getattr(e.args[0], "code", None) != 955:  # ORA-00955: 既に存在
                        raise
                token = secrets.token_hex(16)
                cur.execute(f"INSERT INTO {n['schema']}.{n['marker']} VALUES (:t)", t=token)
                conn.commit()
                print(f"  この run のマーカーを記録: {n['schema']}.{n['marker']}")
            data["schema"]["marker"] = token
            write_ledger(data)
    finally:
        conn.close()


def verify_owned(cur) -> tuple[bool, str]:
    """検証用スキーマが**この run が作ったもの**かを、破壊操作の前に照合する。"""
    n = names()
    entry = (ledger().get("schema") or {})
    if entry.get("name") != n["schema"]:
        return False, "台帳に検証用スキーマの記録が無い"
    created = user_created_at(cur, n["schema"])
    if created is None:
        return False, f"{n['schema']} は存在しない"
    now_id = user_id(cur, n["schema"])
    if entry.get("user_id") is not None and now_id != entry["user_id"]:
        return False, (f"{n['schema']} の USER_ID が台帳と違う"
                       f"（{entry['user_id']} → {now_id}）＝作り直されている")
    if entry.get("created") != created:
        return False, f"{n['schema']} の作成時刻が台帳と違う（作り直されている）"
    marker = read_marker(cur)
    if not marker or marker != entry.get("marker"):
        return False, f"{n['schema']} のマーカーが台帳と一致しない"
    return True, (f"{n['schema']} はこの run が作ったもの"
                  f"（USER_ID={now_id}・作成時刻・マーカーが一致）")


def bucket_identity(osc, ns: str):
    """バケットの「作った証拠」。名前だけでは再作成された別バケットと区別できない。"""
    n = names()
    import oci
    try:
        b = osc.get_bucket(ns, n["bucket"]).data
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return None
        raise
    return {"name": b.name, "etag": b.etag, "time_created": str(b.time_created)}


# ---------------------------------------------------------------- シナリオ 1
def scenario_1() -> None:
    """ENABLE_RESOURCE_PRINCIPAL の適用と冪等性（ops スクリプトを 2 回連続実行）。"""
    n = names()
    print("== シナリオ1: 検証用スキーマへの適用と冪等性 ==")
    conn = admin()
    cur = conn.cursor()
    for user in (n["schema"], n["qry_schema"]):
        if user_created_at(cur, user) is not None:
            conn.close()
            sys.exit(f"{user} が既に存在する。自分が作ったものと確認できないため中止"
                     "（先に teardown するか、別名で実施する）。")
    print(f"  実行前: {n['schema']} 存在=0 / {RP_CRED} EXECUTE={_adb.rp_granted(cur, n["schema"])}")
    conn.close()

    # 所有権の根拠は **setup が CREATE 直後に書く receipt**（user_id 入り）。
    # 「いま在るユーザー」を推測で取り込まないので、CREATE〜capture の間に他プロセスが
    # 同名を作り直しても user_id が合わずに止まる。receipt があれば setup が途中で
    # 失敗していても安全に台帳化できる（＝作ったものが孤児にならない）。
    ensure_wallet()
    pathlib.Path(receipt_path()).unlink(missing_ok=True)
    try:
        for i in (1, 2):
            print(f"\n---- setup-dev-schema.py {i} 回目 ----")
            # 1 回目は **新規作成のはず**。事前確認と実行の間に同名が作られていたら
            # `--require-new` により setup が ALTER も CREATE もせずに中止する。
            run_ops("setup-dev-schema.py", "--dev", n["dev"],
                    "--app-password", schema_pw(n["schema"]),
                    "--query-password", schema_pw(n["qry_schema"]),
                    "--receipt", receipt_path(),
                    *(["--require-new"] if i == 1 else []))
            capture_ownership()   # receipt をもとに台帳へ
            print(f"\n---- setup-select-ai.py {i} 回目 ----")
            run_ops("setup-select-ai.py", "--schema", n["schema"])
            conn = admin()
            cur = conn.cursor()
            granted = _adb.rp_granted(cur, n["schema"])
            created = user_created_at(cur, n["schema"])
            cur.execute("SELECT COUNT(*) FROM all_tables WHERE owner = :o", o=n["schema"])
            tables = cur.fetchone()[0]
            conn.close()
            assert ledger()["schema"]["created"] == created, "1 回目と別のユーザーになっている"
            print(f"  {i} 回目終了: {n['schema']} 作成時刻={created} / {RP_CRED} EXECUTE={granted} /"
                  f" スキーマ内の表={tables}（migrate 済み 14 + マーカー 1）")
            assert created and granted == 1 and tables > 14, "冪等性の判定に失敗"
    finally:
        # setup が途中で失敗しても、receipt にあるユーザーは「この run が作ったもの」なので
        # 台帳へ回収して teardown で消せるようにする（孤児を残さない）。
        # receipt が無ければ 1 つも作られていない＝取り込むものが無い。
        capture_ownership()
    print("\n判定: PASS（2 回連続実行しても成功。RP 付与・スキーマ・マイグレーション適用が保たれる）")


# ---------------------------------------------------------------- シナリオ 2
def send_request(cur, credential: str, url: str) -> int:
    status = cur.var(int)
    cur.execute("""
        DECLARE r DBMS_CLOUD_TYPES.RESP; BEGIN
          r := DBMS_CLOUD.SEND_REQUEST(credential_name => :c, uri => :u, method => 'GET');
          :s := DBMS_CLOUD.GET_RESPONSE_STATUS_CODE(r);
        END;""", c=credential, u=url, s=status)
    return status.getvalue()


def scenario_2() -> None:
    """OCI$RESOURCE_PRINCIPAL で Object Storage を叩いて 200 を得る。"""
    n = names()
    print("== シナリオ2: OCI$RESOURCE_PRINCIPAL で Object Storage 200 ==")
    comp = assert_loop_adb()["compartment_ocid"]
    ns, region = env("OS_NAMESPACE"), env("OCI_REGION")
    base = f"https://objectstorage.{region}.oraclecloud.com/n/{ns}/b/"
    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        cur = conn.cursor()
        cur.execute("SELECT owner, credential_name, enabled FROM all_credentials")
        print("  この接続から見える資格情報:", cur.fetchall())
        for label, url in (
            ("ListBuckets(dev コンパートメント)", f"{base}?compartmentId={comp}"),
            ("GetBucket(共有 SPA バケット)", f"{base}jetuse-dev-app-spa/"),
        ):
            status = send_request(cur, RP_CRED, url)
            print(f"  {label}: HTTP {status}")
            assert status == 200, f"{label} が 200 でない"
    finally:
        conn.close()
    print("判定: PASS（API キー資格情報を作らずに Object Storage を読めている）")


# ---------------------------------------------------------------- シナリオ 3
def scenario_3() -> None:
    """OCI$RESOURCE_PRINCIPAL で Select AI のベクトル索引を作り、検索する。"""
    n = names()
    import oci
    print("== シナリオ3: OCI$RESOURCE_PRINCIPAL でベクトル索引の作成と検索 ==")
    comp = assert_loop_adb()["compartment_ocid"]
    osc = oci.object_storage.ObjectStorageClient(**oci_args())
    ns = osc.get_namespace().data
    found = bucket_identity(osc, ns)
    if found is None:
        try:
            # **作成レスポンスそのもの**を所有証跡にする。作成後に名前で引き直すと、その間に
            # 削除→同名再作成された別バケットの etag を掴みうる。
            made = osc.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(
                name=n["bucket"], compartment_id=comp)).data
            if not made.etag:
                sys.exit("create_bucket の応答に etag が無い。所有証跡を作れないため中止"
                         "（作成済みなら人間が確認すること）。")
            record("bucket", {"name": made.name, "etag": made.etag,
                              "time_created": str(made.time_created)})
        except oci.exceptions.ServiceError as e:
            # **競合エラーでは絶対に採用しない**（他人が同名で作ったものを消しに行かない）。
            # 名前は run 固有なので普通は起こらないが、起きたら人間の確認へ回す。
            if e.status == 409:
                sys.exit(f"バケット {n['bucket']} が同時に作られた（409）。"
                         " 自分が作ったと確認できないので触らずに中止。")
            raise
        except Exception:
            # 結果が不確定（タイムアウト等）。作成されているかもしれないが台帳へは入れない。
            print(f"  バケット {n['bucket']} の作成結果が不確定。"
                  " 残っていないか人間が確認すること（自動削除はしない）。")
            raise
        print(f"  バケット作成: {n['bucket']}（etag を作成応答から記録）")
    elif ledger().get("bucket") == found:
        print(f"  バケット再利用: {n['bucket']}（台帳の etag/作成時刻と一致）")
    else:
        sys.exit(f"バケット {n['bucket']} が既にあるが台帳の作成証跡と一致しない。"
                 " 他人が作った / 作り直された可能性があるため中止。")
    # 書き込む直前にもう一度同一性を確かめる（作成応答〜投入の間に削除→同名再作成されたら、
    # 他人のバケットへ検証文書を書いてしまう。削除は取り消せても書き込みは取り消せない）。
    if bucket_identity(osc, ns) != ledger().get("bucket"):
        sys.exit(f"バケット {n['bucket']} が作成直後の証跡と一致しない。書き込まずに中止。")
    for name, body in docs().items():
        osc.put_object(ns, n["bucket"], name, body.encode())
    print(f"  文書投入: {len(docs())} 件")

    location = (f"https://objectstorage.{env('OCI_REGION')}.oraclecloud.com"
                f"/n/{ns}/b/{n['bucket']}/o/rag/{n['dev']}")
    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        conn.call_timeout = INDEX_BUILD_TIMEOUT_MS  # 索引構築は長い（無期限にはしない）
        cur = conn.cursor()
        cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;", p=n["profile"],
                    a=json.dumps({"provider": "oci", "credential_name": RP_CRED,
                                  "region": env("OCI_REGION"), "model": LLM,
                                  "embedding_model": EMBED, "vector_index_name": n["index"]}))
        record("profile", n["profile"])
        print(f"  CREATE_PROFILE: {n['profile']}（credential_name={RP_CRED}）")
        record("vector_index", n["index"])  # 作成前に記録（作成中に落ちても片付け対象にする）
        cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;", i=n["index"],
                    a=json.dumps({"vector_db_provider": "oracle", "location": location,
                                  "object_storage_credential_name": RP_CRED,
                                  "profile_name": n["profile"], "vector_distance_metric": "cosine",
                                  "chunk_size": 1024, "chunk_overlap": 128, "refresh_rate": 60}))
        conn.commit()
        print(f"  CREATE_VECTOR_INDEX: {n['index']}（object_storage_credential_name={RP_CRED}）")

        rows, deadline = 0, time.time() + 300
        while time.time() < deadline:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{n['index']}$VECTAB"')
                rows = cur.fetchone()[0]
                if rows > 0:
                    break
            except oracledb.DatabaseError:
                pass
            time.sleep(5)
        print(f"  索引の取り込み行数: {rows}")
        assert rows > 0, "ベクトル表に行が入らない（Object Storage 読み取りに失敗している）"
        cur.execute(
            f"SELECT DISTINCT JSON_VALUE(attributes, '$.object_name') FROM \"{n['index']}$VECTAB\"")
        print("  取り込み対象:", sorted(r[0] for r in cur.fetchall()))

        for prompt, expect in (("ドキュメントAの合言葉は何か。", "BLUEHERON"),
                               ("ドキュメントBの合言葉は何か。", "REDFALCON")):
            cur.execute("""SELECT DBMS_CLOUD_AI.GENERATE(
                             prompt => :q, profile_name => :p, action => 'narrate') FROM dual""",
                        q=prompt, p=n["profile"])
            answer = (cur.fetchone()[0] or "").strip()
            hit = expect in answer
            print(f"\n  Q: {prompt}\n  A: {answer[:600]}\n  → 期待語 {expect} を含む: {hit}")
            assert hit, f"検索結果に {expect} が現れない"
    finally:
        conn.close()
    print("\n判定: PASS（索引作成・取り込み・検索のすべてがリソースプリンシパルで成立）")


# ---------------------------------------------------------------- シナリオ 4
def scenario_4() -> None:
    """対照: 誤ったプロファイル由来の API キー資格情報だと ORA-20404 になる。"""
    n = names()
    print("== シナリオ4（対照）: 誤ったプロファイル由来の API キー資格情報 ==")
    comp = assert_loop_adb()["compartment_ocid"]
    ns, region = env("OS_NAMESPACE"), env("OCI_REGION")
    url = f"https://objectstorage.{region}.oraclecloud.com/n/{ns}/b/?compartmentId={comp}"

    # RP-01 で削除した旧パーサの再現（セクションを無視して全行を 1 つの dict に潰す）。
    # 複数プロファイルがある config では **最後のプロファイル** の値になる。
    lines = pathlib.Path("~/.oci/config").expanduser().read_text().splitlines()
    conf = dict(ln.replace(" ", "").split("=", 1) for ln in lines if "=" in ln)
    sections = [ln.strip("[]") for ln in lines if ln.startswith("[")]
    print(f"  ~/.oci/config のプロファイル: {sections}")
    print(f"  旧パーサが拾う値: 最後のプロファイル [{sections[-1]}] のもの")
    key = pathlib.Path(conf["key_file"]).expanduser().read_text()
    private_key = "".join(ln for ln in key.splitlines()
                          if ln and "-----" not in ln and ln != "OCI_API_KEY")

    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        cur = conn.cursor()
        record("bad_credential", n["bad_cred"])  # 作成前に記録（落ちても必ず片付ける）
        cur.execute("""
            BEGIN
              DBMS_CLOUD.CREATE_CREDENTIAL(credential_name => :n, user_ocid => :u,
                tenancy_ocid => :t, private_key => :k, fingerprint => :f);
            END;""", n=n["bad_cred"], u=conf["user"], t=conf["tenancy"],
            k=private_key, f=conf["fingerprint"])
        conn.commit()
        print(f"  資格情報作成: {n['bad_cred']}（CREATE_CREDENTIAL 自体は成功する＝ここでは気づけない）")
        try:
            status = send_request(cur, n["bad_cred"], url)
            result, ok = f"HTTP {status}（想定外）", False
        except oracledb.DatabaseError as e:
            result = str(e).splitlines()[0]
            ok = "ORA-20404" in result
        print(f"  同じ URL を {n['bad_cred']} で叩く: {result}")
        status_rp = send_request(cur, RP_CRED, url)
        print(f"  同じ URL を {RP_CRED} で叩く: HTTP {status_rp}")
        cur.execute("BEGIN DBMS_CLOUD.DROP_CREDENTIAL(:c); END;", c=n["bad_cred"])
        conn.commit()
        forget("bad_credential")
        cur.execute("SELECT COUNT(*) FROM user_credentials WHERE credential_name = :c", c=n["bad_cred"])
        remaining = cur.fetchone()[0]
        print(f"  片付け: {n['bad_cred']} を削除（残存 {remaining} 件。API キーを ADB に残さない）")
        assert ok and status_rp == 200 and remaining == 0, "対照が成立していない"
    finally:
        conn.close()
    print("判定: PASS（旧方式は ORA-20404 / 新方式は 200。本変更が原因を潰したことの対照）")


# ---------------------------------------------------------------- 否定テスト
def guard() -> None:
    """台帳が古い（＝別物を掴んでいる）とき、片付けが**何も壊さずに止まる**ことを実機で確認する。

    **シナリオ1の直後・シナリオ2〜4の前**に実行する（バケットや索引をまだ作っていない状態で
    やることで、「止まらなければ消えていたはずのもの」をユーザーとマーカーに絞って観測できる）。
    台帳を一時的に壊し、teardown が非ゼロ終了し、かつ検証用スキーマ・読取専用ユーザー・
    マーカーが無傷であることを確かめてから台帳を元に戻す。
    """
    n = names()
    print("== 否定テスト: 台帳が一致しないときは何も消さない ==")
    original = ledger()
    assert original.get("schema", {}).get("marker"), "先にシナリオ1を実行すること"

    cases = {
        "マーカーが違う": {**original,
                     "schema": {**original["schema"], "marker": secrets.token_hex(16)}},
        "作成時刻が違う": {**original,
                     "schema": {**original["schema"], "created": "1999-01-01 00:00:00"}},
        # 読取専用ユーザーだけがズレている場合。開始時のゲート（アプリスキーマの照合）は
        # 通ってしまうので、DROP USER 直前の再照合で止まらなければ他人のスキーマを消しうる。
        "読取専用ユーザーの作成時刻が違う": {
            **original,
            "query_schema": {**original["query_schema"], "created": "1999-01-01 00:00:00"}},
        # 同じ秒内に DROP → 同名で再作成された状況の再現。作成時刻では区別できないが、
        # USER_ID は必ず変わるので、これで「別のユーザー」だと判定できる。
        "アプリスキーマの USER_ID が違う（同秒での作り直し相当）": {
            **original, "schema": {**original["schema"], "user_id": -1}},
        "読取専用ユーザーの USER_ID が違う（同秒での作り直し相当）": {
            **original, "query_schema": {**original["query_schema"], "user_id": -1}},
    }
    try:
        for label, broken in cases.items():
            write_ledger(broken)
            r = subprocess.run([sys.executable, str(pathlib.Path(__file__)), "teardown"],
                               cwd=str(ROOT), env=dict(os.environ),
                               capture_output=True, text=True, check=False)
            reasons = [ln.strip() for ln in (r.stdout or "").splitlines()
                       if "照合" in ln or "DROP しない" in ln]
            print(f"\n  [{label}] teardown の exit={r.returncode}")
            for ln in reasons:
                print(f"      {ln}")
            assert r.returncode != 0, "台帳が一致しないのに teardown が成功してしまった"
            conn = admin()
            try:
                cur = conn.cursor()
                alive = {u: user_created_at(cur, u) is not None for u in (n["schema"], n["qry_schema"])}
                marker_alive = read_marker(cur) is not None
            finally:
                conn.close()
            print(f"  [{label}] ユーザー健在={alive} / マーカー健在={marker_alive}")
            assert all(alive.values()) and marker_alive, "止まったはずなのにリソースが消えている"
    finally:
        write_ledger(original)
        print("\n  台帳を元に戻した")
    print("判定: PASS（stale な台帳では破壊操作に入らない）")


# ---------------------------------------------------------------- 片付け
def teardown() -> None:
    """検証用に作ったものだけを消し、**消えたことを再照会して確認する**。

    最初に ADMIN 接続で所有権（作成時刻＋この run のマーカー）を照合し、一致しなければ
    **スキーマへの接続すらせずに中止する**。削除に失敗したものがあれば最後に非ゼロ終了する。
    """
    try:
        _teardown()
    finally:
        # 成否によらず認証資材を片付ける。ただし残作業があるときはスキーマのパスワードだけ
        # 残す（作り直せないので、消すと残ったスキーマを二度と片付けられなくなる）。
        # run タグは資源ではないので「残作業」に数えない。
        remaining = {k: v for k, v in ledger().items() if k != RUN_TAG_KEY}
        purge_wallet(keep_schema_passwords=bool(remaining))


def _teardown() -> None:
    n = names()
    import oci
    print("== 片付け ==")
    data, failed = ledger(), []
    conn = admin()
    try:
        cur = conn.cursor()
        owned, why = verify_owned(cur)
        print(f"  所有権の照合: {why}")
        if not owned and data.get("schema"):
            sys.exit("台帳と一致しないため、検証用スキーマには一切触れずに中止する。")
    finally:
        conn.close()

    if owned:
        conn = connect(n["schema"], schema_pw(n["schema"]))
        try:
            conn.call_timeout = INDEX_BUILD_TIMEOUT_MS
            cur = conn.cursor()
            drop_index = ("DBMS_CLOUD_AI.DROP_VECTOR_INDEX(index_name => :p,"
                          " include_data => TRUE, force => TRUE)")
            # 対照用の資格情報は「この run が作ったスキーマの中」なので所有権は自明。
            # 記録前に落ちて孤児になっていても消えるよう、台帳に無くても削除を試みる。
            # 「既に無い」を表す ORA コードだけを成功扱いにする。それ以外（権限不足・一時障害）は
            # 失敗として数え、台帳も残す＝次回の片付けで対象を見失わない。
            not_found = {20004, 942}
            for stmt, arg in ((drop_index, data.get("vector_index")),
                              ("DBMS_CLOUD_AI.DROP_PROFILE(:p)", data.get("profile")),
                              ("DBMS_CLOUD.DROP_CREDENTIAL(:p)", n["bad_cred"])):
                if not arg:
                    continue
                try:
                    cur.execute(f"BEGIN {stmt}; END;", p=arg)
                    print(f"  dropped {arg}")
                except oracledb.DatabaseError as e:
                    code, line = getattr(e.args[0], "code", None), str(e).splitlines()[0]
                    if code in not_found:
                        print(f"  skip {arg}: {line}")
                    else:
                        failed.append(f"{arg} の削除に失敗: {line}")
            conn.commit()
            # 削除後の再照会（DB 側）。ベクトル索引は実体の $VECTAB 表の有無で見る。
            # **不在を確認できたものだけ**台帳から落とす（照会不能なら残して次回の対象にする）。
            for key, table, col, name in (
                    ("vector_index", "user_tables", "table_name", f"{n['index']}$VECTAB"),
                    ("profile", "user_cloud_ai_profiles", "profile_name", n["profile"]),
                    ("bad_credential", "user_credentials", "credential_name", n["bad_cred"])):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = :n", n=name)
                    left = cur.fetchone()[0]
                except oracledb.DatabaseError as e:
                    failed.append(f"{table} 照会不能: {str(e).splitlines()[0]}（台帳は残す）")
                    continue
                print(f"  確認: {table} に {name} は {left} 件")
                if left:
                    failed.append(f"{name} が {table} に残っている")
                else:
                    forget(key)
        finally:
            conn.close()

    if data.get("bucket"):
        osc = oci.object_storage.ObjectStorageClient(**oci_args())
        ns = osc.get_namespace().data
        found = bucket_identity(osc, ns)
        if found != data["bucket"]:
            failed.append(f"バケット {n['bucket']} が台帳の作成証跡と一致しない（削除しない）")
        else:
            for obj in osc.list_objects(ns, n["bucket"]).data.objects:
                osc.delete_object(ns, n["bucket"], obj.name)
            osc.delete_bucket(ns, n["bucket"])
            forget("bucket")
            print(f"  dropped bucket {n['bucket']}")
        left = bucket_identity(osc, ns)
        print(f"  確認: バケット {n['bucket']} の存在={left is not None}")
        if left is not None:
            failed.append(f"バケット {n['bucket']} が残っている")

    conn = admin()
    try:
        cur = conn.cursor()
        # DROP USER の直前に**両ユーザーとも**もう一度所有権を照合する。
        # 索引やバケットの削除に時間がかかる間に同名ユーザーが作り直されうる（TOCTOU）ので、
        # 開始時の照合だけでは足りない。1 件でも合わなければ 1 件も消さない。
        targets = []
        for key in ("schema", "query_schema"):
            entry = data.get(key) or {}
            user = entry.get("name")
            if user not in (n["schema"], n["qry_schema"]) or not user:
                continue
            now = user_created_at(cur, user)
            if now is None:
                forget(key)
                print(f"  {user} は既に存在しない")
                continue
            now_id = user_id(cur, user)
            if entry.get("user_id") is not None and now_id != entry["user_id"]:
                failed.append(f"{user} の USER_ID が台帳と違う（{entry['user_id']} → {now_id}）"
                              "＝作り直されている。1 件も DROP しない")
                targets = None
                break
            if entry.get("created") != now:
                failed.append(f"{user} の作成時刻が台帳と違う（作り直されている）。1 件も DROP しない")
                targets = None
                break
            targets.append((key, user))
        if targets:
            ok, why = True, "アプリスキーマは対象外"
            if any(u == n["schema"] for _, u in targets):
                ok, why = verify_owned(cur)   # アプリスキーマはマーカーも再照合する
            print(f"  DROP 直前の再照合: {why}")
            if not ok:
                failed.append(f"DROP 直前の再照合に失敗（{why}）。1 件も DROP しない")
            else:
                for key, user in targets:
                    # **1 件ごとに直前で**もう一度照合する。先行する DROP ... CASCADE の間に
                    # 対象が消えて作り直されている可能性があるため（app の削除 → query の削除の間）。
                    now = user_created_at(cur, user)
                    entry = data.get(key) or {}
                    if now is None:
                        forget(key)
                        print(f"  {user} は既に存在しない")
                        continue
                    now_id = user_id(cur, user)
                    if entry.get("user_id") is not None and now_id != entry["user_id"]:
                        failed.append(f"{user} の USER_ID が DROP 直前に変わった"
                                      f"（{entry['user_id']} → {now_id}）。DROP しない")
                        continue
                    if now != entry.get("created"):
                        failed.append(f"{user} の作成時刻が DROP 直前に変わった（作り直された）。DROP しない")
                        continue
                    if user == n["schema"]:
                        ok2, why2 = verify_owned(cur)
                        if not ok2:
                            failed.append(f"{user} の再照合に失敗（{why2}）。DROP しない")
                            continue
                    try:
                        cur.execute(f"DROP USER {user} CASCADE")
                        forget(key)
                        print(f"  dropped user {user}")
                    except oracledb.DatabaseError as e:
                        failed.append(f"{user}: {str(e).splitlines()[0]}")
        conn.commit()
        for user in (n["schema"], n["qry_schema"]):
            left = user_created_at(cur, user)
            print(f"  確認: ユーザー {user} の存在={left is not None}")
            if left is not None:
                failed.append(f"{user} が残っている")
    finally:
        conn.close()

    if failed:
        print("\n片付けに失敗したものがある:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    print("done（作ったものはすべて削除され、再照会でも見つからない）")


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"1": scenario_1, "2": scenario_2, "3": scenario_3, "4": scenario_4,
          "guard": guard, "teardown": teardown}.get(what)
    if not fn:
        sys.exit(__doc__)
    fn()


if __name__ == "__main__":
    main()
