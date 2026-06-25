"""本番アダプタ `OciObjectStore` を OCI セマンティクスのフェイク transport で駆動する統合テスト。

実バケット(apply=人間ゲート)を使わずに、**production の OciObjectStore クラス**を RegistryService の
保存層に差し込み、publish→index→get/download/list と楽観的並行制御(if_none_match='*' での新規 index
作成・if_match での条件付き更新・412→PreconditionFailed)が SDK 呼び出し経由で正しく結線されることを
検証する。OCI SDK の put_object/get_object/head_object/list_objects のヘッダ・例外セマンティクスを
フェイクで忠実に再現する(if-none-match='*'=不在時のみ作成→既存なら 412 等)。
"""

from __future__ import annotations

import pytest
from helpers import PUBLIC_KEY_ID, PUBLISHER, TOKEN, base_manifest, public_key_b64, sign_manifest

from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import OciObjectStore, PreconditionFailed

NS = "ns-fake"
BUCKET = "jetuse-registry"


class FakeServiceError(Exception):
    """oci.exceptions.ServiceError 相当(status 属性)。"""

    def __init__(self, status: int):
        super().__init__(f"service error {status}")
        self.status = status


class _Data:
    def __init__(self, content: bytes):
        self.content = content


class _Resp:
    def __init__(self, data=None, headers=None):
        self.data = data
        self.headers = headers or {}


class _Listing:
    def __init__(self, objects, next_start_with=None):
        self.objects = objects
        self.next_start_with = next_start_with


class _Obj:
    def __init__(self, name):
        self.name = name


class FakeOciTransport:
    """OCI Object Storage の put/get/head/list を条件付きヘッダ込みで再現するフェイク。"""

    def __init__(self):
        self.store: dict[str, tuple[bytes, str]] = {}  # name -> (body, etag)
        self._seq = 0

    def _etag(self) -> str:
        self._seq += 1
        return f'"etag-{self._seq}"'  # OCI の ETag は引用符つきだが本実装は不透明トークン扱い。

    def put_object(self, ns, bucket, name, body, **kwargs):
        cur = self.store.get(name)
        if kwargs.get("if_none_match") == "*" and cur is not None:
            raise FakeServiceError(412)  # 既存 → 作成不可。
        if "if_match" in kwargs:
            if cur is None or cur[1] != kwargs["if_match"]:
                raise FakeServiceError(412)  # etag 不一致。
        self.store[name] = (bytes(body), self._etag())

    def get_object(self, ns, bucket, name):
        cur = self.store.get(name)
        if cur is None:
            raise FakeServiceError(404)
        body, etag = cur
        return _Resp(_Data(body), headers={"etag": etag})

    def head_object(self, ns, bucket, name):
        if name not in self.store:
            raise FakeServiceError(404)
        return _Resp()

    def list_objects(self, ns, bucket, prefix=None, start=None):
        names = sorted(n for n in self.store if n.startswith(prefix or ""))
        return _Resp(_Listing([_Obj(n) for n in names]))


def _service():
    store = OciObjectStore(FakeOciTransport(), NS, BUCKET)
    auth = StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})
    return RegistryService(store, auth), store


def test_oci_adapter_full_publish_flow(private_key):
    svc, store = _service()
    # 新規 index は if_none_match='*' で作成される(初回 register/publish)。
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    entry = svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    assert entry["version"] == "1.0.0"

    # get/download/list が production アダプタ経由で一貫。
    assert svc.get("acme/faq-summarizer")["manifest"]["id"] == "acme/faq-summarizer"
    data, dl = svc.download("acme/faq-summarizer", "1.0.0")
    import hashlib

    assert hashlib.sha256(data).hexdigest() == dl.sha256
    assert any(p["id"] == "acme/faq-summarizer" for p in svc.list_plugins())

    # 2 件目の publish は既存 index を if_match で更新できる。
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.1.0")))
    assert svc.get("acme/faq-summarizer")["entry"]["version"] == "1.1.0"

    # OciObjectStore.list がオブジェクトを返す(成果物 2 + index)。
    names = store.list("")
    assert "index.json" in names
    assert sum(1 for n in names if n.startswith("plugins/")) == 2


def test_oci_adapter_duplicate_version_conflicts(private_key):
    svc, _ = _service()
    svc.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))
    from jetuse_registry.errors import RegistryConflictError

    with pytest.raises(RegistryConflictError):
        svc.publish(TOKEN, sign_manifest(private_key, base_manifest(version="1.0.0")))


def test_oci_adapter_if_none_match_create_then_conflict():
    # production アダプタの if_none_match='*'=「不在時のみ作成」(2 回目は 412→PreconditionFailed)。
    store = OciObjectStore(FakeOciTransport(), NS, BUCKET)
    store.put("index.json", b"{}", if_none_match="*")
    with pytest.raises(PreconditionFailed):
        store.put("index.json", b"{}", if_none_match="*")
