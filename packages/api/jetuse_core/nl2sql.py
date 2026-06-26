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


class SelectAiNoSqlError(RuntimeError):
    """Select AI が SQL を生成しなかった(showsql 空応答等)。

    RuntimeError サブクラス(後方互換: 既存の `except RuntimeError` でも捕捉される)。これにより
    呼び出し側は「SQL 未生成という想定内の失敗」と「未知の実装バグ由来 RuntimeError」を型で区別し、
    前者だけを推論失敗(502)へ正規化して後者は握りつぶさず露出させられる。
    """


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
        raise SelectAiNoSqlError("Select AIがSQLを返しませんでした")
    return sql


# --- 任意スキーマ対象の NL2SQL(SBA-C 売上集計。専用スキーマ JETUSE_SBA04 を照会) -------
# SBA-A/B が SH 固定なのに対し、sample-app が業務データを実 ADB の専用スキーマに隔離して持つ
# 場合に使う(タスク専用スキーマでの分離。例: JETUSE_SBA04)。プロファイルはスキーマ別に遅延作成。

#: Oracle 識別子として安全なスキーマ名のみ受け付ける(プロファイル名/object_list へ素で渡すため)。
_SCHEMA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")

#: 危険なパッケージ/関数の deny-list(動的SQL・外部アクセス・ファイル/ネットワーク・URI フェッチ)。
#: テーブル factor の検査だけでは防げない「関数経由の隔離跨ぎ/SSRF/任意SQL実行」を塞ぐ二次防御。
#: 共有 ADB では PUBLIC に EXECUTE 付与された型/パッケージ(HTTPURITYPE 等)があり得るため、
#: DB 権限(RO の最小権限)に加えてコード側でも拒否する。集計用の生成SQLでは一切使われない。
_DANGEROUS_SQL_RE = re.compile(
    r"(?i)\b("
    r"DBMS_XMLGEN|DBMS_SQL|DBMS_CLOUD\w*|DBMS_LOB|DBMS_LDAP|DBMS_PIPE|DBMS_AQ\w*|"
    r"DBMS_SCHEDULER|DBMS_JOB|DBMS_XSLPROCESSOR|DBMS_XMLSTORE|DBMS_ADVISOR|DBMS_REDACT\w*|"
    r"UTL_HTTP|UTL_TCP|UTL_SMTP|UTL_FILE|UTL_INADDR|UTL_URL|UTL_RAW|"
    r"HTTPURITYPE|DBURITYPE|HTTPURIFACTORY|GETCLOBVAL|GETBLOBVAL|"
    r"EXTRACTVALUE|XMLQUERY|XMLTABLE|XMLTYPE"
    r")\b"
)
_schema_profiles: dict[str, str] = {}

#: FROM 句/JOIN の終わり(次の上位節)を示すキーワード。FROM 領域の走査をここで止める。
_REGION_END_RE = re.compile(
    r"(?i)\b(WHERE|GROUP|ORDER|HAVING|CONNECT|START|MODEL|FETCH|OFFSET|"
    r"UNION|MINUS|INTERSECT|WINDOW)\b"
)
_FROM_RE = re.compile(r"(?i)\bFROM\b")
_JOIN_RE = re.compile(r"(?i)\bJOIN\b")
#: JOIN 以外にもテーブル factor を導入する句: `CROSS/OUTER APPLY <factor>`・`LATERAL <factor>`。
#: これらの直後も factor 位置として扱わないと、右辺の他スキーマ参照がガードを素通りする。
_APPLY_RE = re.compile(r"(?i)\bAPPLY\b")
_LATERAL_RE = re.compile(r"(?i)\bLATERAL\b")
#: factor 位置の `(` が派生表(サブクエリ)か。`(SELECT ...)` / `(WITH ...)` のみサブクエリ扱い。
#: それ以外(親括弧付き JOIN など)はテーブル参照を内包しうるため保守的に拒否する。
_PAREN_SUBQUERY_RE = re.compile(r"(?i)\(\s*(SELECT|WITH)\b")
#: `FROM` を引数構文に持つ関数(`EXTRACT(f FROM e)` / `TRIM(.. FROM s)` / ANSI `SUBSTRING`)。
#: これらの括弧内 FROM はテーブルソースではないので factor 抽出から除外する。
_FROM_FUNC_RE = re.compile(r"(?i)(EXTRACT|TRIM|SUBSTRING)\s*$")


