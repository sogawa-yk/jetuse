"""TOOL-03 の実環境 E2E(tasks/TOOL-03.md の「E2E シナリオ」)。

**実装(`jetuse_core.http_tools` / `jetuse_core.tools` と FastAPI ルート)をそのまま呼ぶ**。
検証用の別実装は書かない。相手(公開 https エコー)・DB(共有 loop ADB)・
OCI Generative AI はすべて実物。

  1. **1 段の入れ子オブジェクト**を持つツールを登録し、**エージェント経由**で呼んで、
     相手が受け取ったボディが**入れ子のまま**であることを示す
  2. **オブジェクトの配列**(要素 2 個以上)で同じことを示す。さらに **配列の中の配列(2 段)**が
     そのまま届くことを、同じツールの同じ実行経路で示す
  3. 内側の**必須欠落・型違い・未知キー**が**相手へ送る前に**拒否される
     (同じツール・同じ経路で、正しい引数なら相手に届くことと対比する)
  4. 回帰: 平坦なツール(GET)が従来どおり呼べる
  5. 否定(登録時): `properties` の無い object・`items` の無い array・深さ / ノード数の
     上限超過が **400** で拒否される。配列の要素数超過は**実行時**に失敗する(切り詰めない)

隔離: 共有 loop ADB の run 固有スキーマ(`JETUSE_TOOL03_<乱数>`。ADB は増やさない)。
**OCI 側の検証用資源は作らない**(このタスクに認証は要らないので Vault 秘密も作らない)。

実行(`E=SPIKE_SCHEMA_PREFIX=JETUSE_TOOL03 SPIKE_HOME=/tmp/jetuse-tool03`,
      `P=PYTHONPATH=spikes/ragm02:spikes/tool03:packages/api`):
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成(台帳つき)
  env $E $P .venv/bin/python spikes/tool03/deploy.py         # マイグレーション適用
  env $E $P .venv/bin/python spikes/tool03/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
"""

import json
import re
import sys

from deploy import use_task_schema

