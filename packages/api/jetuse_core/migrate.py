"""マイグレーションランナー(CHAT-02)。

jetuse_core/migrations/*.sql を辞書順に適用し、SCHEMA_MIGRATIONS に記録する。
実行: python -m jetuse_core.migrate  (JETUSE_APPユーザーで接続)
SQLファイルは ';' 終端の単文の並び(PL/SQLブロック非対応の簡易版)。
"""

import pathlib

from .db import get_pool

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

# 冪等性は version スキップ(migrate 内の `if version in done: continue`)で担保する。
# 適用済み migration は再実行で実行されず `python -m jetuse_core.migrate` の再実行は no-op になる。
# (重複作成エラーを無条件に握ると互換性のない既存オブジェクトを見逃すため握らない。部分適用の
#  途中失敗からの復旧は、隔離スキーマを破棄→再作成する運用に委ねる。)


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


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
            for stmt in _statements(f.read_text()):
                cur.execute(stmt)
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES (:v)", v=version
            )
            conn.commit()
            applied.append(version)
    return applied


if __name__ == "__main__":
    done = migrate()
    print(f"applied: {done or '(none — up to date)'}")
