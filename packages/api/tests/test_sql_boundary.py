"""層2 fail-closed SQL ゲート(specs/18 §4.3 — SP2-03)のテスト。

VPD(層1)は行データの境界、本ゲートは辞書・パッケージ・リンク・登録簿外参照の境界。
**allowlist 方式**(codex review-1 B001): FROM/JOIN のテーブル参照は DUAL・CTE・SH スキーマ・
呼び出し元の登録済みデータセット表(app スキーマ)だけを許可し、それ以外(未知の synonym・
別スキーマ・private synonym・辞書ビュー・table function)は一律拒否(fail-closed)。
パッケージ/スキーマ修飾の関数呼び出し・@ DBリンク・判定不能(q'')も拒否。
"""

import pytest
from jetuse_shared.sqlguard import SqlBoundaryError, SqlRejectedError, enforce_sql_boundary

OWN = frozenset({"JETUSE_DS_ABCD1234_11112222"})
APP = "JETUSE_APP"


def ok(sql, **kw):
    enforce_sql_boundary(sql, **kw)


def bad(sql, **kw):
    with pytest.raises(SqlBoundaryError):
        enforce_sql_boundary(sql, **kw)


# --- 許可(SH・一般 SQL・本人データセット・CTE・DUAL) ---

@pytest.mark.parametrize("sql", [
    "SELECT * FROM sh.sales FETCH FIRST 10 ROWS ONLY",
    'SELECT * FROM "SH"."SALES"',
    "SELECT c.cust_last_name, SUM(s.amount_sold) FROM sh.sales s "
    "JOIN sh.customers c ON c.cust_id = s.cust_id GROUP BY c.cust_last_name",
    "SELECT s.amount_sold FROM sh.sales s, sh.customers c WHERE s.cust_id = c.cust_id",
    "WITH t AS (SELECT prod_id, COUNT(*) n FROM sh.sales GROUP BY prod_id) "
    "SELECT * FROM t ORDER BY n DESC",
    "SELECT EXTRACT(YEAR FROM s.time_id) yr, SUM(s.amount_sold) FROM sh.sales s "
    "GROUP BY EXTRACT(YEAR FROM s.time_id)",  # 関数内 FROM は句境界でない
    "SELECT TRIM(BOTH ' ' FROM c.cust_last_name) FROM sh.customers c",
    "SELECT * FROM sh.sales s WHERE s.cust_id IN (SELECT c.cust_id FROM sh.customers c)",
    "SELECT SYSDATE FROM dual",
    "SELECT '営業部@大阪' AS label FROM dual",  # 文字列内の @ は拒否しない
    "SELECT 'it''s a test' FROM dual",  # '' エスケープ
])
def test_allows_sh_and_general_sql(sql):
    ok(sql)


def test_allows_own_registered_dataset():
    ok("SELECT * FROM JETUSE_DS_ABCD1234_11112222 WHERE 売上 > 100", allowed_tables=OWN)
    # app スキーマ修飾の登録済み DS 表(Select AI 生成 SQL の典型形)
    ok('SELECT * FROM JETUSE_APP."JETUSE_DS_ABCD1234_11112222"',
       allowed_tables=OWN, app_schema=APP)
    # 大文字小文字は非依存(未クォート識別子は Oracle が upcase する)
    ok("SELECT * FROM jetuse_ds_abcd1234_11112222", allowed_tables=OWN)


# --- (a) 登録簿外の参照 / 別スキーマ / 未知 synonym(fail-closed の核 — B001) ---

def test_rejects_unregistered_dataset_table():
    bad("SELECT * FROM JETUSE_DS_FFFF0000_99998888", allowed_tables=OWN)


def test_rejects_all_ds_refs_in_ownerless_mode():
    bad("SELECT * FROM JETUSE_DS_ABCD1234_11112222")  # allowlist 空


