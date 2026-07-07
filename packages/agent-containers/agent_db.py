"""コンテナ内 query_database(NL2SQL)実装(AGT-MULTI 次段)。

- generate_sql: OCI SemanticStore(SQL Search /20260325, IAM署名)でNL→SQL生成
- execute_readonly: JETUSE_QUERY(読取専用)でSELECT実行(多層ガード, SQL-02準拠)
- ウォレットは非公開バケットから resource principal で取得

必要env: SEMSTORE_OCID / ADB_DSN / ADB_QUERY_PASSWORD / ADB_WALLET_PASSWORD /
        ADB_WALLET_BUCKET / ADB_WALLET_OBJECT(既定 adb_wallet.zip)
jetuse-dg(RP)に「対象バケットのobject read」+ generative-ai-family が必要(IAM)。
"""

import io
import os
import pathlib
import threading
import time
import zipfile

import httpx

# SQLサニタイズ(SELECT/WITHガード)は jetuse_shared に一本化(P1b)。
# jetuse_shared.sanitize_sql は SqlRejectedError(ValueError サブクラス)を送出するため、
# 旧 _sanitize の ValueError catch 経路(run_tool の except Exception)は挙動不変。
# enforce_sql_boundary は層2 fail-closed SQL ゲートの owner なしモード(specs/18 §4.3 —
# 本コンテナは execute_readonly を通らず JETUSE_QUERY へ直結する独立経路のため、
# JETUSE_DS_/辞書/パッケージを全拒否して SH 照会という本来用途だけを通す。
# データ行は VPD の fail-closed が遮断する)。
from jetuse_shared.sqlguard import enforce_sql_boundary
from jetuse_shared.sqlguard import sanitize_sql as _sanitize

REGION = os.environ.get("OCI_REGION", "ap-osaka-1")
SEMSTORE = os.environ.get("SEMSTORE_OCID", "")
WALLET_CACHE = "/tmp/adb_wallet"

_pool = None
_lock = threading.Lock()


def _signer():
    if os.environ.get("AUTH_MODE") == "resource_principal":
        from oci_genai_auth import OciResourcePrincipalAuth

        return OciResourcePrincipalAuth()
    from oci_genai_auth import OciUserPrincipalAuth

    return OciUserPrincipalAuth()


def _wallet_dir() -> str:
    dest = pathlib.Path(WALLET_CACHE)
    if (dest / "tnsnames.ora").exists():
        return str(dest)
    dest.mkdir(parents=True, exist_ok=True)
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.object_storage.ObjectStorageClient({"region": REGION}, signer=signer)
    else:
        client = oci.object_storage.ObjectStorageClient(oci.config.from_file())
    ns = client.get_namespace().data
    obj = client.get_object(
        ns, os.environ["ADB_WALLET_BUCKET"],
        os.environ.get("ADB_WALLET_OBJECT", "adb_wallet.zip"))
    zipfile.ZipFile(io.BytesIO(obj.data.content)).extractall(dest)
    return str(dest)


def _query_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                import oracledb

                wd = _wallet_dir()
                _pool = oracledb.create_pool(
                    user="JETUSE_QUERY", password=os.environ["ADB_QUERY_PASSWORD"],
                    dsn=os.environ["ADB_DSN"], config_dir=wd, wallet_location=wd,
                    wallet_password=os.environ["ADB_WALLET_PASSWORD"],
                    min=0, max=2, tcp_connect_timeout=5.0,
                    getmode=__import__("oracledb").POOL_GETMODE_TIMEDWAIT,
                    wait_timeout=15000, ping_interval=30,
                )
    return _pool


def generate_sql(question: str) -> str:
    if not SEMSTORE:
        raise RuntimeError("SEMSTORE_OCID 未設定")
    base = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/20260325"
    with httpx.Client(auth=_signer(), timeout=120.0) as client:
        res = client.post(
            f"{base}/semanticStores/{SEMSTORE}/actions/generateSqlFromNl",
            json={"inputNaturalLanguageQuery": question})
        res.raise_for_status()
        job = res.json()
        for _ in range(24):
            out = job.get("jobOutput") or {}
            if out.get("content"):
                return out["content"]
            state = job.get("lifecycleState")
            if state == "FAILED":
                raise RuntimeError(f"sql generation failed: {job.get('lifecycleDetails')}")
            if state in (None, "SUCCEEDED", "CANCELED"):
                raise RuntimeError("sql generation returned no SQL")
            time.sleep(5)
            job = client.get(
                f"{base}/semanticStores/{SEMSTORE}/sqlGenerationJobs/{job['id']}").json()
    raise RuntimeError("sql generation timed out")


def query_database(question: str) -> dict:
    sql = generate_sql(question)
    cleaned = _sanitize(sql)
    enforce_sql_boundary(cleaned)  # owner なしモード(層2 — specs/18 §4.3)
    with _query_pool().acquire() as conn:
        conn.call_timeout = 30_000
        cur = conn.cursor()
        cur.execute(cleaned)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(21)
        return {
            "sql": cleaned,
            "columns": columns,
            "rows": [["" if c is None else str(c)[:300] for c in r] for r in rows[:20]],
            "row_count": min(len(rows), 20),
            "truncated": len(rows) > 20,
        }
