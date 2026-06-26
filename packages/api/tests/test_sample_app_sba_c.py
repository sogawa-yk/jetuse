"""SBA-C(営業案件管理)の複合AI組込テスト(SBA-04)。

LLM は `_completer` を、NL2SQL 実行は `_nl2sql_runner` を差し替えて DB/外部に出ずに検証する。
議事録要約(minutes)→次アクション提案(agent)→メール下書き(draft)の連動と、
売上集計(nl2sql)の専用スキーマ照会を確認する。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core.plugins import ai_runtime
from jetuse_core.plugins.sample_app import validate_composition
from jetuse_core.plugins.sample_app_builtin_c import (
    SBA_C_INSTANCE_ID,
    SBA_C_NL2SQL_SCHEMA,
    dataset_seed,
    sba_c_definition,
    sba_c_manifest,
)
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    def fake(model_key, messages, max_chars):
        system = messages[0]["content"]
        if "次アクション提案エージェント" in system:
            return (
                "1. [今週] PoC対象ラインを2本に絞る — 提案合意の前進\n"
                "2. セキュリティ構成図を作成"
            )
        if "議事録" in system:
            return "## 要点\nMES老朽化。クラウド移行に前向き。\n## 次アクション候補\nPoC範囲合意"
        return "山田製作所 ご担当者様\n\nお世話になっております。…(メール下書き)"

    monkeypatch.setattr(ai_runtime, "_completer", fake)


# --- 定義・合成バリデーション ---------------------------------------------


def test_sba_c_definition_valid_and_composes():
    """SBA-C 定義は検証を通り、全 capability がコア能力で合成可能(missing なし)。"""
    d = sba_c_definition()
    assert {s.capability for s in d.ai_slots} == {"minutes", "agent", "nl2sql", "draft"}
    report = validate_composition(sba_c_manifest())
    assert report.ok, report
    assert report.missing_capabilities == []


def test_sba_c_capabilities_are_bound():
    """SBA-C が要求する能力はすべて実行時に束縛済み(未束縛 capability ゼロ)。"""
    assert ai_runtime.unbound_capabilities(sba_c_definition()) == []


def test_sales_seed_present_for_nl2sql():
    """売上集計の元データ(sales シード)が存在し、E2E で JETUSE_SBA04 へ投入できる。"""
    rows = dataset_seed("sales")
    assert len(rows) >= 10
    assert all("amount" in r and "region" in r for r in rows)


# --- ハンドラ単体(DB/LLM 差し替え) ---------------------------------------


def test_handle_minutes_structured_summary():
    d = sba_c_definition()
    out = ai_runtime.invoke_slot(
        d, "minutes-summary", {"input": "山田部長: MESが老朽化…"}, owner="u"
    )
    assert out["capability"] == "minutes"
    assert "要点" in out["summary"]


def test_handle_agent_parses_actions():
    d = sba_c_definition()
    out = ai_runtime.invoke_slot(
        d, "next-actions", {"input": "案件: 山田製作所\n議事録要約: …"}, owner="u"
    )
    assert out["capability"] == "agent"
    assert len(out["actions"]) == 2
    assert out["actions"][0].startswith("[今週]")  # 箇条書き番号は除去される


def test_handle_nl2sql_uses_injected_runner_and_schema():
    seen = {}

    def fake_runner(question, *, schema, tables, model_key):
        seen["schema"] = schema
        seen["question"] = question
        seen["tables"] = tables
        return {
            "sql": "SELECT owner, SUM(amount) FROM JETUSE_SBA04.SALES GROUP BY owner",
            "columns": ["OWNER", "TOTAL"],
            "rows": [["加藤", "62900000"], ["佐々木", "36200000"]],
            "row_count": 2,
            "truncated": False,
        }

    ai_runtime._nl2sql_runner = fake_runner
    try:
        d = sba_c_definition()
        out = ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "担当者別の売上合計を多い順に"},
            owner="u", nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )
    finally:
        ai_runtime._nl2sql_runner = ai_runtime._default_nl2sql
    assert out["capability"] == "nl2sql"
    assert out["schema"] == "JETUSE_SBA04"
    assert seen["schema"] == "JETUSE_SBA04"
    # 参照可能テーブルは「このスロットを載せる screen の dataset」だけ(面の広げすぎ防止)。
    # sales-rollup は analytics screen(dataset=sales)のみ → sales に限定(deals/meetings は不可)。
    assert set(seen["tables"]) == {"sales"}
    assert out["columns"] == ["OWNER", "TOTAL"]
    assert out["row_count"] == 2


def test_slot_tables_are_scoped_per_slot():
    """_slot_tables はスロットを載せる screen の dataset のみを返す(売上集計は sales だけ)。"""
    d = sba_c_definition()
    slot = next(s for s in d.ai_slots if s.key == "sales-rollup")
    ctx = ai_runtime.SlotContext(owner="u", slot=slot, definition=d)
    assert ai_runtime._slot_tables(ctx) == ["sales"]


def test_handle_nl2sql_db_error_normalized_not_500(monkeypatch):
    """SQL未生成(RuntimeError)・列不正等の実行失敗は SlotInferenceError(502)に正規化。"""
    def boom_runtime(question, *, schema, tables, model_key):
        raise RuntimeError("Select AIがSQLを返しませんでした")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_runtime)
    d = sba_c_definition()
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_connection_error_maps_to_backend_unavailable(monkeypatch):
    """DB 接続障害(一過性)は 502 に丸めず SlotBackendUnavailableError(ルートで 503)にする。"""
    def boom_conn(question, *, schema, tables, model_key):
        raise RuntimeError("DPY-6005: cannot connect to database")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_conn)
    d = sba_c_definition()
    with pytest.raises(ai_runtime.SlotBackendUnavailableError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_implementation_bug_not_masked(monkeypatch):
    """F-004: TypeError 等の実装バグは 502 に丸めず再送出する(本物のバグを隠さない)。"""
    def boom_bug(question, *, schema, tables, model_key):
        raise TypeError("unexpected keyword argument")  # 実装バグ相当(末尾 'Error')

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_bug)
    d = sba_c_definition()
    # SlotInferenceError(502)に正規化されず、素の TypeError が表面化する(ルートで 500)。
    with pytest.raises(TypeError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_unknown_runtimeerror_not_masked(monkeypatch):
    """review-6: 未知の RuntimeError(SQL未生成でも接続障害でもない)は 502 に丸めず再送出する。"""
    def boom_unknown(question, *, schema, tables, model_key):
        raise RuntimeError("something unexpected happened in the runner")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_unknown)
    d = sba_c_definition()
    with pytest.raises(RuntimeError):  # SlotInferenceError ではなく素の RuntimeError
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_operational_error_maps_503(monkeypatch):
    """review-10: pool 初期化失敗等の oracledb.OperationalError は marker 無しでも 503。"""
    import oracledb

    def boom_init(question, *, schema, tables, model_key):
        raise oracledb.OperationalError("db init failed: wallet not configured")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_init)
    d = sba_c_definition()
    with pytest.raises(ai_runtime.SlotBackendUnavailableError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_database_error_maps_502(monkeypatch):
    """生成SQLの列不正等(oracledb.DatabaseError / ORA-00942)は実行失敗なので 502(503 でない)。"""
    import oracledb

    def boom_col(question, *, schema, tables, model_key):
        raise oracledb.DatabaseError("ORA-00942: table or view does not exist")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_col)
    d = sba_c_definition()
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_handle_nl2sql_select_ai_no_sql_normalized(monkeypatch):
    """Select AI の SQL 未生成専用例外(SelectAiNoSqlError)は SlotInferenceError(502)に正規化。"""
    from jetuse_core.nl2sql import SelectAiNoSqlError

    def no_sql(question, *, schema, tables, model_key):
        raise SelectAiNoSqlError("Select AIがSQLを返しませんでした")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", no_sql)
    d = sba_c_definition()
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            d, "sales-rollup", {"input": "売上"}, owner="u",
            nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
        )


def test_agent_strips_invented_absolute_dates():
    """review-6: 次アクションが入力に無い絶対日付を作ったら「(期限未定)」に中和する(創作防止)。"""
    seen = {}

    def fake(model_key, messages, max_chars):
        seen["sys"] = messages[0]["content"]
        # 入力に無い絶対日付 2026-12-31 を勝手に提示するケース
        return "1. [2026-12-31] 契約書を送付 — 受注\n2. 次回会議までに見積提示 — 前進"

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions", {"input": "案件: 山田製作所。期限の明示なし。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    acts = res["actions"]
    assert not any("2026-12-31" in a for a in acts)  # 創作日付は除去
    assert any("期限未定" in a for a in acts)
    assert any("次回会議まで" in a for a in acts)  # 相対表現は保持
    # システムプロンプトが絶対日付の創作を禁じている
    assert "絶対日付" in seen["sys"]


def test_agent_keeps_dates_present_in_input():
    """入力に明示された絶対日付は次アクションでもそのまま保持する(過剰除去しない)。"""
    def fake(model_key, messages, max_chars):
        return "1. [2026-07-10] 提案書を提出 — 期日厳守"

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions",
            {"input": "案件: 明日工業。提案書の締切は 2026-07-10。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    assert any("2026-07-10" in a for a in res["actions"])  # 入力にある日付は残す


def test_agent_date_normalization_cross_format():
    """review-6(minor): 入力 `2026年7月10日` と出力 `2026-07-10` を同一視して創作扱いしない。"""
    def fake(model_key, messages, max_chars):
        return "1. [2026-07-10] 提案書を提出 — 期日厳守"  # ISO 形式

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions",
            {"input": "案件: 明日工業。締切は 2026年7月10日。"}, owner="u",  # 和式表記
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    # 表記揺れでも入力に存在する日付なので保持(「(期限未定)」に中和しない)。
    assert any("2026-07-10" in a for a in res["actions"])


def test_agent_text_has_no_invented_dates():
    """review-7(major): 公開レスポンスの text も sanitize 済み(raw の創作日付が漏れない)。"""
    def fake(model_key, messages, max_chars):
        return "1. [2099-01-01] 何かする — 狙い"  # 入力に無い創作日付

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions", {"input": "案件: 期限の明示なし。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    assert "2099-01-01" not in res["text"]  # text は actions から再構成され創作日付なし
    # review-8: raw は公開レスポンスに一切載せない(actions/text/capability/slot のみ)。
    assert "raw_audit" not in res and "raw" not in res
    assert set(res) <= {"capability", "actions", "text", "slot"}


def test_agent_strips_invented_yearless_dates():
    """review-8(major): 年なし和式日付(7月10日 / 9月30日)の創作も中和する。"""
    def fake(model_key, messages, max_chars):
        return "1. [9月30日] 何かする — 狙い\n2. 7月10日までに提出 — 期日"

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions", {"input": "案件: 期限の明示なし。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    joined = "\n".join(res["actions"])
    assert "9月30日" not in joined and "7月10日" not in joined  # 年なし和式の創作日付も除去
    assert "期限未定" in joined


def test_agent_keeps_fractions_and_quantities():
    """review-13(major): 分数・比率・数量(3/4ライン, 10/12件, 1/2案)を日付と誤認しない。"""
    def fake(model_key, messages, max_chars):
        return "1. 3/4ラインPoC — 範囲\n2. 10/12件の見積確定 — 進捗\n3. 1/2案を提示 — 比較"

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions", {"input": "案件: 期限の明示なし。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    joined = "\n".join(res["actions"])
    # 年なし slash は日付扱いしない → 数量表現が保持され「期限未定」に化けない。
    assert "3/4" in joined and "10/12" in joined and "1/2" in joined
    assert "期限未定" not in joined


def test_agent_keeps_yearless_date_present_in_input():
    """入力に明示された年なし日付(7月10日)は保持する(過剰除去しない)。"""
    def fake(model_key, messages, max_chars):
        return "1. 7月10日までに提案書を提出 — 期日厳守"

    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        res = ai_runtime.invoke_slot(
            d, "next-actions", {"input": "案件: 締切は 7月10日。"}, owner="u",
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    assert any("7月10日" in a for a in res["actions"])


def test_handle_nl2sql_rejected_sql_normalized_to_inference_error():
    """生成SQLがガードに拒否(他スキーマ等)されたら 500 でなく SlotInferenceError に正規化。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    def reject(question, *, schema, tables, model_key):
        raise SqlRejectedError("許可範囲外のテーブル参照: SH.SALES")

    ai_runtime._nl2sql_runner = reject
    try:
        d = sba_c_definition()
        with pytest.raises(ai_runtime.SlotInferenceError):
            ai_runtime.invoke_slot(
                d, "sales-rollup", {"input": "全社の売上"},
                owner="u", nl2sql_schema=SBA_C_NL2SQL_SCHEMA,
            )
    finally:
        ai_runtime._nl2sql_runner = ai_runtime._default_nl2sql


