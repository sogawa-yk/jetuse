"""会話リポジトリ(CHAT-02)。所有者(owner_sub)分離はすべてSQLのWHEREで強制する。"""

import uuid
from typing import Any

from .db import connect


def _uid() -> str:
    return str(uuid.uuid4())


def list_conversations(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, model, TO_CHAR(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS')
            FROM conversations WHERE owner_sub = :o
            ORDER BY updated_at DESC
            FETCH FIRST 100 ROWS ONLY
            """,
            o=owner,
        )
        return [
            {"id": r[0], "title": r[1], "model": r[2], "updated_at": r[3]}
            for r in cur.fetchall()
        ]


def create_conversation(owner: str, model: str, title: str | None) -> dict[str, Any]:
    cid = _uid()
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO conversations(id, owner_sub, title, model)
            VALUES (:id, :o, :t, :m)
            """,
            id=cid, o=owner, t=(title or "新しい会話")[:400], m=model,
        )
        conn.commit()
    return {"id": cid, "title": title, "model": model}


def get_conversation(owner: str, cid: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, model, oci_conversation_id
            FROM conversations WHERE id = :id AND owner_sub = :o
            """,
            id=cid, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = :id ORDER BY seq
            """,
            id=cid,
        )
        msgs = [{"role": r[0], "content": r[1]} for r in cur.fetchall()]
        return {
            "id": row[0], "title": row[1], "model": row[2],
            "oci_conversation_id": row[3], "messages": msgs,
        }


def delete_conversation(owner: str, cid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM conversations WHERE id = :id AND owner_sub = :o",
            id=cid, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0


def append_message(cid: str, role: str, content: str) -> None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO messages(id, conversation_id, seq, role, content)
            SELECT :id, :cid, NVL(MAX(seq), 0) + 1, :role, :content
            FROM messages WHERE conversation_id = :cid2
            """,
            id=_uid(), cid=cid, role=role, content=content, cid2=cid,
        )
        cur.execute(
            "UPDATE conversations SET updated_at = SYSTIMESTAMP WHERE id = :id", id=cid
        )
        conn.commit()


def set_oci_conversation(owner: str, cid: str, oci_conversation_id: str) -> None:
    """OCI Conversations(短期メモリ — CHAT-06)のIDを紐付ける。"""
    with connect() as conn:
        conn.cursor().execute(
            """
            UPDATE conversations SET oci_conversation_id = :oc
            WHERE id = :id AND owner_sub = :o
            """,
            oc=oci_conversation_id, id=cid, o=owner,
        )
        conn.commit()


def update_title(owner: str, cid: str, title: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE conversations SET title = :t
            WHERE id = :id AND owner_sub = :o
            """,
            t=title[:400], id=cid, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0


def log_usage(
    owner: str, cid: str | None, model: str, input_tokens: int, output_tokens: int
) -> None:
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO usage_log(id, owner_sub, conversation_id, model,
                                  input_tokens, output_tokens)
            VALUES (:id, :o, :cid, :m, :it, :ot)
            """,
            id=_uid(), o=owner, cid=cid, m=model, it=input_tokens, ot=output_tokens,
        )
        conn.commit()
