"""AGT-06 §C: 多段の業務手続きをモデル横断で定量比較する。

**なぜ要るか**: 案件デモで観測した壊れ方は「1 本のツールを呼べるか」では出ない。
`order_context_initialize` が `requested_product_code` を返しているのに、モデルがそれを
使わず**同じ検索を 12 回**繰り返し、最後に**架空のコードを作って**実行せず手順書を返した。
比較できる別モデルが無いと、これがモデルの限界かプロンプトの問題か切り分けられない。

そこで**同じ壊れ方を誘発する手続き**を用意し、同一シナリオを全モデルで流して数える:
  - 手続きを完了できたか(completed)
  - 正しい順序で呼べた本数(correct_sequence)
  - 検索の回数(searches。12 回繰り返した個体を捕まえる)
  - 架空のコードを作ったか(invented_code。デモで実際に起きた失敗)
  - 自己修正の回数(recovered = 拒否されたあとに正しくやり直せた回数)
  - 往復数(hops)・所要秒・トークン

ツールは**プロセス内の状態機械**で相手をする(HTTP は使わない)。外部 HTTP 経路そのものは
`spikes/agt06/e2e.py` のシナリオ1/4 が実物で確かめている。ここで測るのは**モデルの振る舞い**。

実行:
  PYTHONPATH=packages/api .venv/bin/python spikes/agt06/compare.py
"""

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "api"))
sys.path.insert(0, str(ROOT / "ops"))

import _adb as adb  # noqa: E402

os.environ["OCI_REGION"] = "us-chicago-1"

from jetuse_core.chat import stream_agent  # noqa: E402
from jetuse_core.models import MODELS  # noqa: E402
from jetuse_core.tools import ToolDef  # noqa: E402

PROJECT_OCID = adb.env("AGT06_CHICAGO_PROJECT_OCID").strip()

# 架空の受注。実在しない品番だけを使う(顧客データ・案件名は持ち込まない)
ORDER_ID = "ORD-2001"
TRUE_CODE = "PX-9001"  # initialize だけが教える正しい品番
DECOYS = ["PX-9010", "PX-9100", "PX-8001"]  # 検索で出てくるが正解ではない


def build_tools(state: dict) -> list[ToolDef]:
    """架空の受注 API(状態機械)。デモで観測した壊れ方を誘発する形にする。"""

    def initialize(args: dict) -> str:
        state["calls"].append("initialize")
        return json.dumps({
            "order_id": ORDER_ID,
            # **ここで正解を渡している**。デモではこれを使わずに検索を繰り返した
            "requested_product_code": TRUE_CODE,
            "next_steps": ["set_product", "confirm_order"],
        }, ensure_ascii=False)

    def search(args: dict) -> str:
        state["calls"].append("search")
        state["searches"] += 1
        kw = str(args.get("keyword", ""))
        state["queries"].append(kw)
        # 正解は**検索では出ない**(initialize の戻り値を使うしかない)
        return json.dumps({"results": [{"code": c, "name": f"製品{c}"} for c in DECOYS]},
                          ensure_ascii=False)

    def set_product(args: dict) -> str:
        state["calls"].append("set_product")
        code = str(args.get("product_code", ""))
        state["set_attempts"].append(code)
        if code != TRUE_CODE:
            state["rejections"] += 1
            if code not in DECOYS:
                state["invented"].append(code)
            return json.dumps({"error": f"unknown product code: {code}"},
                              ensure_ascii=False)
        state["product_set"] = True
        return json.dumps({"status": "ok", "product_code": code}, ensure_ascii=False)

    def confirm(args: dict) -> str:
        state["calls"].append("confirm_order")
        if not state["product_set"]:
            return json.dumps({"error": "product not set yet"}, ensure_ascii=False)
        state["completed"] = True
        return json.dumps({"status": "confirmed", "order_id": ORDER_ID},
                          ensure_ascii=False)

    def td(name, desc, props, required, handler):
        return ToolDef(name=name, label=name, description=desc,
                       parameters={"type": "object", "properties": props,
                                   "required": required},
                       handler=handler, requires_approval=False)

    return [
        td("order_context_initialize", "受注の作業を開始し、対象の受注内容を取得する。"
           "最初に必ず呼ぶこと",
           {"order_id": {"type": "string", "description": "受注番号"}},
           ["order_id"], initialize),
        td("product_search", "製品カタログをキーワード検索する",
           {"keyword": {"type": "string", "description": "検索キーワード"}},
           ["keyword"], search),
        td("set_product", "受注に品番を設定する",
           {"order_id": {"type": "string"},
            "product_code": {"type": "string", "description": "設定する品番"}},
           ["order_id", "product_code"], set_product),
        td("confirm_order", "受注を確定する。品番の設定後に呼ぶ",
           {"order_id": {"type": "string"}}, ["order_id"], confirm),
    ]


