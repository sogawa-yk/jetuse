"""Select AI RAGバックエンド(RAG-03)。ユーザーごとのprofile+vector indexを遅延作成。

SPIKE-08実機確定:
- ベクトル索引はADB 23ai+必須(19cはORA-20047)
- 索引はバケットURL直結(RAG-01の原本 rag/<sha1(owner)>/ を共用 — SP2-02 で exact 命名化)
- GENERATEの応答末尾に "Sources:" ブロックが自動付与される
前提: ops/setup-select-ai.py によるADMINセットアップ(権限+JETUSE_OCI_CRED)が済んでいること。

SP2-02(specs/18 §3.2 手順 3d):
- demo namespace の profile/index 名は完全 sha1(切詰めなし)から導出。sha1[:8]=32bit の
  衝突は 2 owner が同一 index を共有 = 越境読取・削除波及になる。既存 user 資産の 8hex は
  変えない(main 互換 — user 側は main バックポート課題の residual)。
- 原本の object 名は owner_keys.file_key 由来(<rid>.<ext>)。$VECTAB の object_name 突合は
  新旧両規約に対応(SP2-00 residual M003 の解決): 新 = "<rid>.<ext>" → rid、
  旧 = "<file_id>_<filename>" → file_id。rag_files.id = reservation_id のため同じ ID 空間。
- 個別削除の同期反映 sync_remove_file / 箱の後始末 delete_owner を公開(命名の再実装禁止)。
"""

import hashlib
import json
import logging
import re
import time
from typing import Any

import oracledb

from . import demo_targets
from .db import connect
from .owner_keys import _storage_seg, is_demo_namespace
from .settings import get_settings

logger = logging.getLogger("jetuse.rag_select_ai")

LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"
REFRESH_RATE_MIN = 60
GENERATE_TIMEOUT_MS = 120_000  # narrateは1-3s実測だが索引初回等に余裕を持たせる


def _names(owner: str) -> tuple[str, str]:
    """profile/index 名の正本。demo は完全 sha1(40hex)、user は従来 8hex(main 互換)。"""
    h = hashlib.sha1(owner.encode()).hexdigest()
    tag = (h if is_demo_namespace(owner) else h[:8]).upper()
    return f"JETUSE_RAG_{tag}", f"JETUSE_RAGIDX_{tag}"


def _location(owner: str) -> str:
    """索引のデータ源(原本 prefix)。原本の owner セグメントと一致させる(demo=完全 sha1 /
    user=main 互換の raw owner — review-12 B002。原本と索引 location が食い違うと新規取込漏れ)。"""
    s = get_settings()
    return (
        f"https://objectstorage.{s.oci_region}.oraclecloud.com"
        f"/n/{s.os_namespace}/b/{s.rag_bucket}/o/rag/{_storage_seg(owner)}"
    )


def ensure_profile(owner: str, lease=None) -> str:
    """ユーザー用profile+vector indexを返す(なければ作成。索引構築は数十秒)。

    demo namespace の新規作成は demo 単位リース保持が前提(specs/18 §3.2.1 — 解体中の箱を
    lazy 生成で復活させ孤児化するのを防ぐ)。既存を返す読み取り経路はリース不要。
    """
    from .demo_lease import require_lease_for

    profile, index = _names(owner)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM user_cloud_ai_profiles WHERE profile_name = :p",
            p=profile,
        )
        if cur.fetchone()[0] > 0:
            return profile
    require_lease_for(owner, lease)  # 作成の直前にだけリース保持を検証
    s = get_settings()
    if is_demo_namespace(owner):
        # write-ahead 台帳(specs/18 §3.2): 外部書き込み(索引の原本参照先)の前に記録
        demo_targets.record_target(owner, "select_ai", {
            "region": s.oci_region, "os_namespace": s.os_namespace,
            "bucket": s.rag_bucket,
        })
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


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _file_id_from_object_name(obj: str) -> str | None:
    """$VECTAB / Sources の object 名から file_id を復元する(新旧両規約 — M003)。

    新: "<rid>.<ext>"(rag_files.id = reservation_id) / 旧: "<file_id>_<filename>"。
    パス prefix が付く場合は basename で判定。
    """
    base = (obj or "").rsplit("/", 1)[-1]
    m = _UUID_RE.match(base)
    if not m:
        return None
    rest = base[36:]
    if rest == "" or rest.startswith(".") or rest.startswith("_"):
        return base[:36]
    return None


