"""NL2SQLチャット(SQL-02)。SQL Search(generateSqlFromNl) + 読取専用実行。

SPIKE-04/SQL-01実機確定:
- NL2SQL APIは /20260325 (inference host)。同期応答はjobOutput.content、
  非同期にフォールバックする場合は sqlGenerationJobs をポーリング
- 実行はJETUSE_QUERY(CREATE SESSIONのみ)で多層ガード
"""

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

import httpx
import oracledb

# SQLサニタイズ(_BANNED / sanitize_sql / SqlRejectedError)は jetuse_shared に一本化(P1b)。
# 後方互換: 同名で再エクスポートし、既存の except SqlRejectedError / import を維持する。
from jetuse_shared.charting import propose_chart
from jetuse_shared.sqlguard import (  # noqa: F401
    _BANNED,
    SqlRejectedError,
    sanitize_sql,
    strip_code_fences,
)

from .db import _wallet_dir  # ウォレット取得を共用
from .genai import _signer
from .settings import get_settings

logger = logging.getLogger("jetuse.nl2sql")

GENERATE_TIMEOUT = 120.0
EXECUTE_TIMEOUT_MS = 30_000
# DBMS_CLOUD_AI.CREATE_PROFILE/DROP_PROFILE は遅いことがあるため専用の長めのタイムアウト
PROFILE_DDL_TIMEOUT_MS = 90_000
MAX_ROWS = 200
MAX_CELL_CHARS = 300

_query_pool: oracledb.ConnectionPool | None = None
_lock = threading.Lock()


def _base() -> str:
    s = get_settings()
    return f"https://inference.generativeai.{s.oci_region}.oci.oraclecloud.com/20260325"


def generate_sql(question: str) -> str:
    """SemanticStoreでNL→SQL生成(同期/非同期両対応)。実測30秒前後"""
    s = get_settings()
    if not s.semstore_ocid:
        raise RuntimeError("SEMSTORE_OCID is not configured")
    with httpx.Client(auth=_signer(), timeout=GENERATE_TIMEOUT) as client:
        res = client.post(
            f"{_base()}/semanticStores/{s.semstore_ocid}/actions/generateSqlFromNl",
            json={"inputNaturalLanguageQuery": question},
        )
        res.raise_for_status()
        job = res.json()
        for _ in range(24):
            out = job.get("jobOutput") or {}
            if out.get("content"):
                return out["content"]
            state = job.get("lifecycleState")
            if state == "FAILED":
                raise RuntimeError(f"sql generation failed: {job.get('lifecycleDetails')}")
            if state in (None, "SUCCEEDED", "CANCELED"):
                raise RuntimeError("sql generation returned no SQL")
            time.sleep(5)
            job = client.get(
                f"{_base()}/semanticStores/{s.semstore_ocid}/sqlGenerationJobs/{job['id']}"
            ).json()
    raise RuntimeError("sql generation timed out")


SELECT_AI_PROFILE = "JETUSE_SQL_AI"

# Select AIプロファイルで選択できるモデル(OCIネイティブモデルID)。
# 大阪オンデマンドで実機確認済みのものに限定(ADR-0001)。先頭が既定。
SELECT_AI_MODELS: list[dict[str, str]] = [
    {"key": "meta.llama-3.3-70b-instruct", "label": "Llama 3.3 70B（既定・高速）"},
    {"key": "cohere.command-a-03-2025", "label": "Cohere Command A"},
]
DEFAULT_SELECT_AI_MODEL = SELECT_AI_MODELS[0]["key"]
_VALID_SELECT_AI_MODELS = {m["key"] for m in SELECT_AI_MODELS}
# 後方互換(既存参照): 既定モデル定数
SELECT_AI_MODEL = DEFAULT_SELECT_AI_MODEL

# 作成済みSHプロファイルのモデルをプロセス内に記録(モデル変更時に作り直すため)
_sh_profile_models: dict[str, str] = {}
_profile_lock = threading.Lock()


def resolve_select_ai_model(model: str | None) -> str:
    """allowlist検証。未知/未指定は既定モデルにフォールバックする。"""
    return model if model in _VALID_SELECT_AI_MODELS else DEFAULT_SELECT_AI_MODEL


def model_tag(model: str) -> str:
    return hashlib.sha1(model.encode()).hexdigest()[:6].upper()


def _sh_profile_for_model(model: str) -> str:
    """SH対象のモデル別プロファイル名(既定モデルは従来名のまま=後方互換)。"""
    if model == DEFAULT_SELECT_AI_MODEL:
        return SELECT_AI_PROFILE
    return f"{SELECT_AI_PROFILE}_{model_tag(model)}"