def test_rejects_ds_ref_in_subquery_or_join():
    bad("SELECT * FROM sh.sales WHERE cust_id IN "
        "(SELECT c1 FROM JETUSE_DS_EEEE0000_00001111)", allowed_tables=OWN)
    bad("SELECT * FROM sh.sales s JOIN JETUSE_DS_EEEE0000_00001111 x "
        "ON s.id = x.id", allowed_tables=OWN)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM TABLE_PRIVILEGES",       # 列挙外の辞書 synonym
    "SELECT * FROM SOME_SCHEMA.SOME_TABLE",  # 別スキーマ(未許可)
    "SELECT * FROM my_private_synonym",      # 任意の private synonym
    "SELECT * FROM sh.sales s, other_owner.secrets x WHERE s.id = x.id",
    # app 修飾でも app_schema 未指定なら拒否
    "SELECT * FROM JETUSE_APP.JETUSE_DS_ABCD1234_11112222",
])
def test_rejects_unknown_or_foreign_table_refs(sql):
    bad(sql, allowed_tables=OWN)  # app_schema 未指定


def test_rejects_foreign_schema_qualified_ds_even_with_app_schema():
    # 登録済み表名でも別スキーマ修飾は拒否(app スキーマ以外の同名表を読ませない)
    bad('SELECT * FROM EVIL."JETUSE_DS_ABCD1234_11112222"',
        allowed_tables=OWN, app_schema=APP)


def test_rejects_table_function_in_from():
    bad("SELECT * FROM TABLE(some_pkg.some_func())", allowed_tables=OWN)


# --- CTE スコープ迂回(review-2 B001): 内側 CTE 名を外側で使わせない ---

def test_cte_is_block_scoped_not_global():
    # 内側 CTE `ALL_USERS` が外側の実辞書ビュー ALL_USERS を許可してはならない
    bad("SELECT u.username FROM ALL_USERS u JOIN "
        "(WITH ALL_USERS AS (SELECT 1 n FROM DUAL) SELECT n FROM ALL_USERS) x ON 1=1")
    # DS 表でも同様(内側 CTE 名で登録簿外 DS を外側で許可させない)
    bad("SELECT * FROM JETUSE_DS_FFFF0000_99998888 t JOIN "
        "(WITH JETUSE_DS_FFFF0000_99998888 AS (SELECT 1 n FROM DUAL) SELECT n "
        "FROM JETUSE_DS_FFFF0000_99998888) x ON 1=1", allowed_tables=OWN)


def test_cte_dict_prefix_name_rejected_even_as_cte():
    # CTE 名が辞書接頭辞を持つ場合、その参照自体を拒否(多重防御)
    bad("WITH ALL_EVIL AS (SELECT 1 n FROM DUAL) SELECT * FROM ALL_EVIL")


def test_normal_cte_still_allowed():
    ok("WITH dept AS (SELECT prod_id, COUNT(*) n FROM sh.sales GROUP BY prod_id) "
       "SELECT * FROM dept WHERE n > 5")
    # ネストした CTE も同一/子ブロックでは可視
    ok("WITH a AS (SELECT 1 n FROM dual) SELECT * FROM a "
       "WHERE n IN (SELECT n FROM a)")


def test_column_list_cte_allowed():
    # 列別名リスト付き CTE `WITH t(a,b) AS (...)` は Oracle で有効 — 旧ゲート下で通っていた
    # 公開 SQL の後方互換を守る(review-10 M001)。引用列名・複数 CTE も同様。
    ok("WITH totals(dept, amount) AS (SELECT prod_id, COUNT(*) FROM sh.sales GROUP BY prod_id) "
       "SELECT dept FROM totals WHERE amount > 5")
    ok('WITH t("Col A", "Col B") AS (SELECT 1, 2 FROM dual) SELECT * FROM t')
    # 列リスト CTE でも辞書接頭辞の名前は拒否(多重防御は維持)
    bad("WITH ALL_EVIL(a) AS (SELECT 1 FROM dual) SELECT * FROM ALL_EVIL")
    # 列リスト形でスコープ外(内側 CTE 名を外側で使えない)も維持
    bad("SELECT * FROM (WITH t(a) AS (SELECT 1 FROM dual) SELECT a FROM t) x "
        "JOIN t ON 1=1")