from common import ROOT, banner, require_schema
from fixtures import (
    CONTRACTOR_QUESTION,
    CONTRACTOR_TOOL,
    ECHO_URL,
    FLAT_TOOL,
    ITEMS_QUESTION,
    ITEMS_TOOL,
    PART_NUMBER,
    echo_url,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

_IDS = re.compile(r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.)[a-zA-Z0-9_-]{8,}")


def write(name: str, text: str) -> None:
    """証跡を書く。OCID は残さない(このタスクは秘密を一切送らない)。"""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(_IDS.sub(lambda m: m.group(1) + "…", text))
    print(f"  wrote {EVIDENCE / name}")


def fence(text: str) -> str:
    return "```\n" + (text.rstrip() or "(なし)") + "\n```"


def js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# --- 補助 ---------------------------------------------------------------------

def register(client, expect=200, **body) -> dict:
    res = client.post("/api/agent/http-tools", json=body)
    if res.status_code != expect:
        sys.exit(f"ツール登録の応答が想定外: {res.status_code} {res.text}")
    return res.json()


def execute(client, tool: dict, args: dict):
    return client.post("/api/agent/execute-tool", json={
        "name": tool["name"], "arguments": json.dumps(args), "http_tool_id": tool["id"]})


def echoed_body(res):
    """代理実行の応答から「相手が実際に受け取った JSON ボディ」を取り出す。"""
    out = json.loads(res.json()["output"])
    return json.loads(out["body"]).get("json")


def echoed_query(res) -> dict:
    """GET のときに相手が受け取ったクエリ文字列。"""
    out = json.loads(res.json()["output"])
    return json.loads(out["body"]).get("args", {})


def agent_run(client, tool: dict, question: str) -> tuple[list, list, str]:
    """組込ツールを一切渡さず、登録した外部ツールだけでエージェントを走らせる。"""
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": question}],
        "agent": True, "auto_tools": True,
        "enabled_tools": [],
        "http_tool_ids": [tool["id"]],
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    calls = [f["tool_call"] for f in frames if "tool_call" in f]
    results = [f["tool_result"] for f in frames if "tool_result" in f]
    answer = "".join(f.get("delta", "") for f in frames)
    return calls, results, answer


def nesting_depth(value) -> int:
    """実際に届いた JSON の入れ子段数(root を 1 段目と数える。スカラの葉は数えない)。"""
    if isinstance(value, dict):
        return 1 + max((nesting_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((nesting_depth(v) for v in value), default=0)
    return 0


# --- シナリオ ------------------------------------------------------------------

def scenario_1(client) -> bool:
    """1 段の入れ子オブジェクトが、エージェント経由で入れ子のまま相手へ届く。"""
    banner("シナリオ1: 入れ子オブジェクトがそのまま相手へ届く")
    tool = register(client, url=ECHO_URL, **CONTRACTOR_TOOL)
    listed = client.get("/api/agent/http-tools").json()["tools"]
    calls, results, answer = agent_run(client, tool, CONTRACTOR_QUESTION)

    called = [c for c in calls if c.get("name") == CONTRACTOR_TOOL["name"]]
    sent = json.loads(called[0]["arguments"]) if called else {}
    # SSE の preview は 500 字で切られる(UI 向け)ので、相手が受け取った本文全体を見るために
    # **モデルが組み立てたその引数**で、同じ実行経路をもう 1 回だけ直接呼ぶ
    direct = execute(client, tool, sent) if called else None
    received = echoed_body(direct) if direct is not None and direct.status_code == 200 else None

    ok = bool(called) and received == sent and isinstance(
        (received or {}).get("contractor"), dict)

    write("scenario-1.md", f"""# シナリオ1 — 入れ子オブジェクトが**その形のまま**相手へ届く

**確かめたこと**: 1 段の入れ子オブジェクト(さらにその中に住所オブジェクト)を持つツールを
登録でき、`POST /api/chat/stream` の agent 実行で**モデルが自分の判断で呼び**、
相手が受け取ったボディが**入れ子のまま**である。TOOL-01 の設計では
このスキーマは**登録の時点で 400** になっていた。

- 相手: 公開 https エンドポイント(受け取った JSON をそのまま返すエコー)。**秘密は送らない**。
- 組込ツールは 1 つも渡していない(`enabled_tools: []`)ので、この操作は登録した外部ツール
  からしか行えない。

## 登録(`POST /api/agent/http-tools`)

{fence(js(tool))}

一覧 `GET /api/agent/http-tools`:

{fence(js(listed))}

## 質問

{fence(CONTRACTOR_QUESTION)}

## モデルが起こしたツール呼び出し(引数)

{fence(js(calls))}

## 代理実行の結果(モデルへ返した内容・SSE は preview に切られる)

{fence(js(results))}

## 相手が実際に受け取ったボディ

同じツールを同じ実行経路(`POST /api/agent/execute-tool` → `http_tools.call_tool`)で
**モデルが組み立てた引数のまま** 1 回だけ直接呼び、相手が受け取った JSON を取得した
(HTTP {direct.status_code if direct is not None else '-'})。

{fence(js(received))}

- 入れ子の段数: **{nesting_depth(received)} 段**(root → contractor → address)
- 送った引数と受け取ったボディが一致: **{received == sent}**

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_2(client) -> bool:
    """オブジェクトの配列(要素 2 個以上)と、配列の中の配列(2 段)が届く。"""
    banner("シナリオ2: オブジェクトの配列と、配列の中の配列(2段)が届く")
    tool = register(client, url=ECHO_URL, **ITEMS_TOOL)
    calls, results, answer = agent_run(client, tool, ITEMS_QUESTION)

    called = [c for c in calls if c.get("name") == ITEMS_TOOL["name"]]
    sent = json.loads(called[0]["arguments"]) if called else {}
    direct = execute(client, tool, sent) if called else None
    received = echoed_body(direct) if direct is not None and direct.status_code == 200 else None
    items = (received or {}).get("items") or []
    multi = isinstance(items, list) and len(items) >= 2

    # 2 段(配列の中の配列)は、モデルの出力に依存させず**こちらが組み立てた引数**でも確かめる
    two_level = {"order_id": "ORD-1002", "items": [
        {"sku": PART_NUMBER, "qty": 3, "options": [
            {"code": "COLOR", "value": "赤"}, {"code": "SIZE", "value": "L"}]},
        {"sku": "KM-1180", "qty": 1, "options": [{"code": "GIFT", "value": "包装"}]},
    ]}
    fixed = execute(client, tool, two_level)
    fixed_received = echoed_body(fixed) if fixed.status_code == 200 else None

    ok = (bool(called) and received == sent and multi
          and fixed_received == two_level and nesting_depth(fixed_received) >= 4)

    write("scenario-2.md", f"""# シナリオ2 — オブジェクトの配列と、**配列の中の配列(2 段)**が届く

**確かめたこと**: 明細の配列(要素 2 個以上)を持つツールが登録でき、エージェント経由で
呼ばれ、**要素が複数のまま**相手へ届く。さらに各明細の中の**オプションの配列**——
実案件の「サービス情報設定」に相当する**配列の中の配列**——も形のまま届く。

## 登録(`POST /api/agent/http-tools`)

{fence(js(tool))}

## 質問

{fence(ITEMS_QUESTION)}

## モデルが起こしたツール呼び出し(引数)

{fence(js(calls))}

## 代理実行の結果(モデルへ返した内容)

{fence(js(results))}

## 相手が実際に受け取ったボディ(モデルが組み立てた引数)

{fence(js(received))}

- 配列の要素数: **{len(items)}**(2 個以上: {multi})
- 送った引数と受け取ったボディが一致: **{received == sent}**

## 配列の中の配列(2 段)— こちらが組み立てた引数

モデルの出力に依存させないため、**2 段の入れ子を必ず含む引数**で同じツールを直接呼んだ
(HTTP {fixed.status_code})。

{fence(js(fixed_received))}

- 入れ子の段数: **{nesting_depth(fixed_received)} 段**
  (root → items 配列 → 明細オブジェクト → options 配列 → オプションオブジェクト)
- 送った引数と受け取ったボディが一致: **{fixed_received == two_level}**

## 最終回答

{fence(answer)}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_3(client) -> bool:
    """内側の必須欠落・型違い・未知キーが、相手へ送る前に拒否される。"""
    banner("シナリオ3: 内側の違反は相手へ送る前に拒否される")
    tool = register(client, url=ECHO_URL, **{**ITEMS_TOOL, "name": "set_items_strict"})

    good = {"order_id": "ORD-1003", "items": [{"sku": PART_NUMBER, "qty": 1}]}
    baseline = execute(client, tool, good)
    baseline_received = echoed_body(baseline) if baseline.status_code == 200 else None

    cases = [
        ("配列要素の必須欠落(sku なし)",
         {"order_id": "ORD-1003", "items": [{"qty": 1}]}),
        ("配列要素の型違い(qty が文字列)",
         {"order_id": "ORD-1003", "items": [{"sku": PART_NUMBER, "qty": "3"}]}),
        ("配列要素の未知キー",
         {"order_id": "ORD-1003", "items": [{"sku": PART_NUMBER, "qty": 1, "x": 1}]}),
        ("2 段目(options)の必須欠落",
         {"order_id": "ORD-1003", "items": [
             {"sku": PART_NUMBER, "qty": 1, "options": [{"value": "赤"}]}]}),
        ("2 段目の未知キー",
         {"order_id": "ORD-1003", "items": [
             {"sku": PART_NUMBER, "qty": 1, "options": [{"code": "C", "z": 1}]}]}),
        ("配列であるべき所にオブジェクト",
         {"order_id": "ORD-1003", "items": {"sku": PART_NUMBER, "qty": 1}}),
        ("オブジェクトであるべき所にスカラ",
         {"order_id": "ORD-1003", "items": [PART_NUMBER]}),
    ]
    rejected = []
    for label, args in cases:
        res = execute(client, tool, args)
        detail = res.json().get("detail", "")
        rejected.append({
            "case": label, "status": res.status_code, "detail": detail,
            # 相手へ出ていれば「ツール実行に失敗しました(HTTP ...)」等になる。
            # 引数検証の文言で返っている = **送る前に**止まった
            "stopped_before_sending": res.status_code == 400
            and "ツール実行に失敗しました" not in detail,
        })

    ok = (baseline_received == good
          and all(r["status"] == 400 and r["stopped_before_sending"] for r in rejected))

    write("scenario-3.md", f"""# シナリオ3 — 内側の違反は**相手へ送る前に**拒否される

**確かめたこと**: 入れ子の**内側**(配列要素・その中の配列要素)の未知キー・型違い・
必須欠落が、相手へ HTTP を出す前に拒否される。宣言できる形を広げても、**検証の強さは
各階層で同じ**であることの確認。

## 対比: 正しい引数なら相手に届く(同じツール・同じ経路)

HTTP {baseline.status_code} — 相手が受け取ったボディ:

{fence(js(baseline_received))}

## 内側の違反(`POST /api/agent/execute-tool`)

`stopped_before_sending` は「応答が引数検証の文言で、代理実行(`ツール実行に失敗しました`)
の文言ではない」= JetUse が HTTP を出す前に止めた、の意。

{fence(js(rejected))}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_4(client) -> bool:
    """回帰: 平坦なツール(GET)が従来どおり呼べる。"""
    banner("シナリオ4(回帰): 平坦なツールが従来どおり呼べる")
    tool = register(client, url=echo_url("/get"), **FLAT_TOOL)
    res = execute(client, tool, {"part_number": PART_NUMBER})
    query = echoed_query(res) if res.status_code == 200 else {}
    stored = json.dumps(tool["parameters"], ensure_ascii=False, sort_keys=True)
    original = json.dumps(FLAT_TOOL["parameters"], ensure_ascii=False, sort_keys=True)

    # 平坦なツールに入れ子を渡しても通らない(型検査は従来どおり)
    bad = execute(client, tool, {"part_number": {"a": 1}})

    ok = (res.status_code == 200 and query.get("part_number") == PART_NUMBER
          and stored == original and bad.status_code == 400)

    write("scenario-4.md", f"""# シナリオ4(回帰) — 平坦なツールの挙動が変わらない

**確かめたこと**: TOOL-01 から形を変えていない平坦なスカラーだけのツール(GET)が、
入れ子対応の後も**まったく同じように**登録でき、同じようにクエリ文字列で相手へ届く。

## 登録した引数スキーマ(保存後)

{fence(js(tool["parameters"]))}

- 登録前後で**バイト等価**: **{stored == original}**

## 実行(GET・クエリ文字列)

HTTP {res.status_code} — 相手が受け取ったクエリ:

{fence(js(query))}

## 平坦なツールに入れ子を渡した場合

HTTP {bad.status_code} — {bad.json().get("detail", "")}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_5(client) -> bool:
    """否定: 検証しきれない形と上限超過を拒否する(黙って切り詰めない)。"""
    banner("シナリオ5(否定): 検証しきれない形と上限超過を拒否する")
    from jetuse_core import http_tools, tools

    def obj(props, **rest):
        return {"type": "object", "properties": props, **rest}

    def deep(levels: int) -> dict:
        node = obj({"leaf": {"type": "string"}})
        for _ in range(levels - 1):
            node = obj({"n": node})
        return node

    bad_schemas = [
        ("properties の無い object", obj({"a": {"type": "object"}})),
        ("items の無い array", obj({"a": {"type": "array"}})),
        ("タプル形式の items",
         obj({"a": {"type": "array", "items": [{"type": "string"}]}})),
        (f"深さ上限超過({http_tools.MAX_SCHEMA_DEPTH + 1} 段)",
         deep(http_tools.MAX_SCHEMA_DEPTH + 1)),
        (f"ノード数上限超過(> {http_tools.MAX_SCHEMA_NODES})",
         obj({f"o{i}": obj({f"p{j}": {"type": "string"}
                            for j in range(http_tools.MAX_PROPERTIES)})
              for i in range(http_tools.MAX_PROPERTIES)})),
        ("各階層の引数個数上限超過",
         obj({"a": obj({f"p{j}": {"type": "string"}
                        for j in range(http_tools.MAX_PROPERTIES + 1)})})),
        ("GET ツールに入れ子", None),  # 下で method を変えて登録する
    ]
    rejections = []
    for i, (label, schema) in enumerate(bad_schemas):
        body = {"name": f"bad_tool_{i}", "description": "検証用", "url": ECHO_URL,
                "method": "POST",
                "parameters": schema if schema is not None else ITEMS_TOOL["parameters"]}
        if schema is None:
            body["method"] = "GET"
            body["url"] = echo_url("/get")
        res = client.post("/api/agent/http-tools", json=body)
        rejections.append({"case": label, "status": res.status_code,
                           "detail": res.json().get("detail", "")})

    # 上限ちょうどは通る(締めすぎていないことの確認)
    at_limit = client.post("/api/agent/http-tools", json={
        "name": "at_depth_limit", "description": "上限ちょうど", "url": ECHO_URL,
        "method": "POST", "parameters": deep(http_tools.MAX_SCHEMA_DEPTH)})

    # 配列の要素数は**実行時**に効く(宣言時には何件来るか分からない)
    tool = register(client, url=ECHO_URL, **{**ITEMS_TOOL, "name": "set_items_bulk"})
    over = {"order_id": "ORD-1004", "items": [
        {"sku": f"S-{i}", "qty": 1} for i in range(tools.MAX_ARRAY_ITEMS + 1)]}
    over_res = execute(client, tool, over)
    just = {"order_id": "ORD-1004", "items": [
        {"sku": f"S-{i}", "qty": 1} for i in range(tools.MAX_ARRAY_ITEMS)]}
    just_res = execute(client, tool, just)
    just_received = echoed_body(just_res) if just_res.status_code == 200 else None
    truncated = (isinstance(just_received, dict)
                 and len(just_received.get("items") or []) != tools.MAX_ARRAY_ITEMS)

    ok = (all(r["status"] == 400 for r in rejections)
          and at_limit.status_code == 200
          and over_res.status_code == 400
          and just_res.status_code == 200 and not truncated)

    write("scenario-5.md", f"""# シナリオ5(否定) — 検証しきれない形と上限超過を拒否する

**確かめたこと**: 表現力を広げても「検証しきれる形しか通さない」方針は変えていない。
上限は `MAX_TOOLS_PER_AGENT` と同じ流儀の定数で、**超過は黙って切り詰めず拒否**する。

上限値: 深さ **{http_tools.MAX_SCHEMA_DEPTH}** 段 / ノード数 **{http_tools.MAX_SCHEMA_NODES}** /
各階層の引数 **{http_tools.MAX_PROPERTIES}** 個 / 配列の要素 **{tools.MAX_ARRAY_ITEMS}** 件
(根拠と判断待ちの論点は `docs/decisions/ADR-0024-http-tool-nested-parameters.md` §3)。

## 登録時に 400 で拒否されるもの(`POST /api/agent/http-tools`)

{fence(js(rejections))}

## 上限ちょうどは通る(締めすぎていない)

深さ {http_tools.MAX_SCHEMA_DEPTH} 段の登録: HTTP {at_limit.status_code}

## 配列の要素数は**実行時**に効く(切り詰めない)

- {tools.MAX_ARRAY_ITEMS + 1} 件: HTTP {over_res.status_code} — {over_res.json().get("detail", "")}
- {tools.MAX_ARRAY_ITEMS} 件(上限ちょうど): HTTP {just_res.status_code} /
  相手が受け取った要素数 **{len((just_received or {}).get("items") or [])}**
  (切り詰められた: {truncated})

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def main() -> None:
    banner(f"TOOL-03 実環境 E2E(スキーマ {SCHEMA})")
    use_task_schema()

    from fastapi.testclient import TestClient
    from jetuse_core.db import connect
    from service.main import app

    with connect() as conn:  # 前回の残りがあると所有者ごとの一覧が混ざる
        cur = conn.cursor()
        cur.execute("DELETE FROM http_tools WHERE owner_sub = :o", o=OWNER)
        conn.commit()

    if not ECHO_URL:
        sys.exit("TOOL03_ECHO_URL が未設定。承認していない宛先へは送らないため中止"
                 "(.env に相手の https エコー先を設定する。雛形は .env.example)。")
    client = TestClient(app)

    results = {
        "シナリオ1(入れ子オブジェクトがそのまま届く)": scenario_1(client),
        "シナリオ2(オブジェクトの配列・配列の中の配列)": scenario_2(client),
        "シナリオ3(内側の違反を送る前に拒否)": scenario_3(client),
        "シナリオ4(回帰: 平坦なツール)": scenario_4(client),
        "シナリオ5(否定: 検証できない形と上限超過)": scenario_5(client),
    }
    lines = "\n".join(f"- {'PASS' if v else 'FAIL'} — {k}" for k, v in results.items())
    write("summary.md", f"""# TOOL-03 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント。
相手の https エンドポイント・OCI Generative AI(gpt-oss-120b)・ADB は実物。
マイグレーションの適用結果は `deploy.log`(TOOL-03 は DB スキーマを変えていない)。

{lines}

## 検証で作った資源

- ADB スキーマ `{SCHEMA}`(共有 loop ADB の中。ADB は増やしていない)
- **OCI 側の検証用資源は作っていない**(このタスクに認証は要らないので Vault 秘密も作らない)

片付けは `spikes/ragm02/teardown.py --yes`。
""")
    banner("結果")
    print(lines)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
