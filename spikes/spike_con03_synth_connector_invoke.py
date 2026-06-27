"""CON-03: 合成(synth) → デプロイ前ガバナンス(governance) → ブローカー経由 invoke の実環境スパイク。

jetuse-dev の固定 loop 環境(実 ADB)に対し、専用スキーマ JETUSE_CON_03 で下記を実行し、
**コネクタ束縛 → connector_scope ゲート通過 → コネクタ登録 → broker 経由 invoke → 監査が実 ADB に残る**
ことを確認する。Codex はこの出力＋SELECT 結果を証跡として採点する(実行はしない)。

**実 Slack 認証・実 Vault 束ね・実 SaaS 接続は投入しない**: builtin Slack の HTTP 送信は mock transport で
代替し、投稿フローを検証する。実トークンはどこにも保存せず、監査・証跡にも書かない。

実行(接続・テナント情報は env で注入。コミットしない):
    ADB_USER=JETUSE_CON_03 ... PLATFORM_BROKER_SECRET=... PLATFORM_TENANT=<project-A> \
    PLATFORM_TENANT_OTHER=<project-B> .venv/bin/python spikes/spike_con03_synth_connector_invoke.py <marker>

scenario:
  S1 (合成→ガバナンス→ALLOW): recommend(slack) → synthesize で connector_bindings に slack=active →
        validate_governance ok(connector_scope パス) → register_connector → resolve_active_connector →
        connector.invoke 付与トークンで post_message を mock transport で実行 → 成功 + ALLOW 監査行。
  S2 (パレット外＋fail-closed): (a) teams を含む構成 → governance disallowed_combination・active にならない、
        (b) connector.invoke 未付与トークン → 拒否・mock 不呼出・DENY 監査行、
        (c) 別テナント越境トークン → tenant_mismatch・mock 不呼出・DENY 監査行。
最後に当該 run の監査行と connector_instances を SELECT して JSON 出力する(実 ADB に残った証拠)。
"""

from __future__ import annotations

import json
import os
import sys

from jetuse_core import platform_broker as pb
from jetuse_core.db import connect
from jetuse_core.governance import validate_governance
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
from jetuse_core.plugins.core_connectors import resolve_active_connector
from jetuse_core.plugins.slack_connector_builtin import slack_connector_manifest
from jetuse_core.recommend import recommend
from jetuse_core.synth import synthesize

T1 = os.environ.get("PLATFORM_TENANT", "UNSET-set-PLATFORM_TENANT-project-A")
T2 = os.environ.get("PLATFORM_TENANT_OTHER", "UNSET-set-PLATFORM_TENANT_OTHER-project-B")

# 実トークンは投入しない。secret_resolver はダミー値を返す(どこにも保存しない)。
FAKE_BOT_TOKEN = "xoxb-MOCK-not-a-real-token"

REQUIRED_MIGRATIONS = ("019_connector_instances", "020_platform_broker_audit")


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