# --- 引用/未引用の別名迂回(review-5 B001): Oracle は quoted を大小文字区別する ---

def test_quoted_cte_name_does_not_shadow_unquoted_ref():
    # 引用小文字 CTE `"tab"` は未引用 TAB(公開 synonym に解決される)を許可してはならない
    bad('WITH "tab" AS (SELECT 1 n FROM DUAL) SELECT * FROM TAB')
    # 引用小文字 CTE 名で辞書ビュー(未引用 ALL_USERS)を許可させない
    bad('WITH "all_users" AS (SELECT 1 n FROM DUAL) SELECT * FROM ALL_USERS')


def test_quoted_names_are_case_sensitive():
    # 引用小文字はそれ自体では許可対象でない(synonym TAB とも別物 = fail-closed)
    bad('SELECT * FROM "tab"')
    bad('SELECT * FROM "TAB"')          # 引用大文字 TAB = 公開 synonym。許可外
    bad('SELECT * FROM "ALL_USERS"')    # 引用でも辞書名は拒否
    # 引用の CTE 定義と参照が一致すれば通る(同一 canonical)
    ok('WITH "MyData" AS (SELECT 1 n FROM DUAL) SELECT * FROM "MyData"')
    # 別スキーマの引用小文字修飾は SH と別物 → 拒否
    bad('SELECT * FROM "sh"."sales"')


@pytest.mark.parametrize("sql", [
    # 引用大文字パッケージ呼び出しは Oracle で同じパッケージに解決される → 拒否(review-6 B001)
    'SELECT "DBMS_XMLGEN"."GETXML"(\'SELECT * FROM ALL_USERS\') FROM DUAL',
    'SELECT "UTL_INADDR".GET_HOST_NAME FROM DUAL',
    'SELECT * FROM "SYS".all_objects',        # 引用 SYS 修飾
    'SELECT "SYS"."foo"() FROM DUAL',
])
def test_rejects_quoted_package_and_qualifier_calls(sql):
    bad(sql)


# --- PIVOT/UNPIVOT の後続結合(review-6 B002): 句で FROM を終わらせない ---

@pytest.mark.parametrize("sql", [
    "SELECT * FROM SH.SALES PIVOT (SUM(amount_sold) FOR channel_id IN (3)) p, ALL_USERS u",
    "SELECT * FROM SH.SALES UNPIVOT (v FOR k IN (amount_sold)) p, DBA_USERS u",
    "SELECT * FROM SH.SALES PIVOT (SUM(amount_sold) FOR channel_id IN (3)) p "
    "JOIN ALL_TABLES t ON 1=1",
])
def test_rejects_post_pivot_join_bypass(sql):
    bad(sql, allowed_tables=OWN)


def test_allows_legit_pivot():
    ok("SELECT * FROM SH.SALES PIVOT (SUM(amount_sold) FOR channel_id IN (3, 4, 5)) p")
    ok("SELECT * FROM SH.SALES s UNPIVOT (val FOR col IN (amount_sold, quantity_sold)) u")
    # PIVOT の後に別の許可テーブルを結合するのは通る
    ok("SELECT * FROM SH.SALES PIVOT (SUM(amount_sold) FOR channel_id IN (3)) p, sh.customers c")


# --- XML DB 経由の辞書/表参照(review-7 B001): 文字列に隠れた oradb:/ora:view/fn:collection ---