def test_nl2sql_schema_scope_guard():
    """schema allowlist ガード: 対象スキーマ＋許可テーブルのみ通し、他スキーマ/未許可は拒否。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # OK: 対象スキーマの許可テーブル(別名付き列参照・SELECT リストのカンマを誤検出しない)。
    nl2sql._assert_schema_scoped(
        'SELECT "s"."OWNER", SUM("s"."AMOUNT") FROM "JETUSE_SBA04"."SALES" "s" '
        'GROUP BY "s"."OWNER"',
        "JETUSE_SBA04", allow,
    )
    # OK: 対象スキーマ内の JOIN とサブクエリ。
    nl2sql._assert_schema_scoped(
        "SELECT * FROM JETUSE_SBA04.SALES s JOIN JETUSE_SBA04.DEALS d ON s.ID=d.ID",
        "JETUSE_SBA04", allow,
    )
    nl2sql._assert_schema_scoped(
        "SELECT * FROM (SELECT * FROM JETUSE_SBA04.SALES) t", "JETUSE_SBA04", allow
    )
    # NG: 他スキーマ参照(既存リソース読取の抜け道)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped('SELECT * FROM "SH"."SALES"', "JETUSE_SBA04", allow)
    # NG: カンマ結合での他スキーマすり抜け。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM JETUSE_SBA04.SALES s, SH.SALES x", "JETUSE_SBA04", allow
        )
    # NG: コメント区切りでの JOIN すり抜け(sanitize でコメント除去後に検出)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM JETUSE_SBA04.SALES s JOIN/**/SH.SALES x ON s.ID=x.ID",
            "JETUSE_SBA04", allow,
        )
    # NG: サブクエリ内の他スキーマ参照。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM (SELECT * FROM SH.SALES) t", "JETUSE_SBA04", allow
        )
    # NG: 対象スキーマだが未許可テーブル。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM JETUSE_SBA04.EMPLOYEES", "JETUSE_SBA04", allow
        )
    # NG: 非修飾テーブル参照(スキーマ解決が実行ユーザー依存 → 隔離保証にならない)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped("SELECT * FROM SALES", "JETUSE_SBA04", allow)


def test_nl2sql_guard_rejects_db_link():
    """F-001: DBリンク経由参照(`schema.table@link`)は隔離跨ぎの抜け道なので拒否する。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # 修飾済みに見えても別 DB の同名テーブルを読めてしまう → 拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM JETUSE_SBA04.SALES@REMOTE_LINK", "JETUSE_SBA04", allow
        )
    # JOIN 側に DB リンクを混ぜる抜け道も拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM JETUSE_SBA04.SALES s "
            "JOIN JETUSE_SBA04.DEALS@LINK d ON s.ID=d.ID",
            "JETUSE_SBA04", allow,
        )
    # ドメイン修飾付きリンク名(`@link.domain`)も拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM SALES@db.example.com", "JETUSE_SBA04", allow
        )
    # review-7 blocker: 引用識別子のリンク名(`@"REMOTE LINK"`)も拒否(検出漏れの修正)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            'SELECT * FROM JETUSE_SBA04.SALES@"REMOTE LINK"', "JETUSE_SBA04", allow
        )
    # 空白を挟む `@ "LINK"` も拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            'SELECT * FROM JETUSE_SBA04.SALES @ "L"', "JETUSE_SBA04", allow
        )


