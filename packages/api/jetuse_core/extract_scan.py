"""スキャン文書(テキスト層の無い PDF)と画像の前処理(PREP-03)。

OCR 自体は `docunderstand` が持つ(ENH-07 / ENH-07b / ENH-07g)。**ここは作り直さない** —
このモジュールが担うのは 3 つだけ:

1. **いつ OCR を通すか**の判定。PDF は**ページごと**に、`pypdf` の `extract_text()` が
   空白以外を返すか(= テキスト層があるか)だけで決める。**推測しない**(ファイル名・
   生成ソフト・画像の有無では判定しない)。テキスト層のあるページは OCR を通さない
   = 課金しない。対照は `tests/test_extract_scan.py`。
2. **OCR に出すページを絞る**。テキスト層の無いページだけを 1 本のサブ PDF にまとめて
   渡す(DU は 5 ページ単位の分割を内部で行う — ENH-07b)。全ページを送らないので、
   一部だけスキャンの混在 PDF でも読める分は課金しない。
3. **ページ番号つきの本文**へ並べ直す。返すのは「ページ順の本文リスト」で、
   取り込み経路(`rag_adb.chunk_units` / `rag.prepare_upload` / `rag_opensearch`)は
   これを `p.N` という出典に載せる(`rag_adb` は PDF の出典に元から `p.N` を使っている)。

エンジンの既定は **Document Understanding**(`DEFAULT_ENGINE`)。判断根拠は
`docs/verification/PREP-03.md`。要点は「取り込みの目的は本文が検索に出ること」で、
DU は厳密 OCR・日本語高精度(SPIKE-E4)・ページ単価が安い。VLM の強み(日本語の表)は
**このタスクの非ゴール**(表構造の復元はしない)。切り替えは**利用者の明示指定のみ**で、
自動判定はしない(何を根拠に切るかを OCR 前に知る手段が無く、推測になるため)。

表・キーバリューの抽出は**要求しない**(`tables=False`)。本文検索が目的で、DU の表機能は
ページ単価が別に掛かる。
"""

import hashlib
import io
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable

from . import docunderstand

logger = logging.getLogger("jetuse.extract_scan")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
PDF_EXT = ".pdf"

# 既定エンジン。選択肢は docunderstand.ENGINES(UI・`/api/ocr` と同じ語彙)。
DEFAULT_ENGINE = "document_understanding"
ENGINE_NAMES = tuple(e["name"] for e in docunderstand.ENGINES)

# OCR に出すページ数の上限(=docunderstand の総ページ上限)。超過は切り詰めず 422。
MAX_OCR_PAGES = docunderstand.MAX_TOTAL_PAGES
# 1 画像の上限。DU は inline(base64)で送るので、分割できない画像はチャンク上限がそのまま効く。
MAX_IMAGE_BYTES = docunderstand.MAX_CHUNK_BYTES
# OCR の言語。日本語文書が対象(SPIKE-E4 で日本語高精度を実測済み)。
OCR_LANGUAGE = "JPN"
# 抽出後の総文字数の上限。**ページを読みながら見て、超えた時点で打ち切る**
# (全ページを展開しきってから数えない = 圧縮 PDF の展開爆発でメモリを食い潰さない)。
# 値は `rag_adb.MAX_EXTRACT_CHARS` と同じ。取り込み側は自分でも同じ上限を見る(二重の網)。
MAX_TEXT_CHARS = 2_000_000
# 記憶に置いてよい総文字数(worker のメモリ予算)。1 文書の上限が 200 万字なので、
# 大きな文書でも数件ぶんに収まる。件数上限とあわせて、先に当たったほうで退避する。
MAX_MEMO_CHARS = 8_000_000
# single-flight の待ち上限(秒)。先行者がこれを超えたら自分で計算しに行く
# (OCR の実測は 1 ページ数秒。5 ページ分割 + リトライを見込んでも十分な余裕)。
OCR_WAIT_SECONDS = 300


class ScanExtractError(ValueError):
    """スキャン文書を取り込めない。ルート側で 4xx / 5xx に正規化する。"""


