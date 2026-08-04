"""PREP-01 の実環境 E2E（tasks/PREP-01.md の「E2E シナリオ」）。

**実装（`jetuse_core.extract_xlsx` / `rag` / `rag_adb` と FastAPI ルート）をそのまま呼ぶ**。
検証用の別実装は書かない。相手の ADB・OCI Generative AI（Files / Vector Stores / 埋め込み /
生成）はすべて実物。

  0. 抽出口 `POST /api/extract`（取り込まない）
  1. `adb` バックエンド: xlsx 由来のチャンクが**チャンクごとに異なるシート / セル範囲**を返す
  2. `vector_store`（マネージド）: 同じファイルの属性が**ファイル単位**になる（能力差の証跡）
  3. 版フィルタ（`current_version='Y'`）が xlsx 由来のチャンクにも効く

隔離: 共有 loop ADB の **run 固有スキーマ**（`JETUSE_PREP01_<乱数>`。ADB は増やさない）。
OCI 側の検証用資源は `jetuse-spike-prep01-` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部（`spikes/ragm02/common.py`）をそのまま再利用する（env で接頭辞だけ差し替え）。

実行（`E=SPIKE_SCHEMA_PREFIX=JETUSE_PREP01 SPIKE_HOME=/tmp/jetuse-prep01`,
      `P=PYTHONPATH=spikes/ragm02:spikes/prep01:packages/api`）:
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成（台帳つき）
  env $E $P .venv/bin/python spikes/prep01/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/prep01/teardown.py --yes  # OCI 側（ファイル・箱）
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes  # ADB スキーマ
"""

import json
import os
import re
import sys
import time

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret
from fixtures import DOC_NAME, PREFIX, RATE_LIMIT, workbook

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject（HTTP 経路の名前空間）
QUESTION = "レート制限は1分あたり何リクエストですか"

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
    """OCI 側の識別子は先頭だけ残す（実値をリポジトリに残さない）。"""
    if not value:
        return "(なし)"
    return value[:12] + "…" if len(value) > 12 else value


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をこの run のスキーマへ向ける（他タスクの資源に触れない）。"""
    prepare_env()  # ADB_WALLET_* / ADB_DSN / ADB_COMPARTMENT_OCID（= 承認済み根の直下 dev）
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    # OCI 側も **dev コンパートメント**に閉じる（loop-config の e2e.compartment）。
    # `.env` の COMPARTMENT_OCID は親（jetuse）を指しており、そこには ACTIVE な
    # GenerativeAiProject も loop の資源も無い（Vector Store を親側に作ってしまう）。
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


def ensure_spike_store() -> str:
    """検証用の Vector Store（`jetuse-spike-prep01-<run>`）を用意し、登録簿に載せる。

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