QUESTION = (
    f"受注 {ORDER_ID} の手続きを最後まで進めてください。"
    "作業開始 → 品番の設定 → 受注確定、の順です。"
)
INSTRUCTIONS = (
    "あなたは受発注の担当者です。ツールを使って手続きを最後まで実行します。"
    "推測で品番を作ってはいけません。手順書を返すのではなく、実際にツールを呼んで完了させます。"
)


def run(model_key: str) -> dict:
    state = {"calls": [], "searches": 0, "queries": [], "set_attempts": [],
             "invented": [], "rejections": 0, "product_set": False, "completed": False}
    tools = build_tools(state)
    events, error = [], None
    t0 = time.time()
    try:
        for ev in stream_agent(
            model_key, [{"role": "user", "content": QUESTION}],
            auto_tools=True, instructions=INSTRUCTIONS, enabled_tools=[],
            http_tools=tools, project_ocid=PROJECT_OCID or None,
        ):
            events.append(ev)
            if "error" in ev:
                error = ev["error"]
    except Exception as e:  # noqa: BLE001 - 落ちたことも観測値
        error = f"{type(e).__name__}: {str(e)[:200]}"
    elapsed = round(time.time() - t0, 1)

    calls = state["calls"]
    # 正しい順序で踏めた段数: initialize → set_product(正解) → confirm_order
    correct = 0
    if "initialize" in calls:
        correct += 1
        if state["product_set"]:
            correct += 1
            if state["completed"]:
                correct += 1
    usage = next((e["usage"] for e in reversed(events) if "usage" in e), None)
    text = "".join(e["delta"] for e in events if "delta" in e)
    return {
        "model": model_key,
        "oci_id": MODELS[model_key].oci_id,
        "completed": state["completed"],
        "correct_sequence": correct,          # 3 が満点
        "tool_calls": len(calls),
        "searches": state["searches"],
        "repeated_searches": len(state["queries"]) - len(set(state["queries"])),
        "invented_codes": state["invented"],  # デモで起きた失敗そのもの
        "rejections": state["rejections"],    # 拒否された回数
        "recovered": bool(state["rejections"] and state["completed"]),
        "elapsed_s": elapsed,
        "usage": usage,
        "call_order": calls,
        "answer_tail": text[-160:],
        "error": error,
    }


MODELS_UNDER_TEST = ["gpt-oss-120b", "gpt-oss-20b", "grok-4.3",
                     "grok-4.20-reasoning", "grok-4.20-non-reasoning",
                     "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

# 1 回だけだと当たり外れが分からない(比較ドキュメントに載せる数字なので試行を重ねる)
TRIALS = int(os.environ.get("AGT06_TRIALS", "3"))

rows = []
for key in MODELS_UNDER_TEST:
    for trial in range(1, TRIALS + 1):
        r = run(key)
        r["trial"] = trial
        rows.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)


def summarize(key: str) -> dict:
    rs = [r for r in rows if r["model"] == key]
    n = len(rs)
    return {
        "model": key,
        "trials": n,
        "completed": sum(1 for r in rs if r["completed"]),
        "avg_correct": round(sum(r["correct_sequence"] for r in rs) / n, 2),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in rs) / n, 1),
        "total_searches": sum(r["searches"] for r in rs),
        "total_repeated_searches": sum(r["repeated_searches"] for r in rs),
        "total_invented": sum(len(r["invented_codes"]) for r in rs),
        "total_rejections": sum(r["rejections"] for r in rs),
        "avg_elapsed_s": round(sum(r["elapsed_s"] for r in rs) / n, 1),
        "errors": [r["error"] for r in rs if r["error"]],
    }


summary = [summarize(k) for k in MODELS_UNDER_TEST]
print(f"\n{'model':26} {'完了':7} {'順序平均':9} {'呼出':6} {'検索':5} {'再検索':7} "
      f"{'架空':5} {'拒否':5} {'秒':6}")
for s in summary:
    print(f"{s['model']:26} {s['completed']}/{s['trials']:<5} {s['avg_correct']:<9} "
          f"{s['avg_tool_calls']:<6} {s['total_searches']:<5} "
          f"{s['total_repeated_searches']:<7} {s['total_invented']:<5} "
          f"{s['total_rejections']:<5} {s['avg_elapsed_s']:<6}")

out = os.environ.get("COMPARE_OUT", "agt06-compare.json")
pathlib.Path(out).write_text(json.dumps(
    {"trials_per_model": TRIALS, "summary": summary, "runs": rows},
    ensure_ascii=False, indent=2))
print(f"\n証跡: {out}")
