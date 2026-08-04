"""demo 箱が書き込むバックエンドの write-ahead 台帳 demo_backend_targets(specs/18 §3.2)。

外部書き込みの前に「秘密値を除く完全 locator」を INSERT・commit する追記型台帳。
削除(demo DELETE)は台帳の全 locator でクライアントを構成して行い、
「現在の設定で削除して NotFound=成功」では見逃す旧 target の資源を拾う。
スキップしてよいのは台帳にも行が無い(そのバックエンドを一度も使っていない)場合だけ。

書き込みは正規化 locator のハッシュに対する冪等 upsert(ORA-00001 成功扱い)—
upload/削除の反復では増えず、distinct な target 数(構成変更の回数)でのみ増える。
locator_hash の正規化形(SP2-00 residual N001 の解決): キーを辞書順に整列した
コンパクト JSON(ensure_ascii=False)・文字列値は末尾スラッシュ除去・sha256 hex。
"""

import hashlib
import json
import logging
import uuid

from .db import connect

logger = logging.getLogger("jetuse.demo_targets")

KINDS = ("vector_store", "files", "select_ai", "opensearch", "objectstorage")


def canonical_locator(locator: dict) -> str:
    """正規化 locator(N001): キー辞書順・コンパクト区切り・値の末尾スラッシュ除去。"""
    norm = {
        k: (v.rstrip("/") if isinstance(v, str) else v)
        for k, v in locator.items()
        if v not in (None, "")
    }
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def locator_hash(locator: dict) -> str:
    return hashlib.sha256(canonical_locator(locator).encode()).hexdigest()


def record_target(namespace: str, kind: str, locator: dict) -> None:
    """外部書き込みの前に呼ぶ(write-ahead)。冪等 upsert(ORA-00001 成功扱い)。"""
    if kind not in KINDS:
        raise ValueError(f"unknown backend kind: {kind}")
    canon = canonical_locator(locator)
    with connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """INSERT INTO demo_backend_targets(id, namespace, kind, locator, locator_hash)
                   VALUES (:id, :ns, :k, :loc, :h)""",
                id=str(uuid.uuid4()), ns=namespace, k=kind, loc=canon,
                h=locator_hash(locator),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            if "ORA-00001" not in str(e):
                raise
            conn.rollback()  # 既存 target(同一 locator)= 成功


def targets_for(namespace: str, kind: str | None = None) -> list[dict]:
    """namespace の記録済み target(複数の過去 target を保持)。削除の正はこれ。"""
    with connect() as conn:
        cur = conn.cursor()
        sql = ("SELECT kind, locator FROM demo_backend_targets WHERE namespace = :ns"
               + (" AND kind = :k" if kind else ""))
        binds = {"ns": namespace, **({"k": kind} if kind else {})}
        cur.execute(sql, **binds)
        return [
            {"kind": r[0],
             "locator": r[1] if isinstance(r[1], dict) else json.loads(r[1] or "{}")}
            for r in cur.fetchall()
        ]


def delete_targets(namespace: str) -> int:
    """RAG 箱の掃除が全て成功した後にのみ呼ぶ(specs/18 §3.2 手順 3b)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM demo_backend_targets WHERE namespace = :ns", ns=namespace)
        conn.commit()
        return cur.rowcount
