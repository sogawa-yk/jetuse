"""方式③: ADB 自前索引（DBMS_VECTOR_CHAIN + VECTOR 列 + メタ列）の実機検証。

検証したいこと（tasks/SPIKE-M1.md 完了条件）:
  - 任意のメタデータを**列 / JSON** として持てる
  - 「メタデータ絞り込み（WHERE）＋ベクタ類似検索」が**1 本の SQL** で書ける
  - 旧版（current_version='N'）を除外した検索で旧版が 1 件も返らない（対照: フィルタ無しでは返る）
  - 出典（file / version / sheet / cells / sha256）が本文埋め込みではなく構造化値として返る

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_c_own_index.py
"""

import json
import sys

import oracledb

from common import VEC_CRED, banner, connect_spike, load_env, require_owned_schema
from fixtures import QUERY, chunks

TABLE = "SPIKE_CHUNKS"
EMBED_MODEL = "cohere.embed-multilingual-v3.0"
EMBED_DIM = 1024

DDL = f"""
CREATE TABLE {TABLE} (
  chunk_id        VARCHAR2(64)  PRIMARY KEY,
  doc_file        VARCHAR2(400) NOT NULL,   -- 出典: ファイル名
  doc_version     VARCHAR2(32)  NOT NULL,   -- 出典: 版
  sheet_name      VARCHAR2(128),            -- 出典: シート
  cells           VARCHAR2(64),             -- 出典: セル範囲
  sha256          VARCHAR2(64)  NOT NULL,   -- 出典: 原本ハッシュ
  kind            VARCHAR2(32)  NOT NULL,   -- 分類: spec / constraint
  current_version CHAR(1)       NOT NULL,   -- 版フラグ: Y / N
  attributes      JSON,                     -- 任意の追加メタ（スキーマレス）
  body            CLOB          NOT NULL,
  embedding       VECTOR({EMBED_DIM}, FLOAT32)
)
"""


def embed_params(**extra) -> str:
    region = load_env()["OCI_REGION"]
    p = {
        "provider": "ocigenai",
        "credential_name": VEC_CRED,
        "url": f"https://inference.generativeai.{region}.oci.oraclecloud.com"
               "/20231130/actions/embedText",
        "model": EMBED_MODEL,
    }
    p.update(extra)
    return json.dumps(p)


def drop_if_exists(cur: oracledb.Cursor, name: str, kind: str = "TABLE") -> None:
    try:
        cur.execute(f"DROP {kind} {name}" + (" PURGE" if kind == "TABLE" else ""))
        print(f"  dropped {kind} {name}")
    except oracledb.DatabaseError as e:
        if "ORA-00942" in str(e) or "ORA-01418" in str(e):
            return
        raise


def check_in_db_embedding(cur: oracledb.Cursor) -> str | None:
    """DB 内埋め込み（UTL_TO_EMBEDDING）が使えるか実際に叩いて確かめる。

    使える場合は採用した params(JSON) を返す。全滅なら None（=クライアント側埋め込みへ退避）。
    どちらでも「エラー内容そのもの」を証跡に残す。
    """
    banner("③-1 DB 内埋め込み DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING の可否")
    env = load_env()
    candidates = [
        ("最小構成", embed_params()),
        ("compartment 明示", embed_params(compartmentId=env["COMPARTMENT_OCID"])),
        ("input_type 指定", embed_params(input_type="SEARCH_QUERY")),
    ]
    for label, params in candidates:
        redacted = json.loads(params)
        if "compartmentId" in redacted:
            redacted["compartmentId"] = "<OCID 省略>"  # 証跡に実 OCID を残さない
        print(f"\n[{label}] params={json.dumps(redacted, ensure_ascii=False)}")
        try:
            cur.execute(
                "SELECT VECTOR_DIMENSION_COUNT("
                "  DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING(:t, JSON(:p))) FROM dual",
                t="接続確認用のテキスト", p=params,
            )
            dim = cur.fetchone()[0]
            print(f"  -> OK 次元数={dim}")
            return params
        except oracledb.DatabaseError as e:
            print("  -> NG（エラー全文）:")
            print("     " + str(e).replace("\n", "\n     "))
    return None


