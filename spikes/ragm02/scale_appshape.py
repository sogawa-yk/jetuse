"""スケール検証・第3ラウンド: **実装が実際に発行する SQL そのもの**で測る。

第2ラウンドで「メタデータ列に通常索引が無いとベクタ索引が使われない」ことが分かったが、
そこまでの測定は検証スクリプト側で書いた SQL だった。測った SQL と動く SQL がずれていると
「実装で効く」根拠にならない（Codex 指摘 M-001）。そこでこのラウンドは:

  - 検索 SQL は `jetuse_core.rag_adb.search_sql()`（本実装）を**表名だけ差し替えて**使う
  - フィルタも `rag_adb.build_where()`（本実装）で組む
  - 索引は `rag_adb.BTREE_INDEXES`（本実装が作るもの）を同じ定義で張る
  - 正解（recall の分母）は同じ SQL の `FETCH APPROX FIRST` → `FETCH FIRST` 置換＝厳密検索

実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/scale_appshape.py
"""

import array
import json
import os
import pathlib
import statistics
import time

import oracledb
from jetuse_core import rag_adb

from common import banner, connect_schema
from fixtures import queries
from scale_check import TABLE, embed_all
from scale_filters import plan_for

OWNER = "scale-owner"  # scale_check.py が投入した owner_sub
TOP_K = 10

# アプリが実際に渡すフィルタ（`rag_adb.search()` の filters と同じキー）
APP_FILTERS = [
    ("G0 owner のみ", {}),
    ("G1 owner + 版フィルタ", {"current_version": "Y"}),
    ("G2 owner + 版 + 分類", {"current_version": "Y", "kind": "constraint"}),
    ("G3 owner + 版 + ファイル指定",
     {"current_version": "Y", "file": "サンプル業務仕様書_0000.xlsx"}),
]


def sqls(filters: dict) -> tuple[str, str, dict]:
    """(近似=実装どおり, 厳密=正解用, バインド) を本実装の関数から作る。"""
    where, binds = rag_adb.build_where(filters)
    approx = rag_adb.search_sql(where, TOP_K, table=TABLE)
    exact = approx.replace("FETCH APPROX FIRST", "FETCH FIRST")
    return approx, exact, {**binds, "owner": OWNER}


def run(cur: oracledb.Cursor, sql: str, binds: dict) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    cur.execute(sql, **binds)
    ids = [r[0] for r in cur.fetchall()]
    return ids, (time.perf_counter() - t0) * 1000


def main() -> None:
    conn = connect_schema()
    conn.call_timeout = 900_000
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE owner_sub = :o", o=OWNER)
    rows = cur.fetchone()[0]
    print(f"対象: {TABLE} / owner={OWNER} / {rows} 行")

    banner("① 第2ラウンドの当て物索引を外し、本実装が作る索引（rag_adb.BTREE_INDEXES）を張る")
    for idx in (f"{TABLE}_META_IDX", f"{TABLE}_FILE_IDX"):
        try:
            cur.execute(f"DROP INDEX {idx}")
            print(f"  dropped {idx}")
        except oracledb.DatabaseError as e:
            if "ORA-01418" not in str(e):
                raise
    for name, cols in rag_adb.BTREE_INDEXES.items():
        ddl = f"CREATE INDEX {name}_scale ON {TABLE}{cols}"
        try:
            cur.execute(ddl)
            print(f"  ok: {ddl}")
        except oracledb.DatabaseError as e:
            print(f"  skip: {str(e).splitlines()[0]}")
    cur.execute("BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, :t); END;", t=TABLE)
    print("  統計採取: done")

    qs = queries(int(os.environ.get("RAGM02_SCALE_QUERIES", "20")))
    qvecs = [array.array("f", v)
             for v in embed_all([q["q"] for q in qs], input_type="SEARCH_QUERY")]

    out: dict = {"rows": rows, "owner": OWNER, "queries": len(qvecs), "top_k": TOP_K,
                 "sql_source": "jetuse_core.rag_adb.search_sql()", "by_filter": []}
    for label, filters in APP_FILTERS:
        banner(f"② {label}")
        approx_sql, exact_sql, binds = sqls(filters)
        p = plan_for(conn, approx_sql, {"q": qvecs[0], **binds}, label)
        recalls, ta, te = [], [], []
        for qv in qvecs:
            b = {"q": qv, **binds}
            exact, t_e = run(cur, exact_sql, b)
            approx, t_a = run(cur, approx_sql, b)
            recalls.append(len(set(exact) & set(approx)) / max(len(exact), 1))
            ta.append(t_a)
            te.append(t_e)
        rec = {
            "filter": label,
            "vector_index_used": p["vector_index_used"],
            "recall_at_10_mean": round(statistics.mean(recalls), 4),
            "recall_at_10_min": round(min(recalls), 4),
            "approx_ms_median": round(statistics.median(ta), 1),
            "exact_ms_median": round(statistics.median(te), 1),
        }
        print(f"  recall@{TOP_K} 平均 {rec['recall_at_10_mean']} / 最低 {rec['recall_at_10_min']}"
              f" / 近似 {rec['approx_ms_median']} ms / 厳密 {rec['exact_ms_median']} ms")
        out["by_filter"].append(rec)

    banner("③ ベクタ索引が無いときも同じ SQL が正しい結果を返すか（APPROX の退避動作）")
    cur.execute(f"DROP INDEX {TABLE}_VIDX")
    approx_sql, exact_sql, binds = sqls({"current_version": "Y"})
    p = plan_for(conn, approx_sql, {"q": qvecs[0], **binds}, "索引を落とした状態")
    same = []
    for qv in qvecs[:5]:
        b = {"q": qv, **binds}
        same.append(run(cur, exact_sql, b)[0] == run(cur, approx_sql, b)[0])
    out["without_vector_index"] = {"vector_index_used": p["vector_index_used"],
                                   "identical_to_exact": all(same), "queries": len(same)}
    print(f"  索引なしで APPROX と厳密が一致: {all(same)}（{len(same)} クエリ）")

    banner("④ まとめ（JSON）")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    dest = os.environ.get("RAGM02_APPSHAPE_JSON")
    if dest:
        pathlib.Path(dest).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print(f"  wrote {dest}")
    conn.close()


if __name__ == "__main__":
    main()
