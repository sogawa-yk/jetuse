"""エージェントリポジトリ(AGT-03)。owner分離 + public共有。

Project割当(project_ocid)でそのエージェントの会話・記憶を分離する(SPIKE-05)。
"""

import json
import uuid
from typing import Any

from .db import connect


def _uid() -> str:
    return str(uuid.uuid4())


_COLS = """id, owner_sub, name, description, icon, instructions, model,
           enabled_tools, mcp_server_ids, project_ocid, visibility, tags, auto_tools,
           framework, source_plugin_id, source_version"""


def _row_to_agent(r) -> dict[str, Any]:
    return {
        "id": r[0], "owner_sub": r[1], "name": r[2], "description": r[3],
        "icon": r[4], "instructions": r[5], "model": r[6],
        "enabled_tools": json.loads(r[7]) if r[7] else [],
        "mcp_server_ids": json.loads(r[8]) if r[8] else [],
        "project_ocid": r[9], "visibility": r[10],
        "tags": [t for t in (r[11] or "").split(",") if t],
        "auto_tools": bool(r[12]),
        "framework": r[13] or "native",  # FW-01
        # 出所追跡(PLG-02/03)。プラグイン取込でなければ None。
        # コントリビューションローダー(PLG-07)が出所バッジ・名前衝突解決に使う。
        "source_plugin_id": r[14], "source_version": r[15],
    }


def list_agents(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_COLS} FROM agents
            WHERE owner_sub = :o OR visibility = 'public'
            ORDER BY updated_at DESC FETCH FIRST 100 ROWS ONLY
            """,
            o=owner,
        )
        out = []
        for r in cur.fetchall():
            a = _row_to_agent(r)
            a["mine"] = a["owner_sub"] == owner
            a.pop("instructions", None)  # 一覧は軽量化
            out.append(a)
        return out


def get_agent(owner: str, agent_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_COLS} FROM agents
            WHERE id = :id AND (owner_sub = :o OR visibility = 'public')
            """,
            id=agent_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        a = _row_to_agent(row)
        a["mine"] = a["owner_sub"] == owner
        return a


def create_agent(owner: str, data: dict[str, Any]) -> dict[str, Any]:
    aid = _uid()
    _save(owner, aid, data, insert=True)
    return {**data, "id": aid, "mine": True}


def update_agent(owner: str, aid: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if not _save(owner, aid, data, insert=False):
        return None
    return {**data, "id": aid, "mine": True}


def _save(owner: str, aid: str, d: dict[str, Any], insert: bool) -> bool:
    binds = {
        "n": d["name"][:200],
        "descr": (d.get("description") or "")[:1000],
        "icn": (d.get("icon") or "")[:16],
        "ins": d["instructions"],
        "m": d["model"],
        "et": json.dumps(d.get("enabled_tools") or []),
        "mcp": json.dumps(d.get("mcp_server_ids") or []),
        "proj": d.get("project_ocid"),
        "v": d.get("visibility", "private"),
        "t": ",".join(d.get("tags") or [])[:400],
        "at": 1 if d.get("auto_tools") else 0,
        "fw": d.get("framework") or "native",
    }
    with connect() as conn:
        cur = conn.cursor()
        if insert:
            cur.execute(
                """
                INSERT INTO agents(id, owner_sub, name, description, icon, instructions,
                                   model, enabled_tools, mcp_server_ids, project_ocid,
                                   visibility, tags, auto_tools, framework)
                VALUES (:id, :o, :n, :descr, :icn, :ins, :m, :et, :mcp, :proj, :v, :t, :at,
                        :fw)
                """,
                id=aid, o=owner, **binds,
            )
        else:
            cur.execute(
                """
                UPDATE agents SET name=:n, description=:descr, icon=:icn,
                       instructions=:ins, model=:m, enabled_tools=:et,
                       mcp_server_ids=:mcp, project_ocid=:proj, visibility=:v,
                       tags=:t, auto_tools=:at, framework=:fw, updated_at=SYSTIMESTAMP
                WHERE id = :id AND owner_sub = :o
                """,
                id=aid, o=owner, **binds,
            )
            if cur.rowcount == 0:
                return False
        conn.commit()
    return True


def delete_agent(owner: str, aid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM agents WHERE id = :id AND owner_sub = :o", id=aid, o=owner)
        conn.commit()
        return cur.rowcount > 0


# --- スナップショット取込(PLG-03 / D6) ---------------------------------------
# プラグイン取込で作られるエージェント定義は出所(source_plugin_id/source_version)を刻む。
# 版固定のスナップショットで、編集導線(create/update_agent)とは別経路で書き込む。


def insert_ingested(
    owner: str,
    data: dict[str, Any],
    *,
    source_plugin_id: str,
    source_version: str,
    visibility: str = "private",
) -> str:
    """プラグイン contributes を版固定で取り込んだエージェント定義を 1 件作成し、id を返す。"""
    aid = _uid()
    binds = {
        "n": data["name"][:200],
        "descr": (data.get("description") or "")[:1000],
        "icn": (data.get("icon") or "")[:16],
        "ins": data["instructions"],
        "m": data["model"],
        "et": json.dumps(data.get("enabled_tools") or []),
        "mcp": json.dumps(data.get("mcp_server_ids") or []),
        "proj": data.get("project_ocid"),
        "v": visibility,
        "t": ",".join(data.get("tags") or [])[:400],
        "at": 1 if data.get("auto_tools") else 0,
        "fw": data.get("framework") or "native",
    }
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agents(id, owner_sub, name, description, icon, instructions,
                               model, enabled_tools, mcp_server_ids, project_ocid,
                               visibility, tags, auto_tools, framework,
                               source_plugin_id, source_version)
            VALUES (:id, :o, :n, :descr, :icn, :ins, :m, :et, :mcp, :proj, :v, :t, :at,
                    :fw, :spid, :sver)
            """,
            id=aid, o=owner, spid=source_plugin_id, sver=source_version, **binds,
        )
        conn.commit()
    return aid


def delete_by_source(source_plugin_id: str, source_version: str) -> int:
    """指定プラグイン版から取り込んだエージェント定義を全削除し、削除件数を返す。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM agents
            WHERE source_plugin_id = :spid AND source_version = :sver
            """,
            spid=source_plugin_id, sver=source_version,
        )
        conn.commit()
        return cur.rowcount


def delete_ingested(aid: str) -> bool:
    """取込で作成したエージェント定義 1 件を id で削除する(取込失敗時の補償用)。

    出所キーごとの一括削除(delete_by_source)と違い、特定の取込行だけを消す。誤って通常の
    ユーザー定義を消さないよう、source_plugin_id が刻まれた取込行に限定する。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM agents WHERE id = :id AND source_plugin_id IS NOT NULL",
            id=aid,
        )
        conn.commit()
        return cur.rowcount > 0


def list_projects() -> list[dict[str, str]]:
    """Project割当の選択肢(コンパートメント内ACTIVE)。SDKで取得"""
    import os

    import oci

    from .settings import get_settings

    s = get_settings()
    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.generative_ai.GenerativeAiClient(
            {"region": s.oci_region}, signer=signer
        )
    else:
        client = oci.generative_ai.GenerativeAiClient(oci.config.from_file())
    res = client.list_generative_ai_projects(compartment_id=s.compartment_ocid)
    return [
        {"id": p.id, "name": p.display_name}
        for p in res.data.items
        if p.lifecycle_state == "ACTIVE"
    ]
