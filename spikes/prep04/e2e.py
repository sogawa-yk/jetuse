"""PREP-04 の実環境 E2E（tasks/PREP-04.md の「E2E シナリオ」）。

**実装（`jetuse_core.extract_xlsx` / `rag` / `rag_adb` と FastAPI ルート）をそのまま呼ぶ**。
検証用の別実装は書かない。相手の ADB・OCI Generative AI（Files / Vector Stores / 埋め込み /
生成）はすべて実物。

  1. 1 セル 13,000 文字級の**架空** xlsx が取り込め、検索でヒットし、引用に**元のセル範囲**が載る
  2. 同一セル由来の複数断片が**同じ `cells`** を持ち、つなぐと元のセルに戻る（欠けていない）
  3. 回帰: 通常の xlsx が従来どおり取り込める（チャンクごとに異なるセル範囲）

隔離: 共有 loop ADB の **run 固有スキーマ**（`JETUSE_PREP04_<乱数>`。ADB は増やさない）。
OCI 側の検証用資源は `jetuse-spike-prep04-` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部（`spikes/ragm02/common.py`）をそのまま再利用する（env で接頭辞だけ差し替え）。

実行（`E=SPIKE_SCHEMA_PREFIX=JETUSE_PREP04 SPIKE_HOME=<秘密の置き場>`,
      `P=PYTHONPATH=spikes/ragm02:spikes/prep04:packages/api`）:
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成（台帳つき）
  env $E $P .venv/bin/python spikes/prep04/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/prep04/teardown.py --yes  # OCI 側（ファイル・箱）
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes  # ADB スキーマ
"""

import json
import os
import re
import sys
import time

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret
from fixtures import (
    GIANT_CELL,
    GIANT_NAME,
    GIANT_QUESTION,
    GIANT_SHEET,
    MARKER,
    PLAIN_NAME,
    PLAIN_QUESTION,
    PREFIX,
    RATE_LIMIT,
    giant_cell_text,
    giant_workbook,
    plain_workbook,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject（HTTP 経路の名前空間）

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

_IDS = re.compile(
    r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.|file-kix-|vs_kix_)[a-zA-Z0-9_-]{8,}"
)


def write(name: str, text: str) -> None:
    """証跡を書く。OCI 側の識別子は**先頭だけ残して伏せる**（実値をリポジトリに残さない）。"""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(_IDS.sub(lambda m: m.group(1) + "…", text))
    print(f"  wrote {EVIDENCE / name}")


def fence(text: str) -> str:
    return "```\n" + (text.rstrip() or "(なし)") + "\n```"


