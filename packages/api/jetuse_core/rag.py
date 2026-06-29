"""RAGファイル管理(RAG-01)。ユーザーごとのVector Store + ADBで状態管理。

SPIKE-03実機確定事項に準拠:
- ストア本体CRUD=CPクライアント、files系=DPクライアント(OpenAi-Project必須)
- ファイル単位で取り込み(バッチは1失敗で全体400)。docx非対応
- CP completed後のDP伝播待ちが必要
"""

import logging
import math
import os
import time
import uuid
from typing import Any

import openai
import oracledb

from .db import connect
from .genai import make_cp_client, make_inference_client
from .models import DEFAULT_MODEL, MODELS
from .settings import get_settings

logger = logging.getLogger("jetuse.rag")

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
MAX_BYTES = 20 * 1024 * 1024

# file_search で 1 回に返す検索ヒットの上限(過大要求を縛る fail-closed 境界)。
MAX_SEARCH_TOP_K = 50

# 実機(ap-osaka-1 / gpt-oss-120b + file_search)で確定した委譲の作法(BE-04 E2E):
#   - `instructions` パラメータや長い和文の指示プリアンブルを input に足すと file_search 併用時に
#     500(internal_error)を誘発する → 指示は付けず query をそのまま渡す。
#   - tool_choice=auto だとモデルが検索をスキップして空ヒットになりやすい → 検索専用エンドポイント
#     なので `tool_choice="required"` で file_search を必ず実行させる(決定的に取得する)。


class RagSearchError(RuntimeError):
    """テナント RAG 検索の委譲(OCI Responses file_search)が失敗したことを表す。"""


class ResponseShapeError(RagSearchError):
    """上流 Responses の出力が想定スキーマと異なる(欠落・非 list・非構造 item 等)。

    RagSearchError の派生なので、search() の例外境界で 502 に正規化される(空の正常結果に倒さない)。
    """


class StoreVerificationError(RuntimeError):
    """登録対象のベクトルストアがテナント(Project)に存在/アクセスできないことを表す。

    register_tenant_store の前段でテナント固定クライアントからストアを retrieve して検証し、
    失敗時はこの例外を投げる(DB を更新しない)。ルートは 400 に写像する(BE04-R5-003)。
    """


class StoreConflictError(RuntimeError):
    """登録対象のストアが**別テナント**に既に登録済みであることを表す(越境防止 / BE04-001)。

    vector_store_id は登録簿で UNIQUE。別テナントへ既登録のストアを登録すると一意制約違反になり、
    この例外へ写像する。ルートは 409 に写像する(1 ストア = 高々 1 テナントの一次境界を守る)。
    """


class StoreUpstreamError(RuntimeError):
    """ストア検証時の**一過性の上流障害**(接続失敗・タイムアウト・認証/設定不備・5xx)を表す。

    利用者の入力不正(不存在 id = 400)とは区別し、再試行可能な障害としてルートは 502 に写像する
    (BE04-008: 一過性障害を恒久的な入力エラー 400 に倒さない)。
    """


def _uid() -> str:
    return str(uuid.uuid4())


# --- ADBリポジトリ ---


def get_store_id(owner: str) -> str | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT vector_store_id FROM rag_stores WHERE owner_sub = :o", o=owner)
        row = cur.fetchone()
        return row[0] if row else None


def _save_store_id(owner: str, vs_id: str) -> None:
    with connect() as conn:
        conn.cursor().execute(
            "INSERT INTO rag_stores(owner_sub, vector_store_id) VALUES (:o, :v)",
            o=owner, v=vs_id,
        )
        conn.commit()


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
    _save_store_id(owner, vs.id)
    return vs.id


def add_file(owner: str, filename: str, content: bytes) -> dict[str, Any]:
    """Files APIへアップロードしVector Storeへ登録(status=processingで返す)"""
    vs_id = ensure_store(owner)
    file_id = _uid()
    _backup_original(owner, file_id, filename, content)
    dp = make_inference_client(with_project=True)
    f = dp.files.create(file=(filename, content), purpose="assistants")
    dp.vector_stores.files.create(vector_store_id=vs_id, file_id=f.id)
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


