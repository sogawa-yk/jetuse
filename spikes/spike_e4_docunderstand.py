"""SPIKE-E4: OCI Document Understanding (OCR) の大阪可用性・日本語精度を実測。

- analyze_document(同期/inline base64) で TEXT/TABLE/KEY_VALUE を一括抽出
- 日本語の請求書風画像をPILで生成→OCR→正解文字列と突き合わせ
実行: python spikes/spike_e4_docunderstand.py
"""

import base64
import io
import os
import sys
import time

import oci
from PIL import Image, ImageDraw, ImageFont

from oci.ai_document import AIServiceDocumentClient
from oci.ai_document.models import (
    AnalyzeDocumentDetails,
    DocumentKeyValueExtractionFeature,
    DocumentTableExtractionFeature,
    DocumentTextExtractionFeature,
    InlineDocumentDetails,
)

FONT = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Medium.ttc"

# OCR対象の正解テキスト(請求書を模した日本語+英数)
LINES = [
    "請求書",
    "発行日: 2026年6月16日",
    "請求番号: INV-2026-0042",
    "株式会社オラクル日本 御中",
    "下記の通りご請求申し上げます。",
    "",
    "品目          数量    単価      金額",
    "クラウド利用料   10    5,000    50,000",
    "サポート費用      1   20,000    20,000",
    "合計金額                       70,000円",
    "",
    "お支払期限: 2026年7月31日",
    "振込先: みずほ銀行 大阪支店 普通 1234567",
]


def make_image() -> bytes:
    img = Image.new("RGB", (900, 620), "white")
    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(FONT, 40)
    body = ImageFont.truetype(FONT, 26)
    y = 24
    for i, line in enumerate(LINES):
        f = title if i == 0 else body
        d.text((40, y), line, fill="black", font=f)
        y += 52 if i == 0 else 40
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def client() -> AIServiceDocumentClient:
    cfg = oci.config.from_file()
    return AIServiceDocumentClient(cfg)


def main() -> int:
    png = make_image()
    open("/tmp/spike_e4_invoice.png", "wb").write(png)
    b64 = base64.b64encode(png).decode()
    comp = os.environ.get("COMPARTMENT_OCID") or oci.config.from_file().get("tenancy")

    details = AnalyzeDocumentDetails(
        document=InlineDocumentDetails(data=b64),
        features=[
            DocumentTextExtractionFeature(),
            DocumentTableExtractionFeature(),
            DocumentKeyValueExtractionFeature(),
        ],
        compartment_id=comp,
        language="JPN",
    )
    t0 = time.time()
    try:
        res = client().analyze_document(details)
    except oci.exceptions.ServiceError as e:
        print(f"ServiceError {e.status} {e.code}: {e.message}")
        print(f"endpoint: {e.request_endpoint}")
        return 1
    dt = time.time() - t0
    doc = res.data

    pages = doc.pages or []
    words = []
    lines_out = []
    for p in pages:
        for ln in (p.lines or []):
            lines_out.append(ln.text)
        for w in (p.words or []):
            words.append((w.text, getattr(w, "confidence", None)))

    full = "\n".join(lines_out)
    print(f"=== analyze_document OK in {dt:.2f}s (pages={len(pages)}) ===")
    print(f"document languages: {[(l.language_code, round(l.confidence,3)) for l in (doc.detected_document_types or [])] if False else getattr(doc,'detected_languages',None)}")
    print("--- recognized lines ---")
    print(full)
    confs = [c for _, c in words if c is not None]
    if confs:
        print(f"\nword count={len(words)} mean_conf={sum(confs)/len(confs):.3f} min={min(confs):.3f}")

    # 文字単位の素朴な一致率(順不同・空白無視)
    truth = "".join(LINES).replace(" ", "").replace("　", "")
    got = full.replace(" ", "").replace("\n", "").replace("　", "")
    hit = sum(1 for ch in set(truth) if ch in got)
    print(f"\n[char-set recall] {hit}/{len(set(truth))} = {hit/len(set(truth)):.1%}")

    # テーブル抽出
    tables = []
    for p in pages:
        tables.extend(p.tables or [])
    print(f"\n=== tables detected: {len(tables)} ===")
    for ti, tb in enumerate(tables):
        print(f"table {ti}: rows={tb.row_count} cols={tb.column_count}")
        for cell in (tb.body_rows[0].cells if getattr(tb, 'body_rows', None) else (tb.cells or [])[:6]):
            print(f"  ({cell.row_index},{cell.column_index}) {cell.text!r}")

    # キーバリュー
    kvs = []
    for p in pages:
        for f in (p.document_fields or []):
            kvs.append(f)
    print(f"\n=== key-value fields: {len(kvs)} ===")
    for f in kvs[:12]:
        lab = getattr(getattr(f, 'field_label', None), 'name', None)
        val = getattr(getattr(f, 'field_value', None), 'value', None) if getattr(f, 'field_value', None) else None
        print(f"  {lab!r} = {val!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
