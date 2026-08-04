"""PREP-02 実環境 E2E ドライバ（共有 loop ADB / ap-osaka-1）。

問い: **Select AI のベクトル索引は、バケットに置かれた .xlsx をどう扱うのか**。
アプリ側の抽出（PREP-01）を通らない経路なので、実機で観測しないと分からない。
推測で「使えない」と表示している状態（`rag.SELECT_AI_EXTENSIONS`）を実測で置き換える。

    .venv/bin/python -u runs/<run-id>/e2e/driver.py {1|2|teardown}

    1        … 架空 xlsx + 対照 txt をバケットへ置き、索引を作って観測する（本題）
    2        … 索引を xlsx のみの場所にも作り、xlsx 単独でも成立するかを見る（切り分け）
    teardown … この run が作ったものだけを消す

構造・安全ゲートは `runs/2026-07-29T1605_RP-01/e2e/driver.py` を踏襲する（実績のある形）:
1. `_adb.assert_target()` … SQL 接続が `.env` の ADB_OCID と同一 ADB か。
2. `assert_loop_adb()`   … その ADB が承認済みの共有 loop ADB か（コンパートメント OCID 完全一致）。
3. `verify_owned()`      … 触る対象がこの run の作ったものか（receipt の USER_ID＋run 固有マーカー）。
作る資源はすべて run 固有名（`prep02<乱数4>`）。ADB は増やさない。
"""

import hashlib
import io
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time
import uuid

import oracledb

oracledb.defaults.fetch_lobs = False  # CLOB を str で受ける（jetuse_core.db と同じ）

ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
LEDGER = HERE / "created-resources.json"

RUN_TAG_KEY = "run_tag"
RP_CRED = "OCI$RESOURCE_PRINCIPAL"

EXPECT_ADB_NAME = "jetuse-loop-adb"
EXPECT_DB_NAME = "JETUSELOOP2"
EXPECT_COMPARTMENT = "dev"

CALL_TIMEOUT_MS = 600_000
INDEX_BUILD_TIMEOUT_MS = 900_000

LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"

# 架空データの合言葉。索引に入ったか / 検索で引けるかを一意に見分けるための目印。
XLSX_WORD = "ZEBRAFINCH"
TXT_WORD = "BLUEHERON"

sys.path.insert(0, str(ROOT / "ops"))
import _adb  # noqa: E402


