"""マイグレーションランナー(CHAT-02 / 再実行許容は SP2-01)。

jetuse_core/migrations/*.sql を辞書順に適用し、SCHEMA_MIGRATIONS に記録する。
実行: python -m jetuse_core.migrate  (JETUSE_APPユーザーで接続)
SQLファイルは ';' 終端の単文の並び(PL/SQLブロック非対応の簡易版)。

再実行許容(specs/18 §1.1): 「DDL 成功 → version 記録前クラッシュ」で記録なしの適用済み DDL が
残ると、再実行が ORA-01430/00955/01408 で停止する。既適用を示唆する ORA コードを検知したら、
ORA コードだけで成功と断定せず、その migration の期待事後条件をデータディクショナリで
完全一致検証(_EXPECTED_POST)してから version を記録する。形違いは停止して人間対応。
"""

import pathlib
import re

import oracledb

from .db import get_pool

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

# 既適用を示唆する ORA: 1430=列が既存 / 955=名前が既存 / 1408=列リストが索引済み
_ALREADY_APPLIED_ORA = {1430, 955, 1408}

# 期待事後条件(specs/18 §1.1 の 017〜021、specs/19 §2.1 の 025〜026)。
# columns: {(TABLE, COLUMN): (DATA_TYPE, CHAR_LENGTH, CHAR_USED, NULLABLE, DATA_DEFAULT)}
#   CHAR_LENGTH/CHAR_USED が None の型(CLOB/TIMESTAMP)は長さセマンティクスなし = 比較対象外。
# checks: {TABLE: [search_condition, ...]}(空白正規化して存在を要求)
# indexes: {INDEX: (TABLE, [COLUMN, ...])}(列は position 順の完全一致)
# primary_keys: {TABLE: [COLUMN, ...]}(ENABLED/VALIDATED の PK が position 順で完全一致 —
#   同名テーブルが PK 欠落のまま「適用済み」と誤記録されるのを防ぐ。review-1 M001)
_EXPECTED_POST: dict[str, dict] = {
    "017_demos_v2": {
        "columns": {
            ("DEMOS", "DESCRIPTION"): ("VARCHAR2", 1000, "C", "Y", None),
            ("DEMOS", "CONFIG"): ("CLOB", None, None, "N", "'{}'"),
            ("DEMOS", "STATUS"): ("VARCHAR2", 20, "B", "N", "'ready'"),
            ("DEMOS", "UPDATED_AT"): ("TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"),
        },
        "checks": {
            "DEMOS": [
                "config IS JSON",
                "status IN ('provisioning','ready','failed','deleting')",
            ]
        },
    },
    "018_demos_idx_owner": {
        "indexes": {"IDX_DEMOS_OWNER": ("DEMOS", ["OWNER_SUB", "UPDATED_AT"])}
    },
    "019_demos_idx_visibility": {
        "indexes": {"IDX_DEMOS_VISIBILITY": ("DEMOS", ["VISIBILITY"])}
    },
    "020_conversations_demo_id": {
        "columns": {("CONVERSATIONS", "DEMO_ID"): ("VARCHAR2", 36, "B", "Y", None)}
    },
    "021_conversations_idx_demo": {
        "indexes": {"IDX_CONV_DEMO": ("CONVERSATIONS", ["DEMO_ID"])}
    },
    "022_demo_backend_targets": {
        "columns": {
            ("DEMO_BACKEND_TARGETS", "ID"): ("VARCHAR2", 36, "B", "N", None),
            ("DEMO_BACKEND_TARGETS", "NAMESPACE"): ("VARCHAR2", 255, "B", "N", None),
            ("DEMO_BACKEND_TARGETS", "KIND"): ("VARCHAR2", 20, "B", "N", None),
            ("DEMO_BACKEND_TARGETS", "LOCATOR"): ("CLOB", None, None, "N", None),
            ("DEMO_BACKEND_TARGETS", "LOCATOR_HASH"): ("VARCHAR2", 64, "B", "N", None),
            ("DEMO_BACKEND_TARGETS", "CREATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
        },
        "checks": {
            "DEMO_BACKEND_TARGETS": [
                "kind IN ('vector_store','files','select_ai','opensearch','objectstorage')",
                "locator IS JSON",
            ]
        },
    },
    "023_dbt_idx": {
        "indexes": {"IDX_DBT_NS": ("DEMO_BACKEND_TARGETS", ["NAMESPACE"])}
    },
    "024_rag_files_filename_char": {
        "columns": {("RAG_FILES", "FILENAME"): ("VARCHAR2", 400, "C", "N", None)}
    },
    "025_builder_sessions": {
        "columns": {
            ("BUILDER_SESSIONS", "ID"): ("VARCHAR2", 36, "B", "N", None),
            ("BUILDER_SESSIONS", "OWNER_SUB"): ("VARCHAR2", 255, "B", "N", None),
            ("BUILDER_SESSIONS", "STATUS"): ("VARCHAR2", 20, "B", "N", "'hearing'"),
            ("BUILDER_SESSIONS", "TRANSCRIPT"): ("CLOB", None, None, "N", "'[]'"),
            ("BUILDER_SESSIONS", "REQUIREMENTS"): ("CLOB", None, None, "Y", None),
            ("BUILDER_SESSIONS", "PLAN"): ("CLOB", None, None, "Y", None),
            ("BUILDER_SESSIONS", "DEMO_ID"): ("VARCHAR2", 36, "B", "Y", None),
            ("BUILDER_SESSIONS", "CREATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
            ("BUILDER_SESSIONS", "UPDATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
        },
        "checks": {
            "BUILDER_SESSIONS": [
                "status IN ('hearing','designed')",
                "transcript IS JSON",
                "requirements IS JSON",
                "plan IS JSON",
            ]
        },
        "primary_keys": {"BUILDER_SESSIONS": ["ID"]},
    },
    "026_builder_sessions_idx": {
        "indexes": {"IDX_BS_OWNER": ("BUILDER_SESSIONS", ["OWNER_SUB", "UPDATED_AT"])}
    },
    # sufficient 最終判定の永続化(specs/19 §2.3・§3.1 — SP3-02 review-1 F002)
    "027_builder_sessions_sufficient": {
        "columns": {
            ("BUILDER_SESSIONS", "SUFFICIENT"): ("NUMBER", None, None, "N", "0"),
        },
        "checks": {"BUILDER_SESSIONS": ["sufficient IN (0,1)"]},
    },
}


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def _ora_code(e: Exception) -> int | None:
    """DatabaseError から ORA コードを取り出す(ドライバの _Error.code / 文字列の両対応)。"""
    err = e.args[0] if e.args else None
    code = getattr(err, "code", None)
    if code:
        return int(code)
    m = re.match(r"ORA-(\d{5})", str(err or ""))
    return int(m.group(1)) if m else None


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def _mismatch(version: str, detail: str) -> RuntimeError:
    return RuntimeError(
        f"migration {version}: 既適用を示唆する ORA を検知したが、期待事後条件と不一致: {detail}。"
        "同名で形の違うオブジェクトが存在するため停止(人間対応が必要)"
    )


def _postconditions_met(cur, version: str) -> bool:
    """期待事後条件をデータディクショナリで完全一致検証する。

    True = 完全一致(既適用と確認 → version 記録可)。
    False = この version に期待定義がない(呼び出し側は元エラーを再送出)。
    形違い(部分一致・型/長さ/列構成の不一致)は RuntimeError で停止。
    """
    expected = _EXPECTED_POST.get(version)
    if not expected:
        return False

    for (table, col), (dtype, char_len, char_used, nullable, default) in (
        expected.get("columns") or {}
    ).items():
        cur.execute(
            "SELECT data_type, char_length, char_used, nullable, data_default "
            "FROM user_tab_columns WHERE table_name = :t AND column_name = :c",
            t=table, c=col,
        )
        row = cur.fetchone()
        if not row:
            raise _mismatch(version, f"列 {table}.{col} が存在しない")
        got_default = str(row[4]).strip() if row[4] is not None else None
        got = (row[0], row[1] or None, row[2] or None, row[3], got_default)
        want = (dtype, char_len, char_used, nullable, default)
        # 長さセマンティクスのない型(CLOB/TIMESTAMP)は CHAR_LENGTH/CHAR_USED を比較しない
        if char_len is None:
            got = (got[0], None, None, got[3], got[4])
        if got != want:
            raise _mismatch(version, f"列 {table}.{col} の形が {got} (期待 {want})")

    for table, conditions in (expected.get("checks") or {}).items():
        cur.execute(
            "SELECT search_condition, status, validated FROM user_constraints "
            "WHERE table_name = :t AND constraint_type = 'C'",
            t=table,
        )
        # 同一条件でも DISABLED / NOT VALIDATED は「期待形と完全一致」でない(review-1 M001)
        existing = {_norm_ws(r[0]): (r[1], r[2]) for r in cur.fetchall() if r[0]}
        for cond in conditions:
            state = existing.get(_norm_ws(cond))
            if state is None:
                raise _mismatch(version, f"{table} の check 制約 [{cond}] が存在しない/不一致")
            if state != ("ENABLED", "VALIDATED"):
                raise _mismatch(
                    version, f"{table} の check 制約 [{cond}] が {state} (期待 ENABLED/VALIDATED)"
                )

    for table, pk_columns in (expected.get("primary_keys") or {}).items():
        cur.execute(
            "SELECT constraint_name, status, validated FROM user_constraints "
            "WHERE table_name = :t AND constraint_type = 'P'",
            t=table,
        )
        row = cur.fetchone()
        if not row:
            raise _mismatch(version, f"{table} の PRIMARY KEY が存在しない")
        name, state = row[0], (row[1], row[2])
        if state != ("ENABLED", "VALIDATED"):
            raise _mismatch(
                version, f"{table} の PRIMARY KEY が {state} (期待 ENABLED/VALIDATED)"
            )
        cur.execute(
            "SELECT column_name FROM user_cons_columns WHERE constraint_name = :cn "
            "ORDER BY position",
            cn=name,
        )
        got_cols = [r[0] for r in cur.fetchall()]
        if got_cols != pk_columns:
            raise _mismatch(
                version, f"{table} の PRIMARY KEY 列が {got_cols} (期待 {pk_columns})"
            )

    for index, (table, columns) in (expected.get("indexes") or {}).items():
        cur.execute(
            "SELECT table_name FROM user_indexes WHERE index_name = :i", i=index
        )
        row = cur.fetchone()
        if not row or row[0] != table:
            raise _mismatch(version, f"索引 {index} が {table} 上に存在しない")
        cur.execute(
            "SELECT column_name FROM user_ind_columns WHERE index_name = :i "
            "ORDER BY column_position",
            i=index,
        )
        got_cols = [r[0] for r in cur.fetchall()]
        if got_cols != columns:
            raise _mismatch(version, f"索引 {index} の列が {got_cols} (期待 {columns})")

    return True


def migrate() -> list[str]:
    applied: list[str] = []
    pool = get_pool()
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_MIGRATIONS'
        """)
        if cur.fetchone()[0] == 0:
            cur.execute("""
                CREATE TABLE schema_migrations (
                  version VARCHAR2(64) PRIMARY KEY,
                  applied_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
                )
            """)
        cur.execute("SELECT version FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = f.stem
            if version in done:
                continue
            try:
                for stmt in _statements(f.read_text()):
                    cur.execute(stmt)
            except oracledb.DatabaseError as e:
                if _ora_code(e) not in _ALREADY_APPLIED_ORA or not _postconditions_met(
                    cur, version
                ):
                    raise
                # 既適用DDLの残骸(version記録前クラッシュ)を辞書検証で確認済み → 記録のみ
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES (:v)", v=version
            )
            conn.commit()
            applied.append(version)
    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"applied: {done or '(none — up to date)'}")
