"""RAGファイル管理(RAG-01 → SP2-02 で予約 ledger + exact 外部名 + 外部先行削除)。

SPIKE-03実機確定事項に準拠:
- ストア本体CRUD=CPクライアント、files系=DPクライアント(OpenAi-Project必須)
- ファイル単位で取り込み(バッチは1失敗で全体400)。docx非対応
- CP completed後のDP伝播待ちが必要

SP2-02(specs/18 §3.1・§3.2):
- 外部名の正本は owner_keys.file_key(owner_key, reservation_id, ext)。
  OCI Files filename = "<sha1(owner)>/<rid>.<ext>"、原本 = "rag/<sha1(owner)>/<rid>.<ext>"。
  rag_files.id = reservation_id(単一 ID で ledger・DB 行・外部名・$VECTAB を突合)。
- upload は 予約(ledger pending) → 原本 put(Select AI 有効構成では必須のリトライ可能
  ステップ) → files.create → attach → rag_files INSERT + ledger confirmed(同一Tx)。
- ensure_store は作成前に CP 一覧(ページネーション完走)から metadata.owner =
  sha1(owner) 40hex の孤児を採用(最古の usable を正本、余剰は削除 — SP2-00 M005)。
- delete_file は外部先行(NotFound=成功、他失敗は行とカウンタを保持して 503)。
  Select AI $VECTAB への反映は同期(削除後に不存在確認)。
- demo namespace への書き込みは demo 単位リース保持が前提(specs/18 §3.2.1)。

RAGM-01 / RAGM-02(ADR-0020):
- add_file は Vector Store へ属性(attributes)を付けて登録する。外部 filename は SP2-02 の
  不透明キーなので、**原名が外部に残るのは attributes["file"] だけ**(両者は補完関係)。
- ADB 自前索引(rag_adb)へも取り込み、削除は台帳行と**同一トランザクション**で消す。
"""

import hashlib
import logging
import time
import uuid
from typing import Any

import oracledb
from openai import NotFoundError

from . import demo_targets, extract_scan, extract_xlsx, rag_ledger, rag_metadata
from .db import connect
from .demo_lease import DemoLease, require_lease_for
from .genai import make_cp_client, make_inference_client, resolve_project_ocid
from .owner_keys import (
    file_key,
    is_demo_namespace,
    normalize_ext,
    original_object_name,
    original_prefix,
    owner_hash,
    owner_key_gate,
)

# 例外クラスは名前で直接 import(isinstance / raise が rag_ledger モジュール差し替えに
# 依存しないように — テストが rag.rag_ledger を fake に置換しても壊れない)
from .rag_ledger import QuotaExceededError, UnmanagedFilesError
from .settings import get_settings

logger = logging.getLogger("jetuse.rag")

# 画像(png/jpg/jpeg)は OCR を通して取り込む(PREP-03)。画面側の選択肢
# `packages/web/src/pages/rag/uploadFormats.ts` と揃える(狭いと画面から試せない)。
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", extract_xlsx.XLSX_EXT} | extract_scan.IMAGE_EXTENSIONS
MAX_BYTES = 20 * 1024 * 1024
MAX_FILENAME_CHARS = 400  # rag_files.filename / ledger.filename は VARCHAR2(400 CHAR)


class StoreNotReadyError(Exception):
    """Vector Storeが使える状態にない(DP伝播リトライ枯渇・登録簿競合の異常)。
    ルート側で503に正規化する(SP1-03 REV-007)。"""


class BoxLimitExceededError(Exception):
    """デモ箱あたりのファイル数上限超過(specs/18 §3.1 — ルート側で 422)。"""


class ExternalDeleteError(Exception):
    """外部削除の NotFound 以外の失敗(行とカウンタを保持して 503 — 再試行で収束)。"""


def _uid() -> str:
    return str(uuid.uuid4())


# --- ADBリポジトリ ---


def get_store_id(owner: str) -> str | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT vector_store_id FROM rag_stores WHERE owner_sub = :o", o=owner)
        row = cur.fetchone()
        return row[0] if row else None


def resolve_store_for_read(owner: str) -> str | None:
    """チャット/エージェントの RAG 検索が使う Vector Store 解決。owner_key 移行ゲートを通して
    から解決する(add_file/delete_file/一覧と同じ fail-closed 一貫性 — codex review-12 B003)。

    未分類の予約接頭辞 legacy 行が残る移行窓では、同名 demo 経路からの越境参照を防ぐために
    503 で止める(正常配備ではマーカー記録済み=no-op)。後始末(demo_cleanup)は legacy 行が
    残っていても走らねばならないため get_store_id を直接使う(ゲート不要)。"""
    owner_key_gate()
    return get_store_id(owner)


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
    末尾だけを落とすと**拡張子が消える**ので、形式で分岐する判定(xlsx を抽出へ回すか —
    `extract_xlsx.is_xlsx`)が長い名前で誤る。拡張子は残す。
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    stem, dot, ext = text.rpartition(".")
    suffix = f".{ext}" if dot and len(ext.encode("utf-8")) <= 16 else ""
    keep = limit - len(suffix.encode("utf-8"))
    head = (stem if dot else text).encode("utf-8")[:keep]
    return head.decode("utf-8", errors="ignore") + suffix


