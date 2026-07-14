"""OCR / ドキュメント理解(ENH-07)。2エンジンを選択式で提供(ENH-07g):

- engine="document_understanding": OCI Document Understanding 同期API(analyze_document)。
  日本語テキスト抽出が高精度・高速。表抽出は英語のみ(SPIKE-E4で確定)。
- engine="vlm": OCI Generative AI のビジョンLLM(gemini-2.5-pro等)でページ画像を読む。
  日本語の表も抽出可能。ページごとにLLM呼び出し(コスト高め・厳密OCRではない)。

SPIKE-E4(2026-06-16)で大阪可用・日本語高精度を実測(char recall 100% / mean conf 0.994)。

制限とワークアラウンド(詳細は docs/guides/ocr-limits-and-workarounds.md):
- DU同期API(analyze_document/inline)は **1回あたり最大5ページ**(OCIサービス側固定。
  6ページ以上は HTTP 413)。さらに inline ペイロードは実用上 ~8MB が目安。
- 5ページ超のPDFは **サーバー側で5ページ以下に分割→各チャンクを同期OCR→結果をマージ**
  することで透過的に対応する(ENH-07b)。非同期 processor job(Object Storage入出力)が
  本来の大量ページ向けだが、入出力バケット/ポーリング/追加IAMが不要なこの分割方式を採用。
- CIのRPに `use ai-service-document-family` のIAMが必要(未付与だと404)。
- DUの表抽出は英語のみ・全リージョン同一(SPIKE-E4)。日本語の表はVLMエンジンで。
"""

import base64
import io
import json
import logging
import os

from .settings import get_settings

logger = logging.getLogger("jetuse.docunderstand")

# OCRエンジン(UI提示用)
ENGINES = [
    {"name": "document_understanding", "label": "OCI Document Understanding"},
    {"name": "vlm", "label": "VLM（ビジョンLLM）"},
]
# VLMエンジンで使えるビジョンモデル(models.pyのvision=Trueと整合)
VLM_MODELS = [
    {"key": "google.gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    {"key": "google.gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
]
_VLM_MODEL_KEYS = {m["key"] for m in VLM_MODELS}
DEFAULT_VLM_MODEL = "google.gemini-2.5-pro"
# VLMページ画像のレンダリング解像度(DPI)
VLM_RENDER_DPI = 150

# OCI Document Understanding の言語コード(主要なものだけUI提示)
LANGUAGES = [
    {"code": "JPN", "label": "日本語"}, {"code": "ENG", "label": "英語"},
    {"code": "CHI_SIM", "label": "中国語(簡体)"}, {"code": "KOR", "label": "韓国語"},
    {"code": "FRE", "label": "フランス語"}, {"code": "GER", "label": "ドイツ語"},
    {"code": "SPA", "label": "スペイン語"},
]

# 1回のinline同期呼び出しの上限(OCIサービス側固定=5ページ。SPIKE-E4実測で6ページ目から413)
MAX_SYNC_PAGES = 5
# 1チャンク(=最大5ページのPDF)のinlineペイロード上限の目安
MAX_CHUNK_BYTES = 8_000_000
# 後方互換(既存の参照・テスト用)。1チャンクの上限と同義
MAX_BYTES = MAX_CHUNK_BYTES
# アップロード全体のガード(分割対応により単発上限より大きく許容)
MAX_TOTAL_BYTES = 60_000_000
# 分割で処理する総ページ数の上限(コスト/時間のガード)
MAX_TOTAL_PAGES = 100
# チャンク並列OCRの同時実行数(直列だとGWのread_timeoutを超え504になるため並列化)
OCR_CONCURRENCY = 5

_client = None


class OcrError(Exception):
    """OCR失敗(IAM未整備・非対応形式・サービスエラー等)。"""


def _doc_client():
    global _client
    if _client is None:
        import oci

        region = get_settings().oci_region
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            _client = oci.ai_document.AIServiceDocumentClient(
                {"region": region}, signer=signer
            )
        else:
            from .genai import load_local_oci_config

            _client = oci.ai_document.AIServiceDocumentClient(load_local_oci_config())
    return _client


def _is_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def _pdf_page_count(content: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(content)).pages)


