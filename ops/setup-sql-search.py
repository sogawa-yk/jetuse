"""SQL Search(NL2SQL)セットアップ自動化(SQL-01)。jetusedev 26ai上に一式を構築する。

構成: JETUSE_QUERYユーザー → ウォレットsecret → DBTools接続2本(mTLS) →
SemanticStore → enrich(FULL_BUILD, SH) → 完了ポーリング。冪等(名前で既存検出)。

前提: docs/setup/iam.md のIAM整備済み(動的グループにgenerativeaisemanticstore)。
実行: .venv/bin/python ops/setup-sql-search.py
完了後: .env の DBTOOLS_*_OCID / SEMSTORE_OCID を更新して出力する。
"""

import base64
import json
import pathlib
import re
import subprocess
import sys
import time

import oracledb

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
TFVARS = (ROOT / "infra/terraform/environments/dev/terraform.tfvars").read_text()
WALLET_PW = next(
    line.split('"')[1] for line in TFVARS.splitlines() if "ADB_WALLET_PASSWORD" in line
)
WALLET = "/tmp/jetusedev_wallet"
COMP = ENV["COMPARTMENT_OCID"]
NL2SQL_BASE = f"https://inference.generativeai.{ENV['OCI_REGION']}.oci.oraclecloud.com/20260325"


