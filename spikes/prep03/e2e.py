"""PREP-03 の実環境 E2E（tasks/PREP-03.md の「E2E シナリオ」）。

**実装（`jetuse_core.extract_scan` / `docunderstand` / `rag` / `rag_adb` と FastAPI ルート）を
そのまま呼ぶ**。検証用の別実装は書かない。相手の OCI Document Understanding・ADB・
OCI Generative AI（Files / Vector Stores / 埋め込み / 生成）はすべて実物。

  0. 抽出口 `POST /api/extract`（取り込まない）: スキャン PDF が頁つきで返る / 上限は 422
  1. スキャン PDF（テキスト層なし・日本語）を取り込み、本文の語で検索してヒットし、
     引用に**ページ番号**が載る
  2. 画像（PNG）で同様
  3. **対照**: テキスト層のある PDF は OCR を通らない（OCR 呼び出し回数 0 で証明する）

隔離: 共有 loop ADB の **run 固有スキーマ**（`JETUSE_PREP03_<乱数>`。ADB は増やさない）。
OCI 側の検証用資源は `jetuse-spike-prep03-` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部（`spikes/ragm02/common.py`）をそのまま再利用する（env で接頭辞だけ差し替え）。

実行（`E=SPIKE_SCHEMA_PREFIX=JETUSE_PREP03 SPIKE_HOME=/tmp/jetuse-prep03`,
      `P=PYTHONPATH=spikes/ragm02:spikes/prep03:packages/api`）:
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成（台帳つき）
  env $E $P .venv/bin/python spikes/prep03/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/prep03/teardown.py --yes  # OCI 側（ファイル・箱）
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes  # ADB スキーマ
"""

