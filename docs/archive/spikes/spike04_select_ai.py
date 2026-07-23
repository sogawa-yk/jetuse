"""SPIKE-04: Select AI (DBMS_CLOUD_AI) による日本語NL2SQL評価。

  1. ADMINでAPIキー認証のDBMS_CLOUD credentialとSelect AIプロファイルを作成
  2. 日本語10問で showsql（SQL生成のみ）→ 生成SQLを読取専用ユーザーで実行
  3. 各問の生成SQL・実行結果・エラーを記録（人手検証用の評価表の元データ）

実行: .venv/bin/python spikes/spike04_select_ai.py [モデル名]
"""
import json
import sys
from pathlib import Path

import oracledb

REPO = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (REPO / ".env").read_text().splitlines() if "=" in l)
WALLET = "/home/opc/adb_wallet"
OCI_CONF = dict(
    l.strip().split("=", 1) for l in open(Path.home() / ".oci" / "config")
    if "=" in l and not l.startswith("["))

MODEL = sys.argv[1] if len(sys.argv) > 1 else "meta.llama-3.3-70b-instruct"
PROFILE = "JETUSE_SPIKE_AI"

QUESTIONS = [
    "2001年の売上合計金額はいくらですか",
    "販売チャネルごとの売上合計を教えてください",
    "売上金額が最も多い商品カテゴリの上位3件は何ですか",
    "顧客数が多い国の上位5件を教えてください",
    "1999年に売上金額が最も大きかった商品トップ5は",
    "2001年の月別売上推移を見せてください",
    "プロモーション別の売上合計の上位5件は",
    "平均販売単価が最も高い商品はどれですか",
    "2001年で売上が最大だった四半期はいつですか",
    "インターネットチャネルでの2000年の売上合計はいくらですか",
]


def connect(user, pw):
    return oracledb.connect(user=user, password=pw, dsn="jetusespike_low",
                            config_dir=WALLET, wallet_location=WALLET,
                            wallet_password=ENV["ADB_ADMIN_PASSWORD"])


def setup(admin):
    cur = admin.cursor()
    key = (Path.home() / ".oci" / "oci_api_key.pem").read_text()
    for stmt in [
        f"BEGIN DBMS_CLOUD.DROP_CREDENTIAL('JETUSE_OCI_CRED'); EXCEPTION WHEN OTHERS THEN NULL; END;",
        f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{PROFILE}'); EXCEPTION WHEN OTHERS THEN NULL; END;",
    ]:
        cur.execute(stmt)
    cur.execute("""
        BEGIN
          DBMS_CLOUD.CREATE_CREDENTIAL(
            credential_name => 'JETUSE_OCI_CRED',
            user_ocid       => :u,
            tenancy_ocid    => :t,
            private_key     => :k,
            fingerprint     => :f);
        END;""", u=OCI_CONF["user"], t=OCI_CONF["tenancy"],
        k="".join(l for l in key.splitlines() if l and "-----" not in l and l != "OCI_API_KEY"),
        f=OCI_CONF["fingerprint"])
    attrs = {
        "provider": "oci",
        "credential_name": "JETUSE_OCI_CRED",
        "region": ENV["OCI_REGION"],
        "model": MODEL,
        "object_list": [{"owner": "SH"}],
        "comments": "true",
    }
    cur.execute(f"""
        BEGIN
          DBMS_CLOUD_AI.CREATE_PROFILE(
            profile_name => '{PROFILE}',
            attributes   => :a);
        END;""", a=json.dumps(attrs))
    print(f"[OK] profile {PROFILE} (model={MODEL})")


def gen_sql(admin, q):
    cur = admin.cursor()
    cur.execute("""
        SELECT DBMS_CLOUD_AI.GENERATE(
                 prompt => :p, profile_name => :pr, action => 'showsql')
        FROM dual""", p=q, pr=PROFILE)
    lob = cur.fetchone()[0]
    return lob.read() if hasattr(lob, "read") else str(lob)


def try_run(query_conn, sql):
    """生成SQLを読取専用ユーザーで実行（行数制限つき）"""
    sql = sql.strip().rstrip(";")
    if not sql.upper().lstrip().startswith(("SELECT", "WITH")):
        return None, "REJECTED: not a SELECT"
    cur = query_conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchmany(10)
        cols = [d[0] for d in cur.description]
        return {"columns": cols, "rows": [[str(c)[:40] for c in r] for r in rows[:5]],
                "rowcount_sample": len(rows)}, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:160]}"


def main():
    admin = connect("ADMIN", ENV["ADB_ADMIN_PASSWORD"])
    qconn = connect("jetuse_query", ENV["ADB_QUERY_PASSWORD"])
    setup(admin)
    results = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n--- Q{i}: {q}")
        try:
            sql = gen_sql(admin, q)
        except Exception as e:
            print(f"[NG] 生成失敗: {str(e)[:200]}")
            results.append({"q": q, "sql": None, "error": str(e)[:200]})
            continue
        print(sql.strip()[:400])
        res, err = try_run(qconn, sql)
        if err:
            print(f"[NG] 実行: {err}")
        else:
            print(f"[OK] 実行: cols={res['columns']} sample={res['rows'][:2]}")
        results.append({"q": q, "sql": sql.strip(), "exec_error": err, "result": res})
    out = REPO / "spikes" / "data" / f"spike04_results_{MODEL.replace('.', '_')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    ok = sum(1 for r in results if r.get("result"))
    print(f"\n=== 生成・実行成功: {ok}/10 -> {out.name}")


if __name__ == "__main__":
    main()
