"""SPIKE-M1 共通ヘルパ（接続・出力）。

JetUse 本体は変更しない（tasks/SPIKE-M1.md 非ゴール）ので、ウォレット取得は
`jetuse_core.db` の実装を再利用し、接続だけスパイク用に薄く用意する。
"""

import json
import os
import pathlib
import sys

import oracledb

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "JETUSE_SPIKE_M1"
CRED = "JETUSE_SPIKE_M1_CRED"       # DBMS_CLOUD 側
VEC_CRED = "JETUSE_SPIKE_M1_VCRED"  # DBMS_VECTOR_CHAIN 側（別ストア）


def load_env() -> dict[str, str]:
    """.env（gitignore 済み）を os.environ に載せ、**実効値**を返す。

    既存の環境変数を優先（setdefault）しつつ、戻り値も同じ実効値にする。
    jetuse_core.settings は os.environ を見るため、両者がずれると
    「接続先は環境変数・スキーマ名は .env」のような食い違いが起きる。
    """
    path = ROOT / ".env"
    if not path.exists():
        sys.exit(f"{path} がありません（ADB 接続情報が必要）")
    keys = []
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)
            keys.append(k)
    return {k: os.environ[k] for k in keys}


# スパイクが触ってよい対象（これ以外に何も作らない・消さない fail-closed ガード）。
# 実 OCID はコミットできないので、**名前で照合**する（OCID → 名前の解決は API で行う）。
EXPECT_DB_NAME = "JETUSELOOP2"        # 共有 loop ADB の db_name
EXPECT_ADB_DISPLAY_NAME = "jetuse-loop-adb"
# 共有 loop ADB がいるのは jetuse/dev（親 jetuse・子 dev）。
# 旧レイアウトのトップレベル "jetuse-dev" とは別物なので親まで照合する。
EXPECT_COMPARTMENT_NAME = "dev"
EXPECT_PARENT_COMPARTMENT_NAME = "jetuse"
EXPECT_REGION = "ap-osaka-1"
# テナンシまで固定する（別テナンシにも jetuse/dev がありうるため名前だけでは足りない）。
# OCID はコミットできないので、`.env` の COMPARTMENT_OCID が属するテナンシと
# `~/.oci/config` の tenancy が一致することを確認する＝「今使っている資格情報のテナンシ」に閉じる。

_compartment_checked = False
_target_checked = False


def assert_compartment() -> str:
    """`.env` の COMPARTMENT_OCID が承認済みコンパートメントかを API で名前解決して確認する。

    OCID をリポジトリに書けないので「OCID が合っているか」ではなく
    「その OCID の**名前**が jetuse-dev か」を見る。誤った .env で
    未承認コンパートメントへリソースを作らせないための門番。作成の前に必ず通す。
    """
    global _compartment_checked
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    env = load_env()
    if env.get("OCI_REGION") != EXPECT_REGION:
        sys.exit(f"想定外のリージョン {env.get('OCI_REGION')}（想定 {EXPECT_REGION}）。中止。")
    ocid = env.get("COMPARTMENT_OCID", "")
    if not ocid:
        sys.exit("COMPARTMENT_OCID が未設定。中止。")
    ident = oci.identity.IdentityClient(**sdk_signer_args(EXPECT_REGION))
    comp = ident.get_compartment(ocid).data
    tenancy = oci_api_key().get("tenancy", "")
    root = comp
    while root.compartment_id:
        root = ident.get_compartment(root.compartment_id).data
    if tenancy and root.id != tenancy:
        sys.exit("COMPARTMENT_OCID が ~/.oci/config のテナンシと別テナンシを指している。中止。")
    parent = ident.get_compartment(comp.compartment_id).data.name
    if comp.name != EXPECT_COMPARTMENT_NAME or parent != EXPECT_PARENT_COMPARTMENT_NAME:
        sys.exit(f"想定外のコンパートメント {parent}/{comp.name}"
                 f"（想定 {EXPECT_PARENT_COMPARTMENT_NAME}/{EXPECT_COMPARTMENT_NAME}）。"
                 " .env の COMPARTMENT_OCID を確認すること。中止。")
    if not _compartment_checked:
        print(f"  コンパートメント確認: {parent}/{comp.name} / region={EXPECT_REGION}")
        _compartment_checked = True
    return ocid


