"""3 方式のレイテンシ実測（同一クエリ・各 5 回以上・中央値）。

比較を誤解させないため、**測る土俵を 2 つに分ける**:
  (1) 検索のみ（retrieval-only）: 「関連チャンクを引く」までの時間
  (2) 生成込み（retrieval + LLM）: 「回答文字列が返る」までの時間
方式によって片方が存在しない（例: ③ に「生成」は含まれない）ので、
存在しないものは実測せず理由を書く（推測値を並べない）。

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/latency.py
"""

import array
import json
import statistics
import time

from common import banner, connect_spike, load_env
from fixtures import QUERY, chunks
from method_a_vector_store import MODEL, VS_NAME, _clients
from method_b_select_ai import PROFILE, VECTAB
from method_c_own_index import TABLE, embed_params, search_sql

RUNS = 5


def timed(fn, runs: int = RUNS) -> dict:
    """先に 1 回捨てて（接続・カーソル・キャッシュの初回コストを除く）から runs 回測る。"""
    fn()
    ms = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        ms.append((time.perf_counter() - t0) * 1000)
    return {
        "runs": runs,
        "median_ms": round(statistics.median(ms), 1),
        "min_ms": round(min(ms), 1),
        "max_ms": round(max(ms), 1),
        "all_ms": [round(x, 1) for x in ms],
    }


EXPECTED_CHUNKS = 10


def assert_same_dataset(cur, dp, vs_id: str | None) -> dict[str, int]:
    """3 方式が**同じ 10 チャンク**を見ていることを計測前に確かめる。

    ストアや索引に検証で足したファイルが残っていると条件が揃わず、
    数字を並べても比較にならない（実際に一度ずれた: ① 11 ファイル / ② 12 行）。
    """
    banner("計測前チェック: 3 方式が同じ 10 チャンクを見ているか")
    if not vs_id:
        raise SystemExit("① の Vector Store が見つからない。同一条件を確認できないので中止する。")
    expected_ids = {c["chunk_id"] for c in chunks()}
    counts: dict[str, int] = {}
    seen: dict[str, set[str]] = {}
    raw: dict[str, list[str]] = {}

    cur.execute(f"SELECT chunk_id FROM {TABLE}")
    raw["③ SPIKE_CHUNKS"] = [r[0] for r in cur.fetchall()]
    seen["③ SPIKE_CHUNKS"] = set(raw["③ SPIKE_CHUNKS"])
    cur.execute(f"SELECT JSON_VALUE(attributes, '$.object_name') FROM \"{VECTAB}\"")
    raw["② $VECTAB"] = [(r[0] or "").split("__")[0] for r in cur.fetchall()]
    seen["② $VECTAB"] = set(raw["② $VECTAB"])
    # vector_store.file は filename を持たないので、登録時に入れた属性の chunk_id で照合する。
    # 一覧はページングする（1 ページ目だけ見て「10 件」と言わない）。
    a_ids: list[str] = []
    after = None
    while True:
        page = (dp.vector_stores.files.list(vector_store_id=vs_id, after=after)
                if after else dp.vector_stores.files.list(vector_store_id=vs_id))
        a_ids.extend((f.attributes or {}).get("chunk_id", "?") for f in page.data)
        if not getattr(page, "has_more", False) or not page.data:
            break
        after = page.data[-1].id
    raw["① Vector Store"] = a_ids
    seen["① Vector Store"] = set(a_ids)
    for k, v in seen.items():
        counts[f"{k} 実レコード数"] = len(raw[k])
        counts[f"{k} 異なる chunk_id 数"] = len(v)
        print(f"  {k}: レコード {len(raw[k])} 件 / 異なる chunk_id {len(v)} 件 {sorted(v)}")
    bad = {k: sorted(v) for k, v in seen.items() if v != expected_ids}
    # 重複レコード（同じ chunk_id が 2 行）も条件不一致として扱う
    bad.update({f"{k}(重複)": [len(raw[k])] for k in raw if len(raw[k]) != len(seen[k])})
    if bad:
        raise SystemExit(
            f"データセットが揃っていない（想定 {sorted(expected_ids)}）: {bad}。"
            " 検証で足したファイルが残っている。計測しても比較にならないので中止する。")
    print(f"  => 3 方式とも同じ {EXPECTED_CHUNKS} チャンク（chunk_id 一致）")
    return counts


