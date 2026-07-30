"""スケール検証・第2ラウンド: 「メタデータ WHERE で索引が使われない」の追い込み。

第1ラウンド（scale_check.py・50,000 行）の結果は次のとおりだった:
  - フィルタ**無し**の近似検索 → `VECTOR INDEX HNSW SCAN` が使われ、recall@10 = 1.0
  - `WHERE` を 1 つでも付けると → `TABLE ACCESS STORAGE FULL` + 厳密検索に倒れる
    （= 近似と厳密が同じものになり、再現率 1.0 は「索引を測っていない」ことの裏返し）

この状態で「フィルタ + HNSW の再現率」を報告すると嘘になるので、原因を切り分ける:
  A. 統計情報が無いだけではないか（DBMS_STATS を採取して計画が変わるか）
  B. フィルタ列に通常索引があれば変わるか
  C. ヒントで索引スキャンを強制できるか。できるならそのときの**実再現率**はいくつか

既存の SCALE_CHUNKS（第1ラウンドで投入済み）を再利用する（再埋め込みしない）。
実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/scale_filters.py
"""

import array
import json
import os
import pathlib
import re
import statistics
import time

import oracledb

from common import banner, connect_schema
from fixtures import queries
from scale_check import FILTERS, TABLE, TOP_K, embed_all, run_one


def plan_for(conn: oracledb.Connection, sql: str, binds: dict, label: str) -> dict:
    """実行計画を取る。plan_table は毎回空にし、DDL 直後の ORA-00900 は 1 回だけ引き直す。

    証跡が「取得不可」で埋まると、そこだけ測れていないのに測ったように見える
    （第2ラウンドの初回実行で実際に B 節が空になった）。
    """
    lines: list[str] = []
    for attempt in range(2):
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM plan_table WHERE statement_id = 'RAGM02B'")
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = 'RAGM02B' FOR {sql}", **binds)
            cur.execute("SELECT plan_table_output FROM "
                        "TABLE(DBMS_XPLAN.DISPLAY(NULL, 'RAGM02B', 'BASIC +PREDICATE'))")
            lines = [(r[0] or "") for r in cur.fetchall()]
            break
        except oracledb.DatabaseError as e:
            # 同一セッションで DDL を打った直後の最初の EXPLAIN PLAN は ORA-00900 になる
            # （実測・再現手順は docs/tips.md）。1 回だけ引き直せば通る。
            if attempt == 0 and "ORA-00900" in str(e):
                continue
            lines = [f"取得不可: {str(e).splitlines()[0]}"]
    used = any(re.search(r"VECTOR INDEX", ln) for ln in lines)
    print(f"\n[{label}] ベクタ索引の使用: {'あり' if used else 'なし'}")
    for ln in lines:
        print("  " + ln)
    return {"label": label, "vector_index_used": used, "plan": lines}


def sql_for(where: str, *, approx: bool, hint: str = "", accuracy: int | None = None) -> str:
    fetch = "FETCH APPROX FIRST" if approx else "FETCH FIRST"
    tail = f" WITH TARGET ACCURACY {accuracy}" if (approx and accuracy) else ""
    return (
        f"SELECT {hint} chunk_id FROM {TABLE} {where} "
        f"ORDER BY VECTOR_DISTANCE(embedding, :q, COSINE) {fetch} {TOP_K} ROWS ONLY{tail}"
    )


def recall_vs_exact(cur, qvecs, where, extra, *, hint="", accuracy=None) -> dict:
    """同一条件の厳密検索を正解として recall@K とレイテンシを測る。"""
    recalls, lat, exact_lat = [], [], []
    for qv in qvecs:
        binds = {"q": qv, **extra}
        exact, te = run_one(cur, sql_for(where, approx=False), binds)
        approx, ta = run_one(cur, sql_for(where, approx=True, hint=hint, accuracy=accuracy), binds)
        recalls.append(len(set(exact) & set(approx)) / max(len(exact), 1))
        lat.append(ta)
        exact_lat.append(te)
    return {
        "recall_at_10_mean": round(statistics.mean(recalls), 4),
        "recall_at_10_min": round(min(recalls), 4),
        "approx_ms_median": round(statistics.median(lat), 1),
        "exact_ms_median": round(statistics.median(exact_lat), 1),
    }