import json
import os
import re
import sys
import time

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret
from fixtures import (
    DEADLINE,
    IMAGE_NAME,
    LOT_NUMBER,
    PART_CODE,
    PREFIX,
    QUESTION_IMAGE,
    QUESTION_REPORT,
    SCAN_PDF_NAME,
    TEXT_PDF_NAME,
    VERDICT,
    scan_png,
    scanned_pdf,
    text_pdf,
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


def squash(text: str) -> str:
    """空白を落として比べる。

    DU の日本語 OCR は**英数字の連なりに空白を挟むことがある**（実測: `BRG-7781` →
    `BRG - 778 1`、`LOT-2026-0518` → `LOT - 2026 - 0518`）。文字は正しく読めているので、
    「読めたか」の判定は空白を無視して行い、この癖自体は証跡と docs に記録する
    （OCR 出力を書き換えて隠さない）。
    """
    return re.sub(r"\s+", "", text or "")


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をこの run のスキーマへ向ける（他タスクの資源に触れない）。"""
    prepare_env()
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    # OCI 側も **dev コンパートメント**に閉じる（loop-config の e2e.compartment）
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


class OcrCounter:
    """**実物の OCR を数えるだけ**のラッパ（結果は素通し）。

    「テキスト層のある PDF は OCR を通らない = 課金しない」は、呼び出し回数でしか
    証明できない（結果を見ても分からない）。ここで数えて対照シナリオの根拠にする。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def install(self):
        from jetuse_core import docunderstand

        for name in ("ocr", "ocr_vlm"):
            original = getattr(docunderstand, name)

            def wrapper(content, *, _n=name, _f=original, **kwargs):
                started = time.time()
                try:
                    result = _f(content, **kwargs)
                except Exception as e:
                    self.calls.append({"engine": _n, "bytes": len(content),
                                       "error": f"{type(e).__name__}: {str(e)[:200]}"})
                    raise
                self.calls.append({
                    "engine": _n, "bytes": len(content), "pages": len(result.get("pages") or []),
                    "seconds": round(time.time() - started, 1),
                    "mean_confidence": result.get("mean_confidence"),
                })
                return result

            setattr(docunderstand, name, wrapper)
        return self

    def since(self, mark: int) -> list[dict]:
        return self.calls[mark:]

    @property
    def count(self) -> int:
        return len(self.calls)


def ensure_spike_store() -> str:
    """検証用の Vector Store（`jetuse-spike-prep03-<run>`）を用意し、登録簿に載せる。"""
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


def upload(client, name: str, content: bytes, mime: str) -> dict:
    res = client.post("/api/rag/files", files={"file": (name, content, mime)})
    if res.status_code != 200:
        sys.exit(f"アップロードが失敗した: {res.status_code} {res.text[:400]}")
    return res.json()


def wait_completed(client, file_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        files = client.get("/api/rag/files").json()["files"]
        row = next((f for f in files if f["id"] == file_id), {})
        if row.get("status") in ("completed", "failed"):
            return row
        time.sleep(5)
    return row


def ask(client, question: str) -> tuple[str, list[dict]]:
    """実 API の RAG 応答（`POST /api/chat/stream` / `rag_backend="adb"`）。"""
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": question}],
        "rag": True, "rag_backend": "adb",
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    cites = next((f["citations"] for f in frames if "citations" in f), [])
    return answer, cites


# --- シナリオ ------------------------------------------------------------------


def scenario_0(client, ocr: OcrCounter) -> bool:
    """抽出口: スキャン PDF が頁つきのチャンクで返る / 上限超過は切り詰めず 422。"""
    banner("シナリオ0: POST /api/extract（抽出のみ・取り込みなし）")
    before = len(client.get("/api/rag/files").json()["files"])
    mark = ocr.count
    res = client.post("/api/extract",
                      files={"file": (SCAN_PDF_NAME, scanned_pdf(), "application/pdf")})
    after = len(client.get("/api/rag/files").json()["files"])
    body = res.json()
    chunks = body.get("chunks", [])
    rows = "\n".join(f"{c['sheet']} | {c['cells']} | {c['text'][:44].splitlines()[0]}..."
                     for c in chunks)
    sheets = [c["sheet"] for c in chunks]
    pages_ok = sheets[:1] == ["p.1"] and "p.2" in sheets
    text_ok = any(squash(PART_CODE) in squash(c["text"]) for c in chunks)
    exact_ok = any(PART_CODE in c["text"] for c in chunks)
    ocr_calls = ocr.since(mark)

    # 上限超過（切り詰めない）も同じ口で確認する
    from jetuse_core import extract_scan

    original = extract_scan.MAX_OCR_PAGES
    extract_scan.MAX_OCR_PAGES = 1
    extract_scan._native.clear()
    extract_scan._result.clear()
    try:
        over = client.post("/api/extract",
                           files={"file": (SCAN_PDF_NAME, scanned_pdf(), "application/pdf")})
    finally:
        extract_scan.MAX_OCR_PAGES = original
        extract_scan._native.clear()
        extract_scan._result.clear()
    limit_ok = over.status_code == 422 and "limit=ocr_pages" in over.json().get("detail", "")

    # 未知のエンジン名は黙って既定へ落とさない
    bad = client.post("/api/extract",
                      files={"file": (SCAN_PDF_NAME, scanned_pdf(), "application/pdf")},
                      data={"ocr_engine": "vlmm"})
    engine_ok = bad.status_code == 422

    ok = (res.status_code == 200 and pages_ok and text_ok and after == before
          and limit_ok and engine_ok and len(ocr_calls) == 1)
    write("scenario-0.md", f"""# シナリオ0 — 抽出口 `POST /api/extract`（取り込まない）

架空のスキャン PDF `{SCAN_PDF_NAME}`（**テキスト層なし**・日本語・2 ページ）を実 API に渡した。
**保存はしない**ことを、前後のファイル一覧の件数で確認している。

- HTTP ステータス: **{res.status_code}** / チャンク数: **{body.get('chunk_count')}**
- 取り込み前後のファイル数: {before} → {after}（増えていないこと）

{fence(rows)}

- 出典の見出しがページ番号（`p.1` / `p.2`）: **{pages_ok}** → `{sheets}`
- OCR した本文に `{PART_CODE}` が含まれる（空白を無視して比較）: **{text_ok}**
  / **そのままの一致**: {exact_ok}
- 実際に走った OCR 呼び出し（1 回で 2 ページ分。DU は 5 ページ単位で分割する）:

{fence(json.dumps(ocr_calls, ensure_ascii=False, indent=2))}

## OCR した本文（そのまま）

{fence(chr(10).join(f"[{c['sheet']}]" + chr(10) + c["text"] for c in chunks))}

> **DU の日本語 OCR は英数字の連なりに空白を挟むことがある**（`{PART_CODE}` →
> `BRG - 778 1`）。文字自体は正しく読めており、日本語の語での検索には影響しない。
> 型番の完全一致検索が要る用途では正規化が要る（`docs/verification/PREP-03.md` の残課題）。

## 上限超過（切り詰めずに拒否する）

一時的に OCR 対象ページの上限を 1 に落として同じファイルを投げた:

- HTTP ステータス: **{over.status_code}**（期待 422）
- detail: `{over.json().get('detail', '')}`（どの上限かが書かれていること）

## エンジン名の誤り

`ocr_engine=vlmm`（誤字）: **{bad.status_code}**（期待 422 — 黙って既定へ落とさない）
detail: `{bad.json().get('detail', '')}`

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_1(client, ocr: OcrCounter) -> bool:
    """スキャン PDF を取り込み、本文の語で検索してヒットし、引用に頁が載る。"""
    banner("シナリオ1: スキャン PDF（テキスト層なし・日本語）")
    from jetuse_core import rag_adb

    mark = ocr.count
    up = upload(client, SCAN_PDF_NAME, scanned_pdf(), "application/pdf")
    row = wait_completed(client, up["id"])
    ocr_calls = ocr.since(mark)

    hits = rag_adb.search(OWNER, QUESTION_REPORT, k=5,
                          filters={"file": SCAN_PDF_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | cells={h['source']['cells']} | score={h['score']}"
                     f" | {h['text'][:40].splitlines()[0]}..." for h in hits)
    part_hit = next((h for h in hits if squash(PART_CODE) in squash(h["text"])), None)
    page_of_part = part_hit["source"]["sheet"] if part_hit else None

    answer, cites = ask(client, QUESTION_REPORT)
    # **このシナリオの時点で箱に在るのはこのファイルだけ**（毎回 teardown 済みの箱で始める）。
    # 引用の由来を混ぜないための隔離条件なので、証跡に出して判定にも含める。
    cite_files = sorted({c["filename"] for c in cites})
    isolated = cite_files == [SCAN_PDF_NAME]
    cite_pages = [c["source"]["sheet"] for c in cites]
    page_shaped = bool(cite_pages) and all(re.fullmatch(r"p\.\d+", p or "") for p in cite_pages)
    # 回答は**本文にしか無い日付**で確かめる。型番は OCR が空白を挟むため LLM が整形しうる
    # （実測でそうなった）。**判定には使わないが、隠さず測って報告する**。
    answer_ok = squash(DEADLINE) in squash(answer)
    answer_keeps_part_code = squash(PART_CODE) in squash(answer)
    # 2 ページ目にしか無い語は 2 ページ目の出典で返る（頁が構造化された値で載っている証明）
    right_page = page_of_part == "p.2"
    once = len(ocr_calls) == 1
    ok = (bool(hits) and part_hit is not None and right_page and page_shaped
          and answer_ok and once and isolated and row.get("status") == "completed")

    write("scenario-1.md", f"""# シナリオ1 — スキャン PDF（テキスト層なし）が検索でヒットする

架空の 2 ページのスキャン PDF `{SCAN_PDF_NAME}` を**アプリ経路**
（`POST /api/rag/files` → `rag.add_file` → OCR → 各バックエンド）で取り込んだ。

- file_id: `{up['id']}` / 取り込み状態: **{row.get('status')}**
- バックエンド別の取り込み状況: `{row.get('backends')}`
- このアップロードで走った OCR（**1 回**。マネージド変換と ADB 取り込みで二重に呼ばない）:

{fence(json.dumps(ocr_calls, ensure_ascii=False, indent=2))}

## 検索（`rag_adb.search` / `current_version='Y'`）

質問: `{QUESTION_REPORT}`

{fence(rows)}

- 本文の語 `{PART_CODE}` を含むチャンクがヒットする（空白を無視して比較）:
  **{part_hit is not None}**
- その出典のページ: **{page_of_part}**（2 ページ目にしか書いていない語 → 期待 `p.2`）:
  **{right_page}**

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

{fence(answer)}

引用（`citations[].source`）:

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:1800])}

