"""migrate.py 再実行許容(SP2-01 / specs/18 §1.1)の単体テスト。

「DDL 成功 → version 記録前クラッシュ」の再実行で ORA-01430/00955/01408 を検知したとき、
ORA コードだけで成功と断定せず、期待事後条件をデータディクショナリで完全一致検証してから
version を記録する。形違いは停止(人間対応)。実 ADB での fault-injection は E2E 側で実施。
"""

import pytest

from jetuse_core import migrate


class FakeDict:
    """USER_TAB_COLUMNS / USER_CONSTRAINTS / USER_CONS_COLUMNS / USER_INDEXES /
    USER_IND_COLUMNS の canned 応答。"""

    def __init__(self, columns=None, checks=None, indexes=None, pks=None):
        # columns: {(table, col): (data_type, char_length, char_used, nullable, data_default)}
        # checks: {table: [(search_condition, status, validated), ...]}
        # indexes: {index: (table, [col, ...])}
        # pks: {table: (constraint_name, status, validated, [col, ...])}
        self.columns = columns or {}
        self.checks = checks or {}
        self.indexes = indexes or {}
        self.pks = pks or {}
        self._one = None
        self._all = []

    def execute(self, sql, **binds):
        low = sql.lower()
        self._one, self._all = None, []
        if "user_tab_columns" in low:
            self._one = self.columns.get((binds["t"], binds["c"]))
        elif "constraint_type = 'p'" in low:
            entry = self.pks.get(binds["t"])
            self._one = (entry[0], entry[1], entry[2]) if entry else None
        elif "user_cons_columns" in low:
            for name, _s, _v, cols in self.pks.values():
                if name == binds["cn"]:
                    self._all = [(c,) for c in cols]
        elif "user_constraints" in low:
            self._all = list(self.checks.get(binds["t"], []))
        elif "user_ind_columns" in low:
            _, cols = self.indexes.get(binds["i"], (None, []))
            self._all = [(c,) for c in cols]
        elif "user_indexes" in low:
            entry = self.indexes.get(binds["i"])
            self._one = (entry[0],) if entry else None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


# 017 の期待どおりの辞書状態
COLS_017 = {
    ("DEMOS", "DESCRIPTION"): ("VARCHAR2", 1000, "C", "Y", None),
    ("DEMOS", "CONFIG"): ("CLOB", None, None, "N", "'{}'"),
    ("DEMOS", "STATUS"): ("VARCHAR2", 20, "B", "N", "'ready' "),  # LONG は末尾空白が付く
    ("DEMOS", "UPDATED_AT"): ("TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"),
}
CHECKS_017 = {
    "DEMOS": [
        ('"CONFIG" IS NOT NULL', "ENABLED", "VALIDATED"),
        ("config IS JSON", "ENABLED", "VALIDATED"),
        ("status IN ('provisioning','ready','failed','deleting')", "ENABLED", "VALIDATED"),
    ]
}


def test_ora_code_from_string_error():
    err = Exception("ORA-01430: column being added already exists in table")
    assert migrate._ora_code(err) == 1430
    assert migrate._ora_code(Exception("DPY-4011: connection lost")) is None


def test_ora_code_from_driver_error_object():
    class E:
        code = 955

    assert migrate._ora_code(Exception(E())) == 955


def test_postconditions_match_records_ok():
    cur = FakeDict(columns=COLS_017, checks=CHECKS_017)
    assert migrate._postconditions_met(cur, "017_demos_v2") is True


def test_postconditions_unknown_version_returns_false():
    assert migrate._postconditions_met(FakeDict(), "001_init") is False


def test_postconditions_column_type_mismatch_stops():
    cols = dict(COLS_017)
    cols[("DEMOS", "STATUS")] = ("VARCHAR2", 10, "B", "N", "'ready'")  # 長さ違い = 形違い
    cur = FakeDict(columns=cols, checks=CHECKS_017)
    with pytest.raises(RuntimeError, match="017_demos_v2"):
        migrate._postconditions_met(cur, "017_demos_v2")


def test_postconditions_missing_check_constraint_stops():
    checks = {"DEMOS": [('"CONFIG" IS NOT NULL', "ENABLED", "VALIDATED"),
                        ("config IS JSON", "ENABLED", "VALIDATED"),
                        ("status IN ('ready','failed')", "ENABLED", "VALIDATED")]}  # 値域縮小
    cur = FakeDict(columns=COLS_017, checks=checks)
    with pytest.raises(RuntimeError, match="check"):
        migrate._postconditions_met(cur, "017_demos_v2")


def test_postconditions_disabled_or_novalidate_constraint_stops():
    """同一条件でも DISABLED / NOT VALIDATED は完全一致でない(review-1 M001)。"""
    for status, validated in (("DISABLED", "NOT VALIDATED"), ("ENABLED", "NOT VALIDATED")):
        checks = {"DEMOS": [('"CONFIG" IS NOT NULL', "ENABLED", "VALIDATED"),
                            ("config IS JSON", status, validated),
                            ("status IN ('provisioning','ready','failed','deleting')",
                             "ENABLED", "VALIDATED")]}
        cur = FakeDict(columns=COLS_017, checks=checks)
        with pytest.raises(RuntimeError, match="ENABLED/VALIDATED"):
            migrate._postconditions_met(cur, "017_demos_v2")


