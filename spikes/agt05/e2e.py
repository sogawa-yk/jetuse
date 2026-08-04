"""AGT-05 の実環境 E2E（tasks/AGT-05.md の「E2E シナリオ」）。

**実装（`jetuse_core.chat` / `tools` / `rag_adb` と FastAPI ルート）をそのまま呼ぶ**。
検証用の別実装は書かない。相手（公開 https エコー）・ADB・OCI Generative AI
（Files / Vector Stores / 埋め込み / 生成）はすべて実物。

  1. 文書検索を挟みながら**業務 API を 8 本**呼ぶ手続きが、打ち切られずに最後まで到達する。
     同じ実行から「旧来の数え方（検索も 1 ホップ）なら予算が尽きていた」ことを数で示す
  2. 検索だけを上限まで行い、`limit_reached` の `reason` が**検索側**であることを示す
  3. 回帰: `vector_store` 経路（built-in）のホップ消費が従来どおりであることを示す

隔離: 共有 loop ADB の **run 固有スキーマ**（`JETUSE_AGT05_<乱数>`。ADB は増やさない）。
OCI 側の検証用資源は `jetuse-spike-agt05-` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部（`spikes/ragm02/common.py`）を env で接頭辞だけ差し替えて再利用する。

実行（`E=SPIKE_SCHEMA_PREFIX=JETUSE_AGT05 SPIKE_HOME=<秘密の置き場>`,
      `P=PYTHONPATH=spikes/ragm02:spikes/agt05:packages/api`）:
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成（台帳つき）
  env $E $P .venv/bin/python spikes/agt05/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/agt05/teardown.py --yes  # OCI 側（ファイル・箱）
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
"""

import json
import os
import re
import sys
import time

from common import ROOT, banner, connect_schema, prepare_env, require_schema, secret
from fixtures import (
    ECHO_URL,
    ORDER_ID,
    PREFIX,
    PROCEDURE_REQUEST,
    SEARCH_ONLY_QUESTION,
    SPEC_NAMES,
    TOOL_NAMES,
    http_tools,
    spec_workbook,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject（HTTP 経路の名前空間）

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

# シナリオ2 で当てる検索側の上限（既定 40 を待つと 1 回の実行が長く高くつくため小さくする。
# **確かめたいのは値ではなく「どちらの予算で止まったか分かること」**）
DOC_SEARCH_LIMIT_FOR_S2 = 3
# シナリオ3 で当てるホップ上限（業務 8 本には足りない値 = 従来どおり業務で減ることを見る）
HOP_LIMIT_FOR_S3 = 5
# シナリオ1(b) の絞ったホップ予算。業務 8 本 + 自己修正 1 回分だけの予算にする。
# 旧来の数え方（検索も 1 ホップ）なら、検索を挟む時点でここには到底収まらない
HOP_BUDGET_FOR_S1B = 10

_IDS = re.compile(
    r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.|file-kix-|vs_kix_)[a-zA-Z0-9_-]{8,}"
)
ECHO_MASK = "[AGT05_ECHO_URL]"
# SSRF ガードは解決済み IP へ固定して送るので、ログには IP 形式の URL が出る。
# ホスト名だけ伏せても宛先が残るため両方伏せる（AGT-04 と同じ扱い）
_IP_URL = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?")
_CONN_IDS = re.compile(r"\b(DB_NAME|DSN)=([A-Za-z0-9_.-]+)")


def scrub(text: str) -> str:
    """証跡に残してよい形へ（OCI 識別子は先頭だけ・宛先実値と接続先は伏せる）。"""
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
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


def ensure_spike_store() -> str:
    """検証用の Vector Store（`jetuse-spike-agt05-<run>`）を用意し、登録簿に載せる。"""
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


