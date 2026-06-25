"""ユースケースリポジトリ(UC-01)。definition(JSON)を正としてCLOBに保存する。

一覧・取得は 組み込み + 自分の + 他人のpublic。編集・削除は所有者のみ(SQLで強制)。
"""

import json
import uuid
from typing import Any

from .db import connect
from .usecases_builtin import BUILTIN_USECASES


def _uid() -> str:
    return str(uuid.uuid4())


def _row_to_summary(r) -> dict[str, Any]:
    return {
        "id": r[0], "name": r[1], "description": r[2], "icon": r[3],
        "tags": [t for t in (r[4] or "").split(",") if t],
        "visibility": r[5], "owner_sub": r[6], "builtin": False,
    }


def list_usecases(owner: str) -> list[dict[str, Any]]:
    builtins = [
        {
            "id": u["id"], "name": u["name"], "description": u["description"],
            "icon": u.get("icon"), "tags": u.get("tags", []),
            "visibility": "public", "owner_sub": None, "builtin": True,
            "mine": False,
        }
        for u in BUILTIN_USECASES
    ]
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, description, icon, tags, visibility, owner_sub
            FROM usecases
            WHERE owner_sub = :o OR visibility = 'public'
            ORDER BY updated_at DESC
            FETCH FIRST 200 ROWS ONLY
            """,
            o=owner,
        )
        rows = []
        for r in cur.fetchall():
            s = _row_to_summary(r)
            s["mine"] = s["owner_sub"] == owner  # 編集リンク表示用(ホームカード)
            rows.append(s)
        return builtins + rows


def get_usecase(owner: str, uc_id: str) -> dict[str, Any] | None:
    for u in BUILTIN_USECASES:
        if u["id"] == uc_id:
            return {**u, "owner_sub": None}
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT definition, owner_sub, visibility FROM usecases
            WHERE id = :id AND (owner_sub = :o OR visibility = 'public')
            """,
            id=uc_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        return {**d, "id": uc_id, "owner_sub": row[1], "visibility": row[2], "builtin": False}


def create_usecase(owner: str, definition: dict[str, Any]) -> dict[str, Any]:
    uc_id = _uid()
    return _save(owner, uc_id, definition, insert=True)


def update_usecase(owner: str, uc_id: str, definition: dict[str, Any]) -> dict[str, Any] | None:
    return _save(owner, uc_id, definition, insert=False)


def _save(
    owner: str, uc_id: str, definition: dict[str, Any], insert: bool
) -> dict[str, Any] | None:
    d = {k: v for k, v in definition.items() if k not in ("id", "owner_sub", "builtin")}
    payload = json.dumps(d, ensure_ascii=False)
    tags = ",".join(d.get("tags", []))[:400]
    with connect() as conn:
        cur = conn.cursor()
        if insert:
            cur.execute(
                """
                INSERT INTO usecases(id, owner_sub, name, description, icon, tags,
                                     model, definition, visibility)
                VALUES (:id, :o, :n, :descr, :icn, :t, :m, :payload, :v)
                """,
                id=uc_id, o=owner, n=d["name"][:200],
                descr=(d.get("description") or "")[:1000],
                icn=(d.get("icon") or "")[:16], t=tags, m=d.get("model"),
                payload=payload, v=d.get("visibility", "private"),
            )
        else:
            cur.execute(
                """
                UPDATE usecases
                SET name = :n, description = :descr, icon = :icn, tags = :t,
                    model = :m, definition = :payload, visibility = :v,
                    updated_at = SYSTIMESTAMP
                WHERE id = :id AND owner_sub = :o
                """,
                n=d["name"][:200], descr=(d.get("description") or "")[:1000],
                icn=(d.get("icon") or "")[:16], t=tags, m=d.get("model"),
                payload=payload, v=d.get("visibility", "private"), id=uc_id, o=owner,
            )
            if cur.rowcount == 0:
                return None
        conn.commit()
    return {**d, "id": uc_id, "owner_sub": owner, "builtin": False}


def delete_usecase(owner: str, uc_id: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM usecases WHERE id = :id AND owner_sub = :o", id=uc_id, o=owner
        )
        conn.commit()
        return cur.rowcount > 0


# --- スナップショット取込(PLG-03 / D6) ---------------------------------------
# プラグイン取込で作られる定義は出所(source_plugin_id/source_version)を刻む。版固定の
# スナップショットであり、編集導線(create/update_usecase)とは別経路で書き込む。


def insert_ingested(
    owner: str,
    definition: dict[str, Any],
    *,
    source_plugin_id: str,
    source_version: str,
    visibility: str = "private",
) -> str:
    """プラグイン contributes を版固定で取り込んだユースケース定義を 1 件作成し、id を返す。

    通常の create_usecase と異なり source_plugin_id/source_version を刻む(出所追跡)。
    取込のアンインストールは delete_by_source で出所キーごと除去する。
    """
    uc_id = _uid()
    d = {k: v for k, v in definition.items() if k not in ("id", "owner_sub", "builtin")}
    payload = json.dumps(d, ensure_ascii=False)
    tags = ",".join(d.get("tags", []))[:400]
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usecases(id, owner_sub, name, description, icon, tags,
                                 model, definition, visibility,
                                 source_plugin_id, source_version)
            VALUES (:id, :o, :n, :descr, :icn, :t, :m, :payload, :v, :spid, :sver)
            """,
            id=uc_id, o=owner, n=d["name"][:200],
            descr=(d.get("description") or "")[:1000],
            icn=(d.get("icon") or "")[:16], t=tags, m=d.get("model"),
            payload=payload, v=visibility,
            spid=source_plugin_id, sver=source_version,
        )
        conn.commit()
    return uc_id


def delete_by_source(source_plugin_id: str, source_version: str) -> int:
    """指定プラグイン版から取り込んだユースケース定義を全削除し、削除件数を返す。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM usecases
            WHERE source_plugin_id = :spid AND source_version = :sver
            """,
            spid=source_plugin_id, sver=source_version,
        )
        conn.commit()
        return cur.rowcount


def delete_ingested(uc_id: str) -> bool:
    """取込で作成したユースケース定義 1 件を id で削除する(取込失敗時の補償用)。

    出所キーごとの一括削除(delete_by_source)と違い、特定の取込行だけを消す。誤って通常の
    ユーザー定義を消さないよう、source_plugin_id が刻まれた取込行に限定する。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM usecases WHERE id = :id AND source_plugin_id IS NOT NULL",
            id=uc_id,
        )
        conn.commit()
        return cur.rowcount > 0