def _insert_file_confirmed(owner: str, file_id: str, filename: str,
                           oci_file_id: str, size: int) -> None:
    """rag_files INSERT + ledger confirmed を同一 DB トランザクションで確定する
    (confirmed = DB 登録済みの意味 — specs/18 §3.1)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO rag_files(id, owner_sub, filename, oci_file_id, status, bytes)
            VALUES (:id, :o, :f, :ofi, 'processing', :b)
            """,
            # 切り詰めはバイト境界(_fit)。文字数切りだと BYTE セマンティクスの列で
            # ORA-12899 になる(RAGM-01)。長さの入口検証は add_file の MAX_FILENAME_CHARS。
            id=file_id, o=owner, f=_fit(filename), ofi=oci_file_id, b=size,
        )
        rag_ledger.confirm_in_tx(cur, file_id)
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


# --- Object Storage原本(Select AI 有効構成では vector index の唯一のデータ源) ---
#
# 注: RAGM-02 の `_delete_row()` はここにあったが、SP2-02 が台帳行の削除を delete_file の
# 締めのトランザクション(rag_files + rag_file_ledger を同一 Tx で確定)へ移したため統合した。
# ADB チャンクの同一トランザクション削除は delete_file / demo_cleanup 側に引き継いである。


def _os_client(region: str | None = None):
    import oci

    from .oci_auth import sdk_signer_args

    eff_region = region or get_settings().oci_region
    args = sdk_signer_args(eff_region)
    # settings.oci_region を region の既定にする(RP 経路と一貫)。write-ahead locator は
    # settings.oci_region を記録するため、OS クライアントもそれに揃える(別リージョン配備/
    # Chicago fallback で config ファイルの region と食い違っても locator と整合)。
    args["config"]["region"] = eff_region
    return oci.object_storage.ObjectStorageClient(**args)


_versioning_checked: set[str] = set()


def _resolve_os_namespace(client) -> str:
    """PUT・個別/箱 DELETE・locator が必ず同一の namespace を使うための単一解決。

    config 値を優先し、未設定のときだけ実 namespace(get_namespace)。PUT が get_namespace() を、
    削除/locator が設定値を使うと、設定値の drift/空で別 namespace を走査して原本を孤児化する
    (review-14 B002)。current_locator は予約 Tx 内(FOR UPDATE 保持)で API を呼べないため設定値を
    保存し、ここでの同一フォールバックで PUT と一致させる。
    """
    return get_settings().os_namespace or client.get_namespace().data


def _assert_bucket_not_versioned(client, ns: str, bucket: str) -> None:
    """原本バケットは versioning=Disabled 必須。Enabled/Suspended だと delete_object が旧 version を
    残し、個別 DELETE / 箱 DELETE 後も原本が回収不能に残る(review-14 B001)。プロセス内で1回だけ
    確認して fail-closed(以後はキャッシュ)。"""
    key = f"{ns}/{bucket}"
    if key in _versioning_checked:
        return
    v = client.get_bucket(ns, bucket).data.versioning
    if v != "Disabled":
        raise UnmanagedFilesError(
            f"rag bucket versioning must be Disabled (got {v}); "
            "delete_object would orphan old versions")
    _versioning_checked.add(key)


def _put_original(owner: str, rid: str, ext: str, content: bytes) -> None:
    """原本 put。Select AI 有効構成(rag_bucket 設定時)では必須のリトライ可能ステップ:
    失敗時は upload を成功にせず、呼び出し側が収束削除して失敗応答にする(specs/18 §3.1)。"""
    s = get_settings()
    if not s.rag_bucket:
        return
    client = _os_client()
    ns = _resolve_os_namespace(client)  # 削除/locator と同一解決(B002)
    _assert_bucket_not_versioned(client, ns, s.rag_bucket)  # versioning=Disabled 必須(B001)
    client.put_object(ns, s.rag_bucket, original_object_name(owner, rid, ext), content)


def delete_original_exact(owner: str, rid: str, ext: str,
                          locator: dict | None = None) -> None:
    """原本の exact 削除(NotFound は成功扱い、他失敗は ExternalDeleteError)。

    locator 指定時はその namespace/bucket/region で削除する(台帳/ledger の write-ahead 先)。
    """
    import oci as oci_sdk

    s = get_settings()
    bucket = (locator or {}).get("bucket") or s.rag_bucket
    if not bucket:
        return
    client = _os_client((locator or {}).get("region"))
    ns = (locator or {}).get("os_namespace") or _resolve_os_namespace(client)  # PUT と同一(B002)
    try:
        client.delete_object(ns, bucket, original_object_name(owner, rid, ext))
    except oci_sdk.exceptions.ServiceError as e:
        if e.status != 404:
            raise ExternalDeleteError(f"original delete failed: {e.status}") from e