class ScanUnsupported(ScanExtractError):
    """読めない・本文が取り出せない(壊れた PDF、未対応形式、OCR 結果が空)。→ 422"""


class ScanLimitError(ScanExtractError):
    """上限超過。**どの上限か**を `limit` と本文の両方に持つ(422 の detail に出す)。

    切り詰めて一部だけ取り込まない(PREP-01 と同じ規律 — 黙って一部を落とすと
    「取り込めたのに根拠が出ない」と区別が付かない)。
    """

    def __init__(self, limit: str, message: str):
        super().__init__(f"{message} (limit={limit})")
        self.limit = limit


class OcrUnavailable(ScanExtractError):
    """OCR サービス側の失敗(IAM 未整備・サービスエラー)。→ 503

    利用者の入力の問題ではないので 422 にしない。IAM 未整備はここに出る
    (`docunderstand` の 401/404 → `use ai-service-document-family` 未付与)。
    """


# 画像の先頭バイト(magic)。拡張子だけを信じない — 中身が画像でないものを OCR へ出すと、
# OCI が 400 で断り、こちらは「入力の問題か構成の問題か」を切り分けられなくなる
# (400 は compartment 指定ミス等でも返る)。**呼ぶ前に**こちらで判る分は判っておく。
_IMAGE_MAGIC: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",        # JPEG
)


def is_image(filename: str) -> bool:
    return _ext(filename) in IMAGE_EXTENSIONS


def _check_image(content: bytes) -> None:
    """中身が本当に画像か(拡張子を偽装した / 壊れたファイルを OCR へ出さない)。

    先頭バイトだけでは足りない — ヘッダだけ正しく途中で切れた画像は OCI が 400 で断り、
    こちらは「入力が悪い」と断定できない(400 は構成ミスでも返る)ため 503 になってしまう。
    **最後までデコードできるか**を pymupdf(既存依存)で確かめてから送る。
    """
    if not any(content.startswith(magic) for magic in _IMAGE_MAGIC):
        raise ScanUnsupported(
            "画像として読めませんでした(PNG / JPEG ではありません)。"
            "拡張子と中身が一致しているか確認してください"
        )
    import fitz

    try:
        fitz.Pixmap(content)
    except Exception as e:  # noqa: BLE001 — pymupdf は形式ごとに異なる型を投げる
        raise ScanUnsupported(
            f"画像を読めませんでした(壊れている / 途中で切れている): {type(e).__name__}"
        ) from e


def is_pdf(filename: str) -> bool:
    return _ext(filename) == PDF_EXT


def resolve_engine(engine: str | None) -> str:
    """エンジン名を検証して返す。未知の名前は**黙って既定に落とさず** 422 にする。

    黙って落とすと、`vlm` のつもりの誤字が DU で処理され、利用者は「VLM で読んだ」と
    誤解したまま結果を受け取る(RAGM-01 の「静かに 0 件」と同じ種類の事故)。
    """
    if engine is None or not str(engine).strip():
        return DEFAULT_ENGINE
    name = str(engine).strip()
    if name not in ENGINE_NAMES:
        raise ScanUnsupported(
            f"unknown ocr engine '{name}'. allowed: {', '.join(ENGINE_NAMES)}"
        )
    return name


def _ext(filename: str) -> str:
    name = (filename or "").lower()
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


# --- 同じアップロードを何度も OCR しないための記憶 ------------------------------


