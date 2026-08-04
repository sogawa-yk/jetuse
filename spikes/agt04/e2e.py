"""AGT-04 の実環境 E2E（tasks/AGT-04.md の「E2E シナリオ」）。

**実装（`jetuse_core.chat` / `tools` / `rag_adb` と FastAPI ルート）をそのまま呼ぶ**。
検証用の別実装は書かない。相手（公開 https エコー）・ADB・OCI Generative AI
（Files / Vector Stores / 埋め込み / 生成）はすべて実物。

  1. 入れ子の引数を持つ HTTP ツール 6 本を順に呼ぶ手続きが**最後まで到達する**。
     従来の固定上限（5 ホップ）では届かないことと**同じ要求で**対比する
  2. エージェントの文書検索で**シート名・セル範囲つきの出典**が返る
  3. 同じ問いの検索回数を、ファイル単位（vector_store）とチャンク単位（adb）で比べる。
     **減らなければ「見立てが誤り」とそのまま書く**（数字を作らない）
  4. 回帰: 従来経路（vector_store の file_search built-in）が変わらない

隔離: 共有 loop ADB の **run 固有スキーマ**（`JETUSE_AGT04_<乱数>`。ADB は増やさない）。
OCI 側の検証用資源は `jetuse-spike-agt04-` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部（`spikes/ragm02/common.py`）を env で接頭辞だけ差し替えて再利用する。

実行（`E=SPIKE_SCHEMA_PREFIX=JETUSE_AGT04 SPIKE_HOME=<秘密の置き場>`,
      `P=PYTHONPATH=spikes/ragm02:spikes/agt04:packages/api`）:
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成（台帳つき）
  env $E $P .venv/bin/python spikes/agt04/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/agt04/teardown.py --yes  # OCI 側（ファイル・箱）
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
"""

import json
import os
import re
import sys
import time

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret
from fixtures import (
    CITATION_QUESTION,
    ECHO_URL,
    LOOKUP_QUESTION,
    ORDER_ID,
    PLAN_CODE,
    PREFIX,
    PROCEDURE_REQUEST,
    SPEC_NAME,
    TOOL_NAMES,
    http_tools,
    spec_workbook,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject（HTTP 経路の名前空間）

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

_IDS = re.compile(
    r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.|file-kix-|vs_kix_)[a-zA-Z0-9_-]{8,}"
)


ECHO_MASK = "[AGT04_ECHO_URL]"

# SSRF ガードは解決済み IP へ固定して送るので、HTTP クライアントのログには
# **ホスト名ではなく IP の URL** が出る（`https://203.0.113.10/post`）。
# ホスト名だけ伏せても宛先が残るため、IP 形式の URL も伏せる。
_IP_URL = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")

# 接続先 ADB の識別値（`DB_NAME=...` / `DSN=...`）も環境依存値なので証跡に残さない。
# 秘密ではないが、`.env` で管理する接続先をリポジトリへ書かないという方針に合わせる。
_CONN_IDS = re.compile(r"\b(DB_NAME|DSN)=([A-Za-z0-9_.-]+)")


def scrub(text: str) -> str:
    """証跡に残してよい形へ。

    - OCI 側の識別子は**先頭だけ残して伏せる**
    - **相手のエンドポイント実値は伏せる**（`.env` で管理する値であり、リポジトリには
      置かない。ホスト名・解決後の IP のどちらでも宛先が特定できるので両方伏せる）
    """
    out = _IDS.sub(lambda m: m.group(1) + "…", text)
    if ECHO_URL:
        host = ECHO_URL.split("//")[-1].split("/")[0]
        out = out.replace(ECHO_URL, ECHO_MASK).replace(host, ECHO_MASK)
    out = _IP_URL.sub(ECHO_MASK, out)
    return _CONN_IDS.sub(lambda m: f"{m.group(1)}=[{m.group(1)}]", out)


