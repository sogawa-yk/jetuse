"""TOOL-01 の実環境 E2E(tasks/TOOL-01.md の「E2E シナリオ」)。

**実装(`jetuse_core.http_tools` と FastAPI ルート)をそのまま呼ぶ**。検証用の別実装は書かない。
相手(架空の業務 API・Vault・OCI Generative AI)はすべて実物。

  1. 架空の業務 API(Object Storage の PAR)をツール登録し、エージェントに
     「それを使わないと答えられない質問」をして、**モデルが自分で呼び**答えに反映されること
  2. 秘密ヘッダを要求する API へ **Vault 経由の秘密**が渡ること(証跡に平文を残さない)。
     併せて、**登録者に許可されていない秘密の OCID は登録できない**こと
  3. 否定: 内部メタデータ / ループバック / 私有レンジ / http の URL 登録が**拒否される**
  4. タイムアウトが「黙って切り詰め」ではなく**失敗**としてモデルへ伝わること
  5. 応答サイズ上限超過が同様に**失敗**として伝わること
  6. 圧縮爆弾(小さく送って大きく展開)でも上限判定をすり抜けられないこと

隔離: 共有 loop ADB の run 固有スキーマ(`JETUSE_TOOL01_<乱数>`。ADB は増やさない)。
OCI 側の検証用資源は `jetuse-spike-tool01` 接頭辞。所有台帳・ウォレット・接続ガードは
RAGM-02 の検証共通部(`spikes/ragm02/common.py`)を env で接頭辞だけ差し替えて再利用する。

実行(`E=SPIKE_SCHEMA_PREFIX=JETUSE_TOOL01 SPIKE_HOME=/tmp/jetuse-tool01`,
      `P=PYTHONPATH=spikes/ragm02:spikes/tool01:packages/api`):
  env $E $P .venv/bin/python spikes/ragm02/setup_schema.py   # スキーマ作成(台帳つき)
  env $E $P .venv/bin/python spikes/tool01/deploy.py         # マイグレーション適用
  env $E $P .venv/bin/python spikes/tool01/e2e.py
片付け:
  env $E $P .venv/bin/python spikes/tool01/teardown.py --yes # OCI 側(バケット・PAR)
  env $E $P .venv/bin/python spikes/ragm02/teardown.py --yes # ADB スキーマ
"""

import datetime
import hashlib
import json
import os
import re
import sys

from deploy import use_task_schema

from common import ROOT, banner, require_schema
from fixtures import (
    BIG_OBJECT_BYTES,
    BOMB_PLAIN_BYTES,
    PREFIX,
    QUESTION,
    STOCK_JSON,
    STOCK_TOOL,
    WAREHOUSE,
)

SCHEMA = require_schema()
OWNER = "dev-user"  # 認証無効時の AuthContext.subject
SECRET_NAME = f"{PREFIX}-apikey"
ECHO_URL = "https://postman-echo.com/get"  # ヘッダをそのまま返す公開エンドポイント
DELAY_URL = "https://postman-echo.com/delay/5"

EVIDENCE = ROOT / "runs" / (ROOT / ".current_run_id").read_text().strip() / "e2e"

_IDS = re.compile(
    r"(ocid1\.[a-z0-9]+\.[a-z0-9-]*\.[a-z0-9-]*\.)[a-zA-Z0-9_-]{8,}"
)
_PAR = re.compile(r"(/p/)[A-Za-z0-9_=\-]{8,}(/)")

_SECRET_VALUE = ""  # 実行中だけメモリに置く。証跡にもログにも出さない


def write(name: str, text: str) -> None:
    """証跡を書く。OCID・PAR トークン・秘密の実値は残さない。"""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    text = _IDS.sub(lambda m: m.group(1) + "…", text)
    text = _PAR.sub(lambda m: m.group(1) + "…" + m.group(2), text)
    if _SECRET_VALUE:
        text = text.replace(_SECRET_VALUE, "<REDACTED>")
    (EVIDENCE / name).write_text(text)
    print(f"  wrote {EVIDENCE / name}")


def fence(text: str) -> str:
    return "```\n" + (text.rstrip() or "(なし)") + "\n```"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- 架空の業務 API(Object Storage + PAR) ------------------------------------

