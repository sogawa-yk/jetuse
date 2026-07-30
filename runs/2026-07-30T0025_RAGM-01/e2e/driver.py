"""RAGM-01 実環境 E2E ドライバ（jetuse-dev / 共有 loop ADB のタスク専用スキーマ）。

モックを使わない: FastAPI アプリ（service.main）を **実 ADB・実 OCI Generative AI** に
つないだまま TestClient で叩き、取り込み → 検索 → 引用 までアプリの実経路を通す。

隔離:
- DB: 共有 loop ADB（jetuse-loop-adb）の **タスク専用スキーマ JETUSE_RAGM01**（ADB は増やさない）
- OCI: Vector Store 名は `jetuse-spike-ragm01-<run tag>`（run 固有。他タスクの資源に触れない）

使い方:
  PYTHONPATH=packages/api .venv/bin/python runs/<run-id>/e2e/driver.py <setup|s1|s2|guard|teardown>

資格情報・OCID は環境変数から取り、証跡には書かない（ログの識別子は redact_evidence.py で伏字化）。
"""

import json
import os
import pathlib
import secrets
import sys
import time

EVID = pathlib.Path(__file__).resolve().parent
LEDGER = pathlib.Path(os.environ["RAGM01_LEDGER"])  # scratchpad（リポジトリ外）

MODEL = "gpt-oss-120b"
QUESTION = "在庫照会APIは一度に最大何件まで返しますか。件数と根拠の版を答えてください。"

# 架空データ（顧客データではない）。1 チャンク = 1 ファイル（属性はファイル単位 — SPIKE-M1 ①-a）
DOCS = [
    {
        "name": "inventory-api-spec-v1.md",
        "body": (
            "架空サンプル 在庫連携API仕様書 v1.0（旧版）\n"
            "在庫照会API GET /v1/inventory は一度に最大100件まで返却する。\n"
            "この規定は v1.0 時点のものであり、後続の版で改定されている。\n"
        ),
        "attributes": {
            "file": "架空サンプル_在庫連携API仕様書.xlsx", "version": "1.0",
            "sheet": "API一覧", "cells": "B12:F12", "kind": "spec", "current_version": "N",
        },
    },
    {
        "name": "inventory-api-spec-v2.md",
        "body": (
            "架空サンプル 在庫連携API仕様書 v2.0（最新版）\n"
            "在庫照会API GET /v1/inventory は一度に最大200件まで返却する。\n"
            "v1.0 の100件という上限は本版で廃止された。\n"
        ),
        "attributes": {
            "file": "架空サンプル_在庫連携API仕様書.xlsx", "version": "2.0",
            "sheet": "API一覧", "cells": "B18:F18", "kind": "spec", "current_version": "Y",
        },
    },
]

CURRENT_ONLY = {"type": "eq", "key": "current_version", "value": "Y"}


def _client():
    from fastapi.testclient import TestClient

    from service.main import app

    return TestClient(app)


def _ledger() -> dict:
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}


def _save(led: dict) -> None:
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=2))
    LEDGER.chmod(0o600)


def _write(name: str, text: str) -> None:
    (EVID / name).write_text(text)
    print(f"  -> {name}")


