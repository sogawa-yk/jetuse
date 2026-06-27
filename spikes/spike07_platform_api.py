"""SPIKE-07 / PAPI-03: 実 Platform API ルートの実環境スパイク。

PAPI-01 の認可コア(spike05)・PAPI-02 の承認＋発行(spike06)の上で、実ルート `/platform/*` を
FastAPI TestClient で**実 ADB に対して**叩き、ADR-0014 §13.5「各エンドポイントは冒頭で authorize
(JWT 検証 → scope → テナント一致 → 監査)を呼ぶ」を実証する。jetuse-dev の固定 loop ADB の専用スキーマ
(JETUSE_PAPI03)に対して、承認 → 発行 → `POST /platform/db/query`(読取限定委譲)正常系と、
scope 不足 / テナント越境 / 改竄トークン / 非 SELECT の拒否系を実行し、`platform_broker_audit`(ALLOW/DENY)
に証跡が残ることを実 DB の SELECT で確認する。

env は呼び出し側(e2e ランナー)が注入する。実値はコミットしない:
    ADB_OCID / ADB_DSN / ADB_USER(=JETUSE_PAPI03) / ADB_PASSWORD / ADB_WALLET_PASSWORD /
    ADB_QUERY_USER(=JETUSE_PAPI03) / ADB_QUERY_PASSWORD / PLATFORM_BROKER_SECRET /
    PLATFORM_TENANT(=Project OCID A) / PLATFORM_TENANT_OTHER(=Project OCID B)

実行:
    .venv/bin/python spikes/spike07_platform_api.py <run-marker>
"""

from __future__ import annotations

import json
import os
import sys

from fastapi.testclient import TestClient

from jetuse_core import platform_grants as pg
from jetuse_core.db import connect
from jetuse_core.migrate import MIGRATIONS_DIR, _statements
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest
from service.main import app

PLUGIN = os.environ.get("PLATFORM_PLUGIN_ID", "jetuse/papi03-spike")
APPROVER = "loop-e2e-sa"
SAMPLE_TABLE = "PAPI03_ITEMS"

DB_QUERY = "platform:db.query"
RAG_SEARCH = "platform:rag.search"
CONNECTOR_INVOKE = "platform:connector.invoke"

client = TestClient(app)


def _require_env() -> tuple[str, str]:
    """E2E 必須の env を強制する(緑の偽装を防ぐ。spike06 と同方針)。"""
    required = [
        "ADB_OCID",
        "ADB_DSN",
        "ADB_USER",
        "ADB_PASSWORD",
        "ADB_WALLET_PASSWORD",
        # 読取プールも専用スキーマで動かす(既定 JETUSE_QUERY への無言フォールバックを防ぐ)。
        "ADB_QUERY_USER",
        "ADB_QUERY_PASSWORD",
        "PLATFORM_BROKER_SECRET",
        "PLATFORM_TENANT",
        "PLATFORM_TENANT_OTHER",
    ]
    missing = [n for n in required if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"必須 env が未設定: {missing}(E2E は env 注入を強制する)")
    t1 = os.environ["PLATFORM_TENANT"]
    t2 = os.environ["PLATFORM_TENANT_OTHER"]
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
            "name": "PAPI-03 spike",
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
    """migration 020(監査)/021(承認)を専用スキーマへ冪等適用する。"""
    wanted = {
        # connector.invoke 配管(get_connector)が参照する。未作成だと表欠落→503 になり 404 を観測できない。
        "CONNECTOR_INSTANCES": "019_connector_instances.sql",
        "PLATFORM_BROKER_AUDIT": "020_platform_broker_audit.sql",
        "PLATFORM_SCOPE_GRANTS": "021_platform_scope_grants.sql",
    }
    with connect() as conn:
        cur = conn.cursor()
        for table, fname in wanted.items():
            cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table)
            if cur.fetchone()[0]:
                print(f"[setup] {table} すでに存在 — skip", file=sys.stderr)
                continue
            for stmt in _statements((MIGRATIONS_DIR / fname).read_text()):
                cur.execute(stmt)
            conn.commit()
            print(f"[setup] {table} を作成", file=sys.stderr)


