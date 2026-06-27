"""CON-02: コネクタ実行(invoke)層 ＋ コア Slack コネクタの実環境スパイク。

jetuse-dev の固定 loop 環境(実 ADB)に対し、専用スキーマ JETUSE_CON_02 で下記を実行し、
**コネクタ登録 → ブローカー認可付き invoke → 監査が実 ADB に残る**ことを確認する。Codex はこの
出力＋SELECT 結果を証跡として採点する(実行はしない)。

**実 Slack 認証は投入しない**: builtin Slack の HTTP 送信は mock transport で代替し、投稿フローを
検証する。実トークンはどこにも保存せず、監査・証跡にも書かない(secret_resolver はダミー値を返す)。

実行(接続・テナント情報は env で注入。コミットしない):
    ADB_USER=JETUSE_CON_02 ADB_PASSWORD=... ADB_DSN=..._low ADB_WALLET_DIR=... ADB_WALLET_PASSWORD=... \
    PLATFORM_BROKER_SECRET=... PLATFORM_TENANT=<project-ocid-A> PLATFORM_TENANT_OTHER=<project-ocid-B> \
    COMPARTMENT_OCID=<jetuse-dev> OCI_REGION=ap-osaka-1 \
    .venv/bin/python spikes/spike_con02_connector_invoke.py <run-marker>

scenario:
  S1 (ALLOW): コア Slack コネクタを register_connector → connector_instances に出現(CLOB に secretRef
              のみ・実トークン無し)→ connector.invoke を付与した短期トークンで post_message を
              mock transport で実行 → 成功 + ALLOW 監査行。
  S2 (DENY) : (a) connector.invoke 未付与トークンで invoke → 拒否・mock 不呼出・DENY 監査行、
              (b) 別テナント越境トークンで invoke → tenant_mismatch・mock 不呼出・DENY 監査行。
最後に当該 run の監査行と connector_instances を SELECT して JSON 出力する(実 ADB に残った証拠)。
"""

from __future__ import annotations

import json
import os
import sys

from jetuse_core import platform_broker as pb
from jetuse_core.db import connect
from jetuse_core.migrate import migrate
from jetuse_core.plugins.connector_runtime import (
    ConnectorInvokeDenied,
    invoke_connector_action,
)
from jetuse_core.plugins.connector_store import (
    list_connectors,
    register_connector,
    remove_connector,
)
from jetuse_core.plugins.slack_connector_builtin import (
    slack_connector_definition,
    slack_connector_manifest,
)

# tenant = Project OCID(ADR-0014)。env で実 Project OCID を注入する。未設定は OCID 風でない
# プレースホルダにして証跡上ひと目で気付けるようにする(実値混入と紛らわしくしない)。
T1 = os.environ.get("PLATFORM_TENANT", "UNSET-set-PLATFORM_TENANT-project-A")
T2 = os.environ.get("PLATFORM_TENANT_OTHER", "UNSET-set-PLATFORM_TENANT_OTHER-project-B")

# 実トークンは投入しない。secret_resolver はダミー値を返す(どこにも保存しない)。
FAKE_BOT_TOKEN = "xoxb-MOCK-not-a-real-token"

#: 受け入れ条件が直接かかわる migration（本タスクは 019/020 を使う。migrate() は全件適用するが、
#: 冪等性・出現の確認はこの2つで行う）。
REQUIRED_MIGRATIONS = ("019_connector_instances", "020_platform_broker_audit")


