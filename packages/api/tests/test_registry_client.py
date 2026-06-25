"""RegistryClient(PLG-03)の単体テスト。

実レジストリ(HTTP/Object Storage)へは接続せず、path -> bytes の fake transport を注入して
list / get / download / public_key と各エラー経路を検証する。
"""

import base64
import json

import pytest

from jetuse_core.plugins import registry_client as rc
from jetuse_core.plugins.manifest import (
    SCHEMA_VERSION,
    ManifestError,
    validate_manifest,
)
from jetuse_core.plugins.registry_client import (
    INDEX_PATH,
    RegistryClient,
    RegistryError,
)


def _manifest_dict(plugin_id="acme/faq", version="1.2.0"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": plugin_id,
        "version": version,
        "kind": "usecase",
        "name": "FAQ要約",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:rag.search"],
        "contributes": {
            "usecase": {
                "fields": [{"name": "text", "type": "textarea"}],
                "template": "要約して: {{text}}",
            }
        },
    }


def _manifest_path(plugin_id, version):
    return f"plugins/{plugin_id}/{version}/manifest.json"


def make_registry(manifests, *, publisher_keys=None, index_override=None):
    """manifests(dict のリスト)を配るインメモリ transport と RegistryClient を返す。"""
    files: dict[str, bytes] = {}
    plugins = []
    for m in manifests:
        path = _manifest_path(m["id"], m["version"])
        files[path] = json.dumps(m).encode("utf-8")
        plugins.append(
            {
                "id": m["id"],
                "version": m["version"],
                "kind": m["kind"],
                "name": m["name"],
                "publisher": m["publisher"],
                "manifest": path,
            }
        )
    index = index_override or {
        "schemaVersion": "1",
        "plugins": plugins,
        "publisherKeys": publisher_keys or {},
    }
    files[INDEX_PATH] = json.dumps(index).encode("utf-8")

    def transport(path: str) -> bytes:
        if path not in files:
            raise RegistryError(f"404: {path}")
        return files[path]

    return RegistryClient(base_url="https://reg.example/jetuse/", transport=transport)


def test_list_returns_entries():
    client = make_registry([_manifest_dict()])
    entries = client.list()
    assert len(entries) == 1
    assert entries[0]["id"] == "acme/faq"
    assert entries[0]["version"] == "1.2.0"
    # 返り値はコピー(内部 index を破壊しない)。
    entries[0]["id"] = "mutated"
    assert client.list()[0]["id"] == "acme/faq"


def test_get_resolves_latest_version_by_semver():
    client = make_registry(
        [
            _manifest_dict(version="1.0.0"),
            _manifest_dict(version="2.1.0"),
            _manifest_dict(version="2.0.5"),
        ]
    )
    assert client.get("acme/faq")["version"] == "2.1.0"
    assert client.get("acme/faq", "1.0.0")["version"] == "1.0.0"


def test_get_unknown_plugin_and_version_raise():
    client = make_registry([_manifest_dict(version="1.0.0")])
    with pytest.raises(RegistryError):
        client.get("acme/missing")
    with pytest.raises(RegistryError):
        client.get("acme/faq", "9.9.9")


def test_download_returns_validated_manifest():
    client = make_registry([_manifest_dict()])
    manifest = client.download("acme/faq", "1.2.0")
    assert manifest.id == "acme/faq"
    assert manifest.version == "1.2.0"
    assert manifest.kind == "usecase"


def test_download_rejects_invalid_manifest():
    bad = _manifest_dict()
    bad["kind"] = "not-a-kind"  # manifest 検証で弾かれる
    client = make_registry([_manifest_dict()])
    # index は valid な kind を載せるが、manifest 本体を不正にして配る。
    path = _manifest_path("acme/faq", "1.2.0")
    files = {path: json.dumps(bad).encode("utf-8")}
    index = {
        "schemaVersion": "1",
        "plugins": [
            {"id": "acme/faq", "version": "1.2.0", "kind": "usecase",
             "name": "x", "publisher": "acme-corp", "manifest": path}
        ],
        "publisherKeys": {},
    }
    files[INDEX_PATH] = json.dumps(index).encode("utf-8")
    client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])
    with pytest.raises(ManifestError):
        client.download("acme/faq", "1.2.0")


def test_download_detects_id_version_mismatch():
    # index は 1.2.0 と主張するが manifest 本体は 9.9.9 → 取り違えとして拒否。
    m = _manifest_dict(version="9.9.9")
    path = _manifest_path("acme/faq", "1.2.0")
    files = {path: json.dumps(m).encode("utf-8")}
    index = {
        "schemaVersion": "1",
        "plugins": [
            {"id": "acme/faq", "version": "1.2.0", "kind": "usecase",
             "name": "x", "publisher": "acme-corp", "manifest": path}
        ],
        "publisherKeys": {},
    }
    files[INDEX_PATH] = json.dumps(index).encode("utf-8")
    client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])
    with pytest.raises(RegistryError):
        client.download("acme/faq", "1.2.0")


