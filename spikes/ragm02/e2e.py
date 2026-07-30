"""RAGM-02 の実環境 E2E（tasks/RAGM-02.md の「E2E シナリオ」）。

**実装モジュール `jetuse_core.rag_adb` をそのまま呼ぶ**（検証用の別実装を書かない）。
接続先はこの run 固有の検証用スキーマ（共有 loop ADB 内・RAGM-01 と隔離）。

  1. 同一ファイル由来の複数チャンクが**別々の** cells を返す（マネージド VS との決定的な差）
  2. 業務表と JOIN したベクタ検索が 1 クエリで成立する
  3. 版フィルタの対照（有り 0 件 / 無し ヒット）
  4. 取り込み → 検索 → **回答**まで通る + 取り込み状況バッジに adb が載る

証跡は runs/<run-id>/e2e/scenario-<n>.md へ書く。
実行: PYTHONPATH=spikes/ragm02:packages/api .venv/bin/python spikes/ragm02/e2e.py
"""

import json
import os
import sys
import threading
import time

import oracledb

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret

SCHEMA = require_schema()
OWNER = "e2e-ragm02"
HTTP_OWNER = "dev-user"  # 認証無効時の AuthContext.subject（HTTP 経路の名前空間）
REGISTRY_TABLE = "RAGM02_DOC_REGISTRY"  # JOIN 相手の業務表（サンプル）
DOC_A = "サンプル在庫連携API仕様書.md"
DOC_B = "サンプル配送料金規程.md"

# 架空の文書（顧客データは使わない）。1 ファイルが複数チャンクに割れる長さにする。
DOC_A_V1 = "\n".join([
    "# サンプル在庫連携API仕様書 v1",
    "## 第1章 在庫照会API",
    "在庫照会API GET /v1/inventory は在庫数と引当可能数を返す。" + "本章の説明が続く。" * 60,
    "## 第2章 レート制限",
    "レート制限は1分あたり300リクエストである。超過時は429を返す。" + "旧版の記述である。" * 60,
    "## 第3章 データ保持期間",
    "明細データの保持期間は6か月とする。" + "旧版の補足説明。" * 60,
])
DOC_A_V2 = "\n".join([
    "# サンプル在庫連携API仕様書 v2",
    "## 第1章 在庫照会API",
    "在庫照会API GET /v1/inventory は在庫数と引当可能数を返す。" + "本章の説明が続く。" * 60,
    "## 第2章 レート制限",
    "レート制限は1分あたり600リクエストである。超過時は429を返す。" + "現行版の記述である。" * 60,
    "## 第3章 データ保持期間",
    "明細データの保持期間は13か月とする。" + "現行版の補足説明。" * 60,
])
DOC_B_TEXT = "\n".join([
    "# サンプル配送料金規程",
    "## 第1章 運賃計算",
    "宅配便の運賃は重量区分と距離区分の組み合わせで決まる。" + "計算例が続く。" * 60,
    "## 第2章 チャーター便",
    "チャーター便は車両単位の料金で、重量区分を用いない。" + "適用条件が続く。" * 60,
])

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"


def write(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text)
    print(f"  wrote {EVIDENCE / name}")


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をタスク専用スキーマへ向ける（他タスクの資源に触れない）。"""
    prepare_env()  # ADB_WALLET_DIR / ADB_WALLET_PASSWORD / ADB_DSN / ADB_COMPARTMENT_OCID
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    s = get_settings()
    if s.adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {s.adb_user}。E2E は {SCHEMA} でしか実行しない。中止。")


def ingest(rag_adb, conn, owner: str, file_id: str, name: str, body: bytes) -> int:
    """アップロード経路と同じ順序で取り込む（**先に台帳行 → そのあと取り込み**）。

    `rag_adb.ingest` は台帳行（`rag_files`）を `FOR UPDATE` で押さえてから
    チャンクを作る（取り込み中の削除と直列化するため）。実運用の `rag.add_file` も
    この順序なので、E2E も同じにする。
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM rag_files WHERE id = :id AND owner_sub = :o",
                id=file_id, o=owner)
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO rag_files(id, owner_sub, filename, oci_file_id, status, bytes)"
                    " VALUES (:id, :o, :f, :ofi, 'completed', :b)",
                    id=file_id, o=owner, f=name[:400], ofi=f"oci-{file_id}", b=len(body))
        conn.commit()
    return rag_adb.ingest(owner, file_id, name, body)


