"""AGT-06 の実環境 E2E(tasks/AGT-06.md の「E2E シナリオ」)。

**実装(`jetuse_core.chat.stream_agent` / `jetuse_core.http_tools`)をそのまま呼ぶ**。
検証用の別実装は書かない。相手(公開 https エコー)も OCI Generative AI(us-chicago-1)も実物。

  1. **シカゴで Grok に、入れ子引数を持つ外部 HTTP ツールを呼ばせる**。
     相手が受け取った本文が**入れ子のまま**であることで判定する。
  2. **同じシナリオを複数モデルで流して比較表**にする(往復数・ツール呼び出し数・
     入れ子が保たれたか・所要秒)。gemini は system ロールと id 積み直しの吸収が要る。
  3. **エージェント不可のモデルは断られる**(黙って壊れない)。
  4. 回帰: `gpt-oss-120b` の挙動が変わっていない。

DB は使わない(登録簿の行は組み立てて `http_tools.to_tooldef` に渡す)。
**OCI 側に検証用リソースを作らない** — 既存の `jetuse-loop-project`(シカゴ)を使う。

実行:
  PYTHONPATH=packages/api .venv/bin/python spikes/agt06/e2e.py
"""

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "api"))
sys.path.insert(0, str(ROOT / "ops"))

import _adb as adb  # noqa: E402  環境変数 → .env の順で読む(ops と同じ流儀)

# **シカゴで測る**。settings は import 時に env を読むので、他より先に置く。
os.environ["OCI_REGION"] = "us-chicago-1"

from jetuse_core import http_tools  # noqa: E402
from jetuse_core.chat import stream_agent  # noqa: E402
from jetuse_core.model_compat import agent_refusal  # noqa: E402
from jetuse_core.models import MODELS  # noqa: E402

# 相手の宛先はコードに焼かない(環境依存値は .env。雛形は .env.example の AGT06_ECHO_URL)。
# 未設定なら実行を断る = 承認していない宛先へ送らない
ECHO_URL = adb.env("AGT06_ECHO_URL").strip()

# シカゴの GenAI プロジェクト。project はリージョン別で、大阪のものを送ると 400 になる
PROJECT_OCID = adb.env("AGT06_CHICAGO_PROJECT_OCID").strip() or adb.env(
    "CHICAGO_PROJECT_OCID"
).strip()

# --- 検証用フィクスチャ(架空の受注 API。顧客データ・案件名は持ち込まない) ------------

ORDER_TOOL_ROW = {
    "id": "agt06-order",
    "name": "set_order_items",
    "description": (
        "受注システムに商品明細を登録する。明細は品番・数量と、その行に付けるオプション"
        "(コードと値)を持つ。明細を登録するときは必ずこれを使う"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "受注番号(例 ORD-1001)"},
            "item": {
                "type": "object",
                "description": "明細1行",
                "properties": {
                    "part": {"type": "string", "description": "品番"},
                    "qty": {"type": "integer", "description": "数量"},
                    "options": {
                        "type": "object",
                        "description": "オプション",
                        "properties": {
                            "rush": {"type": "boolean", "description": "至急かどうか"},
                            "note": {"type": "string", "description": "備考"},
                        },
                        "required": ["rush"],
                    },
                },
                "required": ["part", "qty", "options"],
            },
        },
        "required": ["order_id", "item"],
    },
    "url": ECHO_URL,
    "method": "POST",
    "headers": None,
    "idempotency_header": None,
    "auth_header": None,
}

QUESTION = (
    "受注番号 ORD-1001 に、品番 JX-7742 を 3 個、至急扱い(備考は「AGT-06検証」)で"
    "明細登録してください。set_order_items ツールを使うこと。"
)
INSTRUCTIONS = "あなたは受発注の担当者です。手順どおりツールを使い、結果を簡潔に報告します。"

EXPECTED_ITEM = {"part": "JX-7742", "qty": 3, "options": {"rush": True}}


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}", flush=True)


def _nested_ok(sent: dict) -> tuple[bool, str]:
    """相手が受け取った本文が**入れ子のまま**で、値も正しいか。"""
    item = sent.get("item")
    if not isinstance(item, dict):
        return False, f"item が入れ子で届いていない: {type(item).__name__}"
    opts = item.get("options")
    if not isinstance(opts, dict):
        return False, f"item.options が入れ子で届いていない: {type(opts).__name__}"
    for k, v in (("part", EXPECTED_ITEM["part"]), ("qty", EXPECTED_ITEM["qty"])):
        if item.get(k) != v:
            return False, f"item.{k} が {item.get(k)!r}(期待 {v!r})"
    if opts.get("rush") is not True:
        return False, f"item.options.rush が {opts.get('rush')!r}(期待 True)"
    if sent.get("order_id") != "ORD-1001":
        return False, f"order_id が {sent.get('order_id')!r}"
    return True, "入れ子のまま・値も一致"