def test_nl2sql_guard_skips_string_literals():
    """review-6(minor): FROM 領域内の文字列リテラルはテーブル参照と誤認しない(字句スキップ)。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # OK: リテラルに 'SH.SALES' や '@link' を含んでも、実テーブルは許可スキーマのみ。
    nl2sql._assert_schema_scoped(
        "SELECT 'note: see SH.SALES@link', s.OWNER FROM JETUSE_SBA04.SALES s "
        "WHERE s.REGION = 'FROM SH.X'",
        "JETUSE_SBA04", allow,
    )
    # NG: リテラルの外にある実際の他スキーマ参照は依然拒否(リテラルスキップで緩めない)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT 'x' FROM SH.SALES", "JETUSE_SBA04", allow
        )


def test_nl2sql_guard_blocks_dangerous_functions():
    """review-12 blocker: 動的SQL・外部アクセス系の関数/パッケージを deny(SSRF/任意SQL防止)。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    danger_sqls = (
        # URI フェッチ(SSRF)
        "SELECT HTTPURITYPE('http://169.254.169.254/').getclob() FROM JETUSE_SBA04.SALES",
        "SELECT UTL_HTTP.request('http://x/') FROM JETUSE_SBA04.SALES",
        # 動的SQL/外部データ
        "SELECT DBMS_XMLGEN.getxml('SELECT * FROM SH.SALES') FROM JETUSE_SBA04.SALES",
        "SELECT * FROM JETUSE_SBA04.SALES WHERE 1=DBMS_SQL.execute(0)",
        "SELECT DBMS_CLOUD.get_object('x') FROM JETUSE_SBA04.SALES",
    )
    for sql in danger_sqls:
        with pytest.raises(SqlRejectedError):
            nl2sql._assert_schema_scoped(sql, "JETUSE_SBA04", allow)
    # 文字列リテラル内に関数名があるだけ(実呼び出しでない)場合も、保守的に拒否で安全側。
    # 通常の集計SQL(危険関数なし)は通る。
    nl2sql._assert_schema_scoped(
        "SELECT s.OWNER, SUM(s.AMOUNT) FROM JETUSE_SBA04.SALES s GROUP BY s.OWNER",
        "JETUSE_SBA04", allow,
    )