def test_postconditions_index_match_and_mismatch():
    ok = FakeDict(indexes={"IDX_DEMOS_OWNER": ("DEMOS", ["OWNER_SUB", "UPDATED_AT"])})
    assert migrate._postconditions_met(ok, "018_demos_idx_owner") is True

    wrong_cols = FakeDict(indexes={"IDX_DEMOS_OWNER": ("DEMOS", ["OWNER_SUB"])})
    with pytest.raises(RuntimeError, match="IDX_DEMOS_OWNER"):
        migrate._postconditions_met(wrong_cols, "018_demos_idx_owner")

    missing = FakeDict(indexes={})
    with pytest.raises(RuntimeError, match="IDX_DEMOS_OWNER"):
        migrate._postconditions_met(missing, "018_demos_idx_owner")


def test_all_new_migrations_have_expected_postconditions():
    """017〜021 の全 migration に期待事後条件が定義されている(spec §1.1 の対象範囲)。"""
    for v in ("017_demos_v2", "018_demos_idx_owner", "019_demos_idx_visibility",
              "020_conversations_demo_id", "021_conversations_idx_demo"):
        assert v in migrate._EXPECTED_POST


# --- 025/026 builder_sessions(specs/19 §2.1 — ランナー事後条件検証の対象) ---

COLS_025 = {
    ("BUILDER_SESSIONS", "ID"): ("VARCHAR2", 36, "B", "N", None),
    ("BUILDER_SESSIONS", "OWNER_SUB"): ("VARCHAR2", 255, "B", "N", None),
    ("BUILDER_SESSIONS", "STATUS"): ("VARCHAR2", 20, "B", "N", "'hearing'"),
    ("BUILDER_SESSIONS", "TRANSCRIPT"): ("CLOB", None, None, "N", "'[]'"),
    ("BUILDER_SESSIONS", "REQUIREMENTS"): ("CLOB", None, None, "Y", None),
    ("BUILDER_SESSIONS", "PLAN"): ("CLOB", None, None, "Y", None),
    ("BUILDER_SESSIONS", "DEMO_ID"): ("VARCHAR2", 36, "B", "Y", None),
    ("BUILDER_SESSIONS", "CREATED_AT"): ("TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"),
    ("BUILDER_SESSIONS", "UPDATED_AT"): ("TIMESTAMP(6)", None, None, "N", "SYSTIMESTAMP"),
}
CHECKS_025 = {
    "BUILDER_SESSIONS": [
        ("status IN ('hearing','designed')", "ENABLED", "VALIDATED"),
        ("transcript IS JSON", "ENABLED", "VALIDATED"),
        ("requirements IS JSON", "ENABLED", "VALIDATED"),
        ("plan IS JSON", "ENABLED", "VALIDATED"),
    ]
}
PK_025 = {"BUILDER_SESSIONS": ("SYS_C001", "ENABLED", "VALIDATED", ["ID"])}


def test_postconditions_025_full_match():
    cur = FakeDict(columns=COLS_025, checks=CHECKS_025, pks=PK_025)
    assert migrate._postconditions_met(cur, "025_builder_sessions") is True
    idx = FakeDict(indexes={"IDX_BS_OWNER": ("BUILDER_SESSIONS", ["OWNER_SUB", "UPDATED_AT"])})
    assert migrate._postconditions_met(idx, "026_builder_sessions_idx") is True


def test_postconditions_027_sufficient_full_match():
    """027 = sufficient 最終判定の永続化列(specs/19 §2.3・§3.1 — SP3-02 review-1 F002)。"""
    cols = {("BUILDER_SESSIONS", "SUFFICIENT"): ("NUMBER", None, None, "N", "0")}
    checks = {"BUILDER_SESSIONS": [("sufficient IN (0,1)", "ENABLED", "VALIDATED")]}
    cur = FakeDict(columns=cols, checks=checks)
    assert migrate._postconditions_met(cur, "027_builder_sessions_sufficient") is True
    wrong_default = FakeDict(
        columns={("BUILDER_SESSIONS", "SUFFICIENT"): ("NUMBER", None, None, "N", "1")},
        checks=checks)
    with pytest.raises(RuntimeError, match="SUFFICIENT"):
        migrate._postconditions_met(wrong_default, "027_builder_sessions_sufficient")


def test_postconditions_025_missing_primary_key_stops():
    """同名テーブルが PK 欠落でも「適用済み」と誤記録しない(review-1 M001)。"""
    cur = FakeDict(columns=COLS_025, checks=CHECKS_025, pks={})
    with pytest.raises(RuntimeError, match="PRIMARY KEY"):
        migrate._postconditions_met(cur, "025_builder_sessions")


def test_postconditions_025_pk_wrong_columns_or_state_stops():
    wrong_cols = FakeDict(columns=COLS_025, checks=CHECKS_025,
                          pks={"BUILDER_SESSIONS": ("SYS_C001", "ENABLED", "VALIDATED",
                                                    ["OWNER_SUB"])})
    with pytest.raises(RuntimeError, match="PRIMARY KEY"):
        migrate._postconditions_met(wrong_cols, "025_builder_sessions")
    disabled = FakeDict(columns=COLS_025, checks=CHECKS_025,
                        pks={"BUILDER_SESSIONS": ("SYS_C001", "DISABLED", "NOT VALIDATED",
                                                  ["ID"])})
    with pytest.raises(RuntimeError, match="ENABLED/VALIDATED"):
        migrate._postconditions_met(disabled, "025_builder_sessions")