def _delete_original_legacy(owner: str, file_id: str, filename: str) -> None:
    """旧命名(rag/<owner>/<file_id>_<filename>)の原本削除。既存 user 資産の互換(404 成功)。"""
    import oci as oci_sdk

    s = get_settings()
    if not s.rag_bucket:
        return
    client = _os_client()
    ns = client.get_namespace().data
    try:
        client.delete_object(ns, s.rag_bucket, f"rag/{owner}/{file_id}_{filename}")
    except oci_sdk.exceptions.ServiceError as e:
        if e.status != 404:
            raise ExternalDeleteError(f"legacy original delete failed: {e.status}") from e


def list_original_objects(owner: str, locator: dict | None = None,
                          page_limit: int | None = None) -> list[str]:
    """原本 prefix `rag/<sha1(owner)>/` の全 object 名(ページネーション完走)。"""
    s = get_settings()
    bucket = (locator or {}).get("bucket") or s.rag_bucket
    if not bucket:
        return []
    client = _os_client((locator or {}).get("region"))
    ns = (locator or {}).get("os_namespace") or _resolve_os_namespace(client)  # PUT と同一(B002)
    names: list[str] = []
    start = None
    while True:
        kw = {"prefix": original_prefix(owner), "fields": "name"}
        if start:
            kw["start"] = start
        if page_limit:
            kw["limit"] = page_limit
        resp = client.list_objects(ns, bucket, **kw)
        names.extend(o.name for o in resp.data.objects)
        start = resp.data.next_start_with
        if not start:
            return names


def delete_objects(names: list[str], locator: dict | None = None) -> None:
    """object 名リストの削除(NotFound=成功、他失敗は伝播 — best-effort にしない)。"""
    import oci as oci_sdk

    if not names:
        return
    s = get_settings()
    bucket = (locator or {}).get("bucket") or s.rag_bucket
    client = _os_client((locator or {}).get("region"))
    ns = (locator or {}).get("os_namespace") or s.os_namespace or client.get_namespace().data
    for name in names:
        try:
            client.delete_object(ns, bucket, name)
        except oci_sdk.exceptions.ServiceError as e:
            if e.status != 404:
                raise


def bucket_versioning(locator: dict | None = None) -> str | None:
    """バケットの versioning 状態(配備 preflight / DELETE 前確認)。バケット未設定は None。"""
    s = get_settings()
    bucket = (locator or {}).get("bucket") or s.rag_bucket
    if not bucket:
        return None
    client = _os_client((locator or {}).get("region"))
    ns = (locator or {}).get("os_namespace") or s.os_namespace or client.get_namespace().data
    return client.get_bucket(ns, bucket).data.versioning


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


def _list_all_stores(cp, limit: int = 100) -> list[Any]:
    """CP vector_stores 一覧(単一ページ)。

    CP も after カーソルは信頼できない(OpenAI 互換で DP と同系)。ただしテナンシの vector
    store 上限は 10 で、CP は limit>100 を 400 で拒否するため limit=100 の単一取得で必ず全件
    入る。has_more が残った場合のみ警告する(specs/18 §3.2 の「先頭ページのみは不可」)。
    """
    page = cp.vector_stores.list(limit=limit)
    if getattr(page, "has_more", False):
        raise UnmanagedFilesError(
            f"CP vector_stores.list has_more=True at limit={limit}; store list cannot be "
            "completed (no working 'after' cursor, fail-closed)")
    return list(page.data or [])


# 実機確認(2026-07-06): OCI GenAI Files list は 'after' カーソルが前進しない(after=<last_id>
# が同一先頭ページを返す→ union が伸びない)。カーソル走査は不能なので単一の大 limit で全件
# 取得する。API は limit=10000 を受理し project 内全 File を1ページ(has_more=False)で返す。
# has_more が残った場合のみ「取りこぼしうる」と警告(現状 demo 箱×20 で単一ページに収まる)。
DP_LIST_LIMIT = 10_000


def list_all_external_files(dp=None, limit: int = DP_LIST_LIMIT) -> list[dict[str, str]]:
    """DP files.list の全件取得(単一ページ)。reconcile / demo DELETE 3c が使う。

    has_more=True は「一覧が不完全＝未走査 File が残りうる」なので fail-closed で
    UnmanagedFilesError を送出する(部分一覧のまま台帳を消すと孤児 File を残すため —
    ルートは 503。after カーソルが使えない以上 limit を上げるしか手はない)。
    """
    dp = dp or make_inference_client(with_project=True)
    page = dp.files.list(limit=limit)
    if getattr(page, "has_more", False):
        raise UnmanagedFilesError(
            f"DP files.list has_more=True at limit={limit}; OCI Files API lacks a working "
            "'after' cursor so the external file list cannot be completed (fail-closed)")
    return [{"id": f.id, "filename": getattr(f, "filename", "") or ""}
            for f in (page.data or [])]