def create_profile(cur, prof: str, model: str, object_list: list[dict]) -> None:
    """DBMS_CLOUD_AI プロファイルを(あれば作り直して)作成する共通ヘルパ。

    credential JETUSE_OCI_CRED と DBMS_CLOUD_AI 権限は ops/setup-select-ai.py 済み前提。
    DROP/CREATE_PROFILE は既定の call_timeout(10s)を超えることがある(特にADB再開直後)ため、
    この接続のタイムアウトを一時的に引き上げる。
    """
    s = get_settings()
    try:
        cur.connection.call_timeout = PROFILE_DDL_TIMEOUT_MS
    except Exception:  # noqa: BLE001
        pass
    try:
        cur.execute("BEGIN DBMS_CLOUD_AI.DROP_PROFILE(:p); END;", p=prof)
    except Exception:  # noqa: BLE001
        pass
    cur.execute(
        "BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;",
        p=prof,
        a=json.dumps({
            "provider": "oci",
            "credential_name": s.select_ai_credential,
            "region": s.oci_region,
            "model": model,
            "object_list": object_list,
            "comments": "true",
        }),
    )


def _ensure_select_ai_profile(model: str) -> str:
    """SH対象のSelect AIプロファイルを指定モデルで遅延作成し、プロファイル名を返す。"""
    prof = _sh_profile_for_model(model)
    with _profile_lock:
        if _sh_profile_models.get(prof) == model:
            return prof
        from .db import connect

        with connect() as conn:
            cur = conn.cursor()
            create_profile(cur, prof, model, [{"owner": TARGET_SCHEMA}])
            conn.commit()
        _sh_profile_models[prof] = model
    return prof


def generate_sql_select_ai(
    question: str, profile_name: str | None = None, model: str | None = None
) -> str:
    """Select AI(NL2SQL) showsqlでSQL生成(SQL-04比較モード / ENH-01: データセット用プロファイル)。

    model はモデル選択(feedback 20260620 #3)。SH対象はモデル別プロファイルを遅延作成。
    データセット対象は profile_name を渡す(モデル整合は datasets 側で担保)。
    """
    from .db import connect

    model = resolve_select_ai_model(model)
    prof = profile_name or _ensure_select_ai_profile(model)
    with connect() as conn:
        conn.call_timeout = 60_000
        cur = conn.cursor()
        cur.execute(
            """SELECT DBMS_CLOUD_AI.GENERATE(
                 prompt => :q, profile_name => :p, action => 'showsql') FROM dual""",
            q=question, p=prof,
        )
        raw = cur.fetchone()[0] or ""
    # コードフェンス等の除去(jetuse_shared に一本化)
    sql = strip_code_fences(raw)
    if not sql:
        raise RuntimeError("Select AIがSQLを返しませんでした")
    return sql


def _get_query_pool() -> oracledb.ConnectionPool:
    """JETUSE_QUERY(読取専用)の小プール"""
    global _query_pool
    if _query_pool is None:
        with _lock:
            if _query_pool is None:
                s = get_settings()
                wd = _wallet_dir(s)
                _query_pool = oracledb.create_pool(
                    user=s.adb_query_user,
                    password=s.adb_query_password,
                    dsn=s.adb_dsn,
                    config_dir=wd,
                    wallet_location=wd,
                    wallet_password=s.adb_wallet_password,
                    min=0,
                    max=2,
                    tcp_connect_timeout=5.0,
                    getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                    wait_timeout=15000,
                    ping_interval=30,
                )
    return _query_pool


TARGET_SCHEMA = "SH"