def _extract_search_results(
    response: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Responses 出力から検索ヒット・引用・回答テキストを取り出す(chat の引用抽出と同方針)。

    - hits: `file_search_call.results`(取り込み済みチャンク)。file_id ごとに最高スコアへ畳む。
    - citations: message.annotations が指す file_id(回答が実際に引用した出典)。
    - answer: output_text(根拠付きの回答本文)。

    上流スキーマ変更・壊れた応答を **空の正常結果に倒さない**(fail-closed)。`output` が list でない
    (欠落・文字列・dict 等)、item が非構造、または `tool_choice="required"` で必ず実行されるはずの
    `file_search_call` が出力に無い場合は `ResponseShapeError` を送出し、呼び出し側(search)で
    `RagSearchError`→502 に正規化する(BE-04 review BE04-008 / BE04-R5-005)。
    正常なゼロ件は `file_search_call.results == []`(空 list)**のみ**を受理する。
    """
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        raise ResponseShapeError(f"output が list でない: {type(output).__name__}")
    by_file: dict[str, dict[str, Any]] = {}
    cited: dict[str, dict[str, Any]] = {}
    answer_parts: list[str] = []
    saw_file_search_call = False
    for item in output:
        itype = getattr(item, "type", None)
        if itype is None:
            # 文字列/数値などの非構造 item = 想定外スキーマ。空に倒さず fail-closed。
            raise ResponseShapeError(f"output item が非構造: {type(item).__name__}")
        if itype == "file_search_call":
            saw_file_search_call = True
            results = getattr(item, "results", None)
            # tool_choice="required" かつ include=[file_search_call.results] を要求しているので、
            # results は必ず list で返る。None/欠落/非 list は上流スキーマ破損として fail-closed
            # (None を空ヒットの正常に倒さない。正常ゼロ件は results==[] のみ。BE04-R5-005)。
            if not isinstance(results, list):
                raise ResponseShapeError(
                    f"file_search_call.results が list でない: {type(results).__name__}"
                )
            for r in results:
                fid = getattr(r, "file_id", None)
                score = getattr(r, "score", None)
                # 非空 file_id と有限数値 score を要求(偽ヒット・非数値・NaN/Inf を弾く)。
                if not (isinstance(fid, str) and fid):
                    raise ResponseShapeError("file_search result の file_id が不正")
                # bool は int のサブクラスなので明示的に除外し、NaN/Inf は math.isfinite で弾く
                # (NaN/Inf が後段 JSON 直列化で 500 になるのを防ぐ。BE-04 review BE04-R5-005)。
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    raise ResponseShapeError("file_search result の score が非数値")
                if not math.isfinite(score):
                    raise ResponseShapeError("file_search result の score が非有限(NaN/Inf)")
                cur = by_file.get(fid)
                # 比較・ソートは**未丸めスコア**で行う(丸め済みと未丸めの混在比較は近接スコアで
                # 取り違える。BE04-006)。丸めは応答組み立て時(末尾)にのみ行う。
                if not cur or score > cur["score"]:
                    by_file[fid] = {
                        "file_id": fid,
                        "filename": getattr(r, "filename", ""),
                        "score": score,
                        "text": getattr(r, "text", None) or "",
                    }
        elif itype == "message":
            content = getattr(item, "content", None)
            if content is not None and not isinstance(content, list):
                raise ResponseShapeError("message.content が list でない")
            for part in content or []:
                ptext = getattr(part, "text", None)
                if ptext:
                    answer_parts.append(ptext)
                annotations = getattr(part, "annotations", None)
                if annotations is not None and not isinstance(annotations, list):
                    raise ResponseShapeError("message.annotations が list でない")
                for a in annotations or []:
                    fid = getattr(a, "file_id", None)
                    if isinstance(fid, str) and fid and fid not in cited:
                        # スコアは末尾で by_file(未丸め)から解決して丸める(順序非依存にする)。
                        cited[fid] = {
                            "file_id": fid,
                            "filename": getattr(a, "filename", ""),
                        }
    # tool_choice="required" で file_search は必ず実行されるはず。output=[] や message のみで
    # file_search_call が一切無い応答は「検索がスキップされた/壊れた」上流であり、空ヒットの正常に
    # 倒さず fail-closed(BE-04 review BE04-R5-005)。
    if not saw_file_search_call:
        raise ResponseShapeError("file_search_call が output に存在しない(検索未実行)")
    answer = "".join(answer_parts).strip()
    hit_ids = set(by_file)
    # 引用は必ずヒット集合の部分集合(ヒットに無い file_id を引用する応答は上流破損 → fail-closed)。
    for fid in cited:
        if fid not in hit_ids:
            raise ResponseShapeError(f"引用 file_id がヒットに無い: {fid}")
    # 「ヒット＋引用＋根拠付き回答」契約: ヒットがあるなら回答非空かつ引用 1 件以上を要求する
    # (ヒットだけで回答/引用が欠けた応答を 200 に倒さない。BE04-002)。
    if hit_ids and (not answer or not cited):
        raise ResponseShapeError("ヒットがあるのに回答または引用が欠落(根拠付き回答契約の違反)")
    # 比較は未丸めで行い、応答は最後にだけ 3 桁へ丸める(BE04-006)。
    hits = []
    for c in sorted(by_file.values(), key=lambda c: -c["score"]):
        hits.append({**c, "score": round(c["score"], 3)})
    citations = [
        {**c, "score": round(by_file[c["file_id"]]["score"], 3)} for c in cited.values()
    ]
    return hits, citations, answer


# --- Platform テナント RAG ストア登録簿(BE-04 / migration 025 / ADR-0019) ---------------
#
# 既存 rag_stores は **ユーザ(owner_sub=OIDC sub)単位**(rag.add_file が user.subject で作成)。
# Platform 経路はテナント(Project OCID)境界を強制するため、**テナント単位の別系統登録簿**
# platform_rag_stores を正本にする(キーの取り違えを避ける。BE-04 review BE04-001)。


def get_tenant_store_id(tenant: str) -> str | None:
    """テナント(Project OCID)が所有するベクトルストア id を登録簿から解決する(無ければ None)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT vector_store_id FROM platform_rag_stores WHERE tenant = :t",
            t=tenant,
        )
        row = cur.fetchone()
        return row[0] if row else None