def test_nl2sql_guard_blocks_parenthesized_join():
    """review-11 blocker: 親括弧付き JOIN `(a JOIN other.x)` の他スキーマ参照を取りこぼさない。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # NG: 括弧付き JOIN の右辺に他スキーマ(内側 FROM が無く factor を取りこぼす)→ 保守的に拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM (JETUSE_SBA04.SALES s JOIN SH.SALES x ON s.ID=x.ID)",
            "JETUSE_SBA04", allow,
        )
    # NG: 同一スキーマでも括弧付き JOIN(非サブクエリ)は保守的に拒否(安全側)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT * FROM (JETUSE_SBA04.SALES s JOIN JETUSE_SBA04.DEALS d ON s.ID=d.ID)",
            "JETUSE_SBA04", allow,
        )
    # OK: 派生表サブクエリ `(SELECT ...)` は従来どおり許可(内側 FROM を検査)。
    nl2sql._assert_schema_scoped(
        "SELECT * FROM (SELECT * FROM JETUSE_SBA04.SALES) t", "JETUSE_SBA04", allow
    )


def test_nl2sql_guard_quoted_identifier_keywords():
    """review-10(minor): 予約語を含む二重引用識別子(別名 `"FROM"` 等)を誤走査しない。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # OK: 予約語を引用識別子に使った正常系(別名 "FROM"、列別名 "JOIN")。実テーブルは許可スキーマ。
    nl2sql._assert_schema_scoped(
        'SELECT "FROM"."OWNER" AS "JOIN" FROM JETUSE_SBA04.SALES "FROM"',
        "JETUSE_SBA04", allow,
    )
    # NG: 引用識別子を口実にしても実 FROM 句の他スキーマ参照は拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            'SELECT 1 AS "JOIN" FROM SH.SALES', "JETUSE_SBA04", allow
        )


