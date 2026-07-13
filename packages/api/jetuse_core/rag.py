"""RAGファイル管理(RAG-01)。ユーザーごとのVector Store + ADBで状態管理。

SPIKE-03実機確定事項に準拠:
- ストア本体CRUD=CPクライアント、files系=DPクライアント(OpenAi-Project必須)
- ファイル単位で取り込み(バッチは1失敗で全体400)。docx非対応
- CP completed後のDP伝播待ちが必要
"""

import logging
import os
import time
import uuid
from typing import Any

import oracledb
from openai import NotFoundError

from .db import connect
from .genai import make_cp_client, make_inference_client, resolve_project_ocid
from .settings import get_settings

logger = logging.getLogger("jetuse.rag")

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
MAX_BYTES = 20 * 1024 * 1024


class StoreNotReadyError(Exception):
    """Vector Storeが使える状態にない(DP伝播リトライ枯渇・登録簿競合の異常)。
    ルート側で503に正規化する(SP1-03 REV-007)。"""


def _uid() -> str:
    return str(uuid.uuid4())


# --- ADBリポジトリ ---


def get_store_id(owner: str) -> str | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT vector_store_id FROM rag_stores WHERE owner_sub = :o", o=owner)
        row = cur.fetchone()
        return row[0] if row else None


def _save_store_id(owner: str, vs_id: str) -> bool:
    """登録簿へ登録できたらTrue。同時作成で負けたら(ORA-00001)False(SP1-03 REV-008)。"""
    try:
        with connect() as conn:
            conn.cursor().execute(
                "INSERT INTO rag_stores(owner_sub, vector_store_id) VALUES (:o, :v)",
                o=owner, v=vs_id,
            )
            conn.commit()
        return True
    except oracledb.IntegrityError as e:
        (err,) = e.args
        if getattr(err, "full_code", "") == "ORA-00001":
            return False
        raise


def list_files(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, filename, oci_file_id, status, bytes, error,
                   TO_CHAR(created_at, 'YYYY-MM-DD"T"HH24:MI:SS')
            FROM rag_files WHERE owner_sub = :o ORDER BY created_at DESC
            """,
            o=owner,
        )
        return [
            {
                "id": r[0], "filename": r[1], "oci_file_id": r[2], "status": r[3],
                "bytes": r[4], "error": r[5], "created_at": r[6],
            }
            for r in cur.fetchall()
        ]


def _insert_file(owner: str, file_id: str, filename: str, oci_file_id: str, size: int) -> None:
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO rag_files(id, owner_sub, filename, oci_file_id, status, bytes)
            VALUES (:id, :o, :f, :ofi, 'processing', :b)
            """,
            id=file_id, o=owner, f=filename[:400], ofi=oci_file_id, b=size,
        )
        conn.commit()


def _update_status(owner: str, file_id: str, status: str, error: str | None = None) -> None:
    with connect() as conn:
        conn.cursor().execute(
            """
            UPDATE rag_files SET status = :s, error = :e
            WHERE id = :id AND owner_sub = :o
            """,
            s=status, e=(error or "")[:1000] or None, id=file_id, o=owner,
        )
        conn.commit()


def _delete_row(owner: str, file_id: str) -> dict | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT oci_file_id, filename FROM rag_files WHERE id = :id AND owner_sub = :o",
            id=file_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "DELETE FROM rag_files WHERE id = :id AND owner_sub = :o", id=file_id, o=owner
        )
        conn.commit()
        return {"oci_file_id": row[0], "filename": row[1]}


# --- Object Storage原本バックアップ(ベストエフォート) ---


def _os_client():
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        return oci.object_storage.ObjectStorageClient(
            {"region": get_settings().oci_region}, signer=signer
        )
    return oci.object_storage.ObjectStorageClient(oci.config.from_file())


def _backup_original(owner: str, file_id: str, filename: str, content: bytes) -> None:
    bucket = get_settings().rag_bucket
    if not bucket:
        return
    try:
        client = _os_client()
        ns = client.get_namespace().data
        client.put_object(ns, bucket, f"rag/{owner}/{file_id}_{filename}", content)
    except Exception:
        logger.exception("rag original backup failed (ignored)")


def _delete_original(owner: str, file_id: str, filename: str) -> None:
    bucket = get_settings().rag_bucket
    if not bucket:
        return
    try:
        client = _os_client()
        ns = client.get_namespace().data
        client.delete_object(ns, bucket, f"rag/{owner}/{file_id}_{filename}")
    except Exception:
        logger.exception("rag original delete failed (ignored)")


# --- プリフライト診断(FIX-47) ---