def verify_tenant_store_access(tenant: str, vector_store_id: str) -> None:
    """登録前に、ストアが実在し本体からアクセス可能かを検証する(fail-closed)。

    ベクトルストアの retrieve は **CP(本体 CRUD)エンドポイント**のみが提供する
    (推論側は `vector_stores.retrieve` を 404 にする — 実機確定 ap-osaka-1)。よって
    `make_cp_client` で retrieve し、存在しない id・タイプミス・到達不能(=誤登録による
    恒久 502 の芽)を弾く(BE04-R5-003)。失敗時は `StoreVerificationError` を投げ DB を更新しない。

    注(Project 所属の限界): 当該テナンシの OpenAI 互換層では retrieve / file_search とも
    Project 単位の厳密な所属検証を保証しない(別 Project からも到達し得る)。よって越境の主境界は
    **authorize の tenant 一致＋登録簿解決(呼び出し元は store id を渡さない)**で、Project 分離は
    ADR-0019 の通り best-effort の第二境界。本検証は実在・到達可能性を担保し誤登録を防ぐのが主目的。

    障害の分類(BE04-008): 404(不存在 = 利用者の入力不正)は `StoreVerificationError`→400。
    接続失敗・タイムアウト・認証/設定不備・5xx などの**一過性の上流障害**は `StoreUpstreamError`→502
    として区別する(再試行可能な障害を恒久的な 400 に倒さない)。
    """
    try:
        client = make_cp_client()
        vs = client.vector_stores.retrieve(vector_store_id=vector_store_id)
    except openai.NotFoundError as e:
        # 404 = 当該ストアが実在しない/このプリンシパルから見えない = 利用者の入力不正(400)。
        logger.warning("tenant store verify: not found (tenant=%s): %s", tenant, e)
        raise StoreVerificationError("vector store not found") from e
    except Exception as e:
        # 接続/タイムアウト/認証/設定/5xx = 一過性の上流障害(502)。入力不正と混同しない。
        logger.warning("tenant store verify: upstream error (tenant=%s): %s", tenant, e)
        raise StoreUpstreamError("vector store verification upstream error") from e
    # fail-closed: retrieve 応答の id が**非空でかつ要求 id と完全一致**する場合のみ実在確認とする
    # (id 欠落・None・空文字・別 id は上流スキーマ破損として拒否。BE04-009)。
    got = getattr(vs, "id", None)
    if not (isinstance(got, str) and got == vector_store_id):
        raise StoreVerificationError("vector store id missing or mismatched on verify")


