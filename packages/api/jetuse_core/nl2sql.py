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
# SqlBoundaryError / enforce_sql_boundary は層2の fail-closed SQL ゲート(specs/18 §4.3)。
from jetuse_shared.sqlguard import (  # noqa: F401
    _BANNED,
    SqlBoundaryError,
    SqlRejectedError,
    enforce_sql_boundary,
    sanitize_sql,
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
        raise RuntimeError(
            "SemanticStore 未構成(SEMSTORE_OCID)。Select AI を使うか構成してください"
        )
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
    try:
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
    except oracledb.DatabaseError as e:
        # PORT-02: クレデンシャル/DG権限未整備がここに集約して落ちる。原因ヒントを付す。
        raise RuntimeError(
            f"Select AI プロファイル作成に失敗しました。DBMS_CLOUD_AI のクレデンシャル"
            f"({s.select_ai_credential})が未整備の可能性があります。動的グループへの"
            "generative-ai-family 権限、Object Storage バケットの read 権限、および"
            "ENABLE_RESOURCE_PRINCIPAL の起動ログ(/api/health)を確認してください"
        ) from e


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
    # コードフェンス等の除去
    sql = re.sub(r"^```(sql)?\s*|\s*```$", "", raw.strip(), flags=re.I | re.M).strip()
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

CHART_TYPES = ("bar", "line", "pie", "none")


def suggest_chart(question: str, columns: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """結果表に適したチャートをLLM(llama=高速)に提案させる(SQL-03)。

    返り値: {type, x, y, title, reason}。不適なら type="none"。
    LLM出力はJSONで受け、列名の実在チェックで検証する。
    """
    from .chat import complete_once

    sample = "\n".join(",".join(r) for r in rows[:15])
    prompt = (
        "あなたはデータ可視化アシスタントです。以下のSQL実行結果に最適なグラフを"
        "JSONだけで提案してください。説明文は不要です。\n"
        f'形式: {{"type": "bar|line|pie|none", "x": "X軸の列名", '
        f'"y": ["数値列名"], "title": "グラフタイトル(日本語)", "reason": "選定理由(短く)"}}\n'
        "ルール: 時系列はline、カテゴリ比較はbar、構成比(5件程度まで)はpie、"
        'グラフ化に不適(数値列がない等)なら {"type": "none", "reason": "..."}。\n\n'
        f"元の質問: {question}\n列: {', '.join(columns)}\n"
        f"データ(先頭{min(len(rows), 15)}行):\n{sample}"
    )
    raw = complete_once("llama-3.3-70b", [{"role": "user", "content": prompt}], max_chars=1000)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"type": "none", "reason": "提案の解析に失敗しました"}
    try:
        spec = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"type": "none", "reason": "提案の解析に失敗しました"}
    if spec.get("type") not in CHART_TYPES:
        return {"type": "none", "reason": "未対応のグラフ種別が提案されました"}
    if spec["type"] != "none":
        if spec.get("x") not in columns:
            return {"type": "none", "reason": "提案されたX軸列が結果に存在しません"}
        ys = [c for c in (spec.get("y") or []) if c in columns]
        if not ys:
            return {"type": "none", "reason": "提案された数値列が結果に存在しません"}
        spec["y"] = ys
    return {
        "type": spec["type"],
        "x": spec.get("x"),
        "y": spec.get("y", []),
        "title": str(spec.get("title") or "")[:100],
        "reason": str(spec.get("reason") or "")[:200],
    }

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


def sh_sample_status() -> dict[str, Any]:
    """SHサンプルスキーマが読めるか(PORT-02)。bootstrapはSHへのgrantをしないため、
    未整備ADBではALL_TABLESが空になり従来はサイレントに空表示していた — ここで検出する。
    """
    if get_schema_info()["tables"]:
        return {"available": True}
    return {
        "available": False,
        "reason": (
            "SHサンプルスキーマが読み取れません(ADBのSHスキーマがPUBLIC公開されていない"
            "可能性)。CSVデータセット取り込み(datasets)を利用してください"
        ),
    }


def preview_table(table: str, limit: int = 20, *, owner_key: str | None = None) -> dict[str, Any]:
    """対象スキーマの既知テーブルの中身(サンプル行)を返す(ENH-02。read-only)。

    テーブル名は get_schema_info() の既知一覧で検証してから固定識別子で組み立てる
    (任意SQLは受け付けない=インジェクション防止)。owner_key は層2ゲートの呼び出し元
    契約(specs/18 §4.3)= キーワード専用(既存の 1〜2 位置引数呼び出しと後方互換)。
    """
    valid = {t["name"] for t in get_schema_info()["tables"]}
    name = (table or "").strip().upper()
    if name not in valid:
        raise SqlRejectedError(f"未知のテーブル: {table}")
    n = max(1, min(int(limit), MAX_ROWS))
    return execute_readonly(
        f'SELECT * FROM "{TARGET_SCHEMA}"."{name}" FETCH FIRST {n} ROWS ONLY',
        owner_key=owner_key,
    )