@pytest.mark.parametrize("sql", [
    'SELECT XMLQUERY(\'fn:collection("oradb:/SYS/ALL_TAB_COLUMNS")\' RETURNING CONTENT) FROM DUAL',
    "SELECT * FROM XMLTABLE('for $i in ora:view(\"SH\",\"SALES\") return $i')",
    'SELECT XMLEXISTS(\'collection("oradb:/SYS/DUAL")\') FROM DUAL',
    "SELECT EXTRACTVALUE(x, 'ora:view(\"foo\")') FROM DUAL",
    "SELECT * FROM DUAL WHERE 'oradb:/x/y' IS NOT NULL",   # 生 SQL 走査(関数外でも拒否)
    # URI 型コンストラクタ(URI から表/ビュー行を取得 — review-8 B001)
    "SELECT DBURIType('/SYS/ALL_USERS').getXML() FROM DUAL",
    "SELECT XDBURIType('/home/x').getBlob() FROM DUAL",
    "SELECT HTTPURIType('http://x/y').getClob() FROM DUAL",
    "SELECT SYS_DBURIGEN(dummy) FROM DUAL",
    'SELECT "DBURITYPE"(\'/SYS/ALL_USERS\').getXML() FROM DUAL',  # 引用大文字も同一型
])
def test_rejects_xmldb_dictionary_access(sql):
    bad(sql)


def test_datetime_extract_not_confused_with_xml_funcs():
    # 日時 EXTRACT(field FROM datetime)は XML 関数ではない → 通す(EXTRACTVALUE のみ拒否)
    ok("SELECT EXTRACT(YEAR FROM s.time_id) yr FROM sh.sales s")
    ok("SELECT EXTRACT(MONTH FROM s.time_id) m FROM sh.sales s "
       "GROUP BY EXTRACT(MONTH FROM s.time_id)")


# --- 辞書/システム露出の組み込み関数(ADR-0022 C 段階的硬化・人間承認 2026-07-07) ---

@pytest.mark.parametrize("sql", [
    "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER') FROM DUAL",
    "SELECT USERENV('SESSIONID') FROM DUAL",
    "SELECT ORA_INVOKING_USER() FROM DUAL",
    "SELECT ORA_DICT_OBJ_OWNER() FROM DUAL",
    "SELECT ORA_DATABASE_NAME() FROM DUAL",
])
def test_rejects_dict_system_builtins(sql):
    bad(sql)


def test_same_named_bare_column_still_allowed():
    # `(` を伴わない同名の素の列/別名は関数呼び出しでない → 後方互換で通す(N001 を悪化させない)
    ok("SELECT sys_context FROM sh.sales")
    ok("SELECT s.userenv AS u FROM sh.sales s")


# --- 括弧付き JOIN 迂回(review-3 B001): FROM/JOIN のテーブル位置の括弧が
#     サブクエリでない(Oracle の `(t1 JOIN t2)`)なら内部の table_reference も検査する ---

@pytest.mark.parametrize("sql", [
    # JOIN 直後の括弧付き結合の先頭に辞書ビューを置く迂回(検査から外させない)
    "SELECT u.username FROM sh.sales s JOIN (ALL_USERS u CROSS JOIN DUAL d) ON 1=1",
    # 二重括弧でも内側先頭を検査
    "SELECT * FROM sh.sales s JOIN ((ALL_TABLES a JOIN DUAL d)) ON 1=1",
    # 括弧付き結合の後続テーブルも検査(DUAL の後の辞書ビュー)
    "SELECT * FROM sh.sales s JOIN (DUAL d JOIN DBA_USERS u) ON 1=1",
    # 単一テーブル括弧に辞書ビュー
    "SELECT * FROM (ALL_TAB_COLUMNS)",
    # 括弧付き結合の先頭に登録簿外 DS
    "SELECT * FROM (JETUSE_DS_FFFF0000_99998888 a JOIN DUAL b)",
    # 旧式カンマ結合を括弧で包んでも検査
    "SELECT * FROM sh.sales s JOIN (all_users u, DUAL d) ON 1=1",
])
def test_rejects_parenthesized_join_bypass(sql):
    bad(sql, allowed_tables=OWN)


