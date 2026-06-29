"""SSO ハンドオフコード保管庫（単回使用・短 TTL）の単体テスト（BE06-SSO-002）。"""

from __future__ import annotations

import pytest

from jetuse_core.plugins import sso_handoff_store as store


@pytest.fixture(autouse=True)
def _clear():
    store._clear_for_test()
    yield
    store._clear_for_test()


def test_mint_then_redeem_once_returns_entry():
    code = store.mint(app="denpyon", id_token="ID-TOK", subject="u1",
                      issued_token_type="id_token",
                      mapped_claims={"preferred_username": "u1", "roles": ["sales"]})
    entry = store.redeem(code, app="denpyon")
    assert entry is not None
    assert entry.id_token == "ID-TOK" and entry.subject == "u1"
    # claimMapping 適用済みクレームも code に束ねて渡る（groups→roles 等。BE06-MAJ-003）。
    assert entry.mapped_claims == {"preferred_username": "u1", "roles": ["sales"]}


def test_mint_caps_capacity(monkeypatch):
    """容量上限を超えると最古を追い出す（メモリ枯渇の上限。BE06-MAJ-001）。"""
    monkeypatch.setattr(store, "MAX_ENTRIES", 3)
    codes = [store.mint(app="a", id_token="t", subject="u", issued_token_type="id_token")
             for _ in range(5)]
    # 上限3 → 最古2件は追い出され、最新3件のみ残る。
    assert store.redeem(codes[0], app="a") is None
    assert store.redeem(codes[-1], app="a") is not None


def test_mint_sweeps_expired(monkeypatch):
    """mint 時に期限切れエントリを掃除する（未 redeem の id_token を残さない。BE06-MAJ-001）。"""
    t = {"now": 100.0}
    monkeypatch.setattr(store, "_now", lambda: t["now"])
    old = store.mint(app="a", id_token="t", subject="u", issued_token_type="id_token",
                     ttl_seconds=10)
    t["now"] = 200.0  # 期限切れ
    store.mint(app="a", id_token="t2", subject="u", issued_token_type="id_token")  # ここで掃除
    assert old not in store._DEFAULT_STORE._store  # 期限切れは保持されない


def test_redeem_is_single_use():
    code = store.mint(app="denpyon", id_token="ID-TOK", subject="u1",
                      issued_token_type="id_token")
    assert store.redeem(code, app="denpyon") is not None
    # 2回目は使用済みで None（再使用させない）。
    assert store.redeem(code, app="denpyon") is None


def test_redeem_rejects_wrong_app():
    code = store.mint(app="denpyon", id_token="ID-TOK", subject="u1",
                      issued_token_type="id_token")
    # 別アプリ向けの code を流用できない（流用後はエントリも消える＝単回使用）。
    assert store.redeem(code, app="acme") is None
    assert store.redeem(code, app="denpyon") is None


def test_redeem_rejects_expired(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(store, "_now", lambda: t["now"])
    code = store.mint(app="denpyon", id_token="ID-TOK", subject="u1",
                      issued_token_type="id_token", ttl_seconds=60)
    t["now"] = 1000.0 + 61  # TTL 超過
    assert store.redeem(code, app="denpyon") is None


def test_redeem_unknown_code_is_none():
    assert store.redeem("no-such-code", app="denpyon") is None


def test_mint_requires_app_and_token():
    with pytest.raises(ValueError):
        store.mint(app="", id_token="x", subject="u", issued_token_type="id_token")
    with pytest.raises(ValueError):
        store.mint(app="denpyon", id_token="", subject="u", issued_token_type="id_token")


def test_codes_are_unique_and_opaque():
    a = store.mint(app="denpyon", id_token="t", subject="u", issued_token_type="id_token")
    b = store.mint(app="denpyon", id_token="t", subject="u", issued_token_type="id_token")
    assert a != b and len(a) >= 32


# --- DI 差し替え境界 / 同時 redeem の原子性（BE06-MAJ-002） --------------------


def test_get_store_default_is_in_memory():
    assert isinstance(store.get_store(), store.InMemoryHandoffStore)


def test_set_store_injects_alternate_store():
    """set_store で別ストアを注入でき、モジュール関数がそれへ委譲する（本番＝共有ストア）。"""
    alt = store.InMemoryHandoffStore()
    store.set_store(alt)
    try:
        code = store.mint(app="denpyon", id_token="ID", subject="u1",
                          issued_token_type="id_token")
        # 注入ストアに入り、既定ストアには入らない（分離＝別プロセス相当）。
        assert code in alt._store
        assert code not in store._DEFAULT_STORE._store
        # get_store() 経由でも同じ注入ストアが見える。
        assert store.get_store() is alt
        assert store.get_store().redeem(code, app="denpyon") is not None
    finally:
        store.set_store(None)  # 既定へ戻す
    assert store.get_store() is store._DEFAULT_STORE


def test_separate_stores_do_not_share_codes():
    """別インスタンス（マルチワーカー相当）では一方の code を他方は redeem できない（404 相当）。"""
    s1 = store.InMemoryHandoffStore()
    s2 = store.InMemoryHandoffStore()
    code = s1.mint(app="denpyon", id_token="ID", subject="u1", issued_token_type="id_token")
    # exchange が s1、redeem が s2 に配送されると見つからない（→ 本番は共有ストアが必要）。
    assert s2.redeem(code, app="denpyon") is None
    assert s1.redeem(code, app="denpyon") is not None


def test_concurrent_redeem_only_one_succeeds():
    """同時 redeem でも 1 回だけ成功する（原子的単回交換。BE06-MAJ-002）。"""
    import threading

    s = store.InMemoryHandoffStore()
    code = s.mint(app="denpyon", id_token="ID", subject="u1", issued_token_type="id_token")
    results: list = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # 全スレッドを同時に redeem へ突入させる
        results.append(s.redeem(code, app="denpyon"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    successes = [r for r in results if r is not None]
    assert len(successes) == 1  # 並行でも単回使用が破れない
