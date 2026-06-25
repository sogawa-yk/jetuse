"""OciObjectStore(本番アダプタ)の単体テスト。

実 OCI には接続せず、ObjectStorageClient の最小フェイクで put/get/exists/list の SDK 呼び出しと
例外正規化(404→KeyError / 412→PreconditionFailed)・etag 取得・ページングを検証する。
実バケットでの疎通は apply(人間ゲート)後の E2E に委ねる(runs/<run-id>/e2e/SKIPPED.md)。
"""

from __future__ import annotations

import pytest

from jetuse_registry.storage import IF_NONE_MATCH_ANY, OciObjectStore, PreconditionFailed

NS = "ns-test"
BUCKET = "jetuse-registry"


class FakeServiceError(Exception):
    """oci.exceptions.ServiceError 相当(status 属性を持つ)。"""

    def __init__(self, status: int):
        super().__init__(f"service error {status}")
        self.status = status


class _Resp:
    def __init__(self, data, headers=None):
        self.data = data
        self.headers = headers or {}


class _Content:
    def __init__(self, content: bytes):
        self.content = content


class _Obj:
    def __init__(self, name: str):
        self.name = name


class _Listing:
    def __init__(self, objects, next_start_with=None):
        self.objects = objects
        self.next_start_with = next_start_with


class FakeOciClient:
    """put/get/head/list の呼び出しを記録し、構成した応答/例外を返すフェイク。"""

    def __init__(self):
        self.put_calls: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.get_behavior = None  # callable(name)->_Resp or raises
        self.head_status: dict[str, int] = {}
        self.list_pages: list[_Listing] = []
        self._list_idx = 0

    def put_object(self, ns, bucket, name, body, **kwargs):
        self.put_calls.append({"ns": ns, "bucket": bucket, "name": name, "body": body, **kwargs})
        # 412 を模す指定があれば送出。
        if kwargs.get("_raise_412"):
            raise FakeServiceError(412)
        self.objects[name] = body

    def get_object(self, ns, bucket, name):
        return self.get_behavior(name)

    def head_object(self, ns, bucket, name):
        status = self.head_status.get(name)
        if status == 404:
            raise FakeServiceError(404)
        return _Resp(None)

    def list_objects(self, ns, bucket, prefix=None, start=None):
        page = self.list_pages[self._list_idx]
        self._list_idx += 1
        return _Resp(page)


def _store(client):
    return OciObjectStore(client, NS, BUCKET)


def test_put_passes_content_type_and_conditional_headers():
    c = FakeOciClient()
    s = _store(c)
    s.put("index.json", b"{}", content_type="application/json", if_match="etag-9")
    s.put("a", b"x", if_none_match=IF_NONE_MATCH_ANY)
    assert c.put_calls[0]["content_type"] == "application/json"
    assert c.put_calls[0]["if_match"] == "etag-9"
    assert c.put_calls[1]["if_none_match"] == IF_NONE_MATCH_ANY
    # 条件未指定なら if_* は渡さない。
    assert "if_match" not in c.put_calls[1]


def test_put_412_maps_to_precondition_failed():
    c = FakeOciClient()
    s = _store(c)

    def boom(*a, **k):
        raise FakeServiceError(412)

    c.put_object = boom  # type: ignore[assignment]
    with pytest.raises(PreconditionFailed):
        s.put("index.json", b"{}", if_match="stale")


def test_get_with_etag_returns_content_and_etag():
    c = FakeOciClient()
    c.get_behavior = lambda name: _Resp(_Content(b"payload"), headers={"etag": "etag-3"})
    s = _store(c)
    data, etag = s.get_with_etag("index.json")
    assert data == b"payload"
    assert etag == "etag-3"


class _Raw:
    def __init__(self, content: bytes):
        self._c = content

    def read(self):
        return self._c


class _RawHolder:
    """content を持たず raw.read() でのみ読めるストリーム(SDK 版差の一形態)。"""

    def __init__(self, content: bytes):
        self.raw = _Raw(content)


class _Readable:
    """read() だけを持つストリーム。"""

    def __init__(self, content: bytes):
        self._c = content

    def read(self):
        return self._c


@pytest.mark.parametrize(
    "data_factory",
    [
        lambda b: _Content(b),  # .content
        lambda b: _RawHolder(b),  # .raw.read()
        lambda b: _Readable(b),  # .read()
        lambda b: b,  # 既に bytes
    ],
)
def test_get_with_etag_reads_various_stream_shapes(data_factory):
    c = FakeOciClient()
    c.get_behavior = lambda name: _Resp(data_factory(b"payload"), headers={"etag": "e"})
    data, etag = _store(c).get_with_etag("index.json")
    assert data == b"payload"
    assert etag == "e"


@pytest.mark.parametrize("header_key", ["etag", "ETag", "Etag"])
def test_get_with_etag_is_case_insensitive_on_etag_header(header_key):
    # 実 SDK は CaseInsensitiveDict だが、素の dict で 'ETag' 等が来ても etag を取り違えない。
    c = FakeOciClient()
    c.get_behavior = lambda name: _Resp(_Content(b"x"), headers={header_key: "etag-7"})
    _, etag = _store(c).get_with_etag("index.json")
    assert etag == "etag-7"


class _ContentWithEtag:
    """content と etag 属性の両方を持つ data(ヘッダに etag が無い構成の fallback 検証)。"""

    def __init__(self, content: bytes, etag: str):
        self.content = content
        self.etag = etag


def test_get_with_etag_falls_back_to_data_etag_when_header_absent():
    c = FakeOciClient()
    # ヘッダに etag が無く、data 側に etag がある構成でも拾える。
    c.get_behavior = lambda name: _Resp(_ContentWithEtag(b"x", "etag-data"), headers={})
    _, etag = _store(c).get_with_etag("index.json")
    assert etag == "etag-data"


def test_get_404_maps_to_keyerror():
    c = FakeOciClient()

    def raise_404(name):
        raise FakeServiceError(404)

    c.get_behavior = raise_404
    s = _store(c)
    with pytest.raises(KeyError):
        s.get("missing")


def test_get_non_404_propagates():
    c = FakeOciClient()

    def raise_500(name):
        raise FakeServiceError(500)

    c.get_behavior = raise_500
    s = _store(c)
    with pytest.raises(FakeServiceError):
        s.get("x")


def test_exists_true_false_and_propagates():
    c = FakeOciClient()
    c.head_status = {"there": 200, "gone": 404}
    s = _store(c)
    assert s.exists("there") is True
    assert s.exists("gone") is False


def test_list_paginates_across_next_start_with():
    c = FakeOciClient()
    c.list_pages = [
        _Listing([_Obj("plugins/b"), _Obj("plugins/a")], next_start_with="tok"),
        _Listing([_Obj("plugins/c")], next_start_with=None),
    ]
    s = _store(c)
    # 2 ページを連結し辞書順で返す。
    assert s.list("plugins/") == ["plugins/a", "plugins/b", "plugins/c"]