def load_rows(cur: oracledb.Cursor, conn, in_db_params: str | None) -> None:
    banner("③-2 架空チャンク 10 件の投入（メタデータは列 + JSON）")
    rows = chunks()
    if in_db_params:
        print("埋め込み: DB 内（UTL_TO_EMBEDDING・外部に取り出さない）")
        sql = f"""
        INSERT INTO {TABLE}
          (chunk_id, doc_file, doc_version, sheet_name, cells, sha256, kind,
           current_version, attributes, body, embedding)
        VALUES
          (:chunk_id, :doc_file, :doc_version, :sheet_name, :cells, :sha256, :kind,
           :current_version, JSON(:attributes), :body,
           DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING(:body_e, JSON(:p)))
        """
        for c in rows:
            cur.execute(sql, chunk_id=c["chunk_id"], doc_file=c["file"],
                        doc_version=c["version"], sheet_name=c["sheet"], cells=c["cells"],
                        sha256=c["sha256"], kind=c["kind"],
                        current_version="Y" if c["current_version"] else "N",
                        attributes=json.dumps(
                            {"source": "spike-m1-fixture", "lang": "ja",
                             "cells": c["cells"], "sheet": c["sheet"]},
                            ensure_ascii=False),
                        body=c["text"], body_e=c["text"], p=in_db_params)
    else:
        print("埋め込み: クライアント側（jetuse_core.embeddings）に退避")
        from jetuse_core.embeddings import embed

        vecs = embed([c["text"] for c in rows], input_type="SEARCH_DOCUMENT")
        import array

        sql = f"""
        INSERT INTO {TABLE}
          (chunk_id, doc_file, doc_version, sheet_name, cells, sha256, kind,
           current_version, attributes, body, embedding)
        VALUES
          (:chunk_id, :doc_file, :doc_version, :sheet_name, :cells, :sha256, :kind,
           :current_version, JSON(:attributes), :body, :embedding)
        """
        for c, v in zip(rows, vecs, strict=True):
            cur.execute(sql, chunk_id=c["chunk_id"], doc_file=c["file"],
                        doc_version=c["version"], sheet_name=c["sheet"], cells=c["cells"],
                        sha256=c["sha256"], kind=c["kind"],
                        current_version="Y" if c["current_version"] else "N",
                        attributes=json.dumps(
                            {"source": "spike-m1-fixture", "lang": "ja",
                             "cells": c["cells"], "sheet": c["sheet"]},
                            ensure_ascii=False),
                        body=c["text"], embedding=array.array("f", v))
    conn.commit()
    cur.execute(f"SELECT current_version, COUNT(*) FROM {TABLE} GROUP BY current_version")
    print("投入結果 (current_version, 件数):", sorted(cur.fetchall()))


def build_index(cur: oracledb.Cursor) -> str:
    banner("③-3 ベクタ索引の作成（HNSW → 不可なら IVF）")
    for label, ddl in [
        ("HNSW(INMEMORY NEIGHBOR GRAPH)",
         f"CREATE VECTOR INDEX SPIKE_CHUNKS_VIDX ON {TABLE}(embedding) "
         "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95"),
        ("IVF(NEIGHBOR PARTITIONS)",
         f"CREATE VECTOR INDEX SPIKE_CHUNKS_VIDX ON {TABLE}(embedding) "
         "ORGANIZATION NEIGHBOR PARTITIONS DISTANCE COSINE WITH TARGET ACCURACY 95"),
    ]:
        print(f"\n[{label}]\n  {ddl}")
        try:
            cur.execute(ddl)
            print("  -> OK")
            return label
        except oracledb.DatabaseError as e:
            print("  -> NG（エラー全文）:")
            print("     " + str(e).replace("\n", "\n     "))
    # 索引が1つも作れないなら「ADB 自前索引」の前提が崩れる。黙って続行しない。
    raise RuntimeError("ベクタ索引を HNSW / IVF のいずれでも作成できなかった")


SEARCH_COLS = """
    chunk_id, doc_file, doc_version, sheet_name, cells, kind, current_version,
    SUBSTR(sha256, 1, 12) AS sha256_head,
    JSON_VALUE(attributes, '$.source') AS attr_source,
    ROUND(VECTOR_DISTANCE(embedding, (SELECT q FROM qvec), COSINE), 4) AS dist,
    SUBSTR(body, 1, 42) AS body_head
"""


def search_sql(*, where: str, params_json: bool) -> str:
    """1 本の SQL: クエリ埋め込み + メタデータ絞り込み + ベクタ類似検索。"""
    qvec = ("SELECT DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING(:q, JSON(:p)) AS q FROM dual"
            if params_json else "SELECT :q AS q FROM dual")
    return f"""
WITH qvec AS ({qvec})
SELECT {SEARCH_COLS}
FROM {TABLE}
{where}
ORDER BY VECTOR_DISTANCE(embedding, (SELECT q FROM qvec), COSINE)
FETCH FIRST 5 ROWS ONLY
"""


def _print_hits(cur: oracledb.Cursor) -> list[tuple]:
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print("  " + " | ".join(cols))
    for r in rows:
        print("  " + " | ".join("" if v is None else str(v) for v in r))
    return rows


def query_binds(in_db_params: str | None) -> tuple[dict, bool]:
    """検索 SQL のバインド。DB 内埋め込みならクエリ文字列、退避時はベクタを渡す。"""
    if in_db_params:
        return {"q": QUERY, "p": in_db_params}, True
    import array

    from jetuse_core.embeddings import embed

    return {"q": array.array("f", embed([QUERY], input_type="SEARCH_QUERY")[0])}, False