def test_nl2sql_guard_blocks_apply_and_lateral_cross_schema():
    """review-9 blocker: CROSS/OUTER APPLY・LATERAL の右辺の他スキーマ参照も検査して拒否する。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    for sql in (
        "SELECT * FROM JETUSE_SBA04.SALES s CROSS APPLY SH.SALES x",
        "SELECT * FROM JETUSE_SBA04.SALES s OUTER APPLY SH.SALES x",
        "SELECT * FROM JETUSE_SBA04.SALES s, LATERAL SH.SALES x",
    ):
        with pytest.raises(SqlRejectedError):
            nl2sql._assert_schema_scoped(sql, "JETUSE_SBA04", allow)
    # OK: APPLY の右辺も許可スキーマなら通す(過剰拒否しない)。
    nl2sql._assert_schema_scoped(
        "SELECT * FROM JETUSE_SBA04.SALES s CROSS APPLY JETUSE_SBA04.DEALS d",
        "JETUSE_SBA04", allow,
    )


def test_nl2sql_guard_allows_extract_from_function():
    """review-8(major): EXTRACT(.. FROM ..) の関数内 FROM をテーブルソースと誤認しない。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # OK: EXTRACT の FROM はテーブルではない。実テーブルは許可スキーマのみ。
    nl2sql._assert_schema_scoped(
        "SELECT EXTRACT(YEAR FROM s.CLOSED_AT) yr, SUM(s.AMOUNT) "
        "FROM JETUSE_SBA04.SALES s GROUP BY EXTRACT(YEAR FROM s.CLOSED_AT)",
        "JETUSE_SBA04", allow,
    )
    # NG: 関数内 FROM を口実にしても、実 FROM 句の他スキーマ参照は依然拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "SELECT EXTRACT(YEAR FROM s.CLOSED_AT) FROM SH.SALES s",
            "JETUSE_SBA04", allow,
        )