def write(name: str, text: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(scrub(text))
    print(f"  wrote {EVIDENCE / name}")


def fence(text: str) -> str:
    return "```\n" + (text.rstrip() or "(なし)") + "\n```"


def js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def mask(value: str | None) -> str:
    if not value:
        return "(なし)"
    return value[:12] + "…" if len(value) > 12 else value


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をこの run のスキーマへ向ける（他タスクの資源に触れない）。"""
    prepare_env()  # ADB_WALLET_* / ADB_DSN / ADB_COMPARTMENT_OCID（= 承認済み根の直下 dev）
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    # OCI 側も **dev コンパートメント**に閉じる（loop-config の e2e.compartment）
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


def ensure_spike_store() -> str:
    """検証用の Vector Store（`jetuse-spike-agt04-<run>`）を用意し、登録簿に載せる。

    `rag.ensure_store()` が作る名前（`jetuse-rag-<owner>`）では検証用の接頭辞規約を
    満たせないので、**先に接頭辞つきで作って登録簿へ入れる**（アプリ経路は変えていない）。
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


def upload_spec(client) -> dict:
    """アプリのアップロード経路（`POST /api/rag/files`）で仕様書を取り込む。

    1 回のアップロードで Vector Store と ADB の**両方**に入る（`rag.add_file`）ので、
    シナリオ3 の比較は「同じ文書・同じ問い」で成立する。
    """
    res = client.post(
        "/api/rag/files",
        files={"file": (SPEC_NAME, spec_workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"attributes": json.dumps({"version": "1.0", "kind": "spec"})},
    )
    if res.status_code != 200:
        sys.exit(f"アップロードが失敗した: {res.status_code} {res.text[:400]}")
    uploaded = res.json()
    deadline = time.time() + 300
    while time.time() < deadline:
        row = next((f for f in client.get("/api/rag/files").json()["files"]
                    if f["id"] == uploaded["id"]), {})
        if row.get("status") in ("completed", "failed"):
            return {**uploaded, **row}
        time.sleep(5)
    return uploaded


def register_tools(client) -> list[dict]:
    """6 本の業務 API ツールを登録する（`POST /api/agent/http-tools`）。"""
    out = []
    for body in http_tools():
        res = client.post("/api/agent/http-tools", json=body)
        if res.status_code != 200:
            sys.exit(f"ツール登録が失敗した: {body['name']} {res.status_code} {res.text[:300]}")
        out.append(res.json())
    return out


# --- エージェント実行（実 API 経路） -----------------------------------------


def _sum_usage(frames: list[dict]) -> dict:
    """ターン全体の usage。`usage` フレームの数がモデル往復（= ホップ）の回数になる。"""
    items = [f["usage"] for f in frames if "usage" in f]
    return {
        "model_roundtrips": len(items),
        "input_tokens": sum(u.get("input_tokens", 0) for u in items),
        "output_tokens": sum(u.get("output_tokens", 0) for u in items),
    }


def run_agent(client, question: str, *, tool_ids: list[str] | None = None,
              backend: str = "vector_store", max_hops: int | None = None,
              enabled_tools: list[str] | None = None) -> dict:
    """`POST /api/chat/stream`（エージェント・自動実行）を 1 回走らせて事実を集める。"""
    body = {
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": question}],
        "agent": True,
        "auto_tools": True,
        "enabled_tools": enabled_tools if enabled_tools is not None else ["rag_search"],
        "agent_rag_backend": backend,
    }
    if tool_ids:
        body["http_tool_ids"] = tool_ids
    if max_hops is not None:
        body["max_tool_hops"] = max_hops
    started = time.time()
    res = client.post("/api/chat/stream", json=body)
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    calls = [f["tool_call"] for f in frames if "tool_call" in f]
    return {
        "status": res.status_code,
        "frames": frames,
        "answer": "".join(f.get("delta", "") for f in frames),
        "tool_calls": [c["name"] for c in calls if c.get("status") == "running"],
        "tool_results": [f["tool_result"] for f in frames if "tool_result" in f],
        "citations": [c for f in frames if "citations" in f for c in f["citations"]],
        "limit_reached": [f["limit_reached"] for f in frames if "limit_reached" in f],
        # usage は**ホップごとに 1 フレーム**出る（`response.completed` ごと）。
        # 先頭だけを見るとターン全体のコストを過少に見積もる（= 上限を上げるコストの
        # 評価が裏づけを失う）ので、全フレームを足す。フレーム数 = モデル往復の回数。
        "usage": _sum_usage(frames),
        "errors": [f["error"] for f in frames if "error" in f],
        "seconds": round(time.time() - started, 1),
    }


def searches(run: dict) -> int:
    return sum(1 for n in run["tool_calls"] if n == "rag_search")


# モデルは Markdown を整えるときにハイフンを U+2010〜2015 / U+2212 / 全角へ置き換える
# （実測: `PL-GOLD-24` が `PL‑GOLD‑24` になった）。値が合っているかを見たいので、
# 突き合わせの前に正規化する。**検証側の都合であって、実装は何も変えていない。**
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−－"), "-")


def contains_code(text: str, code: str) -> bool:
    return code in text.translate(_DASHES)


def business_calls(run: dict) -> list[str]:
    return [n for n in run["tool_calls"] if n in TOOL_NAMES]


def call_table(run: dict) -> str:
    return "\n".join(f"{i + 1:2d}. {n}" for i, n in enumerate(run["tool_calls"])) or "(なし)"


# --- シナリオ ------------------------------------------------------------------


def _business_results_ok(run: dict) -> tuple[bool, str]:
    """業務 API の各呼び出しが 2xx で、ツールエラーを返していないことを見る。

    「6 本呼んだ」だけでは足りない —— 途中が 500 でもモデルは先へ進んでしまうので、
    **1 本ずつ相手の応答を確かめる**。`preview` は先頭 500 字だが、`status` は先頭に出る。
    """
    rows, ok = [], True
    for r in run["tool_results"]:
        if r.get("name") not in TOOL_NAMES:
            continue
        preview = r.get("preview", "")
        status = re.search(r'"status":\s*(\d{3})', preview)
        code = int(status.group(1)) if status else 0
        failed = not (200 <= code < 300) or '"error"' in preview
        ok = ok and not failed
        rows.append(f"{r['name']:20s} HTTP {code or '?'}"
                    f"{'  ← 失敗' if failed else ''}")
    return (ok and bool(rows)), "\n".join(rows) or "(なし)"


def _nested_echoed(run: dict) -> bool:
    """入れ子オブジェクトが**形のまま**相手に届いたか（エコーの本文で見る）。

    `set_contractor` は住所を 2 段の入れ子で渡す。相手が返した本文にその入れ子が
    見えれば、平坦化も文字列化もされずに届いている（ADR-0024 の要求）。
    """
    got = next((r.get("preview", "") for r in run["tool_results"]
                if r.get("name") == "set_contractor"), "")
    # ツール出力は `{"status":200,"body":"<エスケープされた JSON 文字列>"}` なので、
    # 突き合わせの前にエスケープを戻す（`\"address\"` のままだと素の `"address"` に当たらない）
    body = got.replace('\\"', '"')
    # **入れ子のまま**であることを構造で見る（キー名の出現だけだと平坦化を見逃す）
    return '"contractor":{' in body and '"address":{' in body and '"prefecture":' in body


def scenario_1(client, tool_ids: list[str]) -> bool:
    """入れ子引数の HTTP ツール 6 本を順に呼ぶ手続きが最後まで到達する。"""
    banner("シナリオ1: 多段の業務フロー（6 本）が最後まで到達する")
    old = run_agent(client, PROCEDURE_REQUEST, tool_ids=tool_ids,
                    backend="adb", max_hops=5)
    new = run_agent(client, PROCEDURE_REQUEST, tool_ids=tool_ids, backend="adb")

    old_reached = sorted(set(business_calls(old)))
    new_seq = business_calls(new)
    # **順序どおり・各 1 回**を要求する（集合で見ると順序違反や重複を見逃す）
    order_ok = new_seq == TOOL_NAMES
    http_ok, http_rows = _business_results_ok(new)
    # 入れ子がそのまま相手へ届いたか（エコーが返した本文で見る）
    nested_ok = _nested_echoed(new)
    ok = (
        order_ok
        and http_ok
        and nested_ok
        and not new["limit_reached"]
        and len(old_reached) < len(TOOL_NAMES)      # 旧上限では届かない
        and bool(old["limit_reached"])              # かつ打ち切りだと分かる
    )
    write("scenario-1.md", f"""# シナリオ1 — 多段の業務フローが最後まで到達する

**同じ要求**を、旧固定上限（5 ホップ）と AGT-04 の既定（設定値）で 2 回流した。
相手は公開 https エコー（宛先は `.env` の `AGT04_ECHO_URL`。証跡では {ECHO_MASK} と伏せる）で、
JetUse が組み立てた入れ子ボディをそのまま返す。文書検索は adb バックエンド。

登録した業務 API（すべて入れ子オブジェクト / 配列を持つ・ADR-0024）:
`{', '.join(TOOL_NAMES)}`

要求（**手順は書いていない**。順序とコード値は仕様書から引かせる）:

{fence(PROCEDURE_REQUEST)}

## (a) 旧固定上限と同じ 5 ホップ（`max_tool_hops=5`）

- 到達した業務 API: **{len(old_reached)} / {len(TOOL_NAMES)}** — `{old_reached}`
- ツール呼び出しの順序:

{fence(call_table(old))}

- 打ち切り通知（`limit_reached`）: {js(old["limit_reached"])}
- 所要 {old["seconds"]} 秒 / usage {js(old["usage"])}

## (b) AGT-04 の既定（設定値のまま）

- 到達した業務 API: **{len(set(new_seq))} / {len(TOOL_NAMES)}** — `{sorted(set(new_seq))}`
- ツール呼び出しの順序（**このホップ数が既定値の根拠になる実測**）:

{fence(call_table(new))}

- 打ち切り通知: {js(new["limit_reached"]) if new["limit_reached"] else "**なし**（自力で完了）"}
- 所要 {new["seconds"]} 秒 / usage {js(new["usage"])}
- 最終回答:

{fence(new["answer"][:1500])}

## 業務 API 1 本ごとの相手の応答（「呼んだ」だけでなく「通った」か）

{fence(http_rows)}

- 呼び出し順序が仕様書どおり・各 1 回: **{order_ok}**
  （実際: `{new_seq}` / 期待: `{TOOL_NAMES}`）
- すべて 2xx でツールエラーなし: **{http_ok}**
- 入れ子（`contractor.address.prefecture`）が形のまま相手へ届いた: **{nested_ok}**

## 相手が受け取ったボディ（`set_contractor`・エコーの応答から）

{fence(next((r.get("preview", "") for r in new["tool_results"]
             if r.get("name") == "set_contractor"), "")[:800])}

判定: **{'PASS' if ok else 'FAIL'}**
（条件: 既定で 6 本を**順序どおり各 1 回**・全件 2xx・入れ子が形のまま到達・打ち切りなし /
 旧上限では未到達で、かつ打ち切りが応答から分かる）
""")
    return ok


def scenario_2(client) -> bool:
    """エージェントの文書検索で、シート名・セル範囲つきの出典が返る。"""
    banner("シナリオ2: チャンク単位の出典（シート名・セル範囲）")
    run = run_agent(client, CITATION_QUESTION, backend="adb")
    sources = [c.get("source") or {} for c in run["citations"]]
    with_cells = [s for s in sources if s.get("sheet") and s.get("cells")]
    preview = (run["tool_results"] or [{}])[0].get("preview", "")
    answered = contains_code(run["answer"], PLAN_CODE)
    cited_in_answer = "コード" in run["answer"] and any(
        contains_code(run["answer"], s.get("cells", "")) for s in with_cells
    )
    ok = bool(with_cells) and any(s.get("sheet") == "コード" for s in sources) and answered
    write("scenario-2.md", f"""# シナリオ2 — エージェントの文書検索がチャンク単位の出典を返す

`agent_rag_backend="adb"` で `rag_search` を function tool として実行した
（実体は `rag_adb.search` / 現行版のみ）。

質問: `{CITATION_QUESTION}`

## ツール結果（モデルが受け取ったもの・先頭 800 字）

{fence(preview[:800])}

## 引用イベント（`citations[].source`）

{fence(js(sources)[:1800])}

- シート名とセル範囲を持つ出典: **{len(with_cells)} / {len(sources)}**
- 出典に「コード」シートが含まれる: **{any(s.get('sheet') == 'コード' for s in sources)}**
- 回答に仕様書の値 `{PLAN_CODE}` が出る: **{answered}**
- 回答本文がシート名とセル範囲を引用している: **{cited_in_answer}**
  （モデルへ渡した出典がそのまま利用者に届いているか。判定条件には入れていない
  — 書式はモデル任せなので、載っていることの証拠は上の引用イベント）

## 最終回答

{fence(run["answer"][:1200])}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


NOT_DECREASED_NOTE = (
    "チャンク単位の出典で引き直しが減るという見立ては、**この実測では支持されない**。"
    "ホップ上限の既定値は「検索も 1 ホップを食う」前提で置いてあるので設計自体は成立するが、"
    "ADR-0025 の根拠から「検索回数が減る」は落とすべきである。"
)
DECREASED_NOTE = (
    "チャンク単位の出典で「どこを見たか」が確定するため、"
    "同じ文書を引き直す回数が減った。"
)


def _compare_one(client, label: str, question: str) -> dict:
    """同じ問いを両バックエンドで 1 回ずつ流し、検索回数と本文の有無を記録する。"""
    vs = run_agent(client, question, backend="vector_store")
    ad = run_agent(client, question, backend="adb")
    return {
        "label": label, "question": question, "vs": vs, "adb": ad,
        "vs_n": searches(vs), "adb_n": searches(ad),
    }


def _compare_rows(cases: list[dict]) -> str:
    rows = []
    for c in cases:
        vs, ad = c["vs"], c["adb"]
        rows.append(
            f"| {c['label']} | {c['vs_n']} | {c['adb_n']} | "
            f"{'減った' if c['adb_n'] < c['vs_n'] else '減らなかった'} | "
            f"{len(vs['answer'])} / {len(ad['answer'])} | "
            f"{vs['seconds']}s / {ad['seconds']}s |"
        )
    return "\n".join(rows)


def scenario_3(client) -> tuple[bool, str]:
    """同じ問いの検索回数を、ファイル単位（vector_store）とチャンク単位（adb）で比べる。

    **減らなかったら、その事実をそのまま書く**（tasks/AGT-04.md の指示。数字を作らない）。
    問いを 2 種類にするのは、1 問だけだと「たまたま 1 回で足りた問い」でも
    「減らなかった」と読めてしまうため（回数が動く余地のある問いを含める）。
    """
    banner("シナリオ3: 検索回数の比較（vector_store vs adb）")
    cases = [
        _compare_one(client, "複数の事実をまとめて聞く", LOOKUP_QUESTION),
        _compare_one(client, "1 つの値と出典を聞く", CITATION_QUESTION),
    ]
    decreased = [c for c in cases if c["adb_n"] < c["vs_n"]]
    same_or_more = [c for c in cases if c["adb_n"] >= c["vs_n"]]
    if not decreased:
        verdict = "減らなかった（原因 2 の見立てはこの実測では支持されない）"
    elif not same_or_more:
        verdict = "減った（両方の問いで）"
    else:
        verdict = f"問いによる（{len(decreased)}/{len(cases)} の問いでのみ減った）"
    # この比較は**測れたこと自体**が成果。減らなくても FAIL にはしない（数字を作らないため）
    ok = all(c[k]["status"] == 200 and not c[k]["errors"]
             for c in cases for k in ("vs", "adb"))
    details = "\n\n".join(f"""### {c['label']}

質問: `{c['question']}`

- vector_store の呼び出し列（built-in はホップを消費しない）:

{fence(call_table(c['vs']))}

- adb の呼び出し列（検索 1 回 = 1 ホップ）:

{fence(call_table(c['adb']))}

- vector_store の引用（ファイル単位）:

{fence(js([{k: v for k, v in x.items() if k != 'text'} for x in c['vs']['citations']])[:700])}

- adb の引用（チャンク単位・先頭 2 件）:

{fence(js([x.get('source') for x in c['adb']['citations']][:2])[:700])}
""" for c in cases)

    write("scenario-3.md", f"""# シナリオ3 — 検索回数の比較（ファイル単位 vs チャンク単位）

同じ文書・同じモデル（gpt-oss-120b）で、`agent_rag_backend` **だけ**を変えて
2 種類の問いを 1 回ずつ流した。**各 1 回の実測**であってサンプル統計ではない
（モデルの出方には揺れがある）。数字はそのまま載せる。

| 問い | vector_store の検索回数 | adb の検索回数 | 判定 | 回答の文字数 (vs/adb) | 所要 (vs/adb) |
|---|---|---|---|---|---|
{_compare_rows(cases)}

**結果: {verdict}**

{details}

## 読み方

- **adb では検索 1 回が 1 ホップを消費する**（function tool）。built-in の file_search は
  1 応答の中で完結するのでホップを消費しない。回数の比較はこの差を含んだ値である。
- {NOT_DECREASED_NOTE if not decreased else DECREASED_NOTE}

判定: **{'PASS（比較を実測できた）' if ok else 'FAIL（比較そのものが失敗した）'}**
""")
    return ok, verdict


def scenario_4(client) -> bool:
    """回帰: 従来経路（vector_store の file_search built-in）が変わらない。"""
    banner("シナリオ4: 回帰（従来の rag_search 経路）")
    run = run_agent(client, LOOKUP_QUESTION)  # backend 未指定 = vector_store
    builtin = [f["tool_call"] for f in run["frames"]
               if "tool_call" in f and f["tool_call"].get("name") == "rag_search"]
    all_builtin = bool(builtin) and all(c.get("builtin") for c in builtin)
    file_level = all("source" not in c or not (c.get("source") or {}).get("cells")
                     for c in run["citations"])
    ok = run["status"] == 200 and not run["errors"] and all_builtin and file_level \
        and bool(run["answer"])
    write("scenario-4.md", f"""# シナリオ4 — 回帰: 従来の `rag_search`（file_search built-in）

要求で `agent_rag_backend` を指定しない = 既定。**挙動は AGT-04 前と同じ**であることを見る。

質問: `{LOOKUP_QUESTION}`

- HTTP: {run["status"]} / エラーフレーム: {run["errors"] or "なし"}
- `rag_search` の通知がすべて built-in（OCI 側実行）: **{all_builtin}**
  — 通知 **{len(builtin)} 件**（built-in は 1 応答の中で何度も走る。ホップは消費しない）

{fence(js(builtin[:3])[:700])}

- 引用は**ファイル単位**のまま（`source.cells` を持たない）: **{file_level}**

{fence(js(run["citations"])[:900])}

- 最終回答:

{fence(run["answer"][:900])}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def main() -> None:
    if not ECHO_URL:
        sys.exit("AGT04_ECHO_URL が未設定。承認していない宛先へは送らないため中止"
                 "（.env に相手の https エコー先を設定する。雛形は .env.example）。")
    use_task_schema()
    conn = connect_schema()  # 台帳ゲート（自分が作ったスキーマか）を通す

    banner("マイグレーション適用（deploy 相当）")
    from jetuse_core.migrate import migrate

    applied = migrate()
    print(f"  applied: {applied or '(up to date)'}")

    from fastapi.testclient import TestClient
    from jetuse_core import rag
    from service.main import app

    client = TestClient(app)

    # 前回の**この検証の**残りだけを消してから始める（同じ名前で二重登録しないため）。
    # 消すのは接頭辞 `jetuse-spike-agt04-` のファイルと、この検証が登録した 6 本の
    # ツール行だけ。所有者ごと消す（`WHERE owner_sub = :o`）と、同じスキーマに他の
    # 検証データがあった場合まで巻き添えにする＝確認なしの破壊操作になる。
    # スキーマ自体は run 固有＋所有台帳ゲート（`connect_schema()`）を通っている。
    removed = [row for row in rag.list_files(OWNER)
               if row["filename"].startswith(PREFIX)]
    for row in removed:
        # チャンク・取り込み状態・Vector Store 内のファイル・原本まで同じ経路で消える
        rag.delete_file(OWNER, row["id"])
    cur = conn.cursor()
    cur.executemany(
        "DELETE FROM http_tools WHERE owner_sub = :o AND name = :n",
        [{"o": OWNER, "n": name} for name in TOOL_NAMES],
    )
    conn.commit()
    print(f"  前回の残り: ファイル {len(removed)} 件 / ツール行 {cur.rowcount} 件を削除")

    ensure_spike_store()
    banner("アップロード（アプリ経路 `POST /api/rag/files`）")
    spec = upload_spec(client)
    print(f"  {spec.get('filename')}: status={spec.get('status')} "
          f"backends={spec.get('backends')}")
    if (spec.get("backends") or {}).get("adb") != "indexed":
        sys.exit(f"仕様書が ADB へ取り込まれていない: {spec.get('backends')}。中止。")

    banner("業務 API ツールの登録（`POST /api/agent/http-tools`）")
    tools = register_tools(client)
    tool_ids = [t["id"] for t in tools]
    print(f"  登録: {', '.join(t['name'] for t in tools)}")

    results = {"1": scenario_1(client, tool_ids), "2": scenario_2(client)}
    ok3, verdict3 = scenario_3(client)
    results["3"] = ok3
    results["4"] = scenario_4(client)

    banner("結果")
    for k, v in results.items():
        print(f"  シナリオ{k}: {'PASS' if v else 'FAIL'}")
    write("summary.md", f"""# AGT-04 実環境 E2E 結果一覧

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント /
OCI Generative AI（gpt-oss-120b・埋め込み）と公開 https エコーは実物。
仕様書は**架空**のブック（顧客データ・案件名は持ち込んでいない）。

""" + "\n".join(f"- シナリオ{k}: **{'PASS' if v else 'FAIL'}** → `scenario-{k}.md`"
                for k, v in results.items()) + f"""

シナリオ3（検索回数）の結果: **{verdict3}**。

## 検証で作った資源

- ADB スキーマ `{SCHEMA}`（共有 loop ADB の中。ADB は増やしていない）
- Vector Store `{PREFIX}-{SCHEMA.rsplit('_', 1)[-1].lower()}` と、その中の仕様書 1 件
- 受付番号 `{ORDER_ID}` のリクエストは公開エコーへ送っただけ（相手側に状態は残らない）

片付けは `spikes/agt04/teardown.py --yes`（OCI 側）と
`spikes/ragm02/teardown.py --yes`（ADB スキーマ）。
""")
    conn.close()
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
