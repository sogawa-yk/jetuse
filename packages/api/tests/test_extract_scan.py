"""スキャン PDF / 画像の結線(PREP-03)の単体テスト。

固定したいのは 5 つ:
1. **テキスト層のある PDF は OCR を通らない**(無駄な課金をしない。対照)。
2. テキスト層の無いページだけが OCR に出る(混在 PDF で読める分は課金しない)。
3. OCR 結果が**正しいページ番号**に載る(出典が別ページを指さない)。
4. 上限超過は**切り詰めずに拒否**し、どの上限かが分かる。
5. 同じアップロードを 1 リクエストで何度読んでも OCR は 1 回(二重課金しない)。

OCI は呼ばない(`docunderstand.ocr` / `ocr_vlm` をモックする)。
"""

import pytest

from jetuse_core import docunderstand, extract_scan


@pytest.fixture(autouse=True)
def _clear_memo():
    """内容ハッシュの記憶をテスト間で持ち越さない。"""
    for memo in (extract_scan._native, extract_scan._result, extract_scan._flags):
        memo.clear()
    yield
    for memo in (extract_scan._native, extract_scan._result, extract_scan._flags):
        memo.clear()


def build_pdf(pages: list[str]) -> bytes:
    """架空の PDF を作る。空文字のページは**テキスト層なし**(= スキャン相当)。"""
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=14)
    return doc.tobytes()