def fence(text: str) -> str:
    return "```\n" + text.rstrip() + "\n```"


# --- シナリオ ------------------------------------------------------------------


def scenario_1(rag_adb, conn) -> bool:
    banner("シナリオ1: 同一ファイル由来の複数チャンクが別々の cells を返す")
    n = ingest(rag_adb, conn, OWNER, "fileA", DOC_A, DOC_A_V2.encode())
    print(f"  取り込みチャンク数: {n}")
    hits = rag_adb.search(OWNER, "レート制限は1分あたり何リクエストですか", k=5,
                          filters={"file": DOC_A})
    lines = [f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
             f" | cells={h['source']['cells']} | score={h['score']}"
             f" | {h['text'][:40]}..." for h in hits]
    cells = [h["source"]["cells"] for h in hits]
    same_file = len({h["source"]["file"] for h in hits}) == 1
    distinct_cells = len(set(cells)) == len(cells)
    ok = n >= 2 and len(hits) >= 2 and distinct_cells and same_file
    write("scenario-1.md", f"""# シナリオ1 — チャンク単位の出典（同一ファイルで cells が異なる）

対象: `{DOC_A}`（架空）を 1 ファイルとして取り込み、同一ファイル内を検索した。

- 取り込みチャンク数: **{n}**
- 検索: `rag_adb.search(filters={{"file": "{DOC_A}"}})`

{fence(chr(10).join(lines))}

- 返ったチャンクの cells: `{cells}`
- すべて同一ファイル由来: **{same_file}** / cells がすべて異なる: **{distinct_cells}**

判定: **{'PASS' if ok else 'FAIL'}**

> マネージド Vector Store は属性が**ファイル単位**で、1 ファイル 5 チャンクでも
> 属性は 1 種類しか返らない（SPIKE-M1 ①-a の実測）。ここが `adb` バックエンドの決定的な差。
""")
    return ok


def scenario_2(rag_adb, conn) -> bool:
    banner("シナリオ2: 業務表と JOIN したベクタ検索が 1 クエリで成立する")
    cur = conn.cursor()
    try:
        cur.execute(f"DROP TABLE {REGISTRY_TABLE} PURGE")
    except oracledb.DatabaseError as e:
        if "ORA-00942" not in str(e):
            raise
    cur.execute(f"""
        CREATE TABLE {REGISTRY_TABLE} (
          doc_file VARCHAR2(400) PRIMARY KEY,
          owner_dept VARCHAR2(64) NOT NULL,
          confidential CHAR(1) DEFAULT 'N' NOT NULL
        )""")
    cur.executemany(
        f"INSERT INTO {REGISTRY_TABLE}(doc_file, owner_dept, confidential) VALUES (:1, :2, :3)",
        [(DOC_A, "情報システム部", "N"), (DOC_B, "物流部", "Y")],
    )
    conn.commit()
    ingest(rag_adb, conn, OWNER, "fileB", DOC_B, DOC_B_TEXT.encode())

    import array

    from jetuse_core.embeddings import embed

    qvec = array.array("f",
                       embed(["運賃や在庫の決まりを知りたい"], input_type="SEARCH_QUERY")[0])
    sql = f"""
SELECT c.doc_file, r.owner_dept, r.confidential, c.sheet_name, c.cells,
       ROUND(VECTOR_DISTANCE(c.embedding, :q, COSINE), 4) AS dist
FROM {rag_adb.TABLE} c
JOIN {REGISTRY_TABLE} r ON r.doc_file = c.doc_file
WHERE c.owner_sub = :o AND c.current_version = 'Y' AND r.confidential = 'N'
ORDER BY VECTOR_DISTANCE(c.embedding, :q, COSINE)
FETCH FIRST 5 ROWS ONLY
"""
    cur.execute(sql, q=qvec, o=OWNER)
    rows = cur.fetchall()
    depts = {r[1] for r in rows}
    confidential = [r for r in rows if r[2] != "N"]
    ok = bool(rows) and not confidential and depts == {"情報システム部"}
    body = "\n".join(" | ".join(str(v) for v in r) for r in rows)
    write("scenario-2.md", f"""# シナリオ2 — 業務表と JOIN したベクタ検索（1 クエリ）

サンプル業務表 `{REGISTRY_TABLE}`（文書管理台帳: 所管部門・機密フラグ）を作り、
チャンク表とベクタ検索を**同じ 1 本の SQL** で結合した。機密扱いの文書は結合条件で落ちる。

{fence(sql.strip())}

実行結果（doc_file | owner_dept | confidential | sheet | cells | 距離）:

{fence(body)}

- 返った文書の所管部門: `{sorted(depts)}` / 機密フラグ 'Y' の混入: **{len(confidential)} 件**

判定: **{'PASS' if ok else 'FAIL'}**

> 業務データ側の条件（機密・所管）で候補を絞ったうえで類似度順に返している。
> マネージド Vector Store では業務表と結合できない（ADR-0020 の比較表）。
""")
    return ok


