"""RAGM-02 の検証共通部（接続・タスク専用スキーマ・所有台帳）。

RAGM-01 と並行して走るため、**共有 loop ADB を増やさずスキーマだけで隔離**する。
スキーマ名は **run 固有**（`JETUSE_RAGM02_<乱数>`・オーケストレータ承認 2026-07-30）。
固定名だと「照合してから DROP するまでの間に別主体が同名で作り直す」窓を原理的に塞げず、
検証用の後片付けが他人の資産を消しうる（Codex review-3〜10 の B-001）。run 固有名なら
衝突自体が起こらないので、その窓が構造的に消える（SPIKE-M1 / RP-01 と同じ方針）。
それでも作成物は台帳に記録し、片付けは台帳と一致したものだけを消す。

秘密（生成したスキーマパスワード・ウォレットパスワード）はリポジトリ外の
`RAGM02_HOME`（既定 `/tmp/jetuse-ragm02`・0700）に置く。コミット対象に載せない。
"""

import json
import os
import pathlib
import secrets as _secrets
import stat
import sys

import oracledb

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import _adb as adb  # noqa: E402  ops の接続・fail-closed ゲートを再利用する

# 接頭辞と保管先は env で差し替えられる（**後続タスクがこの台帳ゲートごと再利用するため**。
# PREP-01 は SPIKE_SCHEMA_PREFIX=JETUSE_PREP01 / SPIKE_HOME=/tmp/jetuse-prep01 で使う）。
# 既定値は RAGM-02 のままなので、RAGM-02 の手順書はそのまま動く。
SCHEMA_PREFIX = os.environ.get("SPIKE_SCHEMA_PREFIX", "JETUSE_RAGM02")
HOME = pathlib.Path(
    os.environ.get("SPIKE_HOME") or os.environ.get("RAGM02_HOME", "/tmp/jetuse-ragm02")
)
EMBED_MODEL = "cohere.embed-multilingual-v3.0"
EMBED_DIM = 1024


def _schema_path() -> pathlib.Path:
    return home() / "schema.txt"


def resolve_schema() -> str:
    """この run のスキーマ名。env → `RAGM02_HOME/schema.txt` の順。未作成なら空。"""
    override = os.environ.get("SPIKE_SCHEMA") or os.environ.get("RAGM02_SCHEMA")
    if override:
        return override.strip().upper()
    path = _schema_path()
    return path.read_text().strip().upper() if path.exists() else ""


def new_schema_name() -> str:
    """run 固有のスキーマ名を作って保存する（setup だけが呼ぶ）。"""
    global SCHEMA
    SCHEMA = f"{SCHEMA_PREFIX}_{_secrets.token_hex(3).upper()}"
    _schema_path().write_text(SCHEMA + "\n")
    return SCHEMA


def require_schema() -> str:
    """この run のスキーマ名を返す。未作成なら止める（他のスクリプトが先に走れないように）。"""
    if not SCHEMA:
        sys.exit("この run のスキーマがまだ無い。先に setup_schema.py を実行すること。")
    return SCHEMA


def home() -> pathlib.Path:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    HOME.chmod(0o700)
    return HOME


SCHEMA = ""  # 実体は resolve_schema()（home() が使えるようになってから解決する）


def _secrets_path() -> pathlib.Path:
    return home() / "secrets.json"


def load_secrets() -> dict[str, str]:
    path = _secrets_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """同じディレクトリの一時ファイルへ書いて fsync してから `os.replace` する。

    台帳を直接 `write_text` で切り詰めると、途中で落ちたときに receipt が壊れ、
    片付けが所有を確認できなくなる（固定名スキーマが回収不能になる）。
    """
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_secret(key: str, value: str) -> str:
    data = load_secrets()
    data[key] = value
    _atomic_write(_secrets_path(), json.dumps(data, indent=2))
    return value