def _split_pdf(content: bytes, chunk_pages: int) -> list[bytes]:
    """PDFを chunk_pages ページごとのPDFバイト列のリストに分割する(元の内容を保持)。"""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(content))
    total = len(reader.pages)
    chunks: list[bytes] = []
    for start in range(0, total, chunk_pages):
        writer = PdfWriter()
        for i in range(start, min(start + chunk_pages, total)):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunks.append(buf.getvalue())
    return chunks


def _analyze_chunk(content: bytes, language: str, tables: bool, key_values: bool) -> dict:
    """単一チャンク(≤5ページ)を同期OCRし、生の抽出結果を返す。confidencesは生リストで返す。"""
    import oci
    from oci.ai_document.models import (
        AnalyzeDocumentDetails,
        DocumentKeyValueExtractionFeature,
        DocumentTableExtractionFeature,
        DocumentTextExtractionFeature,
        InlineDocumentDetails,
    )

    if len(content) > MAX_CHUNK_BYTES:
        raise OcrError(
            f"分割後の1チャンクが大きすぎます(上限 {MAX_CHUNK_BYTES // 1_000_000}MB)。"
            "解像度を下げるか、ページ数を減らしてください。"
        )

    features = [DocumentTextExtractionFeature()]
    if tables:
        features.append(DocumentTableExtractionFeature())
    if key_values:
        features.append(DocumentKeyValueExtractionFeature())

    details = AnalyzeDocumentDetails(
        document=InlineDocumentDetails(data=base64.b64encode(content).decode()),
        features=features,
        compartment_id=get_settings().compartment_ocid,
        language=language or "JPN",
    )
    try:
        doc = _doc_client().analyze_document(details).data
    except oci.exceptions.ServiceError as e:
        if e.status in (401, 404):
            logger.warning(
                "document understanding not authorized (%s); IAM未整備の可能性。"
                "`Allow dynamic-group jetuse-dg to use ai-service-document-family "
                "in compartment jetuse-proto` が必要です。",
                e.code,
            )
            raise OcrError(
                "OCRサービスにアクセスできません(IAM未整備の可能性)。管理者に "
                "`use ai-service-document-family` 権限の付与を依頼してください。"
            ) from e
        if e.status == 413 or "too many pages" in (e.message or "").lower():
            # 通常はここに来ない(事前分割済み)。多ページTIFF等の保険
            raise OcrError(
                f"ページ数が同期OCRの上限({MAX_SYNC_PAGES}ページ)を超えています。"
                f"{MAX_SYNC_PAGES}ページ以下に分割してアップロードしてください。"
            ) from e
        raise OcrError(f"OCR失敗: {e.code} {e.message}") from e

    pages = doc.pages or []
    lines: list[str] = []
    confs: list[float] = []
    out_tables: list[dict] = []
    out_kv: list[dict] = []
    for p in pages:
        for ln in (p.lines or []):
            lines.append(ln.text)
        for w in (p.words or []):
            c = getattr(w, "confidence", None)
            if c is not None:
                confs.append(c)
        for tb in (p.tables or []):
            ncols = tb.column_count or 0
            # ヘッダー行を落とさない: header_rows + body_rows + footer_rows を順に展開
            src_rows = list(getattr(tb, "header_rows", None) or []) \
                + list(getattr(tb, "body_rows", None) or []) \
                + list(getattr(tb, "footer_rows", None) or [])
            rows: list[list[str]] = []
            if src_rows:
                for r in src_rows:
                    row = ["" for _ in range(ncols)] if ncols else []
                    for cell in (r.cells or []):
                        ci = cell.column_index or 0
                        if ncols and ci < ncols:
                            row[ci] = cell.text or ""
                        elif not ncols:
                            row.append(cell.text or "")
                    rows.append(row)
            else:
                # フォールバック: 平坦なcellsを row_index/column_index で配置
                rows = [["" for _ in range(ncols)] for _ in range(tb.row_count or 0)]
                for cell in (getattr(tb, "cells", None) or []):
                    ri, ci = cell.row_index or 0, cell.column_index or 0
                    if ri < len(rows) and ci < len(rows[ri]):
                        rows[ri][ci] = cell.text or ""
            out_tables.append({"rows": rows, "row_count": len(rows) or (tb.row_count or 0),
                               "column_count": ncols})
        for f in (p.document_fields or []):
            lab = getattr(getattr(f, "field_label", None), "name", None)
            val = (getattr(getattr(f, "field_value", None), "value", None)
                   if getattr(f, "field_value", None) else None)
            if lab or val:
                out_kv.append({"label": lab, "value": val})

    return {"lines": lines, "page_count": len(pages), "confidences": confs,
            "tables": out_tables, "key_values": out_kv}