def _fmt_hits(hits: list[dict]) -> str:
    return "\n".join(
        f"{h['source']['chunk_id']} | version={h['source']['version']}"
        f" | current={h['source']['current_version']} | cells={h['source']['cells']}"
        f" | {h['text'][:32]}..." for h in hits)


def scenario_3(rag_adb, conn) -> bool:
    banner("シナリオ3: 版フィルタの対照（有り 0 件 / 無し ヒット）")
    # 同名ファイルを再取り込み → 旧チャンクは current_version='N' に落ちる
    ingest(rag_adb, conn, OWNER, "fileA_v1", DOC_A, DOC_A_V1.encode())
    ingest(rag_adb, conn, OWNER, "fileA_v2", DOC_A, DOC_A_V2.encode())
    q = "レート制限は1分あたり何リクエストですか"
    no_filter = rag_adb.search(OWNER, q, k=10, filters={"file": DOC_A})
    filtered = rag_adb.search(OWNER, q, k=10,
                              filters={"file": DOC_A, "current_version": "Y"})
    stale_all = [h["source"]["chunk_id"] for h in no_filter
                 if h["source"]["current_version"] == "N"]
    stale_filtered = [h["source"]["chunk_id"] for h in filtered
                      if h["source"]["current_version"] == "N"]
    versions = sorted({h["source"]["version"] for h in filtered})
    ok = bool(stale_all) and not stale_filtered and bool(filtered)

    write("scenario-3.md", f"""# シナリオ3 — 版フィルタの対照

同名ファイル `{DOC_A}` を v1 → v2 の順に取り込んだ（再取り込みで旧チャンクは
`current_version='N'` に落ち、版が上がる）。同じ問いを 2 通りで検索した。

## A: フィルタ無し（対照）

{fence(_fmt_hits(no_filter))}

旧版（`current_version='N'`）のヒット: **{len(stale_all)} 件** `{stale_all}`

## B: `current_version='Y'` で絞り込み

{fence(_fmt_hits(filtered))}

旧版のヒット: **{len(stale_filtered)} 件** / 返った版: `{versions}`

判定: **{'PASS' if ok else 'FAIL'}**（対照 A で旧版が返り、B で 0 件になること）
""")
    return ok


