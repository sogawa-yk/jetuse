"""生成デモバンドルの Object Storage 保管・配信・後始末(specs/19 §5.1/§5.2/§5.4)。

保管先 = rag と同じ Terraform 管理バケット(settings.rag_bucket)の `demo-bundles/` prefix
(spec §5.1: 専用バケットを新設せず既存を再利用)。オブジェクト名 =
`demo-bundles/<sha1(namespace) 40hex>/<bundle_id>/<相対パス>`。namespace = DemoContext.namespace
(= demo_<id>)。sha1 完全ハッシュ命名は specs/18 §3.1 と同じ削除根拠の規律(長さ有界・exact)。
"""
from __future__ import annotations

from .owner_keys import owner_hash
from .rag import _assert_bucket_not_versioned, _os_client, _resolve_os_namespace, delete_objects
from .settings import get_settings

_BUNDLE_ROOT = "demo-bundles"


def demo_prefix(namespace: str) -> str:
    """当該デモの全バンドル(公開・staging)の prefix。DELETE 3g の全列挙・回収に使う。"""
    return f"{_BUNDLE_ROOT}/{owner_hash(namespace)}/"


def bundle_prefix(namespace: str, bundle_id: str) -> str:
    return f"{demo_prefix(namespace)}{bundle_id}/"


def object_name(namespace: str, bundle_id: str, rel_path: str) -> str:
    return f"{bundle_prefix(namespace, bundle_id)}{rel_path}"


def _bucket(locator: dict | None) -> str:
    return (locator or {}).get("bucket") or get_settings().rag_bucket


def _client_ns(locator: dict | None):
    client = _os_client((locator or {}).get("region"))
    ns = (locator or {}).get("os_namespace") or _resolve_os_namespace(client)
    return client, ns


def _list_prefix(prefix: str, locator: dict | None) -> list[str]:
    """prefix 配下の object 名をページネーション完走で全列挙(next_start_with)。"""
    if not _bucket(locator):
        return []
    client, ns = _client_ns(locator)
    bucket = _bucket(locator)
    names: list[str] = []
    start = None
    while True:
        kw = {"prefix": prefix, "fields": "name"}
        if start:
            kw["start"] = start
        resp = client.list_objects(ns, bucket, **kw)
        names.extend(o.name for o in resp.data.objects)
        start = resp.data.next_start_with
        if not start:
            return names


def put_files(namespace: str, bundle_id: str, files: dict[str, bytes],
              locator: dict | None = None) -> None:
    """バンドルの {相対パス: bytes} を一括 put。versioning=Disabled 必須(削除保証の前提 §5.1)。"""
    s = get_settings()
    if not s.rag_bucket:
        raise RuntimeError("bundle bucket (rag_bucket) is not configured")
    client, ns = _client_ns(locator)
    bucket = _bucket(locator)
    _assert_bucket_not_versioned(client, ns, bucket)
    for rel, content in files.items():
        client.put_object(ns, bucket, object_name(namespace, bundle_id, rel), content)


def get_object(namespace: str, bundle_id: str, rel_path: str,
               locator: dict | None = None) -> bytes | None:
    """配信用に 1 オブジェクト取得(specs/19 §5.2)。不存在は None(→ 404)。"""
    import oci as oci_sdk

    if not _bucket(locator):
        return None
    client, ns = _client_ns(locator)
    bucket = _bucket(locator)
    try:
        resp = client.get_object(ns, bucket, object_name(namespace, bundle_id, rel_path))
    except oci_sdk.exceptions.ServiceError as e:
        if e.status == 404:
            return None
        raise
    return resp.data.content


def list_demo_objects(namespace: str, locator: dict | None = None) -> list[str]:
    """当該デモの demo-bundles prefix 配下を全列挙(DELETE 3g — 公開・staging・失敗分を一括回収)。"""
    return _list_prefix(demo_prefix(namespace), locator)


def delete_bundle(namespace: str, bundle_id: str, locator: dict | None = None) -> None:
    """1 バンドル prefix 配下を削除(ポインタ切替後の旧バンドル掃除 — §5.1 best-effort)。"""
    delete_objects(_list_prefix(bundle_prefix(namespace, bundle_id), locator), locator)