def ocr(content: bytes, *, language: str = "JPN",
        tables: bool = True, key_values: bool = False) -> dict:
    """画像/PDFのバイト列をOCRし、抽出結果(辞書)を返す。

    5ページを超えるPDFは内部で5ページ以下に分割して順次OCRし、結果をマージする。
    返り値: {text, lines:[...], page_count, mean_confidence, tables:[...], key_values:[...],
            chunk_count}
    """
    if not content:
        raise OcrError("空のファイルです")
    if len(content) > MAX_TOTAL_BYTES:
        raise OcrError(f"ファイルが大きすぎます(上限 {MAX_TOTAL_BYTES // 1_000_000}MB)")

    # PDFかつ5ページ超 → 分割。それ以外(画像・小さいPDF)は単発。
    chunks: list[bytes] = [content]
    if _is_pdf(content):
        try:
            n_pages = _pdf_page_count(content)
        except Exception as e:  # noqa: BLE001 — 壊れたPDFは単発でOCIに委ねる
            logger.warning("pdf page count failed (%s); single-shot", e)
            n_pages = 1
        if n_pages > MAX_TOTAL_PAGES:
            raise OcrError(
                f"ページ数が多すぎます({n_pages}ページ。上限 {MAX_TOTAL_PAGES}ページ)。"
                "分割してアップロードしてください。"
            )
        if n_pages > MAX_SYNC_PAGES:
            chunks = _split_pdf(content, MAX_SYNC_PAGES)
            logger.info("split pdf into %d chunks (%d pages)", len(chunks), n_pages)

    # チャンクは並列OCR(各6秒級・直列だとGWのread_timeoutを超え504になる)。
    # ページ順を保つため結果はインデックスで戻してから結合する。
    if len(chunks) == 1:
        results = [_analyze_chunk(chunks[0], language, tables, key_values)]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(OCR_CONCURRENCY, len(chunks))) as ex:
            results = list(
                ex.map(lambda c: _analyze_chunk(c, language, tables, key_values), chunks)
            )

    all_lines: list[str] = []
    all_confs: list[float] = []
    all_tables: list[dict] = []
    all_kv: list[dict] = []
    page_count = 0
    for r in results:
        all_lines.extend(r["lines"])
        all_confs.extend(r["confidences"])
        all_tables.extend(r["tables"])
        all_kv.extend(r["key_values"])
        page_count += r["page_count"]

    return {
        "text": "\n".join(all_lines),
        "lines": all_lines,
        "page_count": page_count,
        "mean_confidence": round(sum(all_confs) / len(all_confs), 4) if all_confs else None,
        "tables": all_tables,
        "key_values": all_kv,
        "chunk_count": len(chunks),
        "engine": "document_understanding",
    }


# ===== VLM(ビジョンLLM)エンジン(ENH-07g) =====

_VLM_PROMPT_TEXT = (
    "Transcribe ALL text from this document image in natural reading order. "
    "Preserve line breaks and the original language. Output ONLY the transcribed text, "
    "no commentary, no markdown fences."
)
_VLM_PROMPT_TABLES = (
    "Read this document image and return a single JSON object with exactly two keys:\n"
    '  "text": the full transcription in natural reading order (string),\n'
    '  "tables": an array of tables; each table is {"rows": [["cell", ...], ...]} '
    "where the first row is the header if present.\n"
    "Preserve the original language and characters exactly (including symbols like ±). "
    "Do not invent data. If there are no tables, use an empty array. "
    "Output ONLY the JSON object, no markdown fences, no commentary."
)


