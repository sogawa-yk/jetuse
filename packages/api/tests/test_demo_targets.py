"""write-ahead 台帳の locator 正規化(N001)と冪等 upsert(specs/18 §3.2)。"""

import pytest

from jetuse_core import demo_targets


def test_canonical_locator_normalization():
    """N001: キー辞書順・コンパクト区切り・末尾スラッシュ除去・空値除外。"""
    a = {"region": "ap-osaka-1", "endpoint": "https://os.example:9200/",
         "empty": "", "none": None}
    b = {"endpoint": "https://os.example:9200", "region": "ap-osaka-1"}
    assert demo_targets.canonical_locator(a) == demo_targets.canonical_locator(b)
    assert demo_targets.locator_hash(a) == demo_targets.locator_hash(b)
    assert len(demo_targets.locator_hash(a)) == 64  # sha256 hex


def test_canonical_locator_case_sensitive():
    # OCID・bucket 名は大小が意味を持つ — 大文字小文字は変換しない
    assert demo_targets.locator_hash({"bucket": "A"}) != demo_targets.locator_hash(
        {"bucket": "a"}
    )


def test_record_target_upsert_tolerates_unique_violation(monkeypatch):
    """冪等 upsert: ORA-00001 は成功扱い(反復 upload で台帳が伸びない)。"""

    class Cur:
        def execute(self, sql, **binds):
            raise Exception("ORA-00001: unique constraint (UQ_DBT) violated")

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            raise AssertionError("commit should not happen on conflict")

        def rollback(self):
            self.rolled = True

    import contextlib

    conn = Conn()
    monkeypatch.setattr(demo_targets, "connect",
                        lambda: contextlib.nullcontext(conn))
    demo_targets.record_target("demo_d1", "files", {"region": "r"})  # 例外にならない
    assert conn.rolled


def test_record_target_rejects_unknown_kind():
    with pytest.raises(ValueError):
        demo_targets.record_target("demo_d1", "sqs", {})
