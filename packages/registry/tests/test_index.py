"""RegistryIndex / IndexEntry / PublisherKey の構築・直列化の単体テスト。

populate_by_name により alias(camelCase)・フィールド名(snake_case)のどちらでも構築でき、
`model_dump(by_alias=True)` で配布表現(camelCase)へ戻ることを確認する(取込側 PLG-03 が読む契約)。
"""

from __future__ import annotations

from jetuse_registry.index import IndexEntry, PublisherKey, RegistryIndex


def test_publisher_key_builds_by_alias_and_field_name():
    by_alias = PublisherKey(publicKeyId="k1", publicKey="base64==")
    by_field = PublisherKey(public_key_id="k1", public_key="base64==")
    assert by_alias.public_key_id == by_field.public_key_id == "k1"
    assert by_alias.public_key == by_field.public_key == "base64=="
    assert by_alias.model_dump(by_alias=True) == {"publicKeyId": "k1", "publicKey": "base64=="}


def test_index_entry_builds_by_alias_and_roundtrips():
    e = IndexEntry(
        id="acme/x",
        version="1.0.0",
        kind="usecase",
        name="X",
        publisher="acme",
        objectPath="plugins/acme/x/1.0.0/abc.json",
        sha256="a" * 64,
        publicKeyId="k1",
        publishedAt="2026-01-01T00:00:00+00:00",
    )
    dumped = e.model_dump(by_alias=True)
    assert dumped["objectPath"] == "plugins/acme/x/1.0.0/abc.json"
    assert dumped["publicKeyId"] == "k1"
    assert dumped["publishedAt"] == "2026-01-01T00:00:00+00:00"


def test_registry_index_bytes_roundtrip():
    idx = RegistryIndex.empty()
    idx.register_key("acme", PublisherKey(publicKeyId="k1", publicKey="b64"))
    idx.upsert_entry(
        IndexEntry(
            id="acme/x",
            version="1.0.0",
            kind="agent",
            name="X",
            publisher="acme",
            objectPath="plugins/acme/x/1.0.0/abc.json",
            sha256="a" * 64,
            publicKeyId="k1",
            publishedAt="2026-01-01T00:00:00+00:00",
        )
    )
    restored = RegistryIndex.from_bytes(idx.to_bytes())
    assert restored.get_public_key("acme", "k1").public_key == "b64"
    assert restored.find("acme/x", "1.0.0").object_path == "plugins/acme/x/1.0.0/abc.json"
    # 公開メタデータには発行者鍵を含めない。
    assert "publisherKeys" not in str(restored.public_summary())