def _to_page_images(content: bytes) -> list[bytes]:
    """PDF/画像を「1ページ=1PNG」のリストにする。PDFはpymupdfでレンダリング。"""
    if _is_pdf(content):
        import fitz  # pymupdf

        doc = fitz.open(stream=content, filetype="pdf")
        images: list[bytes] = []
        for i, page in enumerate(doc):
            if i >= MAX_TOTAL_PAGES:
                break
            images.append(page.get_pixmap(dpi=VLM_RENDER_DPI).tobytes("png"))
        return images
    return [content]  # 画像はそのまま1ページ扱い


def _img_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/png"


def _parse_vlm_json(raw: str) -> dict:
    """LLM出力からJSON({text, tables})を頑健に取り出す。失敗時はtext扱い。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    return {"text": raw or "", "tables": []}


def _vlm_page(image: bytes, model: str, tables: bool) -> dict:
    """1ページ画像をVLMで読み、{lines:[...], tables:[...]}を返す。"""
    from .genai import make_inference_client

    b64 = base64.b64encode(image).decode()
    prompt = _VLM_PROMPT_TABLES if tables else _VLM_PROMPT_TEXT
    try:
        r = make_inference_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_img_mime(image)};base64,{b64}"}},
            ]}],
            temperature=0, max_tokens=4096,
        )
    except Exception as e:  # noqa: BLE001 — モデル不可・タイムアウト等
        raise OcrError(f"VLM OCRに失敗しました: {str(e)[:200]}") from e

    content = r.choices[0].message.content or ""
    if not tables:
        return {"lines": (content.strip().split("\n") if content.strip() else []),
                "tables": []}

    obj = _parse_vlm_json(content)
    text = str(obj.get("text") or "")
    out_tables: list[dict] = []
    for tb in (obj.get("tables") or []):
        rows = tb.get("rows") if isinstance(tb, dict) else tb
        if not isinstance(rows, list) or not rows:
            continue
        rows = [[("" if c is None else str(c)) for c in (row or [])] for row in rows]
        ncols = max((len(r) for r in rows), default=0)
        out_tables.append({"rows": rows, "row_count": len(rows), "column_count": ncols})
    return {"lines": (text.split("\n") if text else []), "tables": out_tables}


def ocr_vlm(content: bytes, *, model: str = DEFAULT_VLM_MODEL,
            tables: bool = True, language: str | None = None) -> dict:
    """ビジョンLLMで画像/PDFをOCRする。DUと同じ辞書形を返す(mean_confidenceはNone)。

    日本語の表も抽出可能。ページごとにLLMを呼ぶため、ページ数分のコストがかかる。
    """
    if not content:
        raise OcrError("空のファイルです")
    if len(content) > MAX_TOTAL_BYTES:
        raise OcrError(f"ファイルが大きすぎます(上限 {MAX_TOTAL_BYTES // 1_000_000}MB)")
    if model not in _VLM_MODEL_KEYS:
        model = DEFAULT_VLM_MODEL

    if _is_pdf(content):
        try:
            n_pages = _pdf_page_count(content)
        except Exception:  # noqa: BLE001
            n_pages = 1
        if n_pages > MAX_TOTAL_PAGES:
            raise OcrError(
                f"ページ数が多すぎます({n_pages}ページ。上限 {MAX_TOTAL_PAGES}ページ)。"
                "分割してアップロードしてください。"
            )

    images = _to_page_images(content)
    if not images:
        raise OcrError("ページ画像を生成できませんでした")

    if len(images) == 1:
        results = [_vlm_page(images[0], model, tables)]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(OCR_CONCURRENCY, len(images))) as ex:
            results = list(ex.map(lambda im: _vlm_page(im, model, tables), images))

    all_lines: list[str] = []
    all_tables: list[dict] = []
    for r in results:
        all_lines.extend(r["lines"])
        all_tables.extend(r["tables"])

    return {
        "text": "\n".join(all_lines),
        "lines": all_lines,
        "page_count": len(images),
        "mean_confidence": None,
        "tables": all_tables,
        "key_values": [],
        "chunk_count": len(images),
        "engine": "vlm",
        "model": model,
    }
