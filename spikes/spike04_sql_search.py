"""SPIKE-04続編: SQL Search (GenerateSqlFromNl) による日本語NL2SQL評価。

enrichment SUCCEEDED後に実行する。Select AI評価(spike04_select_ai.py)と
同一の10問・同一の実行突合方式(読取専用ユーザーで実行)で比較する。

API: /20260325 はCLIにデータプレーンコマンドがないため oci raw-request を使う。
実行: .venv/bin/python spikes/spike04_sql_search.py
"""
import json
import subprocess
import time
from pathlib import Path

import oracledb

from spike04_select_ai import QUESTIONS, try_run

REPO = Path(__file__).resolve().parent.parent
ENV = dict(ln.split("=", 1) for ln in (REPO / ".env").read_text().splitlines() if "=" in ln)
BASE = f"https://inference.generativeai.{ENV['OCI_REGION']}.oci.oraclecloud.com/20260325"
SS = ENV["SEMSTORE_OCID"]
WALLET = "/tmp/jetusedev_wallet"  # jetusedev(26ai)に移行(SQL-01)


def raw(method: str, uri: str, body: dict | None = None) -> dict:
    cmd = ["oci", "raw-request", "--target-uri", uri, "--http-method", method]
    if body is not None:
        cmd += ["--request-body", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:300])
    return json.loads(out.stdout)


def extract_sql(data: dict) -> str | None:
    """同期応答(jobOutput.content)と非同期ジョブの両対応。"""
    job = data
    for _ in range(60):  # 非同期なら最大5分ポーリング
        out = job.get("jobOutput") or {}
        if out.get("content"):
            return out["content"]
        state = job.get("lifecycleState")
        if state in (None, "SUCCEEDED", "FAILED", "CANCELED"):
            if state == "FAILED":
                raise RuntimeError(f"job FAILED: {job.get('lifecycleDetails')}")
            return None
        time.sleep(5)
        job = raw("GET", f"{BASE}/semanticStores/{SS}/sqlGenerationJobs/{job['id']}")["data"]
    return None


def main():
    qconn = oracledb.connect(
        user="jetuse_query", password=ENV["ADB_QUERY_PASSWORD"], dsn="jetusedev_low",
        config_dir=WALLET, wallet_location=WALLET,
        wallet_password=next(
            ln.split('"')[1]
            for ln in (REPO / "infra/terraform/environments/dev/terraform.tfvars").read_text().splitlines()
            if "ADB_WALLET_PASSWORD" in ln))
    results = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n--- Q{i}: {q}")
        t0 = time.time()
        try:
            res = raw("POST", f"{BASE}/semanticStores/{SS}/actions/generateSqlFromNl",
                      {"inputNaturalLanguageQuery": q})
            sql = extract_sql(res["data"])
        except Exception as e:
            print(f"[NG] 生成失敗: {str(e)[:200]}")
            results.append({"q": q, "sql": None, "error": str(e)[:200]})
            continue
        gen_s = round(time.time() - t0, 1)
        if not sql:
            print(f"[NG] SQLなし: {json.dumps(res['data'], ensure_ascii=False)[:200]}")
            results.append({"q": q, "sql": None, "raw": res["data"]})
            continue
        print(f"({gen_s}s)\n{sql.strip()[:400]}")
        exec_res, err = try_run(qconn, sql)
        if err:
            print(f"[NG] 実行: {err}")
        else:
            print(f"[OK] 実行: cols={exec_res['columns']} sample={exec_res['rows'][:2]}")
        results.append({"q": q, "sql": sql.strip(), "gen_seconds": gen_s,
                        "exec_error": err, "result": exec_res})
    out = REPO / "spikes" / "data" / "spike04_results_sql_search.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    ok = sum(1 for r in results if r.get("result"))
    print(f"\n=== 生成・実行成功: {ok}/10 -> {out.name}")


if __name__ == "__main__":
    main()
