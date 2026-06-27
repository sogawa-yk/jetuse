"""SPIKE-06 / PAPI-02: スコープ承認＋短期トークン発行フローの実環境スパイク。

PAPI-01 の認可コア(spike05)の上で、ADR-0014 §2「承認された範囲だけをトークンに載せる」を実 ADB で
実証する。jetuse-dev の固定 loop 環境(実 ADB / 専用スキーマ)に対して、承認(approve_scopes)→
発行(issue_token)→検証→authorize(ALLOW 監査)の正常系と、承認境界・失効・越境の拒否系(トークン未発行)を
実行し、`platform_scope_grants`(承認)と `platform_broker_audit`(アクセス)に証跡が残ることを確認する。

テナント境界(ADR-0014: tenant = Project OCID)は env で注入する(spike05 と同様、代表 Project OCID を 2 つ)。

実行(接続・テナント情報・署名鍵はすべて env で注入。実値はコミットしない):
    ADB_USER=<schema> ADB_PASSWORD=<pw> ADB_DSN=<adb-dsn> ADB_WALLET_DIR=<dir> \
    ADB_WALLET_PASSWORD=<pw> PLATFORM_BROKER_SECRET=<secret> \
    PLATFORM_TENANT=<project-ocid-A> PLATFORM_TENANT_OTHER=<project-ocid-B> \
    .venv/bin/python spikes/spike06_platform_grants.py <run-marker>

scenario:
  S1 (承認→発行 正常): manifest(permissions=[rag.search, db.query]) に rag.search **のみ**承認 →
     platform_scope_grants に ACTIVE 行 → issue_token が rag.search だけ載せる(db.query は載らない) →
     authorize 通過で ALLOW 監査行。
  S2 (拒否): (a) 承認超過 db.query 要求 → scope_not_granted でトークン未発行、(b) manifest 非要求スコープの
     承認 → GrantError、(c) revoke 後 issue_token → grant_revoked(grant 行 REVOKED)、(d) 別テナント
     T2 → no_grant。
最後に当該 plugin のグラント行と marker の監査行を SELECT して JSON 出力する(実 ADB に残った証拠)。
"""

from __future__ import annotations

import json
import os
import sys

from jetuse_core import platform_grants as pg
from jetuse_core.db import connect
from jetuse_core.migrate import MIGRATIONS_DIR, _statements
from jetuse_core.platform_broker import authorize, verify_broker_token
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest

# tenant = Project OCID(ADR-0014)。env 注入漏れのまま実 DB に対して pass を出さないよう、
# 必須 env が無ければ即失敗させる(fail-closed。F-002)。tenant は broker では不透明文字列なので、
# 未設定フォールバックを許すと「環境を注入し損ねた E2E」が緑に見えてしまう。
PLUGIN = os.environ.get("PLATFORM_PLUGIN_ID", "jetuse/papi02-spike")
APPROVER = "loop-e2e-sa"


def _require_env() -> tuple[str, str]:
    """E2E 必須の env を強制する。未設定・同値・署名鍵欠如はここで止める(緑の偽装を防ぐ)。"""
    t1 = os.environ.get("PLATFORM_TENANT", "")
    t2 = os.environ.get("PLATFORM_TENANT_OTHER", "")
    missing = [
        name
        for name in ("PLATFORM_TENANT", "PLATFORM_TENANT_OTHER", "PLATFORM_BROKER_SECRET")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"必須 env が未設定: {missing}(E2E は env 注入を強制する)")
    if t1 == t2:
        raise SystemExit("PLATFORM_TENANT と PLATFORM_TENANT_OTHER は別テナントでなければならない")
    return t1, t2


def _manifest(permissions):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": PLUGIN,
            "version": "1.0.0",
            "kind": "usecase",
            "name": "PAPI-02 spike",
            "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": permissions,
            "contributes": {
                "usecase": {
                    "fields": [{"name": "q", "type": "textarea"}],
                    "template": "{{q}}",
                }
            },
        }
    )


def ensure_tables() -> None:
    """migration 020(監査)/021(承認)を冪等適用する(既存ならスキップ)。"""
    wanted = {
        "PLATFORM_BROKER_AUDIT": "020_platform_broker_audit.sql",
        "PLATFORM_SCOPE_GRANTS": "021_platform_scope_grants.sql",
    }
    with connect() as conn:
        cur = conn.cursor()
        for table, fname in wanted.items():
            cur.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table
            )
            if cur.fetchone()[0]:
                print(f"[setup] {table} すでに存在 — skip", file=sys.stderr)
                continue
            for stmt in _statements((MIGRATIONS_DIR / fname).read_text()):
                cur.execute(stmt)
            conn.commit()
            print(f"[setup] {table} を作成", file=sys.stderr)