def _sse_events(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


def _ask(client, filters: dict | None) -> tuple[str, list[dict], str]:
    payload = {
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
    }
    if filters is not None:
        payload["rag_filters"] = filters
    res = client.post("/api/chat/stream", json=payload)
    assert res.status_code == 200, (res.status_code, res.text[:500])
    evs = _sse_events(res.text)
    answer = "".join(e.get("delta", "") for e in evs)
    cites = next((e["citations"] for e in evs if "citations" in e), [])
    errs = [e["error"] for e in evs if "error" in e]
    assert not errs, errs
    return answer, cites, res.text


# --- setup ---------------------------------------------------------------


def setup() -> None:
    """run 固有の Vector Store を作り、登録簿(rag_stores)へ結び付ける。

    アプリの ensure_store が作る名前は `jetuse-rag-<owner>` で検証用接頭辞を付けられない
    ため、`jetuse-spike-ragm01-` を冠した箱をこちらで作って登録簿に置く。以降の
    取り込み・検索・削除はすべてアプリの実経路が同じ箱を使う。
    """
    from jetuse_core.db import connect
    from jetuse_core.genai import make_cp_client, make_inference_client

    led = _ledger()
    if led.get("vector_store_id"):
        print("already set up:", led["run_tag"])
        return
    tag = secrets.token_hex(3)
    name = f"jetuse-spike-ragm01-{tag}"
    cp = make_cp_client()
    vs = cp.vector_stores.create(name=name, metadata={"owner": "ragm01-e2e"})
    led = {"run_tag": tag, "vector_store_name": name, "vector_store_id": vs.id, "files": []}
    _save(led)  # 作成直後に記録（途中で落ちても片付けられるように）
    for _ in range(60):
        if cp.vector_stores.retrieve(vector_store_id=vs.id).status == "completed":
            break
        time.sleep(2)
    dp = make_inference_client(with_project=True)
    for _ in range(60):  # CP completed 後の DP 伝播待ち（SPIKE-03 / ①-f）
        try:
            dp.vector_stores.files.list(vector_store_id=vs.id)
            break
        except Exception:
            time.sleep(5)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM rag_stores WHERE owner_sub = :o", o="dev-user")
        cur.execute(
            "INSERT INTO rag_stores(owner_sub, vector_store_id) VALUES (:o, :v)",
            o="dev-user", v=vs.id,
        )
        conn.commit()
    print(f"vector store ready: {name}")


# --- scenario 1: 版フィルタの対照 ----------------------------------------


def scenario1() -> None:
    client = _client()
    led = _ledger()
    lines = ["# シナリオ1: 版違い2件の取り込みと版フィルタの対照（実 API）", ""]

    if not led.get("files"):
        for d in DOCS:
            res = client.post(
                "/api/rag/files",
                files={"file": (d["name"], d["body"].encode(), "text/markdown")},
                data={"attributes": json.dumps(d["attributes"], ensure_ascii=False)},
            )
            assert res.status_code == 200, res.text[:500]
            led["files"].append({"id": res.json()["id"], "name": d["name"]})
            _save(led)
            print("uploaded", d["name"], res.json()["status"])
    lines += ["## 取り込み（POST /api/rag/files + attributes）", "",
              "| ファイル | version | current_version | cells |", "|---|---|---|---|"]
    for d in DOCS:
        a = d["attributes"]
        lines.append(f"| `{d['name']}` | {a['version']} | {a['current_version']} | {a['cells']} |")

    deadline = time.time() + 600
    while time.time() < deadline:
        files = client.get("/api/rag/files").json()["files"]
        states = {f["filename"]: f["status"] for f in files}
        if all(states.get(d["name"]) == "completed" for d in DOCS):
            break
        time.sleep(10)
    else:
        raise SystemExit(f"取り込みが completed にならない: {states}")
    lines += ["", f"取り込み状態: {json.dumps(states, ensure_ascii=False)}", ""]

    # 属性が Vector Store 側に保持されていることを retrieve で直接確認（①-a）
    from jetuse_core.genai import make_inference_client
    from jetuse_core.rag import list_files as db_files

    dp = make_inference_client(with_project=True)
    rows = {r["filename"]: r["oci_file_id"] for r in db_files("dev-user")}
    stored = {}
    for d in DOCS:
        vf = dp.vector_stores.files.retrieve(
            vector_store_id=led["vector_store_id"], file_id=rows[d["name"]]
        )
        stored[d["name"]] = vf.attributes
    lines += ["## Vector Store に保持された attributes（files.retrieve の実値）", "",
              "```json", json.dumps(stored, ensure_ascii=False, indent=2), "```", ""]
    for d in DOCS:
        assert stored[d["name"]]["cells"] == d["attributes"]["cells"]
        assert "sha256" in stored[d["name"]]  # 取り込み側で補完される

    ans_all, cites_all, raw_all = _ask(client, None)
    time.sleep(2)
    ans_cur, cites_cur, raw_cur = _ask(client, CURRENT_ONLY)

    def names(cs):
        return sorted(c["filename"] for c in cs)

    def versions(cs):
        return sorted(c.get("source", {}).get("version", "-") for c in cs)

    lines += [
        "## 対照（同一クエリ / フィルタ有無だけが違う）", "",
        f"クエリ: `{QUESTION}`", "",
        "| 条件 | 引用ファイル | 引用の version |", "|---|---|---|",
        f"| フィルタ無し | {names(cites_all)} | {versions(cites_all)} |",
        f"| `current_version=Y` | {names(cites_cur)} | {versions(cites_cur)} |",
        "", "### フィルタ無しの回答", "", "```", ans_all.strip(), "```",
        "", "### 版フィルタ有りの回答", "", "```", ans_cur.strip(), "```",
        "", "### 引用（フィルタ有り・構造化された出典）", "",
        "```json", json.dumps(cites_cur, ensure_ascii=False, indent=2), "```",
    ]
    _write("scenario-1.md", "\n".join(lines) + "\n")
    _write("scenario-1-nofilter.sse.txt", raw_all)
    _write("scenario-1-filtered.sse.txt", raw_cur)

    stale = DOCS[0]["name"]
    assert stale in names(cites_all), f"対照が成立しない（フィルタ無しで旧版が引かれない）: {names(cites_all)}"
    assert stale not in names(cites_cur), f"版フィルタで旧版が残った: {names(cites_cur)}"
    assert cites_cur, "版フィルタで引用が空（0件では対照にならない）"
    assert all(c.get("source", {}).get("current_version") == "Y" for c in cites_cur)
    print("scenario1 PASS")


# --- scenario 2: 引用にセル範囲まで載る ----------------------------------


def scenario2() -> None:
    client = _client()
    _, cites, raw = _ask(client, CURRENT_ONLY)
    assert cites, "引用が空"
    top = cites[0]
    for key in ("file_id", "filename", "score"):
        assert key in top, f"後方互換フィールド {key} が欠けた: {top}"
    src = top.get("source") or {}
    for key in ("file", "version", "sheet", "cells"):
        assert src.get(key), f"構造化出典に {key} が無い: {top}"
    assert "text" in top and top["text"], "本文抜粋が無い"
    body = "\n".join([
        "# シナリオ2: 回答の citations にセル範囲まで載る（実レスポンス）", "",
        "`GET /api/chat/stream` の `citations` イベント（版フィルタ有り）。",
        "`source` は本文に埋め込んだ文字列ではなく、取り込み時 attributes に由来する構造化値。", "",
        "```json", json.dumps(cites, ensure_ascii=False, indent=2), "```", "",
        "## 後方互換の確認", "",
        f"- 既存フロントが読む `file_id` / `filename` / `score`: {json.dumps({k: top[k] for k in ('file_id', 'filename', 'score')}, ensure_ascii=False)}",
        f"- 追加フィールド: `source`（{', '.join(sorted(src))}）/ `text` / `chunk_id`",
        "", "## セル範囲", "",
        f"- sheet = `{src.get('sheet')}` / cells = `{src.get('cells')}` / version = `{src.get('version')}`",
    ])
    _write("scenario-2.md", body + "\n")
    _write("scenario-2.sse.txt", raw)
    print("scenario2 PASS")


# --- guard: 否定側（実 API 経由） ----------------------------------------


def guard() -> None:
    client = _client()
    rows = []

    r1 = client.post("/api/chat/stream", json={
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
        "rag_filters": {"type": "eq", "key": "current_verison", "value": "Y"},
    })
    rows.append(("未知のフィルタキー `current_verison`", r1.status_code, 422,
                 "current_verison" in r1.text))

    r2 = client.post("/api/chat/stream", json={
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
        "rag_filters": {"type": "in", "key": "version", "values": ["1.0", "2.0"]},
    })
    rows.append(("未対応の `in` フィルタ", r2.status_code, 422, "in" in r2.text))

    r3 = client.post(
        "/api/rag/files",
        files={"file": ("x.md", b"body", "text/markdown")},
        data={"attributes": json.dumps({"versoin": "2.0"})},
    )
    rows.append(("未知の属性キー `versoin`", r3.status_code, 422, "versoin" in r3.text))

    r4 = client.post(
        "/api/rag/files",
        files={"file": ("x.md", b"body", "text/markdown")},
        data={"attributes": json.dumps({"cells": "x" * 513})},
    )
    rows.append(("属性値 513 文字（上限 512 超）", r4.status_code, 422, "512" in r4.text))

    r5 = client.post("/api/chat/stream", json={
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
        "rag_backend": "select_ai", "rag_filters": CURRENT_ONLY,
    })
    rows.append(("filters × select_ai バックエンド", r5.status_code, 400,
                 "select_ai" in r5.text))

    r6 = client.post("/api/chat/stream", json={
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
        "agent": True, "rag_filters": CURRENT_ONLY,
    })
    rows.append(("filters × エージェントモード（渡す口が無い）", r6.status_code, 400,
                 "agent mode" in r6.text))

    r7 = client.post("/api/chat/stream", json={
        "model": MODEL, "messages": [{"role": "user", "content": QUESTION}], "rag": True,
        "rag_filters": {"type": "and", "filters": [CURRENT_ONLY, None]},
    })
    rows.append(("複合フィルタの子が null", r7.status_code, 422, "filter" in r7.text))

    after = client.get("/api/rag/files").json()["files"]
    lines = [
        "# 否定側: 通ってはいけない入力（実 API・アプリ全経路）", "",
        "SPIKE-M1 ①-b で「存在しないキーで絞ると **エラーにならず 0 件**」を実測している。",
        "アプリが手前で弾かないと、利用者は「該当なし」と「キー名を間違えた」を区別できない。", "",
        "| ケース | 実 status | 期待 | 本文に根拠 |", "|---|---|---|---|",
    ]
    for label, got, want, hint in rows:
        lines.append(f"| {label} | {got} | {want} | {'あり' if hint else 'なし'} |")
    lines += ["", f"拒否後もファイルは増えていない（{len(after)} 件のまま）。"]
    _write("guard.md", "\n".join(lines) + "\n")
    for label, got, want, hint in rows:
        assert got == want and hint, (label, got, want, hint)
    assert len(after) == len(DOCS)
    print("guard PASS")


# --- teardown ------------------------------------------------------------


def _absent(fn, *, tries: int = 12, wait: int = 5) -> bool:
    """`fn()` が NotFoundError を返すまで待つ。**NotFound 以外の例外は不在の証拠にしない**。

    認証失敗・5xx・通信断を「消えた」と読むと、消えていないものを消えたと報告する
    （レビュー F-006）。判定できなければ False を返し、呼び出し側が非ゼロ終了する。
    """
    from openai import NotFoundError

    for _ in range(tries):
        try:
            fn()
        except NotFoundError:
            return True
        time.sleep(wait)
    return False


def teardown() -> None:
    """この run が作ったものだけを消し、**不在を NotFound で確かめる**（台帳の id のみ）。"""
    from jetuse_core.db import connect
    from jetuse_core.genai import make_cp_client, make_inference_client
    from jetuse_core.rag import list_files as db_files

    client = _client()
    led = _ledger()
    dp = make_inference_client(with_project=True)
    lines = ["# 片付け（この run が作った資源のみ・不在は NotFound で確認）", ""]
    failures: list[str] = []

    oci_ids = {r["id"]: r["oci_file_id"] for r in db_files("dev-user")}
    for f in led.get("files", []):
        res = client.delete(f"/api/rag/files/{f['id']}")
        oci_id = oci_ids.get(f["id"])
        gone = _absent(lambda: dp.files.retrieve(oci_id)) if oci_id else False
        lines.append(
            f"- DELETE /api/rag/files/<id>（{f['name']}） -> {res.status_code} / "
            f"Files API 再照会で NotFound: {gone}"
        )
        if res.status_code != 200 or not gone:
            failures.append(f"file {f['name']}")
    remaining = client.get("/api/rag/files").json()["files"]
    lines.append(f"- 登録簿の残ファイル: {len(remaining)} 件")
    if remaining:
        failures.append("rag_files に残行")

    vs_id = led.get("vector_store_id")
    if vs_id:
        make_cp_client().vector_stores.delete(vector_store_id=vs_id)
        gone = _absent(lambda: dp.vector_stores.retrieve(vector_store_id=vs_id))
        lines.append(f"- Vector Store `{led['vector_store_name']}` 削除 -> NotFound: {gone}")
        if not gone:
            failures.append("vector store")
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM rag_stores WHERE owner_sub = :o", o="dev-user")
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM rag_files")
        rows = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM rag_stores")
        stores = cur.fetchone()[0]
        lines.append(f"- 登録簿(JETUSE_RAGM01): rag_stores {stores} 行 / rag_files {rows} 行")
        if rows or stores:
            failures.append("登録簿に残行")
    if failures:
        lines.append("")
        lines.append(f"**未確認/失敗: {failures} — 台帳は残す（人間が確認すること）**")
    _write("teardown.md", "\n".join(lines) + "\n")
    if failures:
        raise SystemExit(f"teardown 未完了: {failures}")
    LEDGER.unlink(missing_ok=True)
    print("teardown done")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"setup": setup, "s1": scenario1, "s2": scenario2,
             "guard": guard, "teardown": teardown}
    if step == "all":
        for fn in (setup, scenario1, scenario2, guard):
            fn()
    else:
        steps[step]()
