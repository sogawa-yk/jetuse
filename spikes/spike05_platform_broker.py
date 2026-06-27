"""SPIKE-05 / PAPI-01: Platform API ブローカーの実環境スパイク。

ADR-0014 の認可コア(短期トークンの発行/検証/スコープ強制/テナント境界/監査)を、jetuse-dev の
固定 loop 環境(実 ADB)に対して最小実行し、**越境の許可/拒否が実 ADB の `platform_broker_audit` に
記録される**ことを確認する。Codex はこの出力＋SELECT 結果を証跡として採点する。

テナント境界(ADR-0014: tenant = Project OCID)は env で注入する。loop 環境には GenAI Project リソースを
provision していないため、**代表 Project OCID**(Project OCID 形・env 注入・コミットしない)を 2 つ与え、
「同一 → ALLOW / 別 → tenant_mismatch」を実証する(tenancy OCID 風の固定ダミーは使わない)。

実行(接続・テナント情報は env で注入。コミットしない):
    ADB_USER=JETUSE_PAPI ADB_PASSWORD=... ADB_DSN=..._low ADB_WALLET_DIR=... ADB_WALLET_PASSWORD=... \
    PLATFORM_BROKER_SECRET=... PLATFORM_TENANT=<project-ocid-A> PLATFORM_TENANT_OTHER=<project-ocid-B> \
    .venv/bin/python spikes/spike05_platform_broker.py <run-marker>

scenario:
  S1 (ALLOW): tenant T1(Project OCID)/ plugin P / scope rag.search を発行 → authorize 通過 → ALLOW。
  S2 (DENY) : (a) 別テナント T2(別 Project OCID)への越境 → tenant_mismatch、(b) 期限切れトークン →
              invalid_token、(c) 未付与スコープ db.query → scope_denied。各々 DENY 監査行。
最後に当該 run の監査行を SELECT して JSON で出力する(実 ADB に残った証拠)。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from jetuse_core import platform_broker as pb
from jetuse_core.db import connect
from jetuse_core.migrate import MIGRATIONS_DIR, _statements

# tenant = Project OCID(ADR-0014)。E2E は PLATFORM_TENANT[_OTHER] で実 Project OCID を注入する。
# 既定は **OCID 風でない**明示プレースホルダにして、env 設定漏れを証跡上ひと目で気付けるようにする
# (OCID 風の既定だと「実値が入った」と紛らわしいため。review-3 minor 対応)。
T1 = os.environ.get("PLATFORM_TENANT", "UNSET-set-PLATFORM_TENANT-project-A")
T2 = os.environ.get("PLATFORM_TENANT_OTHER", "UNSET-set-PLATFORM_TENANT_OTHER-project-B")
PLUGIN = os.environ.get("PLATFORM_PLUGIN_ID", "jetuse/papi-spike")


def ensure_audit_table() -> None:
    """migration 019 を冪等適用する(既存ならスキップ)。loop 環境の専用スキーマに表を用意。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'PLATFORM_BROKER_AUDIT'"
        )
        if cur.fetchone()[0]:
            print("[setup] platform_broker_audit すでに存在 — skip", file=sys.stderr)
            return
        sql = (MIGRATIONS_DIR / "020_platform_broker_audit.sql").read_text()
        for stmt in _statements(sql):
            cur.execute(stmt)
        conn.commit()
        print("[setup] platform_broker_audit を作成", file=sys.stderr)


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
    results: list[dict] = []

    # S1 ALLOW: 正規の発行 → authorize 通過。
    token = pb.issue_broker_token(PLUGIN, T1, ["platform:rag.search"])
    ctx = pb.authorize(token, "platform:rag.search", tenant=T1, resource=marker)
    results.append(
        {"scenario": "S1-allow", "expected": "ALLOW",
         "plugin": ctx.plugin_id, "tenant": ctx.tenant, "scopes": sorted(ctx.scopes)}
    )

    # S2a DENY: 別テナント越境。
    try:
        pb.authorize(token, "platform:rag.search", tenant=T2, resource=marker)
        results.append({"scenario": "S2a-cross-tenant", "expected": "DENY", "got": "ALLOW(!)"})
    except pb.BrokerDenied as d:
        results.append({"scenario": "S2a-cross-tenant", "expected": "DENY", "reason": d.reason})

    # S2b DENY: 期限切れトークン(過去発行・短 TTL)。
    expired = pb.issue_broker_token(
        PLUGIN, T1, ["platform:rag.search"], ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    try:
        pb.authorize(expired, "platform:rag.search", tenant=T1, resource=marker)
        results.append({"scenario": "S2b-expired", "expected": "DENY", "got": "ALLOW(!)"})
    except pb.BrokerDenied as d:
        results.append({"scenario": "S2b-expired", "expected": "DENY", "reason": d.reason})

    # S2c DENY: 未付与スコープ。
    try:
        pb.authorize(token, "platform:db.query", tenant=T1, resource=marker)
        results.append({"scenario": "S2c-scope", "expected": "DENY", "got": "ALLOW(!)"})
    except pb.BrokerDenied as d:
        results.append({"scenario": "S2c-scope", "expected": "DENY", "reason": d.reason})

    audit_rows = select_audit(marker)
    return {"marker": marker, "scenarios": results, "audit_rows": audit_rows}


def main() -> int:
    marker = sys.argv[1] if len(sys.argv) > 1 else "spike05-default"
    ensure_audit_table()
    out = run(marker)

    # 期待: ALLOW 1 行 + DENY 3 行 が実 ADB に残る。
    decisions = sorted(r["decision"] for r in out["audit_rows"])
    out["audit_summary"] = {
        "total": len(out["audit_rows"]),
        "allow": decisions.count("ALLOW"),
        "deny": decisions.count("DENY"),
    }
    ok = out["audit_summary"]["allow"] == 1 and out["audit_summary"]["deny"] == 3
    out["pass"] = ok
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