def oci_cmd(*args, parse=True):
    res = subprocess.run(["oci", *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"oci {' '.join(args[:3])}...: {res.stderr[:500]}")
    return json.loads(res.stdout) if parse and res.stdout.strip() else None


def step_db_user():
    print("== 1. JETUSE_QUERY ユーザー ==")
    conn = oracledb.connect(
        user="ADMIN", password=ENV["ADB_ADMIN_PASSWORD"], dsn="jetusedev_low",
        config_dir=WALLET, wallet_location=WALLET, wallet_password=WALLET_PW,
        tcp_connect_timeout=20.0,
    )
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM all_users WHERE username = 'JETUSE_QUERY'")
    if cur.fetchone()[0] == 0:
        cur.execute(f'CREATE USER jetuse_query IDENTIFIED BY "{ENV["ADB_QUERY_PASSWORD"]}"')
        print("  created")
    else:
        print("  exists")
    cur.execute("GRANT CREATE SESSION TO jetuse_query")  # SHはPUBLIC公開(SPIKE-04)
    conn.commit()
    conn.close()


def step_wallet_secret() -> str:
    print("== 2. ウォレットsecret (mTLS dbtools用) ==")
    existing = oci_cmd(
        "vault", "secret", "list", "-c", COMP, "--name", "jetuse-dev-wallet-sso",
        "--query", "data[?\"lifecycle-state\"=='ACTIVE'].id | [0]",
    )
    if existing:
        print("  exists")
        return existing
    keys = oci_cmd(
        "kms", "management", "key", "list", "-c", COMP,
        "--endpoint", ENV["KMS_MGMT_ENDPOINT"],
        "--query", "data[?\"lifecycle-state\"=='ENABLED'].id | [0]",
    )
    sso = base64.b64encode((pathlib.Path(WALLET) / "cwallet.sso").read_bytes()).decode()
    sec = oci_cmd(
        "vault", "secret", "create-base64", "-c", COMP,
        "--secret-name", "jetuse-dev-wallet-sso",
        "--vault-id", ENV["VAULT_OCID"], "--key-id", keys,
        "--secret-content-content", sso,
        "--query", "data.id",
    )
    print("  created")
    return sec


def step_dbtools(wallet_secret: str) -> tuple[str, str]:
    print("== 3. DBTools接続 (enrich=ADMIN / query=JETUSE_QUERY) ==")
    tns = (pathlib.Path(WALLET) / "tnsnames.ora").read_text()
    m = re.search(r"jetusedev_low\s*=\s*(\(description.*?)(?=\n\w|\Z)", tns, re.S | re.I)
    conn_str = re.sub(r"\s+", " ", m.group(1)).strip()
    out = []
    for name, user, secret in [
        ("jetuse-dev-dbconn-enrich", "ADMIN", ENV["SECRET_ADMIN_OCID"]),
        ("jetuse-dev-dbconn-query", "JETUSE_QUERY", ENV["SECRET_QUERY_OCID"]),
    ]:
        existing = oci_cmd(
            "dbtools", "connection", "list", "-c", COMP, "--display-name", name,
            "--query", "data.items[?\"lifecycle-state\"=='ACTIVE'].id | [0]",
        )
        if existing:
            print(f"  {name}: exists")
            out.append(existing)
            continue
        d = oci_cmd(
            "dbtools", "connection", "create-oracle-database", "-c", COMP,
            "--display-name", name,
            "--connection-string", conn_str,
            "--user-name", user,
            "--user-password-secret-id", secret,
            "--key-stores", json.dumps([{
                "keyStoreType": "SSO",
                "keyStoreContent": {"valueType": "SECRETID", "secretId": wallet_secret},
            }]),
            "--wait-for-state", "SUCCEEDED", "--max-wait-seconds", "300",
        )
        cid = d["data"]["resources"][0]["identifier"]
        print(f"  {name}: created")
        out.append(cid)
    for cid, vuser in zip(out, ["ADMIN", "JETUSE_QUERY"]):
        v = oci_cmd("dbtools", "connection", "validate-oracle-database",
                    "--connection-id", cid, "--query", "data.code")
        print(f"  validate({vuser}): {v}")
    return out[0], out[1]


def step_semstore(enrich_id: str, query_id: str) -> str:
    print("== 4. SemanticStore ==")
    existing = oci_cmd(
        "generative-ai", "semantic-store-collection", "list-semantic-stores", "-c", COMP,
        "--query",
        "data.items[?\"display-name\"=='jetuse-dev-semstore' && \"lifecycle-state\"=='ACTIVE'].id | [0]",
    )
    if existing:
        print("  exists")
        return existing
    d = oci_cmd(
        "generative-ai", "semantic-store", "create", "-c", COMP,
        "--display-name", "jetuse-dev-semstore",
        "--data-source", json.dumps({
            "connectionType": "DATABASE_TOOLS_CONNECTION",
            "enrichmentConnectionId": enrich_id,
            "queryingConnectionId": query_id,
        }),
        "--schemas", json.dumps({
            "connectionType": "DATABASE_TOOLS_CONNECTION",
            "schemas": [{"name": "SH"}],
        }),
    )
    ss_id = d["data"]["id"]
    for _ in range(60):
        st = oci_cmd("generative-ai", "semantic-store", "get", "--semantic-store-id",
                     ss_id, "--query", "data.\"lifecycle-state\"")
        if st == "ACTIVE":
            break
        time.sleep(5)
    print(f"  created: {ss_id} ({st})")
    return ss_id


def step_enrich(ss_id: str):
    print("== 5. enrichment (FULL_BUILD, SH) ==")
    # 形式はSDK oci.generative_ai_data.models.GenerateEnrichmentJobDetails から確定
    body = {
        "displayName": "jetuse-dev-enrich-full",
        "enrichmentJobType": "FULL_BUILD",
        "enrichmentJobConfiguration": {
            "enrichmentJobType": "FULL_BUILD",
            "schemaName": "SH",
        },
    }
    res = subprocess.run(
        ["oci", "raw-request", "--http-method", "POST",
         "--target-uri", f"{NL2SQL_BASE}/semanticStores/{ss_id}/actions/enrich",
         "--request-body", json.dumps(body)],
        capture_output=True, text=True,
    )
    print("  request:", (res.stdout or res.stderr)[:400])
    if res.returncode != 0:
        sys.exit(1)
    job = json.loads(res.stdout)["data"]
    job_id = job["id"]
    for i in range(120):
        res = subprocess.run(
            ["oci", "raw-request", "--http-method", "GET",
             "--target-uri", f"{NL2SQL_BASE}/semanticStores/{ss_id}/enrichmentJobs/{job_id}"],
            capture_output=True, text=True,
        )
        st = json.loads(res.stdout)["data"].get("lifecycleState")
        print(f"  job: {st} ({i * 15}s)")
        if st in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(15)
    return st


if __name__ == "__main__":
    step_db_user()
    ws = step_wallet_secret()
    enrich_id, query_id = step_dbtools(ws)
    ss_id = step_semstore(enrich_id, query_id)
    state = step_enrich(ss_id)
    print("\n=== 結果 ===")
    print(f"DBTOOLS_ENRICH_OCID={enrich_id}")
    print(f"DBTOOLS_QUERY_OCID={query_id}")
    print(f"SEMSTORE_OCID={ss_id}")
    print(f"enrichment: {state}")