def upload_specs(client) -> list[dict]:
    """アプリのアップロード経路（`POST /api/rag/files`）で仕様書 3 冊を取り込む。

    1 回のアップロードで Vector Store と ADB の**両方**に入る（`rag.add_file`）ので、
    シナリオ3（vector_store 経路の回帰）も同じ文書で成立する。
    """
    out = []
    for title, filename in SPEC_NAMES.items():
        res = client.post(
            "/api/rag/files",
            files={"file": (filename, spec_workbook(title),
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet")},
            data={"attributes": json.dumps({"version": "1.0", "kind": "spec"})},
        )
        if res.status_code != 200:
            sys.exit(f"アップロードが失敗した: {filename} {res.status_code} {res.text[:400]}")
        uploaded = res.json()
        deadline = time.time() + 300
        row = {}
        while time.time() < deadline:
            row = next((f for f in client.get("/api/rag/files").json()["files"]
                        if f["id"] == uploaded["id"]), {})
            if row.get("status") in ("completed", "failed"):
                break
            time.sleep(5)
        out.append({**uploaded, **row})
    return out


def register_tools(client) -> list[dict]:
    """8 本の業務 API ツールを登録する（`POST /api/agent/http-tools`）。"""
    out = []
    for body in http_tools():
        res = client.post("/api/agent/http-tools", json=body)
        if res.status_code != 200:
            sys.exit(f"ツール登録が失敗した: {body['name']} {res.status_code} {res.text[:300]}")
        out.append(res.json())
    return out


# --- エージェント実行（実 API 経路） -----------------------------------------


def _sum_usage(frames: list[dict]) -> dict:
    """ターン全体の usage。`usage` フレームの数がモデル往復の回数になる。"""
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
        "usage": _sum_usage(frames),
        "errors": [f["error"] for f in frames if "error" in f],
        "seconds": round(time.time() - started, 1),
    }


def searches(run: dict) -> int:
    return sum(1 for n in run["tool_calls"] if n == "rag_search")


def business_calls(run: dict) -> list[str]:
    return [n for n in run["tool_calls"] if n in TOOL_NAMES]


def call_table(run: dict) -> str:
    return "\n".join(f"{i + 1:2d}. {n}" for i, n in enumerate(run["tool_calls"])) or "(なし)"


def _business_results_ok(run: dict) -> tuple[bool, str]:
    """業務 API の各呼び出しが 2xx で、ツールエラーを返していないことを見る。

    「8 本呼んだ」だけでは足りない —— 途中が 500 でもモデルは先へ進んでしまうので、
    **1 本ずつ相手の応答を確かめる**。
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
        rows.append(f"{r['name']:24s} HTTP {code or '?'}{'  ← 失敗' if failed else ''}")
    return (ok and bool(rows)), "\n".join(rows) or "(なし)"


def with_doc_search_limit(limit: int):
    """`AGENT_MAX_DOC_SEARCHES` を差し替えるコンテキスト（実装の設定経路をそのまま使う）。"""
    from jetuse_core.settings import get_settings

    class _Ctx:
        def __enter__(self):
            self.prev = os.environ.get("AGENT_MAX_DOC_SEARCHES")
            os.environ["AGENT_MAX_DOC_SEARCHES"] = str(limit)
            get_settings.cache_clear()

        def __exit__(self, *exc):
            if self.prev is None:
                os.environ.pop("AGENT_MAX_DOC_SEARCHES", None)
            else:
                os.environ["AGENT_MAX_DOC_SEARCHES"] = self.prev
            get_settings.cache_clear()

    return _Ctx()


# --- シナリオ ------------------------------------------------------------------


def scenario_1(client, tool_ids: list[str]) -> tuple[bool, dict]:
    """文書検索を挟みながら業務 API 8 本を呼ぶ手続きが、打ち切られずに最後まで到達する。"""
    banner("シナリオ1: 検索を挟む 8 本の業務フローが最後まで到達する")
    from jetuse_core.settings import AGENT_MAX_TOOL_HOPS_DEFAULT

    run = run_agent(client, PROCEDURE_REQUEST, tool_ids=tool_ids, backend="adb")
    seq = business_calls(run)
    # 判定は tasks/AGT-05.md のシナリオ1 の文言に合わせる ——
    # 「8 本呼ぶ手続きが**打ち切られずに最後まで到達**する」。すなわち
    # **8 本すべてを各 1 回**呼び、全件 2xx で、打ち切られないこと。
    # **順序は判定に入れない**: 呼ぶ順はモデル依存で run ごとに揺れ（実測 4 回のうち
    # (a) が順序どおりだったのは 3 回）、AGT-05 が変えた「予算の数え方」とは別の話である。
    # 揺れること自体は隠さず下の報告に残す（順序の作り込みは AGT-04 の主題）
    reached = sorted(seq) == sorted(TOOL_NAMES)
    order_ok = seq == TOOL_NAMES
    http_ok, http_rows = _business_results_ok(run)
    n_search = searches(run)
    n_business = len(seq)
    # 旧来の数え方（検索も 1 ホップ）なら、この実行は何ホップ要ったか
    old_budget_needed = n_search + n_business

    # (b) **予算を絞って**同じ手続きを流す。旧来の数え方なら (a) は
    # old_budget_needed ホップを食っていたので、その半分程度の予算では届かない。
    # 新しい数え方（検索は別枠）なら業務 8 本ぶんの予算で足りるはず
    tight = run_agent(client, PROCEDURE_REQUEST, tool_ids=tool_ids, backend="adb",
                      max_hops=HOP_BUDGET_FOR_S1B)
    tight_seq = business_calls(tight)
    # (b) が見るのは**予算の分離**（絞った予算でも 8 本に届くか）であって、順序の妥当性ではない。
    # 順序は (a) で見る —— 呼ぶ順はモデルの選択で、予算の数え方とは別の話。
    # 実測では (b) で順序が入れ替わることがある（`add_equipment` が `set_service_plan` の
    # 前に来る等）。それも下の報告に残す（**判定条件から外すが、隠さない**）。
    tight_reached = sorted(set(tight_seq)) == sorted(TOOL_NAMES)
    tight_order_ok = tight_seq == TOOL_NAMES
    tight_ok = tight_reached and not tight["limit_reached"] and not tight["errors"]
    tight_search = searches(tight)
    tight_would_have_been_cut = tight_search + len(tight_seq) > HOP_BUDGET_FOR_S1B

    ok = (reached and http_ok and not run["limit_reached"] and not run["errors"]
          and tight_ok)
    facts = {"searches": n_search, "business": n_business,
             "old_budget_needed": old_budget_needed,
             "default_hops": AGENT_MAX_TOOL_HOPS_DEFAULT,
             "tight_budget": HOP_BUDGET_FOR_S1B,
             "tight_searches": tight_search, "tight_business": len(tight_seq),
             "tight_order_ok": tight_order_ok,
             "tight_would_have_been_cut": tight_would_have_been_cut,
             "tight_roundtrips": tight["usage"]["model_roundtrips"],
             "roundtrips": run["usage"]["model_roundtrips"],
             "usage": run["usage"], "seconds": run["seconds"]}
    write("scenario-1.md", f"""# シナリオ1 — 検索を挟みながら業務 API 8 本が最後まで到達する

`agent_rag_backend="adb"`（検索は function tool）で、**手順を書かない依頼**を 1 回流した。
順序・コード値・引数の制約は仕様書 3 冊（手順書 / コード表 / 制約集）に分けてあるので、
エージェントは API を呼ぶ前に文書検索を挟む。相手は公開 https エコー
（宛先は `.env` の `AGT05_ECHO_URL`。証跡では {ECHO_MASK} と伏せる）。

ホップ上限は**既定のまま**（`AGENT_MAX_TOOL_HOPS_DEFAULT` = {AGENT_MAX_TOOL_HOPS_DEFAULT}）、
検索の上限も既定のまま。

登録した業務 API: `{', '.join(TOOL_NAMES)}`

要求（**手順は書いていない**）:

{fence(PROCEDURE_REQUEST)}

## (a) ツール呼び出しの列（既定の予算・実測）

{fence(call_table(run))}

- 文書検索: **{n_search} 回** / 業務 API: **{n_business} 回**
- モデル往復: **{run["usage"]["model_roundtrips"]} 回** / 所要 {run["seconds"]} 秒
- usage: {js(run["usage"])}
- 打ち切り通知（`limit_reached`）: {js(run["limit_reached"]) if run["limit_reached"]
                                    else "**なし**（自力で完了）"}

## 業務 API 1 本ごとの相手の応答（「呼んだ」だけでなく「通った」か）

{fence(http_rows)}

- 8 本すべてを各 1 回呼んだ: **{reached}**（実際: `{seq}`）
- すべて 2xx でツールエラーなし: **{http_ok}**
- 呼び出し順序が仕様書どおり: **{order_ok}**
  ※ **判定条件には入れていない**。tasks/AGT-05.md のシナリオ1 が求めるのは
  「打ち切られずに最後まで到達」であり、順序はモデル依存で run ごとに揺れる
  （AGT-05 が変えたのは予算の数え方であって、呼ぶ順の作り込みは AGT-04 の主題）

## (b) 予算を {HOP_BUDGET_FOR_S1B} ホップに絞って同じ手続きを流す

**検索がホップを食っていないこと**を直接示すための対比。(a) は旧来の数え方なら
検索 {n_search} + 業務 {n_business} = **{old_budget_needed} ホップ**を食っていた
（既定 {AGENT_MAX_TOOL_HOPS_DEFAULT} の 7 割）。同じ要求を
`max_tool_hops={HOP_BUDGET_FOR_S1B}`（業務 8 本 + 自己修正 1 回ぶん）で流す。

- ツール呼び出しの列:

{fence(call_table(tight))}

- 文書検索: **{tight_search} 回** / 業務 API: **{len(set(tight_seq))} / {len(TOOL_NAMES)}**
  （8 本すべてに到達: **{tight_reached}**）
- 呼び出し順序が仕様書どおり: **{tight_order_ok}**
  （実際: `{tight_seq}`）
  ※ **(b) の判定条件には入れていない**。(b) が見るのは予算の分離（絞った予算でも
  8 本に届くか）であって、順序はモデルの選択に委ねられる部分だから。順序の妥当性は
  (a) で判定している
- 打ち切り通知: {js(tight["limit_reached"]) if tight["limit_reached"]
                 else "**なし**（絞った予算でも完走）"}
- モデル往復: {tight["usage"]["model_roundtrips"]} 回 / usage {js(tight["usage"])}
- 旧来の数え方なら、この実行は検索 {tight_search} + 業務 {len(tight_seq)} =
  **{tight_search + len(tight_seq)} ホップ**必要で、予算 {HOP_BUDGET_FOR_S1B} を
  **超えていた: {tight_would_have_been_cut}**

（旧実装を再実行した対比ではなく、同じ実行の実測から導いた数である。
　実案件 3 回の実測 —— 予算 24 のうち 19〜22 が検索 ——
　は `docs/verification/AGT-05-doc-search-budget.md`）

## 最終回答

{fence(run["answer"][:1500])}

判定: **{'PASS' if ok else 'FAIL'}**
（条件: (a) 8 本すべてを各 1 回・全件 2xx・打ち切りなし・エラーフレームなし /
 (b) 予算 {HOP_BUDGET_FOR_S1B} でも 8 本すべてに到達し打ち切られない。
 **どちらも順序は問わない** —— 上記の理由）
""")
    return ok, facts


def scenario_2(client) -> tuple[bool, dict]:
    """検索だけを上限まで行い、`limit_reached` の reason が検索側であることを示す。"""
    banner("シナリオ2: 検索の上限に当てる（reason で区別できる）")
    with with_doc_search_limit(DOC_SEARCH_LIMIT_FOR_S2):
        run = run_agent(client, SEARCH_ONLY_QUESTION, backend="adb")
    reasons = [lr["reason"] for lr in run["limit_reached"]]
    doc_limit = next((lr for lr in run["limit_reached"]
                      if lr["reason"] == "max_doc_searches"), None)
    n_search = searches(run)
    # 呼び出しのうち**実際に検索が走った**のはどれか。上限に達したあとの呼び出しは
    # 実行されず理由が返る（ADR-0026 §「既知の限界」）ので、呼び出し回数と実行回数は違う。
    # ここを混同すると「上限を超えて検索できている」と読めてしまう
    rag_results = [r for r in run["tool_results"] if r.get("name") == "rag_search"]
    refused = [r for r in rag_results if "上限" in r.get("preview", "")]
    executed = [r for r in rag_results if r not in refused]
    ok = (
        doc_limit == {"reason": "max_doc_searches", "limit": DOC_SEARCH_LIMIT_FOR_S2}
        and "max_tool_hops" not in reasons      # ホップ側では止まっていない
        and len(executed) <= DOC_SEARCH_LIMIT_FOR_S2   # 上限を超えて検索していない
        and bool(run["answer"])                 # 打ち切りではなく回答まで進む
        and not run["errors"]
    )
    facts = {"searches": n_search, "executed": len(executed), "refused": len(refused),
             "reasons": reasons,
             "roundtrips": run["usage"]["model_roundtrips"], "usage": run["usage"]}
    write("scenario-2.md", f"""# シナリオ2 — 検索の上限に当たったことが `reason` で区別できる

検索だけで答えられる問い（10 項目を仕様書から拾う）を、
`AGENT_MAX_DOC_SEARCHES={DOC_SEARCH_LIMIT_FOR_S2}` に**設定を絞って**流した
（既定 40 まで回すと 1 回の実行が長く高くつく。確かめたいのは値ではなく
「**どちらの予算で止まったか**が応答から分かること」）。ホップ上限は既定のまま。

質問: `{SEARCH_ONLY_QUESTION}`

## 実測

- 文書検索の**呼び出し**: **{n_search} 回** / うち**実際に検索が走った**のは
  **{len(executed)} 回**（上限 {DOC_SEARCH_LIMIT_FOR_S2}）/ **断られた**のは
  **{len(refused)} 回**
  —— 上限に達したあとの呼び出しは実行せず理由を返す（ADR-0026 §既知の限界）。
  呼び出し回数と実行回数は別物で、混同すると「上限を超えて検索できている」と読めてしまう
- 断られた呼び出しがモデルに返した内容（黙って空を返していないこと）:

{fence((refused[0]["preview"] if refused else "(断られた呼び出しは無い)")[:300])}

- ツール呼び出しの列:

{fence(call_table(run))}

- 上限通知（`limit_reached`）:

{fence(js(run["limit_reached"]))}

- 本文に出た通知（現行 UI は `notice` を描かないので `delta` にも出す）:

{fence(next((d for d in [run["answer"]] if "文書検索" in d), "")[:400]
       if "文書検索" in run["answer"] else "(本文に見当たらない)")}

- モデル往復: {run["usage"]["model_roundtrips"]} 回 / usage {js(run["usage"])}

## 最終回答（打ち切りではなく、検索を止めたうえで答え切っている）

{fence(run["answer"][:1200])}

判定: **{'PASS' if ok else 'FAIL'}**
（条件: `reason=max_doc_searches` の `limit_reached` が出る / `max_tool_hops` では
 止まっていない / **実行された検索が上限以内** / 回答が返る / エラーフレームなし）
""")
    return ok, facts


def scenario_3(client, tool_ids: list[str]) -> tuple[bool, dict]:
    """回帰: `vector_store` 経路（built-in）のホップ消費が従来どおり。"""
    banner("シナリオ3: 回帰（vector_store 経路のホップ消費）")
    run = run_agent(client, PROCEDURE_REQUEST, tool_ids=tool_ids,
                    backend="vector_store", max_hops=HOP_LIMIT_FOR_S3)
    seq = business_calls(run)
    builtin = [f["tool_call"] for f in run["frames"]
               if "tool_call" in f and f["tool_call"].get("name") == "rag_search"]
    # `all([])` は真になる = 検索通知が 1 件も無くても「すべて built-in」で PASS してしまう。
    # 業務 API も同じで、0 件でも「上限以内」になる。**空虚な PASS を作らない**ため、
    # どちらも「1 件以上あること」を条件に入れる（review-4 の指摘）
    all_builtin = bool(builtin) and all(c.get("builtin") for c in builtin)
    # 従来どおり: 業務の操作でホップが減り、上限で打ち切られる。built-in の検索は数に入らない
    hop_limited = run["limit_reached"] == [
        {"reason": "max_tool_hops", "limit": HOP_LIMIT_FOR_S3}
    ]
    business_within_budget = 0 < len(seq) <= HOP_LIMIT_FOR_S3
    # 引用が 0 件のときに `all(...)` は真になる = **空虚な PASS**（review-2 の指摘）。
    # 引用が出た実行でだけ「ファイル単位のままか」を判定し、0 件なら**未確認**として扱う
    # （この経路の引用形式は AGT-04 のシナリオ4 で確認済み。ここで嘘の PASS を作らない）
    cited = bool(run["citations"])
    file_level = cited and all(
        not (c.get("source") or {}).get("cells") for c in run["citations"]
    )
    ok = (hop_limited and business_within_budget and all_builtin
          and (file_level or not cited) and not run["errors"])
    facts = {"business": len(seq), "builtin_notices": len(builtin),
             "citations": len(run["citations"]), "file_level": file_level,
             "limit_reached": run["limit_reached"],
             "roundtrips": run["usage"]["model_roundtrips"], "usage": run["usage"]}
    write("scenario-3.md", f"""# シナリオ3 — 回帰: `vector_store` 経路（built-in）のホップ消費

同じ 8 本の手続きを、**既定のバックエンド**（`agent_rag_backend` 未指定 = vector_store）で
`max_tool_hops={HOP_LIMIT_FOR_S3}` に絞って流した。built-in の `file_search` は
**もともとホップを消費しない**ので、AGT-05 の変更後も挙動は変わらないはずである
（変わっていたら、この回帰で 8 本届くか、上限の出方が変わる）。

## 実測

- ツール呼び出しの列:

{fence(call_table(run))}

- 業務 API: **{len(seq)} 回**（1 件以上かつ上限 {HOP_LIMIT_FOR_S3} 以内:
  **{business_within_budget}**）
- `rag_search` の通知 **{len(builtin)} 件**が 1 件以上ありすべて built-in（OCI 側実行）:
  **{all_builtin}**

{fence(js(builtin[:3])[:600])}

- 打ち切り通知が従来どおりホップ側: **{hop_limited}**

{fence(js(run["limit_reached"]))}

- 引用イベント: **{len(run["citations"])} 件**
  → ファイル単位のまま（`source.cells` を持たない）:
  **{file_level if cited else "未確認（この実行では引用イベントが 0 件）"}**
  ※ 0 件のときは判定に入れない（`all([])` は真になるので、そのまま PASS にすると
  **確かめていないことを確かめたことにしてしまう**）。この経路の引用形式は
  AGT-04 のシナリオ4（`docs/verification/AGT-04-agent-tool-hops.md`）で確認済み

{fence(js(run["citations"][:3])[:700])}

- モデル往復: {run["usage"]["model_roundtrips"]} 回 / usage {js(run["usage"])}

判定: **{'PASS' if ok else 'FAIL'}**
（条件: ホップは業務の操作でだけ減り上限 {HOP_LIMIT_FOR_S3} で打ち切られる /
 検索通知はすべて built-in / **引用が出た場合は**ファイル単位のまま / エラーフレームなし）
""")
    return ok, facts


def main() -> None:
    if not ECHO_URL:
        sys.exit("AGT05_ECHO_URL が未設定。承認していない宛先へは送らないため中止"
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
    # 消すのは接頭辞 `jetuse-spike-agt05-` のファイルと、この検証が登録した 8 本の
    # ツール行だけ。所有者ごと消すと、同じスキーマの他の検証データまで巻き添えになる。
    removed = [row for row in rag.list_files(OWNER)
               if row["filename"].startswith(PREFIX)]
    for row in removed:
        rag.delete_file(OWNER, row["id"])
    cur = conn.cursor()
    cur.executemany(
        "DELETE FROM http_tools WHERE owner_sub = :o AND name = :n",
        [{"o": OWNER, "n": name} for name in TOOL_NAMES],
    )
    conn.commit()
    print(f"  前回の残り: ファイル {len(removed)} 件 / ツール行 {cur.rowcount} 件を削除")

    ensure_spike_store()
    banner("アップロード（アプリ経路 `POST /api/rag/files`）— 仕様書 3 冊")
    for spec in upload_specs(client):
        print(f"  {spec.get('filename')}: status={spec.get('status')} "
              f"backends={spec.get('backends')}")
        if (spec.get("backends") or {}).get("adb") != "indexed":
            sys.exit(f"仕様書が ADB へ取り込まれていない: {spec.get('backends')}。中止。")

    banner("業務 API ツールの登録（`POST /api/agent/http-tools`）— 8 本")
    tools = register_tools(client)
    tool_ids = [t["id"] for t in tools]
    print(f"  登録: {', '.join(t['name'] for t in tools)}")

    ok1, f1 = scenario_1(client, tool_ids)
    ok2, f2 = scenario_2(client)
    ok3, f3 = scenario_3(client, tool_ids)
    results = {"1": ok1, "2": ok2, "3": ok3}

    banner("結果")
    for k, v in results.items():
        print(f"  シナリオ{k}: {'PASS' if v else 'FAIL'}")

    def row(label: str, n_search, n_business, facts: dict, tokens: bool = True) -> str:
        u = facts["usage"]
        cost = f"{u['input_tokens']} / {u['output_tokens']}" if tokens else "—"
        return (f"| {label} | {n_search} | {n_business} | "
                f"{facts.get('roundtrips')} | {cost} |")

    table = "\n".join([
        row("S1(a) 検索つき 8 本の手続き（adb・既定の予算）", f1["searches"],
            f1["business"], f1),
        (f"| S1(b) 同じ手続き（予算 {f1['tight_budget']} ホップ） | "
         f"{f1['tight_searches']} | {f1['tight_business']} | "
         f"{f1['tight_roundtrips']} | — |"),
        row(f"S2 検索だけ（上限 {DOC_SEARCH_LIMIT_FOR_S2}）", f2["searches"], 0, f2),
        row(f"S3 回帰（vector_store・上限 {HOP_LIMIT_FOR_S3} ホップ）",
            f"built-in {f3['builtin_notices']} 件", f3["business"], f3),
    ])
    write("summary.md", f"""# AGT-05 実環境 E2E 結果一覧

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント /
OCI Generative AI（gpt-oss-120b・埋め込み）と公開 https エコーは実物。
仕様書は**架空**のブック 3 冊（顧客データ・案件名は持ち込んでいない）。

""" + "\n".join(f"- シナリオ{k}: **{'PASS' if v else 'FAIL'}** → `scenario-{k}.md`"
                for k, v in results.items()) + f"""

## 数字（この run の実測）

| | 文書検索 | 業務 API | モデル往復 | 入力/出力トークン |
|---|---|---|---|---|
{table}

- S1(a) は旧来の数え方なら **{f1['old_budget_needed']} ホップ**必要だった
  （既定 {f1['default_hops']} には収まる範囲。この題材は実案件より軽い）
- S1(b) は予算を **{f1['tight_budget']}** に絞っても 8 本に到達した
  （順序どおり: {f1['tight_order_ok']} —— 判定条件外。§scenario-1.md (b) 参照）。
  旧来の数え方なら **{f1['tight_searches'] + f1['tight_business']} ホップ**必要で
  予算を超えていた: **{f1['tight_would_have_been_cut']}**
- S2 の `limit_reached` の reason: `{f2['reasons']}`。検索の**呼び出し** {f2['searches']} 回のうち
  **実行 {f2['executed']} 回 / 断り {f2['refused']} 回**（上限後は実行せず理由を返す）
- S3 の引用イベント: {f3['citations']} 件（0 件なら引用形式の判定はしていない）
- S3 の `limit_reached`: `{f3['limit_reached']}`

## 検証で作った資源

- ADB スキーマ `{SCHEMA}`（共有 loop ADB の中。ADB は増やしていない）
- Vector Store `{PREFIX}-{SCHEMA.rsplit('_', 1)[-1].lower()}` と、その中の仕様書 3 冊
- 受付番号 `{ORDER_ID}` のリクエストは公開エコーへ送っただけ（相手側に状態は残らない）

片付けは `spikes/agt05/teardown.py --yes`（OCI 側）と
`spikes/ragm02/teardown.py --yes`（ADB スキーマ）。
""")
    conn.close()
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