def ensure_sample_table() -> None:
    """db.query が読み取る実テーブル(テナントデータ)を専用スキーマに用意する(冪等)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=SAMPLE_TABLE
        )
        if not cur.fetchone()[0]:
            cur.execute(
                f"CREATE TABLE {SAMPLE_TABLE} "
                "(id NUMBER PRIMARY KEY, name VARCHAR2(100))"
            )
        cur.execute(f"DELETE FROM {SAMPLE_TABLE}")
        cur.executemany(
            f"INSERT INTO {SAMPLE_TABLE}(id, name) VALUES (:1, :2)",
            [(1, "請求書A"), (2, "請求書B"), (3, "見積C")],
        )
        conn.commit()
        print(f"[setup] {SAMPLE_TABLE} に 3 行投入", file=sys.stderr)


def cleanup(t1: str, t2: str) -> None:
    """前回 run の残骸を消して決定的にする。

    監査は本スパイク専用の合成テナント(papi03-project-A/B)で絞って消す。改竄トークンの DENY は
    `plugin_id="?"`(検証前で sub 不明)で記録されるため plugin_id では捕捉できない。テナントで消し、
    テナントで取り出すことで anonymous な DENY も証跡に含める(Codex review-3 指摘)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM platform_scope_grants WHERE plugin_id = :p", p=PLUGIN)
        cur.execute(
            "DELETE FROM platform_broker_audit WHERE tenant IN (:t1, :t2)", t1=t1, t2=t2
        )
        conn.commit()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def select_grants() -> list[dict]:
    return [
        {k: g[k] for k in ("tenant", "plugin_id", "scopes", "status")}
        for g in pg.list_grants(plugin_id=PLUGIN)
    ]