def upload(client, version: str) -> dict:
    """アプリのアップロード経路（`POST /api/rag/files`）で取り込む。"""
    res = client.post(
        "/api/rag/files",
        files={"file": (DOC_NAME, workbook(version),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"attributes": json.dumps({"version": version, "kind": "spec"})},
    )
    if res.status_code != 200:
        sys.exit(f"アップロードが失敗した: {res.status_code} {res.text[:400]}")
    return res.json()


def wait_completed(client, file_id: str, timeout: int = 300) -> dict:
    """マネージド側の処理完了を待つ（一覧取得が processing の行を DP へ問い合わせる）。"""
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        files = client.get("/api/rag/files").json()["files"]
        row = next((f for f in files if f["id"] == file_id), {})
        if row.get("status") in ("completed", "failed"):
            return row
        time.sleep(5)
    return row


# --- シナリオ ------------------------------------------------------------------


def scenario_0(client) -> bool:
    """抽出口: 取り込まずに `{sheet, cells, text}` を返す。"""
    banner("シナリオ0: POST /api/extract（抽出のみ・取り込みなし）")
    before = len(client.get("/api/rag/files").json()["files"])
    res = client.post("/api/extract", files={"file": (DOC_NAME, workbook("2.0"), "x")})
    after = len(client.get("/api/rag/files").json()["files"])
    body = res.json()
    chunks = body.get("chunks", [])
    rows = "\n".join(f"{c['sheet']} | {c['cells']} | {c['text'][:38].splitlines()[0]}..."
                     for c in chunks)
    sheets = [c["sheet"] for c in chunks]
    distinct = len({(c["sheet"], c["cells"]) for c in chunks}) == len(chunks)
    ok = (res.status_code == 200 and len(chunks) >= 3 and distinct
          and "作業用" not in sheets and after == before)

    # 上限超過（切り詰めない）も同じ口で確認する
    from jetuse_core import extract_xlsx

    original = extract_xlsx.MAX_CHUNKS
    extract_xlsx.MAX_CHUNKS = 1
    try:
        over = client.post("/api/extract", files={"file": (DOC_NAME, workbook("2.0"), "x")})
    finally:
        extract_xlsx.MAX_CHUNKS = original
    limit_ok = over.status_code == 422 and "limit=chunks" in over.json().get("detail", "")

    write("scenario-0.md", f"""# シナリオ0 — 抽出口 `POST /api/extract`（取り込まない）

架空の仕様書ブック `{DOC_NAME}`（複数シート・空シート・結合セル・数式セル・空白領域つき）を
実 API に渡した。**保存はしない**ことを、前後のファイル一覧の件数で確認している。

- HTTP ステータス: **{res.status_code}** / チャンク数: **{body.get('chunk_count')}**
- 取り込み前後のファイル数: {before} → {after}（増えていないこと）

{fence(rows)}

- シート名とセル範囲がチャンクごとに異なる: **{distinct}**
- 空シート `作業用` はチャンクを作らない: **{'作業用' not in sheets}**

## 上限超過（切り詰めずに拒否する）

一時的に総チャンク上限を 1 に落として同じファイルを投げた:

- HTTP ステータス: **{over.status_code}**（期待 422）
- detail: `{over.json().get('detail', '')}`（どの上限かが書かれていること）

判定: **{'PASS' if ok and limit_ok else 'FAIL'}**
""")
    return ok and limit_ok


def scenario_1(client, rag_adb) -> bool:
    """adb: チャンクごとに異なるシート / セル範囲が引用に載る。"""
    banner("シナリオ1: adb バックエンド（チャンク単位の出典）")
    # 現行版だけを見る（既定の生成経路と同じ絞り込み）。版フィルタ無しだと同名文書の
    # 旧版チャンクが混ざり、同じセル範囲が版違いで 2 度出てくる（それはシナリオ3 の主題）
    hits = rag_adb.search(OWNER, QUESTION, k=5,
                          filters={"file": DOC_NAME, "current_version": "Y"})
    rows = "\n".join(f"{h['source']['chunk_id']} | sheet={h['source']['sheet']}"
                     f" | cells={h['source']['cells']} | score={h['score']}"
                     f" | {h['text'][:36].splitlines()[0]}..." for h in hits)
    pairs = [(h["source"]["sheet"], h["source"]["cells"]) for h in hits]
    distinct = len(set(pairs)) == len(pairs)
    same_file = len({h["source"]["file"] for h in hits}) == 1
    cell_shaped = all(any(ch.isalpha() for ch in c) and any(ch.isdigit() for ch in c)
                      for _, c in pairs)

    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": QUESTION}],
        "rag": True, "rag_backend": "adb",
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    cites = next((f["citations"] for f in frames if "citations" in f), [])
    cite_pairs = [(c["source"]["sheet"], c["source"]["cells"]) for c in cites]
    cite_distinct = len(set(cite_pairs)) == len(cite_pairs) and len(cites) >= 2
    # 取り込み時に渡した分類（kind）が ADB 側の列にも入っていること。既定値のままだと
    # 「マネージドでは spec、ADB では doc」になり、分類での絞り込みがバックエンドで食い違う
    kinds = {h["source"]["kind"] for h in hits}
    kind_matches = kinds == {"spec"}
    ok = (bool(hits) and distinct and same_file and cell_shaped and cite_distinct
          and kind_matches)

    write("scenario-1.md", f"""# シナリオ1 — `adb`: 出典は**チャンク単位**（シート + セル範囲）

同じ 1 ファイル `{DOC_NAME}` を取り込んだ結果を検索した（アップロードはアプリ経路
`POST /api/rag/files`。取り込みは `rag.add_file` → `rag_adb.ingest`）。

## 検索（`rag_adb.search` / `current_version='Y'`）

{fence(rows)}

- すべて**同一ファイル**由来: **{same_file}**
- (シート, セル範囲) がすべて異なる: **{distinct}** → `{pairs}`
- セル範囲が A1 形式（列 + 行）である: **{cell_shaped}**
- 取り込み時の分類 `kind="spec"` が ADB 側にも入っている: **{kind_matches}**
  （実際: `{sorted(kinds)}`）

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

質問: `{QUESTION}`

{fence(answer)}

引用（`citations[].source`）:

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:1800])}

- 引用の (シート, セル範囲) がチャンクごとに異なる: **{cite_distinct}**

判定: **{'PASS' if ok else 'FAIL'}**

> これは `adb` バックエンドだけの粒度である。マネージド Vector Store は属性が
> **ファイル単位**（SPIKE-M1 ①-a）で、同じファイルの全チャンクが同じ出典しか返せない
> （シナリオ2 で実測）。
""")
    return ok


def scenario_2(client, files: list[dict]) -> bool:
    """vector_store: 同じファイルの属性が**ファイル単位**であることの証跡。"""
    banner("シナリオ2: vector_store（マネージドの属性はファイル単位）")
    from jetuse_core import rag
    from jetuse_core.genai import make_inference_client

    dp = make_inference_client(with_project=True)
    vs_id = rag.get_store_id(OWNER)

    # (a) マネージド側が xlsx をそのまま受け付けるか（前処理が要る理由の実測）
    raw_probe = "(未実施)"
    probe_file_id = None
    try:
        probe = dp.files.create(file=(DOC_NAME, workbook("2.0")), purpose="assistants")
        probe_file_id = probe.id
        dp.vector_stores.files.create(vector_store_id=vs_id, file_id=probe.id,
                                      attributes={"file": DOC_NAME})
        for _ in range(30):
            vf = dp.vector_stores.files.retrieve(vector_store_id=vs_id, file_id=probe.id)
            if vf.status not in ("in_progress", "queued"):
                break
            time.sleep(5)
        raw_probe = f"status={vf.status} last_error={getattr(vf, 'last_error', None)}"
    except Exception as e:  # noqa: BLE001 — 何が返るかの実測そのものが目的
        raw_probe = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        if probe_file_id:
            try:
                dp.vector_stores.files.delete(vector_store_id=vs_id, file_id=probe_file_id)
            except Exception:
                pass
            try:
                dp.files.delete(probe_file_id)
            except Exception:
                pass

    # (b) 実装経路で取り込んだファイルの属性（ファイル単位で 1 種類）
    rows = []
    attrs_by_file = {}
    statuses = []
    for f in files:
        vf = dp.vector_stores.files.retrieve(vector_store_id=vs_id, file_id=f["oci_file_id"])
        attrs_by_file[f["id"]] = dict(vf.attributes or {})
        statuses.append(vf.status)
        rows.append(f"{mask(f['oci_file_id'])} | status={vf.status} | "
                    f"attributes={json.dumps(dict(vf.attributes or {}), ensure_ascii=False)}")

    # (c) 実 API の RAG 応答（file_search）で引用の出典を見る
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": QUESTION}],
        "rag": True,
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    cites = next((f["citations"] for f in frames if "citations" in f), [])
    per_file: dict[str, set] = {}
    for c in cites:
        source = c.get("source") or {}
        per_file.setdefault(c.get("file_id", "?"), set()).add(
            (source.get("sheet"), source.get("cells"))
        )
    file_level_citations = all(len(v) == 1 for v in per_file.values()) if per_file else None

    variants = {mask(k): len(v) for k, v in per_file.items()}
    workbook_marker = all(
        a.get("cells") == "(ブック全体)" and a.get("sheet", "").startswith("(ブック全体")
        for a in attrs_by_file.values()
    )
    # **主要条件はすべて必須にする**（引用が 0 件でも PASS になる緩い判定にしない）
    probe_rejected = "unsupported_file" in raw_probe
    all_completed = bool(statuses) and set(statuses) == {"completed"}
    ok = (bool(attrs_by_file) and workbook_marker and probe_rejected and all_completed
          and bool(cites) and file_level_citations is True)

    write("scenario-2.md", f"""# シナリオ2 — `vector_store`: 属性は**ファイル単位**（能力差の証跡）

シナリオ1 と**同じファイル**をマネージド Vector Store 側で見た。ADR-0020 の決定
（2 バックエンドの能力差）の裏付けになる部分なので、3 つの角度から記録する。

## (a) マネージド側は xlsx をそのまま受け付けるか

`files.create(...xlsx...)` → `vector_stores.files.create` を素の xlsx で試した結果:

{fence(raw_probe)}

→ だから取り込み経路では**抽出したテキストを `<原名>.xlsx.txt` として渡す**
（SPIKE-03 で docx が `Unsupported file type` だったのと同じ扱い）。
素の xlsx が `unsupported_file` で断られた: **{probe_rejected}**（この行が判定条件）

## (b) 取り込んだファイルの属性（`vector_stores.files.retrieve`）

{fence(chr(10).join(rows))}

- `sheet` / `cells` が**ブック全体**を表す値になっている: **{workbook_marker}**
- 取り込み状態がすべて `completed`: **{all_completed}**（実際: `{statuses}`）
- 1 ファイルにつき属性は **1 種類**。チャンクが何個できても増えない（SPIKE-M1 ①-a）

## (c) 実 API の RAG 応答（`POST /api/chat/stream` / 既定の `vector_store`）

質問: `{QUESTION}`

{fence(answer)}

引用:

{fence(json.dumps(cites, ensure_ascii=False, indent=2)[:1500])}

- 引用の件数: **{len(cites)}**（0 件なら判定は FAIL）
- 同一ファイル由来の引用が持つ (シート, セル範囲) の種類数: `{variants}`
  → **1 種類だけ**であること（= 属性はファイル単位）: **{file_level_citations}**

判定: **{'PASS' if ok else 'FAIL'}**

> **この差は隠さない。** 「マネージドでもセル単位で返る」ように見せる実装
> （1 チャンク = 1 ファイルへ無理に分割する等）はしていない。セル単位の出典が要るなら
> `adb` バックエンドを選ぶ、というのが ADR-0020 の決定内容そのものである（可視化は RAGM-03）。
""")
    return ok


def scenario_3(rag_adb) -> bool:
    """版フィルタが xlsx 由来のチャンクにも効く。"""
    banner("シナリオ3: 版フィルタ（current_version='Y'）")
    no_filter = rag_adb.search(OWNER, QUESTION, k=10, filters={"file": DOC_NAME})
    filtered = rag_adb.search(OWNER, QUESTION, k=10,
                              filters={"file": DOC_NAME, "current_version": "Y"})

    def fmt(hits):
        return "\n".join(
            f"{h['source']['chunk_id']} | version={h['source']['version']}"
            f" | current={h['source']['current_version']}"
            f" | {h['source']['sheet']} {h['source']['cells']}"
            f" | {h['text'][:30].splitlines()[0]}..." for h in hits)

    stale_all = [h["source"]["chunk_id"] for h in no_filter
                 if h["source"]["current_version"] == "N"]
    stale_filtered = [h["source"]["chunk_id"] for h in filtered
                      if h["source"]["current_version"] == "N"]
    old_value = RATE_LIMIT["1.0"].split()[0]
    new_value = RATE_LIMIT["2.0"].split()[0]
    old_in_all = any(old_value in h["text"] for h in no_filter)
    old_in_filtered = any(old_value in h["text"] for h in filtered)
    new_in_filtered = any(new_value in h["text"] for h in filtered)
    ok = (bool(stale_all) and not stale_filtered and bool(filtered)
          and old_in_all and not old_in_filtered and new_in_filtered)

    write("scenario-3.md", f"""# シナリオ3 — 版フィルタが xlsx 由来のチャンクにも効く

同名の xlsx を v1.0 → v2.0 の順にアップロードした（再取り込みで旧チャンクは
`current_version='N'` に落ちる）。制約シートの「レート制限」だけが
{RATE_LIMIT['1.0']} → {RATE_LIMIT['2.0']} に変わっている。

## A: フィルタ無し（対照）

{fence(fmt(no_filter))}

旧版のヒット: **{len(stale_all)} 件** `{stale_all}`

本文に旧値 {old_value} を含む: **{old_in_all}**

## B: `current_version='Y'`

{fence(fmt(filtered))}

旧版のヒット: **{len(stale_filtered)} 件** / 旧値 {old_value} を含む: **{old_in_filtered}** /
現行値 {new_value} を含む: **{new_in_filtered}**

判定: **{'PASS' if ok else 'FAIL'}**（A で旧版が返り、B では 0 件になること）
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

    # 前回の残りを消してから始める（対照の件数が積み上がらないように）
    cur = conn.cursor()
    for table in (rag_adb.TABLE, rag_adb.DOC_TABLE, rag_adb.INGEST_TABLE):
        cur.execute(f"DELETE FROM {table} WHERE owner_sub = :o", o=OWNER)
    conn.commit()
    for row in rag.list_files(OWNER):
        rag.delete_file(OWNER, row["id"])

    ensure_spike_store()

    banner("アップロード（v1.0 → v2.0・アプリ経路）")
    uploaded = [upload(client, "1.0"), upload(client, "2.0")]
    files = []
    for u in uploaded:
        row = wait_completed(client, u["id"])
        print(f"  {u['id']}: status={row.get('status')} backends={row.get('backends')}")
        files.append({**u, "oci_file_id": row.get("oci_file_id"),
                      "status": row.get("status"), "backends": row.get("backends")})
    write("upload.md", f"""# アップロード（実 API・アプリ経路）

`POST /api/rag/files`（multipart + `attributes`）に架空の xlsx を v1.0 → v2.0 の順で投げた。

{fence(chr(10).join(f"{f['filename']} v{v} | file_id={f['id']} | "
                    f"oci_file={mask(f['oci_file_id'])} | status={f['status']} | "
                    f"backends={f['backends']}"
                    for f, v in zip(files, ('1.0', '2.0'), strict=True)))}

- `backends.vector_store` = マネージド側の処理状態 / `backends.adb` = 自前索引の取り込み状態
- スキーマ: `{SCHEMA}`（共有 loop ADB 内の run 固有スキーマ。ADB は増やしていない）
""")

    results = {
        "0": scenario_0(client),
        "1": scenario_1(client, rag_adb),
        "2": scenario_2(client, files),
        "3": scenario_3(rag_adb),
    }
    banner("結果")
    for k, v in results.items():
        print(f"  シナリオ{k}: {'PASS' if v else 'FAIL'}")
    write("summary.md", "# E2E 結果一覧\n\n" + "\n".join(
        f"- シナリオ{k}: **{'PASS' if v else 'FAIL'}** → `scenario-{k}.md`"
        for k, v in results.items()) + "\n")
    conn.close()
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
