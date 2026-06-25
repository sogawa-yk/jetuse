"""レジストリ保存層の抽象(Object Storage)。

サービス層は `ObjectStore` プロトコルにのみ依存する。これにより:
  - 本番: OCI Object Storage バケット(`OciObjectStore`)
  - テスト/エミュレート: インメモリ(`InMemoryObjectStore`)
を差し替え可能にする。実 Object Storage バケットの作成は Terraform apply(課金・人間ゲート)が
必要なため、統合テストは `InMemoryObjectStore` で検証する(tasks/PLG-04 受け入れ条件)。

オブジェクトはバイト列で出し入れする。キーは Object Storage のオブジェクト名(例
`plugins/acme/faq/1.0.0/manifest.json`、`index.json`)。`index.json` の read-modify-write 競合
(同時 publish での更新消失・不変性破り)を避けるため、楽観的並行制御(etag / if-match /
if-none-match)を提供する。レジストリのドメイン知識(index 構造・署名検証)は service.py に閉じ込める。
"""

from __future__ import annotations

import itertools
import threading
from typing import Protocol, runtime_checkable

#: 「オブジェクトが存在しないこと」を要求する条件付き put の if_none_match 値(HTTP 由来)。
IF_NONE_MATCH_ANY = "*"


def _header_get(headers, name: str) -> str:
    """HTTP ヘッダを大小無視で引く。

    OCI SDK の Response.headers は `requests` の CaseInsensitiveDict なので `.get` で足りるが、
    fake/別実装で素の dict が来ても 'ETag'/'Etag'/'etag' のいずれでも取れるようにする
    (etag を取り違えると `if_match` が壊れ、楽観ロックが機能しない)。
    """
    if not headers:
        return ""
    val = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        val = getter(name)
    if val is None and hasattr(headers, "items"):
        lower = name.lower()
        for k, v in headers.items():
            if str(k).lower() == lower:
                val = v
                break
    return val or ""