def delete_external_file(oci_file_id: str, dp=None) -> None:
    """DP File 削除(NotFound は成功扱い)。"""
    dp = dp or make_inference_client(with_project=True)
    try:
        dp.files.delete(oci_file_id)
    except NotFoundError:
        pass


def _dp_for(locator: dict | None):
    """locator(あれば)で DP クライアントを構成する。無ければ現在設定。"""
    if locator and locator.get("region") and locator.get("compartment") \
            and locator.get("project"):
        from .genai import make_inference_client_for
        return make_inference_client_for(
            locator["region"], locator["compartment"], locator["project"])
    return make_inference_client(with_project=True)


def find_orphan_stores(owner: str, cp=None) -> list[Any]:
    """CP 一覧から metadata.owner == sha1(owner) 40hex の store を列挙する
    (登録前クラッシュの未登録 store も実在ベースで拾う — specs/18 §3.2 手順 3b)。"""
    cp = cp or make_cp_client()
    tag = owner_hash(owner)
    return [
        vs for vs in _list_all_stores(cp)
        if (getattr(vs, "metadata", None) or {}).get("owner") == tag
    ]


def ensure_store(owner: str, lease: DemoLease | None = None) -> str:
    """ユーザーのVector Storeを返す(なければ孤児採用 or 作成し、DP伝播まで待つ)。"""
    require_lease_for(owner, lease)
    vs_id = get_store_id(owner)
    if vs_id:
        return vs_id
    cp = make_cp_client()
    # 作成の前に孤児採用(specs/18 §3.2 — 作成後・登録前クラッシュの store を demo 削除なしに
    # 回収し、テナンシ上限 10 を浪費しない)。最古の usable(completed)を正本、余剰は削除。
    orphans = find_orphan_stores(owner, cp)
    usable = sorted(
        (vs for vs in orphans if getattr(vs, "status", "") == "completed"),
        key=lambda vs: getattr(vs, "created_at", 0) or 0,
    )
    if usable:
        winner = usable[0]
        for extra in orphans:
            if extra.id != winner.id:
                try:
                    cp.vector_stores.delete(vector_store_id=extra.id)
                except NotFoundError:
                    pass
        if _save_store_id(owner, winner.id):
            logger.info("adopted orphan store %s for %s", winner.id, owner[:32])
            return winner.id
        vs_id = get_store_id(owner)
        if vs_id:
            return vs_id
        raise StoreNotReadyError(f"orphan adoption race for {owner[:32]}")
    if orphans:
        # usable(completed)が無い = failed/中途状態の孤児のみ。全削除してから新規作成する
        # (残すと再試行のたびにテナンシ上限 10 を消費し続ける — codex review-2 major)。
        # 削除失敗は新規作成へ進まず 503(再試行で収束)。
        for stale in orphans:
            try:
                cp.vector_stores.delete(vector_store_id=stale.id)
            except NotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                raise StoreNotReadyError(
                    f"stale orphan store cleanup failed for {owner[:32]}: {e}"
                ) from e
    if is_demo_namespace(owner):
        # write-ahead 台帳(specs/18 §3.2): 外部書き込みの前に locator を記録
        s = get_settings()
        demo_targets.record_target(owner, "vector_store", {
            "region": s.oci_region, "compartment": s.compartment_ocid,
        })
    # 作成側も削除側(metadata exact 一致)と同じ導出値を保存する(specs/18 §3.2 手順 3b)
    vs = cp.vector_stores.create(
        name=f"jetuse-rag-{owner[:32]}", metadata={"owner": owner_hash(owner)}
    )
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
    # 同時作成で負けた(REV-008): 自分のstoreをbest-effortで片付け、勝者のstoreを使う
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

    SP2-02 で OCI Files 側の filename は不透明キー(file_key)になったため、**原名が外部に
    残るのはここで積む `file` 属性だけ**になる(出典表示はこの値を使う)。
    """
    base = {"file": filename, "sha256": hashlib.sha256(content).hexdigest()}
    return rag_metadata.normalize_attributes({**base, **(attributes or {})})


def derived_keys(filename: str, content: bytes) -> set[str]:
    """この形式で**抽出結果から決まる**属性キー(利用者は指定できない)。

    OCR も抽出も呼ばずに答える(判定に使うのはファイル名とテキスト層の有無だけ)。
    `add_file` はこれを使って、課金される抽出の**前に**属性の競合を 422 で断る。
    """
    if extract_scan.needs_ocr(filename, content):
        return {"sheet"}          # ページ範囲(`p.1-p.3`)
    if extract_xlsx.is_xlsx(filename):
        return {"sheet", "cells"}  # シート名 + セル範囲
    return set()


def prepare_upload(filename: str, content: bytes, *,
                   ocr_engine: str | None = None) -> tuple[str, bytes, dict[str, str]]:
    """マネージド Vector Store へ渡す `(ファイル名, 本文, ファイル単位の属性)`(PREP-01)。

    変換するのは 2 つだけ。マネージド側は Office 形式を受け付けない(SPIKE-03: docx は
    `Unsupported file type`。xlsx も同じであることは PREP-01 の E2E で実測した)ので、
    抽出したテキストを渡す。

    - **xlsx**: シート名 + セル範囲つきの抽出テキスト(PREP-01)。
    - **画像 / テキスト層の無い頁を含む PDF**: OCR したページ順のテキスト(PREP-03)。
      テキスト層の揃った PDF は**素通し**する(従来どおり。OCR を呼ばない = 課金しない)。

    **属性はファイル単位にしかできない**(SPIKE-M1 ①-a: 1 ファイルが複数チャンクに割れても
    属性は 1 種類)。したがって返す `sheet` / `cells` は「そのファイル全体」を表す値であり、
    チャンクごとのセル範囲ではない。セル単位の出典が要るなら `adb` バックエンドを使う
    (この能力差が ADR-0020 の決定内容。1 チャンク = 1 ファイルに割って
    「セル単位で返る」ように見せる細工はしない)。
    """
    if extract_scan.needs_ocr(filename, content):
        pages = extract_scan.page_texts(filename, content, engine=ocr_engine)
        text = extract_scan.render_text(pages)
        if not text:
            # 空の本文を送らない(送ると「取り込めたのに何も引けない」になる)
            raise extract_scan.ScanUnsupported(
                "OCR で本文を抽出できませんでした(白紙 / 判読できない画像)"
            )
        return (f"{filename}.txt", text.encode("utf-8"),
                extract_scan.file_attributes(pages))
    if not extract_xlsx.is_xlsx(filename):
        return filename, content, {}
    chunks = extract_xlsx.extract(filename, content)
    if not chunks:
        raise extract_xlsx.EmptyWorkbook(
            "シートから本文を抽出できませんでした(空のブック)"
        )
    return (f"{filename}.txt", extract_xlsx.render_text(chunks).encode("utf-8"),
            extract_xlsx.file_attributes(chunks))


def add_file(owner: str, filename: str, content: bytes,
             attributes: dict[str, Any] | None = None,
             ocr_engine: str | None = None,
             *, lease: DemoLease | None = None) -> dict[str, Any]:
    """Files APIへアップロードしVector Storeへ登録(status=processingで返す)。

    順序(specs/18 §3.1): 予約(pending) → 原本 put(必須) → files.create →
    attach → rag_files INSERT + confirmed(同一Tx)。途中失敗は収束削除して失敗応答。

    attributes は出典の構造化と版フィルタのためのメタデータ(RAGM-01)。
    不正な属性は `rag_metadata.MetadataError`(ルート側で 422)で、OCI を呼ぶ前に弾く。

    第4位置引数は RAGM-01 の `attributes`(main 側が位置で渡す)。SP2-02 の `lease` は
    **キーワード専用**にしてある — 位置引数のまま両方を並べると
    `add_file(o, n, c, lease)` が DemoLease を attributes として展開して壊れる(review-1 F-002)。

    xlsx は抽出を通してから渡す(PREP-01)。上限超過は `extract_xlsx.ExtractionLimitError`
    (ルート側で 422)で、こちらも OCI を呼ぶ前に弾く。スキャン PDF・画像は OCR を通してから
    渡す(PREP-03)。`ocr_engine` は利用者の明示指定(省略時は `extract_scan.DEFAULT_ENGINE`)。

    台帳・原本・sha256 は**元のファイル**のまま。変換するのは送信する本文だけで、送信名の
    拡張子は台帳の `upload_ext` に持つ(SYNC-02 案 A)。
    """
    owner_key_gate()
    rag_ledger.upload_gate()
    require_lease_for(owner, lease)
    if len(filename) > MAX_FILENAME_CHARS:
        raise ValueError(f"ファイル名が長すぎます(最大{MAX_FILENAME_CHARS}文字)")
    # 属性の検証は**あらゆる副作用より前**(予約・demo_targets の記録・OCI 呼び出しの前)。
    # 不正な属性で 422 になるときに、外部資産も台帳行も作らないため(RAGM-01)。
    # 導出属性との競合も**抽出より前に**弾く。あとで見ると、どうせ 422 になる要求のために
    # OCR(課金される)を先に走らせてしまう。判定に使う `derived_keys` は OCR を呼ばない(PREP-03)
    conflicting = sorted(derived_keys(filename, content) & set(attributes or {}))
    if conflicting:
        raise rag_metadata.MetadataError(
            f"この形式では {', '.join(conflicting)} は抽出結果から決まります(指定できません)。"
            "チャンク単位のセル範囲・頁が要るなら adb バックエンドを使ってください"
        )
    # 抽出の位置は**課金されるかどうか**で分ける(SYNC-02)。
    #  - OCR を要さない変換(xlsx / 素通し)は無料で副作用も無い。予約より**前**に済ませて、
    #    壊れたブックを DB も OCI も触らずに 422 で弾く(PREP-01 の受け入れ条件)。
    #  - OCR は課金される。quota 満杯で 422 になる要求のために払わないよう、予約の**後**に回す。
    needs_ocr = extract_scan.needs_ocr(filename, content)
    upload_name, upload_bytes, derived = (
        (filename, content, {}) if needs_ocr
        else prepare_upload(filename, content, ocr_engine=ocr_engine)
    )
    attrs = build_attributes(filename, content, attributes)
    if is_demo_namespace(owner):
        limit = get_settings().demo_max_rag_files
        # 箱上限は予約 ledger 行数で測る(rag_files 登録行だけだと外部削除失敗で残した
        # pending 行が数えられず、失敗+再 upload の反復で上限を超えられる — M002)
        if rag_ledger.count_for_owner(owner) >= limit:
            raise BoxLimitExceededError(f"RAGファイル数の上限({limit})に達しています")
        s = get_settings()
        demo_targets.record_target(owner, "files", {
            "region": s.oci_region, "compartment": s.compartment_ocid,
            "project": s.project_ocid,
        })
        if s.rag_bucket:
            demo_targets.record_target(owner, "objectstorage", {
                "region": s.oci_region, "os_namespace": s.os_namespace,
                "bucket": s.rag_bucket,
            })
    ext = normalize_ext(filename)
    # 予約(quota gate)を全外部作成より先に行う(M001 — quota 満杯で 422 になる前に
    # 外部 Vector Store を作ってテナンシ枠を空消費しない)。File 作成前の失敗は予約を戻す。
    rid = rag_ledger.reserve(owner, filename, ext)
    if needs_ocr:
        try:
            upload_name, upload_bytes, derived = prepare_upload(filename, content,
                                                               ocr_engine=ocr_engine)
        except Exception:
            # 外部資産はまだ無い(store も原本も作っていない)ので予約は戻して良い
            rag_ledger.release(rid)
            raise
    conflicting = sorted(set(derived) & set(attributes or {}))
    if conflicting:  # 念のための二重の網(上の derived_keys で弾けているはず)
        # 導出値を利用者指定で上書きさせない。上書きを許すと、複数シートのブックに特定の
        # セル範囲を付けて「マネージドでもセル単位で返る」ように見せられてしまう
        # (ADR-0020 が隠すなと決めた能力差そのもの)。黙って捨てずに 422 で断る
        rag_ledger.release(rid)
        raise rag_metadata.MetadataError(
            f"この形式では {', '.join(conflicting)} は抽出結果から決まります(指定できません)。"
            "チャンク単位のセル範囲・頁が要るなら adb バックエンドを使ってください"
        )
    if derived:
        attrs = build_attributes(filename, content, {**(attributes or {}), **derived})
    upload_ext = normalize_ext(upload_name)
    if upload_ext != ext:
        # 送信名は原本と拡張子が違う(xlsx / スキャン PDF → .txt)。**外部 File を作る前に**
        # 台帳へ記録しないと、set_external 前に落ちた行を reconcile が照合できない
        rag_ledger.set_upload_ext(rid, upload_ext)
    try:
        vs_id = ensure_store(owner, lease=lease)
    except Exception as e:
        # store 準備は原本 put より前 = この rid の外部成果物は未作成。予約を戻して良い。
        rag_ledger.release(rid)
        if isinstance(e, (StoreNotReadyError, UnmanagedFilesError,
                          QuotaExceededError, BoxLimitExceededError)):
            raise
        raise StoreNotReadyError(f"store setup failed (retryable): {e}") from e
    try:
        _put_original(owner, rid, ext, content)
    except Exception as e:
        # PUT はサーバ保存後に応答だけ失敗しうる(ambiguous success)。無条件に解放すると
        # 原本だけ残り台帳から辿れない孤児になる。exact 削除を試み、削除確定(成功 or 404)時
        # のみ予約を解放し、削除も不確定なら pending 予約を残して reconcile に委ねる
        # (fail-closed — B003)。
        try:
            delete_original_exact(owner, rid, ext)
        except Exception:
            logger.exception("original cleanup after put failure incomplete (keeping reservation)")
            raise UnmanagedFilesError(
                f"original put failed and cleanup incomplete ({rid}); "
                "reservation kept for reconcile (fail-closed)") from e
        rag_ledger.release(rid)
        raise StoreNotReadyError(f"original put failed (retryable): {e}") from e
    dp = make_inference_client(with_project=True)
    f = dp.files.create(file=(file_key(owner, rid, upload_ext), upload_bytes),
                        purpose="assistants")
    rag_ledger.set_external(rid, f.id)
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
                # リトライ枯渇(REV-007): 収束削除を試み、成功時のみ予約を解放する。削除に
                # 失敗したら予約(external_file_id + locator 付き)を残して reconcile に委ね、
                # quota を誤返却して孤児 File を辿れなくするのを防ぐ(fail-closed — B001)。
                cleaned = True
                try:
                    dp.files.delete(f.id)
                except NotFoundError:
                    pass
                except Exception:
                    cleaned = False
                    logger.exception("orphan file cleanup failed (keeping reservation)")
                try:
                    delete_original_exact(owner, rid, ext)
                except Exception:
                    cleaned = False
                    logger.exception("orphan original cleanup failed (keeping reservation)")
                if not cleaned:
                    raise UnmanagedFilesError(
                        f"attach exhausted and orphan cleanup incomplete ({rid}); "
                        "reservation kept for reconcile (fail-closed)") from None
                rag_ledger.release(rid)
                raise StoreNotReadyError(
                    f"vector store {vs_id} not visible on DP after bounded retries"
                ) from None
            logger.info("vector store not yet visible on DP, retrying (%s)", attempt + 1)
            time.sleep(5)
    _insert_file_confirmed(owner, rid, filename, f.id, len(content))
    # OpenSearch RAG(ENH-05)にも取り込む(有効時のみ・best-effort)
    try:
        from . import rag_opensearch

        if rag_opensearch.enabled():
            if is_demo_namespace(owner):
                demo_targets.record_target(owner, "opensearch", {
                    "endpoint": get_settings().opensearch_endpoint,
                })
            rag_opensearch.ingest(owner, rid, filename, content, lease=lease,
                                  ocr_engine=ocr_engine)
    except Exception:
        logger.exception("opensearch ingest failed (ignored)")
    # ADB自前索引(RAGM-02)にも取り込む(表がある環境のみ)。
    # 失敗しても他バックエンドの取り込みは成立しているのでアップロード自体は失敗させない。
    # 取り込めなかったファイルは backends.adb が "error" になるので画面から分かる
    # (再取り込みは同じファイルを上げ直す = 版が上がる。自動リトライは未実装 — RAGM-02 の残課題)。
    # id は SP2-02 の予約 ID(rid)。ledger・rag_files・外部名・チャンクを単一 ID で突合する。
    try:
        from . import rag_adb

        state = rag_adb.availability()
        if state == rag_adb.READY:
            # `kind` は**両バックエンドで同じ値**にする。ここを既定値のままにすると、
            # 同じファイルがマネージド側では kind='spec'、ADB 側では kind='doc' になり、
            # 分類での絞り込みがバックエンドを変えた瞬間に結果を変える(review-2 PREP01-004)。
            # 値は rag_metadata が文字列に限っているので、ここで変換はしない(RAGM-04)
            rag_adb.ingest(owner, rid, filename, content, ocr_engine=ocr_engine,
                           **({"kind": attrs["kind"]} if "kind" in attrs else {}))
        elif state == rag_adb.UNAVAILABLE:
            # 「表が無い(未導入)」と「今つながらない」を区別する。後者を黙って飛ばすと、
            # 復旧後もそのファイルだけ取り込まれないまま誰も気づけない。
            rag_adb.mark_unavailable(owner, rid, filename)
    except Exception:
        logger.exception("adb ingest failed (ignored)")
    return {"id": rid, "filename": filename, "status": "processing", "bytes": len(content)}


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
    """各ファイルにバックエンドの取り込み状況を付与する(ENH-05 可視化)。

    backends[*] = "indexed" | "pending" | "error" | "disabled"
    - vector_store: Files API/Vector Storeの処理状態
    - select_ai: ベクトル索引($VECTAB)に存在するか(refresh_rate間隔で同期=反映が遅い)。
      **原本をDB側(DBMS_CLOUD_AI)が読む方式**なので、アプリ側の抽出(PREP-01)を通らない。
      それでも xlsx は DB 側パイプライン(Oracle Text)がテキスト化して取り込む
      (PREP-02 で実測。docs/verification/PREP-02.md)。よって形式では分岐せず、
      **索引に在るかどうかだけ**で決める。未反映は同期待ちの "pending"
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
            "select_ai": "indexed" if fid in sai_ids else "pending",
            "opensearch": ("disabled" if not os_enabled
                           else ("indexed" if fid in os_ids else "pending")),
            "adb": ("disabled" if not adb_enabled
                    else "indexed" if fid in adb_ids
                    else "error" if fid in adb_errors else "pending"),
        }
    return files