def secret(key: str, *, generate: bool = False) -> str:
    """秘密の取得。`generate=True` なら無いときだけ作って保存する。"""
    data = load_secrets()
    if data.get(key):
        return data[key]
    if not generate:
        return ""
    # ADB のパスワード規則: 12〜30 文字・大小英数字を含み `"` 不可
    value = "Rg2" + _secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")[:21] + "9"
    return save_secret(key, value)


# --- ウォレット / DSN ---------------------------------------------------------


def ensure_wallet() -> str:
    """mTLS ウォレットを `ADB_OCID` から生成して展開する（無ければ）。

    `.env` にウォレットパスワードが無い環境（この worktree）でも E2E できるよう、
    パスワードは自前で生成して `RAGM02_HOME` に保存する（PKCS12 の保護にしか使わない）。
    """
    import io
    import zipfile

    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    dest = home() / "wallet"
    if (dest / "tnsnames.ora").exists():
        return str(dest)
    pw = secret("wallet_password", generate=True)
    region = adb.env("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args.setdefault("config", {})
    args["config"] = {**args["config"], "region": region}
    db = oci.database.DatabaseClient(**args)
    resp = db.generate_autonomous_database_wallet(
        adb.env("ADB_OCID"),
        oci.database.models.GenerateAutonomousDatabaseWalletDetails(
            generate_type="SINGLE", password=pw
        ),
    )
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(resp.data.content)).extractall(dest)
    for f in dest.iterdir():
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return str(dest)


def resolve_dsn(wallet: str) -> str:
    """ウォレットの tnsnames.ora から `*_low` の別名を選ぶ。

    `.env` の `ADB_DSN` は共有 dev ADB 向けの既定値（`jetusedev_low`）で、
    loop ADB には存在しない。ウォレット実体から引くことで取り違えを防ぐ。
    """
    if os.environ.get("ADB_DSN"):
        return os.environ["ADB_DSN"]
    text = (pathlib.Path(wallet) / "tnsnames.ora").read_text()
    aliases = [ln.split("=", 1)[0].strip() for ln in text.splitlines()
               if "=" in ln and ln[:1].strip()]
    low = [a for a in aliases if a.lower().endswith("_low")]
    if not low:
        sys.exit(f"tnsnames.ora に *_low の別名が無い: {aliases[:5]}")
    return low[0]


DEV_COMPARTMENT_NAME = "dev"