def suggest_chart(question: str, columns: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """結果表に適したチャートをLLM(llama=高速)に提案させる(SQL-03)。

    返り値: {type, x, y, title, reason}。不適なら type="none"。
    提案・検証ロジックは jetuse_shared.charting.propose_chart に一本化(sample-app の chart
    capability と共有)。ここではモデル/接続を束ねた generate コールバックを渡すだけ。
    """
    from .chat import complete_once

    return propose_chart(
        lambda prompt: complete_once(
            "llama-3.3-70b", [{"role": "user", "content": prompt}], max_chars=1000
        ),
        question,
        columns,
        rows,
    )

# SHサンプルにはコメントが薄いため日本語説明を補完(デモ用キュレーション — SQL-02b)
_TABLE_DESCRIPTIONS_JA = {
    "SALES": "売上明細（ファクト表。金額・数量を商品/顧客/日付/チャネル別に記録）",
    "CUSTOMERS": "顧客マスタ（氏名・性別・生年・所在地など）",
    "PRODUCTS": "商品マスタ（商品名・カテゴリ・定価など）",
    "TIMES": "日付ディメンション（日・月・四半期・年度）",
    "CHANNELS": "販売チャネル（店舗/インターネット/パートナー等）",
    "PROMOTIONS": "プロモーション（キャンペーン名・期間・コスト）",
    "COUNTRIES": "国・地域マスタ",
    "COSTS": "商品別コスト（単価原価）",
    "SUPPLEMENTARY_DEMOGRAPHICS": "顧客の補足属性（デモグラフィック）",
}

_schema_cache: dict | None = None


def get_schema_info() -> dict[str, Any]:
    """対象スキーマのテーブル/カラム情報(UI表示用 — SQL-02b)。プロセス内キャッシュ"""
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache
    with _get_query_pool().acquire() as conn:
        conn.call_timeout = EXECUTE_TIMEOUT_MS
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.table_name, c.comments, t.num_rows
            FROM all_tables t
            LEFT JOIN all_tab_comments c
              ON c.owner = t.owner AND c.table_name = t.table_name
            WHERE t.owner = :o ORDER BY t.table_name
            """,
            o=TARGET_SCHEMA,
        )
        tables = {
            r[0]: {
                "name": r[0],
                "comment": _TABLE_DESCRIPTIONS_JA.get(r[0]) or r[1] or "",
                "rows": int(r[2]) if r[2] is not None else None,
                "columns": [],
            }
            for r in cur.fetchall()
        }
        cur.execute(
            """
            SELECT col.table_name, col.column_name, col.data_type, cc.comments
            FROM all_tab_columns col
            LEFT JOIN all_col_comments cc
              ON cc.owner = col.owner AND cc.table_name = col.table_name
             AND cc.column_name = col.column_name
            WHERE col.owner = :o ORDER BY col.table_name, col.column_id
            """,
            o=TARGET_SCHEMA,
        )
        for tname, cname, dtype, comment in cur.fetchall():
            if tname in tables:
                tables[tname]["columns"].append(
                    {"name": cname, "type": dtype, "comment": comment or ""}
                )
    _schema_cache = {"schema": TARGET_SCHEMA, "tables": list(tables.values())}
    return _schema_cache


def preview_table(table: str, limit: int = 20) -> dict[str, Any]:
    """対象スキーマの既知テーブルの中身(サンプル行)を返す(ENH-02。read-only)。

    テーブル名は get_schema_info() の既知一覧で検証してから固定識別子で組み立てる
    (任意SQLは受け付けない=インジェクション防止)。
    """
    valid = {t["name"] for t in get_schema_info()["tables"]}
    name = (table or "").strip().upper()
    if name not in valid:
        raise SqlRejectedError(f"未知のテーブル: {table}")
    n = max(1, min(int(limit), MAX_ROWS))
    return execute_readonly(f'SELECT * FROM "{TARGET_SCHEMA}"."{name}" FETCH FIRST {n} ROWS ONLY')


_SCHEMA_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]*")


def execute_readonly(sql: str, current_schema: str | None = None) -> dict[str, Any]:
    """読取専用ユーザーで実行し、行数上限・タイムアウト付きで結果を返す。

    current_schema を渡すと実行接続の CURRENT_SCHEMA を当該スキーマへ固定し、非修飾
    テーブル名を確実にそのスキーマの物理表へ解決させる(synonym 依存や読取ユーザ側の
    同名オブジェクトに左右されない / SBA-03 B1)。識別子は厳格に検証し、不正値は拒否。

    プールは既存 `/api/dbchat/execute` 等と共有のため、固定は当該実行に限定する。本文実行後は
    必ず接続ユーザ自身のスキーマへ戻し、後続の current_schema 未指定呼び出しに JETUSE_SBA03 等の
    解決が残留しないようにする(プール接続の状態漏れ防止 / 後方互換)。復元に失敗した接続は汚染
    状態のままプールへ戻さず破棄する(`pool.drop`)。これにより固定が残留した接続が再利用される経路を
    断つ。`with ... acquire()` ではなく明示 acquire/close なのは、復元失敗時に close(返却)ではなく
    drop(破棄)へ分岐するため。
    """
    cleaned = sanitize_sql(sql)
    if current_schema is not None:
        if not _SCHEMA_IDENT_RE.fullmatch(current_schema):
            raise SqlRejectedError(f"不正なスキーマ識別子: {current_schema}")
    pool = _get_query_pool()
    conn = pool.acquire()
    tainted = False
    try:
        conn.call_timeout = EXECUTE_TIMEOUT_MS
        cur = conn.cursor()
        if current_schema is not None:
            cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {current_schema}")
        try:
            cur.execute(cleaned)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
            truncated = len(rows) > MAX_ROWS
            return {
                "columns": columns,
                "rows": [
                    ["" if c is None else str(c)[:MAX_CELL_CHARS] for c in r]
                    for r in rows[:MAX_ROWS]
                ],
                "row_count": min(len(rows), MAX_ROWS),
                "truncated": truncated,
            }
        finally:
            # 固定した CURRENT_SCHEMA を接続ユーザ自身へ戻してからプールへ返す(残留防止)。
            # call_timeout は固定 SQL のため超過しない。username は接続ユーザ=有効な識別子。
            if current_schema is not None:
                try:
                    cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {conn.username}")
                except Exception:  # 復旧失敗 → 本来の結果/例外は覆い隠さず、接続は破棄対象に印
                    tainted = True
                    logger.exception(
                        "failed to restore CURRENT_SCHEMA; dropping tainted pooled connection"
                    )
    finally:
        # 復元成功 → close でプールへ返却。復元失敗(汚染)→ drop でプールから破棄。
        if tainted:
            try:
                pool.drop(conn)
            except Exception:
                logger.exception("failed to drop tainted pooled connection")
        else:
            conn.close()