def _os_client():
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    region = os.environ.get("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args["config"] = {**args.get("config", {}), "region": region}
    return oci.object_storage.ObjectStorageClient(**args)


def ensure_fake_api() -> dict[str, str]:
    """架空の業務 API を実在の https エンドポイントとして立てる(PAR)。

    「JetUse が外部の HTTP を代理実行する」ことの検証なので、相手は本物の公開 https で
    なければならない(SSRF ガードがループバックを弾くため、ローカルの偽サーバは使えない)。
    """
    import oci

    client = _os_client()
    ns = client.get_namespace().data
    bucket = f"{PREFIX}-{SCHEMA.split('_')[-1].lower()}"
    comp = os.environ["ADB_COMPARTMENT_OCID"]
    try:
        client.get_bucket(ns, bucket)
        print(f"  既存バケットを再利用: {bucket}")
    except oci.exceptions.ServiceError as e:
        if e.status != 404:
            raise
        client.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(
            name=bucket, compartment_id=comp, public_access_type="NoPublicAccess"))
        print(f"  バケット作成: {bucket}")
    client.put_object(ns, bucket, "stock.json", STOCK_JSON)
    client.put_object(ns, bucket, "big.json", b"x" * BIG_OBJECT_BYTES)
    # 圧縮爆弾: 実 Object Storage に Content-Encoding: gzip で置く。相手が返すバイト数は
    # 小さいが、展開すると上限をはるかに超える
    import gzip as _gzip
    client.put_object(ns, bucket, "bomb.json", _gzip.compress(b"x" * BOMB_PLAIN_BYTES),
                      content_encoding="gzip", content_type="application/json")

    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=6)
    urls = {}
    for obj in ("stock.json", "big.json", "bomb.json"):
        par = client.create_preauthenticated_request(
            ns, bucket, oci.object_storage.models.CreatePreauthenticatedRequestDetails(
                name=f"{PREFIX}-{obj}", object_name=obj,
                access_type="ObjectRead", time_expires=expires,
            )).data
        urls[obj] = f"https://objectstorage.{os.environ.get('OCI_REGION', 'ap-osaka-1')}" \
                    f".oraclecloud.com{par.access_uri}"
    print(f"  架空業務 API: {bucket}/stock.json（PAR・{expires:%Y-%m-%d %H:%M} まで）")
    return {"bucket": bucket, "namespace": ns, **urls}