def scenario_4(rag_adb, conn) -> bool:
    """`POST /api/chat/stream`（**ASGI 統合テスト**）で `rag_backend='adb'` を通す。

    モジュール直呼びではなく、アプリのルーティング・SSE・ディスパッチ・citations 整形を
    そのまま通す（相手の ADB と LLM は実物）。**配備済みインスタンスへの実ネットワーク
    呼び出しではない**（プロセス内 ASGI。認証・LB・Resource Principal は通っていない）。
    """
    banner("シナリオ4: ASGI 経路（rag_backend='adb'）で取り込み → 検索 → 回答")
    from fastapi.testclient import TestClient
    from jetuse_core import rag
    from service.main import app

    # HTTP 経路の名前空間は認証コンテキストの subject（認証無効時は dev-user）。
    # シナリオ 1〜3 の OWNER とは別なので、この経路用に同じ文書を取り込んでおく。
    http_owner = HTTP_OWNER
    ingest(rag_adb, conn, http_owner, "fileA_v1_http", DOC_A, DOC_A_V1.encode())
    ingest(rag_adb, conn, http_owner, "fileA_v2_http", DOC_A, DOC_A_V2.encode())
    question = "レート制限は1分あたり何リクエストですか"
    res = TestClient(app).post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": question}],
        "rag": True, "rag_backend": "adb",
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    cites = next((f["citations"] for f in frames if "citations" in f), [])
    errors = [f["error"] for f in frames if "error" in f]

    files = [{"id": "fileA_v2_http", "filename": DOC_A, "status": "completed"},
             {"id": "not-ingested", "filename": "未取り込み.md", "status": "processing"}]
    # 他バックエンドは実環境の状態に依存するため、ここで見るのは adb の欄だけ
    badges = [f["backends"]["adb"] for f in rag.attach_backend_status(http_owner, files)]
    has_cells = bool(cites) and all(c["source"]["cells"] for c in cites)
    compat = bool(cites) and all({"file_id", "filename", "score"} <= set(c) for c in cites)
    current_only = bool(cites) and all(c["source"]["current_version"] == "Y" for c in cites)
    # 現行版(v2)は 600、旧版(v1)は 300。旧版を根拠にしていたら 300 が出る
    correct = "600" in answer and "300" not in answer
    ok = (res.status_code == 200 and not errors and correct and has_cells and compat
          and current_only and badges == ["indexed", "pending"])
    has_600, no_300 = "600" in answer, "300" not in answer
    write("scenario-4.md", f"""# シナリオ4 — アプリ経路で回答まで（`rag_backend=adb`）

`POST /api/chat/stream`（`rag: true` / `rag_backend: "adb"`）を実 ADB・実 LLM に対して実行した。
モジュールの直呼びではなく、ルーティング・SSE・ディスパッチ・citations 整形を通している。

> **これはプロセス内の ASGI 統合テスト**（`TestClient`）であり、配備済みインスタンスへの
> 実ネットワーク呼び出しではない。認証・LB・Resource Principal 経路は通っていない
> （`SKIPPED.md` 参照）。相手側の ADB と LLM は実物。
名前空間は認証コンテキストの subject（`{http_owner}`）。
同じ架空文書を v1 → v2 の順に取り込んである。

- HTTP ステータス: **{res.status_code}** / エラーフレーム: **{len(errors)} 件**
- 質問: `{question}`

## 回答（SSE の delta を連結）

{fence(answer)}

- 現行版の値 **600** を含む: **{has_600}** / 旧版の値 **300** を含まない: **{no_300}**

## citations（既存契約 + チャンク単位の出典）

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:2000])}

- 既存契約 `{{file_id, filename, score}}` を全件が保持: **{compat}**
- 全件に cells（チャンク単位の出典）がある: **{has_cells}**
- 全件が現行版（`current_version='Y'`）: **{current_only}**

## 取り込み状況バッジ（`rag.attach_backend_status` の `adb` 欄）

取り込み済み / 未取り込みの 2 件で: `{badges}`（期待 `['indexed', 'pending']`）

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_5(rag_adb, conn) -> bool:
    """削除は台帳行とチャンクが同一トランザクションで消えること（実 DB で確認）。"""
    banner("シナリオ5: 削除の原子性（台帳行とチャンクが同時に消える）")
    from jetuse_core import rag

    content = "# 削除確認用の架空文書\n" + ("削除されるべき本文。" * 40 + "\n") * 3
    file_id = "fileDel"
    ingest(rag_adb, conn, OWNER, file_id, "サンプル削除確認文書.md", content.encode())
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {rag_adb.TABLE} WHERE owner_sub = :o AND file_id = :f",
                o=OWNER, f=file_id)
    before = cur.fetchone()[0]
    row = rag._delete_row(OWNER, file_id)  # 台帳行 + チャンクを 1 トランザクションで削除
    cur.execute(f"SELECT COUNT(*) FROM {rag_adb.TABLE} WHERE owner_sub = :o AND file_id = :f",
                o=OWNER, f=file_id)
    after = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM rag_files WHERE id = :id AND owner_sub = :o",
                id=file_id, o=OWNER)
    ledger_left = cur.fetchone()[0]
    ok = before > 0 and after == 0 and ledger_left == 0 and row is not None
    write("scenario-5.md", f"""# シナリオ5 — 削除の原子性

RAG 台帳（`rag_files`）の行と ADB 自前索引のチャンクは同じ ADB にあるので、
**同一トランザクション**で消す。「API は削除成功なのにチャンクだけ残って以後の回答に混ざる」
が構造的に起きないことを実 DB で確認した。

| | 件数 |
|---|---|
| 削除前のチャンク | {before} |
| 削除後のチャンク | {after} |
| 削除後の台帳行 | {ledger_left} |

判定: **{'PASS' if ok else 'FAIL'}**（削除前 > 0 かつ削除後 0 かつ台帳行 0）

> 単体テストでは「チャンク削除が失敗したら commit しない（台帳行も残る）」も固定してある
> （`packages/api/tests/test_rag_adb.py`）。
""")
    return ok


def scenario_6(rag_adb, conn) -> bool:
    """現行版のファイルを削除したら、残っている旧版が現行へ戻ること（実 DB）。"""
    banner("シナリオ6: 現行版の削除で旧版が現行へ戻る")
    from jetuse_core import rag

    doc = "サンプル版戻し確認文書.md"
    ingest(rag_adb, conn, OWNER, "fileR_v1", doc, DOC_A_V1.encode())
    ingest(rag_adb, conn, OWNER, "fileR_v2", doc, DOC_A_V2.encode())
    q = "レート制限は1分あたり何リクエストですか"
    before = rag_adb.search(OWNER, q, k=5, filters={"file": doc, "current_version": "Y"})
    rag._delete_row(OWNER, "fileR_v2")  # 現行版(v2)を削除
    after = rag_adb.search(OWNER, q, k=5, filters={"file": doc, "current_version": "Y"})
    v_before = sorted({h["source"]["version"] for h in before})
    v_after = sorted({h["source"]["version"] for h in after})
    ok = bool(before) and bool(after) and v_before != v_after
    write("scenario-6.md", f"""# シナリオ6 — 現行版を削除したら旧版が現行へ戻る

同名ファイル `{doc}` を v1 → v2 と取り込み（v1 は `current_version='N'` に落ちる）、
その後 **v2 を削除**した。何もしないと v1 が `N` のまま残り、既定の検索（現行版のみ）から
永久に見えなくなる。削除と同じトランザクションで最新の残存版を現行へ戻している。

| | 現行版で見えるチャンク | 版 |
|---|---|---|
| v2 削除前 | {len(before)} 件 | `{v_before}` |
| v2 削除後 | {len(after)} 件 | `{v_after}` |

判定: **{'PASS' if ok else 'FAIL'}**（削除後も現行版で検索でき、版が繰り下がっていること）
""")
    return ok


def scenario_7(rag_adb, conn) -> bool:
    """同名ファイルの**同時取り込み**でも、現行版が 1 つに定まること（実 ADB・2 接続）。

    レジストリ行を先に作ってからロックする方式が実環境で効くかを見る（SQL 文字列の
    存在確認では「効いた」ことにならない）。
    """
    banner("シナリオ7: 同名ファイルの同時取り込み（初回競合）")
    from concurrent.futures import ThreadPoolExecutor

    doc = "サンプル同時取り込み文書.md"
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {rag_adb.TABLE} WHERE owner_sub = :o AND doc_file = :f",
                o=OWNER, f=doc)
    cur.execute(f"DELETE FROM {rag_adb.DOC_TABLE} WHERE owner_sub = :o AND doc_file = :f",
                o=OWNER, f=doc)
    conn.commit()

    # 台帳行は**競争の前に**用意する（レース中に同じ接続を共有しない）。
    for n in (1, 2):
        cur.execute("INSERT INTO rag_files(id, owner_sub, filename, oci_file_id, status, bytes)"
                    " VALUES (:id, :o, :f, :ofi, 'completed', 1)",
                    id=f"fileC{n}", o=OWNER, f=doc, ofi=f"oci-fileC{n}")
    conn.commit()

    gate = threading.Barrier(2)
    spans: dict[int, tuple[float, float]] = {}

    def go(n: int) -> int:
        body = f"# 同時取り込み {n}\n" + ("同時取り込みの本文。" * 40 + "\n") * 2
        gate.wait()  # 2 スレッドを同時に critical section へ突入させる
        t0 = time.perf_counter()
        got = rag_adb.ingest(OWNER, f"fileC{n}", doc, body.encode())
        spans[n] = (t0, time.perf_counter())
        return got

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(go, [1, 2]))
    # 実行区間が重なっていれば、直列化はロックによるもの（逐次実行ではない）
    overlapped = spans[1][0] < spans[2][1] and spans[2][0] < spans[1][1]

    cur.execute(f"SELECT doc_version, current_version, COUNT(*) FROM {rag_adb.TABLE}"
                " WHERE owner_sub = :o AND doc_file = :f GROUP BY doc_version, current_version"
                " ORDER BY 1", o=OWNER, f=doc)
    rows = cur.fetchall()
    versions = {r[0] for r in rows}
    current = {r[0] for r in rows if r[1] == "Y"}
    cur.execute(f"SELECT COUNT(*) FROM {rag_adb.DOC_TABLE} WHERE owner_sub = :o AND doc_file = :f",
                o=OWNER, f=doc)
    registry_rows = cur.fetchone()[0]
    ok = (all(c > 0 for c in counts) and len(versions) == 2 and len(current) == 1
          and registry_rows == 1 and overlapped)
    body = "\n".join(f"{v} | current={c} | {n} 行" for v, c, n in rows)
    write("scenario-7.md", f"""# シナリオ7 — 同名ファイルの同時取り込み（初回競合）

同じ利用者・同じファイル名を **2 つの接続から同時に**取り込んだ（レジストリ行が
まだ無い「初回」の競合）。版採番はレジストリ行を作ってからロックするので直列化される。

- 取り込みチャンク数: `{counts}`
- 2 スレッドの取り込み区間が**時間的に重なった**: **{overlapped}**
  （重なっていなければ逐次実行になっており、ロックの検証にならない）

```
{body}
```

- 採番された版: `{sorted(versions)}`（重複していないこと）
- **現行版（`current_version='Y'`）の版: `{sorted(current)}`（1 つだけであること）**
- 文書レジストリの行数: {registry_rows}（1 であること）

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def main() -> None:
    use_task_schema()
    conn = connect_schema()  # 台帳ゲート（自分が作ったスキーマか）を通す
    banner("マイグレーション適用（deploy 相当）")
    from jetuse_core.migrate import migrate

    applied = migrate()
    print(f"  applied: {applied or '(up to date)'}")
    cur0 = conn.cursor()
    cur0.execute("SELECT table_name FROM user_tables WHERE table_name IN "
                 "('RAG_ADB_CHUNKS','RAG_ADB_DOCS','RAG_ADB_INGEST') ORDER BY 1")
    tables = [r[0] for r in cur0.fetchall()]
    write("migrate.md", f"""# マイグレーション適用（deploy 相当）

`python -m jetuse_core.migrate` を検証スキーマ `{SCHEMA}` に対して実行した。

```
applied: {applied or '(up to date — 既に適用済み)'}
```

RAGM-02 が足した 3 表の存在確認（`user_tables`）:

```
{chr(10).join(tables) or '(無し)'}
```

- `017_rag_adb.sql`（チャンク表）/ `018_rag_adb_docs.sql`（版のロック用レジストリ）/
  `019_rag_adb_ingest.sql`（取り込み状態・file_id 単位）は**それぞれ CREATE TABLE 1 文だけ**。
  Oracle の DDL は暗黙コミットなので、1 ファイルに複数 DDL を並べると途中失敗時に
  「表はあるが migration 未記録」で再実行不能になる。
- 索引は `rag_adb.ensure_indexes()` が冪等に作る（マイグレーションには置かない）。
""")

    from jetuse_core import rag_adb

    # 前回の E2E 残りを消してから始める（対照の件数が積み上がらないように）
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {rag_adb.TABLE} WHERE owner_sub IN (:o, :h)",
                o=OWNER, h=HTTP_OWNER)
    cur.execute(f"DELETE FROM {rag_adb.DOC_TABLE} WHERE owner_sub IN (:o, :h)",
                o=OWNER, h=HTTP_OWNER)
    cur.execute(f"DELETE FROM {rag_adb.INGEST_TABLE} WHERE owner_sub IN (:o, :h)",
                o=OWNER, h=HTTP_OWNER)
    cur.execute("DELETE FROM rag_files WHERE owner_sub IN (:o, :h)", o=OWNER, h=HTTP_OWNER)
    conn.commit()

    results = {
        "1": scenario_1(rag_adb, conn),
        "2": scenario_2(rag_adb, conn),
        "3": scenario_3(rag_adb, conn),
        "4": scenario_4(rag_adb, conn),
        "5": scenario_5(rag_adb, conn),
        "6": scenario_6(rag_adb, conn),
        "7": scenario_7(rag_adb, conn),
    }
    banner("結果")
    for k, v in results.items():
        print(f"  シナリオ{k}: {'PASS' if v else 'FAIL'}")
    cur.execute(f"SELECT COUNT(*) FROM {rag_adb.TABLE} WHERE owner_sub = :o", o=OWNER)
    print(f"  投入チャンク総数: {cur.fetchone()[0]}")
    write("summary.md", "# E2E 結果一覧\n\n" + "\n".join(
        f"- シナリオ{k}: **{'PASS' if v else 'FAIL'}** → `scenario-{k}.md`"
        for k, v in results.items()) + "\n")
    conn.close()
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
