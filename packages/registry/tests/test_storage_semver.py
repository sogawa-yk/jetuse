"""保存層(InMemoryObjectStore)と semver precedence の単体テスト。"""

from __future__ import annotations

import pytest

from jetuse_registry import semver
from jetuse_registry.storage import IF_NONE_MATCH_ANY, InMemoryObjectStore, PreconditionFailed


def test_inmemory_put_get_exists_list():
    s = InMemoryObjectStore()
    assert s.exists("a") is False
    with pytest.raises(KeyError):
        s.get("a")
    s.put("plugins/a", b"x", content_type="application/json")
    s.put("plugins/b", b"y")
    s.put("index.json", b"{}")
    assert s.exists("plugins/a") is True
    assert s.get("plugins/a") == b"x"
    assert s.list("plugins/") == ["plugins/a", "plugins/b"]
    assert s.list() == ["index.json", "plugins/a", "plugins/b"]


def test_inmemory_put_rejects_non_bytes():
    s = InMemoryObjectStore()
    with pytest.raises(TypeError):
        s.put("a", "string-not-bytes")  # type: ignore[arg-type]


def test_inmemory_etag_changes_on_put_and_if_match():
    s = InMemoryObjectStore()
    s.put("k", b"v1")
    _, etag1 = s.get_with_etag("k")
    # 正しい etag での条件付き put は通り、etag が進む。
    s.put("k", b"v2", if_match=etag1)
    data, etag2 = s.get_with_etag("k")
    assert data == b"v2"
    assert etag2 != etag1
    # 古い etag での put は PreconditionFailed。
    with pytest.raises(PreconditionFailed):
        s.put("k", b"v3", if_match=etag1)


def test_inmemory_if_none_match_creates_only_when_absent():
    s = InMemoryObjectStore()
    s.put("idx", b"{}", if_none_match=IF_NONE_MATCH_ANY)  # 不在 → 作成成功
    with pytest.raises(PreconditionFailed):
        s.put("idx", b"{}", if_none_match=IF_NONE_MATCH_ANY)  # 既存 → 失敗
    # if_match で存在しないキーを更新しようとしても失敗。
    with pytest.raises(PreconditionFailed):
        s.put("missing", b"x", if_match="etag-1")


def test_inmemory_get_returns_copy_semantics():
    s = InMemoryObjectStore()
    payload = bytearray(b"abc")
    s.put("k", payload)
    payload[0] = ord("z")  # 外部変更が保存値に波及しない(bytes 化して保持)。
    assert s.get("k") == b"abc"


@pytest.mark.parametrize(
    "versions,expected",
    [
        (["1.0.0", "1.2.0", "1.10.0"], "1.10.0"),
        (["1.0.0", "2.0.0-rc.1"], "2.0.0-rc.1"),  # rc は同じ 1.0.0 より上だが 2.0.0 正式版未満
        (["2.0.0-rc.1", "2.0.0"], "2.0.0"),  # 正式版 > prerelease
        (["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta"], "1.0.0-beta"),
        # build メタデータは precedence 不問。同順位で max は先頭を返す。
        (["1.0.0+build.1", "1.0.0+build.2"], "1.0.0+build.1"),
    ],
)
def test_semver_latest(versions, expected):
    # build メタデータは precedence に影響しないため両者は同順位。max は安定で先頭を返す。
    assert semver.latest(versions) == expected


def test_semver_latest_empty_raises():
    with pytest.raises(ValueError):
        semver.latest([])


def test_semver_prerelease_lower_than_release():
    assert semver.precedence_key("1.0.0-rc.1") < semver.precedence_key("1.0.0")
    assert semver.precedence_key("1.0.0") < semver.precedence_key("1.0.1")


def test_semver_canonical_precedence_chain():
    # semver.org §11 の正準例。厳密に増加し、最大は正式版 1.0.0 であること。
    chain = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    keys = [semver.precedence_key(v) for v in chain]
    assert all(keys[i] < keys[i + 1] for i in range(len(keys) - 1))
    assert semver.latest(chain) == "1.0.0"
    # 数値識別子は非数値より低い(alpha.1 < alpha.beta)。
    assert semver.precedence_key("1.0.0-alpha.1") < semver.precedence_key("1.0.0-alpha.beta")
    # prefix 一致なら識別子が多い方が高い(alpha < alpha.1)。
    assert semver.precedence_key("1.0.0-alpha") < semver.precedence_key("1.0.0-alpha.1")