def indexed_file_ids(owner: str) -> set[str]:
    """Select AIのベクトル索引に現在取り込まれている file_id 集合を返す。

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
                fid = _file_id_from_object_name(obj)
                if fid:
                    out.add(fid)
    except Exception:
        logger.exception("select_ai indexed_file_ids failed (ignored)")
    return out


def sync_remove_file(owner: str, file_id: str) -> None:
    """個別ファイル削除の索引への同期反映(specs/18 §3.2 — 同期一択)。

    原本 object の削除後に呼ぶ。$VECTAB から当該 object_name のチャンク行を削除し、
    不存在を確認する(refresh_rate の周期まで残存すると削除後も当該文書で回答しうる)。
    失敗は例外のまま伝播(呼び出し側が 503 で行とカウンタを保持 → 再試行で収束)。
    索引未作成なら何もしない。
    """
    _, index = _names(owner)
    vectab = f"{index}$VECTAB"
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=vectab)
        if cur.fetchone()[0] == 0:
            return
        # ponytail: パイプライン再実行でなく $VECTAB 直接削除(原本は削除済みのため
        # 次回同期でも再出現しない)。object_name は新旧両規約(basename 末尾)に一致させる。
        # path prefix 付き(<sha1>/<name>)も拾うため接頭辞は %。ESCAPE 句必須
        # (バックスラッシュは Oracle の LIKE で既定メタ文字ではない — codex review-2 blocker)。
        match = """(
              JSON_VALUE(attributes, '$.object_name') LIKE :new_style ESCAPE '\\'
           OR JSON_VALUE(attributes, '$.object_name') LIKE :old_style ESCAPE '\\'
           OR JSON_VALUE(attributes, '$.object_name') LIKE :new_pref ESCAPE '\\'
           OR JSON_VALUE(attributes, '$.object_name') LIKE :old_pref ESCAPE '\\'
        )"""
        binds = {
            "new_style": f"{file_id}.%",       # 新: <rid>.<ext>
            "old_style": f"{file_id}\\_%",     # 旧: <file_id>_<filename>
            "new_pref": f"%/{file_id}.%",      # path prefix 付き 新
            "old_pref": f"%/{file_id}\\_%",    # path prefix 付き 旧
        }
        cur.execute(f'DELETE FROM "{vectab}" WHERE {match}', **binds)
        conn.commit()
        cur.execute(f'SELECT COUNT(*) FROM "{vectab}" WHERE {match}', **binds)
        if cur.fetchone()[0] != 0:
            raise RuntimeError(f"$VECTAB rows for {file_id} still present after delete")


def delete_owner(owner: str) -> None:
    """箱の後始末(specs/18 §3.2 手順 3d): owner の profile と vector index を DROP。

    不存在は無視($VECTAB は index とともに消える)。名前は _names() の決定的導出のみを
    根拠にする(命名の再実装禁止 — 後始末側で規則を書かない)。
    """
    profile, index = _names(owner)
    with connect() as conn:
        conn.call_timeout = 60_000
        cur = conn.cursor()
        # 不存在の無視は「存在確認 → DROP」で実現(ORA コードの一律無視で実失敗を隠さない)
        cur.execute(
            "SELECT COUNT(*) FROM user_cloud_ai_profiles WHERE profile_name = :p",
            p=profile,
        )
        if cur.fetchone()[0]:
            cur.execute("BEGIN DBMS_CLOUD_AI.DROP_PROFILE(:p); END;", p=profile)
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=f"{index}$VECTAB"
        )
        if cur.fetchone()[0]:
            cur.execute("BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX(:i); END;", i=index)
        conn.commit()


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
        if not name:
            continue
        fid = _file_id_from_object_name(name)
        # 表示名の暫定値: 旧命名 {uuid}_{filename} は prefix を剥がす(既存互換)。
        # 最終的な表示名は resolve_citation_filenames が DB の元名で解決する(specs/18 §3.1)
        display = re.sub(r"^[0-9a-f]{8}-[0-9a-f-]{27}_", "", name)
        citations.append(
            {"file_id": fid or display, "filename": display, "score": None}
        )
    return body, citations


def generate(owner: str, prompt: str, lease=None) -> tuple[str, list[dict[str, Any]]]:
    """Select AI narrateで回答を生成し、(本文, citations) を返す。

    demo namespace の初回は profile/index を lazy 生成するため lease を渡す(呼び出し側が
    リースを保持。SSE 生成本体はリースを跨がないが、作成区間だけは保持する — specs/18 §3.2.1)。
    """
    profile = ensure_profile(owner, lease=lease)
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