def test_allows_legit_parenthesized_join():
    # 括弧付き結合でも中身が許可対象なら通す
    ok("SELECT * FROM (sh.sales s JOIN sh.products p ON s.prod_id = p.prod_id)")
    ok("SELECT * FROM (DUAL)")
    ok("SELECT * FROM (JETUSE_DS_ABCD1234_11112222 a JOIN DUAL b ON a.id = b.dummy)",
       allowed_tables=OWN)
    # 派生テーブル(サブクエリ)は従来どおり内部が独自検証される
    ok("SELECT * FROM (SELECT * FROM sh.sales) x")


# --- CROSS/OUTER APPLY・LATERAL 迂回(review-4 B001): 右辺の table_reference も検査する ---

@pytest.mark.parametrize("sql", [
    "SELECT * FROM sh.sales s CROSS APPLY ALL_USERS u",       # APPLY 右辺の素辞書ビュー
    "SELECT * FROM sh.sales s OUTER APPLY DBA_TABLES t",      # OUTER APPLY 右辺
    "SELECT * FROM sh.sales s CROSS APPLY TABLE(some_pkg.f()) t",  # APPLY 右辺の table function
    "SELECT * FROM sh.sales s CROSS APPLY JETUSE_DS_FFFF0000_99998888 x",  # 登録簿外 DS
    "SELECT * FROM sh.sales s CROSS APPLY (SELECT * FROM all_users) v",    # APPLY 右辺サブクエリ内
])
def test_rejects_apply_lateral_bypass(sql):
    bad(sql, allowed_tables=OWN)


def test_allows_legit_apply_lateral():
    ok("SELECT * FROM sh.sales s CROSS APPLY "
       "(SELECT * FROM sh.products p WHERE p.prod_id = s.prod_id) v")
    ok("SELECT * FROM sh.sales s, LATERAL "
       "(SELECT * FROM sh.customers c WHERE c.cust_id = s.cust_id) v")


# --- SH サンプルスキーマの fail-closed(review-3 M001): 既知オブジェクトだけ許可 ---

@pytest.mark.parametrize("sql", [
    "SELECT * FROM SH.SECRET_SYNONYM",     # SH 内の未知オブジェクト/synonym
    "SELECT * FROM sh.all_users",           # SH 修飾で辞書名を通させない
    "SELECT * FROM SH.PLAN_TABLE",          # SH の非サンプル表
])
def test_rejects_unknown_sh_object(sql):
    bad(sql, allowed_tables=OWN)


def test_allows_known_sh_objects():
    ok("SELECT * FROM SH.SALES")
    ok("SELECT * FROM sh.customers c JOIN sh.times t ON c.cust_id = t.time_id")
    ok("SELECT * FROM SH.CAL_MONTH_SALES_MV")  # 標準 MV


# --- (b) データディクショナリ・動的ビュー(allowlist が FROM 位置で捕捉) ---

@pytest.mark.parametrize("sql", [
    "SELECT table_name FROM ALL_TABLES",
    "SELECT column_name FROM all_tab_columns WHERE table_name LIKE 'JETUSE_DS_%'",
    "SELECT * FROM dba_users",
    "SELECT * FROM USER_TABLES",
    "SELECT * FROM cdb_objects",
    "SELECT * FROM v$session",
    "SELECT * FROM gv$sql",
    'SELECT * FROM "ALL_TAB_COLUMNS"',
    "SELECT t.name FROM sh.sales s, all_users t",  # 一部だけ辞書でも拒否
    "SELECT s.id FROM sh.sales s WHERE s.x IN (SELECT username FROM all_users)",
])
def test_rejects_dictionary_views(sql):
    bad(sql, allowed_tables=OWN)


# --- (c) パッケージ呼び出し・動的 SQL ベクタ ---

@pytest.mark.parametrize("sql", [
    "SELECT DBMS_XMLGEN.GETXML('select * from x') FROM dual",
    "SELECT dbms_metadata.get_ddl('TABLE', 'T') FROM dual",
    "SELECT UTL_INADDR.GET_HOST_NAME FROM dual",
    "SELECT SYS.DBMS_ASSERT.NOOP('x') FROM dual",
    "SELECT sys.odcivarchar2list('a') FROM dual",
    "SELECT sh.some_func(1) FROM dual",  # スキーマ修飾呼び出しは一律拒否(fail-closed)
])
def test_rejects_package_calls(sql):
    bad(sql, allowed_tables=OWN)


