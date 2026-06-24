"""NL2SQLバックエンド比較計測(SQL-04 / チェックポイント③定点指標)。

アプリAPI経由で SQL Search と Select AI (NL2SQL) に同一の日本語10問を投げ、
生成レイテンシ・実行成功・結果一致を計測する。
実行: python spikes/cp3_nl2sql_compare.py <APIGW_HOST> <BEARER_TOKEN>
"""

import json
import statistics
import sys
import time

import httpx

HOST, TOKEN = sys.argv[1], sys.argv[2]
B = f"https://{HOST}"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

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


def generate(question: str, backend: str) -> tuple[str | None, float]:
    for attempt in (1, 2):  # SSE切断(incomplete chunked read)対策で1回リトライ
        t0 = time.perf_counter()
        sql = None
        try:
            with httpx.Client(timeout=200) as c, c.stream(
                "POST", f"{B}/api/chat/nl2sql", headers=H,
                json={"question": question, "backend": backend},
            ) as r:
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    ev = json.loads(d)
                    if "sql" in ev:
                        sql = ev["sql"]
                    if "error" in ev:
                        print(f"    gen error: {ev['error'][:100]}")
            return sql, time.perf_counter() - t0
        except httpx.HTTPError as e:
            print(f"    {backend}: 接続断({type(e).__name__}) attempt={attempt}")
            if attempt == 2:
                return None, time.perf_counter() - t0
    return None, 0.0


def execute(sql: str) -> dict | None:
    r = httpx.post(f"{B}/api/dbchat/execute", headers=H, json={"sql": sql}, timeout=90)
    return r.json() if r.status_code == 200 else None


def main():
    stats = {b: {"gen_ok": 0, "exec_ok": 0, "lat": []} for b in ("sql_search", "select_ai")}
    matches = 0
    comparable = 0
    rows_detail = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"--- Q{i}: {q}")
        firsts = {}
        for backend in ("sql_search", "select_ai"):
            sql, lat = generate(q, backend)
            st = stats[backend]
            if sql:
                st["gen_ok"] += 1
                st["lat"].append(lat)
                res = execute(sql)
                if res and res["row_count"] > 0:
                    st["exec_ok"] += 1
                    firsts[backend] = res["rows"][0]
                    print(f"    {backend}: {lat:.1f}s exec OK rows={res['row_count']} "
                          f"first={res['rows'][0][:3]}")
                else:
                    print(f"    {backend}: {lat:.1f}s exec NG")
            else:
                print(f"    {backend}: 生成失敗 ({lat:.1f}s)")
        if len(firsts) == 2:
            comparable += 1
            # 数値らしき末尾要素で比較(列順差異を許容する緩い一致)
            a = {v for v in firsts["sql_search"] if v.replace(".", "").replace("-", "").isdigit()}
            b2 = {v for v in firsts["select_ai"] if v.replace(".", "").replace("-", "").isdigit()}
            if a and a & b2:
                matches += 1
        rows_detail.append({"q": q, "firsts": firsts})

    print("\n=== サマリ ===")
    for backend, st in stats.items():
        med = statistics.median(st["lat"]) if st["lat"] else 0
        print(f"{backend}: 生成 {st['gen_ok']}/10, 実行成功 {st['exec_ok']}/10, "
              f"生成レイテンシ中央値 {med:.1f}s")
    print(f"両者実行成功 {comparable}問中、先頭行の数値一致 {matches}問")


if __name__ == "__main__":
    main()
