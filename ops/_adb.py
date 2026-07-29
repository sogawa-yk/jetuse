"""ops の ADB セットアップ共通部（接続・権限・資格情報）。

DB の中で `DBMS_CLOUD` / `DBMS_CLOUD_AI` が使う資格情報は **`OCI$RESOURCE_PRINCIPAL` に統一**した
（ADR-0021）。開発者の `~/.oci/config` から API キーを抜き出して `DBMS_CLOUD.CREATE_CREDENTIAL`
で DB に焼き込む経路は廃止したので、このモジュールにも API キーを読む処理は無い。

接続先は環境変数 → `.env` → 既定値 の順に解決する（`ADB_DSN` / `ADB_WALLET_DIR` /
`ADB_WALLET_PASSWORD` / `ADB_CALL_TIMEOUT_MS`）。ウォレットのパスワードだけは従来どおり
`infra/terraform/environments/dev/terraform.tfvars` にも後方互換でフォールバックする。
DDL の前には `assert_target()`（対象 ADB の同一性と認可）を必ず通す。認可の照合先は
`ADB_COMPARTMENT_OCID`（無ければ `COMPARTMENT_OCID`）。
"""

import os
import pathlib
import re

import oracledb

ROOT = pathlib.Path(__file__).resolve().parent.parent
TFVARS_PATH = ROOT / "infra/terraform/environments/dev/terraform.tfvars"

RP_CRED = "OCI$RESOURCE_PRINCIPAL"
# データセット/Select AI に必要な PL/SQL パッケージ。
# DBMS_CLOUD_AI_AGENT が無いと Agent フレームワークが PLS-00201、
# DBMS_CLOUD_PIPELINE が無いとベクトル索引の同期が ORA-20000 になる（いずれも実機で発覚）。
CLOUD_PACKAGES = ("DBMS_CLOUD", "DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT", "DBMS_CLOUD_PIPELINE")
SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_]{1,29}")

_dotenv: dict[str, str] | None = None


