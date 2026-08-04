"""会話リポジトリ(CHAT-02)。所有者(owner_sub)分離はすべてSQLのWHEREで強制する。

demo 紐付け(SP2-03 / specs/18 §4.2): owner_sub は資源キー列(owner_key ヘルパー経由)。
user 単位の全 verb(一覧/GET/DELETE/title/set_oci)は `demo_id IS NULL` を強制し、
demo 会話の user 経路への持ち込み・その逆を 404 にする(既存データは全行 NULL のため
Public 挙動は不変)。demo スコープは get/create の demo_id 引数で exact 一致。
"""

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
            FROM conversations WHERE owner_sub = :o AND demo_id IS NULL
            ORDER BY updated_at DESC
            FETCH FIRST 100 ROWS ONLY
            """,
            o=owner,
        )
        return [
            {"id": r[0], "title": r[1], "model": r[2], "updated_at": r[3]}
            for r in cur.fetchall()
        ]


def create_conversation(
    owner: str, model: str, title: str | None, demo_id: str | None = None
) -> dict[str, Any]:
    """demo_id 付きは箱への紐付け(specs/18 §4.2 — POST /api/demos/{id}/conversations)。"""
    cid = _uid()
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO conversations(id, owner_sub, title, model, demo_id)
            VALUES (:id, :o, :t, :m, :d)
            """,
            id=cid, o=owner, t=(title or "新しい会話")[:400], m=model, d=demo_id,
        )
        conn.commit()
    return {"id": cid, "title": title, "model": model}


def get_conversation(
    owner: str, cid: str, demo_id: str | None = None
) -> dict[str, Any] | None:
    """demo_id=None は user 単位(demo_id IS NULL 強制)、指定時は箱の exact 一致。
    不一致・不存在はどちらも None(ルート側 404 — 両方向の持ち込み拒否)。"""
    scope = "demo_id IS NULL" if demo_id is None else "demo_id = :d"
    binds = {"id": cid, "o": owner}
    if demo_id is not None:
        binds["d"] = demo_id
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, title, model, oci_conversation_id
            FROM conversations WHERE id = :id AND owner_sub = :o AND {scope}
            """,
            **binds,
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
            "DELETE FROM conversations WHERE id = :id AND owner_sub = :o "
            "AND demo_id IS NULL",
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
    """OCI Conversations(短期メモリ — CHAT-06)のIDを紐付ける。user 単位専用
    (demo 会話は OCI Conversation を作らない — specs/18 §4.2)。"""
    with connect() as conn:
        conn.cursor().execute(
            """
            UPDATE conversations SET oci_conversation_id = :oc
            WHERE id = :id AND owner_sub = :o AND demo_id IS NULL
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
            WHERE id = :id AND owner_sub = :o AND demo_id IS NULL
            """,
            t=title[:400], id=cid, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0


CHUNK_ROWS = 1000


def delete_demo_conversations(demo_id: str, chunk: int = CHUNK_ROWS) -> dict[str, int]:
    """demo の会話後始末(specs/18 §3.2 手順 4)。

    messages を明示チャンク(chunk 行 + commit)で先に削除し、ゼロ確認後に conversations の
    demo_id 行を同様にチャンク削除する。CASCADE 任せは「1 会話に大量 message」で
    タイムアウト → 全量ロールバックになる。チャンク commit ならタイムアウトしても進捗が残り、
    再 DELETE が続きから収束する。usage_log は削除しない(監査/利用量の明示的な保持契約)。
    """
    deleted = {"messages": 0, "conversations": 0}
    with connect() as conn:
        cur = conn.cursor()
        while True:
            cur.execute(
                """DELETE FROM messages WHERE conversation_id IN (
                     SELECT id FROM conversations WHERE demo_id = :d)
                   AND ROWNUM <= :n""",
                d=demo_id, n=chunk,
            )
            if cur.rowcount == 0:
                break
            deleted["messages"] += cur.rowcount
            conn.commit()
        # messages ゼロを確認してから conversations を消す(手順 4 の順序)
        cur.execute(
            """SELECT COUNT(*) FROM messages WHERE conversation_id IN (
                 SELECT id FROM conversations WHERE demo_id = :d)""",
            d=demo_id,
        )
        if cur.fetchone()[0] != 0:
            raise RuntimeError("demo messages remain after chunked delete")
        while True:
            cur.execute(
                "DELETE FROM conversations WHERE demo_id = :d AND ROWNUM <= :n",
                d=demo_id, n=chunk,
            )
            if cur.rowcount == 0:
                break
            deleted["conversations"] += cur.rowcount
            conn.commit()
    return deleted


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