def test_nl2sql_guard_allows_cte_but_checks_cte_body():
    """F-002: WITH(CTE)名の参照は許容(誤 502 を防ぐ)。ただし CTE 本体内の実テーブルは検査する。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # OK: CTE 名 `agg` を FROM で参照(非修飾でもクエリ内定義なので許可)。本体は許可テーブル。
    nl2sql._assert_schema_scoped(
        "WITH agg AS (SELECT OWNER, SUM(AMOUNT) t FROM JETUSE_SBA04.SALES GROUP BY OWNER) "
        "SELECT * FROM agg ORDER BY t DESC",
        "JETUSE_SBA04", allow,
    )
    # OK: 複数 CTE(カラムリスト付き)＋ CTE 同士の JOIN。
    nl2sql._assert_schema_scoped(
        "WITH a AS (SELECT ID FROM JETUSE_SBA04.SALES), "
        "b (id) AS (SELECT ID FROM JETUSE_SBA04.DEALS) "
        "SELECT * FROM a JOIN b ON a.ID=b.id",
        "JETUSE_SBA04", allow,
    )
    # NG: CTE 本体が他スキーマを参照する抜け道は依然として拒否(CTE 許可で隔離を緩めない)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "WITH leak AS (SELECT * FROM SH.SALES) SELECT * FROM leak",
            "JETUSE_SBA04", allow,
        )
    # NG: CTE を定義していない非修飾参照は従来どおり拒否(CTE 名に該当しない)。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "WITH agg AS (SELECT 1 FROM JETUSE_SBA04.SALES) SELECT * FROM other_tbl",
            "JETUSE_SBA04", allow,
        )


def test_nl2sql_guard_with_recursive_cte_name():
    """review-13 blocker: `WITH RECURSIVE` の `RECURSIVE` を CTE 名と誤収集しない。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    allow = {"SALES", "DEALS"}
    # NG: `RECURSIVE` を CTE 名と誤認すると `FROM recursive` の非修飾参照が通ってしまう → 拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "WITH RECURSIVE r AS (SELECT 1 FROM JETUSE_SBA04.SALES) SELECT * FROM recursive",
            "JETUSE_SBA04", allow,
        )
    # OK: `WITH RECURSIVE r AS (...) SELECT * FROM r` は CTE `r` 参照を許可(本体は許可スキーマ)。
    nl2sql._assert_schema_scoped(
        "WITH RECURSIVE r AS (SELECT 1 FROM JETUSE_SBA04.SALES) SELECT * FROM r",
        "JETUSE_SBA04", allow,
    )
    # OK: CTE 名が `recursive`(キーワードではなく識別子)のケースも正しく許可。
    nl2sql._assert_schema_scoped(
        "WITH recursive AS (SELECT 1 FROM JETUSE_SBA04.SALES) SELECT * FROM recursive",
        "JETUSE_SBA04", allow,
    )
    # NG: WITH RECURSIVE でも CTE 本体の他スキーマ参照は拒否。
    with pytest.raises(SqlRejectedError):
        nl2sql._assert_schema_scoped(
            "WITH RECURSIVE r AS (SELECT 1 FROM SH.SALES) SELECT * FROM r",
            "JETUSE_SBA04", allow,
        )


