"""RAGファイル管理(RAG-01)。ユーザーごとのVector Store + ADBで状態管理。

SPIKE-03実機確定事項に準拠:
- ストア本体CRUD=CPクライアント、files系=DPクライアント(OpenAi-Project必須)
- ファイル単位で取り込み(バッチは1失敗で全体400)。docx非対応
- CP completed後のDP伝播待ちが必要
"""

import hashlib
import logging
import time
import uuid
from typing import Any

import oracledb
from openai import NotFoundError

from . import extract_xlsx, rag_metadata
from .db import connect
from .genai import make_cp_client, make_inference_client, resolve_project_ocid
from .settings import get_settings

logger = logging.getLogger("jetuse.rag")

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", extract_xlsx.XLSX_EXT}
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


def _fit(text: str, limit: int = 400) -> str:
    """VARCHAR2(400) へ収まる長さに**バイト境界で**切る(拡張子は残す)。

    文字数で切ると、日本語ファイル名(1 文字 3 バイト)は BYTE セマンティクスの列で
    ORA-12899 になる(実運用で長い名前を上げた瞬間にアップロードが落ちる)。
    末尾だけを落とすと**拡張子が消える**ので、形式で分岐する判定(バックエンドが
    その形式を読めるか — `_select_ai_supports`)が長い名前で誤る。拡張子は残す。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    stem, dot, ext = text.rpartition(".")
    suffix = f".{ext}" if dot and len(ext.encode("utf-8")) <= 16 else ""
    keep = limit - len(suffix.encode("utf-8"))
    head = (stem if dot else text).encode("utf-8")[:keep]
    return head.decode("utf-8", errors="ignore") + suffix


def _insert_file(owner: str, file_id: str, filename: str, oci_file_id: str, size: int) -> None:
    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO rag_files(id, owner_sub, filename, oci_file_id, status, bytes)
            VALUES (:id, :o, :f, :ofi, 'processing', :b)
            """,
            id=file_id, o=owner, f=_fit(filename), ofi=oci_file_id, b=size,
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
    """台帳行を削除する。ADB自前索引(RAGM-02)のチャンクは**同一トランザクション**で消す。

    チャンクは台帳と同じADBにあるので、ここで一緒に消せば「APIは削除成功なのに
    チャンクだけ残って以後の回答に混ざる」が構造的に起きない(外部サービス側の
    Vector Store/OpenSearchは別サービスなのでbest-effortのまま)。
    """
    with connect() as conn:
        cur = conn.cursor()
        # FOR UPDATE: 取り込み中(rag_adb 側が同じ行をロックする)なら、そちらの完了を待つ。
        # 待たないと「削除は成功したのに、あとから取り込みがチャンクを commit する」が起きる。
        cur.execute(
            "SELECT oci_file_id, filename FROM rag_files WHERE id = :id AND owner_sub = :o"
            " FOR UPDATE",
            id=file_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "DELETE FROM rag_files WHERE id = :id AND owner_sub = :o", id=file_id, o=owner
        )
        from . import rag_adb

        # **可用性チェックを挟まない**。enabled() は別接続で、瞬断やプール枯渇を False に
        # 丸めるため、それを削除のスキップ条件にすると「APIは削除成功なのにチャンクが残る」
        # を再現してしまう。ここは同じcursorで必ず実行する。
        # 「チャンク表が無い(未導入)」の判定は delete_chunks 側が同じ接続で行う。
        # 例外は握り潰さない(commitしない=台帳行も残る=削除は失敗として返る)。
        rag_adb.delete_chunks(cur, owner, file_id)
        conn.commit()
        return {"oci_file_id": row[0], "filename": row[1]}


# --- Object Storage原本バックアップ(ベストエフォート) ---


def _os_client():
    import oci

    from .oci_auth import sdk_signer_args

    return oci.object_storage.ObjectStorageClient(**sdk_signer_args(get_settings().oci_region))


def _backup_original(owner: str, file_id: str, filename: str, content: bytes) -> None:
    bucket = get_settings().rag_bucket
    if not bucket:
        return
    try:
        client = _os_client()
        ns = client.get_namespace().data
        # 台帳(rag_files.filename)と**同じ正規化名**をキーに使う。ここだけ原名にすると、
        # 400 バイト超のファイル名で削除時にキーが一致せず、原本が消し残る。
        client.put_object(ns, bucket, f"rag/{owner}/{file_id}_{_fit(filename)}", content)
    except Exception:
        logger.exception("rag original backup failed (ignored)")