def mask(value: str | None) -> str:
    if not value:
        return "(なし)"
    return value[:12] + "…" if len(value) > 12 else value


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をこの run のスキーマへ向ける（他タスクの資源に触れない）。"""
    prepare_env()  # ADB_WALLET_* / ADB_DSN / ADB_COMPARTMENT_OCID（= 承認済み根の直下 dev）
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    # OCI 側も **dev コンパートメント**に閉じる（loop-config の e2e.compartment）。
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


def ensure_spike_store() -> str:
    """検証用の Vector Store（`jetuse-spike-prep04-<run>`）を用意し、登録簿に載せる。

    `rag.ensure_store()` が作る名前（`jetuse-rag-<owner>`）では検証用の接頭辞規約を
    満たせないので、**先に接頭辞つきで作って登録簿へ入れる**。以後 `rag.add_file` は
    この箱を使う（アプリ経路そのものは変えていない）。
    """
    from jetuse_core import rag
    from jetuse_core.genai import make_cp_client, make_inference_client

    existing = rag.get_store_id(OWNER)
    if existing:
        return existing
    name = f"{PREFIX}-{SCHEMA.rsplit('_', 1)[-1].lower()}"
    cp = make_cp_client()
    vs = cp.vector_stores.create(name=name, metadata={"owner": OWNER})
    for _ in range(30):
        if cp.vector_stores.retrieve(vector_store_id=vs.id).status == "completed":
            break
        time.sleep(2)
    dp = make_inference_client(with_project=True)
    for _ in range(30):
        try:
            dp.vector_stores.files.list(vector_store_id=vs.id)
            break
        except Exception:
            time.sleep(2)
    rag._save_store_id(OWNER, vs.id)
    print(f"  検証用 Vector Store: {name} ({mask(vs.id)})")
    return vs.id


def upload(client, name: str, content: bytes) -> dict:
    """アプリのアップロード経路（`POST /api/rag/files`）で取り込む。"""
    res = client.post(
        "/api/rag/files",
        files={"file": (name, content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"attributes": json.dumps({"version": "1.0", "kind": "spec"})},
    )
    if res.status_code != 200:
        sys.exit(f"アップロードが失敗した: {res.status_code} {res.text[:400]}")
    return res.json()


def backends(client, file_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        files = client.get("/api/rag/files").json()["files"]
        row = next((f for f in files if f["id"] == file_id), {})
        if row.get("status") in ("completed", "failed"):
            return row
        time.sleep(5)
    return row


def ask(client, question: str, backend: str) -> tuple[str, list[dict]]:
    """実 API 経路（`POST /api/chat/stream`）で聞く。"""
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": question}],
        "rag": True, "rag_backend": backend,
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    cites = next((f["citations"] for f in frames if "citations" in f), [])
    return answer, cites


def db_chunks(conn, doc_file: str) -> list[dict]:
    """ADB のチャンク表から、その文書の現行版チャンクを chunk_no 順に読む。"""
    from jetuse_core import rag_adb

    cur = conn.cursor()
    cur.execute(
        f"SELECT chunk_id, chunk_no, sheet_name, cells, body FROM {rag_adb.TABLE}"
        " WHERE owner_sub = :o AND doc_file = :f AND current_version = 'Y'"
        " ORDER BY chunk_no",
        o=OWNER, f=doc_file,
    )
    rows = []
    for chunk_id, chunk_no, sheet, cells, body in cur:
        rows.append({"chunk_id": chunk_id, "chunk_no": chunk_no, "sheet": sheet,
                     "cells": cells, "body": body.read() if hasattr(body, "read") else body})
    return rows


# --- シナリオ ------------------------------------------------------------------


def scenario_1(client, rag_adb, uploaded: dict) -> bool:
    """1 セル 13,000 文字級が取り込め、検索でヒットし、引用に元のセル範囲が載る。"""
    banner("シナリオ1: 1 セル 13,000 文字級の xlsx（取り込み → 検索 → 引用）")
    text = giant_cell_text()

    # (a) 取り込み経路が作るチャンクそのものを抽出口で見る（保存しない口）
    res = client.post("/api/extract", files={"file": (GIANT_NAME, giant_workbook(), "x")})
    chunks = res.json().get("chunks", [])
    fragments = [c for c in chunks if c["cells"] == GIANT_CELL]
    rejoined = "".join(c["text"] for c in fragments) == text
    parts = [c.get("part") for c in fragments]
    extract_ok = (res.status_code == 200 and len(fragments) >= 5 and rejoined
                  and parts == [f"{i}/{len(fragments)}" for i in range(1, len(fragments) + 1)])

    # (b) 検索（ADB 自前索引・現行版のみ）
    hits = rag_adb.search(OWNER, GIANT_QUESTION, k=5,
                          filters={"file": GIANT_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | cells={h['source']['cells']} | score={h['score']}"
                     f" | {h['text'][:40].splitlines()[0]}..." for h in hits)
    marker_hit = next((h for h in hits if MARKER in h["text"]), None)
    marker_ok = marker_hit is not None and marker_hit["source"]["cells"] == GIANT_CELL
    sheet_ok = bool(hits) and all(h["source"]["sheet"] == GIANT_SHEET for h in hits)

    # (c) 実 API の RAG 応答（引用の出典）
    answer, cites = ask(client, GIANT_QUESTION, "adb")
    cite_cells = [(c["source"]["sheet"], c["source"]["cells"]) for c in cites]
    cite_ok = bool(cites) and all(s == GIANT_SHEET and c == GIANT_CELL for s, c in cite_cells)

    ok = extract_ok and marker_ok and sheet_ok and cite_ok
    write("scenario-1.md", f"""# シナリオ1 — 1 セル 13,000 文字級の xlsx が取り込める

**架空**のブック `{GIANT_NAME}`（顧客の実ファイルは使っていない）。実データと同じ形:
`{GIANT_SHEET}` シートの **{GIANT_CELL} に {len(text):,} 文字**があり、その行の非空セルは 1 個。
**行境界でもセル境界でも割れない**ので、以前はここで `422 limit=chunk_chars` になり
**ファイル全体が入らなかった**。

- 取り込み: `POST /api/rag/files` → file_id `{uploaded['id']}` /
  バックエンド `{uploaded.get('backends')}` / 状態 `{uploaded.get('status')}`

## (a) 取り込み経路が作るチャンク（`POST /api/extract`・保存しない口）

- HTTP ステータス: **{res.status_code}**（以前はここが 422 だった）
- チャンク総数: **{len(chunks)}** / うち `{GIANT_CELL}` 由来の断片: **{len(fragments)}**
- 断片の `part`: `{parts}`（**黙って分割していない**ことが応答から分かる）
- 断片をつなぐと元のセルの値に**完全に一致**する（1 文字も落ちていない）: **{rejoined}**

{fence(chr(10).join(f"{c['sheet']} | {c['cells']} | part={c.get('part')} |"
                    f" {len(c['text'])} 文字 | {c['text'][:40].splitlines()[0]}..."
                    for c in chunks))}

