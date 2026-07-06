"""デモ最小レジストリ(SP1-02 / specs/17 §5)。

get_demo は owner_sub / visibility を含む生取得で、認可判定は呼び出し側(require_demo)。
削除は所有者のみ(SQLで強制)。REST CRUD・箱のプロビジョニングは SP2。
"""

import uuid
from typing import Any

from .db import connect


def create_demo(owner: str, name: str) -> dict[str, Any]:
    demo_id = str(uuid.uuid4())
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO demos(id, owner_sub, name) VALUES (:id, :o, :n)",
            id=demo_id, o=owner, n=name[:200],
        )
        conn.commit()
    return {"id": demo_id, "owner_sub": owner, "name": name[:200], "visibility": "private"}


def get_demo(demo_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, owner_sub, name, visibility FROM demos WHERE id = :id",
            id=demo_id,
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "owner_sub": row[1], "name": row[2], "visibility": row[3]}


def delete_demo(owner: str, demo_id: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM demos WHERE id = :id AND owner_sub = :o", id=demo_id, o=owner
        )
        conn.commit()
        return cur.rowcount > 0
