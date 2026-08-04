"""owner キー導出の単射エスケープと file_key 導出(specs/18 §3.1・§3.2.1)。"""

import hashlib

from jetuse_core.owner_keys import (
    file_key,
    is_demo_namespace,
    normalize_ext,
    original_object_name,
    original_prefix,
    owner_hash,
    user_owner_key,
)


def test_normal_subs_are_noop():
    for sub in ("dev-user", "sp2-user-a", "ocid1.user.oc1..abc", "予約でない"):
        assert user_owner_key(sub) == sub  # 実在の sub には no-op = 既存データと互換


def test_reserved_prefixes_are_escaped_injectively():
    # demo_ / sub_ で始まる sub のみ sub_ を前置(決定的・単射)
    assert user_owner_key("demo_123") == "sub_demo_123"
    assert user_owner_key("sub_x") == "sub_sub_x"
    # 単射: エスケープ後の衝突がない(demo_123 の user と sub_demo_123 の user)
    assert user_owner_key("demo_123") != user_owner_key("sub_demo_123")
    assert user_owner_key("sub_demo_123") == "sub_sub_demo_123"


def test_demo_namespace_never_collides_with_user_keys():
    # user がどんな sub を名乗っても demo_<uuid> キー空間には入れない
    demo_key = "demo_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert user_owner_key(demo_key).startswith("sub_")
    assert is_demo_namespace(demo_key)
    assert not is_demo_namespace(user_owner_key(demo_key))


def test_agent_rag_owner_key_no_demo_collision():
    """Select AI Agent の RAG tool 経路(B001): 予約接頭辞ユーザーの owner は escaped され、
    実 demo の RAG profile/index 名と衝突しない(= demo 文書を越境参照できない)。"""
    from jetuse_core import rag_select_ai

    demo_ns = "demo_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    user_owner = user_owner_key(demo_ns)  # sub が demo_<id> を騙るユーザー → escaped
    assert user_owner != demo_ns
    assert rag_select_ai._names(user_owner) != rag_select_ai._names(demo_ns)


def test_length_boundary_251_to_255():
    """251〜255 文字の予約接頭辞 sub の境界(specs/18 §3.2.1 — 前置で 255 バイト超過)。"""
    for n in range(251, 256):
        sub = "demo_" + "x" * (n - 5)
        assert len(sub) == n
        key = user_owner_key(sub)
        assert len(key.encode()) <= 255
        if n <= 251:  # +4 バイトで 255 以内 → 通常エスケープ
            assert key == f"sub_{sub}"
        else:  # 溢れる → ハッシュ形式(決定的・長さ有界)
            assert key == f"sub_h_{hashlib.sha1(sub.encode()).hexdigest()}"
        assert user_owner_key(sub) == key  # 決定的


def test_file_key_derivation_is_single_source():
    rid = "11111111-2222-4333-8444-555555555555"
    tag = hashlib.sha1(b"demo_d1").hexdigest()
    assert owner_hash("demo_d1") == tag
    assert len(tag) == 40  # 固定長完全ハッシュ(64 文字 metadata 枠に入る)
    assert file_key("demo_d1", rid, "pdf") == f"{tag}/{rid}.pdf"
    assert file_key("demo_d1", rid, ".PDF") == f"{tag}/{rid}.pdf"  # 正規化
    assert original_object_name("demo_d1", rid, "md") == f"rag/{tag}/{rid}.md"
    assert original_prefix("demo_d1") == f"rag/{tag}/"


def test_original_storage_seg_user_main_compat_demo_hashed():
    """review-12 B002: 原本 prefix / Select AI 索引 location の owner セグメントは
    demo=完全 sha1、user=main 互換の raw owner。user だけ hash 化すると既存索引
    (location=rag/<owner>)が更新後の新規アップロード(原本)を取り込めなくなる。"""
    from jetuse_core import rag_select_ai

    rid = "11111111-2222-4333-8444-555555555555"
    user = "dev-user"  # 予約接頭辞でない実 sub
    # user 原本は main 互換の raw owner prefix(rag/<owner>/…)
    assert original_object_name(user, rid, "md") == f"rag/{user}/{rid}.md"
    assert original_prefix(user) == f"rag/{user}/"
    # demo 原本は完全 sha1(箱の越境防止)
    dtag = owner_hash("demo_d1")
    assert original_object_name("demo_d1", rid, "md") == f"rag/{dtag}/{rid}.md"
    assert original_prefix("demo_d1") == f"rag/{dtag}/"
    # Select AI 索引 location が原本 prefix と一致する(食い違うと取り込み漏れ)
    assert rag_select_ai._location(user).endswith(f"/o/rag/{user}")
    assert rag_select_ai._location("demo_d1").endswith(f"/o/rag/{dtag}")
    # OCI Files filename(file_id 参照)は user でも完全 sha1 のままでよい(一意性のみ要件)
    assert file_key(user, rid, "md") == f"{owner_hash(user)}/{rid}.md"


def test_normalize_ext():
    assert normalize_ext("資料.PDF") == "pdf"
    assert normalize_ext("a.b.md") == "md"
    assert normalize_ext("noext") == "bin"