# ------------------------------------------------------------------ 架空の検証データ
def fake_xlsx() -> bytes:
    """架空の xlsx（顧客データは持ち込まない）。本文がテキストとして読めたか判る合言葉入り。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "制約"
    rows = [
        ["項目", "値", "備考"],
        ["最大同時接続数", 100, f"合言葉 {XLSX_WORD}"],
        ["応答時間 SLA", "300ms", "95 パーセンタイル"],
    ]
    for r in rows:
        ws.append(r)
    ws2 = wb.create_sheet("改訂履歴")
    ws2.append(["版", "日付", "内容"])
    ws2.append(["v2.0", "2026-07-30", "架空の検証用ブック（PREP-02）"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def docs() -> dict[str, bytes]:
    """バケットへ置く検証用オブジェクト。名前は本番と同じ `{uuid}_{filename}` 形。"""
    tag = run_tag()
    xid, tid = str(uuid.UUID(int=0xA1)), str(uuid.UUID(int=0xB2))
    return {
        f"rag/{tag}/{xid}_prep02-fake-workbook.xlsx": fake_xlsx(),
        f"rag/{tag}/{tid}_prep02-control.txt": (
            "JetUse PREP-02 の対照ドキュメント（プレーンテキスト）。\n"
            f"この文書の合言葉は {TXT_WORD} である。\n"
            "同じ索引に xlsx が混ざっても、この文書は取り込まれるはずである。\n"
        ).encode(),
    }


def xlsx_only_docs() -> dict[str, bytes]:
    """切り分け用: xlsx だけを置く別の場所（混在の影響を排して観測する）。"""
    tag = run_tag()
    xid = str(uuid.UUID(int=0xC3))
    return {f"rag/{tag}-xlsxonly/{xid}_prep02-fake-workbook.xlsx": fake_xlsx()}


# ------------------------------------------------------------------ 名前と台帳
def env(name: str, default: str = "") -> str:
    return _adb.env(name, default)


def ledger() -> dict:
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}


def write_ledger(data: dict) -> None:
    LEDGER.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def record(key: str, value) -> None:
    data = ledger()
    data[key] = value
    write_ledger(data)


def forget(key: str) -> None:
    data = ledger()
    if data.pop(key, None) is not None:
        write_ledger(data)


def run_tag() -> str:
    data = ledger()
    tag = data.get(RUN_TAG_KEY)
    if not tag:
        tag = "prep02" + secrets.token_hex(2)
        record(RUN_TAG_KEY, tag)
    return tag


def names() -> dict[str, str]:
    tag = run_tag()
    up = tag.upper()
    return {
        "dev": tag,
        "schema": f"JETUSE_{up}",
        "qry_schema": f"JETUSE_{up}_Q",
        "marker": "PREP02_RUN_MARKER",
        "bucket": f"jetuse-spike-{tag}-rag",
        "profile": f"JETUSE_SPIKE_{up}_PROF",
        "index": f"JETUSE_SPIKE_{up}_IDX",
        "profile_x": f"JETUSE_SPIKE_{up}_PROFX",
        "index_x": f"JETUSE_SPIKE_{up}_IDXX",
    }


def oci_args() -> dict:
    from jetuse_core.oci_auth import sdk_signer_args

    args = sdk_signer_args(env("OCI_REGION"))
    args.setdefault("config", {})
    args["config"] = {**args["config"], "region": env("OCI_REGION")}
    return args


# ------------------------------------------------------------------ 接続ゲート
def wallet_dir() -> str:
    """ウォレット置き場。パスに OCID を出さない（証跡はコミットされる）。"""
    tag = hashlib.sha256(env("ADB_OCID").encode()).hexdigest()[:12]
    return f"/tmp/prep02_wallet_{tag}"


def _write_secret(path: pathlib.Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def ensure_wallet() -> None:
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
    """認証資材は残さない。ただし片付け未完了ならスキーマのパスワードだけ残す。"""
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
    import oci

    if _loop_adb:
        return _loop_adb
    args = oci_args()
    approved = approved_compartment_ocid(oci.identity.IdentityClient(**args))
    adb = oci.database.DatabaseClient(**args).get_autonomous_database(env("ADB_OCID")).data
    if adb.compartment_id != approved:
        sys.exit("ADB が承認済みコンパートメントに無い。中止。")
    if adb.display_name != EXPECT_ADB_NAME:
        sys.exit(f"想定外の ADB {adb.display_name}（想定 {EXPECT_ADB_NAME}）。中止。")
    os.environ["ADB_COMPARTMENT_OCID"] = approved
    _loop_adb.update(compartment_ocid=approved, adb_name=adb.display_name)
    print(f"  対象 ADB: {adb.display_name} / jetuse:{EXPECT_COMPARTMENT}（OCID 一致）/"
          f" {adb.lifecycle_state}")
    return _loop_adb


def schema_pw(name: str) -> str:
    ensure_wallet()
    path = pathlib.Path(wallet_dir()) / f".pw_{name.lower()}"
    if not path.exists():
        _write_secret(path, "Gx" + secrets.token_hex(8) + "Ab#7")
    return path.read_text()


def connect(user: str, password: str) -> oracledb.Connection:
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


# ------------------------------------------------------------------ 所有権
def receipt_path() -> str:
    return str(pathlib.Path(wallet_dir()) / "setup-receipt.json")


def read_receipts() -> dict[str, dict]:
    p = pathlib.Path(receipt_path())
    return {e["user"]: e for e in json.loads(p.read_text())} if p.exists() else {}


def user_created_at(cur, user: str) -> str | None:
    cur.execute("SELECT TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') FROM dba_users"
                " WHERE username = :u", u=user)
    row = cur.fetchone()
    return row[0] if row else None


def user_id(cur, user: str) -> int | None:
    cur.execute("SELECT user_id FROM dba_users WHERE username = :u", u=user)
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


def capture_ownership() -> None:
    """setup が CREATE 直後に書いた receipt だけを根拠に所有権を台帳へ入れる。"""
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
                if now is not None and data[key].get("user_id") not in (None, now):
                    sys.exit(f"{user} の USER_ID が台帳と違う。触らずに中止。")
                continue
            if not r.get("created_by_this_run"):
                sys.exit(f"{user} は receipt 上『この run が作ったものではない』。中止。")
            if now is None:
                print(f"  {user} は receipt にあるが現在は存在しない（削除済み）")
                continue
            if now != r.get("user_id"):
                sys.exit(f"{user} の USER_ID が receipt と違う。中止。")
            data[key] = {"name": user, "created": r.get("created_at"), "user_id": now}
            print(f"  台帳に記録: {user}（user_id={now} / 作成時刻 {r.get('created_at')}）")
        write_ledger(data)
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
    n = names()
    entry = (ledger().get("schema") or {})
    if entry.get("name") != n["schema"]:
        return False, "台帳に検証用スキーマの記録が無い"
    created = user_created_at(cur, n["schema"])
    if created is None:
        return False, f"{n['schema']} は存在しない"
    now_id = user_id(cur, n["schema"])
    if entry.get("user_id") is not None and now_id != entry["user_id"]:
        return False, f"{n['schema']} の USER_ID が台帳と違う＝作り直されている"
    if entry.get("created") != created:
        return False, f"{n['schema']} の作成時刻が台帳と違う（作り直されている）"
    marker = read_marker(cur)
    if not marker or marker != entry.get("marker"):
        return False, f"{n['schema']} のマーカーが台帳と一致しない"
    return True, f"{n['schema']} はこの run が作ったもの（USER_ID・作成時刻・マーカーが一致）"


def bucket_identity(osc, ns: str):
    import oci

    n = names()
    try:
        b = osc.get_bucket(ns, n["bucket"]).data
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return None
        raise
    return {"name": b.name, "etag": b.etag, "time_created": str(b.time_created)}


# ------------------------------------------------------------------ 準備
def ensure_schema() -> None:
    """検証用スキーマ（run 固有）を作り、Select AI の権限を付ける。"""
    n = names()
    if (ledger().get("schema") or {}).get("marker"):
        print(f"  検証用スキーマ再利用: {n['schema']}（台帳の所有証跡と一致を後段で照合）")
        return
    conn = admin()
    try:
        cur = conn.cursor()
        for user in (n["schema"], n["qry_schema"]):
            if user_created_at(cur, user) is not None:
                sys.exit(f"{user} が既に存在する。自分が作ったと確認できないため中止。")
    finally:
        conn.close()
    ensure_wallet()
    pathlib.Path(receipt_path()).unlink(missing_ok=True)
    try:
        run_ops("setup-dev-schema.py", "--dev", n["dev"],
                "--app-password", schema_pw(n["schema"]),
                "--query-password", schema_pw(n["qry_schema"]),
                "--receipt", receipt_path(), "--require-new")
        capture_ownership()
        run_ops("setup-select-ai.py", "--schema", n["schema"])
    finally:
        capture_ownership()


def ensure_bucket(osc, ns: str, comp: str) -> None:
    import oci

    n = names()
    found = bucket_identity(osc, ns)
    if found is None:
        try:
            made = osc.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(
                name=n["bucket"], compartment_id=comp)).data
            if not made.etag:
                sys.exit("create_bucket の応答に etag が無い。所有証跡を作れないため中止。")
            record("bucket", {"name": made.name, "etag": made.etag,
                              "time_created": str(made.time_created)})
        except oci.exceptions.ServiceError as e:
            if e.status == 409:
                sys.exit(f"バケット {n['bucket']} が同時に作られた（409）。中止。")
            raise
        except Exception:
            print(f"  バケット {n['bucket']} の作成結果が不確定。人間が確認すること。")
            raise
        print(f"  バケット作成: {n['bucket']}（etag を作成応答から記録）")
    elif ledger().get("bucket") == found:
        print(f"  バケット再利用: {n['bucket']}（台帳の etag/作成時刻と一致）")
    else:
        sys.exit(f"バケット {n['bucket']} が台帳の作成証跡と一致しない。中止。")
    if bucket_identity(osc, ns) != ledger().get("bucket"):
        sys.exit("書き込み直前の同一性照合に失敗。書き込まずに中止。")


def put_docs(osc, ns: str, objects: dict[str, bytes]) -> None:
    n = names()
    for name, body in objects.items():
        osc.put_object(ns, n["bucket"], name, body)
        print(f"  投入: {name}（{len(body)} bytes）")


def create_index(cur, profile: str, index: str, location: str, ledger_key: str) -> str | None:
    """プロファイルと索引を作る。**失敗したらエラー本文をそのまま返す**（観測が目的）。"""
    cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;", p=profile,
                a=json.dumps({"provider": "oci", "credential_name": RP_CRED,
                              "region": env("OCI_REGION"), "model": LLM,
                              "embedding_model": EMBED, "vector_index_name": index}))
    record(f"{ledger_key}_profile", profile)
    print(f"  CREATE_PROFILE: {profile}")
    record(ledger_key, index)  # 作成前に記録（途中で落ちても片付け対象にする）
    try:
        cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;", i=index,
                    a=json.dumps({"vector_db_provider": "oracle", "location": location,
                                  "object_storage_credential_name": RP_CRED,
                                  "profile_name": profile,
                                  "vector_distance_metric": "cosine",
                                  "chunk_size": 1024, "chunk_overlap": 128,
                                  "refresh_rate": 60}))
    except oracledb.DatabaseError as e:
        err = str(e).strip()
        print(f"  CREATE_VECTOR_INDEX: **失敗**\n----- エラー本文（そのまま）-----\n{err}\n-----")
        return err
    print(f"  CREATE_VECTOR_INDEX: {index}（成功）")
    return None


def wait_rows(cur, index: str, timeout_s: int = 420) -> int:
    """$VECTAB に行が入るまで待つ（入らないことの観測も結果なので、待って 0 を返す）。"""
    vectab = f"{index}$VECTAB"
    deadline = time.time() + timeout_s
    last = 0
    while time.time() < deadline:
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{vectab}"')
            last = cur.fetchone()[0]
            if last > 0:
                return last
        except oracledb.DatabaseError as e:
            last = -1
            print(f"    （まだ照会できない: {str(e).splitlines()[0]}）")
        time.sleep(10)
    return last


def dump_vectab(cur, index: str) -> None:
    """$VECTAB の中身を、**本文が読めているか判る形で**そのまま出す。"""
    vectab = f"{index}$VECTAB"
    cur.execute("SELECT column_name, data_type FROM user_tab_columns"
                " WHERE table_name = :t ORDER BY column_id", t=vectab)
    cols = cur.fetchall()
    print(f"  {vectab} の列: {cols}")
    names_ = {c for c, _ in cols}
    cur.execute(f"SELECT JSON_VALUE(attributes, '$.object_name') AS obj, COUNT(*)"
                f' FROM "{vectab}" GROUP BY JSON_VALUE(attributes, \'$.object_name\')'
                " ORDER BY 1")
    print("  オブジェクト別のチャンク数:")
    for obj, cnt in cur.fetchall():
        print(f"    {obj}: {cnt}")
    # 本文の列名は実装依存なので、実際の列から選ぶ（決め打ちしない）。
    text_col = next(
        (c for c in ("CONTENT", "EMBED_DATA", "CHUNK_DATA", "DATA", "TEXT") if c in names_), None)
    if not text_col:
        print("  本文列が見つからない（上の列一覧を参照）")
        return
    doc_col = "DOC_ID" if "DOC_ID" in names_ else "1"
    emb_col = "EMBED_ID" if "EMBED_ID" in names_ else "1"
    cur.execute(
        f"SELECT JSON_VALUE(attributes, '$.object_name'), {doc_col}, {emb_col},"
        f' SUBSTR({text_col}, 1, 2000), LENGTH({text_col}), attributes'
        f' FROM "{vectab}" ORDER BY 1, 2, 3')
    print(f"  チャンク本文（{text_col} の先頭 2000 文字）:")
    for obj, doc_id, embed_id, text, full_len, attrs in cur.fetchall():
        print(f"    attributes: {attrs} / 本文の全長={full_len}")
        printable = sum(1 for ch in (text or "") if ch.isprintable() or ch in "\n\t")
        total = len(text or "")
        ratio = (printable / total * 100) if total else 0
        print(f"\n    --- {obj} doc_id={doc_id} embed_id={embed_id}"
              f" / 長さ={total} / 印字可能文字 {ratio:.1f}% ---")
        print("    " + (text or "").replace("\n", "\n    "))


def ask(cur, profile: str, prompt: str, expect: str) -> bool:
    cur.execute("""SELECT DBMS_CLOUD_AI.GENERATE(
                     prompt => :q, profile_name => :p, action => 'narrate') FROM dual""",
                q=prompt, p=profile)
    answer = (cur.fetchone()[0] or "").strip()
    hit = expect in answer
    print(f"\n  Q: {prompt}\n  A: {answer[:1200]}\n  → 期待語 {expect} を含む: {hit}")
    return hit


# ---------------------------------------------------------------- シナリオ 1
def scenario_1() -> None:
    """架空 xlsx + 対照 txt を同じ場所に置き、索引を作って**実際にどうなるか**を観測する。"""
    import oci

    n = names()
    print("== シナリオ1: Select AI の索引が xlsx をどう扱うか（混在。本番と同じ形）==")
    comp = assert_loop_adb()["compartment_ocid"]
    ensure_schema()
    osc = oci.object_storage.ObjectStorageClient(**oci_args())
    ns = osc.get_namespace().data
    ensure_bucket(osc, ns, comp)
    put_docs(osc, ns, docs())

    location = (f"https://objectstorage.{env('OCI_REGION')}.oraclecloud.com"
                f"/n/{ns}/b/{n['bucket']}/o/rag/{n['dev']}")
    print(f"  location: <OBJECT_STORAGE>/b/{n['bucket']}/o/rag/{n['dev']}")
    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        conn.call_timeout = INDEX_BUILD_TIMEOUT_MS
        cur = conn.cursor()
        err = create_index(cur, n["profile"], n["index"], location, "vector_index")
        conn.commit()
        if err:
            print("\n観測: 索引作成そのものが失敗した（上のエラー本文が一次証拠）")
            return
        rows = wait_rows(cur, n["index"])
        print(f"\n  $VECTAB の行数: {rows}")
        if rows > 0:
            dump_vectab(cur, n["index"])
        hits = {
            "xlsx 由来": ask(cur, n["profile"],
                           "最大同時接続数の備考に書かれている合言葉は何か。", XLSX_WORD),
            "txt 由来": ask(cur, n["profile"], "対照ドキュメントの合言葉は何か。", TXT_WORD),
        }
        print(f"\n観測: 索引行数={rows} / 検索ヒット={hits}")
    finally:
        conn.close()


# ---------------------------------------------------------------- シナリオ 2
def scenario_2() -> None:
    """切り分け: xlsx だけの場所に索引を作る（混在の影響を排して xlsx 単独を見る）。"""
    import oci

    n = names()
    print("== シナリオ2: xlsx のみの場所に索引を作る（切り分け）==")
    comp = assert_loop_adb()["compartment_ocid"]
    ensure_schema()
    osc = oci.object_storage.ObjectStorageClient(**oci_args())
    ns = osc.get_namespace().data
    ensure_bucket(osc, ns, comp)
    put_docs(osc, ns, xlsx_only_docs())

    location = (f"https://objectstorage.{env('OCI_REGION')}.oraclecloud.com"
                f"/n/{ns}/b/{n['bucket']}/o/rag/{n['dev']}-xlsxonly")
    print(f"  location: <OBJECT_STORAGE>/b/{n['bucket']}/o/rag/{n['dev']}-xlsxonly")
    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        conn.call_timeout = INDEX_BUILD_TIMEOUT_MS
        cur = conn.cursor()
        err = create_index(cur, n["profile_x"], n["index_x"], location, "vector_index_x")
        conn.commit()
        if err:
            print("\n観測: xlsx 単独でも索引作成が失敗した（上のエラー本文が一次証拠）")
            return
        rows = wait_rows(cur, n["index_x"])
        print(f"\n  $VECTAB の行数: {rows}")
        if rows > 0:
            dump_vectab(cur, n["index_x"])
        hit = ask(cur, n["profile_x"], "最大同時接続数の備考に書かれている合言葉は何か。", XLSX_WORD)
        print(f"\n観測: xlsx 単独 索引行数={rows} / 合言葉ヒット={hit}")
    finally:
        conn.close()


# ---------------------------------------------------------------- シナリオ 3
def scenario_3() -> None:
    """アプリの実経路で、表示（取り込み状況バッジ）がシナリオ1の観測と一致することを示す。

    本番と同じ関数（`rag.add_file` / `rag_select_ai.ensure_profile` /
    `rag.attach_backend_status`）を、この run のスキーマとバケットに向けて実行する。
    """
    n = names()
    tag = n["dev"]
    ensure_schema()
    ensure_wallet()
    # 本番コードを **この run の隔離先** へ向ける（共有スキーマ・共有バケットは触らない）
    os.environ.update({
        "ADB_USER": n["schema"],
        "ADB_PASSWORD": schema_pw(n["schema"]),
        "ADB_QUERY_USER": n["qry_schema"],
        "ADB_QUERY_PASSWORD": schema_pw(n["qry_schema"]),
        "RAG_BUCKET": n["bucket"],
    })
    import oci

    comp = assert_loop_adb()["compartment_ocid"]
    osc = oci.object_storage.ObjectStorageClient(**oci_args())
    ns_os = osc.get_namespace().data
    ensure_bucket(osc, ns_os, comp)
    # GenerativeAI プロジェクトは承認済みの dev コンパートメントにある既存のものを使う
    # （`.env` の COMPARTMENT_OCID は親を指すのでアプリ側の自動解決が届かない。新規作成はしない）。
    projects = oci.pagination.list_call_get_all_results(
        oci.generative_ai.GenerativeAiClient(**oci_args()).list_generative_ai_projects, comp).data
    active = [p for p in projects if p.lifecycle_state == "ACTIVE"]
    if len(active) != 1:
        sys.exit(f"dev コンパートメントの ACTIVE な GenAI プロジェクトが {len(active)} 個。中止。")
    os.environ["PROJECT_OCID"] = active[0].id
    print(f"  GenAI プロジェクト: {active[0].display_name}（既存を使用・作成しない）")

    from jetuse_core import rag, rag_select_ai
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    s = get_settings()
    print(f"  向き先: schema={s.adb_user} / bucket={s.rag_bucket}")
    assert s.adb_user == n["schema"] and s.rag_bucket == n["bucket"], "隔離先に向いていない"

    owner = f"prep02-{tag}"
    print("== シナリオ3: アプリの実経路（アップロード → 索引 → バッジ）==")
    uploaded = {}
    for filename, content in (("prep02-fake-workbook.xlsx", fake_xlsx()),
                              ("prep02-control.md", f"合言葉は {TXT_WORD}。\n".encode())):
        row = rag.add_file(owner, filename, content)
        uploaded[filename] = row["id"]
        print(f"  add_file: {filename} -> id={row['id']} status={row['status']}")
    record("app_owner", owner)

    # 本番の索引作成関数をそのまま呼ぶ（location は rag/{owner}/ ＝ 原本バックアップ先）
    profile = rag_select_ai.ensure_profile(owner)
    index = rag_select_ai._names(owner)[1]
    record("app_profile", profile)
    record("app_index", index)
    print(f"  ensure_profile: profile={profile} / index={index}")

    ids = rag_select_ai.indexed_file_ids(owner)
    print(f"  索引に居る file_id: {sorted(ids)}")
    files = rag.attach_backend_status(owner, rag.list_files(owner))
    for f in files:
        print(f"  {f['filename']}: backends={f['backends']}")
    xlsx_id = uploaded["prep02-fake-workbook.xlsx"]
    badge = next(f["backends"]["select_ai"] for f in files if f["id"] == xlsx_id)
    print(f"\n観測: xlsx の select_ai バッジ = {badge}"
          f"（PREP-01 の実装なら拡張子だけを見て 'error' だった）")
    assert badge == "indexed", "索引に入っているのに indexed になっていない"

    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        cur = conn.cursor()
        dump_vectab(cur, index)
    finally:
        conn.close()


# ---------------------------------------------------------------- 片付け
def teardown() -> None:
    try:
        _teardown()
    finally:
        remaining = {k: v for k, v in ledger().items() if k != RUN_TAG_KEY}
        purge_wallet(keep_schema_passwords=bool(remaining))


def _teardown() -> None:
    import oci

    n = names()
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
            not_found = {20004, 942}
            for stmt, arg in ((drop_index, data.get("vector_index")),
                              (drop_index, data.get("vector_index_x")),
                              (drop_index, data.get("app_index")),
                              ("DBMS_CLOUD_AI.DROP_PROFILE(:p)", data.get("vector_index_profile")),
                              ("DBMS_CLOUD_AI.DROP_PROFILE(:p)",
                               data.get("vector_index_x_profile")),
                              ("DBMS_CLOUD_AI.DROP_PROFILE(:p)", data.get("app_profile"))):
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
            for key, table, col, name in (
                    ("vector_index", "user_tables", "table_name", f"{n['index']}$VECTAB"),
                    ("vector_index_x", "user_tables", "table_name", f"{n['index_x']}$VECTAB"),
                    ("vector_index_profile", "user_cloud_ai_profiles", "profile_name",
                     n["profile"]),
                    ("vector_index_x_profile", "user_cloud_ai_profiles", "profile_name",
                     n["profile_x"]),
                    *((("app_index", "user_tables", "table_name", f"{data['app_index']}$VECTAB"),)
                      if data.get("app_index") else ()),
                    *((("app_profile", "user_cloud_ai_profiles", "profile_name",
                        data["app_profile"]),) if data.get("app_profile") else ())):
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
        targets = []
        for key in ("schema", "query_schema"):
            entry = data.get(key) or {}
            user = entry.get("name")
            if not user or user not in (n["schema"], n["qry_schema"]):
                continue
            now = user_created_at(cur, user)
            if now is None:
                forget(key)
                print(f"  {user} は既に存在しない")
                continue
            now_id = user_id(cur, user)
            if entry.get("user_id") is not None and now_id != entry["user_id"]:
                failed.append(f"{user} の USER_ID が台帳と違う＝作り直されている。1 件も DROP しない")
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
                ok, why = verify_owned(cur)
            print(f"  DROP 直前の再照合: {why}")
            if not ok:
                failed.append(f"DROP 直前の再照合に失敗（{why}）。1 件も DROP しない")
            else:
                for key, user in targets:
                    now = user_created_at(cur, user)
                    entry = data.get(key) or {}
                    if now is None:
                        forget(key)
                        print(f"  {user} は既に存在しない")
                        continue
                    now_id = user_id(cur, user)
                    if entry.get("user_id") is not None and now_id != entry["user_id"]:
                        failed.append(f"{user} の USER_ID が DROP 直前に変わった。DROP しない")
                        continue
                    if now != entry.get("created"):
                        failed.append(f"{user} の作成時刻が DROP 直前に変わった。DROP しない")
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


def dump() -> None:
    """既にある索引の $VECTAB を再観測する（作り直さずに中身だけ見る）。"""
    n = names()
    conn = connect(n["schema"], schema_pw(n["schema"]))
    try:
        cur = conn.cursor()
        for index in (n["index"], n["index_x"]):
            cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t",
                        t=f"{index}$VECTAB")
            if cur.fetchone()[0] == 0:
                print(f"== {index}$VECTAB は存在しない ==")
                continue
            print(f"== {index}$VECTAB ==")
            dump_vectab(cur, index)
    finally:
        conn.close()


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"1": scenario_1, "2": scenario_2, "3": scenario_3, "dump": dump,
          "teardown": teardown}.get(what)
    if not fn:
        sys.exit(__doc__)
    fn()


if __name__ == "__main__":
    main()
