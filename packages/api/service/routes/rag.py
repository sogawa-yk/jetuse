"""RAGファイル管理ルート(RAG-01)。

*_response 関数は user 単位/デモスコープ(SP1-03)で共有する本体。ns はRAG文書の
名前空間キー(user単位= user.subject、デモスコープ= DemoContext.namespace)。
"""

import asyncio
import json
import logging
import pathlib
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from openai import APIStatusError

from jetuse_core import extract_scan, extract_xlsx, rag, rag_adb, rag_metadata
from jetuse_core.auth import AuthContext, require_user
from jetuse_core.genai import ProjectResolutionError

logger = logging.getLogger("jetuse.service")
router = APIRouter()

# FIX-47: CP/DP 由来の 4xx を 500 のまま漏らさず、原因ヒント付きで表面化する。
# レスポンスbody(OCID等を含みうる)は返さない。詳細はサーバーログと /api/rag/health で追う。
_GENAI_HINT = (
    "OCI GenAI 呼び出しが HTTP {code} で失敗しました。DG matching rule / "
    "IAM policy statements / PROJECT_OCID / リージョンの agentic API 対応を"
    "確認してください(GET /api/rag/health で自己診断できます)"
)


async def _rag_call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except rag_metadata.MetadataError as e:
        # 取り込み側で補完する属性(長すぎるファイル名由来の file 等)も 422 に正規化する。
        # 500 のまま漏らすと API 契約(不正な属性は 422)が破れる — レビュー F-002
        raise HTTPException(status_code=422, detail=str(e)) from e
    except extract_scan.OcrUnavailable as e:
        # OCR サービス側の失敗(IAM 未整備・障害)。利用者の入力の問題ではないので 422 にしない
        logger.warning("rag: ocr unavailable: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except extract_scan.ScanExtractError as e:
        # スキャン PDF / 画像の上限超過・読めない入力(PREP-03)。**切り詰めない**ので、
        # どの上限に当たったかを detail に載せて 422 で返す
        raise HTTPException(status_code=422, detail=str(e)) from e
    except (rag_adb.TooLarge, rag_adb.UnsupportedDocument) as e:
        # 抽出口(POST /api/extract)で本文を取り出せない・上限を超えた場合。
        # 取り込み経路(add_file)はこれらを内部で握って best-effort にしているので影響しない
        raise HTTPException(status_code=422, detail=str(e)) from e
    except extract_xlsx.XlsxExtractError as e:
        # xlsx の上限超過・壊れたブック(PREP-01)。**切り詰めて一部だけ取り込まない**ので、
        # どの上限に当たったかを detail に載せて 422 で返す
        raise HTTPException(status_code=422, detail=str(e)) from e
    except rag.StoreNotReadyError as e:
        raise HTTPException(
            status_code=503, detail="vector store not ready, retry later"
        ) from e
    except ProjectResolutionError as e:
        logger.warning("rag: generative-ai project unresolved: %s", e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except APIStatusError as e:
        logger.exception("rag: OCI GenAI call failed (HTTP %s)", e.status_code)
        status = 503 if e.status_code in (401, 403, 404) else 502
        raise HTTPException(
            status_code=status, detail=_GENAI_HINT.format(code=e.status_code)
        ) from e


async def list_files_response(ns: str) -> dict:
    files = await _rag_call(rag.list_files, ns)
    files = await _rag_call(rag.refresh_statuses, ns, files)
    files = await _rag_call(rag.attach_backend_status, ns, files)
    return {"files": files}


def _parse_attributes(raw: str | None) -> dict:
    """multipart の attributes フィールド(JSON文字列)を検証して返す(RAGM-01)。

    未知キー・上限超過・入れ子は 422。切り詰めない(値が変わるとフィルタが静かに外れる)。
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=422, detail=f"attributes must be a JSON object: {e}"
        ) from e
    try:
        return rag_metadata.normalize_attributes(parsed)
    except rag_metadata.MetadataError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


_ALLOWED_HINT = "/".join(sorted(e.lstrip(".") for e in rag.ALLOWED_EXTENSIONS))


def _parse_ocr_engine(raw: str | None) -> str | None:
    """OCR エンジンの明示指定(PREP-03)。未知の名前は 422(黙って既定へ落とさない)。"""
    try:
        return extract_scan.resolve_engine(raw)
    except extract_scan.ScanExtractError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    """アップロード共通の入口(拡張子・サイズ・空)。取り込みと抽出で同じ条件にする。"""
    name = pathlib.Path(file.filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in rag.ALLOWED_EXTENSIONS:
        detail = f"unsupported file type '{ext}'. allowed: {_ALLOWED_HINT}"
        if ext == ".docx":
            detail += " (docxはVector Store非対応 — SPIKE-03)"
        raise HTTPException(status_code=422, detail=detail)
    content = await file.read()
    if len(content) > rag.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 20MB)")
    if not content:
        raise HTTPException(status_code=422, detail="empty file")
    return name, content


async def upload_file_response(
    ns: str, file: UploadFile, attributes: str | None = None,
    ocr_engine: str | None = None,
) -> dict:
    attrs = _parse_attributes(attributes)  # 読み込み前に弾く(不正なら OCI を呼ばない)
    engine = _parse_ocr_engine(ocr_engine)
    name, content = await _read_upload(file)
    return await _rag_call(rag.add_file, ns, name, content, attrs, engine)


async def extract_response(file: UploadFile, ocr_engine: str | None = None) -> dict:
    """取り込まずに抽出結果だけ返す(PREP-01 層1 の公開)。

    投入されるチャンクそのもの(`adb` バックエンドが作るのと同じ `{sheet, cells, text}`)を
    返す。案件側が独自の構造化を挟みたいときの口で、**このエンドポイントは何も保存しない**。
    スキャン PDF・画像は OCR を通した本文が返り、`sheet` に頁(`p.3`)が載る(PREP-03)。
    """
    engine = _parse_ocr_engine(ocr_engine)
    name, content = await _read_upload(file)
    return await _rag_call(_extract, name, content, engine)


def _extract(name: str, content: bytes, ocr_engine: str | None) -> dict:
    chunks = rag_adb.chunk_units(name, content, ocr_engine=ocr_engine)
    return {"filename": name, "chunk_count": len(chunks), "chunks": chunks}


async def delete_file_response(ns: str, file_id: str) -> dict:
    if not await _rag_call(rag.delete_file, ns, file_id):
        raise HTTPException(status_code=404, detail="file not found")
    return {"deleted": True}


@router.get("/api/rag/health")
async def rag_health(user: Annotated[AuthContext, Depends(require_user)]):
    """プリフライト3点検査(FIX-47): project解決 / CP / DP。失敗点をヒント付きで特定する。"""
    try:
        return await asyncio.to_thread(rag.health_check)
    except Exception as e:  # 診断エンドポイントは 500 を漏らさない(REV-001 minor)
        logger.exception("rag health check crashed")
        raise HTTPException(
            status_code=503, detail=f"health check failed: {type(e).__name__}"
        ) from e


@router.get("/api/rag/files")
async def list_rag_files(user: Annotated[AuthContext, Depends(require_user)]):
    return await list_files_response(user.subject)


@router.post("/api/rag/files")
async def upload_rag_file(
    file: UploadFile,
    user: Annotated[AuthContext, Depends(require_user)],
    # RAGM-01: 出典メタデータ(JSONオブジェクト文字列)。省略可 — 既存クライアントは無変更で動く
    attributes: Annotated[str | None, Form()] = None,
    # PREP-03: OCR エンジンの明示指定(document_understanding | vlm)。
    # 省略時は既定(DU)。**自動では切り替えない**(切り替え根拠を OCR 前に持てないため)
    ocr_engine: Annotated[str | None, Form()] = None,
):
    return await upload_file_response(user.subject, file, attributes, ocr_engine)


@router.post("/api/extract")
async def extract_document(
    file: UploadFile, user: Annotated[AuthContext, Depends(require_user)],
    ocr_engine: Annotated[str | None, Form()] = None,
):
    """抽出だけ行い、取り込みはしない(PREP-01)。認証・サイズ上限はアップロードと同じ。"""
    return await extract_response(file, ocr_engine)


@router.delete("/api/rag/files/{file_id}")
async def delete_rag_file(
    file_id: str, user: Annotated[AuthContext, Depends(require_user)]
):
    return await delete_file_response(user.subject, file_id)