def test_default_nl2sql_forwards_model(monkeypatch):
    """_default_nl2sql はスロットの model_key を Select AI(run_nl2sql_for_schema)へ伝播する。"""
    from jetuse_core import nl2sql

    seen = {}

    def fake_run(question, *, schema, tables, model):
        seen.update(schema=schema, tables=tables, model=model)
        return {"sql": "x", "columns": [], "rows": [], "row_count": 0, "truncated": False}

    monkeypatch.setattr(nl2sql, "run_nl2sql_for_schema", fake_run)
    ai_runtime._default_nl2sql(
        "売上", schema="JETUSE_SBA04", tables=["sales"], model_key="llama-3.3-70b"
    )
    assert seen["model"] == "llama-3.3-70b"  # モデル指定が NL2SQL でも無視されない
    assert seen["schema"] == "JETUSE_SBA04"


def test_run_nl2sql_for_schema_requires_tables():
    """tables 未指定の専用スキーマ照会は実行しない(allowlist 必須)。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    from jetuse_core import nl2sql

    with pytest.raises(SqlRejectedError):
        nl2sql.run_nl2sql_for_schema("売上", schema="JETUSE_SBA04", tables=[])


def test_draft_empty_corpus_is_business_email_no_faq():
    """SBA-C(コーパス空)のメール下書きは FAQ 文脈を渡さない(営業メールに不適切な文言を防ぐ)。"""
    seen = {}

    def fake(model_key, messages, max_chars):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[-1]["content"]
        return "件名: ご提案のお礼\n\n山田製作所 ご担当者様\n\n本日はありがとうございました。"

    orig = ai_runtime._completer
    ai_runtime._completer = fake
    try:
        d = sba_c_definition()
        out = ai_runtime.invoke_slot(
            d, "email-draft", {"input": "山田製作所へPoC合意のお礼"}, owner="u"
        )
    finally:
        ai_runtime._completer = orig
    assert out["capability"] == "draft"
    assert "FAQ" not in seen["user"]  # コーパス空 → FAQ 文脈を渡さない
    assert "営業" in seen["system"]  # 営業メール用システムプロンプト
    assert out["citations"] == []


def test_draft_nonempty_corpus_is_support_reply_backward_compat():
    """review-10: 共有 draft capability が SBA-A(コーパスあり)で従来のサポート返信を維持する。"""
    from jetuse_core.plugins.sample_app_builtin import knowledge_corpus, sba_a_definition

    seen = {}

    def fake(model_key, messages, max_chars):
        seen["sys"] = messages[0]["content"]
        return "お問い合わせありがとうございます。確認のうえご連絡します。"

    ai_runtime._completer = fake
    try:
        d = sba_a_definition()
        corpus = knowledge_corpus(d)
        res = ai_runtime.invoke_slot(
            d, "reply-draft", {"input": "パスワードを忘れました"}, owner="u", corpus=corpus,
        )
    finally:
        ai_runtime._completer = ai_runtime._default_completer
    # コーパスありはサポート返信プロンプト(FAQ 根拠)で、営業メール分岐に落ちない。
    assert "カスタマーサポート" in seen["sys"]
    assert res["capability"] == "draft" and res["draft"]


def test_handle_nl2sql_without_schema_is_generate_only():
    """マージ後のデュアルモード(SBA-B 統合)契約。

    nl2sql ハンドラは `ctx.nl2sql_schema` が無ければ SBA-B 互換の「生成のみ」
    (SELECT を返すだけで実行しない)になる。**行を捏造せず実行ランナーも呼ばない**ので
    「成功偽装」にはならない。SBA-C の『売上集計は必ず実 ADB(JETUSE_SBA04)を実行する』
    保証は route 層が担保する(resolve_app が schema を必ず渡す)
    → test_invoke_sales_rollup_route / *_503 / *_502。
    """
    d = sba_c_definition()
    ran = {"runner": False}

    def fake(model_key, messages, max_chars):
        return "SELECT owner, SUM(amount) AS total FROM sales GROUP BY owner"

    def boom_runner(*a, **k):
        ran["runner"] = True
        raise AssertionError("schema 未指定で実行ランナーを呼んではならない")

    orig_runner = ai_runtime._nl2sql_runner
    ai_runtime._completer = fake
    ai_runtime._nl2sql_runner = boom_runner
    try:
        res = ai_runtime.invoke_slot(d, "sales-rollup", {"input": "売上合計"}, owner="u")
    finally:
        ai_runtime._completer = ai_runtime._default_completer
        ai_runtime._nl2sql_runner = orig_runner

    assert res["capability"] == "nl2sql"
    assert res["sql"].lstrip().upper().startswith("SELECT")
    assert "rows" not in res       # 生成のみ。実行結果(行)は返さない=成功偽装しない
    assert ran["runner"] is False  # 実行ランナーは呼ばれない


# --- ルート経由 -----------------------------------------------------------


def test_list_includes_sba_c():
    res = client.get("/api/sample-apps")
    assert res.status_code == 200
    ids = {a["id"] for a in res.json()["sample_apps"]}
    assert SBA_C_INSTANCE_ID in ids


def test_get_sba_c_definition_no_knowledge_dataset():
    res = client.get(f"/api/sample-apps/{SBA_C_INSTANCE_ID}")
    assert res.status_code == 200, res.text
    body = res.json()
    # SBA-C は RAG コーパスを持たない → knowledge_dataset は付かない。
    assert "knowledge_dataset" not in body
    assert all(body["slot_bindings"].values())
    assert set(body["slot_bindings"]) == {s["key"] for s in body["definition"]["aiSlots"]}


def test_invoke_minutes_then_agent_chain():
    """議事録要約 → 次アクション提案の連動(出力を入力に渡せる形で返る)。"""
    r1 = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/minutes-summary/invoke",
        json={"input": "山田部長: MESが老朽化し保守切れが近い。クラウド移行に前向き。"},
    )
    assert r1.status_code == 200, r1.text
    summary = r1.json()["summary"]
    r2 = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/next-actions/invoke",
        json={"input": f"案件: 山田製作所\n議事録要約:\n{summary}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["actions"]


def test_invoke_email_draft_slot():
    res = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/email-draft/invoke",
        json={"input": "山田製作所へPoC範囲合意のお礼と次回日程の打診"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["capability"] == "draft"


def test_invoke_sales_rollup_route(monkeypatch):
    """売上集計スロットは route 経由で nl2sql_schema=JETUSE_SBA04 が runner に渡る。"""
    captured = {}

    def fake_runner(question, *, schema, tables, model_key):
        captured["schema"] = schema
        return {"sql": "SELECT 1 FROM dual", "columns": ["X"], "rows": [["1"]],
                "row_count": 1, "truncated": False}

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", fake_runner)
    res = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/sales-rollup/invoke",
        json={"input": "今四半期の地域別売上合計"},
    )
    assert res.status_code == 200, res.text
    assert captured["schema"] == "JETUSE_SBA04"
    assert res.json()["schema"] == "JETUSE_SBA04"


def test_sales_rollup_route_db_unavailable_returns_503(monkeypatch):
    """F-003: DB 接続障害(marker 付き)は route で 503 に写像する(500 でない)。"""
    def boom_conn(question, *, schema, tables, model_key):
        raise RuntimeError("DPY-6005: cannot connect to database instance")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", boom_conn)
    res = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/sales-rollup/invoke",
        json={"input": "地域別売上"},
    )
    assert res.status_code == 503, res.text


def test_sales_rollup_route_rejected_sql_returns_502(monkeypatch):
    """生成SQLがガード拒否(他スキーマ/DBリンク等)なら route で 502(成功偽装/500 露出を防ぐ)。"""
    from jetuse_shared.sqlguard import SqlRejectedError

    def reject(question, *, schema, tables, model_key):
        raise SqlRejectedError("DBリンク経由のテーブル参照は不可(隔離跨ぎ防止)")

    monkeypatch.setattr(ai_runtime, "_nl2sql_runner", reject)
    res = client.post(
        f"/api/sample-apps/{SBA_C_INSTANCE_ID}/slots/sales-rollup/invoke",
        json={"input": "全社の売上"},
    )
    assert res.status_code == 502, res.text
