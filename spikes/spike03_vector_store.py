"""SPIKE-03: Vector Store / File Search 検証。

  1. Files API へ日本語文書3種（pdf/docx/md）をアップロード（attributes付与）
  2. Vector Store `jetuse-spike-vs` 作成 + file_batch 取り込み、ステータス確認
  3. Vector Store 直接検索（日本語10問）: ヒットファイルとスコアを記録
  4. メタデータ（attributes）フィルタ検索
  5. Responses API の file_search ツール経由でRAG応答 + 引用（annotations）構造ダンプ

実行: .venv/bin/python spikes/spike03_vector_store.py
"""
import json
import time
from pathlib import Path

from common import make_client, make_cp_client

DATA = Path(__file__).resolve().parent / "data"
MODEL = "openai.gpt-oss-120b"

FILES = [
    ("travel-policy.pdf", {"category": "travel", "dept": "soumu"}),
    ("remote-work-policy.docx", {"category": "hr", "dept": "jinji"}),
    ("remote-work-policy.txt", {"category": "hr", "dept": "jinji"}),  # docx非対応時の代替
    ("expense-guidelines.md", {"category": "finance", "dept": "keiri"}),
]

# (質問, 正解の根拠ファイル, 期待キーワード)
QUESTIONS = [
    ("出張の定義を教えてください", "travel-policy.pdf", "50キロ"),
    ("新幹線のグリーン車を利用できるのは誰ですか", "travel-policy.pdf", "部長"),
    ("管理職の国内出張の日当はいくらですか", "travel-policy.pdf", "3,000"),
    ("一般職が東京23区内に宿泊する場合の宿泊費上限は", "travel-policy.pdf", "12,000"),
    ("出張から戻った後、いつまでに精算が必要ですか", "travel-policy.pdf", "5営業日"),
    ("在宅勤務は週に何日までできますか", "remote-work-policy.docx", "3日"),
    ("在宅勤務手当の金額と支給されない条件は", "remote-work-policy.docx", "3,000"),
    ("在宅勤務でカフェの公衆Wi-Fiを使ってもいいですか", "remote-work-policy.docx", "禁止"),
    ("領収書が必須になるのはいくら以上の経費ですか", "expense-guidelines.md", "5,000"),
    ("タクシーを利用できるのはどんな場合ですか", "expense-guidelines.md", "終電"),
]


def section(t):
    print(f"\n{'=' * 70}\n## {t}\n{'=' * 70}", flush=True)


