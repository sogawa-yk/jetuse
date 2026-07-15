"""MCPサーバーレジストリ(AGT-02)。owner_sub分離、認証情報はVault(OCIDのみ保持)。

SPIKE-11実機確定: Responses APIの type:"mcp" ツールでサーバーサイド実行される。
"""

import logging
import uuid
from typing import Any
from urllib.parse import urlparse

from .db import connect
from .webtools import SsrfBlockedError, _assert_public_host

logger = logging.getLogger("jetuse.mcp")


def _uid() -> str:
    return str(uuid.uuid4())


def validate_url(url: str) -> None:
    """https必須 + 公開ホストのみ(SSRFガード流用)"""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise SsrfBlockedError("MCPサーバーのURLはhttpsである必要があります")
    _assert_public_host(p.hostname)


def list_servers(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, label, url, auth_secret_ocid,
                   TO_CHAR(created_at, 'YYYY-MM-DD"T"HH24:MI:SS')
            FROM mcp_servers WHERE owner_sub = :o ORDER BY created_at
            """,
            o=owner,
        )
        return [
            {
                "id": r[0], "label": r[1], "url": r[2],
                "has_auth": r[3] is not None, "created_at": r[4],
            }
            for r in cur.fetchall()
        ]


def get_servers(owner: str, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    binds = {f"id{i}": v for i, v in enumerate(ids[:10])}
    placeholders = ", ".join(f":id{i}" for i in range(len(binds)))
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, label, url, auth_secret_ocid FROM mcp_servers
            WHERE owner_sub = :o AND id IN ({placeholders})
            """,
            o=owner, **binds,
        )
        return [
            {"id": r[0], "label": r[1], "url": r[2], "auth_secret_ocid": r[3]}
            for r in cur.fetchall()
        ]


def create_server(owner: str, label: str, url: str, auth_secret_ocid: str | None) -> dict:
    validate_url(url)
    sid = _uid()
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO mcp_servers(id, owner_sub, label, url, auth_secret_ocid)
            VALUES (:id, :o, :l, :u, :a)
            """,
            id=sid, o=owner, l=label[:100], u=url[:1000], a=auth_secret_ocid,
        )
        conn.commit()
    return {"id": sid, "label": label, "url": url, "has_auth": auth_secret_ocid is not None}


def delete_server(owner: str, sid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM mcp_servers WHERE id = :id AND owner_sub = :o", id=sid, o=owner
        )
        conn.commit()
        return cur.rowcount > 0


def mcp_tool_spec(server: dict, auto: bool) -> dict:
    """Responses APIのmcpツール定義に変換(AGT-02)"""
    spec: dict = {
        "type": "mcp",
        "server_label": server["label"],
        "server_url": server["url"],
        "require_approval": "never" if auto else "always",
    }
    if server.get("auth_secret_ocid"):
        token = _read_secret(server["auth_secret_ocid"])
        spec["headers"] = {"Authorization": f"Bearer {token}"}
    return spec


def _read_secret(secret_ocid: str) -> str:
    import base64
    import os

    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.secrets.SecretsClient({}, signer=signer)
    else:
        from .genai import load_local_oci_config

        client = oci.secrets.SecretsClient(load_local_oci_config())
    bundle = client.get_secret_bundle(secret_ocid).data
    return base64.b64decode(bundle.secret_bundle_content.content).decode()