def _check_hint(e: Exception, what: str) -> str:
    """失敗ヒント。レスポンスbody(OCID等を含みうる)は載せずステータスと確認箇所だけ返す。"""
    code = getattr(e, "status_code", None)
    base = f"{what} の呼び出しが失敗"
    if code:
        base += f" (HTTP {code})"
    return (base + "。DG matching rule / IAM policy statements / PROJECT_OCID / "
            "リージョンの agentic API 対応を確認してください")


def health_check() -> dict[str, Any]:
    """RAG 経路の3点検査: ①project解決 ②CP vector_stores.list ③DP files.list(OpenAi-Project付き)。

    Issue #47 の報告者が「どこで落ちているか」を自己診断できる粒度で返す(認可済み前提)。
    """
    checks: dict[str, dict[str, Any]] = {}
    project: str | None = None
    try:
        project = resolve_project_ocid()
        checks["project"] = {
            "ok": True, "source": "env" if get_settings().project_ocid else "auto",
        }
    except Exception as e:  # noqa: BLE001 - 診断エンドポイント。落とさず構造化して返す
        checks["project"] = {"ok": False, "hint": str(e)}
    try:
        make_cp_client().vector_stores.list()
        checks["control_plane"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        checks["control_plane"] = {"ok": False,
                                   "hint": _check_hint(e, "CP vector_stores.list")}
    if project:
        try:
            make_inference_client(with_project=True).files.list()
            checks["data_plane"] = {"ok": True}
        except Exception as e:  # noqa: BLE001
            checks["data_plane"] = {"ok": False,
                                    "hint": _check_hint(e, "DP files.list")}
    else:
        checks["data_plane"] = {"ok": False, "hint": "project 未解決のため検査不能"}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


# --- Vector Store / Files API ---


def ensure_store(owner: str) -> str:
    """ユーザーのVector Storeを返す(なければ作成し、DP伝播まで待つ)"""
    vs_id = get_store_id(owner)
    if vs_id:
        return vs_id
    cp = make_cp_client()
    vs = cp.vector_stores.create(name=f"jetuse-rag-{owner[:32]}", metadata={"owner": owner[:64]})
    for _ in range(30):
        if cp.vector_stores.retrieve(vector_store_id=vs.id).status == "completed":
            break
        time.sleep(2)
    # CP completed後もDP伝播に10〜30秒(SPIKE-03)
    dp = make_inference_client(with_project=True)
    for _ in range(30):
        try:
            dp.vector_stores.files.list(vector_store_id=vs.id)
            break
        except Exception:
            time.sleep(2)
    if _save_store_id(owner, vs.id):
        return vs.id
    # 同時作成で負けた(REV-008): 自分の箱をbest-effortで片付け、勝者のstoreを使う
    try:
        cp.vector_stores.delete(vector_store_id=vs.id)
    except Exception:
        logger.exception("duplicate store cleanup failed (ignored)")
    winner = get_store_id(owner)
    if not winner:
        # 競合したのに勝者行が無い(想定外)。未登録のIDを返さず503へ
        raise StoreNotReadyError(f"rag_stores conflict for {owner[:32]} but no winner row")
    return winner


def add_file(owner: str, filename: str, content: bytes) -> dict[str, Any]:
    """Files APIへアップロードしVector Storeへ登録(status=processingで返す)"""
    vs_id = ensure_store(owner)
    file_id = _uid()
    _backup_original(owner, file_id, filename, content)
    dp = make_inference_client(with_project=True)
    f = dp.files.create(file=(filename, content), purpose="assistants")
    # CP completed直後はDP側にstoreが未伝播で404になる(SPIKE-03)。デモは箱ごとに新規store
    # なので初回uploadが通常経路 — 有界リトライで吸収する(SP1-03 REV-005)。
    for attempt in range(6):
        try:
            dp.vector_stores.files.create(vector_store_id=vs_id, file_id=f.id)
            break
        except NotFoundError:
            if attempt == 5:
                # リトライ枯渇(REV-007): DB行の無いファイルはAPIから辿れず孤立する —
                # best-effortで即後始末し、503に正規化できる型付き例外へ
                try:
                    dp.files.delete(f.id)
                except Exception:
                    logger.exception("orphan file cleanup failed (ignored)")
                _delete_original(owner, file_id, filename)
                raise StoreNotReadyError(
                    f"vector store {vs_id} not visible on DP after bounded retries"
                ) from None
            logger.info("vector store not yet visible on DP, retrying (%s)", attempt + 1)
            time.sleep(5)
    _insert_file(owner, file_id, filename, f.id, len(content))
    # OpenSearch RAG(ENH-05)にも取り込む(有効時のみ・best-effort)
    try:
        from . import rag_opensearch

        if rag_opensearch.enabled():
            rag_opensearch.ingest(owner, file_id, filename, content)
    except Exception:
        logger.exception("opensearch ingest failed (ignored)")
    return {"id": file_id, "filename": filename, "status": "processing", "bytes": len(content)}


def refresh_statuses(owner: str, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """processingの行だけDPへ問い合わせて反映する"""
    pending = [f for f in files if f["status"] == "processing"]
    if not pending:
        return files
    vs_id = get_store_id(owner)
    if not vs_id:
        return files
    dp = make_inference_client(with_project=True)
    for f in pending:
        try:
            vf = dp.vector_stores.files.retrieve(
                vector_store_id=vs_id, file_id=f["oci_file_id"]
            )
            if vf.status == "completed":
                f["status"] = "completed"
                _update_status(owner, f["id"], "completed")
            elif vf.status not in ("in_progress", "queued"):
                err = str(getattr(vf, "last_error", "") or vf.status)
                f["status"] = "failed"
                f["error"] = err
                _update_status(owner, f["id"], "failed", err)
        except Exception:
            logger.exception("rag status refresh failed (ignored)")
    return files


# Vector Storeのファイル状態をバックエンド共通の語彙へ
_VS_MAP = {"completed": "indexed", "processing": "pending", "failed": "error"}


def resolve_citation_filenames(owner: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """引用のファイル名を、こちらで保持する元のファイル名に置換する。

    OCI Files API は日本語(非ASCII)ファイル名を文字化けして返すことがあるため、
    citation.file_id(=oci_file_id か アプリ内file_id)からDBの元ファイル名へ解決する。
    一致しないものはそのまま返す(致命的でない)。
    """
    if not citations:
        return citations
    try:
        rows = list_files(owner)
    except Exception:
        logger.exception("resolve citation filenames failed (ignored)")
        return citations
    by_oci = {r["oci_file_id"]: r["filename"] for r in rows if r.get("oci_file_id")}
    by_id = {r["id"]: r["filename"] for r in rows}
    out: list[dict[str, Any]] = []
    for c in citations:
        fid = c.get("file_id")
        name = by_oci.get(fid) or by_id.get(fid)
        out.append({**c, "filename": name} if name else c)
    return out


def attach_backend_status(owner: str, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各ファイルに3バックエンドの取り込み状況を付与する(ENH-05 可視化)。

    backends[*] = "indexed" | "pending" | "error" | "disabled"
    - vector_store: Files API/Vector Storeの処理状態
    - select_ai: ベクトル索引($VECTAB)に存在するか(refresh_rate間隔で同期=反映が遅い)
    - opensearch: indexに存在するか(取り込みは同期=即時)。無効時は disabled
    """
    sai_ids: set[str] = set()
    os_ids: set[str] = set()
    os_enabled = False
    try:
        from . import rag_select_ai

        sai_ids = rag_select_ai.indexed_file_ids(owner)
    except Exception:
        logger.exception("select_ai status failed (ignored)")
    try:
        from . import rag_opensearch

        os_enabled = rag_opensearch.enabled()
        if os_enabled:
            os_ids = rag_opensearch.indexed_file_ids(owner)
    except Exception:
        logger.exception("opensearch status failed (ignored)")

    for f in files:
        fid = f["id"]
        f["backends"] = {
            "vector_store": _VS_MAP.get(f.get("status", ""), "pending"),
            "select_ai": "indexed" if fid in sai_ids else "pending",
            "opensearch": ("disabled" if not os_enabled
                           else ("indexed" if fid in os_ids else "pending")),
        }
    return files


def delete_file(owner: str, file_id: str) -> bool:
    row = _delete_row(owner, file_id)
    if not row:
        return False
    vs_id = get_store_id(owner)
    dp = make_inference_client(with_project=True)
    try:
        if vs_id:
            dp.vector_stores.files.delete(vector_store_id=vs_id, file_id=row["oci_file_id"])
    except Exception:
        logger.exception("vector store file delete failed (ignored)")
    try:
        dp.files.delete(row["oci_file_id"])
    except Exception:
        logger.exception("file delete failed (ignored)")
    _delete_original(owner, file_id, row["filename"])
    try:
        from . import rag_opensearch

        if rag_opensearch.enabled():
            rag_opensearch.delete_file(owner, file_id)
    except Exception:
        logger.exception("opensearch delete failed (ignored)")
    return True
