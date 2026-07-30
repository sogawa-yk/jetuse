"""RAGM-02 の前提検証: 数万チャンク規模での「メタデータ WHERE + ベクタ索引」の実測。

SPIKE-M1 は 10 行しか測っておらず、その規模ではオプティマイザが索引を使わない
（実行計画で確認済み）。本実装の前に、実データ規模で次を測る:

  1. ベクタ索引（HNSW / IVF）が実際に作れるか・作成時間
  2. 版フィルタ等の WHERE 付き近似検索で**索引が使われるか**（実行計画）
  3. `TARGET ACCURACY` に対する**実再現率**（同一 SQL の厳密検索を正解とした recall@10）
  4. フィルタの選択度を変えたときの再現率とレイテンシの動き

実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/scale_check.py
      環境変数 RAGM02_SCALE_ROWS で行数（既定 50000）、RAGM02_SCALE_QUERIES でクエリ数。
"""

import array
import json
import os
import pathlib
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import oracledb

from common import EMBED_DIM, banner, connect_schema
from fixtures import gen_chunks, queries

TABLE = "SCALE_CHUNKS"
MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "packages/api/jetuse_core/migrations/017_rag_adb.sql"
)
ROWS = int(os.environ.get("RAGM02_SCALE_ROWS", "50000"))
NQUERIES = int(os.environ.get("RAGM02_SCALE_QUERIES", "20"))
TOP_K = 10
EMBED_BATCH = 96  # cohere の 1 リクエスト上限（jetuse_core.embeddings と同じ）
INSERT_BATCH = 500


def table_ddl() -> str:
    """本実装のマイグレーション（017）から CREATE TABLE を取り出し、検証用の表名に変える。

    検証用に別の DDL を書き起こすと「測った表」と「実装した表」がずれる。
    ずれない唯一の方法は同じ定義を読むことなので、正本のファイルから引く。
    """
    text = MIGRATION.read_text()
    stmt = next(s.strip() for s in text.split(";") if "CREATE TABLE" in s)
    stmt = stmt.replace("rag_adb_chunks_cv_ck", f"{TABLE}_cv_ck")
    return stmt.replace("rag_adb_chunks", TABLE)


def drop_if_exists(cur: oracledb.Cursor, name: str, kind: str = "TABLE") -> None:
    try:
        cur.execute(f"DROP {kind} {name}" + (" PURGE" if kind == "TABLE" else ""))
        print(f"  dropped {kind} {name}")
    except oracledb.DatabaseError as e:
        if "ORA-00942" in str(e) or "ORA-01418" in str(e):
            return
        raise