def _from_in_function_paren(sql: str, pos: int) -> bool:
    """この `FROM`(pos=先頭位置)が EXTRACT/TRIM/SUBSTRING の関数括弧の直下にあるか。

    pos から後方に括弧バランスを取りながら最も近い未閉じ `(` を探し、その直前トークンが
    対象関数名なら True(=テーブルソースの FROM ではない)。`(SELECT ... FROM ...)` の
    サブクエリ括弧は関数名が前置しないので False のまま正しく検査される。
    """
    depth = 0
    i = pos - 1
    while i >= 0:
        c = sql[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return bool(_FROM_FUNC_RE.search(sql[:i]))
            depth -= 1
        i -= 1
    return False
#: ON / USING は join 条件の始まり。ここから次の factor 位置まではテーブルではない(列参照)。
_ONUSING_RE = re.compile(r"(?i)\b(ON|USING)\b")
#: factor 文字列を schema.table / table に分解する。
_QUALIFIED_RE = re.compile(r'^"?([A-Za-z][\w$#]*)"?\s*\.\s*"?([A-Za-z][\w$#]*)"?$')
_UNQUALIFIED_RE = re.compile(r'^"?([A-Za-z][\w$#]*)"?$')
#: factor 先頭トークン(引用識別子・通常識別子・schema.table、末尾の `@dblink` も取り込む)。
#: `@dblink` まで factor に含めることで、後段で DB リンク参照(隔離跨ぎ)を検出して拒否できる
#: (取りこぼすと `JETUSE_SBA04.SALES@LINK` が修飾済みに見えて素通りする)。
#: 注: re.match(sql, i) では `^` は pos=i に合致しないため使わない(pos から直接マッチさせる)。
#: `@dblink` の link 名は通常識別子・ドメイン修飾(`@l.domain`)・引用識別子(`@"REMOTE LINK"`)を
#: 取りうる。いずれの形でも factor に `@...` を取り込み、後段で `@` を含む factor を拒否する。
_FACTOR_HEAD_RE = re.compile(
    r'("?[A-Za-z][\w$#]*"?(?:\s*\.\s*"?[A-Za-z][\w$#]*"?)?'
    r'(?:\s*@\s*(?:"[^"]*"|[\w$#.]+))?)'
)

def _blank_string_literals(sql: str) -> str:
    """単一引用符の文字列リテラルの **中身** を空白へ置換する(長さ・引用符位置は保持)。

    `_FROM_RE`/`_JOIN_RE` の finditer はリテラル内の `FROM`/`JOIN` という語にも当たってしまうため、
    factor 抽出・CTE 名収集の前にリテラル内容を無効化して、`'... FROM SH.X ...'` のような文字列を
    テーブル参照と誤認しないようにする(`''` エスケープ対応)。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            out.append("'")
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("  ")
                        i += 2
                        continue
                    out.append("'")
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _in_double_quote(sql: str, pos: int) -> bool:
    """pos が二重引用識別子 `"..."` の内側か(単一引用リテラル無効化後の SQL 前提)。

    リテラル内の `"` は既に空白化されているので、残る `"` は識別子区切りのみ。pos までの
    `"` の個数が奇数なら引用識別子の内側=その位置の `FROM`/キーワードは予約語ではない。
    """
    return sql.count('"', 0, pos) % 2 == 1


#: WITH(CTE)句の先頭。CTE 名はクエリ内ローカル定義で実テーブルではない(非修飾でも許可する)。
_WITH_RE = re.compile(r"(?i)^\s*WITH\b")
#: CTE 名候補(引用/通常識別子)。
_IDENT_RE = re.compile(r'"?([A-Za-z][\w$#]*)"?')
#: WITH 句の終端(本体クエリの開始)を示すトップレベルキーワード。
_MAIN_KW_RE = re.compile(r"(?i)(SELECT|INSERT|UPDATE|DELETE|MERGE)\b")
#: CTE 定義は `name [(cols)] AS (...)`。AS が続く識別子だけを CTE 名として採用する。
_AS_RE = re.compile(r"(?i)AS\b")


def _followed_by_as(sql: str, pos: int, n: int) -> bool:
    """pos(識別子直後)から、任意の `(col list)` と空白を読み飛ばした次が `AS` か。

    これにより `WITH RECURSIVE r AS (...)` の `RECURSIVE`(AS が続かない)を CTE 名として
    誤収集せず、`WITH recursive AS (...)`(CTE 名 `recursive`)は正しく収集できる。
    """
    i = pos
    while i < n and sql[i].isspace():
        i += 1
    if i < n and sql[i] == "(":
        depth = 1
        i += 1
        while i < n and depth > 0:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
            i += 1
        while i < n and sql[i].isspace():
            i += 1
    return bool(_AS_RE.match(sql, i))


def _cte_names(sql: str) -> set[str]:
    """WITH 句で定義された CTE 名の集合を返す(無ければ空集合)。

    `WITH a AS (...), b (c1,c2) AS (...) SELECT ...` の `a`/`b` を、深さ 0 で WITH 直後・
    トップレベルのカンマ直後に現れる識別子として収集する。本体クエリ(トップレベルの
    SELECT/INSERT 等)に達したら打ち切る。CTE 本体 `(...)` 内の実テーブル参照は別途
    `_table_factors` が走査・検査するため、ここで CTE 名を許可しても隔離は緩まない。
    """
    sql = _blank_string_literals(sql)
    m = _WITH_RE.match(sql)
    if not m:
        return set()
    names: set[str] = set()
    i, n = m.end(), len(sql)
    depth = 0
    expecting = True  # 次の depth0 識別子が CTE 名か
    while i < n:
        c = sql[i]
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0:
            if c == ",":
                expecting = True
                i += 1
                continue
            if not c.isspace():
                if expecting:
                    im = _IDENT_RE.match(sql, i)
                    if im:
                        # `name [(cols)] AS (...)` の AS が続く識別子だけを CTE 名として採用。
                        # WITH 直後の RECURSIVE 等(AS が続かない予約語)は採用せず読み飛ばす。
                        if _followed_by_as(sql, im.end(), n):
                            names.add(im.group(1).upper())
                            expecting = False
                        i = im.end()
                        continue
                elif _MAIN_KW_RE.match(sql, i):
                    break  # 本体クエリに到達 → WITH 句の終端
        i += 1
    return names


def _table_factors(sql: str) -> list[str]:
    """SQL の全 FROM/JOIN テーブル factor を列挙する(カンマ結合・JOIN・サブクエリ網羅)。

    各 FROM キーワードから上位節(WHERE/GROUP/...)までを paren 深さ考慮で領域とし、その中で
    「factor 位置」(領域先頭・トップレベルのカンマ直後・JOIN 直後)の先頭トークンを factor とする。
    ON/USING 以降の join 条件(列参照)は factor 位置にならないので拾わない。サブクエリ
    `(SELECT ... FROM ...)` の内側 FROM も、外側ループの finditer が別途その位置から走査する。
    """
    sql = _blank_string_literals(sql)  # リテラル内の FROM/JOIN/識別子を誤検出しない
    factors: list[str] = []
    n = len(sql)
    for fm in _FROM_RE.finditer(sql):
        # EXTRACT(.. FROM ..)/TRIM(.. FROM ..) 等の関数引数の FROM はテーブルソースではない。
        if _from_in_function_paren(sql, fm.start()):
            continue
        # `"FROM"` のような引用識別子内の FROM はキーワードではない(誤走査を防ぐ)。
        if _in_double_quote(sql, fm.start()):
            continue
        i = fm.end()
        depth = 0
        expecting = True  # 次の非空白トークンが factor かどうか
        while i < n:
            if depth == 0:
                if _REGION_END_RE.match(sql, i):
                    break
                jm = _JOIN_RE.match(sql, i)
                if jm:
                    i = jm.end()
                    expecting = True
                    continue
                am = _APPLY_RE.match(sql, i)
                if am:
                    # `CROSS APPLY`/`OUTER APPLY` の右辺はテーブル factor。検査対象に含める。
                    i = am.end()
                    expecting = True
                    continue
                lm = _LATERAL_RE.match(sql, i)
                if lm:
                    # `LATERAL <factor>`(派生表/インライン表関数)も factor 位置として扱う。
                    i = lm.end()
                    expecting = True
                    continue
                om = _ONUSING_RE.match(sql, i)
                if om:
                    # join 条件(列参照)に入る。次の factor 位置まで factor を拾わない。
                    i = om.end()
                    expecting = False
                    continue
            c = sql[i]
            if c == '"' and not (depth == 0 and expecting):
                # 非 factor 位置の引用識別子(別名・列名)を不可分にスキップし、内部の予約語
                # (`"JOIN"`/`"APPLY"` 等)を句キーワードと誤認しない。factor 位置の引用識別子は
                # 後段の _FACTOR_HEAD_RE が取り込む。
                i += 1
                while i < n and sql[i] != '"':
                    i += 1
                i += 1  # 閉じ "
                continue
            if c == "(":
                if depth == 0 and expecting:
                    # factor 位置の `(`: `(SELECT/WITH ...)` の派生表だけ許可し内側 FROM を別途検査
                    # する(別名 `) alias` 誤認回避のため expecting を下ろす)。それ以外の括弧付き
                    # テーブル参照(親括弧付き JOIN `(a JOIN other.x)` 等)は内側 FROM が無く
                    # factor を取りこぼすため保守的に拒否(隔離跨ぎ防止)。
                    if not _PAREN_SUBQUERY_RE.match(sql, i):
                        raise SqlRejectedError(
                            "括弧付きテーブル参照(非サブクエリ)は未対応のため拒否(隔離跨ぎ防止)"
                        )
                    expecting = False
                depth += 1
                i += 1
                continue
            if c == ")":
                if depth == 0:
                    break  # この FROM を含むサブクエリの終端
                depth -= 1
                i += 1
                continue
            if depth == 0 and c == ",":
                expecting = True
                i += 1
                continue
            if c.isspace():
                i += 1
                continue
            if depth == 0 and expecting:
                hm = _FACTOR_HEAD_RE.match(sql, i)
                if hm:
                    factors.append(hm.group(1).strip())
                    expecting = False
                    i = hm.end()
                    continue
            i += 1
    return factors


def _assert_schema_scoped(sql: str, schema: str, tables: set[str]) -> None:
    """生成SQLが対象スキーマの許可テーブルのみを参照することを強制する(専用スキーマ隔離)。

    Select AI はスキーマ別プロファイル(object_list)で対象スキーマに絞って生成するが、生成物は
    保証されない。`SH.SALES` のような他スキーマ参照や未許可テーブルを実行前に拒否し、読取専用
    ユーザーが既存リソースを読める抜け道(隔離破り)を塞ぐ。

    検査対象は **コメント除去済み(sanitize_sql)** の SQL。FROM/JOIN/カンマ結合に続く全 table
    factor を列挙し、**対象スキーマ修飾＝かつ許可テーブル**のみ通す。非修飾参照(`FROM SALES`。
    実行ユーザー依存のスキーマ解決)とサブクエリ外の他スキーマ参照は拒否する。サブクエリ
    `(SELECT ...)` の内側 FROM/JOIN も同じ走査で個別に検査される。例外として、WITH 句で定義した
    CTE 名の参照(クエリ内ローカル。実テーブルではない)は許可する——ただし CTE **本体**内の
    実テーブル参照は通常どおり検査するので隔離は緩まない。`schema.table@dblink` の DB リンク参照は
    別 DB の同名テーブルを読める抜け道になるため拒否する。これは**多層防御の一層**で、一次的な
    隔離保証は読取専用ユーザーの最小権限(対象スキーマのみ SELECT 可)が担う。
    """
    target = schema.upper()
    allowed = {t.upper() for t in tables}
    # コメント除去＋単一 SELECT 強制を先に通す(コメント区切りの取りこぼし防止)。
    cleaned = sanitize_sql(sql)
    blanked = _blank_string_literals(cleaned)
    # DBリンク参照(`@link` / `@"link"`)の決定的バックストップ: 文字列リテラルを無効化した後の
    # SQL に `@` が残れば隔離跨ぎの恐れとして拒否する(factor 解析の取りこぼしに依存しない一次防御)。
    # スコープ済み SELECT に `@` の正当な用途は無い(バインド変数は `:`、識別子に `@` は使わない)。
    if "@" in blanked:
        raise SqlRejectedError("DBリンク/`@` を含むテーブル参照は不可(隔離跨ぎ防止)")
    # 危険なパッケージ/関数(動的SQL・外部アクセス・URI フェッチ)は、テーブル参照が正当でも
    # 関数経由で隔離を破り得るため拒否する(集計用生成SQLでは使われない)。
    danger = _DANGEROUS_SQL_RE.search(blanked)
    if danger:
        raise SqlRejectedError(f"危険な関数/パッケージ参照は不可: {danger.group(1)}")
    factors = _table_factors(cleaned)
    # 実テーブル参照を1件も拾えないのは異常として拒否(サブクエリのみ等は想定しない)。
    if not factors:
        raise SqlRejectedError("テーブル参照を検出できないSQLは実行しない")
    ctes = _cte_names(cleaned)
    for f in factors:
        # DB リンク参照(`schema.table@link`)は専用スキーマ外/隔離跨ぎへの抜け道になるため拒否。
        # スキーマ修飾済みに見えても別 DB の同名テーブルを読めてしまう。
        if "@" in f:
            raise SqlRejectedError(f"DBリンク経由のテーブル参照は不可(隔離跨ぎ防止): {f}")
        m = _QUALIFIED_RE.match(f)
        if not m:
            um = _UNQUALIFIED_RE.match(f)
            if um:
                # WITH 句で定義された CTE 参照はクエリ内ローカルで実テーブルではない → 許可。
                if um.group(1).upper() in ctes:
                    continue
                raise SqlRejectedError(
                    f"非修飾のテーブル参照は不可(対象スキーマ修飾が必須): {f}"
                )
            raise SqlRejectedError(f"解析できないテーブル参照: {f}")
        sch, tbl = m.group(1).upper(), m.group(2).upper()
        if sch != target or tbl not in allowed:
            raise SqlRejectedError(
                f"許可範囲外のテーブル参照: {sch}.{tbl}(許可: {target}.{sorted(allowed)})"
            )


def _ensure_profile_for_schema(schema: str, model: str) -> str:
    """指定スキーマを object_list に持つ Select AI プロファイルを遅延作成し、名前を返す。"""
    prof = f"{SELECT_AI_PROFILE}_{schema.upper()}"
    with _profile_lock:
        if _schema_profiles.get(prof) == model:
            return prof
        from .db import connect

        with connect() as conn:
            cur = conn.cursor()
            create_profile(cur, prof, model, [{"owner": schema.upper()}])
            conn.commit()
        _schema_profiles[prof] = model
    return prof


def run_nl2sql_for_schema(
    question: str,
    *,
    schema: str,
    tables: list[str] | set[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """専用スキーマに対し NL→SQL 生成→読取専用実行までを束ねて結果を返す(SBA-C 売上集計)。

    返り値: {schema, sql, columns, rows, row_count, truncated}。`tables` はそのスキーマで参照を
    許可するテーブル名の集合(sample-app の dataset から導出)。生成SQLは実行前に
    `_assert_schema_scoped` で対象スキーマ＋許可テーブルのみに制限する(専用スキーマ隔離。他スキーマ
    /未許可テーブル参照は SqlRejectedError)。多層ガード(sanitize_sql・行数/タイムアウト上限)は
    execute_readonly。読取専用ユーザー側も当該スキーマだけ読める最小権限にすること(多層防御)。
    """
    if not _SCHEMA_RE.match(schema or ""):
        raise SqlRejectedError(f"不正なスキーマ名: {schema!r}")
    if not tables:
        raise SqlRejectedError("許可テーブル(tables)が空のスキーマ照会は実行しない")
    model = resolve_select_ai_model(model)
    prof = _ensure_profile_for_schema(schema, model)
    sql = generate_sql_select_ai(question, profile_name=prof, model=model)
    _assert_schema_scoped(sql, schema, set(tables))
    result = execute_readonly(sql)
    return {"schema": schema.upper(), "sql": sql, **result}


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