def client_args(region: str = EXPECT_REGION) -> dict:
    """OCI SDK クライアント引数。**リージョンを必ず引数の値にする**。

    `jetuse_core.oci_auth.sdk_signer_args()` は config_file モードでは
    引数の region を無視し `~/.oci/config` のプロファイル値を使う（docstring 記載の仕様）。
    そのまま使うと ~/.oci/config が別リージョンのとき検証リソースが想定外の場所にできる。
    """
    from jetuse_core.oci_auth import sdk_signer_args

    args = sdk_signer_args(region)
    args.setdefault("config", {})
    args["config"] = {**args["config"], "region": region}
    return args


def db_identity(conn: oracledb.Connection) -> str:
    """この接続の DB を一意に指す文字列。

    台帳の ID に使う（スキーマ名だけでは別 ADB の同名ユーザーと衝突する）。
    """
    cur = conn.cursor()
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'DB_NAME') FROM dual")
    return (cur.fetchone()[0] or "").upper()


def assert_target(conn: oracledb.Connection) -> None:
    """接続先が「承認済みコンパートメントの共有 loop ADB」であることを実機に問い合わせて確認する。

    誤った / 古い .env を掴んだまま CREATE USER・GRANT・ACL・DROP を打つと
    未承認の共有リソースを壊す。DDL を打つ前に必ずここを通す（fail-closed）。
    db_name の末尾一致だけでは「同じ db_name の別 ADB」を弾けないので、
    `.env` の ADB_OCID を Database API で名前解決し、**表示名とコンパートメント**まで照合する。
    """
    global _target_checked
    import oci

    comp = assert_compartment()
    env = load_env()
    adb_ocid = env.get("ADB_OCID", "")
    if not adb_ocid:
        sys.exit("ADB_OCID が未設定。接続先 ADB を同定できないため中止。")
    adb = oci.database.DatabaseClient(**client_args()).get_autonomous_database(adb_ocid).data
    if adb.display_name != EXPECT_ADB_DISPLAY_NAME:
        sys.exit(f"想定外の ADB {adb.display_name}（想定 {EXPECT_ADB_DISPLAY_NAME}）。中止。")
    if adb.compartment_id != comp:
        sys.exit(f"ADB {adb.display_name} が承認済みコンパートメントの外にある。中止。")
    db_name = db_identity(conn)
    if db_name != EXPECT_DB_NAME and not db_name.endswith(f"_{EXPECT_DB_NAME}"):
        sys.exit(f"想定外の接続先 DB_NAME={db_name}（想定 {EXPECT_DB_NAME}）。"
                 " .env の ADB_OCID / ADB_DSN を確認すること。中止。")
    # ここまでは「ADB_OCID の ADB が正しい」と「SQL 接続先の DB 名が正しい」を別々に見ただけ。
    # ADB_DSN や使い回しのウォレットが別 ADB を指していても、その DB 名が同じなら通ってしまう。
    # ADB Serverless の DB_NAME は `<インスタンス固有トークン>_<db_name>` で、
    # そのトークンは当該 ADB の接続文字列にだけ現れる。両者を突き合わせて同一 ADB を確定する。
    token = db_name.rsplit("_", 1)[0].lower() if "_" in db_name else ""
    strings = adb.connection_strings
    candidates = list((getattr(strings, "all_connection_strings", None) or {}).values())
    candidates += list((getattr(strings, "profiles", None) or []) and
                       [p.value for p in strings.profiles] or [])
    for attr in ("high", "medium", "low", "dedicated"):
        val = getattr(strings, attr, None)
        if val:
            candidates.append(val)
    blob = " ".join(c for c in candidates if c).lower()
    if not token or not blob or token not in blob:
        sys.exit(f"SQL 接続先（DB_NAME={db_name}）が ADB_OCID の ADB "
                 f"{adb.display_name} と同一だと確認できない。"
                 " ADB_DSN / ウォレットが別 ADB を指している可能性がある。中止。")
    if not _target_checked:
        print(f"  接続先確認: {adb.display_name} / DB_NAME={db_name} / "
              f"compartment={EXPECT_COMPARTMENT_NAME}")
        _target_checked = True