def secret_ocid() -> str:
    """検証用に人が作った Vault 秘密(`jetuse-spike-tool01-apikey`)の OCID を引く。"""
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    region = os.environ.get("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args["config"] = {**args.get("config", {}), "region": region}
    vaults = oci.vault.VaultsClient(**args)
    found = [
        s for s in vaults.list_secrets(
            os.environ["ADB_COMPARTMENT_OCID"], name=SECRET_NAME).data
        if s.lifecycle_state == "ACTIVE"
    ]
    if not found:
        sys.exit(f"Vault 秘密 {SECRET_NAME} が無い。中止(平文の代替は使わない)。")
    return found[0].id


def other_secret_ocid() -> str:
    """**この検証用ではない**既存 Secret の OCID(否定シナリオの相手)。値は読まない。"""
    import oci
    from jetuse_core.oci_auth import sdk_signer_args

    region = os.environ.get("OCI_REGION", "ap-osaka-1")
    args = sdk_signer_args(region)
    args["config"] = {**args.get("config", {}), "region": region}
    vaults = oci.vault.VaultsClient(**args)
    others = [
        s for s in vaults.list_secrets(os.environ["ADB_COMPARTMENT_OCID"]).data
        if s.lifecycle_state == "ACTIVE" and s.secret_name != SECRET_NAME
    ]
    if not others:
        sys.exit("否定シナリオに使える別 Secret が無い。中止。")
    return others[0].id


# --- シナリオ ------------------------------------------------------------------

def register(client, **body) -> dict:
    res = client.post("/api/agent/http-tools", json=body)
    if res.status_code != 200:
        sys.exit(f"ツール登録に失敗: {res.status_code} {res.text}")
    return res.json()


def scenario_1(client, urls: dict) -> bool:
    """架空の業務 API をツール登録 → エージェントが自分で呼び、答えに反映される。"""
    banner("シナリオ1: 外部HTTPツールをエージェントが自分で呼ぶ")
    tool = register(client, url=urls["stock.json"], **STOCK_TOOL)
    listed = client.get("/api/agent/http-tools").json()["tools"]

    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": QUESTION}],
        "agent": True, "auto_tools": True,
        "enabled_tools": [],          # 組込ツールは一切渡さない(外部ツールだけで答えさせる)
        "http_tool_ids": [tool["id"]],
    })
    frames = [json.loads(ln[6:]) for ln in res.text.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() not in ("[DONE]", "")]
    answer = "".join(f.get("delta", "") for f in frames)
    calls = [f["tool_call"] for f in frames if "tool_call" in f]
    results = [f["tool_result"] for f in frames if "tool_result" in f]

    called = any(c.get("name") == STOCK_TOOL["name"] for c in calls)
    reflected = "137" in answer and WAREHOUSE in answer
    ok = called and reflected and not any("error" in f for f in frames)

    write("scenario-1.md", f"""# シナリオ1 — 外部 HTTP ツールをエージェントが自分で呼ぶ

**確かめたこと**: 素の HTTP エンドポイント(架空の在庫 API)を JSON Schema つきで登録し、
`POST /api/chat/stream` の agent 実行に `http_tool_ids` で渡すと、**モデルが自分の判断で
それを呼び**、JetUse がサーバー側で代理実行した結果が最終回答に反映される。

- 相手: Object Storage の PAR(実在の https エンドポイント。JetUse 側に業務ロジックは無い)
- 組込ツールは 1 つも渡していない(`enabled_tools: []`)ので、この答えは登録した外部ツール
  からしか得られない。

## 登録

{fence(json.dumps(tool, ensure_ascii=False, indent=2))}

一覧 `GET /api/agent/http-tools`:

{fence(json.dumps(listed, ensure_ascii=False, indent=2))}

## 質問

{fence(QUESTION)}

## モデルが起こしたツール呼び出し

{fence(json.dumps(calls, ensure_ascii=False, indent=2))}

## 代理実行の結果(モデルへ返した内容)

{fence(json.dumps(results, ensure_ascii=False, indent=2))}

## 最終回答

{fence(answer)}

- ツールが呼ばれた: **{called}**
- 在庫数 137 と保管倉庫 {WAREHOUSE} が回答に載った: **{reflected}**

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_2(client, ocid: str) -> bool:
    """Vault の秘密がヘッダとして届く。証跡には平文を残さない(SHA-256 で示す)。"""
    global _SECRET_VALUE
    banner("シナリオ2: Vault の秘密が認証ヘッダとして届く")
    from jetuse_core import http_tools

    _SECRET_VALUE = http_tools._read_secret(ocid)  # 期待値の突き合わせ用(出力しない)

    with_auth = register(
        client, name="echo_with_secret", description="ヘッダをそのまま返す検証用API",
        parameters={"type": "object", "properties": {}}, url=ECHO_URL,
        method="GET", auth_header="X-Api-Key", auth_secret_ocid=ocid,
    )
    without = register(
        client, name="echo_without_secret", description="同じAPIを認証なしで登録したもの",
        parameters={"type": "object", "properties": {}}, url=ECHO_URL, method="GET",
    )
    # 否定: 登録者に許可されていない(タグの無い)秘密は登録できない — レビュー TOOL-01-001。
    # 相手はアプリ運用用の既存 Secret。これが通ると「サービスの権限で読める秘密を、
    # 利用者が指定した外部 URL へ送らせる」経路になる
    foreign = other_secret_ocid()
    denied = client.post("/api/agent/http-tools", json={
        "name": "steal_other_secret", "description": "他人の秘密を使おうとする",
        "parameters": {"type": "object", "properties": {}}, "url": ECHO_URL,
        "method": "GET", "auth_header": "X-Api-Key", "auth_secret_ocid": foreign,
    })
    denied_ok = denied.status_code == 400

    # 承認後の実行は **承認イベントが返した id** で名指しする(名前だけの再解決は 400)
    got = client.post("/api/agent/execute-tool", json={
        "name": "echo_with_secret", "arguments": "{}",
        "http_tool_id": with_auth["id"]})
    plain = client.post("/api/agent/execute-tool", json={
        "name": "echo_without_secret", "arguments": "{}",
        "http_tool_id": without["id"]})
    by_name_only = client.post("/api/agent/execute-tool",
                               json={"name": "echo_with_secret", "arguments": "{}"})
    out_with = got.json()["output"]
    out_plain = plain.json()["output"]
    echoed = json.loads(json.loads(out_with)["body"])["headers"]
    echoed_plain = json.loads(json.loads(out_plain)["body"])["headers"]

    # 相手は受け取ったヘッダをそのまま返すが、JetUse が**送った値だけ**を伏せ字にする。
    # 伏せ字が x-api-key の位置に現れる = その値が Vault の秘密と完全一致していた証拠であり、
    # 同時に「秘密が呼び出し元/モデルへ出ない」ことの証拠でもある(レビュー TOOL-01-001)
    delivered = echoed.get("x-api-key") == http_tools.REDACTED
    leaked = _SECRET_VALUE in out_with
    absent = "x-api-key" not in echoed_plain
    # DB と API 応答に平文が現れないこと
    from jetuse_core.db import connect
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, auth_header, auth_secret_ocid FROM http_tools "
            "WHERE owner_sub = :o ORDER BY name", o=OWNER)
        rows = cur.fetchall()
    row_text = json.dumps(rows, ensure_ascii=False)
    api_text = json.dumps(client.get("/api/agent/http-tools").json(), ensure_ascii=False)
    not_in_db = _SECRET_VALUE not in row_text
    not_in_api = _SECRET_VALUE not in api_text
    id_required = by_name_only.status_code == 400
    ok = (delivered and absent and not_in_db and not_in_api and denied_ok
          and not leaked and id_required)

    write("scenario-2.md", f"""# シナリオ2 — Vault 経由の秘密が認証ヘッダとして届く

**確かめたこと**: 登録時には秘密そのものを渡さず **Vault の OCID だけ**を渡す。代理実行の
瞬間に JetUse が Vault から読み、指定ヘッダに載せて送る。DB にも API 応答にも平文は現れない。

- 秘密: Vault `{SECRET_NAME}`(OCID 参照。値はランダム文字列。この証跡に実値は書かない)
- 相手: `{ECHO_URL}`(**リクエストヘッダをそのまま返す**公開エンドポイント)

## 登録(認証あり / なしの対照)

{fence(json.dumps([with_auth, without], ensure_ascii=False, indent=2))}

> 応答に `auth_secret_ocid` は出ない(`has_auth` だけ)。`mcp_servers` と同じ流儀。

## 否定: 許可されていない秘密は登録できない

登録者に紐づいていない(freeform タグ `jetuse_tool_owner` が一致しない)既存 Secret の OCID を
指定して登録を試みた。これが通ると、サービスの OCI 権限で読める秘密を利用者が指定した外部 URL
へ送らせられる(confused deputy)。

- 対象: このコンパートメントにある**アプリ運用用の別 Secret**(値は一切読んでいない)
- 結果: HTTP {denied.status_code}

{fence(json.dumps(denied.json(), ensure_ascii=False, indent=2))}

- 拒否された: **{denied_ok}**

## 相手が受け取ったヘッダ(= 呼び出し元へ返った本文)

相手はヘッダをそのまま返すが、JetUse は**送った秘密の実値だけ**を `{http_tools.REDACTED}` に
置き換えてから返す。伏せ字が `x-api-key` の位置に現れることは、

1. その値が Vault の秘密と**完全一致していた**(= 正しく届いた)
2. かつ**呼び出し元・モデル・会話履歴に平文が出ない**

の両方を同時に示す。

認証ありツールの応答本文:

{fence(json.dumps(echoed, ensure_ascii=False, indent=2))}

認証なしツールの応答本文:

{fence(json.dumps(echoed_plain, ensure_ascii=False, indent=2))}

- 秘密が届いた(伏せ字が一致位置に出た): **{delivered}**
- 応答本文に平文が出ていない: **{not leaked}**
- 認証なしでは付かない: **{absent}**

## DB の中身(所有者のツール行)

{fence(row_text)}

- DB に平文の秘密が無い: **{not_in_db}**(保持しているのは Vault の OCID だけ)
- API 応答に平文の秘密が無い: **{not_in_api}**

## 承認後の実行は id で名指しする

名前だけで再解決すると、承認待ちの間に同名で別 URL・別 Secret のツールへ差し替えられる。
`http_tool_id` を付けずに実行した結果: HTTP {by_name_only.status_code}(id 必須: **{id_required}**)

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


DANGEROUS = [
    ("インスタンスメタデータ", "https://169.254.169.254/opc/v2/instance/"),
    ("メタデータ(旧IP形式)", "https://169.254.169.254/latest/meta-data/"),
    ("ループバック", "https://127.0.0.1:8000/internal"),
    ("localhost 名前解決", "https://localhost/internal"),
    ("私有レンジ", "https://10.0.0.10/admin"),
    ("平文 http", "http://example.com/api"),
    ("URL に認証情報", "https://user:pass@example.com/api"),
]


def scenario_3(client) -> bool:
    """否定: 危険な URL の登録が拒否される(fail-closed)。"""
    banner("シナリオ3(否定): 内部を向く URL の登録が拒否される")
    rows = []
    ok = True
    for i, (label, url) in enumerate(DANGEROUS):
        res = client.post("/api/agent/http-tools", json={
            "name": f"blocked_probe_{i}", "description": "拒否されるはず",
            "parameters": {"type": "object", "properties": {}}, "url": url,
        })
        rejected = res.status_code == 400
        ok = ok and rejected
        detail = res.json().get("detail", "") if res.status_code == 400 else res.text
        rows.append(f"| {label} | `{url}` | {res.status_code} | {detail} |")
    listed = [t["name"] for t in client.get("/api/agent/http-tools").json()["tools"]]
    none_stored = not any(n.startswith("blocked_probe_") for n in listed)
    ok = ok and none_stored

    write("scenario-3.md", f"""# シナリオ3(否定) — 内部を向く URL の登録が拒否される

**確かめたこと**: この機能は SSRF の入口になりうる。登録の時点で https 以外・内部メタデータ・
ループバック・私有レンジ・URL 埋め込みの認証情報を **400 で拒否**し、1 件も保存しない。
判定は `mcp_servers.validate_url` と同じ経路(`jetuse_shared.webtools.assert_public_host`)で、
名前解決の結果が私有/ループバック/リンクローカル/予約/マルチキャストなら弾く(fail-closed)。

| 種別 | URL | HTTP | 応答 |
|---|---|---|---|
{chr(10).join(rows)}

登録後のツール一覧: {listed or '(なし)'}
- 1 件も保存されていない: **{none_stored}**

> 実行時にも同じ検証を通す(登録後に DNS が内部へ向いても止まる)。
> 単体テスト `test_execution_revalidates_host` で固定。

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_4(client) -> bool:
    """タイムアウトが「黙って切り詰め」ではなく失敗として伝わる。"""
    banner("シナリオ4: タイムアウトが失敗として伝わる")
    from jetuse_core import http_tools

    tool = register(
        client, name="slow_api", description="遅い検証用API",
        parameters={"type": "object", "properties": {}}, url=DELAY_URL, method="GET",
    )
    original = http_tools.TIMEOUT_SECONDS
    http_tools.TIMEOUT_SECONDS = 2.0  # 相手は 5 秒待つ実 API。上限だけ縮めて実測する
    try:
        res = client.post("/api/agent/execute-tool", json={
            "name": "slow_api", "arguments": "{}", "http_tool_id": tool["id"]})
    finally:
        http_tools.TIMEOUT_SECONDS = original
    detail = res.json().get("detail", "")
    ok = res.status_code == 400 and "タイムアウト" in detail

    write("scenario-4.md", f"""# シナリオ4 — タイムアウトが失敗として伝わる(切り詰めない)

**確かめたこと**: 相手が返ってこないとき、部分的な結果を黙って返さず「ツール実行に失敗した」
としてモデル/呼び出し元へ伝える。リトライもしない(業務 API の二重実行を避ける)。

- 相手: `{DELAY_URL}`(5 秒待って返す実 API)
- 上限: 既定 {original} 秒 → この検証だけ 2.0 秒に縮めて実測

登録: `{tool['name']}` / `{tool['method']}`

応答 `POST /api/agent/execute-tool`: HTTP {res.status_code}

{fence(detail)}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_5(client, urls: dict) -> bool:
    """応答サイズ上限超過が失敗として伝わる(切り詰めない)。"""
    banner("シナリオ5: 応答サイズ上限超過が失敗として伝わる")
    from jetuse_core import http_tools

    tool = register(
        client, name="huge_api", description="大きな応答を返す検証用API",
        parameters={"type": "object", "properties": {}}, url=urls["big.json"],
        method="GET",
    )
    res = client.post("/api/agent/execute-tool", json={
        "name": "huge_api", "arguments": "{}", "http_tool_id": tool["id"]})
    detail = res.json().get("detail", "")
    ok = res.status_code == 400 and "上限" in detail

    write("scenario-5.md", f"""# シナリオ5 — 応答サイズ上限超過が失敗として伝わる(切り詰めない)

**確かめたこと**: 応答が上限を超えたら、途中まで読んだ内容を「それらしい結果」として
モデルへ返さない。失敗として伝える(切り詰めた本文で誤答させないため)。

- 相手: 実 Object Storage 上の {BIG_OBJECT_BYTES:,} バイトのオブジェクト
- 上限: `http_tools.MAX_RESPONSE_BYTES` = {http_tools.MAX_RESPONSE_BYTES:,} バイト

応答 `POST /api/agent/execute-tool`: HTTP {res.status_code}

{fence(detail)}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def scenario_6(client, urls: dict) -> bool:
    """圧縮爆弾: 送られてくる量が小さくても、展開後のバイト数で上限に掛かる。"""
    banner("シナリオ6: 圧縮された巨大応答も上限で止まる")
    from jetuse_core import http_tools

    tool = register(
        client, name="bomb_api", description="圧縮された巨大応答を返す検証用API",
        parameters={"type": "object", "properties": {}}, url=urls["bomb.json"],
        method="GET",
    )
    res = client.post("/api/agent/execute-tool", json={
        "name": "bomb_api", "arguments": "{}", "http_tool_id": tool["id"]})
    detail = res.json().get("detail", "")
    ok = res.status_code == 400 and "上限" in detail

    write("scenario-6.md", f"""# シナリオ6 — 圧縮された巨大応答も上限で止まる

**確かめたこと**: 上限をバイト数で測る以上、「小さく送って大きく展開させる」応答
(圧縮爆弾)で上限判定をすり抜けられてはいけない。JetUse は `Accept-Encoding: identity` で
圧縮を要求せず、Content-Length の申告が上限を超えていれば 1 バイトも読まず、読む場合も
**足す前に**測る。

- 相手: 実 Object Storage 上の `bomb.json`
  (`Content-Encoding: gzip` / 展開後 {BOMB_PLAIN_BYTES:,} バイト。送られる量は数 KB)
- 上限: `http_tools.MAX_RESPONSE_BYTES` = {http_tools.MAX_RESPONSE_BYTES:,} バイト

応答 `POST /api/agent/execute-tool`: HTTP {res.status_code}

{fence(detail)}

判定: **{'PASS' if ok else 'FAIL'}**
""")
    return ok


def main() -> None:
    banner(f"TOOL-01 実環境 E2E(スキーマ {SCHEMA})")
    use_task_schema()

    from fastapi.testclient import TestClient
    from jetuse_core.db import connect
    from service.main import app

    with connect() as conn:  # 前回の残りがあると所有者ごとの一覧が混ざる
        cur = conn.cursor()
        cur.execute("DELETE FROM http_tools WHERE owner_sub = :o", o=OWNER)
        conn.commit()

    urls = ensure_fake_api()
    ocid = secret_ocid()
    client = TestClient(app)

    results = {
        "シナリオ1(外部ツールをエージェントが自分で呼ぶ)": scenario_1(client, urls),
        "シナリオ2(Vault 経由の秘密が届く・平文を残さない)": scenario_2(client, ocid),
        "シナリオ3(否定: 内部を向く URL の登録が拒否される)": scenario_3(client),
        "シナリオ4(タイムアウトが失敗として伝わる)": scenario_4(client),
        "シナリオ5(サイズ上限超過が失敗として伝わる)": scenario_5(client, urls),
        "シナリオ6(圧縮された巨大応答も上限で止まる)": scenario_6(client, urls),
    }
    lines = "\n".join(f"- {'PASS' if v else 'FAIL'} — {k}" for k, v in results.items())
    write("summary.md", f"""# TOOL-01 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `{SCHEMA}` / dev コンパートメント。
架空の業務 API(Object Storage + PAR)・Vault・OCI Generative AI(gpt-oss-120b)は実物。
JetUse 側に業務ロジックは持たせていない(渡す口だけ)。

{lines}

## 検証で作った資源(すべて `{PREFIX}` 接頭辞)

- Object Storage バケット `{urls['bucket']}`(stock.json / big.json / bomb.json + PAR 3 本)
- Vault 秘密 `{SECRET_NAME}`(検証用の使い捨てトークン)
- ADB スキーマ `{SCHEMA}`

片付けは `spikes/tool01/teardown.py --yes` と `spikes/ragm02/teardown.py --yes`。
""")
    banner("結果")
    print(lines)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