def build_png(text: str = "検査記録") -> bytes:
    """**実物の PNG**（デコードできる）。偽の magic bytes ではサーバ側の検証を通らない。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((20, 50), text, fontname="japan", fontsize=12)
    return page.get_pixmap(dpi=60).tobytes("png")


PNG = build_png()


class FakeOcr:
    """`docunderstand.ocr` の代わり。渡されたページ数だけ行を返し、呼び出しを記録する。"""

    def __init__(self, prefix: str = "OCR"):
        self.calls: list[bytes] = []
        self.prefix = prefix

    def __call__(self, content, **kwargs):
        self.calls.append(content)
        self.kwargs = kwargs
        n = _page_count(content)
        return {"pages": [[f"{self.prefix}{i + 1}行目"] for i in range(n)]}


def _page_count(content: bytes) -> int:
    if content[:5] != b"%PDF-":
        return 1
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(content)).pages)


@pytest.fixture
def fake_ocr(monkeypatch):
    fake = FakeOcr()
    monkeypatch.setattr(docunderstand, "ocr", fake)
    monkeypatch.setattr(docunderstand, "ocr_vlm", FakeOcr("VLM"))
    return fake


# --- 1. 対照: テキスト層のある PDF -------------------------------------------


def test_pdf_with_a_text_layer_never_calls_ocr(fake_ocr):
    """完了条件「テキスト層のある PDF は従来どおり OCR を通さない」の固定。"""
    pdf = build_pdf(["Inventory API spec", "Rate limit 600 rpm"])
    assert extract_scan.needs_ocr("spec.pdf", pdf) is False
    pages = extract_scan.page_texts("spec.pdf", pdf)
    assert fake_ocr.calls == []                      # OCI を 1 回も呼ばない = 課金しない
    assert "Inventory API spec" in pages[0]
    assert "Rate limit 600 rpm" in pages[1]


def test_text_layer_judgement_is_per_page_and_only_about_extractable_text(fake_ocr):
    """判定根拠は「そのページの extract_text() が空白以外を返すか」だけ。"""
    assert extract_scan.needs_ocr("scan.pdf", build_pdf(["", ""])) is True
    assert extract_scan.needs_ocr("mixed.pdf", build_pdf(["text", ""])) is True
    assert extract_scan.needs_ocr("born-digital.pdf", build_pdf(["a", "b"])) is False
    assert fake_ocr.calls == []                      # 判定だけでは OCR を呼ばない


# --- 2/3. スキャン PDF・混在 PDF ---------------------------------------------


def test_scanned_pdf_is_ocred_and_keeps_page_order(fake_ocr):
    pages = extract_scan.page_texts("scan.pdf", build_pdf(["", "", ""]))
    assert len(fake_ocr.calls) == 1                  # 5 ページ単位の分割は docunderstand 側
    assert pages == ["OCR1行目", "OCR2行目", "OCR3行目"]


def test_only_pages_without_a_text_layer_are_sent_to_ocr(fake_ocr):
    """混在 PDF: 読める 2 ページは送らない(その分は課金されない)。"""
    pdf = build_pdf(["first page text", "", "third page text", ""])
    pages = extract_scan.page_texts("mixed.pdf", pdf)
    assert _page_count(fake_ocr.calls[0]) == 2       # OCR に出したのは 2 ページだけ
    assert "first page text" in pages[0]
    assert pages[1] == "OCR1行目"                    # OCR の 1 枚目 = 元の 2 ページ目
    assert "third page text" in pages[2]
    assert pages[3] == "OCR2行目"


def test_ocr_result_with_a_different_page_count_fails_instead_of_misplacing_text(monkeypatch):
    """ページ数がずれたら**詰めて並べない**(出典が別ページを指すほうが害が大きい)。"""
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [["a"]]})
    with pytest.raises(extract_scan.OcrUnavailable):
        extract_scan.page_texts("scan.pdf", build_pdf(["", ""]))


# --- 画像 ---------------------------------------------------------------------


def test_image_is_always_ocred_as_a_single_page(fake_ocr):
    assert extract_scan.needs_ocr("photo.png", PNG) is True
    assert extract_scan.page_texts("photo.jpg", PNG) == ["OCR1行目"]
    assert fake_ocr.calls == [PNG]


def test_non_document_formats_are_not_ocr_targets(fake_ocr):
    assert extract_scan.needs_ocr("a.md", b"# x") is False
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("a.md", b"# x")


# --- エンジンの選択 -----------------------------------------------------------


def test_default_engine_is_document_understanding(fake_ocr):
    extract_scan.page_texts("scan.pdf", build_pdf([""]))
    assert extract_scan.DEFAULT_ENGINE == "document_understanding"
    assert len(fake_ocr.calls) == 1                  # DU 側が呼ばれる
    # 表・キーバリューは要求しない(本文検索が目的。DU の表機能はページ単価が別に掛かる)
    assert fake_ocr.kwargs["tables"] is False and fake_ocr.kwargs["key_values"] is False


def test_vlm_engine_is_opt_in(monkeypatch):
    du, vlm = FakeOcr("DU"), FakeOcr("VLM")
    monkeypatch.setattr(docunderstand, "ocr", du)
    monkeypatch.setattr(docunderstand, "ocr_vlm", vlm)
    assert extract_scan.page_texts("scan.pdf", build_pdf([""]), engine="vlm") == ["VLM1行目"]
    assert du.calls == [] and len(vlm.calls) == 1


def test_unknown_engine_is_rejected_instead_of_falling_back(fake_ocr):
    """誤字を黙って既定へ落とすと「VLM で読んだ」と誤解したまま結果を受け取る。"""
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("scan.pdf", build_pdf([""]), engine="vlmm")
    assert fake_ocr.calls == []


# --- 4. 上限(切り詰めない) ---------------------------------------------------


def test_too_many_ocr_pages_is_rejected_before_calling_ocr(fake_ocr, monkeypatch):
    monkeypatch.setattr(extract_scan, "MAX_OCR_PAGES", 2)
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("scan.pdf", build_pdf(["", "", ""]))
    assert e.value.limit == "ocr_pages" and "limit=ocr_pages" in str(e.value)
    assert fake_ocr.calls == []                      # 上限超過で OCI を呼ばない


def test_oversized_image_is_rejected_with_the_limit_name(fake_ocr):
    big = PNG + b"x" * extract_scan.MAX_IMAGE_BYTES
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("big.png", big)
    assert e.value.limit == "ocr_bytes"
    assert fake_ocr.calls == []


def test_ocr_limit_from_docunderstand_stays_a_limit_error(monkeypatch):
    def boom(content, **kw):
        raise docunderstand.OcrLimitError("ファイルが大きすぎます(上限 60MB)")

    monkeypatch.setattr(docunderstand, "ocr", boom)
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("scan.pdf", build_pdf([""]))
    assert e.value.limit == "ocr_input"


def test_service_failure_is_not_reported_as_a_client_error(monkeypatch):
    """IAM 未整備・サービス障害は利用者の入力の問題ではない(ルート側で 503)。"""
    def boom(content, **kw):
        raise docunderstand.OcrError("OCRサービスにアクセスできません(IAM未整備の可能性)")

    monkeypatch.setattr(docunderstand, "ocr", boom)
    with pytest.raises(extract_scan.OcrUnavailable):
        extract_scan.page_texts("scan.pdf", build_pdf([""]))


def test_broken_pdf_is_a_client_error(fake_ocr):
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("broken.pdf", b"%PDF-1.7 garbage")


# --- 5. 二重 OCR をしない ------------------------------------------------------


def test_the_same_upload_is_ocred_once_for_every_ingest_path(fake_ocr):
    """マネージド変換 / ADB チャンク化 / OpenSearch が同じ本文を読んでも OCR は 1 回。"""
    pdf = build_pdf(["", ""])
    first = extract_scan.page_texts("scan.pdf", pdf)
    assert extract_scan.page_texts("scan.pdf", pdf) == first
    assert extract_scan.page_texts("scan.pdf", pdf) == first
    assert len(fake_ocr.calls) == 1


def test_a_different_engine_is_a_different_result(fake_ocr, monkeypatch):
    vlm = FakeOcr("VLM")
    monkeypatch.setattr(docunderstand, "ocr_vlm", vlm)
    pdf = build_pdf([""])
    assert extract_scan.page_texts("scan.pdf", pdf) == ["OCR1行目"]
    assert extract_scan.page_texts("scan.pdf", pdf, engine="vlm") == ["VLM1行目"]


# --- 取り込み経路へ渡す形 ------------------------------------------------------


def test_file_attributes_use_the_existing_position_key():
    """新しい属性キーを足さない(`sheet` に頁を載せる。ADB の PDF 出典と同じ語彙)。"""
    from jetuse_core import rag_metadata

    assert extract_scan.file_attributes(["a"]) == {"sheet": "p.1"}
    assert extract_scan.file_attributes(["a", "b", "c"]) == {"sheet": "p.1-p.3"}
    assert extract_scan.file_attributes(["", ""]) == {}
    # 追加キーを作っていない = 16 個上限に触れない
    assert set(extract_scan.file_attributes(["a"])) <= set(rag_metadata.ATTRIBUTE_KEYS)


def test_render_text_marks_each_page():
    text = extract_scan.render_text(["一行目", "", "三頁目"])
    assert text == "[p.1]\n一行目\n\n[p.3]\n三頁目"


# --- レビュー指摘の回帰（review-2 PREP03-002/003/004） -------------------------


def test_concurrent_requests_for_the_same_document_ocr_once(monkeypatch):
    """同じ文書を同時に上げても OCR は 1 回（single-flight）。

    覚えるだけでは足りない: キャッシュに入る前に来た 2 本目は同じ OCR を走らせてしまう
    （課金 2 倍）。**同じキーの先行者を待つ**ことで 1 回に束ねる。
    """
    import threading
    import time

    started = threading.Event()
    release = threading.Event()
    calls: list[bytes] = []

    def slow(content, **kw):
        calls.append(content)
        started.set()
        release.wait(timeout=5)
        return {"pages": [["OCR1行目"]]}

    monkeypatch.setattr(docunderstand, "ocr", slow)
    pdf = build_pdf([""])
    out: list[list[str]] = []
    threads = [threading.Thread(target=lambda: out.append(extract_scan.page_texts("s.pdf", pdf)))
               for _ in range(3)]
    threads[0].start()
    assert started.wait(timeout=5)          # 1 本目が OCR に入ったところで残りを走らせる
    for t in threads[1:]:
        t.start()
    # **待ち手が実際に待ちへ入るまで**先行者を離さない（離すのが早いと競合を再現しない）
    for _ in range(500):
        if extract_scan._result.waits >= 2:
            break
        time.sleep(0.01)
    assert extract_scan._result.waits >= 2
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert len(calls) == 1                  # 3 本の要求で OCR は 1 回だけ
    assert out == [["OCR1行目"]] * 3


def test_a_failed_extraction_does_not_block_the_next_request(monkeypatch):
    """先行者が失敗しても、待っていた側が自分で計算しに行ける（待ち続けない）。"""
    attempts = {"n": 0}

    def flaky(content, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise docunderstand.OcrError("一時的な失敗")
        return {"pages": [["OCR1行目"]]}

    monkeypatch.setattr(docunderstand, "ocr", flaky)
    pdf = build_pdf([""])
    with pytest.raises(extract_scan.OcrUnavailable):
        extract_scan.page_texts("s.pdf", pdf)
    assert extract_scan.page_texts("s.pdf", pdf) == ["OCR1行目"]


def test_extraction_stops_at_the_character_limit_while_reading(monkeypatch, fake_ocr):
    """上限は**読みながら**見る（全ページを展開しきってから数えない）。"""
    monkeypatch.setattr(extract_scan, "MAX_TEXT_CHARS", 40)
    pdf = build_pdf(["x" * 30, "y" * 30, "z" * 30])
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("big.pdf", pdf)
    assert e.value.limit == "extract_chars"


def test_ocr_output_over_the_character_limit_is_rejected(monkeypatch):
    """OCR で起こした本文が上限を超えた場合も切り詰めずに拒否する。"""
    monkeypatch.setattr(extract_scan, "MAX_TEXT_CHARS", 10)
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [["あ" * 50]]})
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("scan.pdf", build_pdf([""]))
    assert e.value.limit == "extract_chars"


def test_a_pdf_that_reads_but_cannot_be_split_is_a_client_error(monkeypatch, fake_ocr):
    """読めても書き出せない PDF がある。500 を漏らさない（ルート側で 422）。"""
    from pypdf.errors import PyPdfError

    class Boom:
        def __init__(self, *a, **kw):
            raise PyPdfError("write failed")

    monkeypatch.setattr("pypdf.PdfWriter", Boom)
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("scan.pdf", build_pdf([""]))
    assert fake_ocr.calls == []


def test_a_pdf_full_of_blank_pages_is_rejected_while_reading(fake_ocr, monkeypatch):
    """空ページの数え上げも**読みながら**。全ページ展開してから拒否しない。

    要否の判定（`needs_ocr`）は 1 枚見つけた時点で打ち切るので、ページ数の上限は
    実際に OCR へ出す側（`page_texts`）が読みながら見る。
    """
    monkeypatch.setattr(extract_scan, "MAX_OCR_PAGES", 3)
    pdf = build_pdf([""] * 10)
    assert extract_scan.needs_ocr("many.pdf", pdf) is True
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("many.pdf", pdf)
    assert e.value.limit == "ocr_pages"
    assert fake_ocr.calls == []


def test_a_blank_scan_is_rejected_instead_of_returning_no_body(monkeypatch):
    """白紙・判読できない入力は 2 つの入口とも「本文が無い」として断る（200 で 0 件にしない）。"""
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [[], []]})
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("blank.pdf", build_pdf(["", ""]))
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [[""]]})
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("blank.png", PNG)


def test_a_pdf_with_some_readable_pages_survives_a_blank_ocr_page(monkeypatch):
    """一部でも本文があれば取り込む（読めたページを白紙 1 枚のために捨てない）。"""
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [[]]})
    pages = extract_scan.page_texts("mixed.pdf", build_pdf(["readable page", ""]))
    assert "readable page" in pages[0] and pages[1] == ""


def test_the_cache_key_includes_the_format(fake_ocr):
    """同じバイト列でも形式が違えば別扱い（PDF の検証を画像の結果で飛ばさせない）。"""
    assert extract_scan.page_texts("a.png", PNG) == ["OCR1行目"]
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("a.pdf", PNG)      # PDF としては読めない


def test_a_slow_first_computer_does_not_break_the_one_that_took_over(monkeypatch):
    """待ち時間を超えて引き継がれたあと、遅れて終わった先行者が**他人の札を外さない**。

    外すと、その後に来た要求が「誰も計算していない」と誤認して同じ文書をもう一度 OCR に出す
    （＝二重課金）。review-4 PREP03-003 の回帰。
    """
    import threading
    import time

    monkeypatch.setattr(extract_scan, "OCR_WAIT_SECONDS", 0.05)
    release = threading.Event()
    calls: list[str] = []

    def slow(content, **kw):
        calls.append("call")
        if len(calls) == 1:
            release.wait(timeout=5)          # 1 本目だけ遅い（待ち手が引き継ぐ）
        return {"pages": [["OCR1行目"]]}

    monkeypatch.setattr(docunderstand, "ocr", slow)
    pdf = build_pdf([""])
    out: list[list[str]] = []
    first = threading.Thread(target=lambda: out.append(extract_scan.page_texts("s.pdf", pdf)))
    first.start()
    while not calls:
        time.sleep(0.01)
    second = threading.Thread(target=lambda: out.append(extract_scan.page_texts("s.pdf", pdf)))
    second.start()
    second.join(timeout=10)                  # 引き継いだ側は先行者を待たずに終わる
    release.set()
    first.join(timeout=10)
    assert out == [["OCR1行目"], ["OCR1行目"]]
    assert extract_scan._result._inflight == {}   # 札が残らない（次の要求が待ち続けない）
    assert extract_scan.page_texts("s.pdf", pdf) == ["OCR1行目"]
    assert len(calls) == 2                   # 引き継ぎで 2 回。3 回目はキャッシュから返る


def test_input_that_ocr_rejects_is_a_client_error_not_a_service_failure(monkeypatch):
    """拡張子だけ画像に偽装したファイル等（OCI が 4xx で拒否）は 422 側（review-5 PREP03-002）。"""
    def boom(content, **kw):
        raise docunderstand.OcrInputError("この文書はOCRできませんでした: InvalidParameter x")

    monkeypatch.setattr(docunderstand, "ocr", boom)
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.page_texts("fake.png", PNG)


def test_a_pdf_with_no_pages_is_rejected(fake_ocr):
    import io

    from pypdf import PdfWriter

    buf = io.BytesIO()
    PdfWriter().write(buf)          # ページが 1 つも無い PDF
    empty = buf.getvalue()
    with pytest.raises(extract_scan.ScanUnsupported):
        extract_scan.needs_ocr("empty.pdf", empty)
    assert fake_ocr.calls == []


def test_a_late_finisher_does_not_overwrite_the_takeover_result(monkeypatch):
    """引き継がれたあとに遅れて終わった処理は、キャッシュを古い結果へ巻き戻さない。"""
    import threading
    import time

    monkeypatch.setattr(extract_scan, "OCR_WAIT_SECONDS", 0.05)
    release = threading.Event()
    calls: list[str] = []

    def ocr(content, **kw):
        calls.append("call")
        if len(calls) == 1:
            release.wait(timeout=5)
            return {"pages": [["古い結果"]]}
        return {"pages": [["新しい結果"]]}

    monkeypatch.setattr(docunderstand, "ocr", ocr)
    pdf = build_pdf([""])
    out: list[list[str]] = []
    first = threading.Thread(target=lambda: out.append(extract_scan.page_texts("s.pdf", pdf)))
    first.start()
    while not calls:
        time.sleep(0.01)
    second = threading.Thread(target=lambda: out.append(extract_scan.page_texts("s.pdf", pdf)))
    second.start()
    second.join(timeout=10)
    release.set()
    first.join(timeout=10)
    # 引き継いだ側の結果が確定値。あとから終わった先行者は自分の呼び出し元にだけ返す
    assert extract_scan.page_texts("s.pdf", pdf) == ["新しい結果"]
    assert sorted(out) == [["古い結果"], ["新しい結果"]]


def test_rate_limiting_is_not_reported_as_a_bad_document(monkeypatch):
    """429/408 は再試行しうるサービス側の事情。「文書が不正」にしない（review-6 PREP03-002）。"""
    from oci.exceptions import ServiceError

    for status in (408, 429, 500):
        def fail(details, _s=status):
            raise ServiceError(status=_s, code="TooManyRequests", headers={}, message="slow down")

        client = type("C", (), {"analyze_document": staticmethod(fail)})()
        monkeypatch.setattr(docunderstand, "_doc_client", lambda c=client: c)
        with pytest.raises(docunderstand.OcrError) as e:
            docunderstand.ocr(b"%PDF- fake", tables=False)
        assert not isinstance(e.value, docunderstand.OcrInputError), status


def test_image_ocr_output_over_the_character_limit_is_rejected(monkeypatch):
    """画像経路も PDF と同じ総文字数の上限を通る（review-6 PREP03-003）。"""
    monkeypatch.setattr(extract_scan, "MAX_TEXT_CHARS", 10)
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: {"pages": [["あ" * 50]]})
    with pytest.raises(extract_scan.ScanLimitError) as e:
        extract_scan.page_texts("big.png", PNG)
    assert e.value.limit == "extract_chars"


def test_the_memo_evicts_by_total_characters_not_only_by_count():
    """件数だけでなく**総文字数**でも退避する（大きな文書を抱え続けない）。"""
    memo = extract_scan._Memo(size=32, budget=100)
    for i in range(5):
        memo.get_or_call((i,), lambda i=i: ["x" * 60])
    assert memo._chars <= 100 + 60          # 直近の 1〜2 件だけが残る
    assert len(memo._items) <= 2


def test_a_misconfiguration_is_not_reported_as_a_bad_document(monkeypatch):
    """400（compartment 指定ミス等でも返る）を「文書が不正」にしない（review-7 PREP03-003）。"""
    from oci.exceptions import ServiceError

    def fail(details):
        raise ServiceError(status=400, code="InvalidParameter", headers={},
                           message="compartmentId is invalid")

    client = type("C", (), {"analyze_document": staticmethod(fail)})()
    monkeypatch.setattr(docunderstand, "_doc_client", lambda: client)
    with pytest.raises(docunderstand.OcrError) as e:
        docunderstand.ocr(b"%PDF- fake", tables=False)
    assert not isinstance(e.value, docunderstand.OcrInputError)
    # 取り込み経路では 503 側（利用者の入力の問題ではない）
    monkeypatch.setattr(docunderstand, "ocr", lambda content, **kw: (_ for _ in ()).throw(
        docunderstand.OcrError("OCR失敗: InvalidParameter compartmentId is invalid")))
    with pytest.raises(extract_scan.OcrUnavailable):
        extract_scan.page_texts("scan.pdf", build_pdf([""]))


def test_a_file_that_only_pretends_to_be_an_image_is_rejected_before_ocr(fake_ocr):
    """拡張子だけ画像に偽装したファイルは**呼ぶ前に** 422（review-8 PREP03-002）。

    OCI に投げると 400 で断られるが、400 は compartment 指定ミス等でも返るので、
    そこから「入力が悪い」と断定できない（review-7 PREP03-003）。判る分はこちらで判る。
    """
    for name in ("fake.png", "fake.jpg"):
        with pytest.raises(extract_scan.ScanUnsupported):
            extract_scan.page_texts(name, b"%PDF-1.7 not an image")
        with pytest.raises(extract_scan.ScanUnsupported):
            extract_scan.needs_ocr(name, b"just text")
    assert fake_ocr.calls == []      # OCI を 1 回も呼ばない = 課金しない


def test_real_png_and_jpeg_magic_bytes_pass(fake_ocr):
    assert extract_scan.page_texts("a.png", PNG) == ["OCR1行目"]
    import fitz

    doc = fitz.open()
    jpeg = doc.new_page().get_pixmap(dpi=60).tobytes("jpg")
    assert extract_scan.page_texts("a.jpg", jpeg) == ["OCR1行目"]


def test_a_truncated_image_is_a_client_error_not_a_service_failure(fake_ocr):
    """ヘッダだけ正しく途中で切れた画像は 422（review-9 PREP03-003）。

    OCI へ投げると 400 になるが、400 は構成ミスでも返るので「入力が悪い」と断定できない。
    最後までデコードできるかを**こちらで**確かめてから送る。
    """
    real = build_png()
    for broken in (real[: len(real) // 2], b"\x89PNG\r\n\x1a\n" + b"only a header"):
        with pytest.raises(extract_scan.ScanUnsupported):
            extract_scan.page_texts("cut.png", broken)
    assert fake_ocr.calls == []


def test_a_huge_text_layer_pdf_is_still_passed_through(monkeypatch, fake_ocr):
    """テキスト層が揃った PDF は、抽出文字数が上限を超えても**素通し**（従来どおり）。

    要否の判定に総文字数の上限を掛けると、大きな仕様書 PDF が急に 422 になる
    （PREP-03 以前はマネージド側へ原本のまま渡していた）。review-9 PREP03-002 の回帰。
    """
    from jetuse_core import rag as rag_module

    monkeypatch.setattr(extract_scan, "MAX_TEXT_CHARS", 20)
    pdf = build_pdf(["long text page one", "long text page two"])
    assert extract_scan.needs_ocr("big.pdf", pdf) is False
    assert rag_module.prepare_upload("big.pdf", pdf) == ("big.pdf", pdf, {})
    assert fake_ocr.calls == []