class _MockSlackHttp:
    """builtin Slack の HTTP 送信を mock する。実 Slack へは到達しない。呼び出しを記録する。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "has_auth": "Authorization" in headers, "body": body})
        return {"ok": True, "channel": body.get("channel"), "ts": "1700000000.000300"}


def _fake_resolver(ref: str) -> str:
    return FAKE_BOT_TOKEN


def ensure_tables() -> list[str]:
    """実ランナー migrate() で全 migration を冪等適用する(deploy_cmd と同一経路)。"""
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

    # --- 合成(synth): slack 連携の推薦 → デモ構成。connector_bindings に slack=active ---
    comp = synthesize(recommend(_answers(Q4="slack")))
    slack_binding = next(
        (b for b in comp.connector_bindings if b.provider == "slack"), None
    )
    out["synthesis"] = {
        "connectors": comp.connectors,  # 後方互換の生リスト
        "active_connectors": comp.active_connectors,
        "slack_status": slack_binding.status if slack_binding else None,
        "slack_required_scopes": slack_binding.required_scopes if slack_binding else [],
        "slack_secret_ref": slack_binding.secret_ref if slack_binding else None,
        # 合成結果に実トークンが出ない(参照名のみ)。
        "no_real_token_in_composition": FAKE_BOT_TOKEN not in comp.model_dump_json(),
    }

    # --- デプロイ前ガバナンス: connector_scope を含む4制約を通過するか ---
    gov = validate_governance(comp)
    out["governance"] = {
        "ok": gov.ok,
        "checks": gov.checks,
        "connector_scope": gov.checks.get("connector_scope"),
        "violations": [v.kind for v in gov.violations],
    }

    # --- パレット外コネクタ(teams)を含む構成は governance が弾く(active にならない) ---
    comp_teams = synthesize(
        recommend(_answers(Q4="slack")).model_copy(update={"connectors": ["slack", "teams"]})
    )
    gov_teams = validate_governance(comp_teams)
    out["governance_palette_outside"] = {
        "active_connectors": comp_teams.active_connectors,  # 期待: ["slack"](teams 除外)
        "ok": gov_teams.ok,  # 期待: False
        "has_disallowed_connector": any(
            v.kind == "disallowed_combination" and v.element == "teams"
            for v in gov_teams.violations
        ),
    }

    # --- コネクタ登録(CON-01 再利用): コア Slack コネクタを inst へ登録 ---
    manifest = slack_connector_manifest()
    rec = register_connector(manifest, registered_by="spike-con03", name="Slack コネクタ(spike)")
    instance_id = rec["id"]
    definition_json = json.dumps(rec["definition"], ensure_ascii=False)
    out["register"] = {
        "instance_id": instance_id,
        "provider": rec["provider"],
        "transport": rec["transport"],
        "definition_has_secretRef": "slack-bot-token" in definition_json,
        "definition_has_real_token": FAKE_BOT_TOKEN in definition_json,  # 期待: False
        "composition_ok": rec["composition"]["ok"],
    }

    # --- 合成構成の active コネクタを解決して broker 経由で invoke する ---
    definition = resolve_active_connector(comp, "slack")
    out["resolve_active_connector_ok"] = definition is not None
    scenarios: list[dict] = []

    # --- S1 ALLOW: connector.invoke 付与トークンで post_message(mock transport) ---
    http1 = _MockSlackHttp()
    token = pb.issue_broker_token("jetuse/slack-connector", T1, ["platform:connector.invoke"])
    result = invoke_connector_action(
        definition,
        "post_message",
        {"channel": "#demo", "text": "JetUse 合成→invoke E2E(mock)"},
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
            "no_token_in_output": FAKE_BOT_TOKEN not in json.dumps(result.output, default=str),
            "jti": result.jti,
        }
    )

    # --- S2b DENY: connector.invoke 未付与 → 拒否・mock 不呼出 ---
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
        scenarios.append({"scenario": "S2b-no-scope", "expected": "DENY", "got": "ALLOW(!)"})
    except ConnectorInvokeDenied as d:
        scenarios.append(
            {
                "scenario": "S2b-no-scope",
                "expected": "DENY",
                "reason": d.reason,
                "http_calls": len(http2.calls),  # 期待: 0(外部不到達)
            }
        )

    # --- S2c DENY: 別テナント越境 → 拒否・mock 不呼出 ---
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
        scenarios.append({"scenario": "S2c-cross-tenant", "expected": "DENY", "got": "ALLOW(!)"})
    except ConnectorInvokeDenied as d:
        scenarios.append(
            {
                "scenario": "S2c-cross-tenant",
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
    marker = sys.argv[1] if len(sys.argv) > 1 else "spike-con03-default"

    applied_first = ensure_tables()
    applied_again = ensure_tables()  # 冪等性: 2回目は空であるべき。

    out = run(marker)
    out["migrations"] = {
        "runner": "jetuse_core.migrate.migrate()",
        "applied_first_run": applied_first,
        "applied_second_run": applied_again,
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
        out["synthesis"]["slack_status"] == "active"
        and "platform:connector.invoke" in out["synthesis"]["slack_required_scopes"]
        and out["synthesis"]["no_real_token_in_composition"]
        and out["governance"]["ok"] is True
        and out["governance"]["connector_scope"] is True
        and out["governance_palette_outside"]["active_connectors"] == ["slack"]
        and out["governance_palette_outside"]["ok"] is False
        and out["governance_palette_outside"]["has_disallowed_connector"]
        and out["register"]["definition_has_secretRef"]
        and not out["register"]["definition_has_real_token"]
        and out["register"]["composition_ok"]
        and out["resolve_active_connector_ok"]
        and s.get("S1-allow-post", {}).get("ok") is True
        and s.get("S1-allow-post", {}).get("http_calls") == 1
        and s.get("S1-allow-post", {}).get("no_token_in_output") is True
        and s.get("S2b-no-scope", {}).get("reason") == "scope_denied"
        and s.get("S2b-no-scope", {}).get("http_calls") == 0
        and s.get("S2c-cross-tenant", {}).get("reason") == "tenant_mismatch"
        and s.get("S2c-cross-tenant", {}).get("http_calls") == 0
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
