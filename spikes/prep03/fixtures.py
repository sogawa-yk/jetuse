"""PREP-03 の E2E で使う**架空**の文書（顧客データは使わない・顧客名も書かない）。

同じ内容を 3 つの形で作り、OCR の要否だけが違う対照になるようにしてある:

- `scanned_pdf()`: ページ画像だけの PDF（**テキスト層なし** = スキャン相当）
- `text_pdf()`: 同じ内容をテキスト層つきで作った PDF（**OCR を通してはいけない**対照）
- `scan_png()`: 1 枚の紙をスキャンした想定の PNG

作り方: PyMuPDF で日本語（組み込み CJK フォント `japan`）を描画 → そのページを
画像に焼き直して新しい PDF / PNG に貼る。焼き直した側にはテキスト層が残らない。
"""

import fitz

# OCI 側・検証用の資源には接頭辞を付ける（CLAUDE.md の運用規約）
PREFIX = "jetuse-spike-prep03"
SCAN_PDF_NAME = f"{PREFIX}-設備点検報告書スキャン.pdf"
TEXT_PDF_NAME = f"{PREFIX}-設備点検報告書デジタル.pdf"
IMAGE_NAME = f"{PREFIX}-受入検査記録.png"

RENDER_DPI = 200

# 2 ページの点検報告書。**ページごとに違う語**を入れてある（引用のページ番号を確かめるため）。
REPORT_PAGES = [
    [
        "架空商事株式会社  設備点検報告書",
        "点検日: 2026年5月14日",
        "対象設備: 第2工場 冷却ポンプ P-204",
        "点検者: 保全課 山田",
        "所見: 軸受部の振動値が管理上限を超過していた。",
    ],
    [
        "是正処置および交換部品",
        "処置区分: 予防保全（計画停止中に実施）",
        "交換部品コード: BRG-7781",
        "交換期限: 2026年6月30日まで",
        "備考: 次回点検で振動値の再測定を行う。",
    ],
]

# 画像（1 枚）の記録。上と違う語にして、どちらの文書がヒットしたか分かるようにする。
INSPECTION_LINES = [
    "架空製作所  受入検査記録",
    "ロット番号: LOT-2026-0518",
    "検査項目: 外径寸法 42.0 mm ± 0.05",
    "判定: 合格（測定値 41.98 mm）",
    "検査員: 品質保証部 佐藤",
]

# 検索の手掛かり（本文にしか無い語）。どのページ・どの文書に載るかは上のとおり。
PART_CODE = "BRG-7781"          # 報告書 2 ページ目にしか無い
DEADLINE = "2026年6月30日"      # 同上（回答の根拠確認に使う）
VERDICT = "合格"                # 画像にしか無い
LOT_NUMBER = "LOT-2026-0518"    # 画像にしか無い
QUESTION_REPORT = "冷却ポンプの交換部品コードと交換期限は何ですか"
QUESTION_IMAGE = "受入検査のロット番号と判定結果は何ですか"


def _render(lines: list[str]) -> fitz.Document:
    """テキスト層つきの 1 ページ PDF を作る。"""
    doc = fitz.open()
    page = doc.new_page()
    y = 90
    for line in lines:
        page.insert_text((56, y), line, fontname="japan", fontsize=15)
        y += 34
    return doc


def text_pdf() -> bytes:
    """テキスト層のある PDF（対照）。"""
    doc = fitz.open()
    for lines in REPORT_PAGES:
        doc.insert_pdf(_render(lines))
    return doc.tobytes(deflate=True, garbage=3)


def _page_png(lines: list[str]) -> bytes:
    return _render(lines)[0].get_pixmap(dpi=RENDER_DPI).tobytes("png")


def scanned_pdf() -> bytes:
    """ページ画像だけの PDF（テキスト層なし）。"""
    doc = fitz.open()
    for lines in REPORT_PAGES:
        page = doc.new_page()
        page.insert_image(page.rect, stream=_page_png(lines))
    # deflate しないと 1 ページ 10MB 級になる（同期 OCR の 1 チャンク上限 8MB に触る）
    return doc.tobytes(deflate=True, garbage=3)


def scan_png() -> bytes:
    return _page_png(INSPECTION_LINES)