def run_searches(cur: oracledb.Cursor, binds: dict, params_json: bool) -> dict:
    """フィルタ無し / 版フィルタ有り / 版+kind フィルタ の 3 本を同一クエリで実行する。"""
    out = {}
    for label, where in [
        ("A: フィルタ無し（対照）", ""),
        ("B: 版フィルタのみ", "WHERE current_version = 'Y'"),
        ("C: 版 + 分類フィルタ", "WHERE current_version = 'Y' AND kind = 'constraint'"),
    ]:
        banner(f"③-4 {label}")
        sql = search_sql(where=where, params_json=params_json)
        print(f"--- SQL ---{sql}--- 実行結果 ---")
        cur.execute(sql, **binds)
        rows = _print_hits(cur)
        stale = [r[0] for r in rows if r[6] == "N"]
        print(f"  => ヒット {len(rows)} 件 / うち旧版(current_version='N') {len(stale)} 件 {stale}")
        out[label[0]] = {"hits": [r[0] for r in rows], "stale": stale}
    return out


def explain_filtered(cur: oracledb.Cursor, binds: dict, params_json: bool) -> None:
    """版フィルタ付き検索の**推定**実行計画（EXPLAIN PLAN）を表示する。

    DBMS_XPLAN.DISPLAY_CURSOR による実績プランは V$SESSION の SELECT 権限が要り、
    スパイク用スキーマには付いていない（実機で確認）。ここで分かるのは
    「オプティマイザが WHERE をどう扱う計画を立てたか」までである。
    """
    banner("③-5 版フィルタ付きベクタ検索の実行計画（WHERE が索引前に効くか）")
    cur.execute(search_sql(where="WHERE current_version = 'Y'", params_json=params_json), **binds)
    cur.fetchall()
    try:
        cur.execute("EXPLAIN PLAN SET STATEMENT_ID = 'SPIKEM1' FOR " +
                    search_sql(where="WHERE current_version = 'Y'", params_json=params_json),
                    **binds)
        cur.execute("SELECT plan_table_output FROM "
                    "TABLE(DBMS_XPLAN.DISPLAY(NULL, 'SPIKEM1', 'BASIC +PREDICATE'))")
        for (line,) in cur.fetchall():
            print("  " + (line or ""))
    except oracledb.DatabaseError as e:
        print("  実行計画取得は不可:", str(e).splitlines()[0])


def show_citation_payload(cur: oracledb.Cursor) -> None:
    banner("③-6 出典を構造化 JSON として 1 クエリで組み立てる（本文埋め込みではない）")
    sql = f"""
SELECT JSON_SERIALIZE(
         JSON_OBJECT(
           'chunk_id' VALUE chunk_id,
           'file'     VALUE doc_file,
           'version'  VALUE doc_version,
           'sheet'    VALUE sheet_name,
           'cells'    VALUE cells,
           'sha256'   VALUE sha256,
           'kind'     VALUE kind,
           'current_version' VALUE current_version
         ) PRETTY)
FROM {TABLE} WHERE chunk_id = 'c05'
"""
    print(f"--- SQL ---{sql}--- 実行結果 ---")
    cur.execute(sql)
    print(cur.fetchone()[0])


def main() -> None:
    conn = connect_spike()
    require_owned_schema(conn)   # DROP TABLE の前に「自分のスキーマか」を必ず確認
    cur = conn.cursor()
    banner("③ ADB 自前索引: 表の作り直し")
    drop_if_exists(cur, TABLE)
    print(DDL)
    cur.execute(DDL)
    print("  created", TABLE)

    in_db_params = check_in_db_embedding(cur)
    if in_db_params is None:
        print("\n※ DB 内埋め込みは不可。クライアント側埋め込みへ退避して検証を続行する。")
    load_rows(cur, conn, in_db_params)
    index_kind = build_index(cur)
    binds, params_json = query_binds(in_db_params)
    result = run_searches(cur, binds, params_json)
    explain_filtered(cur, binds, params_json)
    show_citation_payload(cur)

    banner("③ 判定")
    # 「B に旧版が無い」だけでは、B が 0 件でも PASS になってしまう（偽陽性）。
    # 対照(A に旧版が出る)・B が現行版を実際に返す・件数が想定どおり、まで見る。
    expected_current = {c["chunk_id"] for c in chunks() if c["current_version"]}
    ok = (
        len(result["A"]["stale"]) > 0                      # 対照: フィルタ無しなら旧版が返る
        and len(result["B"]["stale"]) == 0                 # 版フィルタで旧版が消える
        and len(result["B"]["hits"]) == 5                  # FETCH FIRST 5 が埋まっている
        and set(result["B"]["hits"]) <= expected_current   # 返ったのは全部現行版
        and len(result["C"]["hits"]) > 0                   # 複合フィルタも空振りしていない
    )
    print(f"作成できたベクタ索引: {index_kind}")
    print(f"A(フィルタ無し) ヒット: {result['A']['hits']} / 旧版: {result['A']['stale']}")
    print(f"B(版フィルタ)   ヒット: {result['B']['hits']} / 旧版: {result['B']['stale']}")
    print("PASS: 版フィルタ 1 本の SQL で旧版を完全排除" if ok else "FAIL: 対照が成立していない")
    conn.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