def test_allows_alias_column_refs_and_plain_functions():
    ok("SELECT s.amount_sold, COUNT(*), MAX(s.time_id) FROM sh.sales s "
       "GROUP BY s.amount_sold")


# --- (d) DB リンク・判定不能 ---

def test_rejects_db_link():
    bad("SELECT * FROM tab1@remote_db")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM tab", "SELECT * FROM dict", "SELECT * FROM dictionary",
    "SELECT * FROM cat", "SELECT * FROM cols", "SELECT * FROM session_privs",
    "SELECT * FROM nls_database_parameters",
])
def test_rejects_dictionary_synonyms(sql):
    bad(sql)


def test_rejects_q_quoted_literal_as_undecidable():
    bad("SELECT q'[abc]' FROM dual")


def test_rejects_unterminated_string():
    bad("SELECT 'abc FROM dual")


def test_string_literal_cannot_hide_identifiers():
    # クォート識別子内の ' で文字列判定がずれても識別子走査が欺かれない(単一パス字句解析)
    bad('SELECT "a\'" , x FROM ALL_USERS WHERE x = "b\'"')


def test_boundary_error_is_sql_rejected_subclass():
    assert issubclass(SqlBoundaryError, SqlRejectedError)


# --- execute_readonly 統合(登録簿照合 — 4 方向の越境 403。specs/18 §4.3) ---

class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("C",)]

    def execute(self, sql, **kw):
        self.conn.calls.append(("execute", sql))

    def fetchmany(self, n):
        return [["1"]]

    def callproc(self, name, args):
        self.conn.calls.append((name.split(".")[-1], tuple(args)))


class _Conn:
    def __init__(self):
        self.calls = []
        self.call_timeout = 0

    def cursor(self):
        return _Cur(self)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn

    def drop(self, conn):
        pass

    def release(self, conn):
        pass


REGISTRY = {
    "user-a": {"JETUSE_DS_AAAA0001_11110000"},
    "user-b": {"JETUSE_DS_BBBB0002_22220000"},
    "demo_d1": {"JETUSE_DS_" + "D" * 40 + "_33330000"},
    "demo_d2": {"JETUSE_DS_" + "E" * 40 + "_44440000"},
}


@pytest.fixture()
def exec_env(monkeypatch):
    from jetuse_core import datasets, nl2sql, owner_keys, vpd

    monkeypatch.setattr(vpd, "verify_integrity", lambda: [])
    monkeypatch.setattr(owner_keys, "owner_key_gate", lambda: None)  # 移行ゲートは別テスト
    monkeypatch.setattr(datasets, "owner_ds_tables", lambda o: REGISTRY.get(o, set()))
    # SH allowlist はスキーマ修飾(sh.sales)で通るため DB を触らない mock でよい
    monkeypatch.setattr(nl2sql, "get_schema_info",
                        lambda: {"schema": "SH", "tables": []})
    conn = _Conn()
    monkeypatch.setattr(nl2sql, "_get_query_pool", lambda: _Pool(conn))
    return conn


@pytest.mark.parametrize("caller,target", [
    ("user-a", "user-b"),    # user A → user B
    ("user-a", "demo_d1"),   # user → demo
    ("demo_d1", "user-a"),   # demo → user
    ("demo_d1", "demo_d2"),  # demo A → demo B
])
def test_execute_readonly_rejects_cross_owner_ds(exec_env, caller, target):
    from jetuse_core import nl2sql

    other = next(iter(REGISTRY[target]))
    with pytest.raises(SqlBoundaryError):
        nl2sql.execute_readonly(f"SELECT * FROM {other}", caller)
    assert all(c[0] != "execute" for c in exec_env.calls)  # DB に到達しない(早期 403)