def register_tenant_store(tenant: str, vector_store_id: str, *, verify: bool = True) -> None:
    """テナント→ベクトルストアの所有を登録簿へ upsert する(取込側からの登録の正本)。

    検索(get_tenant_store_id)の対向。テナントへの文書取込パイプライン(別タスク)がストアを作って
    ここで紐付ける。同一テナントの再登録は updated_at を更新して冪等。`verify=True`(既定)では DB
    更新の前に `verify_tenant_store_access` で存在・到達性を検証する(誤登録防止。BE04-R5-003)。

    vector_store_id は UNIQUE。**別テナントへ既登録**のストアを登録すると一意制約違反になり、
    `StoreConflictError` を送出する(1 ストア = 高々 1 テナント。越境防止の一次境界。BE04-001)。
    """
    if verify:
        verify_tenant_store_access(tenant, vector_store_id)
    try:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                MERGE INTO platform_rag_stores d
                USING (SELECT :t AS tenant FROM dual) s ON (d.tenant = s.tenant)
                WHEN MATCHED THEN
                    UPDATE SET vector_store_id = :v, updated_at = SYSTIMESTAMP
                WHEN NOT MATCHED THEN
                    INSERT (tenant, vector_store_id) VALUES (:t, :v)
                """,
                t=tenant,
                v=vector_store_id,
            )
            conn.commit()
    except oracledb.IntegrityError as e:
        # ORA-00001 は vector_store_id の UNIQUE 違反だけでなく tenant 主キー競合(未登録テナントへの
        # 並行 MERGE)でも起こる。制約名でなく**競合後の再読込**で冪等性を判定する(BE04-010):
        # 再読込で「このテナント = この store」なら並行登録の冪等成功。違えば別テナント保有 → 409。
        (err,) = e.args
        if getattr(err, "code", None) != 1:
            raise
        if get_tenant_store_id(tenant) == vector_store_id:
            return  # 冪等(並行 upsert / 主キー競合でも最終状態は要求と一致)
        raise StoreConflictError(
            "vector store is already registered to another tenant"
        ) from e


def search(tenant: str, query: str, *, top_k: int = 5) -> dict[str, Any]:
    """テナント所有ベクトルストアへのセマンティック検索(OCI Responses file_search 委譲)。

    呼び出し元はストア id を渡さない。broker 検証済みテナント(Project OCID)から**本体が**
    `get_tenant_store_id` でストアを解決するため、テナント境界を越えた検索は構造的に起こらない
    (秘密=vector_store_id は本体のみ保持し、応答にも含めない)。テナントにストアが無ければ
    空ヒットを返す(データ未取込であって異常ではない)。委譲(クライアント生成〜応答抽出)の
    あらゆる失敗は `RagSearchError` に正規化する(fail-closed: DB 解決の失敗とは別の例外境界)。
    """
    k = max(1, min(int(top_k), MAX_SEARCH_TOP_K))
    # DB レジストリ解決はこの try の外(DB 障害は委譲失敗=502 ではなく
    # グローバル handler の 503 へ委ねる)。
    store_id = get_tenant_store_id(tenant)
    if not store_id:
        return {"hits": [], "citations": [], "answer": "", "store_present": False}
    model = MODELS[DEFAULT_MODEL]
    # クライアント生成・responses.create・応答抽出を**同一の例外境界**に入れる。署名器/設定/
    # クライアント初期化の失敗や、変更/不正な上流レスポンスの抽出失敗も含め委譲失敗として
    # 502 へ写像する(BE-04 review BE04-003)。
    try:
        # tenant(ADR-0014 のテナント境界)は GenAI Project OCID。OpenAi-Project をテナントに固定し、
        # Project 単位の状態リソース分離に検索を閉じる(別 Project のストアへ到達しない＝二重境界の
        # 一方を OCI 側でも担保する。BE-04 review BE04-007)。
        client = make_inference_client(with_project=True, project_ocid=tenant)
        response = client.responses.create(
            model=model.oci_id,
            input=query,
            tools=[
                {
                    "type": "file_search",
                    "vector_store_ids": [store_id],
                    "max_num_results": k,
                }
            ],
            include=["file_search_call.results"],
            tool_choice="required",
            temperature=0,
            store=False,
        )
        hits, citations, answer = _extract_search_results(response)
    except Exception as e:  # 委譲先の失敗は曖昧に 200 へ倒さず fail-closed
        logger.exception("rag search delegation failed")
        raise RagSearchError(str(e)) from e
    citations = resolve_citation_filenames(tenant, citations)
    return {
        "hits": hits,
        "citations": citations,
        "answer": answer,
        "store_present": True,
    }


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