class _Memo:
    """直近の抽出結果を内容ハッシュで覚える(**OCR は課金される**ため)。

    1 回のアップロードで同じ本文を読む経路が 3 つある(マネージド変換 =
    `rag.prepare_upload` / ADB チャンク化 = `rag_adb.chunk_units` / OpenSearch)。
    覚えないと同じスキャン PDF を 3 回 OCR に出す = 3 倍課金される。
    内容ハッシュのみで決まる純関数なので、利用者を跨いで共有しても結果は変わらない。

    **同じキーの同時要求は 1 つに束ねる**(single-flight)。束ねないと、同じ文書を 2 人が
    同時に上げたときに OCR が 2 回走る。**別キーの計算は直列化しない**(先行者の完了を
    待つのは同じキーの要求だけ)。容量は同時に処理されうるアップロード数より十分大きく取る
    (小さいと、他文書の取り込みが割り込んだ隙に追い出されて同じ文書を再 OCR してしまう)。

    **有効範囲はこのプロセスの中だけ**。worker やコンテナが複数あるとき、同じ文書が別々の
    プロセスへ振り分けられれば OCR はその数だけ走る。ここが確実に防ぐのは
    「**1 リクエストの中で 3 経路が同じ文書を読む**」重複であり、それがこの記憶の目的である。
    配備全体での重複排除が要るなら分散ロック(ADB 等)が要る — **入れていない**。
    """

    def __init__(self, size: int = 32, budget: int = MAX_MEMO_CHARS) -> None:
        self._size = size
        self._budget = budget   # 総文字数の予算(件数だけだと大きな文書で数百 MB 残る)
        self._chars = 0
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, list[str]] = OrderedDict()
        self._inflight: dict[tuple, threading.Event] = {}
        self.waits = 0  # 待ちに入った回数(テストが競合を確定的に再現するために見る)

    def get_or_call(self, key: tuple, fn: Callable[[], list[str]]) -> list[str]:
        while True:
            with self._lock:
                hit = self._items.get(key)
                if hit is not None:
                    self._items.move_to_end(key)
                    return list(hit)
                waiting = self._inflight.get(key)
                if waiting is None:
                    owned = threading.Event()
                    self._inflight[key] = owned
                    break
                self.waits += 1
            # 同じ文書を誰かが計算中。その完了を待って結果を使う(自分では OCR を呼ばない)。
            # 先行者が失敗した場合は結果が入らないので、ループして自分が計算役になる。
            waiting.wait(timeout=OCR_WAIT_SECONDS)
            with self._lock:
                if self._inflight.get(key) is waiting:
                    # 先行者が落ちた / 待ち時間を超えた。自分が引き受ける
                    owned = threading.Event()
                    self._inflight[key] = owned
                    break
        stored = False
        try:
            value = fn()  # **ロックの外で**計算する(別文書のアップロードを直列化しない)
            stored = True
        finally:
            # **結果を入れてから inflight を外す**(同じロックの中で、この順で)。
            # 先に起こすと、起きた待ち手が「キャッシュにも inflight にも無い」状態を見て
            # 自分が計算役になり、同じ文書をもう一度 OCR に出す。
            # 成功でも失敗でも待ち手は必ず起こす(起こさないと待ち時間ぶん固まる)。
            with self._lock:
                # **書き込みも札を持っているときだけ**。待ち時間を超えて引き継がれた
                # あとに遅れて終わった処理が書くと、引き継ぎ側が確定させた新しい結果を
                # 古い結果で上書きしてしまう(呼び出し時刻で結果が巻き戻る)。
                # 自分の呼び出し元へは返す(計算はできているので捨てない)。
                mine = self._inflight.get(key) is owned
                if stored and mine:
                    self._put(key, value)
                if mine:
                    self._inflight.pop(key, None)
            owned.set()  # 自分を待っていた人は必ず起こす(結果はキャッシュに入っている)
        return value

    def _put(self, key: tuple, value: list[str]) -> None:
        """LRU で入れる。**件数と総文字数の両方**で退避する(ロック内で呼ぶ)。

        件数だけだと、200 万字級の文書が 32 件で数百 MB を worker に抱え続けうる。
        1 件で予算を超える場合も入れる(次の格納で押し出される。入れないと
        「同じ文書を毎回 OCR し直す」= 覚える意味が消える)。
        """
        old = self._items.pop(key, None)
        if old is not None:
            self._chars -= sum(len(t) for t in old)
        self._items[key] = list(value)
        self._chars += sum(len(t) for t in value)
        while len(self._items) > self._size or (
            self._chars > self._budget and len(self._items) > 1
        ):
            _, dropped = self._items.popitem(last=False)
            self._chars -= sum(len(t) for t in dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._inflight.clear()
            self._chars = 0
            self.waits = 0


_native = _Memo()   # テキスト層から取れたページ本文(OCR 前)
_result = _Memo()   # OCR 後を含む最終のページ本文
_flags = _Memo()    # 「テキスト層の無いページがあるか」だけ(本文は持たない)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --- テキスト層 ---------------------------------------------------------------

# 壊れた PDF は pypdf から複数の型で上がる。500 として漏らすと、抽出口(POST /api/extract)が
# 利用者入力で 500 を返してしまう(`rag_adb._segments` と同じ扱い)。
_BROKEN_PDF: tuple[type[BaseException], ...] = (
    ValueError, KeyError, TypeError, OSError, RecursionError,
)


def native_page_texts(content: bytes) -> list[str]:
    """PDF のテキスト層から取れるページごとの本文(**OCR は呼ばない**)。

    テキスト層の無いページは空文字になる。これがそのまま「OCR を通すか」の判定根拠。
    """
    return _native.get_or_call((_digest(content),), lambda: _read_native(content))


def _read_native(content: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    broken = (PyPdfError, *_BROKEN_PDF)
    try:
        reader = PdfReader(io.BytesIO(content))
        count = len(reader.pages)
    except broken as e:
        raise ScanUnsupported(f"PDF として読めませんでした: {type(e).__name__}") from e
    if count == 0:
        # ページが 1 つも無い PDF。素通しすると本文ゼロのまま "取り込めた" ことになる
        raise ScanUnsupported("PDF にページがありません")
    texts: list[str] = []
    total = 0
    blank = 0
    # **1 ページずつ読み、読んだ分だけ数える**(全ページを先に展開しない)。
    # 上限を超えた時点で打ち切る = 圧縮 PDF が展開後に桁違いへ膨らんでも worker を落とさない。
    for i in range(count):
        try:
            text = reader.pages[i].extract_text() or ""
        except broken as e:
            raise ScanUnsupported(
                f"PDF の {i + 1} ページ目を読めませんでした: {type(e).__name__}"
            ) from e
        if not text.strip():
            # OCR に出すことになるページ。**読みながら**数えて上限で打ち切る
            # (最後まで展開してから拒否すると、空ページだらけの小さな PDF で
            #  「拒否するために全ページ読む」時間を掛けてしまう)
            blank += 1
            if blank > MAX_OCR_PAGES:
                raise ScanLimitError(
                    "ocr_pages",
                    f"OCR が要るページ数が上限を超えました(> {MAX_OCR_PAGES} ページ)。"
                    "ファイルを分割してください",
                )
        total += len(text)
        if total > MAX_TEXT_CHARS:
            raise ScanLimitError(
                "extract_chars",
                f"抽出後の文字数が上限を超えました(> {MAX_TEXT_CHARS} 文字)",
            )
        texts.append(text)
    return texts


def needs_ocr(filename: str, content: bytes) -> bool:
    """このファイルが OCR を要するか。**画像は常に要る / PDF はテキスト層で決める**。

    テキスト層のあるページだけで構成された PDF は False = 従来どおり素通しする
    (無駄な課金をしない)。1 ページでもテキスト層が無ければ True。
    """
    if is_image(filename):
        _check_image(content)   # 偽装は**取り込みの入口で**断る(OCR も課金も発生させない)
        return True
    if not is_pdf(filename):
        return False
    return _has_page_without_text(content)


def _has_page_without_text(content: bytes) -> bool:
    """テキスト層の無いページが 1 つでもあるか。**本文は保持しない / 見つけ次第打ち切る**。

    要否の判定に本文の総量は要らない。ここで `native_page_texts` を使うと
    (a) 全ページぶんの本文をメモリに載せ (b) 抽出後の文字数上限に掛かるため、
    **テキスト層が揃った巨大な PDF が素通しできなくなる**(従来は原本のまま渡していた)。
    判定と変換を分けて、従来の素通しを壊さない。
    """
    return _flags.get_or_call((_digest(content),), lambda: _scan_for_blank(content))[0] == "Y"


def _scan_for_blank(content: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    broken = (PyPdfError, *_BROKEN_PDF)
    try:
        reader = PdfReader(io.BytesIO(content))
        count = len(reader.pages)
    except broken as e:
        raise ScanUnsupported(f"PDF として読めませんでした: {type(e).__name__}") from e
    if count == 0:
        raise ScanUnsupported("PDF にページがありません")
    for i in range(count):
        try:
            text = reader.pages[i].extract_text() or ""
        except broken as e:
            raise ScanUnsupported(
                f"PDF の {i + 1} ページ目を読めませんでした: {type(e).__name__}"
            ) from e
        if not text.strip():
            # 1 つ見つかれば十分(残りのページは読まない)。ページ数の上限は、実際に
            # OCR へ出すときに `_read_native` が数えながら見る
            return ["Y"]
    return ["N"]


# --- 抽出 ---------------------------------------------------------------------


def page_texts(filename: str, content: bytes, *, engine: str | None = None) -> list[str]:
    """ページ順の本文リストを返す(テキスト層はそのまま / 無いページだけ OCR)。

    画像は 1 ページ扱い。PDF はテキスト層のあるページの本文をそのまま使い、
    無いページだけを 1 本のサブ PDF にまとめて OCR へ出す。
    """
    name = resolve_engine(engine)
    if is_image(filename):
        _check_image(content)
        if len(content) > MAX_IMAGE_BYTES:
            raise ScanLimitError(
                "ocr_bytes",
                f"画像が大きすぎます({len(content)} > {MAX_IMAGE_BYTES} バイト)。"
                "解像度を下げてください",
            )
        # キーに**形式**も入れる。同じバイト列を `.png` で処理したあと `.pdf` で送られたら、
        # PDF としての検証を飛ばして画像の結果を返してしまう(逆も同じ)
        return _result.get_or_call(
            (_digest(content), name, "image"), lambda: _image_page_texts(content, name)
        )
    if not is_pdf(filename):
        raise ScanUnsupported(f"OCR の対象外の形式です: {_ext(filename) or '(拡張子なし)'}")
    return _result.get_or_call(
        (_digest(content), name, "pdf"), lambda: _pdf_page_texts(content, name)
    )


def _image_page_texts(content: bytes, engine: str) -> list[str]:
    """画像は 1 ページ扱い。**本文が 1 文字も取れなければ成功にしない**。

    空で成功させると、抽出口(`POST /api/extract`)が白紙・判読できない画像に
    200 / `chunk_count=0` を返す。取り込み口は同じ入力を 422 で断るので、
    2 つの入口で挙動が食い違う。
    """
    pages = _ocr(content, engine)
    text = "\n".join(pages[0]) if pages else ""
    if not text.strip():
        raise ScanUnsupported("画像から本文を抽出できませんでした(白紙 / 判読できない画像)")
    if len(text) > MAX_TEXT_CHARS:
        # PDF 経路と同じ上限を掛ける(画像だけ素通りさせない)
        raise ScanLimitError(
            "extract_chars",
            f"抽出後の文字数が上限を超えました(> {MAX_TEXT_CHARS} 文字)",
        )
    return [text]


def _pdf_page_texts(content: bytes, engine: str) -> list[str]:
    texts = list(native_page_texts(content))
    missing = [i for i, t in enumerate(texts) if not t.strip()]
    if not missing:
        return texts  # テキスト層が揃っている = OCR を呼ばない(対照シナリオ)
    if len(missing) > MAX_OCR_PAGES:
        raise ScanLimitError(
            "ocr_pages",
            f"OCR が要るページ数が上限を超えました({len(missing)} > {MAX_OCR_PAGES} ページ)。"
            "ファイルを分割してください",
        )
    pages = _ocr(_subset_pdf(content, missing), engine)
    if len(pages) != len(missing):
        # ページ数がずれたまま並べると**出典が別ページを指す**。黙って詰めない。
        logger.warning("ocr returned %d pages for %d requested", len(pages), len(missing))
        raise OcrUnavailable(
            f"OCR の結果ページ数が一致しませんでした({len(pages)} != {len(missing)})"
        )
    total = sum(len(t) for t in texts)
    for index, lines in zip(missing, pages, strict=True):
        texts[index] = "\n".join(lines)
        total += len(texts[index])
        if total > MAX_TEXT_CHARS:
            raise ScanLimitError(
                "extract_chars",
                f"抽出後の文字数が上限を超えました(> {MAX_TEXT_CHARS} 文字)",
            )
    if not any(t.strip() for t in texts):
        # 全ページ白紙。OCR も何も返さなかった = 本文が 1 文字も無い。
        # 空で成功させると 2 つの入口(抽出口 200 / 取り込み口 422)で挙動が食い違う
        raise ScanUnsupported("PDF から本文を抽出できませんでした(白紙 / 判読できない紙面)")
    return texts


def _subset_pdf(content: bytes, indices: list[int]) -> bytes:
    """指定ページだけの PDF を作る(OCR に出すページを絞るため)。"""
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PyPdfError

    # **読めた PDF が書き出せるとは限らない**(壊れたページを add_page / write する段で
    # 初めて落ちる)。ここで拾い損ねると、利用者入力に対して 500 が漏れる。
    try:
        reader = PdfReader(io.BytesIO(content))
        writer = PdfWriter()
        for i in indices:
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
    except (PyPdfError, *_BROKEN_PDF) as e:
        raise ScanUnsupported(f"PDF を分割できませんでした: {type(e).__name__}") from e
    return buf.getvalue()


def _ocr(content: bytes, engine: str) -> list[list[str]]:
    """`docunderstand` を呼び、**ページごとの行**を返す(結線だけ・実装は触らない)。"""
    try:
        if engine == "vlm":
            result = docunderstand.ocr_vlm(content, tables=False, language=OCR_LANGUAGE)
        else:
            result = docunderstand.ocr(
                content, language=OCR_LANGUAGE, tables=False, key_values=False
            )
    except docunderstand.OcrLimitError as e:
        raise ScanLimitError("ocr_input", str(e)) from e
    except docunderstand.OcrInputError as e:
        # 入力が受け付けられない(拡張子だけ画像に偽装したファイル等)。利用者の入力の問題
        raise ScanUnsupported(str(e)) from e
    except docunderstand.OcrError as e:
        # IAM 未整備・サービス障害。利用者の入力の問題ではないので 422 にしない
        raise OcrUnavailable(str(e)) from e
    return result["pages"]


# --- 取り込み経路へ渡す形 ------------------------------------------------------


def source_label(index: int) -> str:
    """出典の見出し。PDF の出典は元から `p.N`(RAGM-02)なので、画像も同じ語彙に揃える。"""
    return f"p.{index + 1}"


def render_text(pages: list[str]) -> str:
    """マネージド Vector Store へ渡す本文(ページ見出しつき)。

    マネージド側の属性は**ファイル単位**(SPIKE-M1 ①-a)なので、チャンクごとのページ番号は
    属性には載らない。本文に見出しを書くのは xlsx(PREP-01 `extract_xlsx.render_text`)と
    同じ扱いで、これで属性がチャンク単位になるわけではない。
    """
    return "\n\n".join(
        f"[{source_label(i)}]\n{t.strip()}" for i, t in enumerate(pages) if t.strip()
    )


def file_attributes(pages: list[str]) -> dict[str, str]:
    """マネージド Vector Store 向けの**ファイル単位**の属性。

    ページ番号は既存キー `sheet` に載せる。**新しい属性キーは足さない**:
    `rag_adb` は PDF の出典に元から `p.N` を `sheet_name` 列へ入れており(RAGM-02)、
    `page` キーを足すと同じ位置情報が 2 通りになる(フィルタもどちらで書くか分かれる)。
    16 個の上限にも余裕を残す(現状 8 個)。判断の記録は docs/verification/PREP-03.md。
    """
    labels = [source_label(i) for i, t in enumerate(pages) if t.strip()]
    if not labels:
        return {}
    return {"sheet": labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"}