def main() -> None:
    load_env()
    results: dict[str, dict] = {}
    conn = connect_spike()
    cur = conn.cursor()

    from jetuse_core.embeddings import embed

    cp, dp = _clients()
    vs_id = next((v.id for v in cp.vector_stores.list().data if v.name == VS_NAME), None)
    results["_dataset"] = assert_same_dataset(cur, dp, vs_id)

    banner("レイテンシ実測 (1) 検索のみ")

    # ③ DB 内埋め込み込みの 1 本 SQL（アプリ↔DB 往復 1 回）
    p = embed_params()
    def c_in_db():
        cur.execute(search_sql(where="WHERE current_version = 'Y'", params_json=True),
                    q=QUERY, p=p)
        cur.fetchall()
    results["③ ADB自前索引: 1本SQL（DB内埋め込み込み・版フィルタ）"] = timed(c_in_db)

    # ③ クライアント側で埋め込んでからの SQL（埋め込み API 呼び出しは含めない）
    qv = array.array("f", embed([QUERY], input_type="SEARCH_QUERY")[0])
    def c_client():
        cur.execute(search_sql(where="WHERE current_version = 'Y'", params_json=False), q=qv)
        cur.fetchall()
    results["③ ADB自前索引: SQLのみ（埋め込み済みベクタを渡す・版フィルタ）"] = timed(c_client)

    # ② $VECTAB への直接ベクタ検索（自前で足した列でフィルタ）
    def b_vectab():
        cur.execute(
            f'''SELECT JSON_VALUE(attributes, '$.object_name'),
                       VECTOR_DISTANCE(EMBEDDING, :q, COSINE)
                FROM "{VECTAB}" WHERE current_version = 'Y'
                ORDER BY VECTOR_DISTANCE(EMBEDDING, :q, COSINE)
                FETCH FIRST 5 ROWS ONLY''', q=qv)
        cur.fetchall()
    results["② Select AI索引: $VECTAB直接検索（列を自前追加してフィルタ）"] = timed(b_vectab)

    # クライアント側埋め込み API 単体（③/② のクライアント経路に上乗せされる分）
    results["（参考）埋め込み API 単体 cohere.embed-multilingual-v3.0"] = timed(
        lambda: embed([QUERY], input_type="SEARCH_QUERY"))

    # ① Vector Store の検索 API（生成なし。属性フィルタ付き）
    if vs_id:
        def a_search():
            dp.vector_stores.search(
                vector_store_id=vs_id, query=QUERY, max_num_results=5,
                filters={"type": "eq", "key": "current_version", "value": "Y"})
        results["① Vector Store: search API（属性フィルタ付き・生成なし）"] = timed(a_search)

    banner("レイテンシ実測 (2) 生成込み")

    if vs_id:
        def a_gen():
            dp.responses.create(model=MODEL, input=QUERY,
                                tools=[{"type": "file_search", "vector_store_ids": [vs_id]}])
        results["① Vector Store: Responses+file_search（生成込み）"] = timed(a_gen)
    else:
        results["① Vector Store: Responses+file_search（生成込み）"] = {
            "skipped": f"Vector Store {VS_NAME} が見つからない"}

    # ② Select AI narrate（検索＋生成が 1 コール）
    def b_gen():
        cur.execute("SELECT DBMS_CLOUD_AI.GENERATE(prompt => :q, profile_name => :p, "
                    "action => 'narrate') FROM dual", q=QUERY, p=PROFILE)
        cur.fetchall()
    results["② Select AI: GENERATE(narrate)（検索＋生成が1コール）"] = timed(b_gen)

    conn.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