def main():
    client = make_client(timeout=180.0, with_project=True)

    section("1. Files API アップロード")
    file_ids = {}
    for name, attrs in FILES:
        f = client.files.create(file=open(DATA / name, "rb"), purpose="assistants")
        file_ids[name] = f.id
        print(f"[OK] {name}: id={f.id} bytes={f.bytes} status={getattr(f, 'status', '?')}")

    section("2. Vector Store 作成 + file_batch")
    cp = make_cp_client()
    vs = cp.vector_stores.create(name="jetuse-spike-vs")
    print(f"[OK] vector_store: {vs.id} status={vs.status}")
    # ストア本体は非同期プロビジョニング。completedになるまでDP操作は404になる
    for _ in range(60):
        vs = cp.vector_stores.retrieve(vector_store_id=vs.id)
        if vs.status == "completed":
            break
        time.sleep(5)
    print(f"vector_store status: {vs.status}")
    # CPがcompletedでもDPへの伝播に数十秒かかる。DPの files list が200になるまで待つ
    for i in range(36):
        try:
            client.vector_stores.files.list(vector_store_id=vs.id)
            print(f"DP伝播確認 OK ({i * 5}s)")
            break
        except Exception:
            time.sleep(5)
    # ファイル単位で取り込み、形式ごとの対応可否を記録する
    # （バッチ一括だとdocx等の未対応形式で全体が400になる）
    ingested = {}
    for name, attrs in FILES:
        fid = file_ids[name]
        try:
            vf = client.vector_stores.files.create(
                vector_store_id=vs.id, file_id=fid, attributes=attrs)
            for _ in range(60):
                vf = client.vector_stores.files.retrieve(
                    vector_store_id=vs.id, file_id=fid)
                if vf.status not in ("in_progress", "queued"):
                    break
                time.sleep(5)
            err = getattr(vf, "last_error", None)
            print(f"[{'OK' if vf.status == 'completed' else 'NG'}] {name}: "
                  f"{vf.status}{' err=' + str(err) if err else ''} attrs={vf.attributes}")
            if vf.status == "completed":
                ingested[name] = fid
        except Exception as e:
            print(f"[NG] {name}: {type(e).__name__}: {str(e)[:160]}")

    section("3. Vector Store 直接検索（10問）")
    direct_results = []
    for q, expect_file, _ in QUESTIONS:
        try:
            res = client.vector_stores.search(vector_store_id=vs.id, query=q)
            hits = [(d.filename, round(d.score, 3)) for d in res.data[:3]]
            top = hits[0][0] if hits else None
            direct_results.append((q, expect_file, top, hits))
            # docx代替txtを許容するため拡張子を除いた名前で判定
            mark = "○" if top and Path(top).stem == Path(expect_file).stem else "×"
            print(f"[{mark}] {q}\n     top3={hits}")
        except Exception as e:
            direct_results.append((q, expect_file, None, []))
            print(f"[NG] {q}: {type(e).__name__}: {str(e)[:150]}")

    section("4. attributes フィルタ検索")
    try:
        res = client.vector_stores.search(
            vector_store_id=vs.id, query="手当はいくらですか",
            filters={"type": "eq", "key": "category", "value": "hr"})
        print("filter(category=hr):", [(d.filename, round(d.score, 3)) for d in res.data[:3]])
    except Exception as e:
        print(f"[NG] filter search: {type(e).__name__}: {str(e)[:200]}")

    section("5. Responses API + file_search ツール（10問RAG）")
    rag_results = []
    annotation_dump = None
    for q, expect_file, expect_kw in QUESTIONS:
        try:
            resp = client.responses.create(
                model=MODEL,
                input=q,
                tools=[{"type": "file_search", "vector_store_ids": [vs.id]}],
                include=["file_search_call.results"],
            )
            text = resp.output_text or ""
            kw_ok = expect_kw in text.replace(",", ",").replace(",", ",")
            anns = []
            for item in resp.output:
                if item.type == "message":
                    for part in item.content:
                        for a in getattr(part, "annotations", []) or []:
                            anns.append(a)
            cited = {getattr(a, "filename", getattr(a, "file_id", "?")) for a in anns}
            cite_ok = Path(expect_file).stem in {Path(c).stem for c in cited}
            if annotation_dump is None and anns:
                annotation_dump = resp
            rag_results.append((q, kw_ok, cite_ok, sorted(cited)))
            print(f"[{'○' if kw_ok else '×'}kw {'○' if cite_ok else '×'}cite] {q}")
            print(f"     A: {text[:100].replace(chr(10), ' ')}")
            print(f"     引用: {sorted(cited)}")
        except Exception as e:
            rag_results.append((q, False, False, []))
            print(f"[NG] {q}: {type(e).__name__}: {str(e)[:200]}")

    section("6. 引用レスポンス構造（1件目の生ダンプ）")
    if annotation_dump:
        d = annotation_dump.model_dump()
        # output内のmessage/annotationsとfile_search_callだけ抜粋
        slim = {"output": d.get("output"), "usage": d.get("usage")}
        print(json.dumps(slim, ensure_ascii=False, indent=2, default=str)[:4000])

    section("採点サマリ")
    ds = sum(1 for _, e, t, _ in direct_results
             if t and Path(t).stem == Path(e).stem)
    kws = sum(1 for _, k, _, _ in rag_results if k)
    cs = sum(1 for _, _, c, _ in rag_results if c)
    print(f"直接検索 top1一致: {ds}/10")
    print(f"RAG応答 キーワード正答: {kws}/10")
    print(f"RAG応答 正引用: {cs}/10")
    print(f"\nvector_store_id={vs.id}（後続スパイク再利用のため残置）")


if __name__ == "__main__":
    main()
