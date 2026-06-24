"""OCR(ENH-07)の整形・エラーハンドリングの単体テスト(OCIクライアントはモック)。"""

from types import SimpleNamespace as NS
from unittest import mock

import pytest

from jetuse_core import docunderstand


def _fake_page():
    word = NS(text="請求書", confidence=0.99)
    line = NS(text="請求書")
    cell = NS(row_index=0, column_index=0, text="品目")
    table = NS(row_count=1, column_count=1, body_rows=[NS(cells=[cell])], cells=None)
    field = NS(field_label=NS(name="Items"), field_value=NS(value="x"))
    return NS(lines=[line], words=[word], tables=[table], document_fields=[field])


def test_ocr_shapes_result():
    doc = NS(pages=[_fake_page()])
    client = mock.Mock()
    client.analyze_document.return_value = NS(data=doc)
    with mock.patch.object(docunderstand, "_doc_client", return_value=client):
        out = docunderstand.ocr(b"\x89PNG fake", language="JPN")
    assert out["text"] == "請求書"
    assert out["page_count"] == 1
    assert out["mean_confidence"] == 0.99
    assert out["tables"][0]["rows"] == [["品目"]]
    assert out["key_values"][0] == {"label": "Items", "value": "x"}


def test_ocr_empty_raises():
    with pytest.raises(docunderstand.OcrError):
        docunderstand.ocr(b"", language="JPN")


def test_ocr_too_large_raises():
    with pytest.raises(docunderstand.OcrError):
        docunderstand.ocr(b"x" * (docunderstand.MAX_BYTES + 1))


def _make_pdf(n_pages: int) -> bytes:
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_ocr_splits_pdf_over_5_pages_and_merges():
    # 12ページ → 5/5/2 の3チャンクに分割され、結果がマージされる
    pdf = _make_pdf(12)
    assert docunderstand._is_pdf(pdf)
    assert docunderstand._pdf_page_count(pdf) == 12

    def fake_chunk(content, language, tables, key_values):
        n = docunderstand._pdf_page_count(content)
        return {
            "lines": [f"chunk{n}"], "page_count": n, "confidences": [0.9] * n,
            "tables": [{"rows": [["x"]], "row_count": 1, "column_count": 1}],
            "key_values": [{"label": "k", "value": "v"}],
        }

    with mock.patch.object(docunderstand, "_analyze_chunk", side_effect=fake_chunk) as m:
        out = docunderstand.ocr(pdf, language="JPN")
    assert m.call_count == 3  # ceil(12/5)
    assert out["chunk_count"] == 3
    assert out["page_count"] == 12
    assert len(out["tables"]) == 3
    assert len(out["key_values"]) == 3
    assert out["mean_confidence"] == 0.9


def test_ocr_small_pdf_single_shot():
    pdf = _make_pdf(3)
    with mock.patch.object(docunderstand, "_analyze_chunk") as m:
        m.return_value = {"lines": ["a"], "page_count": 3, "confidences": [0.8],
                          "tables": [], "key_values": []}
        out = docunderstand.ocr(pdf)
    assert m.call_count == 1  # 5ページ以下は分割しない
    assert out["chunk_count"] == 1
    assert out["page_count"] == 3


def test_ocr_too_many_total_pages_raises():
    pdf = _make_pdf(docunderstand.MAX_TOTAL_PAGES + 1)
    with pytest.raises(docunderstand.OcrError):
        docunderstand.ocr(pdf)


# ===== VLMエンジン(ENH-07g) =====

def test_parse_vlm_json_handles_fences_and_invalid():
    assert docunderstand._parse_vlm_json('{"text":"a","tables":[]}')["text"] == "a"
    fenced = '```json\n{"text":"b","tables":[]}\n```'
    assert docunderstand._parse_vlm_json(fenced)["text"] == "b"
    # 非JSONはtext扱いにフォールバック
    out = docunderstand._parse_vlm_json("just plain text")
    assert out["text"] == "just plain text" and out["tables"] == []


def _mock_vlm_client(content: str):
    msg = NS(content=content)
    resp = NS(choices=[NS(message=msg)])
    client = mock.Mock()
    client.chat.completions.create.return_value = resp
    return client


def test_ocr_vlm_image_extracts_text_and_tables():
    payload = ('{"text":"月次売上レポート","tables":[{"rows":'
               '[["商品名","金額"],["ノートPC","1176000"]]}]}')
    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    with mock.patch("jetuse_core.genai.make_inference_client",
                    return_value=_mock_vlm_client(payload)):
        out = docunderstand.ocr_vlm(png, tables=True)
    assert out["engine"] == "vlm"
    assert out["page_count"] == 1
    assert out["mean_confidence"] is None
    assert out["tables"][0]["rows"] == [["商品名", "金額"], ["ノートPC", "1176000"]]
    assert out["tables"][0]["column_count"] == 2
    assert "月次売上レポート" in out["text"]


def test_ocr_vlm_text_only_no_json():
    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    with mock.patch("jetuse_core.genai.make_inference_client",
                    return_value=_mock_vlm_client("こんにちは\n世界")):
        out = docunderstand.ocr_vlm(png, tables=False)
    assert out["text"] == "こんにちは\n世界"
    assert out["tables"] == []


def test_ocr_vlm_unknown_model_falls_back_to_default():
    png = b"\x89PNG\r\n\x1a\n" + b"fake"
    with mock.patch("jetuse_core.genai.make_inference_client",
                    return_value=_mock_vlm_client("x")):
        out = docunderstand.ocr_vlm(png, model="bogus", tables=False)
    assert out["model"] == docunderstand.DEFAULT_VLM_MODEL


def test_ocr_too_many_pages_maps_to_friendly_error():
    import oci

    err = oci.exceptions.ServiceError(
        413, "413", {}, "Input file has too many pages, maximum number of pages allowed is: 5"
    )
    client = mock.Mock()
    client.analyze_document.side_effect = err
    with mock.patch.object(docunderstand, "_doc_client", return_value=client):
        with pytest.raises(docunderstand.OcrError) as ei:
            docunderstand.ocr(b"%PDF fake")
    assert "5" in str(ei.value) and "ページ" in str(ei.value)


def test_ocr_not_authorized_maps_to_friendly_error():
    import oci

    err = oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope")
    client = mock.Mock()
    client.analyze_document.side_effect = err
    with mock.patch.object(docunderstand, "_doc_client", return_value=client):
        with pytest.raises(docunderstand.OcrError) as ei:
            docunderstand.ocr(b"\x89PNG fake")
    assert "IAM" in str(ei.value) or "権限" in str(ei.value)