def _read_stream(data) -> bytes:
    """OCI get_object の Response.data(stream-like)から生バイト列を取り出す。

    SDK 版差を吸収する: `.content`(まとめ読み)→ `.raw.read()`(urllib3 raw)→ `.read()` の順で試す。
    既にバイト列ならそのまま返す。stream への素朴な bytes() 変換は TypeError になるため避ける。
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    content = getattr(data, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    raw = getattr(data, "raw", None)
    if raw is not None and hasattr(raw, "read"):
        return raw.read()
    if hasattr(data, "read"):
        return data.read()
    raise TypeError(f"OCI get_object のレスポンスからバイト列を取り出せない: {type(data).__name__}")


class PreconditionFailed(Exception):
    """条件付き put(if_match / if_none_match)の前提が崩れた(他者が先に更新した)。

    楽観的並行制御の衝突を表す。OCI Object Storage の HTTP 412(If-Match/If-None-Match 失敗)に対応。
    サービス層はこれを捕捉して index を読み直し、publish 検証ごとリトライする。
    """


@runtime_checkable
class ObjectStore(Protocol):
    """バイト列オブジェクトの保存層。実装は OCI Object Storage / インメモリ。

    楽観的並行制御: `get_with_etag` で読んだ etag を `put(..., if_match=etag)` に渡すと、その間に
    他者が更新していれば `PreconditionFailed` を送出する。新規作成は
    `put(..., if_none_match=IF_NONE_MATCH_ANY)` で「存在しないこと」を条件にする。
    """

    def put(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        """オブジェクトを保存する。

        if_match: 指定 etag と一致するときだけ上書き(不一致は PreconditionFailed)。
        if_none_match='*': オブジェクトが存在しないときだけ作成(存在すれば PreconditionFailed)。
        """
        ...

    def get(self, name: str) -> bytes:
        """オブジェクトを取得する。存在しなければ KeyError を送出する。"""
        ...

    def get_with_etag(self, name: str) -> tuple[bytes, str]:
        """オブジェクトのバイト列と etag を返す。存在しなければ KeyError。"""
        ...

    def exists(self, name: str) -> bool:
        """オブジェクトの存在を返す。"""
        ...

    def list(self, prefix: str = "") -> list[str]:
        """prefix に前方一致するオブジェクト名を辞書順で返す。"""
        ...


class InMemoryObjectStore:
    """`ObjectStore` のインメモリ実装(テスト/エミュレート用)。

    実バケットを作らずに publish→index→list/get/download の往復を検証するために使う。
    格納は name→(bytes, etag)。etag は put のたびに単調増加させ、楽観的並行制御を模す。
    複数スレッドからの read-modify-write 競合をテストできるよう put は lock で原子的に判定する。
    """

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()
        self._etag_seq = itertools.count(1)

    def _next_etag(self) -> str:
        return f"etag-{next(self._etag_seq)}"

    def put(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("ObjectStore.put にはバイト列を渡すこと")
        # content_type は実 SDK と引数互換にするため受けるが、インメモリでは保持不要。
        with self._lock:
            current = self._objects.get(name)
            if if_none_match == IF_NONE_MATCH_ANY and current is not None:
                raise PreconditionFailed(f"{name} は既に存在する(if_none_match)")
            if if_match is not None:
                if current is None or current[1] != if_match:
                    raise PreconditionFailed(f"{name} の etag が一致しない(if_match)")
            self._objects[name] = (bytes(data), self._next_etag())

    def get(self, name: str) -> bytes:
        return self.get_with_etag(name)[0]

    def get_with_etag(self, name: str) -> tuple[bytes, str]:
        try:
            return self._objects[name]
        except KeyError as e:
            raise KeyError(name) from e

    def exists(self, name: str) -> bool:
        return name in self._objects

    def list(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self._objects if n.startswith(prefix))


class OciObjectStore:
    """OCI Object Storage バケットを保存層にする `ObjectStore` 実装。

    実バケット(`bucket`)・namespace(`namespace`)・`ObjectStorageClient` を受け取る。クライアントの
    構築(認証・リージョン)は呼び出し側の責務(`build_from_env`)。本クラスは name↔オブジェクトの
    薄い写像のみを担い、レジストリのドメイン知識は持たない。

    実バケットの作成は Terraform apply(課金・人間ゲート)。本クラスは作成済みバケット前提で動く。
    """

    def __init__(self, client, namespace: str, bucket: str) -> None:
        self._client = client
        self._namespace = namespace
        self._bucket = bucket

    def put(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        # OCI Python SDK(>=2.150)の put_object は kwargs `if_match`/`if_none_match`/`content_type`
        # を受け、HTTP ヘッダ `if-match`/`if-none-match`/`Content-Type` に写像する
        # (SDK の expected_kwargs/header_params で確認済み)。if_none_match='*' = 不在時のみ作成。
        kwargs = {"content_type": content_type}
        if if_match is not None:
            kwargs["if_match"] = if_match
        if if_none_match is not None:
            kwargs["if_none_match"] = if_none_match
        try:
            self._client.put_object(
                self._namespace, self._bucket, name, bytes(data), **kwargs
            )
        except Exception as e:  # 条件付き put の失敗(HTTP 412)を PreconditionFailed へ正規化。
            if getattr(e, "status", None) == 412:
                raise PreconditionFailed(name) from e
            raise

    def get(self, name: str) -> bytes:
        return self.get_with_etag(name)[0]

    def get_with_etag(self, name: str) -> tuple[bytes, str]:
        try:
            resp = self._client.get_object(self._namespace, self._bucket, name)
        except Exception as e:  # oci.exceptions.ServiceError(status=404) 等を KeyError へ正規化
            if getattr(e, "status", None) == 404:
                raise KeyError(name) from e
            raise
        # OCI get_object の Response.data は stream-like(requests/urllib3 系)。SDK 版差に備えて
        # content → raw.read() → read() の順で生バイト列を取り出す(_read_stream が版差を吸収)。
        raw = _read_stream(resp.data)
        # ETag は OCI get_object の HTTP ヘッダが正(CaseInsensitiveDict)。版差に備えて
        # ヘッダ('ETag'/'etag')→ resp.data.etag → resp.data.headers の順で拾い、空のままにしない。
        etag = _header_get(resp.headers, "etag")
        if not etag:
            etag = (getattr(resp.data, "etag", None) or "") if resp.data is not None else ""
        if not etag:
            etag = _header_get(getattr(resp.data, "headers", None), "etag")
        return raw, etag

    def exists(self, name: str) -> bool:
        try:
            self._client.head_object(self._namespace, self._bucket, name)
            return True
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return False
            raise

    def list(self, prefix: str = "") -> list[str]:
        # OCI ListObjects のページング(oci>=2.150 で検証): リクエストの `start` に前ページ応答の
        # `ListObjects.next_start_with` を渡して次ページを得る。next_start_with が空になれば終端。
        # (本 MVP の read 系は index.json を正本にするため list は保守用。critical path ではない。)
        names: list[str] = []
        start = None
        while True:
            resp = self._client.list_objects(
                self._namespace, self._bucket, prefix=prefix, start=start
            )
            listing = resp.data
            names.extend(o.name for o in listing.objects)
            start = getattr(listing, "next_start_with", None)
            if not start:
                break
        return sorted(names)


def build_from_env() -> OciObjectStore:
    """環境変数から実 Object Storage バックエンドを構築する(本番/手動 E2E 用)。

    - REGISTRY_BUCKET: バケット名(必須)
    - REGISTRY_NAMESPACE: Object Storage namespace(未指定なら API から解決)
    - AUTH_MODE=resource_principal: Container Instance/Functions 上では RP 署名を使う
      (jetuse_core.db と同方針)。OCI_REGION を併用。未指定なら `~/.oci/config` の API キー。

    実バケットは Terraform(infra/terraform/modules/plugin-registry)で作成する。本関数は
    統合テストでは呼ばれない(テストは InMemoryObjectStore を使う)。OCID・認証値は .env 管理で
    リポジトリにコミットしない。
    """
    import os

    import oci

    bucket = os.environ["REGISTRY_BUCKET"]
    if os.environ.get("AUTH_MODE") == "resource_principal":
        # Container Instance/Functions 実行時。リージョンは OCI_REGION で渡す(jetuse_core.db と同様)
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.object_storage.ObjectStorageClient(
            {"region": os.environ.get("OCI_REGION", "")}, signer=signer
        )
    else:
        client = oci.object_storage.ObjectStorageClient(oci.config.from_file())
    namespace = os.environ.get("REGISTRY_NAMESPACE") or client.get_namespace().data
    return OciObjectStore(client, namespace, bucket)