def delete_file(owner: str, file_id: str) -> bool:
    """個別ファイル削除の外部先行化(specs/18 §3.2 前提改修)。

    「外部削除(NotFound=成功) → Select AI 索引の同期反映・不存在確認 → 行 DELETE →
    予約解放」の順。NotFound 以外の失敗は ExternalDeleteError(503)で行とカウンタを保持し、
    再試行で収束する。
    """
    owner_key_gate()
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT oci_file_id, filename FROM rag_files WHERE id = :id AND owner_sub = :o",
            id=file_id, o=owner,
        )
        row = cur.fetchone()
    if not row:
        return False
    oci_file_id, filename = row
    # ledger の write-ahead locator で外部クライアントを構成する(specs/18 §3.1 —
    # user 経路は demo_backend_targets を持たないため、region/project 変更後は現在設定でなく
    # 保存 locator で旧 File/原本を辿る。codex review-2 blocker)。ledger 前の legacy 行は
    # locator 無し = 現在設定にフォールバック。
    led = rag_ledger.rows_for_owner_by_id(file_id)
    locator = (led or {}).get("locator") or None
    vs_id = get_store_id(owner)
    dp = _dp_for(locator)
    try:
        if vs_id:
            try:
                dp.vector_stores.files.delete(vector_store_id=vs_id, file_id=oci_file_id)
            except NotFoundError:
                pass
        delete_external_file(oci_file_id, dp)
    except ExternalDeleteError:
        raise
    except NotFoundError:
        pass
    except Exception as e:
        raise ExternalDeleteError(f"external file delete failed: {e}") from e
    # 原本: 新旧両命名を冪等に削除(旧 = 既存 user 資産の互換。どちらも 404 成功)。
    # ext は **予約時に ledger へ記録した値が正本**(rag_ledger.reconcile と同じ)。
    # rag_files.filename は _fit() でバイト境界に切られており、400 文字のマルチバイト名では
    # 拡張子まで落ちる(`あ*397 + .md` → 拡張子なし)。そこから導くと upload が put した
    # `<rid>.md` ではなく `<rid>.bin` を消しにいき、404=成功と見なして原本を残したまま
    # 台帳行を消す(追跡不能な残存 — review-1 F-001)。ledger 前の legacy 行のみ filename から。
    ext = (led or {}).get("ext") or normalize_ext(filename)
    delete_original_exact(owner, file_id, ext, locator=locator)
    _delete_original_legacy(owner, file_id, filename)
    # OpenSearch。取り込み時の endpoint を台帳 locator から取り出し、その endpoint で消す
    # (取り込み後に endpoint を無効化/変更しても旧 index のチャンクを確実に削除する — B004)。
    # 保存 endpoint が無い legacy 行のみ現在設定へフォールバック。失敗は 503(best-effort にしない)。
    saved_os_ep = (locator or {}).get("opensearch_endpoint")
    try:
        from . import rag_opensearch

        if saved_os_ep or rag_opensearch.enabled():
            rag_opensearch.delete_file(owner, file_id, endpoint=saved_os_ep)
    except Exception as e:
        raise ExternalDeleteError(f"opensearch delete failed: {e}") from e
    # Select AI 索引($VECTAB)の同期反映(specs/18 §3.2 — 同期一択)。失敗は 503 に正規化する
    # (ExternalDeleteError 未変換だと 500 になり「再試行可」の API 契約が崩れる — 索引は残存)
    from . import rag_select_ai

    try:
        rag_select_ai.sync_remove_file(owner, file_id)
    except ExternalDeleteError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ExternalDeleteError(f"select_ai index remove failed: {e}") from e
    # ここまで全て成功 → 行 DELETE と予約解放を同一 Tx で確定する(M001 — 別 Tx にすると
    # rag_files だけ消えて ledger 行が残り、再 DELETE は 404 で終わって枠が恒久漏れする)。
    # ADB自前索引(RAGM-02)のチャンクも**この同一 Tx**で消す。チャンクは台帳と同じ ADB に
    # あるので、ここで一緒に消せば「API は削除成功なのにチャンクだけ残って以後の回答に
    # 混ざる」が構造的に起きない(外部サービスの Vector Store/OpenSearch は別サービスなので
    # 上の外部先行削除が担う)。
    from . import rag_adb

    with connect() as conn:
        cur = conn.cursor()
        # FOR UPDATE: 取り込み中(rag_adb._ingest が同じ行をロックする)なら、そちらの完了を
        # 待つ。待たないと「削除は成功したのに、あとから取り込みがチャンクを commit する」。
        cur.execute(
            "SELECT id FROM rag_files WHERE id = :id AND owner_sub = :o FOR UPDATE",
            id=file_id, o=owner,
        )
        cur.fetchone()
        # **可用性チェックを挟まない**。enabled() は別接続で、瞬断やプール枯渇を False に
        # 丸めるため、それを削除のスキップ条件にすると「API は削除成功なのにチャンクが残る」
        # を再現してしまう。ここは同じ cursor で必ず実行する。
        # 「チャンク表が無い(未導入)」の判定は delete_chunks 側が同じ接続で行う。
        # 例外は握り潰さない(commit しない = 台帳行も残る = 削除は失敗として返る)。
        rag_adb.delete_chunks(cur, owner, file_id)
        cur.execute(
            "DELETE FROM rag_files WHERE id = :id AND owner_sub = :o", id=file_id, o=owner
        )
        cur.execute("DELETE FROM rag_file_ledger WHERE id = :id", id=file_id)
        conn.commit()
    return True
