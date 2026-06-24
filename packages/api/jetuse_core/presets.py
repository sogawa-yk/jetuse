"""システムプロンプトプリセット(CHAT-04)。所有者分離はSQLで強制。"""

import uuid
from typing import Any

from .db import connect


def list_presets(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, content FROM prompt_presets
            WHERE owner_sub = :o ORDER BY created_at DESC
            FETCH FIRST 50 ROWS ONLY
            """,
            o=owner,
        )
        return [{"id": r[0], "name": r[1], "content": r[2]} for r in cur.fetchall()]


def create_preset(owner: str, name: str, content: str) -> dict[str, Any]:
    pid = str(uuid.uuid4())
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO prompt_presets(id, owner_sub, name, content)
            VALUES (:id, :o, :n, :c)
            """,
            id=pid, o=owner, n=name[:200], c=content,
        )
        conn.commit()
    return {"id": pid, "name": name, "content": content}


def delete_preset(owner: str, pid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM prompt_presets WHERE id = :id AND owner_sub = :o",
            id=pid, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0