def env(name: str, default: str = "") -> str:
    """環境変数を優先し、無ければ `.env`（gitignore 済み）を読む。"""
    global _dotenv
    if name in os.environ:
        return os.environ[name]
    if _dotenv is None:
        path = ROOT / ".env"
        text = path.read_text() if path.exists() else ""
        _dotenv = dict(
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
    return _dotenv.get(name) or default


def _tfvar(name: str) -> str:
    if not TFVARS_PATH.exists():
        return ""
    m = re.search(rf'{name}\s*=\s*"([^"]+)"', TFVARS_PATH.read_text())
    return m.group(1) if m else ""


def assert_schema(name: str) -> str:
    """スキーマ名を検証して大文字で返す（GRANT 等へ文字列連結するため)。"""
    upper = name.strip().upper()
    if not SCHEMA_RE.fullmatch(upper):
        raise SystemExit(f"不正なスキーマ名: {name!r}（英大文字始まり・英数字と _ のみ・2〜30文字）")
    return upper


def assert_password(user: str, pw: str) -> str:
    """パスワードを `IDENTIFIED BY "..."` へ埋める前に検証する。

    Oracle の引用識別子はダブルクォートで閉じられるため、`"` を含む値は SQL を壊す
    （壊れた先が別の DDL になりうる）。改行・空白も同様に弾く。バインド変数は
    CREATE/ALTER USER のパスワードには使えないので、ここで入口を絞る。
    """
    if not pw or len(pw) > 60:
        raise SystemExit(f"{user} のパスワードが空か長すぎる（1〜60 文字）")
    bad = [c for c in pw if c == '"' or c.isspace()]
    if bad:
        raise SystemExit(
            f"{user} のパスワードにダブルクォートまたは空白・改行が含まれている。"
            " これらは使えない（SQL の引用識別子を壊すため）。"
        )
    return pw


def dsn() -> str:
    return env("ADB_DSN", "jetusedev_low")


def wallet_dir() -> str:
    return env("ADB_WALLET_DIR", "/tmp/jetusedev_wallet")


def wallet_password() -> str:
    """ウォレットのパスワード。自動ログイン(cwallet.sso)だけのウォレットでは空でよい。"""
    return env("ADB_WALLET_PASSWORD") or _tfvar("ADB_WALLET_PASSWORD")


DEFAULT_CALL_TIMEOUT_MS = 600_000


def call_timeout_ms() -> int:
    """SQL 往復の上限（ms）。`tcp_connect_timeout` は接続確立しか縛らないので別に要る。

    無期限（0 以下）は許さない。GRANT / ACL / ENABLE_RESOURCE_PRINCIPAL が返らないと
    セットアップが黙って止まったままになる。
    """
    raw = env("ADB_CALL_TIMEOUT_MS", str(DEFAULT_CALL_TIMEOUT_MS))
    try:
        ms = int(raw)
    except ValueError as e:
        raise SystemExit(f"ADB_CALL_TIMEOUT_MS が数値でない: {raw!r}") from e
    if ms <= 0:
        raise SystemExit(f"ADB_CALL_TIMEOUT_MS は正の値にすること（無期限待機は禁止）: {ms}")
    return ms


def connect(user: str, password: str) -> oracledb.Connection:
    wallet = wallet_dir()
    wallet_pw = wallet_password()
    if not password:
        raise SystemExit(f"{user} のパスワードが未設定（.env の ADB_ADMIN_PASSWORD 等を確認）")
    conn = oracledb.connect(
        user=user, password=password, dsn=dsn(),
        config_dir=wallet, wallet_location=wallet, wallet_password=wallet_pw or None,
        tcp_connect_timeout=20.0,
    )
    conn.call_timeout = call_timeout_ms()
    return conn


def db_name(conn: oracledb.Connection) -> str:
    cur = conn.cursor()
    cur.execute("SELECT SYS_CONTEXT('USERENV', 'DB_NAME') FROM dual")
    return (cur.fetchone()[0] or "").upper()


def _connection_string_blob(adb) -> str:
    """ADB の接続文字列を全部つないだもの（インスタンス固有トークンの照合に使う）。"""
    strings = adb.connection_strings
    parts = list((getattr(strings, "all_connection_strings", None) or {}).values())
    parts += [p.value for p in (getattr(strings, "profiles", None) or [])]
    parts += [getattr(strings, a, None) for a in ("high", "medium", "low", "dedicated")]
    return " ".join(p for p in parts if p).lower()


def _assert_approved_compartment(idc, adb) -> str:
    """ADB が**承認済みコンパートメントそのもの**にあることを OCID の完全一致で確認する。

    照合先は `.env` の `ADB_COMPARTMENT_OCID`（無ければ `COMPARTMENT_OCID`）で、**完全一致**を要求する。
    「承認済みの配下ならどこでもよい」にはしない — 親を承認済みにしている環境では
    兄弟・子孫のコンパートメントまで許してしまうため（ADB は 1 つに絞る）。
    `ADB_COMPARTMENT_OCID` で上書きする場合も、それが `COMPARTMENT_OCID` の配下にあることを
    OCID で遡って確認する（上書きが別テナンシへの抜け穴にならないように）。
    名前（表示名・コンパートメント名）はテナンシをまたいで一意でないので根拠にしない。
    """
    root = env("COMPARTMENT_OCID")
    if not root:
        raise SystemExit(
            ".env の COMPARTMENT_OCID が未設定。承認済みの根を決められないため DDL を実行しない（中止）。"
        )
    approved = env("ADB_COMPARTMENT_OCID") or root
    if approved != root:
        # override は「承認済みの根の配下」に限る。これが無いと ADB_OCID・ウォレット・
        # プロファイル・override が揃った別テナンシがゲートを素通りする。
        current, hops = approved, 0
        while current and current != root and hops < 20:
            current = idc.get_compartment(current).data.compartment_id
            hops += 1
        if current != root:
            raise SystemExit(
                "ADB_COMPARTMENT_OCID が COMPARTMENT_OCID（承認済みの根）の配下にない。"
                " 別テナンシ / 別環境を指している可能性がある（中止）。"
            )
    if adb.compartment_id != approved:
        raise SystemExit(
            f"ADB {adb.display_name} が承認済みコンパートメント"
            "（.env の ADB_COMPARTMENT_OCID / COMPARTMENT_OCID）に無い。"
            " 別テナンシ / 別コンパートメントを指している可能性がある（中止）。"
        )
    return idc.get_compartment(adb.compartment_id).data.name


def assert_target(conn: oracledb.Connection) -> str:
    """**この SQL 接続が承認済みコンパートメント配下の `ADB_OCID` の ADB か**を実機で確認する。

    DDL（CREATE USER / GRANT / ACL / リソースプリンシパル付与）の前に必ず通す fail-closed ゲート。
    2 つを別々に見る:

    1. **同一性** — `ADB_DSN` やウォレットが古いまま別の ADB を指していないか。表示名の一致では
       「同名の別 ADB」を弾けないので、ADB Serverless の
       `DB_NAME = <インスタンス固有トークン>_<db_name>` のトークンが当該 ADB の接続文字列に
       現れることまで確認する。
    2. **認可** — その ADB が `.env` の `ADB_COMPARTMENT_OCID`（無ければ `COMPARTMENT_OCID`）
       そのものにあるか（OCID の完全一致。名前はテナンシをまたいで一意でないので根拠にせず、
       「承認済みの配下ならどこでも」も許さない）。ADB_OCID・ウォレット・OCI プロファイルが
       揃って別環境を指していても、ここで止まる。

    確認できなければ **表示だけして続行せず中止する**（SystemExit）。
    """
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    adb_ocid = env("ADB_OCID")
    if not adb_ocid:
        raise SystemExit(
            ".env の ADB_OCID が未設定。接続先 ADB を同定できないため DDL を実行しない（中止）。"
        )
    args = sdk_signer_args(env("OCI_REGION"))
    args.setdefault("config", {})
    if env("OCI_REGION"):
        args["config"] = {**args["config"], "region": env("OCI_REGION")}
    adb = oci.database.DatabaseClient(**args).get_autonomous_database(adb_ocid).data
    name = db_name(conn)
    token = name.rsplit("_", 1)[0].lower() if "_" in name else ""
    blob = _connection_string_blob(adb)
    if not token or token not in blob:
        raise SystemExit(
            f"SQL 接続先（DB_NAME={name}）が ADB_OCID の ADB {adb.display_name} と同一だと"
            " 確認できない。ADB_DSN / ウォレットが別の ADB を指している可能性がある（中止）。"
        )
    idc = oci.identity.IdentityClient(**args)
    comp_name = _assert_approved_compartment(idc, adb)
    print(f"  接続先確認: {adb.display_name} / compartment={comp_name}（承認済み）/"
          f" DB_NAME={name} / DSN={dsn()}")
    return name


def verify_login(user: str, password: str) -> bool:
    """その資格情報で実際にログインできるかを確かめる（推測でパスワードを案内しないため）。"""
    try:
        connect(user, password).close()
        return True
    except oracledb.DatabaseError:
        return False


def acl_hosts() -> list[str]:
    region = env("OCI_REGION", "ap-osaka-1")
    return [
        f"inference.generativeai.{region}.oci.oraclecloud.com",
        f"generativeai.{region}.oci.oraclecloud.com",
        f"objectstorage.{region}.oraclecloud.com",
    ]


def grant_cloud_packages(admin_cur, schema: str) -> None:
    for pkg in CLOUD_PACKAGES:
        admin_cur.execute(f"GRANT EXECUTE ON {pkg} TO {schema}")
    print(f"  grants: {', '.join(CLOUD_PACKAGES)} -> {schema}")


def append_acl(admin_cur, schema: str) -> None:
    """GenAI / Object Storage ホストへの HTTP ACL を付与する（同じ ACE の再付与は無害）。"""
    for host in acl_hosts():
        admin_cur.execute("""
            BEGIN
              DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host => :h,
                ace  => xs$ace_type(privilege_list => xs$name_list('http'),
                                    principal_name => :p,
                                    principal_type => xs_acl.ptype_db));
            END;""", h=host, p=schema)
        print(f"  ACL: {host}")


def rp_granted(admin_cur, schema: str) -> int:
    """`schema` に `OCI$RESOURCE_PRINCIPAL` の EXECUTE が付いているか（0 or 1）。

    `ENABLE_RESOURCE_PRINCIPAL(username => X)` は X のスキーマに資格情報を作るのではなく、
    ADMIN 所有の `OCI$RESOURCE_PRINCIPAL` へ EXECUTE を付与する（実機確認）。
    よって `DBA_CREDENTIALS` ではなく `DBA_TAB_PRIVS` を見る。
    """
    admin_cur.execute(
        "SELECT COUNT(*) FROM dba_tab_privs"
        " WHERE grantee = :u AND owner = 'ADMIN' AND table_name = :c AND privilege = 'EXECUTE'",
        u=schema, c=RP_CRED,
    )
    return admin_cur.fetchone()[0]


def enable_resource_principal(admin_cur, schema: str) -> None:
    """`schema` が `OCI$RESOURCE_PRINCIPAL` を使えるようにする（ADR-0021）。

    `ENABLE_RESOURCE_PRINCIPAL` は既に有効なスキーマへ再実行しても成功する（実機確認済み）ので
    分岐は要らない。ただし「呼べたのに使えない」を見逃さないよう、付与された EXECUTE を
    実際に問い合わせて確認する（fail-closed）。
    """
    admin_cur.execute(
        "BEGIN DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username => :u); END;", u=schema
    )
    if rp_granted(admin_cur, schema) == 0:
        raise SystemExit(
            f"{schema} に {RP_CRED} の EXECUTE が付いていない。"
            " ENABLE_RESOURCE_PRINCIPAL は成功したが権限が無い＝以降の DBMS_CLOUD 呼び出しは"
            " ORA-20404 になる。中止。"
        )
    print(f"  resource principal: {schema} -> {RP_CRED}")
