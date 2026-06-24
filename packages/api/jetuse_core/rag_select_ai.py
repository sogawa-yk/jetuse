"""Select AI RAGバックエンド(RAG-03)。ユーザーごとのprofile+vector indexを遅延作成。

SPIKE-08実機確定:
- ベクトル索引はADB 23ai+必須(19cはORA-20047)
- 索引はバケットURL直結(RAG-01の原本バックアップ rag/{owner}/ を共用)
- GENERATEの応答末尾に "Sources:" ブロックが自動付与される
前提: ops/setup-select-ai.py によるADMINセットアップ(権限+JETUSE_OCI_CRED)が済んでいること。
"""

import hashlib
import json
import logging
import re
import time
from typing import Any

import oracledb

from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.rag_select_ai")

LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"
REFRESH_RATE_MIN = 60
GENERATE_TIMEOUT_MS = 120_000  # narrateは1-3s実測だが索引初回等に余裕を持たせる


def _names(owner: str) -> tuple[str, str]:
    h = hashlib.sha1(owner.encode()).hexdigest()[:8].upper()
    return f"JETUSE_RAG_{h}", f"JETUSE_RAGIDX_{h}"


def _location(owner: str) -> str:
    s = get_settings()
    return (
        f"https://objectstorage.{s.oci_region}.oraclecloud.com"
        f"/n/{s.os_namespace}/b/{s.rag_bucket}/o/rag/{owner}"
    )


def ensure_profile(owner: str) -> str:
    """ユーザー用profile+vector indexを返す(なければ作成。索引構築は数十秒)"""
    profile, index = _names(owner)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_cloud_ai_profiles WHERE profile_name = :p",
            p=profile,
        )
        if cur.fetchone()[0] > 0:
            return profile
    s = get_settings()
    with connect() as conn:
        conn.call_timeout = 0  # 索引構築は長い: call_timeoutを外す
        cur = conn.cursor()
        cur.execute(
            "BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;",
            p=profile,
            a=json.dumps({
                "provider": "oci",
                "credential_name": s.select_ai_credential,
                "region": s.oci_region,
                "model": LLM,
                "embedding_model": EMBED,
                "vector_index_name": index,
            }),
        )
        cur.execute(
            "BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;",
            i=index,
            a=json.dumps({
                "vector_db_provider": "oracle",
                "location": _location(owner),
                "object_storage_credential_name": s.select_ai_credential,
                "profile_name": profile,
                "vector_distance_metric": "cosine",
                "chunk_size": 1024,
                "chunk_overlap": 128,
                "refresh_rate": REFRESH_RATE_MIN,
            }),
        )
        conn.commit()
        # 初回構築: ベクトル表に行が入るまで待つ(SPIKE-08: 3文書約20秒)
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{index}$VECTAB"')
                if cur.fetchone()[0] > 0:
                    break
            except oracledb.DatabaseError:
                pass
            time.sleep(5)
    return profile


def indexed_file_ids(owner: str) -> set[str]:
    """Select AIのベクトル索引に現在取り込まれている file_id 集合を返す。

    索引($VECTAB)の attributes.object_name = "{file_id}_{filename}"(RAG-01のバックアップ名)。
    索引は refresh_rate(既定60分)間隔でバケットから同期されるため、アップロード直後は未反映。
    """
    _, index = _names(owner)
    vectab = f"{index}$VECTAB"
    out: set[str] = set()
    try:
        with connect() as conn:
            conn.call_timeout = 15_000
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=vectab)
            if cur.fetchone()[0] == 0:
                return out  # 索引未作成(まだ誰もSelect AIで質問していない)
            cur.execute(
                f"SELECT DISTINCT JSON_VALUE(attributes, '$.object_name') FROM \"{vectab}\""
            )
            for (obj,) in cur.fetchall():
                if obj:
                    out.add(obj.split("_", 1)[0])
    except Exception:
        logger.exception("select_ai indexed_file_ids failed (ignored)")
    return out


_SOURCES_RE = re.compile(r"\n+Sources:\s*\n(.*)\Z", re.S)


def split_sources(answer: str) -> tuple[str, list[dict[str, Any]]]:
    """応答末尾の Sources: ブロックを citations 形式に変換して分離する"""
    m = _SOURCES_RE.search(answer)
    if not m:
        return answer, []
    body = answer[: m.start()].rstrip()
    citations = []
    for line in m.group(1).splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        name = line.split(" (")[0].strip()
        # RAG-01のバックアップオブジェクト名 {uuid}_{filename} から表示名を復元
        name = re.sub(r"^[0-9a-f]{8}-[0-9a-f-]{27}_", "", name)
        if name:
            citations.append({"file_id": name, "filename": name, "score": None})
    return body, citations


def generate(owner: str, prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """Select AI narrateで回答を生成し、(本文, citations) を返す"""
    profile = ensure_profile(owner)
    with connect() as conn:
        conn.call_timeout = GENERATE_TIMEOUT_MS
        cur = conn.cursor()
        cur.execute(
            """SELECT DBMS_CLOUD_AI.GENERATE(
                 prompt => :q, profile_name => :p, action => 'narrate') FROM dual""",
            q=prompt, p=profile,
        )
        answer = cur.fetchone()[0] or ""
    return split_sources(answer)