def execute_readonly(sql: str, owner_key: str | None = None) -> dict[str, Any]:
    """読取専用ユーザーで実行し、行数上限・タイムアウト付きで結果を返す。

    owner_key は呼び出し元契約(specs/18 §4.3 — 導出ヘルパー経由の owner キーまたは
    DemoContext.namespace)。既定 None は owner なしモード(agent 経路・SH 等の固定
    スキーマ照会)で **fail-closed**: 層2ゲートが JETUSE_DS_ 参照を全拒否し、dataset 表は
    VPD の default-deny で必ず 0 行。既定値は公開シグネチャの後方互換のため維持する
    (codex review-4 M002 — `execute_readonly(sql)` を TypeError にしない。省略時は最も安全な
    owner なしモード)。

    VPD コンテキスト契約(specs/18 §4.3): owner_key があれば SQL の parse 前に必ず
    そのリクエストの owner で SET_CONTEXT を上書きし、設定失敗時は SQL を実行しない。
    finally で CLEAR_CONTEXT してから接続を返却する(プール接続は再利用されるため、
    clear に失敗した接続はプールへ返さず破棄する)。
    """
    from . import vpd  # 遅延 import(循環回避)
    from .owner_keys import owner_key_gate

    # 静的 allowlist 拒否(DB 非接触)を先に済ませ、DB へ渡す前に VPD 完全性を必須化する
    # (specs/18 §4.3 — ポリシー欠落状態での fail-open を塞ぐ中央ゲート。SH 固定表照会も
    # 含め、実際に SQL を実行する全経路がこのゲートを通る)。
    cleaned = sanitize_sql(sql)
    # owner キー移行ゲートを登録簿参照・VPD 設定より前に通す(review-11 B003): route
    # だけでなく Fn 経路(func.py)も execute_readonly を直接呼ぶため、共有チョークポイント
    # で必須化する。未分類の予約接頭辞行が残る間は 503 で DB へ到達させない(legacy owner
    # 衝突での越境読取を塞ぐ)。owner なしモード(agent/SH 固定照会)は対象外。
    if owner_key is not None:
        owner_key_gate()
    vpd.integrity_gate()
    # 層2 fail-closed SQL ゲート(specs/18 §4.3 — allowlist 方式): FROM/JOIN のテーブル参照は
    # SH スキーマ(修飾)・当人の登録済み DS 表・DUAL・CTE だけを許可し、それ以外(未知
    # synonym・別スキーマ・辞書ビュー・table function)は一律拒否。SH 照会は SemanticStore /
    # Select AI とも常に `SH.<表>` 修飾で生成されるため素名の許可は不要(gate 側で SH 修飾を許可)。
    # JETUSE_DS_ の登録簿照合は SQL が言及するときだけ引く。
    allowed: set[str] = set()
    if owner_key is not None and re.search(r"jetuse_ds_", cleaned, re.I):
        from . import datasets  # 遅延 import(datasets → nl2sql の循環回避)

        allowed = {t.upper() for t in datasets.owner_ds_tables(owner_key)}
    enforce_sql_boundary(cleaned, allowed_tables=allowed,
                         app_schema=get_settings().adb_user)
    pool = _get_query_pool()
    conn = pool.acquire()
    drop = False
    try:
        conn.call_timeout = EXECUTE_TIMEOUT_MS
        if owner_key is not None:
            try:
                vpd.set_owner_context(conn, owner_key)
            except Exception:
                # SET_CONTEXT 失敗時は SQL を実行せず、context 残留の恐れがある接続を破棄する
                drop = True
                raise
        try:
            cur = conn.cursor()
            cur.execute(cleaned)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
        finally:
            if owner_key is not None:
                try:
                    vpd.clear_owner_context(conn)
                except Exception:
                    drop = True  # コンテキスト残留の越境を防ぐ: この接続は再利用しない
                    logger.exception("clear_owner_context failed (dropping connection)")
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
        try:
            # oracledb の pooled connection の返却は pool.release(conn)(conn.release は無い)
            if drop:
                pool.drop(conn)
            else:
                pool.release(conn)
        except Exception:  # noqa: BLE001
            logger.exception("query connection return failed")
