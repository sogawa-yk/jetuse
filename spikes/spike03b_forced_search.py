"""SPIKE-03補足: instructionsでfile_search使用を強制した場合の正答率変化。

実行: .venv/bin/python spikes/spike03b_forced_search.py <vector_store_id>
"""
import sys
from pathlib import Path

from common import make_client
from spike03_vector_store import MODEL, QUESTIONS

INSTRUCTIONS = (
    "あなたは株式会社サンプル商事の社内規程アシスタントです。"
    "質問には必ずfile_searchツールで社内規程を検索し、その結果のみに基づいて回答してください。"
    "一般論で答えてはいけません。回答には根拠となる規程名を引用してください。"
)


def main(vs_id):
    client = make_client(timeout=180.0, with_project=True)
    kw_ok_n = cite_ok_n = 0
    for q, expect_file, expect_kw in QUESTIONS:
        try:
            resp = client.responses.create(
                model=MODEL, input=q, instructions=INSTRUCTIONS,
                tools=[{"type": "file_search", "vector_store_ids": [vs_id]}],
                include=["file_search_call.results"])
            text = resp.output_text or ""
            kw_ok = expect_kw in text
            cited = set()
            for item in resp.output:
                if item.type == "message":
                    for part in item.content:
                        for a in getattr(part, "annotations", []) or []:
                            cited.add(getattr(a, "filename", "?"))
            cite_ok = Path(expect_file).stem in {Path(c).stem for c in cited}
            kw_ok_n += kw_ok
            cite_ok_n += cite_ok
            print(f"[{'○' if kw_ok else '×'}kw {'○' if cite_ok else '×'}cite] {q} 引用={sorted(cited)}")
        except Exception as e:
            print(f"[NG] {q}: {type(e).__name__}: {str(e)[:150]}")
    print(f"\nキーワード正答: {kw_ok_n}/10, 正引用: {cite_ok_n}/10")


if __name__ == "__main__":
    main(sys.argv[1])
