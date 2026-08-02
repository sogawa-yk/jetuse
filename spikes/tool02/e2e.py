"""TOOL-02 の実環境 E2E(tasks/TOOL-02.md の「E2E シナリオ」)。

**実装(`jetuse_core.http_tools` と FastAPI ルート)をそのまま呼ぶ**。検証用の別実装は書かない。
相手(公開 https エンドポイント・Vault・OCI Generative AI)と DB(共有 loop ADB)はすべて実物。

  1. 認証キー / 追跡 ID / 冪等キーの **3 ヘッダを必須とする API** をツール登録し、
     **エージェント経由**で呼んで 200 を得る(相手が受け取ったヘッダに契約規則を当てて判定)
  2. **同一パスへ異なるボディで 2 回**呼び、冪等キーが毎回変わるので **409 にならない**
     (固定注入だったらどうなるかを、同じ規則で対比して示す)
  3. 回帰: 追加項目なし(両列 NULL)の既存ツールが従来どおり呼べ、ヘッダが 1 つも増えない
  4. 否定: 禁止ヘッダ・CR/LF・上限超過が**登録時に拒否**され、**DB を直接書き換えても
     実行時に拒否**される(登録時だけの検証では素通りする経路)

隔離: 共有 loop ADB の run 固有スキーマ(`JETUSE_TOOL02_<乱数>`。ADB は増やさない)。
OCI 側の検証用資源は `jetuse-spike-tool02` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部(`spikes/ragm02/common.py`)を env で接頭辞だけ差し替えて再利用する。

実行(`E=SPIKE_SCHEMA_PREFIX=JETUSE_TOOL02 SPIKE_HOME=/tmp/jetuse-tool02`,
      `P=PYTHONPATH=spikes/ragm02:spikes/tool02:packages/api`):
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成(台帳つき)
  env $E $P .venv/bin/python spikes/tool02/deploy.py         # マイグレーション適用
  env $E $P .venv/bin/python spikes/tool02/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/tool02/teardown.py --yes # Vault 秘密(削除予約)
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
"""

import json
import os
import re
import secrets
import sys

from deploy import use_task_schema