def main() -> None:
    conn = connect_schema()
    conn.call_timeout = 900_000
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    rows = cur.fetchone()[0]
    print(f"再利用する表: {TABLE} / {rows} 行")
    qs = queries(int(os.environ.get("RAGM02_SCALE_QUERIES", "20")))
    qvecs = [array.array("f", v)
             for v in embed_all([q["q"] for q in qs], input_type="SEARCH_QUERY")]
    out: dict = {"rows": rows, "queries": len(qvecs)}
    # 再実行時に B 節の副作用（通常索引）が A 節へ漏れないよう、素の状態へ戻してから測る
    for idx in (f"{TABLE}_META_IDX", f"{TABLE}_FILE_IDX"):
        try:
            cur.execute(f"DROP INDEX {idx}")
            print(f"  前回の残りを削除: {idx}")
        except oracledb.DatabaseError as e:
            if "ORA-01418" not in str(e):
                raise

    banner("A 統計情報を採取してから計画を取り直す")
    t0 = time.perf_counter()
    cur.execute("BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, :t); END;", t=TABLE)
    print(f"  GATHER_TABLE_STATS: {time.perf_counter() - t0:.1f}s")
    out["after_stats"] = [
        plan_for(conn, sql_for(where, approx=True), {"q": qvecs[0], **extra}, f"統計後 {label}")
        for label, where, extra in FILTERS
    ]

    banner("B フィルタ列に通常索引を足してから計画を取り直す")
    for ddl in (
        f"CREATE INDEX {TABLE}_META_IDX ON {TABLE}(current_version, kind)",
        f"CREATE INDEX {TABLE}_FILE_IDX ON {TABLE}(doc_file)",
    ):
        try:
            cur.execute(ddl)
            print(f"  ok: {ddl}")
        except oracledb.DatabaseError as e:
            print(f"  skip: {str(e).splitlines()[0]}")
    cur.execute("BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, :t); END;", t=TABLE)
    out["after_btree"] = [
        plan_for(conn, sql_for(where, approx=True), {"q": qvecs[0], **extra},
                 f"B木索引後 {label}")
        for label, where, extra in FILTERS
    ]

    out["after_btree_recall"] = recall_vs_exact(cur, qvecs, FILTERS[1][1], FILTERS[1][2])
    print(f"  B木索引あり F1: recall@{TOP_K} {out['after_btree_recall']['recall_at_10_mean']} /"
          f" 近似 {out['after_btree_recall']['approx_ms_median']} ms /"
          f" 厳密 {out['after_btree_recall']['exact_ms_median']} ms")

    banner("C ヒントで索引スキャンを強制できるか + そのときの実再現率")
    hint = f"/*+ VECTOR_INDEX_SCAN({TABLE} {TABLE}_VIDX) */"
    out["hinted"] = []
    for label, where, extra in FILTERS:
        p = plan_for(conn, sql_for(where, approx=True, hint=hint),
                     {"q": qvecs[0], **extra}, f"ヒント付き {label}")
        m = recall_vs_exact(cur, qvecs, where, extra, hint=hint)
        print(f"  recall@{TOP_K} 平均 {m['recall_at_10_mean']} / 最低 {m['recall_at_10_min']} /"
              f" 近似 {m['approx_ms_median']} ms")
        out["hinted"].append({"filter": label, "vector_index_used": p["vector_index_used"], **m})

    banner("D フィルタ無し（索引が実際に使われる経路）での TARGET ACCURACY と実再現率")
    out["accuracy_sweep_unfiltered"] = []
    for acc in (70, 80, 90, 95, 100):
        m = recall_vs_exact(cur, qvecs, "", {}, accuracy=acc)
        print(f"  TARGET ACCURACY {acc} -> recall@{TOP_K} {m['recall_at_10_mean']} "
              f"(最低 {m['recall_at_10_min']}) / {m['approx_ms_median']} ms")
        out["accuracy_sweep_unfiltered"].append({"target_accuracy": acc, **m})

    banner("E まとめ（JSON）")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    dest = os.environ.get("RAGM02_FILTERS_JSON")
    if dest:
        pathlib.Path(dest).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        print(f"  wrote {dest}")
    conn.close()


if __name__ == "__main__":
    main()