class _MockSlackHttp:
    """builtin Slack の HTTP 送信を mock する。実 Slack へは到達しない。呼び出しを記録する。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, url, headers, body):
        # 実トークンを証跡に残さないため、Authorization ヘッダの有無のみ記録する。
        self.calls.append({"url": url, "has_auth": "Authorization" in headers, "body": body})
        return {"ok": True, "channel": body.get("channel"), "ts": "1700000000.000200"}


def _fake_resolver(ref: str) -> str:
    return FAKE_BOT_TOKEN


def ensure_tables() -> list[str]:
    """**実ランナー `migrate()`** で全 migration を適用する(deploy_cmd `python -m jetuse_core.migrate`
    と同一経路)。冪等性は `schema_migrations` の既適用スキップで担保され、再実行は no-op になる。
    手動 SQL 実行では schema_migrations を通さず部分適用を no-op と誤判定し得るため、本来の runner を使う。
    """
    return migrate()


def select_audit(marker: str) -> list[dict]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tenant, plugin_id, scope, decision, reason, resource_id, jti
            FROM platform_broker_audit
            WHERE resource_id = :m
            ORDER BY created_at
            """,
            m=marker,
        )
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def run(marker: str) -> dict:
    out: dict = {"marker": marker, "tenant1": T1, "tenant2": T2}

    # --- 登録(CON-01 再利用): コア Slack コネクタを inst へ登録 ---
    manifest = slack_connector_manifest()
    rec = register_connector(manifest, registered_by="spike-con02", name="Slack コネクタ(spike)")
    instance_id = rec["id"]
    # 定義 CLOB に secretRef(参照名)はあるが実トークンは無いことを確認する。
    definition_json = json.dumps(rec["definition"], ensure_ascii=False)
    out["register"] = {
        "instance_id": instance_id,
        "provider": rec["provider"],
        "transport": rec["transport"],
        "definition_has_secretRef": "slack-bot-token" in definition_json,
        "definition_has_real_token": FAKE_BOT_TOKEN in definition_json,  # 期待: False
        "composition_ok": rec["composition"]["ok"],
    }

    definition = slack_connector_definition()
    scenarios: list[dict] = []

    # --- S1 ALLOW: connector.invoke 付与トークンで post_message(mock transport) ---
    http1 = _MockSlackHttp()
    token = pb.issue_broker_token("jetuse/slack-connector", T1, ["platform:connector.invoke"])
    result = invoke_connector_action(
        definition,
        "post_message",
        {"channel": "#demo", "text": "JetUse コネクタ E2E(mock)"},
        broker_token=token,
        tenant=T1,
        resource=marker,
        secret_resolver=_fake_resolver,
        http_caller=http1,
    )
    scenarios.append(
        {
            "scenario": "S1-allow-post",
            "expected": "ALLOW",
            "ok": result.ok,
            "transport": result.transport,
            "http_calls": len(http1.calls),  # 期待: 1
            "http_had_auth": http1.calls[0]["has_auth"] if http1.calls else None,
            "jti": result.jti,
        }
    )

    # --- S2a DENY: connector.invoke 未付与 → 拒否・mock 不呼出 ---
    http2 = _MockSlackHttp()
    no_scope = pb.issue_broker_token("jetuse/slack-connector", T1, ["platform:rag.search"])
    try:
        invoke_connector_action(
            definition,
            "post_message",
            {"channel": "#demo", "text": "should not reach slack"},
            broker_token=no_scope,
            tenant=T1,
            resource=marker,
            secret_resolver=_fake_resolver,
            http_caller=http2,
        )
        scenarios.append({"scenario": "S2a-no-scope", "expected": "DENY", "got": "ALLOW(!)"})
    except ConnectorInvokeDenied as d:
        scenarios.append(
            {
                "scenario": "S2a-no-scope",
                "expected": "DENY",
                "reason": d.reason,
                "http_calls": len(http2.calls),  # 期待: 0(外部不到達)
            }
        )

    # --- S2b DENY: 別テナント越境 → 拒否・mock 不呼出 ---
    http3 = _MockSlackHttp()
    try:
        invoke_connector_action(
            definition,
            "post_message",
            {"channel": "#demo", "text": "cross tenant"},
            broker_token=token,  # tenant=T1 のトークン
            tenant=T2,  # 別テナントへ越境
            resource=marker,
            secret_resolver=_fake_resolver,
            http_caller=http3,
        )
        scenarios.append({"scenario": "S2b-cross-tenant", "expected": "DENY", "got": "ALLOW(!)"})
    except ConnectorInvokeDenied as d:
        scenarios.append(
            {
                "scenario": "S2b-cross-tenant",
                "expected": "DENY",
                "reason": d.reason,
                "http_calls": len(http3.calls),  # 期待: 0
            }
        )

    out["scenarios"] = scenarios

    # --- 監査と登録の SELECT(実 ADB に残った証拠) ---
    out["audit_rows"] = select_audit(marker)
    out["connectors_after_register"] = [
        {"id": c["id"], "provider": c["provider"], "transport": c["transport"]}
        for c in list_connectors(provider="slack")
    ]

    # 後始末: 登録した spike インスタンスを削除する(むやみに残さない)。監査行は証跡として残す。
    out["removed"] = remove_connector(instance_id)
    return out


def main() -> int:
    marker = sys.argv[1] if len(sys.argv) > 1 else "spike-con02-default"

    applied_first = ensure_tables()
    applied_again = ensure_tables()  # 冪等性: 2回目は空であるべき(schema_migrations で既適用スキップ)。

    out = run(marker)
    out["migrations"] = {
        "runner": "jetuse_core.migrate.migrate()",  # deploy_cmd と同一経路
        "applied_first_run": applied_first,
        "applied_second_run": applied_again,  # 期待: [](no-op)
        "required_applied": all(m in applied_first for m in REQUIRED_MIGRATIONS),
        "idempotent": applied_again == [],
    }

    decisions = [r["decision"] for r in out["audit_rows"]]
    out["audit_summary"] = {
        "total": len(decisions),
        "allow": decisions.count("ALLOW"),
        "deny": decisions.count("DENY"),
    }

    s = {r["scenario"]: r for r in out["scenarios"]}
    ok = (
        out["register"]["definition_has_secretRef"]
        and not out["register"]["definition_has_real_token"]
        and out["register"]["composition_ok"]
        and s.get("S1-allow-post", {}).get("ok") is True
        and s.get("S1-allow-post", {}).get("http_calls") == 1
        and s.get("S2a-no-scope", {}).get("reason") == "scope_denied"
        and s.get("S2a-no-scope", {}).get("http_calls") == 0
        and s.get("S2b-cross-tenant", {}).get("reason") == "tenant_mismatch"
        and s.get("S2b-cross-tenant", {}).get("http_calls") == 0
        and out["audit_summary"]["allow"] == 1
        and out["audit_summary"]["deny"] == 2
        and out["migrations"]["required_applied"]
        and out["migrations"]["idempotent"]
    )
    out["pass"] = ok
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