- 引用のページが構造化された値（`p.N`）で載る: **{page_shaped}** → `{cite_pages}`
- 引用の出所が**このスキャン PDF だけ**（他文書が混ざっていない）: **{isolated}**
  → `{cite_files}`
- 回答が本文の `{DEADLINE}` に基づく: **{answer_ok}**

### 生成回答が識別子を忠実に写すか（判定条件ではない・隠さず測る）

- この実行で回答が原文の `{PART_CODE}` を保った: **{answer_keeps_part_code}**
- **これは実行ごとに揺れる**。同じ入力・同じ手順で 2 回測ったところ、1 回目の回答は
  `BRG-778` と末尾の `1` を落とし、2 回目は一致した。原因は OCR が `BRG - 778 1` と
  空白を挟むこと（`docs/verification/PREP-03.md` §7）。**引用として返るチャンク本文も
  空白入りのまま**（上の `citations[].text`）で、原文そのままではない。ただし空白を除けば
  原文と一致する（＝文字は落ちていない）。生成側がそれをどう読むかで揺れる。
- したがって本タスクが実証したのは「**スキャン文書の本文が検索でヒットし、出典にページ番号が載る**」
  ことであって、「生成回答が識別子まで忠実に写す」ことではない（**それは保証しない**）。
  識別子の忠実性は前処理（OCR 出力の正規化）か生成側（引用の逐語化）の別タスク。→ 残課題

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_2(client, ocr: OcrCounter) -> bool:
    """画像（PNG）を取り込み、中身が検索でヒットする。"""
    banner("シナリオ2: 画像（PNG）")
    from jetuse_core import rag_adb

    mark = ocr.count
    up = upload(client, IMAGE_NAME, scan_png(), "image/png")
    row = wait_completed(client, up["id"])
    ocr_calls = ocr.since(mark)

    hits = rag_adb.search(OWNER, QUESTION_IMAGE, k=5,
                          filters={"file": IMAGE_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | cells={h['source']['cells']} | score={h['score']}"
                     f" | {h['text'][:40].splitlines()[0]}..." for h in hits)
    lot_hit = next((h for h in hits if squash(LOT_NUMBER) in squash(h["text"])), None)
    answer, cites = ask(client, QUESTION_IMAGE)
    cite_pages = [c["source"]["sheet"] for c in cites if c["filename"] == IMAGE_NAME]
    page_ok = cite_pages == ["p.1"] * len(cite_pages) and bool(cite_pages)
    answer_ok = squash(LOT_NUMBER) in squash(answer) and VERDICT in answer
    ok = (bool(hits) and lot_hit is not None and page_ok and answer_ok
          and len(ocr_calls) == 1 and row.get("status") == "completed")

    write("scenario-2.md", f"""# シナリオ2 — 画像（PNG）が検索でヒットする

架空の受入検査記録を 1 枚の PNG（`{IMAGE_NAME}`）にしたものを、
同じアプリ経路で取り込んだ。画像は**1 ページ扱い**。

- file_id: `{up['id']}` / 取り込み状態: **{row.get('status')}**
- バックエンド別の取り込み状況: `{row.get('backends')}`
- このアップロードで走った OCR:

{fence(json.dumps(ocr_calls, ensure_ascii=False, indent=2))}

## 検索（`rag_adb.search`）

質問: `{QUESTION_IMAGE}`

{fence(rows)}

- 本文の語 `{LOT_NUMBER}` を含むチャンクがヒットする（空白を無視して比較）:
  **{lot_hit is not None}**

## OCR した本文（そのまま）

{fence(chr(10).join(h["text"] for h in hits))}

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

{fence(answer)}

引用:

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:1500])}

- 画像由来の引用のページが `p.1`: **{page_ok}** → `{cite_pages}`
- 回答に `{LOT_NUMBER}`（空白を無視）と判定 `{VERDICT}` が出る: **{answer_ok}**

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_3(client, ocr: OcrCounter) -> bool:
    """対照: テキスト層のある PDF は OCR を通らない（無駄な課金をしていない証跡）。"""
    banner("シナリオ3: 対照（テキスト層のある PDF は OCR を通さない）")
    from jetuse_core import rag_adb

    mark = ocr.count
    res = client.post("/api/extract",
                      files={"file": (TEXT_PDF_NAME, text_pdf(), "application/pdf")})
    extract_calls = ocr.since(mark)

    mark = ocr.count
    up = upload(client, TEXT_PDF_NAME, text_pdf(), "application/pdf")
    row = wait_completed(client, up["id"])
    upload_calls = ocr.since(mark)

    hits = rag_adb.search(OWNER, QUESTION_REPORT, k=5,
                          filters={"file": TEXT_PDF_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | {h['text'][:40].splitlines()[0]}..." for h in hits)
    chunks = res.json().get("chunks", [])
    searchable = any(squash(PART_CODE) in squash(h["text"]) for h in hits)
    no_ocr = not extract_calls and not upload_calls
    ok = (res.status_code == 200 and no_ocr and searchable
          and row.get("status") == "completed" and bool(chunks))

    write("scenario-3.md", f"""# シナリオ3 — 対照: テキスト層のある PDF は **OCR を通らない**

シナリオ1 と**同じ内容**をテキスト層つきで作った PDF（`{TEXT_PDF_NAME}`）を、
同じ 2 つの口（抽出 `POST /api/extract` と取り込み `POST /api/rag/files`）へ通した。

判定根拠は**実際の OCR 呼び出し回数**である（結果だけ見ても「通ったか」は分からない）。
`docunderstand.ocr` / `ocr_vlm` を数えるラッパで包み、この 2 回の操作の前後で数えた。

- 抽出（`POST /api/extract`）中の OCR 呼び出し: **{len(extract_calls)} 回**
- 取り込み（`POST /api/rag/files`）中の OCR 呼び出し: **{len(upload_calls)} 回**
- どちらも 0 回（= 課金していない）: **{no_ocr}**

## それでも本文は従来どおり取り込まれている

- 抽出のチャンク数: **{len(chunks)}** / 先頭の出典: `{chunks[0]['sheet'] if chunks else None}`
- 取り込み状態: **{row.get('status')}** / バックエンド: `{row.get('backends')}`

{fence(rows)}

- 本文の語 `{PART_CODE}` が検索で引ける: **{searchable}**

判定: **{'PASS' if ok else 'FAIL'}**

> 判定根拠（ページごとに `extract_text()` が空白以外を返すか）は
> `packages/api/tests/test_extract_scan.py` の
> `test_pdf_with_a_text_layer_never_calls_ocr` でも固定してある。
""")
    return ok


def main() -> None:
    banner(f"PREP-03 実環境 E2E（スキーマ {SCHEMA}）")
    use_task_schema()
    connect_schema().close()  # 所有台帳ゲート（自分が作ったスキーマか）を通す

    from fastapi.testclient import TestClient
    from jetuse_core import rag_adb
    from service.main import app

    if rag_adb.availability() != rag_adb.READY:
        sys.exit(f"ADB バックエンドが使えない（{rag_adb.availability()}）。"
                 "先に migrate を適用すること。")
    ensure_spike_store()
    ocr = OcrCounter().install()
    client = TestClient(app)
    existing = client.get("/api/rag/files").json()["files"]
    if existing:
        sys.exit(f"箱に {len(existing)} 件のファイルが残っている。"
                 "先に teardown.py --yes で片付けること（引用の由来が混ざる）。")

    results = {
        "シナリオ0（抽出口・頁つき / 上限 422）": scenario_0(client, ocr),
        "シナリオ1（スキャン PDF が検索でヒット・引用に頁）": scenario_1(client, ocr),
        "シナリオ2（画像が検索でヒット）": scenario_2(client, ocr),
        "シナリオ3（対照: テキスト層のある PDF は OCR を通らない）": scenario_3(client, ocr),
    }
    lines = "\n".join(f"- {'PASS' if v else 'FAIL'} — {k}" for k, v in results.items())
    write("summary.md", f"""# PREP-03 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント /
OCI Document Understanding（既定エンジン）・OCI Generative AI（埋め込み・生成）は実物。

{lines}

## OCR 呼び出しの全記録（このセッション）

{fence(json.dumps(ocr.calls, ensure_ascii=False, indent=2))}

> スキャン PDF 2 ページ = 1 回 / 画像 1 枚 = 1 回。テキスト層のある PDF では 1 度も呼ばれない。
""")
    banner("結果")
    print(lines)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