def wallet_dir() -> str:
    """mTLS ウォレットの展開先。ADB_OCID から Database API で生成する（jetuse_core.db 再利用）。"""
    from jetuse_core import db
    from jetuse_core.settings import get_settings

    return db._wallet_dir(get_settings())


DEFAULT_CALL_TIMEOUT_MS = 600_000


def call_timeout_ms() -> int:
    """SQL 往復の上限（ms）。**import 時ではなく接続時**に解決する（.env を読んだ後にする）。

    無期限（0 / 負値）は許さない。DB 内埋め込みは外部 OCI を叩くため、
    障害時にスパイクが固まったままになる。
    """
    load_env()
    raw = os.environ.get("SPIKE_CALL_TIMEOUT_MS", str(DEFAULT_CALL_TIMEOUT_MS))
    try:
        ms = int(raw)
    except ValueError:
        sys.exit(f"SPIKE_CALL_TIMEOUT_MS が数値でない: {raw!r}")
    if ms <= 0:
        sys.exit(f"SPIKE_CALL_TIMEOUT_MS は正の値にすること（無期限待機は禁止）: {ms}")
    return ms


def connect(user: str, password: str) -> oracledb.Connection:
    env = load_env()
    wd = wallet_dir()
    conn = oracledb.connect(
        user=user, password=password, dsn=env["ADB_DSN"],
        config_dir=wd, wallet_location=wd, wallet_password=env["ADB_WALLET_PASSWORD"],
        tcp_connect_timeout=20.0,
    )
    conn.call_timeout = call_timeout_ms()
    return conn


def connect_admin() -> oracledb.Connection:
    """ADMIN 接続。**必ず接続先ガードを通す**（この接続では DDL を打つため）。"""
    conn = connect("ADMIN", load_env()["ADB_ADMIN_PASSWORD"])
    assert_target(conn)
    return conn


def connect_spike() -> oracledb.Connection:
    """スパイクスキーマ接続。ここでも DROP/CREATE を打つのでガードを通す。"""
    conn = connect(SCHEMA, load_env()["ADB_PASSWORD"])
    assert_target(conn)
    return conn


# --- 作成したリソースの台帳 -------------------------------------------------
# 「名前が一致するから消してよい」は危険（同名の無関係リソースを壊す）。
# 自分が作ったものだけを ID で覚えておき、teardown はこの台帳のものだけを消す。

RESOURCE_TAG = "spike-m1"


def _registry_path() -> pathlib.Path:
    """台帳の置き場。**証跡ディレクトリには置かない**。

    台帳は OCID を持つ運用状態であって読み物ではない。証跡側に置くと
    (a) OCID がコミット対象に乗る (b) 伏字化スクリプトが ID を壊して片付け不能になる、
    の両方を踏む（実際に両方踏んだ）。よって gitignore 済みの `.env` と同じ層に置く。
    """
    override = os.environ.get("SPIKE_REGISTRY_PATH")
    if override:
        path = pathlib.Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return ROOT / ".spike-m1-registry.json"


def dump_registry_names() -> None:
    """証跡用に**名前だけ**の写しを出す（OCID は書かない）。"""
    run_id = ""
    marker = ROOT / ".current_run_id"
    if marker.exists():
        run_id = marker.read_text().strip()
    if not run_id:
        return
    out = ROOT / "runs" / run_id / "e2e" / "created-resources-names.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    names = {k: [i["name"] for i in v] for k, v in registry().items()}
    out.write_text(json.dumps(names, ensure_ascii=False, indent=2) + "\n")


def registry() -> dict[str, list[dict]]:
    path = _registry_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def record_created(kind: str, ident: str, name: str) -> None:
    """自分が作ったリソースを台帳に足す（同じ ident は重複させない）。"""
    path = _registry_path()
    data = registry()
    items = data.setdefault(kind, [])
    if not any(i["id"] == ident for i in items):
        items.append({"id": ident, "name": name})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"  台帳に記録: {kind} {name}")
    dump_registry_names()