def test_public_key_roundtrip_and_errors():
    raw = bytes(range(32))
    client = make_registry(
        [_manifest_dict()],
        publisher_keys={"acme-key-1": base64.b64encode(raw).decode()},
    )
    assert client.public_key("acme-key-1") == raw
    with pytest.raises(RegistryError):
        client.public_key("unknown-key")


def test_public_key_rejects_wrong_length():
    short = base64.b64encode(b"too-short").decode()
    client = make_registry([_manifest_dict()], publisher_keys={"k": short})
    with pytest.raises(RegistryError):
        client.public_key("k")


def test_malformed_index_raises():
    client = RegistryClient(base_url="https://reg/", transport=lambda p: b"{not json")
    with pytest.raises(RegistryError):
        client.list()
    client2 = RegistryClient(
        base_url="https://reg/", transport=lambda p: json.dumps({"x": 1}).encode()
    )
    with pytest.raises(RegistryError):
        client2.list()


def test_requires_base_url_or_transport():
    with pytest.raises(RegistryError):
        RegistryClient()  # どちらも無い
    # base_url だけでも構築できる(transport は遅延生成)。
    assert RegistryClient(base_url="https://reg/").base_url == "https://reg/"


def test_index_is_cached_until_refresh():
    calls = {"n": 0}
    m = _manifest_dict()
    index = {
        "schemaVersion": "1",
        "plugins": [
            {"id": m["id"], "version": m["version"], "kind": m["kind"],
             "name": m["name"], "publisher": m["publisher"],
             "manifest": _manifest_path(m["id"], m["version"])}
        ],
        "publisherKeys": {},
    }

    def transport(path):
        if path == INDEX_PATH:
            calls["n"] += 1
            return json.dumps(index).encode()
        return json.dumps(m).encode()

    client = RegistryClient(base_url="https://reg/", transport=transport)
    client.list()
    client.list()
    assert calls["n"] == 1  # キャッシュ済み
    client.refresh()
    client.list()
    assert calls["n"] == 2


def test_served_manifest_validates_against_spec():
    # make_registry が配る manifest が実バリデータを通ること(テスト素材の健全性)。
    assert validate_manifest(_manifest_dict()).id == "acme/faq"


@pytest.mark.parametrize(
    "bad_path",
    [
        "https://evil.example/manifest.json",  # 絶対 URL
        "http://evil.example/m.json",
        "//evil.example/m.json",               # スキーム相対
        "/abs/path/m.json",                    # ホスト絶対パス
        "plugins/../../etc/passwd",            # 親ディレクトリ遡行
        "..\\..\\m.json",                      # backslash 遡行
    ],
)
def test_download_rejects_non_relative_manifest_path(bad_path):
    """index の manifest パスが base URL 配下の相対でなければ取得を拒否する(取得先差替防止)。"""
    fetched = {"path": None}

    def transport(path):
        if path == INDEX_PATH:
            index = {
                "schemaVersion": "1",
                "plugins": [{"id": "acme/faq", "version": "1.2.0", "kind": "usecase",
                             "name": "x", "publisher": "acme-corp", "manifest": bad_path}],
                "publisherKeys": {},
            }
            return json.dumps(index).encode()
        fetched["path"] = path  # ここに到達してはならない(検証で先に弾く)。
        return json.dumps(_manifest_dict()).encode()

    client = RegistryClient(base_url="https://reg/", transport=transport)
    with pytest.raises(RegistryError):
        client.download("acme/faq", "1.2.0")
    assert fetched["path"] is None  # 不正パスは fetch されない


def test_http_transport_does_not_follow_redirects(monkeypatch):
    """既定 HTTP トランスポートは 3xx を追従せず RegistryError にする(base URL 外転送防止)。"""

    class _Resp:
        is_redirect = True
        status_code = 302

        def raise_for_status(self):  # 3xx では呼ばれない想定だが安全側に no-op。
            return None

    captured = {}

    def fake_get(url, timeout=None, follow_redirects=None):
        captured["follow_redirects"] = follow_redirects
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    transport = rc._http_transport("https://reg.example/jetuse/", 5.0)
    with pytest.raises(RegistryError):
        transport("index.json")
    assert captured["follow_redirects"] is False  # 追従しない設定で呼ばれる