def test_execute_readonly_allows_own_ds_and_sh(exec_env):
    from jetuse_core import nl2sql

    own = next(iter(REGISTRY["user-a"]))
    assert nl2sql.execute_readonly(f"SELECT * FROM {own}", "user-a")["row_count"] == 1
    assert nl2sql.execute_readonly("SELECT * FROM sh.sales", "user-a")["row_count"] == 1


def test_execute_readonly_ownerless_rejects_ds_and_dict(exec_env):
    from jetuse_core import nl2sql

    with pytest.raises(SqlBoundaryError):
        nl2sql.execute_readonly(f"SELECT * FROM {next(iter(REGISTRY['user-a']))}", None)
    with pytest.raises(SqlBoundaryError):
        nl2sql.execute_readonly("SELECT * FROM all_tab_columns", None)
    assert nl2sql.execute_readonly("SELECT * FROM sh.sales", None)["row_count"] == 1


def test_execute_readonly_dict_rejected_in_owner_mode(exec_env):
    from jetuse_core import nl2sql

    with pytest.raises(SqlBoundaryError):
        nl2sql.execute_readonly("SELECT * FROM ALL_TAB_COLUMNS", "user-a")


# --- agent 経路(tools.query_database — owner なしモード。specs/18 §4.3) ---

@pytest.mark.parametrize("bad_sql", [
    "SELECT * FROM JETUSE_DS_AAAA0001_11110000",  # 本人所有でも agent 経路は拒否
    "SELECT table_name FROM all_tables",
    "SELECT DBMS_XMLGEN.GETXML('q') FROM dual",
])
def test_agent_tool_rejects_boundary_violations(exec_env, monkeypatch, bad_sql):
    from jetuse_core import nl2sql, tools

    monkeypatch.setattr(nl2sql, "generate_sql", lambda q: bad_sql)
    with pytest.raises(SqlBoundaryError):
        tools.query_database_handler({"question": "q"})


def test_agent_tool_sh_query_still_works(exec_env, monkeypatch):
    from jetuse_core import nl2sql, tools

    monkeypatch.setattr(nl2sql, "generate_sql", lambda q: "SELECT prod_id FROM sh.sales")
    assert '"row_count": 1' in tools.query_database_handler({"question": "売上"})


def test_agent_container_db_gate(monkeypatch):
    """agent-containers の独立経路(agent_db.py)にも同じ層2ゲート(owner なしモード)。"""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "agent_db",
        pathlib.Path(__file__).resolve().parents[2] / "agent-containers" / "agent_db.py",
    )
    agent_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_db)
    monkeypatch.setattr(agent_db, "generate_sql", lambda q: "SELECT * FROM all_tab_columns")
    with pytest.raises(SqlBoundaryError):
        agent_db.query_database("q")


# --- 旧登録簿(STATE 列なし)からの層2ゲート照会(review-4 M003) ---

def test_owner_ds_tables_migrates_legacy_registry_before_state_query(monkeypatch):
    """owner_ds_tables は state 参照の前に _ensure_meta を呼ぶ(旧登録簿の遅延移行 →
    `SELECT ... state` の ORA-00904 を回避)。登録簿への最初の操作が SQL execute でも成立。"""
    from jetuse_core import datasets

    order = []

    class Cur:
        def execute(self, sql, **kw):
            order.append("state_query" if "table_name" in sql and "state" in sql else "other")

        def fetchall(self):
            return [("JETUSE_DS_ABCD1234_11112222",)]

    class Conn:
        def cursor(self):
            return Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(datasets, "connect", lambda: Conn())
    monkeypatch.setattr(datasets, "_ensure_meta",
                        lambda cur: order.append("ensure_meta"))
    result = datasets.owner_ds_tables("user-a")
    assert result == {"JETUSE_DS_ABCD1234_11112222"}
    assert order.index("ensure_meta") < order.index("state_query")