from common import ROOT, banner, require_schema
from fixtures import (
    AUTH_HEADER,
    CLIENT_HEADER,
    CLIENT_VALUE,
    CORRELATION_HEADER,
    CORRELATION_VALUE,
    ECHO_URL,
    IDEMPOTENCY_HEADER,
    ORDER_TOOL,
    PREFIX,
    QUESTION,
    SECRET_NAME,
    contract_verdict,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

_IDS = re.compile(r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.)[a-zA-Z0-9_-]{8,}")
_SECRET_VALUE = ""  # 実行中だけメモリに置く。証跡にもログにも出さない


def write(name: str, text: str) -> None:
    """証跡を書く。OCID・秘密の実値は残さない。"""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    text = _IDS.sub(lambda m: m.group(1) + "…", text)
    if _SECRET_VALUE:
        text = text.replace(_SECRET_VALUE, "<REDACTED>")
    (EVIDENCE / name).write_text(text)
    print(f"  wrote {EVIDENCE / name}")


def fence(text: str) -> str:
    return "```\n" + (text.rstrip() or "(なし)") + "\n```"


# --- 検証用の Vault 秘密(使い捨て) --------------------------------------------

def _vault_args():
    from jetuse_core.oci_auth import sdk_signer_args

    region = os.environ.get("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args["config"] = {**args.get("config", {}), "region": region}
    return args


def ensure_secret() -> str:
    """`jetuse-spike-tool02-apikey`(使い捨てトークン)を用意し OCID を返す。

    実在の資格情報は使わない。所有タグ `jetuse_tool_owner` を登録者に合わせて付ける
    (これが無いと TOOL-01 の認可で弾かれる = 認可が効いていることの裏返し)。
    """
    global _SECRET_VALUE
    import base64

    import oci

    args = _vault_args()
    comp = os.environ["ADB_COMPARTMENT_OCID"]
    vaults = oci.vault.VaultsClient(**args)
    found = [
        s for s in vaults.list_secrets(comp, name=SECRET_NAME).data
        if s.lifecycle_state in ("ACTIVE", "CREATING")
    ]
    if found:
        print(f"  既存の検証用秘密を再利用: {SECRET_NAME}")
        _SECRET_VALUE = ""  # 値は読まない(伏せ字は応答側の実装が行う)
        return found[0].id
    kms = oci.key_management.KmsVaultClient(**args)
    vault = next(v for v in kms.list_vaults(comp).data if v.lifecycle_state == "ACTIVE")
    mgmt = oci.key_management.KmsManagementClient(
        **args, service_endpoint=vault.management_endpoint)
    key = next(k for k in mgmt.list_keys(comp).data if k.lifecycle_state == "ENABLED")
    _SECRET_VALUE = f"tool02-{secrets.token_urlsafe(18)}"
    created = vaults.create_secret(oci.vault.models.CreateSecretDetails(
        compartment_id=comp, vault_id=vault.id, key_id=key.id, secret_name=SECRET_NAME,
        description="TOOL-02 検証用の使い捨てトークン(実在の資格情報ではない)",
        # 所有タグ(TOOL-01 の認可)に加えて **この run の印**を付ける。teardown はこの印が
        # 自分の run のものである秘密しか消さない(他人/別 run の同名を消さない)
        freeform_tags={"jetuse_tool_owner": OWNER, "jetuse_spike_run": SCHEMA},
        secret_content=oci.vault.models.Base64SecretContentDetails(
            content=base64.b64encode(_SECRET_VALUE.encode()).decode()),
    )).data
    print(f"  検証用秘密を作成: {SECRET_NAME}(使い捨て)")
    return created.id


# --- 補助 ---------------------------------------------------------------------

def register(client, expect=200, **body) -> dict:
    res = client.post("/api/agent/http-tools", json=body)
    if res.status_code != expect:
        sys.exit(f"ツール登録の応答が想定外: {res.status_code} {res.text}")
    return res.json()


def execute(client, tool: dict, args: dict):
    return client.post("/api/agent/execute-tool", json={
        "name": tool["name"], "arguments": json.dumps(args), "http_tool_id": tool["id"]})


def echoed(res) -> tuple[dict, str]:
    """代理実行の応答から「相手が実際に受け取ったヘッダとボディ」を取り出す。"""
    out = json.loads(res.json()["output"])
    body = json.loads(out["body"])
    return body.get("headers", {}), json.dumps(body.get("json"), ensure_ascii=False)


def row(tid: str) -> tuple:
    from jetuse_core.db import connect

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT extra_headers, idempotency_header FROM http_tools WHERE id = :i",
            i=tid)
        return cur.fetchone()


def tamper(tid: str, extra_headers: str | None) -> None:
    """登録を通さず **DB を直接書き換える**(後から DB を触られた場合の再現)。"""
    from jetuse_core.db import connect

    with connect() as conn:
        conn.cursor().execute(
            "UPDATE http_tools SET extra_headers = :x WHERE id = :i",
            x=extra_headers, i=tid)
        conn.commit()


# --- シナリオ ------------------------------------------------------------------

def scenario_1(client, ocid: str) -> bool:
    """3 ヘッダ必須の API をエージェント経由で呼び、200 を得る。"""
    banner("シナリオ1: 3ヘッダ必須のAPIをエージェント経由で呼ぶ")
    tool = register(
        client, url=ECHO_URL, auth_header=AUTH_HEADER, auth_secret_ocid=ocid,
        headers={CORRELATION_HEADER: CORRELATION_VALUE, CLIENT_HEADER: CLIENT_VALUE},
        idempotency_header=IDEMPOTENCY_HEADER, **ORDER_TOOL,
    )
    listed = client.get("/api/agent/http-tools").json()["tools"]

    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": QUESTION}],
        "agent": True, "auto_tools": True,
        "enabled_tools": [],          # 組込ツールは一切渡さない
        "http_tool_ids": [tool["id"]],
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    calls = [f["tool_call"] for f in frames if "tool_call" in f]
    results = [f["tool_result"] for f in frames if "tool_result" in f]
    answer = "".join(f.get("delta", "") for f in frames)

    called = any(c.get("name") == ORDER_TOOL["name"] for c in calls)
    # 代理実行の結果は SSE では 500 字の preview に切られる(UI 向け)。相手が受け取った
    # ヘッダ全体を見るために、**同じツール・同じ実行経路**をもう 1 回だけ直接呼ぶ
    preview = results[0]["preview"] if results else ""
    m = re.search(r'"status":\s*(\d+)', preview)
    status = int(m.group(1)) if m else 0
    direct = execute(client, tool, {"part_number": "JX-7742", "quantity": 12})
    received, direct_body = echoed(direct)
    verdict = contract_verdict(received, direct_body, {})
    shown = {k: v for k, v in received.items()
             if k.lower() in {AUTH_HEADER.lower(), CORRELATION_HEADER.lower(),
                              IDEMPOTENCY_HEADER.lower(), CLIENT_HEADER.lower()}}
    ok = called and status == 200 and verdict[0] == 200

    write("scenario-1.md", f"""# シナリオ1 — 3 ヘッダ必須の API をエージェント経由で呼ぶ

**確かめたこと**: 認証キー / 追跡 ID / **冪等キー**の 3 つを必須とする API を JetUse に
ツール登録し、`POST /api/chat/stream` の agent 実行に渡すと、**モデルが自分の判断で呼び**、
相手には 3 ヘッダが揃って届き、**200** が返る(TOOL-01 の設計では必ず 400 になっていた)。

- 相手: 公開 https エンドポイント(受け取ったヘッダをそのまま返すエコー)。**強制の側**は
  tasks/TOOL-02.md に記録した契約規則を `fixtures.contract_verdict` として当てる
  (相手が実際に受け取ったヘッダに対してのみ判定する)。限界は `SKIPPED.md`。
- 認証キーは **Vault 参照**(`auth_secret_ocid`)。固定ヘッダは追跡 ID と
  クライアント版数の 2 つ。冪等キーは**ヘッダ名だけ**登録している。

## 登録(`POST /api/agent/http-tools`)

{fence(json.dumps(tool, ensure_ascii=False, indent=2))}

一覧 `GET /api/agent/http-tools`(**値は返さず名前だけ**返す):

{fence(json.dumps(listed, ensure_ascii=False, indent=2))}

## 質問

{fence(QUESTION)}

## モデルが起こしたツール呼び出し

{fence(json.dumps(calls, ensure_ascii=False, indent=2))}

## 代理実行の結果(モデルへ返した内容・SSE は 500 字の preview)

{fence(preview)}

## 相手が実際に受け取ったヘッダ(該当分)

SSE の preview は切り詰められるため、**同じツールを同じ実行経路
(`POST /api/agent/execute-tool` → `http_tools.call_tool`)で 1 回だけ直接呼び**、
相手が受け取ったヘッダ全体を取得した(HTTP {direct.status_code})。

{fence(json.dumps(shown, ensure_ascii=False, indent=2))}

- 認証キーの値が `<redacted>` なのは、**送った秘密を応答から伏せる**実装のため(TOOL-01)。
  ヘッダ自体は相手に届いている(キーが存在する)。
- 冪等キーは**登録していない値**が入っている = JetUse が呼び出し時に発行した。

## 相手 API の契約規則を当てた判定

HTTP {verdict[0]} {verdict[1]}(代理実行の実 HTTP ステータス: {status})

## 最終回答

{fence(answer)}

判定: **{'PASS' if ok else 'FAIL'}**
(モデルが呼んだ={called} / HTTP={status} / 契約判定={verdict[1]})
""")
    return ok


def scenario_2(client, ocid: str) -> bool:
    """同一パスへ異なるボディで 2 回。冪等キーが毎回変わるので 409 にならない。"""
    banner("シナリオ2: 異なるボディで2回呼んでも409にならない")
    tool = register(
        client, url=ECHO_URL, auth_header=AUTH_HEADER, auth_secret_ocid=ocid,
        headers={CORRELATION_HEADER: CORRELATION_VALUE},
        idempotency_header=IDEMPOTENCY_HEADER,
        **{**ORDER_TOOL, "name": "create_order_twice"},
    )
    seen: dict = {}
    calls = []
    for qty in (12, 30):
        res = execute(client, tool, {"part_number": "JX-7742", "quantity": qty})
        received, body = echoed(res)
        low = {k.lower(): v for k, v in received.items()}
        calls.append({
            "quantity": qty,
            "http_status": res.status_code,
            "idempotency_key": low.get(IDEMPOTENCY_HEADER.lower()),
            "body": body,
            "contract": contract_verdict(received, body, seen),
        })
    keys = [c["idempotency_key"] for c in calls]
    distinct = len(set(keys)) == len(keys) and all(keys)
    ok = distinct and all(c["contract"][0] == 200 for c in calls)

    # 対比: ゲートウェイで固定値を注入していたら同じ規則で 409 になる
    fixed_seen: dict = {}
    fixed = [
        contract_verdict(
            {AUTH_HEADER: "x", CORRELATION_HEADER: CORRELATION_VALUE,
             IDEMPOTENCY_HEADER: "fixed-key"}, c["body"], fixed_seen)
        for c in calls
    ]

    write("scenario-2.md", f"""# シナリオ2 — 同一パスへ異なるボディで 2 回呼んでも 409 にならない

**確かめたこと**: 冪等キーを **JetUse が呼び出しごとに発行**しているので、同じツールを
異なるボディで 2 回呼んでも `409 IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INPUT` に
ならない。これが崩れると「エージェントがエラーから自己修正して呼び直せるか」を
測れなくなる(機構の都合で失敗する)。

## 2 回の呼び出し(`POST /api/agent/execute-tool`)

{fence(json.dumps(calls, ensure_ascii=False, indent=2))}

冪等キーが**毎回異なる**: {distinct}(値: {keys})

## 対比 — 固定値を注入していた場合(同じ契約規則を当てる)

{fence(json.dumps(fixed, ensure_ascii=False, indent=2))}

2 回目が **409** になる = 回避策(ゲートウェイでの固定注入)では成立しないこと。

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_3(client) -> bool:
    """回帰: 追加項目なしの既存ツールが従来どおり呼べ、ヘッダが 1 つも増えない。"""
    banner("シナリオ3: 既存ツール(両列NULL)の挙動が変わらない")
    tool = register(
        client, url=ECHO_URL,
        **{**ORDER_TOOL, "name": "legacy_order", "description": "追加項目なしの既存ツール"},
    )
    stored = row(tool["id"])
    res = execute(client, tool, {"part_number": "JX-7742", "quantity": 1})
    received, _ = echoed(res)
    low = {k.lower() for k in received}
    added = low & {CORRELATION_HEADER.lower(), CLIENT_HEADER.lower(),
                   IDEMPOTENCY_HEADER.lower()}
    ok = (res.status_code == 200 and stored == (None, None) and not added
          and tool["header_names"] == [] and tool["idempotency_header"] is None)

    write("scenario-3.md", f"""# シナリオ3(回帰) — 追加項目なしの既存ツールは挙動が変わらない

**確かめたこと**: 021 で列を足しても、`headers` / `idempotency_header` を指定しない
ツールは **DB で両方 NULL** のままで、送られるヘッダが 1 つも増えない。

## 実 ADB に保存された行

{fence(f"SELECT extra_headers, idempotency_header FROM http_tools WHERE id = '{tool['id']}'"
       f"\n→ {stored}")}

## 登録応答

{fence(json.dumps(tool, ensure_ascii=False, indent=2))}

## 相手が実際に受け取ったヘッダ

{fence(json.dumps(received, ensure_ascii=False, indent=2))}

TOOL-02 が足しうるヘッダのうち届いたもの: {sorted(added) or "(なし)"}

判定: **{'PASS' if ok else 'FAIL'}**(HTTP {res.status_code})
""")
    return ok


def scenario_4(client, ocid: str) -> bool:
    """否定: 登録時に拒否され、DB を直接書き換えても実行時に拒否される。"""
    banner("シナリオ4: 禁止ヘッダ・CR/LF・上限超過を登録時と実行時に拒否")
    from jetuse_core import http_tools

    bad_registrations = []
    for label, body in [
        ("Host の上書き", {"headers": {"Host": "evil.example"}}),
        ("Authorization の持ち込み", {"headers": {"Authorization": "Bearer x"}}),
        ("そのツール自身の認証ヘッダ",
         {"auth_header": AUTH_HEADER, "headers": {AUTH_HEADER: "attacker"}}),
        ("CR/LF 混入", {"headers": {"X-Trace": "a\r\nX-Injected: b"}}),
        ("非 ASCII", {"headers": {"X-Trace": "日本語"}}),
        ("個数上限超過",
         {"headers": {f"X-H{i}": "v"
                      for i in range(http_tools.MAX_EXTRA_HEADERS + 1)}}),
        ("値の長さ上限超過",
         {"headers": {"X-Trace": "x" * (http_tools.MAX_HEADER_VALUE_LENGTH + 1)}}),
        ("冪等キーに禁止ヘッダ", {"idempotency_header": "Cookie"}),
    ]:
        res = client.post("/api/agent/http-tools", json={
            **{**ORDER_TOOL, "name": f"bad_{len(bad_registrations)}"},
            "url": ECHO_URL, **body})
        bad_registrations.append({
            "case": label, "status": res.status_code,
            "detail": res.json().get("detail", "")})

    # 実行時: 登録は正しく通したツールの行を **DB で直接書き換えて**から実行する
    tool = register(
        client, url=ECHO_URL, auth_header=AUTH_HEADER, auth_secret_ocid=ocid,
        headers={CORRELATION_HEADER: CORRELATION_VALUE},
        **{**ORDER_TOOL, "name": "tampered_order"})
    tampered = []
    cases = [(label, json.dumps(payload, ensure_ascii=False)) for label, payload in [
        ("Host の上書き", {"Host": "evil.example"}),
        ("認証ヘッダの上書き", {AUTH_HEADER: "attacker"}),
        ("CR/LF 混入", {"X-Trace": "a\r\nX-Injected: b"}),
        ("個数上限超過",
         {f"X-H{i}": "v" for i in range(http_tools.MAX_EXTRA_HEADERS + 1)}),
    ]]
    # JSON として壊れた値も置く(一覧が 500 にならず、実行だけが止まること)
    cases += [("JSON として壊れた CLOB", "not-json"), ("JSON 配列", '["X-Trace"]')]
    for label, raw in cases:
        tamper(tool["id"], raw)
        listed = client.get("/api/agent/http-tools")
        res = execute(client, tool, {"part_number": "JX-7742", "quantity": 1})
        tampered.append({
            "case": label, "db_value": raw[:120],
            "list_status": listed.status_code,
            "status": res.status_code, "detail": res.json().get("detail", "")})
    tamper(tool["id"], json.dumps({CORRELATION_HEADER: CORRELATION_VALUE}))
    restored = execute(client, tool, {"part_number": "JX-7742", "quantity": 1})

    ok = (all(b["status"] == 400 for b in bad_registrations)
          and all(t["status"] == 400 and "ヘッダ" in t["detail"] for t in tampered)
          and all(t["list_status"] == 200 for t in tampered)   # 壊れた行で一覧が落ちない
          and restored.status_code == 200)

    write("scenario-4.md", f"""# シナリオ4(否定) — 登録時に拒否し、\
DB を直接書き換えても実行時に拒否する

**確かめたこと**: 禁止ヘッダ・CR/LF 混入・上限超過は**登録時**に 400 で拒否される。
さらに、登録を通さず **実 ADB の行を直接 UPDATE** した場合でも、**実行時**に拒否される
(登録時だけの検証では、後から DB を触られた経路が素通りする)。

## 登録時(`POST /api/agent/http-tools`)

{fence(json.dumps(bad_registrations, ensure_ascii=False, indent=2))}

## 実行時(`UPDATE http_tools SET extra_headers = ...` の後に一覧 → 代理実行)

`list_status` は壊れた行がある状態での `GET /api/agent/http-tools`。**壊れた 1 行で
一覧全体が 500 にならず**、その行の実行だけが止まる。

{fence(json.dumps(tampered, ensure_ascii=False, indent=2))}

正常な値へ戻した後の実行: HTTP {restored.status_code}(拒否は「壊れた定義」に対してだけ働く)

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def main() -> None:
    banner(f"TOOL-02 実環境 E2E(スキーマ {SCHEMA})")
    use_task_schema()

    from fastapi.testclient import TestClient
    from jetuse_core.db import connect
    from service.main import app

    with connect() as conn:  # 前回の残りがあると所有者ごとの一覧が混ざる
        cur = conn.cursor()
        cur.execute("DELETE FROM http_tools WHERE owner_sub = :o", o=OWNER)
        conn.commit()

    if not ECHO_URL:
        sys.exit("TOOL02_ECHO_URL が未設定。承認していない宛先へは送らないため中止"
                 "(.env に相手の https エコー先を設定する。雛形は .env.example)。")
    ocid = ensure_secret()
    client = TestClient(app)

    results = {
        "シナリオ1(3ヘッダ必須APIをエージェント経由で200)": scenario_1(client, ocid),
        "シナリオ2(異なるボディで2回でも409にならない)": scenario_2(client, ocid),
        "シナリオ3(回帰: 既存ツールの挙動が変わらない)": scenario_3(client),
        "シナリオ4(否定: 登録時と実行時の両方で拒否)": scenario_4(client, ocid),
    }
    lines = "\n".join(f"- {'PASS' if v else 'FAIL'} — {k}" for k, v in results.items())
    write("summary.md", f"""# TOOL-02 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント。
相手の https エンドポイント・Vault・OCI Generative AI(gpt-oss-120b)・ADB は実物。
マイグレーション 021 の適用結果は `deploy.log`。

{lines}

## 検証で作った資源

- Vault 秘密 `{SECRET_NAME}`(使い捨てトークン・`{PREFIX}` 接頭辞)
- ADB スキーマ `{SCHEMA}`

片付けは `spikes/tool02/teardown.py --yes` と `spikes/ragm02/teardown.py --yes`。
""")
    banner("結果")
    print(lines)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