def select_audit(t1: str, t2: str) -> list[dict]:
    # テナントで取り出す(plugin_id="?" の anonymous DENY=改竄トークンも含めるため)。
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tenant, plugin_id, scope, decision, reason, resource_id
            FROM platform_broker_audit
            WHERE tenant IN (:t1, :t2)
            ORDER BY created_at
            """,
            t1=t1,
            t2=t2,
        )
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def run(t1: str, t2: str) -> dict:
    scenarios: list[dict] = []
    manifest = _manifest([RAG_SEARCH, DB_QUERY])

    # 承認: db.query のみ(manifest は rag.search も宣言するが承認はしない)。
    grant = pg.approve_scopes(manifest, tenant=t1, scopes=[DB_QUERY], approved_by=APPROVER)
    token = pg.issue_token(t1, PLUGIN)  # 承認(db.query)に閉じた broker トークン

    # S1: db.query 正常系 — 実テーブルを SELECT → 200 + 行、ALLOW 監査。
    r1 = client.post(
        "/platform/db/query",
        json={"tenant": t1, "sql": f"SELECT id, name FROM {SAMPLE_TABLE} ORDER BY id"},
        headers=_auth(token),
    )
    scenarios.append(
        {
            "scenario": "S1-db.query-allow",
            "expected": "200 + rows",
            "status": r1.status_code,
            "row_count": r1.json().get("row_count") if r1.status_code == 200 else None,
            "rows": r1.json().get("rows") if r1.status_code == 200 else r1.text[:300],
        }
    )

    # S2a: scope 不足 — db.query だけのトークンで connector.invoke を要求 → 403。
    r2a = client.post(
        "/platform/connector/invoke",
        json={"tenant": t1, "connector_id": "x", "action": "y"},
        headers=_auth(token),
    )
    scenarios.append(
        {"scenario": "S2a-scope-denied", "expected": 403, "status": r2a.status_code}
    )

    # S2b: テナント越境 — トークン tenant=T1 だが要求 tenant=T2 → 403。
    r2b = client.post(
        "/platform/db/query",
        json={"tenant": t2, "sql": f"SELECT id FROM {SAMPLE_TABLE}"},
        headers=_auth(token),
    )
    scenarios.append(
        {"scenario": "S2b-tenant-mismatch", "expected": 403, "status": r2b.status_code}
    )

    # S2c: 改竄トークン → 401(fail-closed)。
    r2c = client.post(
        "/platform/db/query",
        json={"tenant": t1, "sql": f"SELECT id FROM {SAMPLE_TABLE}"},
        headers=_auth(token + "tampered"),
    )
    scenarios.append(
        {"scenario": "S2c-invalid-token", "expected": 401, "status": r2c.status_code}
    )

    # S2d: 読取限定 — db.query に UPDATE を投げる → 400(書込は到達しない)。
    r2d = client.post(
        "/platform/db/query",
        json={"tenant": t1, "sql": f"UPDATE {SAMPLE_TABLE} SET name = 'x' WHERE id = 1"},
        headers=_auth(token),
    )
    scenarios.append(
        {
            "scenario": "S2d-readonly-reject",
            "expected": 400,
            "status": r2d.status_code,
            "detail": r2d.json().get("detail", "")[:120] if r2d.status_code != 200 else None,
        }
    )

    # S3: rag.search の配管 — rag.search を承認したトークンは authorize を通り(ALLOW 監査)、
    # 実体未実装で 501 に倒れる(配管が live で動く証跡)。承認→発行は PAPI-02 経路を再利用。
    pg.approve_scopes(manifest, tenant=t1, scopes=[RAG_SEARCH], approved_by=APPROVER)
    rag_token = pg.issue_token(t1, PLUGIN, scopes=[RAG_SEARCH])
    r3a = client.post(
        "/platform/rag/search",
        json={"tenant": t1, "query": "請求書"},
        headers=_auth(rag_token),
    )
    scenarios.append(
        {"scenario": "S3a-rag.search-wired-501", "expected": 501, "status": r3a.status_code}
    )

    # S4: connector.invoke 配管 — connector.invoke を承認 → 401/403 ではなく、
    # 未登録コネクタなら 404(authorize は通る=ALLOW 監査)。実 MCP は CON-02/03。
    manifest_conn = _manifest([CONNECTOR_INVOKE])
    pg.approve_scopes(manifest_conn, tenant=t1, scopes=[CONNECTOR_INVOKE], approved_by=APPROVER)
    conn_token = pg.issue_token(t1, PLUGIN, scopes=[CONNECTOR_INVOKE])
    r4 = client.post(
        "/platform/connector/invoke",
        json={"tenant": t1, "connector_id": "no-such-connector", "action": "x"},
        headers=_auth(conn_token),
    )
    scenarios.append(
        {"scenario": "S4-connector.invoke-wired-404", "expected": 404, "status": r4.status_code}
    )

    return {
        "plugin": PLUGIN,
        "grant_status": grant["status"],
        "scenarios": scenarios,
        "grants": select_grants(),
        "audit_rows": select_audit(t1, t2),
    }


def _check(out: dict) -> bool:
    by = {s["scenario"]: s for s in out["scenarios"]}
    ok = (
        by.get("S1-db.query-allow", {}).get("status") == 200
        and by.get("S1-db.query-allow", {}).get("row_count") == 3
        and by.get("S2a-scope-denied", {}).get("status") == 403
        and by.get("S2b-tenant-mismatch", {}).get("status") == 403
        and by.get("S2c-invalid-token", {}).get("status") == 401
        and by.get("S2d-readonly-reject", {}).get("status") == 400
    )
    ok_wired = (
        by.get("S3a-rag.search-wired-501", {}).get("status") == 501
        and by.get("S4-connector.invoke-wired-404", {}).get("status") == 404
    )
    audit = out["audit_rows"]
    has_allow = any(
        r["decision"] == "ALLOW" and r["scope"] == DB_QUERY for r in audit
    )
    has_scope_deny = any(r["decision"] == "DENY" and r["reason"] == "scope_denied" for r in audit)
    has_tenant_deny = any(
        r["decision"] == "DENY" and r["reason"] == "tenant_mismatch" for r in audit
    )
    # 改竄トークンの DENY は plugin_id="?"(検証前)で記録される。テナント抽出で証跡に含める。
    has_invalid_deny = any(
        r["decision"] == "DENY" and r["reason"] == "invalid_token" for r in audit
    )
    return bool(
        ok
        and ok_wired
        and has_allow
        and has_scope_deny
        and has_tenant_deny
        and has_invalid_deny
    )


def main() -> int:
    marker = sys.argv[1] if len(sys.argv) > 1 else "spike07-default"
    t1, t2 = _require_env()
    ensure_tables()
    ensure_sample_table()
    cleanup(t1, t2)
    out = run(t1, t2)
    out["marker"] = marker
    out["pass"] = _check(out)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