## (b) 検索（`rag_adb.search` / `current_version='Y'`）

質問: `{GIANT_QUESTION}`

{fence(rows)}

- セルの**中ほど**（{len(text):,} 文字中およそ {text.find(MARKER):,} 文字目）に書いた
  `{MARKER}` を含む断片がヒットする: **{marker_ok}**
  → 先頭 2,000 文字だけが検索対象になっているのではない
- ヒットの出典シートがすべて `{GIANT_SHEET}`: **{sheet_ok}**

## (c) 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

{fence(answer)}

引用（`citations[].source`）:

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:1600])}

- 引用の (シート, セル範囲) が**元のセル** `{GIANT_SHEET} {GIANT_CELL}`: **{cite_ok}**
  → 実際: `{cite_cells}`

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_2(conn) -> bool:
    """同一セル由来の断片が同じ `cells` を持ち、つなぐと元に戻る。"""
    banner("シナリオ2: 同一セル由来の断片が同じ cells を持つ")
    text = giant_cell_text()
    rows = db_chunks(conn, GIANT_NAME)
    fragments = [r for r in rows if r["cells"] == GIANT_CELL]
    same_cells = bool(fragments) and len({(r["sheet"], r["cells"]) for r in fragments}) == 1
    unique_ids = len({r["chunk_id"] for r in fragments}) == len(fragments)
    rejoined = "".join(r["body"] for r in fragments) == text
    within_limit = all(len(r["body"]) <= 2_000 for r in fragments)
    ok = same_cells and unique_ids and rejoined and within_limit and len(fragments) >= 5

    listing = "\n".join(f"{r['chunk_id']} | chunk_no={r['chunk_no']} | sheet={r['sheet']}"
                        f" | cells={r['cells']} | {len(r['body'])} 文字"
                        f" | {r['body'][:32].splitlines()[0]}..." for r in rows)
    write("scenario-2.md", f"""# シナリオ2 — 同一セル由来の複数断片が**同じ `cells`** を持つ

ADB のチャンク表（`{GIANT_NAME}` の現行版）をそのまま読んだ。

{fence(listing)}

- `{GIANT_CELL}` 由来の断片: **{len(fragments)} 件**
- 断片の (シート, セル範囲) が**1 種類だけ**（= どれも「そのセルが根拠」）: **{same_cells}**
- `chunk_id` は断片ごとに一意（既存の採番規則 `<file_id>-<n>`）: **{unique_ids}**
- 各断片が 1 チャンクの上限 2,000 文字に収まっている: **{within_limit}**
- 断片を `chunk_no` 順につなぐと、**元のセルの値に完全一致**する: **{rejoined}**
  （{len(text):,} 文字。切り詰めも読み飛ばしもしていない）

判定: **{'PASS' if ok else 'FAIL'}**

> 出典の精度は落ちていない。どの断片を引いても「`{GIANT_SHEET}` シートの `{GIANT_CELL}` が根拠」
> であり、これは分割前に返せたはずの出典とまったく同じ粒度である（分割前はそもそも
> ファイル全体が取り込めなかった）。
""")
    return ok