def cleanup(marker: str) -> None:
    """前回 run の残骸を消して決定的にする。承認(plugin)と当該 marker の監査を消し、証跡を run に閉じる。

    監査も marker で消すのは、同じ marker で再実行したとき ALLOW 行が累積して件数チェックが
    壊れるのを防ぐため(E2E ハーネスの再現性。product 側の挙動ではない)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM platform_scope_grants WHERE plugin_id = :p", p=PLUGIN)
        cur.execute("DELETE FROM platform_broker_audit WHERE resource_id = :m", m=marker)
        conn.commit()


def select_grants() -> list[dict]:
    return [
        {k: g[k] for k in ("tenant", "plugin_id", "source_version", "scopes", "status")}
        for g in pg.list_grants(plugin_id=PLUGIN)
    ]


def select_audit(marker: str) -> list[dict]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tenant, plugin_id, scope, decision, reason, jti
            FROM platform_broker_audit
            WHERE resource_id = :m
            ORDER BY created_at
            """,
            m=marker,
        )
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def run(marker: str, t1: str, t2: str) -> dict:
    results: list[dict] = []
    m_full = _manifest(["platform:rag.search", "platform:db.query"])
    m_rag_only = _manifest(["platform:rag.search"])

    # S1: rag.search のみ承認 → 永続化 → 発行は rag.search だけ → authorize ALLOW。
    grant = pg.approve_scopes(
        m_full, tenant=t1, scopes=["platform:rag.search"], approved_by=APPROVER
    )
    token = pg.issue_token(t1, PLUGIN)
    ctx = verify_broker_token(token)
    authorize(token, "platform:rag.search", tenant=t1, resource=marker)
    results.append(
        {
            "scenario": "S1-approve-issue",
            "expected": "ALLOW",
            "grant_status": grant["status"],
            "grant_scopes": grant["scopes"],
            # manifest は db.query も宣言したが、承認は rag.search のみ → トークンに db.query は載らない。
            "token_scopes": sorted(ctx.scopes),
            "db_query_in_token": "platform:db.query" in ctx.scopes,
        }
    )

    # S2a: 承認超過(db.query 要求)→ scope_not_granted でトークン未発行。
    try:
        pg.issue_token(t1, PLUGIN, scopes=["platform:db.query"])
        results.append({"scenario": "S2a-excess-scope", "expected": "DENY", "got": "ISSUED(!)"})
    except pg.GrantDenied as d:
        results.append({"scenario": "S2a-excess-scope", "expected": "DENY", "reason": d.reason})

    # S2b: manifest 非要求スコープの承認 → GrantError(最小権限)。
    try:
        pg.approve_scopes(
            m_rag_only, tenant=t1, scopes=["platform:db.query"], approved_by=APPROVER
        )
        results.append({"scenario": "S2b-not-requested", "expected": "DENY", "got": "APPROVED(!)"})
    except pg.GrantError as e:
        results.append(
            {"scenario": "S2b-not-requested", "expected": "DENY", "reason": str(e)[:60]}
        )

    # S2c: 失効後の発行 → grant_revoked(トークン未発行)。grant 行は REVOKED に遷移。
    revoked = pg.revoke_grant(t1, PLUGIN)
    try:
        pg.issue_token(t1, PLUGIN)
        results.append({"scenario": "S2c-revoked", "expected": "DENY", "got": "ISSUED(!)"})
    except pg.GrantDenied as d:
        results.append(
            {"scenario": "S2c-revoked", "expected": "DENY", "reason": d.reason, "revoked": revoked}
        )

    # S2d: 別テナント(グラント無し)→ no_grant。
    try:
        pg.issue_token(t2, PLUGIN)
        results.append({"scenario": "S2d-cross-tenant", "expected": "DENY", "got": "ISSUED(!)"})
    except pg.GrantDenied as d:
        results.append({"scenario": "S2d-cross-tenant", "expected": "DENY", "reason": d.reason})

    return {
        "marker": marker,
        "scenarios": results,
        "grants": select_grants(),
        "audit_rows": select_audit(marker),
    }


def _check(out: dict, t1: str) -> bool:
    by = {r["scenario"]: r for r in out["scenarios"]}
    s1 = by.get("S1-approve-issue", {})
    ok_s1 = (
        s1.get("token_scopes") == ["platform:rag.search"]
        and s1.get("db_query_in_token") is False
        and s1.get("grant_status") == "ACTIVE"
    )
    ok_denials = (
        by.get("S2a-excess-scope", {}).get("reason") == "scope_not_granted"
        and "reason" in by.get("S2b-not-requested", {})
        and by.get("S2c-revoked", {}).get("reason") == "grant_revoked"
        and by.get("S2d-cross-tenant", {}).get("reason") == "no_grant"
    )
    # ALLOW 監査が 1 行残る(S1)。最終グラントは REVOKED(S2c)。
    allow = sum(1 for r in out["audit_rows"] if r["decision"] == "ALLOW")
    final_grant = next((g for g in out["grants"] if g["tenant"] == t1), {})
    ok_db = allow == 1 and final_grant.get("status") == "REVOKED"
    return bool(ok_s1 and ok_denials and ok_db)


def main() -> int:
    marker = sys.argv[1] if len(sys.argv) > 1 else "spike06-default"
    t1, t2 = _require_env()
    ensure_tables()
    cleanup(marker)
    out = run(marker, t1, t2)
    out["pass"] = _check(out, t1)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