def run_model(model_key: str) -> dict:
    """1 モデルでシナリオを流し、観測値を返す。実装の stream_agent をそのまま使う。"""
    received: list[dict] = []
    row = dict(ORDER_TOOL_ROW)
    tooldef = http_tools.to_tooldef(row)

    # 相手が受け取った本文を記録する(判定の根拠は**相手が受け取ったもの**)。
    # ツール実行そのものは実装の call_tool を通す(検証用の別実装は書かない)。
    original_handler = tooldef.handler

    def recording_handler(args: dict) -> str:
        out = original_handler(args)
        try:
            body = json.loads(json.loads(out)["body"])
            received.append(body.get("json") or body.get("data") or body)
        except Exception:  # noqa: BLE001 - 記録の失敗で本筋を止めない
            received.append({"_unparsed": out[:200]})
        return out

    tooldef = type(tooldef)(**{**tooldef.__dict__, "handler": recording_handler,
                               "requires_approval": False})

    events: list[dict] = []
    t0 = time.time()
    error = None
    try:
        for ev in stream_agent(
            model_key,
            [{"role": "user", "content": QUESTION}],
            auto_tools=True,
            instructions=INSTRUCTIONS,
            enabled_tools=[],
            http_tools=[tooldef],
            project_ocid=PROJECT_OCID or None,
        ):
            events.append(ev)
            if "error" in ev:
                error = ev["error"]
    except Exception as e:  # noqa: BLE001 - 落ちたことも観測値
        error = f"{type(e).__name__}: {str(e)[:200]}"
    elapsed = round(time.time() - t0, 1)

    tool_calls = [e["tool_call"] for e in events if "tool_call" in e]
    text = "".join(e["delta"] for e in events if "delta" in e)
    usage = next((e["usage"] for e in reversed(events) if "usage" in e), None)
    ok, why = (_nested_ok(received[0]) if received else (False, "相手に1件も届いていない"))
    return {
        "model": model_key,
        "oci_id": MODELS[model_key].oci_id if model_key in MODELS else "?",
        "ok": ok and error is None,
        "why": why if error is None else f"error: {error}",
        "tool_calls": len(tool_calls),
        "http_calls": len(received),
        "elapsed_s": elapsed,
        "usage": usage,
        "answer": text[:200],
        "received": received[:1],
        "error": error,
    }


def main() -> int:
    if not ECHO_URL:
        print("AGT06_ECHO_URL が未設定です(.env)。承認していない宛先へは送りません。")
        return 2

    results: dict = {"region": "us-chicago-1", "echo_url": ECHO_URL,
                     "project_ocid_set": bool(PROJECT_OCID)}
    failures: list[str] = []

    # --- シナリオ1: Grok が入れ子引数つき外部 HTTP ツールを呼ぶ ---
    banner("シナリオ1: シカゴで Grok に入れ子引数つき外部 HTTP ツールを呼ばせる")
    s1 = run_model("grok-4.3")
    print(json.dumps(s1, ensure_ascii=False, indent=2))
    results["scenario_1"] = s1
    if not s1["ok"]:
        failures.append(f"シナリオ1: {s1['why']}")

    # --- シナリオ2: 複数モデルで同一シナリオ → 比較表 ---
    banner("シナリオ2: 同一シナリオを複数モデルで流して比較する")
    compare = [s1]
    for key in ("grok-4.20-non-reasoning", "gemini-2.5-flash", "gemini-2.5-pro",
                "gpt-oss-20b"):
        r = run_model(key)
        print(json.dumps(r, ensure_ascii=False, indent=2), flush=True)
        compare.append(r)
    results["scenario_2"] = compare
    # gemini は吸収層が無ければ 400 になる組。ここが通ることが吸収の証拠
    for r in compare:
        if r["model"].startswith("gemini") and not r["ok"]:
            failures.append(f"シナリオ2({r['model']}): {r['why']}")
    if len([r for r in compare if r["ok"]]) < 2:
        failures.append("シナリオ2: 比較できるモデルが2つに満たない")

    # --- シナリオ3: エージェント不可のモデルは断られる ---
    banner("シナリオ3: エージェント不可のモデルは断られる")
    s3 = {}
    for key in ("grok-4.20-multi-agent", "llama-3.3-70b"):
        reason = agent_refusal(key)
        events = list(stream_agent(key, [{"role": "user", "content": QUESTION}],
                                   auto_tools=True, http_tools=[]))
        refused = len(events) == 1 and "error" in events[0]
        s3[key] = {"refusal_reason": reason, "stream_refused": refused,
                   "events": events}
        print(key, json.dumps(s3[key], ensure_ascii=False), flush=True)
        if not (reason and refused):
            failures.append(f"シナリオ3({key}): 断られていない")
    results["scenario_3"] = s3

    # --- シナリオ4: 回帰(gpt-oss-120b の挙動が変わっていない) ---
    banner("シナリオ4: 回帰 — gpt-oss-120b")
    s4 = run_model("gpt-oss-120b")
    print(json.dumps(s4, ensure_ascii=False, indent=2))
    results["scenario_4"] = s4
    if not s4["ok"]:
        failures.append(f"シナリオ4(回帰): {s4['why']}")

    banner("結果")
    for r in [*compare, s4]:
        print(f"  {r['model']:26} ok={str(r['ok']):5} "
              f"tool_calls={r['tool_calls']} {r['elapsed_s']}s  {r['why'][:60]}")
    results["failures"] = failures
    out = os.environ.get("E2E_OUT", "agt06-e2e.json")
    pathlib.Path(out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n証跡: {out}")
    if failures:
        print("\n失敗:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nすべて通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