def scenario_3(client, rag_adb, uploaded: dict) -> bool:
    """回帰: 通常の xlsx が従来どおり取り込める。"""
    banner("シナリオ3: 回帰（通常の xlsx）")
    res = client.post("/api/extract", files={"file": (PLAIN_NAME, plain_workbook(), "x")})
    chunks = res.json().get("chunks", [])
    pairs = [(c["sheet"], c["cells"]) for c in chunks]
    distinct = len(set(pairs)) == len(pairs) and len(chunks) >= 3
    no_part = all("part" not in c for c in chunks)      # 分割していない = 印も付かない
    no_empty_sheet = "作業用" not in {c["sheet"] for c in chunks}

    hits = rag_adb.search(OWNER, PLAIN_QUESTION, k=5,
                          filters={"file": PLAIN_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | cells={h['source']['cells']} | score={h['score']}"
                     f" | {h['text'][:36].splitlines()[0]}..." for h in hits)
    hit_pairs = [(h["source"]["sheet"], h["source"]["cells"]) for h in hits]
    hit_distinct = bool(hits) and len(set(hit_pairs)) == len(hit_pairs)
    answer, cites = ask(client, PLAIN_QUESTION, "adb")
    answered = "600" in answer
    ok = (distinct and no_part and no_empty_sheet and hit_distinct and answered
          and uploaded.get("backends", {}).get("adb") == "indexed")

    write("scenario-3.md", f"""# シナリオ3（回帰）— 通常の xlsx は従来どおり

上限に掛からない通常のブック `{PLAIN_NAME}`（複数シート・空シート・行の飛び）。
セル内分割の追加で**既存の挙動が変わっていない**ことを見る。

- 取り込み: file_id `{uploaded['id']}` / バックエンド `{uploaded.get('backends')}`

## 抽出（`POST /api/extract`）

{fence(chr(10).join(f"{c['sheet']} | {c['cells']} | part={c.get('part')} |"
                    f" {c['text'][:36].splitlines()[0]}..." for c in chunks))}

- チャンクごとに (シート, セル範囲) が異なる（PREP-01 の粒度のまま）: **{distinct}**
- 分割していないチャンクに `part` は付かない（既存の鍵は増えていない）: **{no_part}**
- 空シート `作業用` はチャンクを作らない: **{no_empty_sheet}**

## 検索と引用

質問: `{PLAIN_QUESTION}`

{fence(rows)}

{fence(answer)}

- ヒットの (シート, セル範囲) がチャンクごとに異なる: **{hit_distinct}** → `{hit_pairs}`
- 回答が現行の レート制限 {RATE_LIMIT} に基づく: **{answered}**
- 引用件数: **{len(cites)}**

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok



def scenario_4(client) -> bool:
    """回帰: 総チャンク数・ブックのバイト数の上限は据え置き（超過は従来どおり 422）。"""
    banner("シナリオ4: 上限は据え置き（切り詰めずに 422）")
    from jetuse_core import extract_xlsx

    content = giant_workbook()
    probes = {}
    for limit, attr, value in (("chunks", "MAX_CHUNKS", 3),
                               ("workbook_bytes", "MAX_WORKBOOK_BYTES", 100)):
        original = getattr(extract_xlsx, attr)
        setattr(extract_xlsx, attr, value)
        try:
            res = client.post("/api/extract", files={"file": (GIANT_NAME, content, "x")})
        finally:
            setattr(extract_xlsx, attr, original)
        probes[limit] = (res.status_code, res.json().get("detail", ""))
    ok = all(code == 422 and f"limit={limit}" in detail
             for limit, (code, detail) in probes.items())

    write("scenario-4.md", f"""# シナリオ4（回帰）— 上限の意味は変えていない

セル内分割で**収まらなくなる上限**（総チャンク数・ブックのバイト数）は据え置きで、
超過は従来どおり **422**（切り詰めない）。一時的に上限を下げて同じ架空ブックを投げた。

{fence(chr(10).join(f"{limit}: HTTP {code} / detail = {detail}"
                    for limit, (code, detail) in probes.items()))}

- どちらも 422 で、**どの上限に当たったか**が detail に出る: **{ok}**

判定: **{'PASS' if ok else 'FAIL'}**

> セル内分割は「1 チャンクの文字数」だけを拒否理由から外した（分割で必ず収まるため）。
> 断片が増えて総チャンク数の上限を超えれば、従来どおり `limit=chunks` で拒否する。
""")
    return ok


def main() -> None:
    use_task_schema()
    conn = connect_schema()  # 台帳ゲート（自分が作ったスキーマか）を通す

    banner("マイグレーション適用（deploy 相当）")
    from jetuse_core.migrate import migrate

    applied = migrate()
    print(f"  applied: {applied or '(up to date)'}")

    from fastapi.testclient import TestClient
    from jetuse_core import rag, rag_adb
    from service.main import app

    client = TestClient(app)

    # 前回の残りを消してから始める（件数が積み上がらないように）
    cur = conn.cursor()
    for table in (rag_adb.TABLE, rag_adb.DOC_TABLE, rag_adb.INGEST_TABLE):
        cur.execute(f"DELETE FROM {table} WHERE owner_sub = :o", o=OWNER)
    conn.commit()
    for row in rag.list_files(OWNER):
        rag.delete_file(OWNER, row["id"])

    ensure_spike_store()

    banner("アップロード（アプリ経路 `POST /api/rag/files`）")
    giant = upload(client, GIANT_NAME, giant_workbook())
    plain = upload(client, PLAIN_NAME, plain_workbook())
    giant = {**giant, **backends(client, giant["id"])}
    plain = {**plain, **backends(client, plain["id"])}
    for f in (giant, plain):
        print(f"  {f['filename']}: status={f.get('status')} backends={f.get('backends')}")

    results = {
        "1": scenario_1(client, rag_adb, giant),
        "2": scenario_2(conn),
        "3": scenario_3(client, rag_adb, plain),
        "4": scenario_4(client),
    }
    banner("結果")
    for k, v in results.items():
        print(f"  シナリオ{k}: {'PASS' if v else 'FAIL'}")
    write("summary.md", "# PREP-04 実環境 E2E 結果一覧\n\n"
          f"実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント /\n"
          "OCI Generative AI（埋め込み・生成）は実物。\n\n" + "\n".join(
              f"- シナリオ{k}: **{'PASS' if v else 'FAIL'}** → `scenario-{k}.md`"
              for k, v in results.items()) + "\n")
    conn.close()
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
