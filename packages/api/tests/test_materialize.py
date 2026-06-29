"""dataset 定義の実テーブル・マテリアライズ(BE-02)の単体テスト。

実 ADB には接続せず、所有権レジストリ・テーブル存在・CREATE/INSERT/GRANT/DROP/COUNT を再現する
インメモリ fake 接続で検証する(DBMS_LOCK 直列化は nullcontext で無効化し、実機 E2E で確認):
  - 列型マップ・string バイト長採寸・>4000 拒否・大小衝突列の拒否・seed 値 coerce。
  - 読取ユーザへの GRANT SELECT(権限分離)。
  - **非破壊**: 既存 ready 表は seed 方針が変わっても reuse(DROP しない)。空の未 seed 表へ
    seeded=True が来たら(空のときに限り)seed を注入する(False→True)。
  - 再構築は recreate / pending(不完全) / fingerprint(形)不一致 のときだけ。
  - 所有権: 管理外 / 別 owner の同名物理表は触らず fail-closed。
  - 設定整合: target_schema()≠adb_user は MaterializeConfigError。
  - seed 方針: seeded=False は表だけ作って seed しない・幅は上限 4000。
実機(loop ADB)での展開・NL2SQL 成立・真の同時起動(DBMS_LOCK 直列化)は E2E に委ねる。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import types

import pytest

from jetuse_core import materialize
from jetuse_core.plugins.sample_app import Dataset, DatasetField
from jetuse_core.plugins.sample_app_builtin_sba_b import sba_b_definition

# --- fake 接続 ----------------------------------------------------------------


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.connection = conn
        self.db = conn.db
        self._result: list[tuple] = []

    def execute(self, sql: str, **b):
        s = " ".join(sql.split())
        if "CREATE TABLE JETUSE_MATERIALIZED_DATASETS" in s:
            return
        if s.startswith("SELECT status, fingerprint, owner_id, seeded FROM JETUSE_MATERIALIZED"):
            row = self.db["registry"].get((b["s"].upper(), b["t"].upper()))
            self._result = [row] if row else []
        elif s.startswith("MERGE INTO JETUSE_MATERIALIZED_DATASETS"):
            self.db["registry"][(b["s"].upper(), b["t"].upper())] = (
                b["st"], b["fp"], b["o"], b["sd"]
            )
            self.db["merges"].append((b["t"].upper(), b["st"], b["o"], b["sd"]))
        elif s.startswith("DELETE FROM JETUSE_MATERIALIZED_DATASETS"):
            self.db["registry"].pop((b["s"].upper(), b["t"].upper()), None)
        elif s.startswith("SELECT comments FROM all_tab_comments"):
            owner = self.db["markers"].get((b["o"].upper(), b["t"].upper()))
            self._result = [(f"JETUSE_MAT:{owner}",)] if owner is not None else [(None,)]
        elif s.startswith("SELECT 1 FROM all_tables"):
            key = (b["o"].upper(), b["t"].upper())
            self._result = [(1,)] if key in self.db["tables"] else []
        elif s.startswith("SELECT COUNT(*) FROM"):
            self._result = [(self.db.get("row_count", 0),)]
        elif s.startswith("CREATE TABLE"):
            ident = s.split("CREATE TABLE", 1)[1].split("(", 1)[0].strip()
            if self.db.get("create_name_exists"):
                raise RuntimeError("ORA-00955: name is already used by an existing object")
            self.db["created"].append(s)
            self.db["tables"].add(_owner_table(ident))
        elif s.startswith("COMMENT ON TABLE"):
            ident = s.split("COMMENT ON TABLE", 1)[1].split(" IS ", 1)[0].strip()
            owner = s.split(" IS '", 1)[1].rsplit("'", 1)[0].split(":", 1)[1]
            self.db["markers"][_owner_table(ident)] = owner
        elif s.startswith("DROP TABLE"):
            ident = s.split("DROP TABLE", 1)[1].rsplit("PURGE", 1)[0].strip()
            self.db["dropped"].append(s)
            self.db["tables"].discard(_owner_table(ident))
            self.db["markers"].pop(_owner_table(ident), None)
        elif s.startswith("GRANT SELECT"):
            self.db["grants"].append(s)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

    def executemany(self, sql: str, seq):
        assert " ".join(sql.split()).startswith("INSERT INTO"), sql
        self.db["inserts"].append((sql, list(seq)))

    def fetchone(self):
        return self._result[0] if self._result else None


def _owner_table(ident: str) -> tuple[str, str]:
    schema, _, table = ident.partition(".")
    return schema.strip().upper(), table.strip().strip('"').upper()


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.committed = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1


def _set_reg(db, table, status, fp, owner="", seeded=0):
    db["registry"][("JETUSE_APP", table)] = (status, fp, owner, seeded)


def _reset(db):
    """前段の materialize で溜まったログ(created/dropped/grants/inserts/merges)を空にする。"""
    for k in ("created", "dropped", "grants", "inserts", "merges"):
        db[k].clear()


@pytest.fixture
def fake_db(monkeypatch):
    db = {
        "tables": set(), "registry": {}, "markers": {}, "merges": [],
        "created": [], "dropped": [], "grants": [], "inserts": [], "row_count": 0,
    }

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(materialize, "connect", fake_connect)
    monkeypatch.setattr(materialize, "_table_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(
        materialize, "get_settings",
        lambda: types.SimpleNamespace(
            adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY", sample_db_schema=""
        ),
    )
    return db


# --- 型マップ / 採寸 / coerce --------------------------------------------------


def test_oracle_type_maps_field_types():
    assert materialize.oracle_type("string").startswith("VARCHAR2")
    assert "BYTE" in materialize.oracle_type("string")
    assert materialize.oracle_type("text") == "CLOB"
    assert materialize.oracle_type("number") == "NUMBER"
    assert materialize.oracle_type("boolean") == "NUMBER(1)"
    assert materialize.oracle_type("date") == "DATE"
    assert materialize.oracle_type("datetime") == "TIMESTAMP"
    assert materialize.oracle_type("weird").startswith("VARCHAR2")


def test_string_column_type_is_always_4000_byte():
    """string 列は seed 長に関わらず常に VARCHAR2(4000 BYTE)(差替え耐性＋fingerprint 安定)。"""
    f = DatasetField(name="x", type="string")
    assert materialize._column_type(f) == "VARCHAR2(4000 BYTE)"


def test_fingerprint_is_seed_independent():
    """seed 内容/件数が変わっても fingerprint は不変(列幅固定)→ 起動で再構築を誘発しない。"""
    ds1 = Dataset(name="t", fields=[DatasetField(name="a", type="string")], seed=[{"a": "x"}])
    ds2 = Dataset(name="t", fields=[DatasetField(name="a", type="string")],
                  seed=[{"a": "y" * 100}, {"a": "z"}])
    assert materialize._fingerprint(ds1) == materialize._fingerprint(ds2)


def test_assert_string_widths_rejects_over_limit():
    ds = Dataset(
        name="big", fields=[DatasetField(name="t", type="string")],
        seed=[{"t": "a" * 4001}],
    )
    with pytest.raises(ValueError, match="VARCHAR2 上限"):
        materialize._assert_string_widths(ds)


def test_assert_unique_physical_columns_rejects_case_collision():
    ds = Dataset(
        name="c",
        fields=[DatasetField(name="foo", type="string"), DatasetField(name="FOO", type="number")],
        seed=[],
    )
    with pytest.raises(ValueError, match="列名が衝突"):
        materialize._assert_unique_physical_columns(ds)


def test_coerce_converts_dates_and_blanks():
    assert materialize._coerce("date", "2026-06-20") == _dt.date(2026, 6, 20)
    assert materialize._coerce("datetime", "2026-06-20T10:30:00") == _dt.datetime(
        2026, 6, 20, 10, 30, 0
    )
    assert materialize._coerce("number", 42) == 42
    assert materialize._coerce("boolean", True) == 1
    assert materialize._coerce("boolean", False) == 0
    assert materialize._coerce("date", "") is None
    assert materialize._coerce("string", None) is None


# --- target_schema 整合 -------------------------------------------------------


def test_target_schema_falls_back_to_adb_user(fake_db):
    assert materialize.target_schema() == "JETUSE_APP"


def test_target_schema_prefers_sample_db_schema(monkeypatch):
    monkeypatch.setattr(
        materialize, "get_settings",
        lambda: types.SimpleNamespace(
            adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY",
            sample_db_schema="JETUSE_SBA03",
        ),
    )
    assert materialize.target_schema() == "JETUSE_SBA03"


def test_materialize_config_error_when_target_differs_from_conn_user(monkeypatch, fake_db):
    monkeypatch.setattr(
        materialize, "get_settings",
        lambda: types.SimpleNamespace(
            adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY",
            sample_db_schema="JETUSE_OTHER",
        ),
    )
    with pytest.raises(materialize.MaterializeConfigError, match="接続ユーザ"):
        materialize.materialize_definition(sba_b_definition())


# --- 展開(CREATE + seed + GRANT + レジストリ) ------------------------------


def test_materialize_creates_tables_seeds_and_grants(fake_db):
    result = materialize.materialize_definition(sba_b_definition(), owner="sba-b")

    assert result["schema"] == "JETUSE_APP"
    assert result["query_user"] == "JETUSE_QUERY"
    assert result["owner"] == "sba-b"
    assert result["seeded"] is True
    names = {d["table"]: d for d in result["datasets"]}
    assert set(names) == {"INVENTORY", "ORDERS"}
    assert all(d["action"] == "created" for d in result["datasets"])
    assert names["INVENTORY"]["rows"] == 15
    assert names["ORDERS"]["rows"] == 24

    assert len(fake_db["created"]) == 2
    grants = " ".join(fake_db["grants"])
    assert grants.count("GRANT SELECT") == 2
    assert "TO JETUSE_QUERY" in grants

    inv_ddl = next(c for c in fake_db["created"] if '"INVENTORY"' in c)
    assert inv_ddl.startswith('CREATE TABLE JETUSE_APP."INVENTORY"')
    assert '"QUANTITY" NUMBER' in inv_ddl
    assert '"UPDATED_AT" DATE' in inv_ddl
    assert '"PRODUCT_CODE" VARCHAR2' in inv_ddl

    assert fake_db["registry"][("JETUSE_APP", "INVENTORY")][0] == "ready"
    statuses = [m[1] for m in fake_db["merges"] if m[0] == "INVENTORY"]
    assert statuses == ["pending", "ready"]


def test_materialize_skips_seed_when_not_seeded(fake_db):
    result = materialize.materialize_definition(sba_b_definition(), seeded=False)
    assert result["seeded"] is False
    assert all(d["rows"] == 0 and d["action"] == "created" for d in result["datasets"])
    assert fake_db["inserts"] == []
    inv_ddl = next(c for c in fake_db["created"] if '"INVENTORY"' in c)
    assert "VARCHAR2(4000 BYTE)" in inv_ddl


def test_materialize_idempotent_reuses_when_ready_and_fingerprint_matches(fake_db):
    materialize.materialize_definition(sba_b_definition())
    _reset(fake_db)

    result = materialize.materialize_definition(sba_b_definition())
    assert all(d["action"] == "reused" for d in result["datasets"])
    assert fake_db["created"] == [] and fake_db["inserts"] == [] and fake_db["dropped"] == []
    assert len(fake_db["grants"]) == 2


def test_launch_seed_strategy_change_is_non_destructive(fake_db):
    """既存 ready 表は seeded が変わっても DROP しない(F-001 既存データ保全)。"""
    materialize.materialize_definition(sba_b_definition())  # seeded=True
    _reset(fake_db)
    result = materialize.materialize_definition(sba_b_definition(), seeded=False)
    assert all(d["action"] == "reused" for d in result["datasets"])
    assert fake_db["dropped"] == [] and fake_db["created"] == [] and fake_db["inserts"] == []


def test_seeds_empty_table_on_false_then_true(fake_db):
    """空の未 seed 表へ後から seeded=True が来たら、空のときに限り seed を注入する。"""
    materialize.materialize_definition(sba_b_definition(), seeded=False)  # 空作成
    _reset(fake_db)
    fake_db["row_count"] = 0  # 空のまま
    result = materialize.materialize_definition(sba_b_definition(), seeded=True)
    assert all(d["action"] == "seeded" for d in result["datasets"])
    assert fake_db["dropped"] == []  # 非破壊(DROP せず INSERT)
    assert len(fake_db["inserts"]) == 2
    assert fake_db["registry"][("JETUSE_APP", "INVENTORY")][3] == 1  # seeded フラグ立つ


def test_does_not_reseed_nonempty_table(fake_db):
    """seeded=False 表に行が入っている場合(顧客データ)は seeded=True でも重複投入しない。"""
    materialize.materialize_definition(sba_b_definition(), seeded=False)
    fake_db["inserts"].clear()
    fake_db["row_count"] = 5  # 既に行あり
    result = materialize.materialize_definition(sba_b_definition(), seeded=True)
    assert all(d["action"] == "reused" for d in result["datasets"])
    assert fake_db["inserts"] == []


def test_materialize_recreate_drops_then_recreates(fake_db):
    materialize.materialize_definition(sba_b_definition())
    _reset(fake_db)
    result = materialize.materialize_definition(sba_b_definition(), recreate=True)
    assert all(d["action"] == "recreated" for d in result["datasets"])
    assert len(fake_db["dropped"]) == 2 and len(fake_db["created"]) == 2
    assert len(fake_db["inserts"]) == 2


def test_materialize_recovers_from_pending_when_empty(fake_db):
    """pending(不完全)かつ **空** の表は安全に作り直す(行 0 なのでデータ損失なし)。"""
    materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    fp = fake_db["registry"][("JETUSE_APP", "INVENTORY")][1]
    _set_reg(fake_db, "INVENTORY", "pending", fp, owner="sba-b")
    fake_db["row_count"] = 0
    _reset(fake_db)
    result = materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    inv = next(d for d in result["datasets"] if d["table"] == "INVENTORY")
    orders = next(d for d in result["datasets"] if d["table"] == "ORDERS")
    assert inv["action"] == "recreated"
    assert orders["action"] == "reused"


def test_materialize_rebuilds_on_fingerprint_mismatch_when_empty(fake_db):
    """形 fingerprint 不一致かつ **空** なら作り直す(空 DROP は安全)。"""
    materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    _set_reg(fake_db, "INVENTORY", "ready", "deadbeef", owner="sba-b")
    fake_db["row_count"] = 0
    fake_db["dropped"].clear()
    result = materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    inv = next(d for d in result["datasets"] if d["table"] == "INVENTORY")
    assert inv["action"] == "recreated"
    assert any('"INVENTORY"' in d for d in fake_db["dropped"])


def test_never_drops_pending_table_without_owner_marker(fake_db):
    """レジストリ claim だけ在り **物理表に所有マーカーが無い**(外部表が割り込んだ)場合、空でも
    DROP せず fail-closed(F-002: claim だけで他者の表を消さない)。"""
    # pending claim(owner=sba-b)＋同名の物理表は在るが、マーカーは未焼付(=外部表/claim 残骸)。
    _set_reg(fake_db, "INVENTORY", "pending", "deadbeef", owner="sba-b")
    fake_db["tables"].add(("JETUSE_APP", "INVENTORY"))  # マーカー無しの物理表
    fake_db["row_count"] = 0  # 空でも所有未検証なら触らない
    with pytest.raises(materialize.MaterializeConflictError, match="所有を物理表で検証できない"):
        materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    assert fake_db["dropped"] == []


def test_external_create_during_claim_raises_conflict_and_clears_claim(fake_db):
    """新規作成中(claim 後)に同名表が外部で出現(ORA-00955)したら、claim を取り消して
    fail-closed(その表を後で誤って DROP しない。F-002 クラッシュ/競合境界)。"""
    fake_db["create_name_exists"] = True  # CREATE が ORA-00955 を返す
    with pytest.raises(materialize.MaterializeConflictError, match="ORA-00955"):
        materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    # claim(pending)は残さない(残すと次回の空 DROP 判定で他者表を消しかねない)。
    assert ("JETUSE_APP", "INVENTORY") not in fake_db["registry"]
    assert fake_db["dropped"] == []


def test_recreate_refuses_when_physical_marker_mismatch(fake_db):
    """recreate=True でも、レジストリ owner が一致しても **物理表のマーカー** が違えば DROP しない
    (レジストリが書き換わっても物理表で検証=他者データ保護。F-002)。"""
    materialize.materialize_definition(sba_b_definition(), owner="app-a")  # 物理 marker=app-a
    # レジストリだけ owner=app-b に偽装(物理表マーカーは app-a のまま)。
    _set_reg(fake_db, "INVENTORY", "ready", "deadbeef", owner="app-b")
    with pytest.raises(materialize.MaterializeConflictError, match="所有を物理表で検証できない"):
        materialize.materialize_definition(sba_b_definition(), owner="app-b", recreate=True)
    assert not any('"INVENTORY"' in d for d in fake_db["dropped"])


def test_launch_finalizes_nonempty_pending_without_drop(fake_db):
    """pending でも **データがあり形一致** なら DROP せず ready に確定して reuse(作りかけ回復)。"""
    materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    fp = fake_db["registry"][("JETUSE_APP", "INVENTORY")][1]
    _set_reg(fake_db, "INVENTORY", "pending", fp, owner="sba-b")
    fake_db["row_count"] = 5  # データあり・形一致
    _reset(fake_db)
    result = materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    inv = next(d for d in result["datasets"] if d["table"] == "INVENTORY")
    assert inv["action"] == "reused"
    assert fake_db["dropped"] == []
    assert fake_db["registry"][("JETUSE_APP", "INVENTORY")][0] == "ready"  # 確定


def test_conflict_on_nonempty_fingerprint_mismatch(fake_db):
    """**非空 × 形 fingerprint 不一致** は起動で DROP も silent reuse もせず明示 conflict(F2)。

    古い形のままデータを抱えた表を ready に戻さない。recreate=True / migration を要求する。"""
    materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    _set_reg(fake_db, "INVENTORY", "ready", "deadbeef", owner="sba-b")  # 形不一致
    fake_db["row_count"] = 5  # データあり
    _reset(fake_db)
    with pytest.raises(materialize.MaterializeConflictError, match="形.*不一致.*データ"):
        materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    assert fake_db["dropped"] == []
    # ready(古い形)に戻していない=起動を失敗させて人手の migration/recreate を促す。
    assert fake_db["registry"][("JETUSE_APP", "INVENTORY")][1] == "deadbeef"


def test_reuse_refuses_when_physical_marker_missing(fake_db):
    """レジストリ ready でも **物理表が外部置換(マーカー欠落)** なら reuse/GRANT せず conflict
    (registry を信用せず物理表で所有検証=他者表を読取ユーザへ晒さない。F-002 全経路)。"""
    materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    _reset(fake_db)
    fake_db["markers"].pop(("JETUSE_APP", "INVENTORY"), None)  # 外部 DROP+CREATE で置換を模す
    with pytest.raises(materialize.MaterializeConflictError, match="所有を物理表で検証できない"):
        materialize.materialize_definition(sba_b_definition(), owner="sba-b")
    assert fake_db["grants"] == [] and fake_db["dropped"] == []


def test_materialize_refuses_unmanaged_existing_table(fake_db):
    fake_db["tables"].add(("JETUSE_APP", "INVENTORY"))  # 管理外の物理表
    with pytest.raises(materialize.MaterializeConflictError, match="管理外"):
        materialize.materialize_definition(sba_b_definition())
    assert fake_db["dropped"] == [] and fake_db["grants"] == []


def test_materialize_refuses_other_owner_table(fake_db):
    """別アプリ(別 owner)が所有する同名表は混ぜない(fail-closed)。"""
    materialize.materialize_definition(sba_b_definition(), owner="app-a")
    with pytest.raises(materialize.MaterializeConflictError, match="別アプリ"):
        materialize.materialize_definition(sba_b_definition(), owner="app-b")


def test_owner_empty_normalized_to_sentinel(fake_db):
    """owner 未指定は安定 sentinel に正規化(Oracle 空文字→NULL で所有比較が壊れるのを防ぐ。F4)。"""
    result = materialize.materialize_definition(sba_b_definition())  # owner 未指定
    assert result["owner"] == materialize._DEFAULT_OWNER
    # レジストリ owner も sentinel(NULL 化しない)。マーカーも同じ → 次回 reuse できる。
    assert fake_db["registry"][("JETUSE_APP", "INVENTORY")][2] == materialize._DEFAULT_OWNER
    _reset(fake_db)
    again = materialize.materialize_definition(sba_b_definition())
    assert all(d["action"] == "reused" for d in again["datasets"])


def test_owner_too_long_rejected(fake_db):
    """owner_id 列(VARCHAR2(200))を超える owner は予測可能な ValueError(F-004)。"""
    with pytest.raises(ValueError, match="owner が長すぎ"):
        materialize.materialize_definition(sba_b_definition(), owner="x" * 201)


def test_materialize_rejects_bad_identifiers(fake_db):
    with pytest.raises(ValueError, match="不正なスキーマ識別子"):
        materialize.materialize_definition(sba_b_definition(), schema="bad-schema; DROP")
    with pytest.raises(ValueError, match="不正な読取ユーザ識別子"):
        materialize.materialize_definition(sba_b_definition(), query_user="x; GRANT")


def test_table_lock_fail_closed_when_dbms_lock_unavailable(monkeypatch):
    """DBMS_LOCK が使えない構成は縮退せず MaterializeConfigError(fail-closed)。

    fake_db fixture は使わず(_table_lock を no-op 化しないため)、callproc が DBMS_LOCK 不可を
    模す接続を差し込んで **実 `_table_lock`** を通す。
    """
    db = {"tables": set(), "registry": {}, "markers": {}, "merges": [], "created": [],
          "dropped": [], "grants": [], "inserts": [], "row_count": 0}

    class NoLockCursor(FakeCursor):
        def var(self, *a, **k):
            return types.SimpleNamespace(getvalue=lambda: "h")

        def callproc(self, *a, **k):
            raise RuntimeError("PLS-00201: identifier 'DBMS_LOCK' must be declared")

    class NoLockConn(FakeConn):
        def cursor(self):
            return NoLockCursor(self)

    @contextlib.contextmanager
    def conn_cm():
        yield NoLockConn(db)

    monkeypatch.setattr(materialize, "connect", conn_cm)
    monkeypatch.setattr(
        materialize, "get_settings",
        lambda: types.SimpleNamespace(
            adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY", sample_db_schema=""
        ),
    )
    with pytest.raises(materialize.MaterializeConfigError, match="DBMS_LOCK"):
        materialize.materialize_definition(sba_b_definition())


# --- materialize_app（起動アダプタ） ------------------------------------------


def test_materialize_app_resolves_builtin_sba_b(fake_db):
    result = materialize.materialize_app("builtin-sba-b")
    assert {d["table"] for d in result["datasets"]} == {"INVENTORY", "ORDERS"}
    assert result["owner"] == "builtin-sba-b"


def test_materialize_app_passes_seeded_flag(fake_db):
    result = materialize.materialize_app("builtin-sba-b", seeded=False)
    assert result["seeded"] is False
    assert fake_db["inserts"] == []


def test_materialize_app_skips_dedicated_schema_app(fake_db):
    """専用外部スキーマ(SBA-C / JETUSE_SBA04≠target_schema)は auto-materialize 対象外。"""
    result = materialize.materialize_app("builtin-sba-c")
    assert result["skipped"] == "dedicated_schema"
    assert result["datasets"] == []
    assert fake_db["created"] == []  # adb_user に誤った表を作らない


def test_materialize_app_unknown_instance_returns_none(fake_db):
    assert materialize.materialize_app("nope-not-real") is None


def test_materialize_app_skips_when_sample_db_schema_is_preprovisioned(monkeypatch):
    """SAMPLE_DB_SCHEMA が接続ユーザと別 = 事前プロビジョニング(legacy)。launch は config error で
    落とさず auto をスキップし、その既存スキーマを読取先として返す(F-003 後方互換)。"""
    created: list = []

    @contextlib.contextmanager
    def fake_connect():  # 呼ばれたら失敗(=DB に触れず skip するのが期待)
        created.append("connected")
        yield FakeConn({"tables": set(), "registry": {}, "markers": {}, "merges": [],
                        "created": [], "dropped": [], "grants": [], "inserts": [],
                        "row_count": 0})

    monkeypatch.setattr(materialize, "connect", fake_connect)
    monkeypatch.setattr(
        materialize, "get_settings",
        lambda: types.SimpleNamespace(
            adb_user="JETUSE_APP", adb_query_user="JETUSE_QUERY",
            sample_db_schema="JETUSE_LEGACY",  # adb_user と別 → 事前プロビジョニング扱い
        ),
    )
    result = materialize.materialize_app("builtin-sba-b")
    assert result["skipped"] == "pre_provisioned_schema"
    assert result["schema"] == "JETUSE_LEGACY"
    assert result["datasets"] == []
    assert created == []  # DB へ接続せず=表を作らない(launch を壊さない)
