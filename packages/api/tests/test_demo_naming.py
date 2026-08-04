"""demo namespace の完全ハッシュ命名(specs/18 §3.2 手順 2/3d/3e)と
$VECTAB/Sources の新旧規約突合(SP2-00 residual M003)。"""

import hashlib

import pytest

from jetuse_core import datasets, rag_opensearch, rag_select_ai

DEMO_A = "demo_aaaaaaaa-0000-4000-8000-000000000001"
DEMO_B = "demo_bbbbbbbb-0000-4000-8000-000000000002"


def test_datasets_demo_naming_is_full_sha1_user_stays_8hex():
    full = hashlib.sha1(DEMO_A.encode()).hexdigest().upper()
    assert datasets.profile_name(DEMO_A) == f"JETUSE_DS_{full}"
    assert len(full) == 40
    # user は従来 8hex(main 互換 — 既存資産の名前を変えない)
    user8 = hashlib.sha1(b"dev-user").hexdigest()[:8].upper()
    assert datasets.profile_name("dev-user") == f"JETUSE_DS_{user8}"


def test_select_ai_demo_naming_is_full_sha1():
    prof_a, idx_a = rag_select_ai._names(DEMO_A)
    prof_b, idx_b = rag_select_ai._names(DEMO_B)
    assert prof_a != prof_b and idx_a != idx_b
    assert len(prof_a) == len("JETUSE_RAG_") + 40
    prof_u, idx_u = rag_select_ai._names("dev-user")
    assert len(prof_u) == len("JETUSE_RAG_") + 8  # user は 8hex のまま


def test_opensearch_demo_naming_is_full_sha1():
    assert rag_opensearch._index(DEMO_A) == (
        "jetuse-rag-" + hashlib.sha1(DEMO_A.encode()).hexdigest()
    )
    assert rag_opensearch._index("dev-user") == (
        "jetuse-rag-" + hashlib.sha1(b"dev-user").hexdigest()[:16]
    )


def test_forced_8hex_collision_does_not_share_names():
    """tag 強制衝突(8hex 一致)の 2 demo owner でも完全ハッシュ名は分離する。

    (完全 sha1 の衝突は暗号学的に無視可能 — 名前の分離が越境読取・削除波及を構造排除)
    """
    # 8hex を強制一致させるのは sha1 では作為的に困難なため、導出関数が
    # 「先頭 8hex でなく 40hex 全体」を使うことを 2 owner の実名で確認する
    a_forty = hashlib.sha1(DEMO_A.encode()).hexdigest()
    b_forty = hashlib.sha1(DEMO_B.encode()).hexdigest()
    assert datasets._owner_tag(DEMO_A) == a_forty.upper()
    assert datasets._owner_tag(DEMO_B) == b_forty.upper()
    assert rag_select_ai._names(DEMO_A)[1] != rag_select_ai._names(DEMO_B)[1]
    assert rag_opensearch._index(DEMO_A) != rag_opensearch._index(DEMO_B)


def test_vectab_object_name_parser_new_and_old():
    rid = "11111111-2222-4333-8444-555555555555"
    p = rag_select_ai._file_id_from_object_name
    assert p(f"{rid}.pdf") == rid                      # 新: <rid>.<ext>
    assert p(f"{rid}_請求書.pdf") == rid                # 旧: <file_id>_<filename>
    assert p(f"path/{rid}.md") == rid                  # prefix 付き
    assert p("請求書.pdf") is None                      # uuid でない → 対象外
    assert p("") is None and p(None) is None


def test_split_sources_new_naming_resolves_via_file_id():
    rid = "11111111-2222-4333-8444-555555555555"
    body, cites = rag_select_ai.split_sources(
        f"回答です。\n\nSources:\n- {rid}.pdf\n- {rid}_旧形式.pdf\n"
    )
    assert body == "回答です。"
    assert cites[0]["file_id"] == rid   # 新形式 → rid(表示名は DB 解決)
    assert cites[1]["file_id"] == rid   # 旧形式 → uuid 接頭辞
    assert cites[1]["filename"] == "旧形式.pdf"  # 旧形式は prefix を剥がした暫定表示名


def test_reconcile_creating_migrates_state_col_before_select(monkeypatch):
    """B001: 旧登録簿(STATE 列なし)でも _ensure_meta を SELECT state より先に呼ぶ
    (SELECT state の ORA-00904 で dataset/dbchat 経路が恒久停止しない — codex review-10)。"""
    order: list[str] = []
    monkeypatch.setattr(datasets.ddl_verify, "table_exists", lambda cur, t: True)
    monkeypatch.setattr(datasets, "_ensure_meta", lambda cur: order.append("ensure_meta"))

    class _Cur:
        def execute(self, sql, **kw):
            if "WHERE state" in sql:
                order.append("select_state")

        def fetchall(self):
            return []

    assert datasets.reconcile_creating(cur=_Cur()) == 0
    assert order == ["ensure_meta", "select_state"]  # 移行を先に、その後 state 参照


class _FakeCur:
    def __init__(self, raise_on: str):
        self.raise_on = raise_on
        self.calls: list[str] = []

    def execute(self, sql, **kw):
        self.calls.append(sql)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError(
                "ORA-00955: name is already used by an existing object"
                if self.raise_on == "CREATE TABLE" else "boom")

    def executemany(self, sql, seq):
        self.calls.append(sql)

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cur

    def commit(self):
        pass


def _patch_create_dataset(monkeypatch, cur):
    monkeypatch.setattr(datasets, "connect", lambda: _FakeConn(cur))
    monkeypatch.setattr(datasets.vpd, "integrity_gate", lambda: None)
    monkeypatch.setattr(datasets, "owner_key_gate", lambda: None)
    monkeypatch.setattr(datasets, "require_lease_for", lambda owner, lease: None)
    monkeypatch.setattr(datasets, "_ensure_meta", lambda cur: None)
    monkeypatch.setattr(datasets, "reconcile_creating", lambda owner, cur=None: 0)


def test_create_dataset_name_collision_never_drops_existing_table(monkeypatch):
    """review-12 B001: CREATE TABLE が ORA-00955(名前衝突)で失敗しても、他データセットの
    実表を DROP しない(データ損失防止)。新規 'creating' 行だけは削除して収束する。"""
    cur = _FakeCur(raise_on="CREATE TABLE")
    _patch_create_dataset(monkeypatch, cur)
    with pytest.raises(RuntimeError):
        datasets.create_dataset("dev-user", "m", b"a,b\n1,2\n")
    assert not any("DROP TABLE" in s for s in cur.calls)  # 既存表を消さない
    assert any("DELETE FROM JETUSE_DATASETS" in s for s in cur.calls)  # 新規行は掃除


def test_create_dataset_drops_only_own_table_on_later_failure(monkeypatch):
    """CREATE 成功後(GRANT 等)の失敗では自分が作った表を DROP して収束する。"""
    cur = _FakeCur(raise_on="GRANT SELECT")
    _patch_create_dataset(monkeypatch, cur)
    with pytest.raises(RuntimeError):
        datasets.create_dataset("dev-user", "m", b"a,b\n1,2\n")
    assert any("DROP TABLE" in s for s in cur.calls)  # 自作の表は落とす
