"""マイグレーションランナー(CHAT-02 / 再実行許容は SP2-01)。

jetuse_core/migrations/*.sql を辞書順に適用し、SCHEMA_MIGRATIONS に記録する。
実行: python -m jetuse_core.migrate  (JETUSE_APPユーザーで接続)
SQLファイルは ';' 終端の単文の並び(PL/SQLブロック非対応の簡易版)。

不足の検出(ER-0015): 配備したイメージが要求する migration が DB に無いと、アプリは
必要な表の無い DB に向いたまま正常起動し、DB 系だけが 503 になる。判定材料は
`pending_versions`(イメージ − DB)で、`/api/health` の `schema` として出す。
別系統の checkout から流した場合(DB − checkout)は、このランナーが適用前に警告する。

再実行許容(specs/18 §1.1): 「DDL 成功 → version 記録前クラッシュ」で記録なしの適用済み DDL が
残ると、再実行が ORA-01430/00955/01408 で停止する。既適用を示唆する ORA コードを検知したら、
ORA コードだけで成功と断定せず、その migration の期待事後条件をデータディクショナリで
完全一致検証(_EXPECTED_POST)してから version を記録する。形違いは停止して人間対応。
"""

import logging
import pathlib
import re
from collections.abc import Iterable

import oracledb

from .db import connect, get_pool

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

logger = logging.getLogger("jetuse.migrate")