def is_ours(kind: str, ident: str) -> bool:
    return any(i["id"] == ident for i in registry().get(kind, []))


def forget_created(kind: str, ident: str) -> None:
    """削除に成功したものを台帳から落とす。

    落とさないと、同じ DB / コンパートメントで**別の誰かが同名を作り直した**ときに
    古い台帳のせいで「自分のもの」と誤認する。
    """
    data = registry()
    items = data.get(kind, [])
    left = [i for i in items if i["id"] != ident]
    if len(left) != len(items):
        data[kind] = left
        _registry_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))
        dump_registry_names()


def schema_key(conn: oracledb.Connection) -> str:
    """台帳における DB スキーマの ID。

    `DB_NAME:SCHEMA` だけでは足りない。DROP 後に**別人が同名で作り直した**場合、
    古い台帳が一致して他人のスキーマを ALTER/DROP しうる（stale ledger）。
    ユーザーの**作成時刻**まで含めて、作り直されたら別物になるようにする。
    """
    cur = conn.cursor()
    cur.execute("SELECT TO_CHAR(created, 'YYYYMMDDHH24MISS') FROM all_users WHERE username = :u",
                u=SCHEMA)
    row = cur.fetchone()
    created = row[0] if row else "absent"
    return f"{db_identity(conn)}:{SCHEMA}:{created}"


def require_owned_schema(conn: oracledb.Connection) -> None:
    """このスキーマが**自分が作ったもの**であることを台帳で確認する。

    `connect_spike()` は「接続先 ADB が合っているか」しか見ない。各スクリプトは
    単独実行できるので、ここを通さないと同名スキーマ内の他人のオブジェクトを
    DROP/ALTER しうる。DDL を打つスクリプトは必ず冒頭で呼ぶ。
    """
    if not is_ours("db_schema", schema_key(conn)):
        sys.exit(f"スキーマ {SCHEMA} が台帳に無い。setup_schema.py で自分が作ったもの"
                 " でなければ触らない（他人の同名スキーマを壊さないため）。中止。")


def require_owned_bucket(bucket_ocid: str, bucket: str) -> None:
    if not is_ours("bucket", bucket_ocid):
        sys.exit(f"バケット {bucket} が台帳に無い。自分が作ったものでなければ触らない。中止。")


def require_owned_store(vs_id: str, name: str) -> None:
    if not is_ours("vector_store", vs_id):
        sys.exit(f"Vector Store {name} が台帳に無い。自分が作ったものでなければ触らない。中止。")


def oci_api_key(profile: str = "") -> dict[str, str]:
    """~/.oci/config から**指定プロファイルだけ**を読む。

    ops/setup-select-ai.py / setup-dev-schema.py の parser は全行を1つの dict に潰すため、
    複数プロファイルがある config では最後のプロファイルの値を拾ってしまう（実機で発覚:
    DBMS_CLOUD の全呼び出しが ORA-20404 = OCI の NotAuthorizedOrNotFound になる）。
    """
    want = (profile or os.environ.get("OCI_PROFILE") or "DEFAULT").upper()
    conf: dict[str, str] = {}
    cur_section = ""
    for raw in pathlib.Path("~/.oci/config").expanduser().read_text().splitlines():
        line = raw.strip()
        if line.startswith("["):
            cur_section = line.strip("[]").upper()
        elif "=" in line and cur_section == want:
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip()
    if not conf:
        sys.exit(f"~/.oci/config にプロファイル [{want}] がありません")
    key = pathlib.Path(conf["key_file"]).expanduser().read_text()
    # OCI_API_KEY マーカー行と PEM ヘッダの除去が必須（SPIKE-04 の実機知見）
    conf["private_key"] = "".join(
        ln for ln in key.splitlines() if ln and "-----" not in ln and ln != "OCI_API_KEY"
    )
    return conf


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n== {title}\n{'=' * 78}", flush=True)