def resolve_dev_compartment() -> str:
    """承認済みの根（`.env` の `COMPARTMENT_OCID`）**直下**の `dev` の OCID を引く。

    共有 loop ADB は `jetuse/dev` に居るが、`.env` の `COMPARTMENT_OCID` は親（`jetuse`）を
    指す。`ops/_adb.assert_target()` は「ADB が承認済みコンパートメント**そのもの**」を
    要求する（子孫を許すと兄弟環境まで通るため）ので、子の OCID を明示して渡す。
    根から名前で 1 段だけ辿る＝ADB 自身の申告を根拠にしない（それでは検査にならない）。
    """
    if os.environ.get("ADB_COMPARTMENT_OCID"):
        return os.environ["ADB_COMPARTMENT_OCID"]
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    approved = adb.env("COMPARTMENT_OCID")
    if not approved:
        sys.exit(".env の COMPARTMENT_OCID が未設定。承認済みの範囲を決められないため中止。")
    region = adb.env("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args.setdefault("config", {})
    args["config"] = {**args["config"], "region": region}
    idc = oci.identity.IdentityClient(**args)

    # dev ブランチ派生の作業では COMPARTMENT_OCID = jetuse:dev そのもの（2026-08-01 施主明言）。
    # ops/_adb.assert_target() も「COMPARTMENT_OCID そのもの」を要求するのでこちらが正。
    # 旧来の「COMPARTMENT_OCID は親で、直下の dev を探す」構成も受け入れる（両対応）。
    # いずれも **名前が dev であること**を要求する＝fail-closed は維持する。
    self_c = idc.get_compartment(approved).data
    if self_c.name == DEV_COMPARTMENT_NAME and self_c.lifecycle_state == "ACTIVE":
        os.environ["ADB_COMPARTMENT_OCID"] = approved
        return approved

    children = [
        c for c in idc.list_compartments(approved).data
        if c.name == DEV_COMPARTMENT_NAME and c.lifecycle_state == "ACTIVE"
    ]
    if len(children) != 1:
        sys.exit(
            f"COMPARTMENT_OCID 自身が {DEV_COMPARTMENT_NAME} でもなく、"
            f"その直下の {DEV_COMPARTMENT_NAME} も一意に定まらない。中止。"
        )
    os.environ["ADB_COMPARTMENT_OCID"] = children[0].id
    return children[0].id


def prepare_env() -> None:
    """`ops/_adb` が読む接続設定（ウォレット・DSN・認可先）を実体から決めて環境へ載せる。"""
    resolve_dev_compartment()
    wallet = ensure_wallet()
    os.environ["ADB_WALLET_DIR"] = wallet
    os.environ["ADB_WALLET_PASSWORD"] = secret("wallet_password")
    os.environ["ADB_DSN"] = resolve_dsn(wallet)


# --- 接続（DDL の前に必ず fail-closed ゲートを通す） --------------------------


def connect_admin() -> oracledb.Connection:
    prepare_env()
    conn = adb.connect("ADMIN", adb.env("ADB_ADMIN_PASSWORD"))
    adb.assert_target(conn)
    return conn


def connect_schema() -> oracledb.Connection:
    prepare_env()
    pw = secret("schema_password")
    if not pw:
        sys.exit(f"{SCHEMA} のパスワードが未保存。先に setup_schema.py を実行すること。")
    conn = adb.connect(SCHEMA, pw)
    adb.assert_target(conn)
    require_owned_schema(conn)
    return conn


# --- 所有台帳（自分が作ったスキーマだけを触る） ------------------------------
# スキーマ名は run 固有だが、それだけを根拠に「自分のもの」とはしない。
# 作った証拠（receipt）を 3 つ記録し、**破壊操作の直前に毎回**照合する:
#   ① USER_ID（Oracle が採番する内部 ID。作り直せば変わる）
#   ② 作成時刻（秒精度。単独では同秒の作り直しを見分けられない）
#   ③ この run 固有の乱数マーカー（スキーマ内のマーカー表に入れる）
# ①②だけだと「同じ秒に別人が同名で作り直した」を見分けられない（Codex B-001）。
# ③はスキーマの中にしか無いので、作り直された瞬間に必ず消える。

MARKER_TABLE = "RAGM02_OWNER_MARKER"


def _ledger_path() -> pathlib.Path:
    """台帳の位置。`RAGM02_LEDGER_PATH` で**台帳だけ**差し替えられる。

    否定シナリオのために `RAGM02_HOME` ごと複製すると、ウォレットと秘密（DB パスワード）まで
    一時ディレクトリに増える。差し替えたいのは台帳だけなので、そこだけ外に出す。
    """
    override = os.environ.get("RAGM02_LEDGER_PATH")
    return pathlib.Path(override) if override else home() / "ledger.json"


def user_receipt(conn: oracledb.Connection) -> dict[str, str]:
    """スキーマの同一性を示す値（DB / スキーマ / USER_ID / 作成時刻）を実機から読む。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, TO_CHAR(created, 'YYYYMMDDHH24MISS') FROM all_users WHERE username = :u",
        u=SCHEMA,
    )
    row = cur.fetchone()
    return {
        "db": adb.db_name(conn),
        "schema": SCHEMA,
        "user_id": str(row[0]) if row else "absent",
        "created": row[1] if row else "absent",
    }


def new_marker() -> str:
    return _secrets.token_hex(16)


def write_marker(conn: oracledb.Connection, marker: str) -> None:
    """マーカー表を作り直して、この run の乱数を入れる（作成側で 1 回だけ呼ぶ）。"""
    cur = conn.cursor()
    try:
        cur.execute(f"DROP TABLE {MARKER_TABLE} PURGE")
    except oracledb.DatabaseError as e:
        if "ORA-00942" not in str(e):
            raise
    cur.execute(f"CREATE TABLE {MARKER_TABLE} (marker VARCHAR2(64) PRIMARY KEY)")
    cur.execute(f"INSERT INTO {MARKER_TABLE}(marker) VALUES (:m)", m=marker)
    conn.commit()


def read_marker(conn: oracledb.Connection, *, qualified: bool = True) -> str:
    """マーカーを読む。ADMIN からは修飾名で、スキーマ自身の接続では自分の表から。"""
    cur = conn.cursor()
    table = f"{SCHEMA}.{MARKER_TABLE}" if qualified else MARKER_TABLE
    try:
        cur.execute(f"SELECT marker FROM {table}")
        row = cur.fetchone()
        return row[0] if row else ""
    except oracledb.DatabaseError:
        return ""


def record_owned(receipt: dict[str, str], marker: str, *, verified: bool = True) -> None:
    """所有を台帳へ書く。`verified=False` は「作った直後・まだ本人確認していない」印。

    未検証の receipt を所有証明として再利用すると、CREATE と確認の間に作り直された
    別主体の識別値を、次回実行で「一致」と見なしてしまう。
    """
    _atomic_write(_ledger_path(),
                  json.dumps({**receipt, "marker": marker, "verified": bool(verified)}, indent=2))


def purge_local_secrets() -> list[str]:
    """検証が完全に終わったあと、ローカルの認証資材と台帳を消す（片付け成功時のみ）。"""
    removed = []
    for path in (_secrets_path(), _ledger_path(), _schema_path()):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    wallet = home() / "wallet"
    if wallet.exists():
        for f in wallet.iterdir():
            f.unlink()
        wallet.rmdir()
        removed.append("wallet/")
    return removed


def ledger() -> dict[str, str]:
    path = _ledger_path()
    return json.loads(path.read_text()) if path.exists() else {}


def ownership_mismatch(conn: oracledb.Connection, *, marker: str | None = None) -> str:
    """台帳と実機がずれていたら理由を返す（一致なら空文字）。**破壊操作の直前に毎回呼ぶ。**"""
    recorded = ledger()
    if not recorded:
        return "台帳が無い（このスキーマを作ったのは自分ではない）"
    if not recorded.get("verified"):
        return ("台帳が未検証（作成直後に本人確認できていない）。"
                "パスワード一致の再確認が要る")
    actual = user_receipt(conn)
    for key in ("db", "schema", "user_id", "created"):
        if recorded.get(key) != actual.get(key):
            return f"{key} が不一致: 台帳={recorded.get(key)} / 実物={actual.get(key)}"
    if marker is None:
        return ""
    # 空同士の一致は許す（CREATE USER 直後・マーカー書き込み前に落ちた状態からの復旧）。
    # その状態でも USER_ID と作成時刻は照合済みなので、別人のスキーマは弾かれる。
    if marker != (recorded.get("marker") or ""):
        return (f"マーカーが不一致: 台帳={recorded.get('marker') or '(無し)'}"
                f" / 実物={marker or '(無し)'}")
    return ""


def require_owned_schema(conn: oracledb.Connection) -> None:
    """スキーマ接続でも 3 点（USER_ID / 作成時刻 / マーカー）をすべて照合する。"""
    reason = ownership_mismatch(conn, marker=read_marker(conn, qualified=False))
    if reason:
        sys.exit(f"スキーマ {SCHEMA} が台帳と一致しない（{reason}）。"
                 " 他タスクの資源を壊さないため中止する。")


def _init_schema() -> None:
    global SCHEMA
    SCHEMA = resolve_schema()


_init_schema()


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}", flush=True)