# 警告に並べる版の数。全部並べると本文が流れて読まれない。
_SHOW = 5

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
    # RAGM-02 の 017〜019(main 由来)。dev と番号が衝突しているが version は stem なので
    # 別行として共存する(SYNC-01)。ここに登録しないと、この 3 件だけ再実行耐性が効かない
    # (DDL 成功 → version 記録前クラッシュ → 再実行が ORA-00955 で停止・人間対応)。
    "017_rag_adb": {
        "columns": {
            ("RAG_ADB_CHUNKS", "CHUNK_ID"): ("VARCHAR2", 128, "B", "N", None),
            ("RAG_ADB_CHUNKS", "OWNER_SUB"): ("VARCHAR2", 255, "B", "N", None),
            ("RAG_ADB_CHUNKS", "FILE_ID"): ("VARCHAR2", 36, "B", "N", None),
            ("RAG_ADB_CHUNKS", "CHUNK_NO"): ("NUMBER", None, None, "N", "0"),
            ("RAG_ADB_CHUNKS", "DOC_FILE"): ("VARCHAR2", 400, "B", "N", None),
            ("RAG_ADB_CHUNKS", "DOC_VERSION"): ("VARCHAR2", 32, "B", "N", "'1.0'"),
            ("RAG_ADB_CHUNKS", "SHEET_NAME"): ("VARCHAR2", 128, "B", "Y", None),
            ("RAG_ADB_CHUNKS", "CELLS"): ("VARCHAR2", 64, "B", "Y", None),
            ("RAG_ADB_CHUNKS", "SHA256"): ("VARCHAR2", 64, "B", "N", None),
            ("RAG_ADB_CHUNKS", "KIND"): ("VARCHAR2", 32, "B", "N", "'doc'"),
            ("RAG_ADB_CHUNKS", "CURRENT_VERSION"): ("CHAR", 1, "B", "N", "'Y'"),
            ("RAG_ADB_CHUNKS", "BODY"): ("CLOB", None, None, "N", None),
            ("RAG_ADB_CHUNKS", "CREATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
        },
        "checks": {"RAG_ADB_CHUNKS": ["current_version IN ('Y', 'N')"]},
        "primary_keys": {"RAG_ADB_CHUNKS": ["CHUNK_ID"]},
    },
    "018_rag_adb_docs": {
        "columns": {
            ("RAG_ADB_DOCS", "OWNER_SUB"): ("VARCHAR2", 255, "B", "N", None),
            ("RAG_ADB_DOCS", "DOC_FILE"): ("VARCHAR2", 400, "B", "N", None),
            ("RAG_ADB_DOCS", "DOC_VERSION"): ("VARCHAR2", 32, "B", "N", "'0.0'"),
            ("RAG_ADB_DOCS", "UPDATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
        },
        "primary_keys": {"RAG_ADB_DOCS": ["OWNER_SUB", "DOC_FILE"]},
    },
    "019_rag_adb_ingest": {
        "columns": {
            ("RAG_ADB_INGEST", "OWNER_SUB"): ("VARCHAR2", 255, "B", "N", None),
            ("RAG_ADB_INGEST", "FILE_ID"): ("VARCHAR2", 36, "B", "N", None),
            ("RAG_ADB_INGEST", "DOC_KEY"): ("VARCHAR2", 400, "B", "N", None),
            ("RAG_ADB_INGEST", "STATUS"): ("VARCHAR2", 20, "B", "N", "'pending'"),
            ("RAG_ADB_INGEST", "CHUNKS"): ("NUMBER", None, None, "N", "0"),
            ("RAG_ADB_INGEST", "ERROR"): ("VARCHAR2", 1000, "B", "Y", None),
            ("RAG_ADB_INGEST", "UPDATED_AT"): (
                "TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"
            ),
        },
        "primary_keys": {"RAG_ADB_INGEST": ["OWNER_SUB", "FILE_ID"]},
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


def checkout_versions() -> list[str]:
    """いまの checkout が持つ migration の版(ファイル名の stem)。"""
    return sorted(f.stem for f in MIGRATIONS_DIR.glob("*.sql"))


def applied_versions() -> list[str]:
    """DB に適用済みの版。`schema_migrations` がまだ無ければ空を返す。

    migration を**適用せずに**状態だけ読む。`/api/health` から呼ぶための入口
    (診断のために DDL を流してしまっては本末転倒)。

    接続は `db.connect()`(= `call_timeout` 付き)を使う。`migrate()` 側が生の
    `get_pool().acquire()` なのは適用に時間がかかるためで、**診断はその例外に
    乗せてはいけない** —— ADB 停止時に `/api/health` 自体が返らなくなる(CHAT-07)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_MIGRATIONS'
        """)
        if cur.fetchone()[0] == 0:
            return []
        cur.execute("SELECT version FROM schema_migrations")
        return sorted(r[0] for r in cur.fetchall())


def pending_versions(db: Iterable[str], checkout: Iterable[str]) -> list[str]:
    """この checkout(コンテナ内ではイメージ)が持っているのに、DB へ適用されていない版。

    **ER-0015 を検出するのはこちら。** 実害時の状態は「DB = Public 集合 / 配備したのは
    Internal 版」だった。DB と migration ランナーの checkout だけを見比べても差は 0 で、
    どちらの側にも「この環境は Internal を要求している」と書いていない。
    判断材料は**動いているアプリのイメージ**にしかない —— イメージが持つ migration が
    DB に無ければ、そのアプリは必要な表の無い DB に向いている。
    """
    return sorted(set(checkout) - set(db))


def foreign_versions(db: Iterable[str], checkout: Iterable[str]) -> list[str]:
    """DB に適用済みだが、いまの checkout に**存在しない**版。

    `pending_versions` とは別の取り違えを見る。こちらが空でないなら、その DB は
    いまの checkout より多くの migration を持つ系統(例: Internal)で育てられている。
    Public 系の checkout から流しても不足分は作られないので、警告して止める材料になる。

    **これだけでは ER-0015 は塞がらない**(実害時はこの差も 0 だった)。塞ぐのは
    `pending_versions` を `/api/health` から見る経路。
    """
    return sorted(set(db) - set(checkout))


def foreign_warning(db: Iterable[str], checkout: Iterable[str]) -> str | None:
    """`foreign_versions` を人が読める警告文にする。該当が無ければ None。

    **言い切れることだけを書く。** ここで分かるのは「DB のほうが先を行っている」まで。
    この checkout が持つ版はすべて DB にあるので、**壊れているとは限らない**
    (後方互換な旧イメージを先行 DB に向ける運用は成り立つ)。不足を断定できるのは
    `pending_versions` を見たときだけなので、503 には言及しない。
    """
    extra = foreign_versions(db, checkout)
    if not extra:
        return None
    shown = ", ".join(extra[:_SHOW]) + (" ほか" if len(extra) > _SHOW else "")
    # 端末に出る文字列なので Markdown の強調記号は使わない(そのまま `**` が見える)。
    return (
        f"この DB には、いまの checkout に無い migration が {len(extra)} 件適用されている"
        f"({shown})。DB のほうが先を行っており、系統の取り違え"
        "(例: Internal で育てたスキーマを Public 系の checkout から流している)か、"
        "意図的に古いイメージを向けているかのどちらか。"
        "配備しているイメージとこの checkout が対応しているか確かめること。"
        "Internal 機能を使う環境なら internal 系の checkout から流し直す。"
    )


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
        # 適用の前に出す。適用が 0 件で終わったときこそ読ませたい警告なので、
        # 「何かを適用したときだけ何か出る」経路に置いてはいけない。
        warning = foreign_warning(done, checkout_versions())
        if warning:
            logger.warning("%s", warning)
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
    # 人が読む経路。`jetuse_core.logging.configure()` の JSON ではなく素の1行で出す
    # (警告を見落とすと ER-0015 の「流したのに動かない」に戻る)。
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    done = migrate()
    # **「up to date」と言い切らない。** 別系統の checkout から流すと 0 件で正常終了するため、
    # 0 件を「最新」と断定すると ER-0015 の誤解をこの行が再生産する。
    # 判断材料は直前の WARNING に出る。
    print(f"applied: {done or '(none)'}")