def embed_all(texts: list[str], *, input_type: str = "SEARCH_DOCUMENT") -> list[list[float]]:
    """埋め込み API を並列に叩く（429 は指数バックオフで再試行）。"""
    from jetuse_core.embeddings import embed

    batches = [texts[i:i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]

    def one(batch: list[str]) -> list[list[float]]:
        delay = 2.0
        for attempt in range(6):
            try:
                return embed(batch, input_type=input_type)
            except Exception as e:  # noqa: BLE001 - 429/瞬断は再試行、最後は諦めて上げる
                if attempt == 5:
                    raise
                print(f"    embed retry ({attempt + 1}): {type(e).__name__} {str(e)[:80]}")
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    out: list[list[float]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, vecs in enumerate(pool.map(one, batches)):
            out.extend(vecs)
            if (i + 1) % 50 == 0:
                print(f"    embedded {len(out)}/{len(texts)}", flush=True)
    return out


def load_rows(conn: oracledb.Connection, rows: list[dict]) -> float:
    banner(f"② 架空チャンク {len(rows)} 件の投入（クライアント側埋め込み）")
    t0 = time.perf_counter()
    vecs = embed_all([r["text"] for r in rows])
    t_embed = time.perf_counter() - t0
    print(f"  埋め込み完了: {len(vecs)} 件 / {t_embed:.1f}s")

    sql = f"""
    INSERT INTO {TABLE}
      (chunk_id, owner_sub, file_id, chunk_no, doc_file, doc_version, sheet_name, cells,
       sha256, kind, current_version, attributes, body, embedding)
    VALUES
      (:1, :2, :3, :4, :5, :6, :7, :8, :9, :10, :11, JSON(:12), :13, :14)
    """
    cur = conn.cursor()
    t0 = time.perf_counter()
    for i in range(0, len(rows), INSERT_BATCH):
        batch = rows[i:i + INSERT_BATCH]
        cur.executemany(sql, [
            (r["chunk_id"], "scale-owner", r["file_id"], r["chunk_no"], r["doc_file"],
             r["doc_version"], r["sheet_name"], r["cells"], r["sha256"], r["kind"],
             r["current_version"],
             json.dumps({"topic": r["topic"], "source": "ragm02-fixture"}, ensure_ascii=False),
             r["text"], array.array("f", v))
            for r, v in zip(batch, vecs[i:i + INSERT_BATCH], strict=True)
        ])
        conn.commit()
        if (i // INSERT_BATCH + 1) % 20 == 0:
            print(f"    inserted {i + len(batch)}/{len(rows)}", flush=True)
    t_insert = time.perf_counter() - t0
    print(f"  投入完了: {t_insert:.1f}s")
    cur.execute(f"SELECT current_version, COUNT(*) FROM {TABLE} GROUP BY current_version")
    print("  内訳 (current_version, 件数):", sorted(cur.fetchall()))
    return t_embed


def build_index(cur: oracledb.Cursor) -> dict:
    """ベクタ索引を作る（HNSW → 不可なら IVF）。作成時間とエラー全文を残す。"""
    banner("③ ベクタ索引の作成")
    attempts = []
    for label, ddl in [
        ("HNSW(INMEMORY NEIGHBOR GRAPH)",
         f"CREATE VECTOR INDEX {TABLE}_VIDX ON {TABLE}(embedding) "
         "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95"),
        ("IVF(NEIGHBOR PARTITIONS)",
         f"CREATE VECTOR INDEX {TABLE}_VIDX ON {TABLE}(embedding) "
         "ORGANIZATION NEIGHBOR PARTITIONS DISTANCE COSINE WITH TARGET ACCURACY 95"),
    ]:
        print(f"\n[{label}]\n  {ddl}")
        t0 = time.perf_counter()
        try:
            cur.execute(ddl)
            dt = time.perf_counter() - t0
            print(f"  -> OK ({dt:.1f}s)")
            attempts.append({"kind": label, "ok": True, "seconds": round(dt, 1)})
            return {"kind": label, "seconds": round(dt, 1), "attempts": attempts}
        except oracledb.DatabaseError as e:
            msg = str(e).strip()
            print("  -> NG（エラー全文）:\n     " + msg.replace("\n", "\n     "))
            attempts.append({"kind": label, "ok": False, "error": msg.splitlines()[0]})
    raise RuntimeError("ベクタ索引を HNSW / IVF のいずれでも作成できなかった")


def index_meta(cur: oracledb.Cursor) -> list[dict]:
    """作成された索引の実体（種別・パラメータ）をディクショナリから読む。"""
    try:
        cur.execute(
            "SELECT index_name, index_type, index_subtype, distance_metric, "
            "       target_accuracy, index_parameters "
            "FROM user_vector_indexes"
        )
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    except oracledb.DatabaseError as e:
        print("  user_vector_indexes 参照不可:", str(e).splitlines()[0])
        return []


# --- 検索 ---------------------------------------------------------------------

FILTERS = [
    ("F0 フィルタ無し", "", {}),
    ("F1 版フィルタ current_version='Y'", "WHERE current_version = 'Y'", {}),
    ("F2 版+分類 current_version='Y' AND kind='constraint'",
     "WHERE current_version = 'Y' AND kind = 'constraint'", {}),
    ("F3 高選択度 doc_file 指定 + 版フィルタ",
     "WHERE current_version = 'Y' AND doc_file = :f", {"f": "サンプル業務仕様書_0000.xlsx"}),
]


def search_sql(where: str, *, approx: bool, accuracy: int | None = None) -> str:
    fetch = "FETCH APPROX FIRST" if approx else "FETCH FIRST"
    tail = f" WITH TARGET ACCURACY {accuracy}" if (approx and accuracy) else ""
    return (
        f"SELECT chunk_id FROM {TABLE} {where} "
        f"ORDER BY VECTOR_DISTANCE(embedding, :q, COSINE) {fetch} {TOP_K} ROWS ONLY{tail}"
    )


def run_one(cur: oracledb.Cursor, sql: str, binds: dict) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    cur.execute(sql, **binds)
    rows = [r[0] for r in cur.fetchall()]
    return rows, (time.perf_counter() - t0) * 1000


def selectivity(cur: oracledb.Cursor, where: str, binds: dict) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {TABLE} {where}", **{k: v for k, v in binds.items()})
    return cur.fetchone()[0]


def measure(cur: oracledb.Cursor, qvecs: list[array.array]) -> list[dict]:
    """フィルタ別に「厳密検索を正解とした近似検索の再現率」とレイテンシを測る。"""
    results = []
    for label, where, extra in FILTERS:
        banner(f"④ {label}")
        n_match = selectivity(cur, where, extra)
        print(f"  条件に一致する行: {n_match}")
        recalls, t_exact, t_approx = [], [], []
        for qv in qvecs:
            binds = {"q": qv, **extra}
            exact, te = run_one(cur, search_sql(where, approx=False), binds)
            approx, ta = run_one(cur, search_sql(where, approx=True), binds)
            recalls.append(len(set(exact) & set(approx)) / max(len(exact), 1))
            t_exact.append(te)
            t_approx.append(ta)
        rec = {
            "filter": label,
            "matching_rows": n_match,
            "recall_at_10_mean": round(statistics.mean(recalls), 4),
            "recall_at_10_min": round(min(recalls), 4),
            "exact_ms_median": round(statistics.median(t_exact), 1),
            "approx_ms_median": round(statistics.median(t_approx), 1),
        }
        print(f"  recall@{TOP_K} 平均 {rec['recall_at_10_mean']} / 最低 {rec['recall_at_10_min']}")
        print(f"  厳密 {rec['exact_ms_median']} ms / 近似 {rec['approx_ms_median']} ms（中央値）")
        results.append(rec)
    return results


def accuracy_sweep(cur: oracledb.Cursor, qvecs: list[array.array]) -> list[dict]:
    """クエリ側 `TARGET ACCURACY` を振って、指定値と実再現率の関係を見る。"""
    banner("⑤ TARGET ACCURACY と実再現率（版フィルタ付き）")
    where = FILTERS[1][1]
    out = []
    for acc in (70, 80, 90, 95):
        recalls, lat = [], []
        for qv in qvecs:
            exact, _ = run_one(cur, search_sql(where, approx=False), {"q": qv})
            approx, ms = run_one(cur, search_sql(where, approx=True, accuracy=acc), {"q": qv})
            recalls.append(len(set(exact) & set(approx)) / max(len(exact), 1))
            lat.append(ms)
        row = {
            "target_accuracy": acc,
            "recall_at_10_mean": round(statistics.mean(recalls), 4),
            "approx_ms_median": round(statistics.median(lat), 1),
        }
        print(f"  TARGET ACCURACY {acc} -> recall@{TOP_K} {row['recall_at_10_mean']} / "
              f"{row['approx_ms_median']} ms")
        out.append(row)
    return out


def explain(cur: oracledb.Cursor, qv: array.array) -> dict:
    """WHERE 付き近似検索の実行計画（索引が使われているか）を残す。"""
    banner("⑥ 実行計画（メタデータ WHERE + 近似ベクタ検索）")
    plans = {}
    for label, where, extra in FILTERS:
        sql = search_sql(where, approx=True)
        try:
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = 'RAGM02' FOR {sql}", q=qv, **extra)
            cur.execute("SELECT plan_table_output FROM "
                        "TABLE(DBMS_XPLAN.DISPLAY(NULL, 'RAGM02', 'BASIC +PREDICATE'))")
            lines = [(r[0] or "") for r in cur.fetchall()]
        except oracledb.DatabaseError as e:
            lines = [f"取得不可: {str(e).splitlines()[0]}"]
        print(f"\n[{label}]")
        for ln in lines:
            print("  " + ln)
        used = any(re.search(r"VECTOR INDEX", ln) for ln in lines)
        plans[label] = {"vector_index_used": used, "plan": lines}
        print(f"  => ベクタ索引の使用: {'あり' if used else 'なし（全件走査＋厳密検索）'}")
    return plans


def main() -> None:
    conn = connect_schema()
    conn.call_timeout = 900_000  # 索引構築・大量投入は長い（無期限にはしない）
    cur = conn.cursor()
    banner(f"① 表の作り直し（{TABLE} / {ROWS} 行）")
    drop_if_exists(cur, f"{TABLE}_VIDX", "INDEX")
    drop_if_exists(cur, TABLE)
    ddl = table_ddl()
    print(ddl)
    cur.execute(ddl)

    rows = gen_chunks(ROWS)
    load_rows(conn, rows)
    idx = build_index(cur)
    meta = index_meta(cur)
    print("  索引メタ:", json.dumps(meta, ensure_ascii=False, default=str))

    qs = queries(NQUERIES)
    qvecs = [array.array("f", v)
             for v in embed_all([q["q"] for q in qs], input_type="SEARCH_QUERY")]
    results = measure(cur, qvecs)
    sweep = accuracy_sweep(cur, qvecs)
    plans = explain(cur, qvecs[0])

    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    total = cur.fetchone()[0]
    summary = {
        "rows": total,
        "dim": EMBED_DIM,
        "index": idx,
        "index_meta": meta,
        "queries": len(qvecs),
        "top_k": TOP_K,
        "by_filter": results,
        "accuracy_sweep": sweep,
        "plans": {k: v["vector_index_used"] for k, v in plans.items()},
    }
    banner("⑦ まとめ（JSON）")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    out = os.environ.get("RAGM02_SCALE_JSON")
    if out:
        pathlib.Path(out).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        print(f"  wrote {out}")
    conn.close()
    # 索引が 1 つも使われない結果でも「測れた」ことは事実なので異常終了にはしない。
    # 判断材料は実行計画と再現率であり、それらは上に出ている。
    sys.exit(0)


if __name__ == "__main__":
    main()