def _delete_original(owner: str, file_id: str, filename: str) -> None:
    bucket = get_settings().rag_bucket
    if not bucket:
        return
    try:
        client = _os_client()
        ns = client.get_namespace().data
        client.delete_object(ns, bucket, f"rag/{owner}/{file_id}_{_fit(filename)}")
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


def health_check(*, allow_autocreate: bool = True) -> dict[str, Any]:
    """RAG 経路の3点検査: ①project解決 ②CP vector_stores.list ③DP files.list(OpenAi-Project付き)。

    Issue #47 の報告者が「どこで落ちているか」を自己診断できる粒度で返す(認可済み前提)。
    allow_autocreate=False は集約health(/api/health)からの呼び出し向け(PORT-02):
    GETの読み取り専用ポーリングだけでGenerativeAiProjectを作ってしまわないようにする
    (/api/rag/health は既定のallow_autocreate=True=従来どおりの挙動を維持)。
    """
    checks: dict[str, dict[str, Any]] = {}
    project: str | None = None
    try:
        project = resolve_project_ocid(allow_autocreate=allow_autocreate)
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


def build_attributes(
    filename: str, content: bytes, attributes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Vector Store へ渡すメタデータ属性を組み立てる(RAGM-01 / ADR-0020 §1)。

    `file`(元のファイル名)と `sha256`(本文のハッシュ)は指定が無ければこちらで補う。
    呼び出し側が明示した値が優先。検証(未知キー・上限・空値の除去)は rag_metadata が担う。
    """
    base = {"file": filename, "sha256": hashlib.sha256(content).hexdigest()}
    return rag_metadata.normalize_attributes({**base, **(attributes or {})})


def prepare_upload(filename: str, content: bytes) -> tuple[str, bytes, dict[str, str]]:
    """マネージド Vector Store へ渡す `(ファイル名, 本文, ファイル単位の属性)`(PREP-01)。

    xlsx だけ変換する。マネージド側は Office 形式を受け付けない(SPIKE-03: docx は
    `Unsupported file type`。xlsx も同じであることは PREP-01 の E2E で実測した)ので、
    抽出したテキストを渡す。

    **属性はファイル単位にしかできない**(SPIKE-M1 ①-a: 1 ファイルが複数チャンクに割れても
    属性は 1 種類)。したがって返す `sheet` / `cells` は「そのファイル全体」を表す値であり、
    チャンクごとのセル範囲ではない。セル単位の出典が要るなら `adb` バックエンドを使う
    (この能力差が ADR-0020 の決定内容。1 チャンク = 1 ファイルに割って
    「セル単位で返る」ように見せる細工はしない)。
    """
    if not extract_xlsx.is_xlsx(filename):
        return filename, content, {}
    chunks = extract_xlsx.extract(filename, content)
    if not chunks:
        raise extract_xlsx.EmptyWorkbook(
            "シートから本文を抽出できませんでした(空のブック)"
        )
    return (f"{filename}.txt", extract_xlsx.render_text(chunks).encode("utf-8"),
            extract_xlsx.file_attributes(chunks))


def add_file(
    owner: str, filename: str, content: bytes, attributes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Files APIへアップロードしVector Storeへ登録(status=processingで返す)。

    attributes は出典の構造化と版フィルタのためのメタデータ(RAGM-01)。
    不正な属性は `rag_metadata.MetadataError`(ルート側で 422)で、OCI を呼ぶ前に弾く。
    xlsx は抽出を通してから渡す(PREP-01)。上限超過は `extract_xlsx.ExtractionLimitError`
    (ルート側で 422)で、こちらも OCI を呼ぶ前に弾く。
    """
    # 台帳・原本バックアップ・sha256 は**元のファイル**のまま(変換するのは送信する本文だけ)
    upload_name, upload_bytes, derived = prepare_upload(filename, content)
    conflicting = sorted(set(derived) & set(attributes or {}))
    if conflicting:
        # 導出値を利用者指定で上書きさせない。上書きを許すと、複数シートのブックに
        # 特定のセル範囲を付けて「マネージドでもセル単位で返る」ように見せられてしまう
        # (ADR-0020 が隠すなと決めた能力差そのもの)。黙って捨てずに 422 で断る
        raise rag_metadata.MetadataError(
            f"xlsx では {', '.join(conflicting)} は抽出結果から決まります(指定できません)。"
            "チャンク単位のセル範囲が要るなら adb バックエンドを使ってください"
        )
    attrs = build_attributes(filename, content, {**(attributes or {}), **derived})
    vs_id = ensure_store(owner)
    file_id = _uid()
    _backup_original(owner, file_id, filename, content)
    dp = make_inference_client(with_project=True)
    f = dp.files.create(file=(upload_name, upload_bytes), purpose="assistants")
    # CP completed直後はDP側にstoreが未伝播で404になる(SPIKE-03)。デモは箱ごとに新規store
    # なので初回uploadが通常経路 — 有界リトライで吸収する(SP1-03 REV-005)。
    for attempt in range(6):
        try:
            dp.vector_stores.files.create(
                vector_store_id=vs_id, file_id=f.id, attributes=attrs
            )
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
    # ADB自前索引(RAGM-02)にも取り込む(表がある環境のみ)。
    # 失敗しても他バックエンドの取り込みは成立しているのでアップロード自体は失敗させない。
    # 取り込めなかったファイルは backends.adb が "error" になるので画面から分かる
    # (再取り込みは同じファイルを上げ直す = 版が上がる。自動リトライは未実装 — RAGM-02 の残課題)。
    try:
        from . import rag_adb

        state = rag_adb.availability()
        if state == rag_adb.READY:
            # `kind` は**両バックエンドで同じ値**にする。ここを既定値のままにすると、
            # 同じファイルがマネージド側では kind='spec'、ADB 側では kind='doc' になり、
            # 分類での絞り込みがバックエンドを変えた瞬間に結果を変える(review-2 PREP01-004)
            rag_adb.ingest(owner, file_id, filename, content,
                           **({"kind": str(attrs["kind"])} if "kind" in attrs else {}))
        elif state == rag_adb.UNAVAILABLE:
            # 「表が無い(未導入)」と「今つながらない」を区別する。後者を黙って飛ばすと、
            # 復旧後もそのファイルだけ取り込まれないまま誰も気づけない。
            rag_adb.mark_unavailable(owner, file_id, filename)
    except Exception:
        logger.exception("adb ingest failed (ignored)")
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

# Select AI が原本から読める形式(RAG-03 / SPIKE-08 の構成で索引に載る形式)。
# xlsx は入らない — 索引はバケットの原本を DB 側が読むので、PREP-01 の抽出を通らない。
SELECT_AI_EXTENSIONS = {".pdf", ".txt", ".md"}


def _select_ai_supports(filename: str) -> bool:
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in SELECT_AI_EXTENSIONS)


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
    """各ファイルにバックエンドの取り込み状況を付与する(ENH-05 可視化)。

    backends[*] = "indexed" | "pending" | "error" | "disabled"
    - vector_store: Files API/Vector Storeの処理状態
    - select_ai: ベクトル索引($VECTAB)に存在するか(refresh_rate間隔で同期=反映が遅い)。
      **原本をDB側(DBMS_CLOUD_AI)が読む方式**なので、アプリ側の抽出(PREP-01)を通らない。
      Select AI が読めない形式(xlsx)は永久に索引へ現れないため、"pending" ではなく
      "error" として出す(いつか入る、という嘘の期待を作らない — review-2 PREP01-002)
    - opensearch: indexに存在するか(取り込みは同期=即時)。無効時は disabled
    - adb: 自前チャンク表に存在するか(取り込みは同期=即時)。取り込みに失敗した/本文を
      取り出せなかったファイルは error。表が無い環境は disabled(RAGM-02)
    """
    sai_ids: set[str] = set()
    os_ids: set[str] = set()
    adb_ids: set[str] = set()
    adb_errors: set[str] = set()
    os_enabled = False
    adb_enabled = False
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
    try:
        from . import rag_adb

        adb_enabled = rag_adb.enabled()
        if adb_enabled:
            adb_ids = rag_adb.indexed_file_ids(owner)
            adb_errors = rag_adb.errored_file_ids(owner)
    except Exception:
        logger.exception("adb status failed (ignored)")

    for f in files:
        fid = f["id"]
        f["backends"] = {
            "vector_store": _VS_MAP.get(f.get("status", ""), "pending"),
            "select_ai": ("indexed" if fid in sai_ids
                          else "pending" if _select_ai_supports(f.get("filename", ""))
                          else "error"),
            "opensearch": ("disabled" if not os_enabled
                           else ("indexed" if fid in os_ids else "pending")),
            "adb": ("disabled" if not adb_enabled
                    else "indexed" if fid in adb_ids
                    else "error" if fid in adb_errors else "pending"),
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
