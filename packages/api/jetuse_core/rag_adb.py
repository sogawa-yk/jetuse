"""Oracle AI Database 自前索引の RAG バックエンド(RAGM-02 / ADR-0020 §2)。

既存の 3 バックエンド(vector_store / select_ai / opensearch)には手を入れず、
高機能側として `rag_backend='adb'` を足す。マネージド Vector Store との決定的な差は:

- **出典がチャンク単位**: シート(PDF なら頁)と行範囲をチャンクごとに持つ。
  ① Vector Store は属性が**ファイル単位**なので、1 ファイル 5 チャンクは同じ出典しか返せない
  (SPIKE-M1 ①-a 実測)。
- **絞り込みが SQL の表現力**: 版・分類・ファイル・シートを WHERE で組み合わせられ、
  業務表と JOIN したベクタ検索も 1 クエリで書ける。

埋め込みは**クライアント側**(`jetuse_core.embeddings`)で行う。DB 内埋め込み
(`DBMS_VECTOR_CHAIN.UTL_TO_EMBEDDING`)は採らない — `OCI$RESOURCE_PRINCIPAL` では
ORA-24247 で通らず(RAGM-02 実測)、`DBMS_VECTOR_CHAIN.CREATE_CREDENTIAL` による
API キーの資格証明を DB に作る必要があり、それは ADR-0021 で廃止した経路だからである。
どちらにせよ本文は OCI Generative AI の embedText へ送られる(「テナント外に出ない」ではない)。
判断の根拠とスケール実測は docs/verification/RAGM-02.md。
"""

import hashlib
import json
import logging
import re
from typing import Any

import oracledb

from . import extract_scan, extract_xlsx
from .db import connect
from .embeddings import embed
from .genai import make_inference_client
from .settings import get_settings

logger = logging.getLogger("jetuse.rag_adb")

TABLE = "rag_adb_chunks"
DOC_TABLE = "rag_adb_docs"      # 文書ごとの版（採番のロック対象・018）
INGEST_TABLE = "rag_adb_ingest"  # 取り込み試行ごとの状態（file_id 単位・019）
TOP_K = 5
GEN_MODEL = "meta.llama-3.3-70b-instruct"
CHUNK_CHARS = 800
CHUNK_OVERLAP_LINES = 1
INGEST_TIMEOUT_MS = 120_000  # 埋め込み API 込み。索引作成は別途伸ばす
EMBED_BATCH = 200   # 埋め込み API へ一度に渡すチャンク数
MAX_CHUNKS = 2_000       # 1 ファイルの上限(ベクタの同時保持量と取り込み時間の上限)
MAX_EXTRACT_CHARS = 2_000_000  # 抽出後の総文字数の上限(圧縮 PDF の展開爆発対策)
INDEX_TIMEOUT_MS = 900_000
CONTEXT_CHARS_PER_HIT = 1_200  # 生成へ渡す 1 ヒットあたりの上限
DOC_FILE_MAX = 400  # 表定義 doc_file VARCHAR2(400) に合わせる（BYTE セマンティクス想定）
TEXT_EXTENSIONS = {".txt", ".md"}  # PDF は別扱い。それ以外は未対応として弾く

# 絞り込みに使えるキー → 列。**値は必ずバインドする**(キーだけを許可制にして SQL を組む)。
FILTER_COLUMNS = {
    "current_version": "current_version",
    "version": "doc_version",
    "file": "doc_file",
    "file_id": "file_id",
    "sheet": "sheet_name",
    "kind": "kind",
}


class AdbBackendUnavailable(RuntimeError):
    """表が無い(017 未適用 / 23ai 未満)。

    チャットの RAG ディスパッチは既存 3 バックエンドと同じ扱いで、SSE の `error` フレーム
    (HTTP は 200 のまま)として返す。ストリーム開始後に判明するため 503 にはしない。
    """


# --- 可用性 -------------------------------------------------------------------


READY, ABSENT, UNAVAILABLE = "ready", "absent", "unavailable"


def availability() -> str:
    """`ready`(使える) / `absent`(表が無い = 未導入) / `unavailable`(DB 側の運用エラー)。

    **「無い」と「今つながらない」を分ける**。同じ False に丸めると、瞬断の瞬間に来た
    アップロードが取り込みも失敗記録もされないまま "未取り込み" として残り、
    復旧後も誰も気づけない。

    **結果はキャッシュしない**。一度 READY を覚えると、その後の DB 障害を
    UNAVAILABLE として検出できなくなる(= 三値にした意味が無くなる)。
    毎回 1 往復かかるが、判定を誤るより安い。
    """
    if not get_settings().adb_dsn:
        return ABSENT  # DB を持たない構成(単体テスト等)。ウォレット取得まで行かせない
    try:
        with connect() as conn:
            cur = conn.cursor()
            # **3 表そろって初めて READY**。起動時マイグレーションは API と並行して進むので、
            # 「チャンク表だけある」状態を READY にすると、取り込みも失敗記録もできないまま
            # そのファイルが永久に pending になる。
            cur.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name IN (:a, :b, :c)",
                a=TABLE.upper(), b=DOC_TABLE.upper(), c=INGEST_TABLE.upper(),
            )
            found = cur.fetchone()[0]
            if found == 3:
                return READY
            # 部分適用(マイグレーション進行中)は「無い」ではなく「今は使えない」
            return ABSENT if found == 0 else UNAVAILABLE
    except Exception:
        logger.exception("rag_adb availability check failed")
        return UNAVAILABLE


def enabled() -> bool:
    """表示用の二値(取り込み状況バッジ)。運用エラーは「使えない」側に寄せる。"""
    return availability() == READY


# --- テキスト抽出とチャンク化(出典つき) --------------------------------------


class IngestAborted(RuntimeError):
    """取り込み中に対象ファイルが削除された(台帳行が消えた)。チャンクは 1 行も作らない。"""


class UnsupportedDocument(ValueError):
    """このバックエンドが本文を取り出せない形式。取り込み状態に error として残す。"""


class TooLarge(ValueError):
    """抽出後の分量が上限を超えた。**途中で打ち切る**ので全量は展開しない。"""


def _segments(filename: str, content: bytes, *, ocr_engine: str | None = None):
    """(出典の見出し, 本文) を**逐次**返す。PDF と画像は頁ごと、テキストは 1 本。

    **対応拡張子を明示する**。何でも UTF-8 として読むと、DOCX / 画像が
    文字化けした本文として "indexed" になり、利用者には正常に見えてしまう。
    xlsx はここへ来ない(`chunk_units` が `extract_xlsx` へ振り分ける)。
    PDF / 画像は `extract_scan` が頁ごとの本文にする(テキスト層の無い頁だけ OCR。
    PREP-03)。抽出済みの総文字数が上限を超えたらそこで打ち切る。
    """
    name = (filename or "").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext == extract_scan.PDF_EXT or extract_scan.is_image(name):
        try:
            pages = extract_scan.page_texts(filename, content, engine=ocr_engine)
        except extract_scan.ScanLimitError as e:
            raise TooLarge(str(e)) from e
        except extract_scan.ScanUnsupported as e:
            raise UnsupportedDocument(str(e)) from e
        total = 0
        for i, text in enumerate(pages):
            total += len(text)
            if total > MAX_EXTRACT_CHARS:
                raise TooLarge(f"抽出後の文字数が上限を超えました(> {MAX_EXTRACT_CHARS} 文字)")
            yield (extract_scan.source_label(i), text)
        return
    if ext not in TEXT_EXTENSIONS:
        raise UnsupportedDocument(
            f"この形式は ADB バックエンドで本文を取り出せません: {ext or '(拡張子なし)'}"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UnsupportedDocument("UTF-8 として読めませんでした（バイナリの可能性）") from e
    if len(text) > MAX_EXTRACT_CHARS:
        raise TooLarge(f"本文の文字数が上限を超えました(> {MAX_EXTRACT_CHARS} 文字)")
    yield ("本文", text)


def _split_line(sheet: str, no: int, line: str) -> list[dict[str, Any]]:
    """上限を超える 1 行を、文字オフセット付きの出典で分割する。

    分割しないと 20MB のアップロードが 1 チャンクになり、
    (a) 埋め込み側は先頭 2000 文字で切られる = 本文と検索表現が食い違う
    (b) 生成時に本文全体がプロンプトへ載る、の両方を踏む(実際に踏みうる形だった)。
    """
    out = []
    for i in range(0, len(line), CHUNK_CHARS):
        piece = line[i:i + CHUNK_CHARS]
        out.append({"sheet": sheet, "cells": f"L{no}c{i + 1}-{i + len(piece)}", "text": piece})
    return out


def chunk_units(filename: str, content: bytes, *,
                ocr_engine: str | None = None) -> list[dict[str, Any]]:
    """チャンクへ割り、**チャンクごとの出典**(見出し + 行範囲)を付けて返す。

    通常は行を跨いで切らない(行番号で出典を示すため)。1 行が上限を超えるときだけ
    その行を文字オフセットで分割する(`L12c1-800`)。本文は切り詰めない。
    xlsx は `extract_xlsx` が担当し、出典は行番号ではなく**シート名 + セル範囲**になる
    (PREP-01)。返す形は同じ `{sheet, cells, text}`。
    スキャン PDF・画像は `extract_scan` が OCR して頁ごとの本文にする(PREP-03)。
    出典の `sheet` は頁(`p.3`)なので、引用に何頁目かが構造化された値で載る。
    """
    if extract_xlsx.is_xlsx(filename):
        # xlsx はシート名 + セル範囲つきで抽出する(PREP-01)。行番号ではなく実際の
        # セル範囲が `cells` に入るので、引用は「制約シート C5:E5」まで返せる。
        try:
            return extract_xlsx.extract(filename, content)
        except extract_xlsx.ExtractionLimitError as e:
            raise TooLarge(str(e)) from e
        except extract_xlsx.XlsxExtractError as e:
            raise UnsupportedDocument(str(e)) from e
    units: list[dict[str, Any]] = []
    for sheet, text in _segments(filename, content, ocr_engine=ocr_engine):
        if len(units) > MAX_CHUNKS:
            # 上限を超えた時点で打ち切る（全チャンクを展開しきってから判定しない）
            raise TooLarge(f"チャンク数が上限を超えました(> {MAX_CHUNKS})。"
                           "ファイルを分割してください")
        lines = text.splitlines()
        buf: list[str] = []
        start = 1
        for no, line in enumerate(lines, start=1):
            if len(line) > CHUNK_CHARS:
                units.extend(_flush(sheet, start, no - 1, buf))
                buf, start = [], no + 1
                units.extend(_split_line(sheet, no, line))
                continue
            # 実際に保存する本文（改行込み）で判定する。改行を数えないと上限を超え、
            # 埋め込み側の 2000 文字切り詰めと保存本文の対応が崩れる。
            if buf and len("\n".join([*buf, line])) > CHUNK_CHARS:
                units.extend(_flush(sheet, start, no - 1, buf))
                # 直前の数行を重ねて文脈の断絶を減らす。重ねた分を足しても
                # 上限に収まらないなら重ねない（overlap で上限を破らない）。
                keep = buf[-CHUNK_OVERLAP_LINES:] if CHUNK_OVERLAP_LINES else []
                while keep and len("\n".join([*keep, line])) > CHUNK_CHARS:
                    keep = keep[1:]
                start = no - len(keep)
                buf = list(keep)
            buf.append(line)
        units.extend(_flush(sheet, start, len(lines), buf))
    return [u for u in units if u["text"].strip()]


def _flush(sheet: str, start: int, end: int, buf: list[str]) -> list[dict[str, Any]]:
    if not any(x.strip() for x in buf):
        return []
    return [{"sheet": sheet, "cells": f"L{start}:L{end}", "text": "\n".join(buf)}]


# --- 埋め込み(クライアント側) -------------------------------------------------


def _vector(values: list[float]):
    import array

    return array.array("f", values)


# --- 取り込み -----------------------------------------------------------------


def doc_key(filename: str) -> str:
    """文書の識別キー（= 保存する `doc_file`）。**バイト長で切り、切ったら印を付ける**。

    - `VARCHAR2(400)` は BYTE セマンティクスのことがあり、日本語名は 1 文字 3 バイトなので
      文字数で切ると ORA-12899 になりうる。
    - 先頭を切り落とすだけだと「先頭が同じ別ファイル」が同一文書に統合され、片方が
      勝手に旧版化される。切ったときは**原名のハッシュ**を付けて衝突しないようにする。
    """
    raw = (filename or "").encode("utf-8")
    if len(raw) <= DOC_FILE_MAX:
        return filename
    digest = hashlib.sha256(raw).hexdigest()[:12]
    keep = DOC_FILE_MAX - len(f"…#{digest}".encode())
    head = raw[:keep].decode("utf-8", errors="ignore")  # マルチバイト境界で切らない
    return f"{head}…#{digest}"


def _lock_doc(cur: oracledb.Cursor, owner: str, doc_file: str) -> str:
    """文書レジストリ(018)の行を確保して**ロック**し、現在の版を返す。

    先に行を作ってからロックするので、**同名ファイルの初回同時取り込みも直列化される**
    (チャンク行だけを `FOR UPDATE` する方式は、行がまだ無い初回に効かなかった)。
    競合した側は主キー違反(ORA-00001)になるので、それは無視して同じ行をロックしに行く。
    """
    try:
        cur.execute(
            f"INSERT INTO {DOC_TABLE}(owner_sub, doc_file, doc_version)"
            " VALUES (:o, :f, '0.0')",
            o=owner, f=doc_file,
        )
    except oracledb.IntegrityError as e:
        # 許すのは主キー衝突(= 既にある / 同時取り込みが先に作った)だけ。
        # それ以外の制約違反を握り潰すと、行が無いまま版処理へ進む。
        if "ORA-00001" not in str(e):
            raise
    cur.execute(
        f"SELECT doc_version FROM {DOC_TABLE} WHERE owner_sub = :o AND doc_file = :f FOR UPDATE",
        o=owner, f=doc_file,
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"文書レジストリの行を取得できなかった: {doc_file[:80]}")
    return row[0]


def _next_version(cur: oracledb.Cursor, owner: str, doc_file: str) -> str:
    """同名ファイルの再取り込みは版を上げる(旧版は `current_version='N'` に落とす)。"""
    current = _lock_doc(cur, owner, doc_file)
    # 既存チャンクの版も見る(レジストリ導入前に入ったデータとの整合)
    cur.execute(
        f"SELECT DISTINCT doc_version FROM {TABLE} WHERE owner_sub = :o AND doc_file = :f",
        o=owner, f=doc_file,
    )
    versions = [_version_key(v[0]) for v in cur.fetchall()] + [_version_key(current)]
    return f"{max(versions, default=0) + 1}.0"


def _record_doc_version(cur: oracledb.Cursor, owner: str, doc_file: str, version: str) -> None:
    cur.execute(
        f"UPDATE {DOC_TABLE} SET doc_version = :v, updated_at = SYSTIMESTAMP"
        " WHERE owner_sub = :o AND doc_file = :f",
        v=version, o=owner, f=doc_file,
    )


def _record_ingest_state(cur: oracledb.Cursor, owner: str, file_id: str, doc_file: str, *,
                         status: str, chunks: int = 0, error: str | None = None) -> None:
    """取り込み試行の状態を **file_id 単位**で記録する(冪等な upsert)。

    文書単位(`rag_adb_docs`)に持たせると、同名ファイルの後続の取り込みが前の失敗を
    上書きしてしまい、失敗したファイルが "pending" に戻って見える(実際にそうなっていた)。
    """
    cur.execute(
        f"""MERGE INTO {INGEST_TABLE} t
            USING (SELECT :o AS owner_sub, :fi AS file_id FROM dual) s
            ON (t.owner_sub = s.owner_sub AND t.file_id = s.file_id)
            WHEN MATCHED THEN UPDATE SET t.status = :st, t.chunks = :c, t.error = :e,
                                         t.doc_key = :dk, t.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN
              INSERT (owner_sub, file_id, doc_key, status, chunks, error)
              VALUES (:o, :fi, :dk, :st, :c, :e)""",
        o=owner, fi=file_id, dk=doc_file, st=status, c=chunks,
        e=(error or "")[:1000] or None,
    )


def ingest(owner: str, file_id: str, filename: str, content: bytes,
           *, kind: str = "doc", ocr_engine: str | None = None) -> int:
    """1 ファイルをチャンク化 → 埋め込み → 投入する。投入チャンク数を返す。

    同名ファイルの再取り込みでは旧チャンクを `current_version='N'` にして版を上げる
    (削除しない = 「旧版を根拠にしない」を検索側の WHERE で選べるようにするため)。
    """
    # 版の検索・旧版化・INSERT で**同じ値**を使う（ずれると複数の版が同時に現行版になる）。
    doc_file = doc_key(filename)
    try:
        units = chunk_units(filename, content, ocr_engine=ocr_engine)
    except (UnsupportedDocument, TooLarge, extract_scan.OcrUnavailable) as e:
        # OCR サービス側の失敗も**状態として残す**(PREP-03)。握り潰すとバッジが永久に
        # "pending" のままで、IAM 未整備が誰にも伝わらない
        _mark_failed(owner, doc_file, file_id, str(e))
        return 0
    if len(units) > MAX_CHUNKS:
        _mark_failed(owner, doc_file, file_id,
                     f"チャンク数が上限を超えました({len(units)} > {MAX_CHUNKS})。"
                     "ファイルを分割してください")
        return 0
    if not units:
        # 本文を取り出せなかった(空・テキスト層の無い PDF 等)。黙って成功にすると
        # バッジが永久に "pending" のままで、原因が利用者に伝わらない。
        _mark_failed(owner, doc_file, file_id, "本文を抽出できませんでした(0 チャンク)")
        return 0
    try:
        return _ingest(owner, file_id, doc_file, units, kind, content)
    except IngestAborted:
        logger.info("rag_adb ingest aborted: file %s was deleted", file_id)
        return 0  # 削除が先行しただけ。error 状態にはしない(残すべき状態が無い)
    except Exception as e:
        _mark_failed(owner, doc_file, file_id, f"{type(e).__name__}: {e}")
        raise


def mark_unavailable(owner: str, file_id: str, filename: str) -> None:
    """DB 側の運用エラーで取り込めなかったことを記録する(best-effort)。"""
    _mark_failed(owner, doc_key(filename), file_id,
                 "ADB に接続できず取り込めませんでした（再アップロードで取り込まれます）")


def _mark_failed(owner: str, doc_file: str, file_id: str, message: str) -> None:
    """取り込みの失敗を状態として残す(best-effort)。`backends.adb` が "error" になる。"""
    try:
        with connect() as conn:
            cur = conn.cursor()
            _record_ingest_state(cur, owner, file_id, doc_file, status="error", error=message)
            conn.commit()
    except Exception:
        logger.exception("rag_adb failure state not recorded (ignored)")


def _ingest(owner: str, file_id: str, doc_file: str, units: list[dict[str, Any]],
            kind: str, content: bytes) -> int:
    sha = hashlib.sha256(content).hexdigest()  # 出典: 原本のハッシュ
    attrs = json.dumps({"source": "upload", "ext": (doc_file.rsplit(".", 1) + [""])[1].lower()},
                       ensure_ascii=False)
    # **埋め込み(外部 API)はトランザクションの外で終わらせる**。DB 接続とロックを
    # 保持したまま外部 API を待つと、プール(最大 4)が数件の同時アップロードで枯渇し、
    # チャットや削除まで巻き添えで失敗する。`call_timeout` は外部 API には効かない。
    vectors: list = []
    for base in range(0, len(units), EMBED_BATCH):
        batch = units[base:base + EMBED_BATCH]
        vectors.extend(_vector(v) for v in
                       embed([u["text"] for u in batch], input_type="SEARCH_DOCUMENT"))

    with connect() as conn:
        conn.call_timeout = INGEST_TIMEOUT_MS
        cur = conn.cursor()
        # **台帳行(rag_files)をロックしてから**チャンクを作る。ロックしないと、
        # 取り込み中に削除が走ったとき「削除側は未コミットのチャンクを見つけられず成功し、
        # あとから取り込み側がチャンクを commit する」= 削除済み文書が回答に残る。
        # 行が既に無ければ削除が先行したということなので、1 行も作らずに中止する。
        cur.execute(
            "SELECT id FROM rag_files WHERE id = :f AND owner_sub = :o FOR UPDATE",
            f=file_id, o=owner,
        )
        if not cur.fetchone():
            raise IngestAborted(f"取り込み対象の台帳行が無い(削除済み): {file_id}")
        version = _next_version(cur, owner, doc_file)
        cur.execute(
            f"UPDATE {TABLE} SET current_version = 'N' WHERE owner_sub = :o AND doc_file = :f",
            o=owner, f=doc_file,
        )
        cols = ("chunk_id, owner_sub, file_id, chunk_no, doc_file, doc_version, sheet_name,"
                " cells, sha256, kind, current_version, attributes, body, embedding")
        sql = (f"INSERT INTO {TABLE} ({cols}) VALUES (:chunk_id, :owner, :file_id, :chunk_no,"
               f" :doc_file, :version, :sheet, :cells, :sha256, :kind, 'Y', JSON(:attrs), :body,"
               f" :embedding)")
        for n, unit in enumerate(units):
            cur.execute(sql, **{
                "chunk_id": f"{file_id}-{n}", "owner": owner, "file_id": file_id,
                "chunk_no": n, "doc_file": doc_file, "version": version,
                "sheet": unit["sheet"], "cells": unit["cells"], "sha256": sha,
                "kind": kind, "attrs": attrs, "body": unit["text"],
                "embedding": vectors[n],
            })
        _record_doc_version(cur, owner, doc_file, version)
        _record_ingest_state(cur, owner, file_id, doc_file, status="indexed",
                             chunks=len(units))
        conn.commit()
    ensure_indexes()
    return len(units)


# 絞り込みに使う列の B木索引。**これが無いとメタデータ WHERE 付きの検索でベクタ索引が
# 使われず全件走査に倒れる**(50,000 行で実測 — docs/verification/RAGM-02.md ②)。
# マイグレーションではなくここで作る: Oracle の DDL は暗黙コミットで、1 ファイルに
# 複数 DDL を並べると途中失敗時に「表はあるが migration 未記録」で再実行不能になるため。
BTREE_INDEXES = {
    "idx_rag_adb_chunks_owner": "(owner_sub, file_id)",
    "idx_rag_adb_chunks_meta": "(owner_sub, current_version, kind)",
    "idx_rag_adb_chunks_file": "(owner_sub, doc_file)",
}


def _existing_indexes(cur: oracledb.Cursor) -> set[str]:
    cur.execute("SELECT index_name FROM user_indexes WHERE table_name = :t", t=TABLE.upper())
    return {r[0].upper() for r in cur.fetchall()}


def ensure_indexes() -> dict[str, bool]:
    """B木索引とベクタ索引を(無ければ)作る。冪等。作れなくても検索は成立するので落とさない。

    索引が無い状態でも厳密検索で**結果は正しい**(遅くなるだけ)。取り込みを索引作成の
    失敗で落とすほうが害が大きいので、ここは best-effort に留める。
    """
    made: dict[str, bool] = {}
    try:
        with connect() as conn:
            conn.call_timeout = INDEX_TIMEOUT_MS
            cur = conn.cursor()
            existing = _existing_indexes(cur)
            for name, cols in BTREE_INDEXES.items():
                if name.upper() in existing:
                    continue
                made[name] = _try_ddl(cur, f"CREATE INDEX {name} ON {TABLE}{cols}")
            vidx = f"{TABLE}_vidx".upper()
            if vidx not in existing:
                made[vidx] = _try_ddl(
                    cur,
                    f"CREATE VECTOR INDEX {vidx} ON {TABLE}(embedding) "
                    "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95",
                ) or _try_ddl(
                    cur,
                    f"CREATE VECTOR INDEX {vidx} ON {TABLE}(embedding) "
                    "ORGANIZATION NEIGHBOR PARTITIONS DISTANCE COSINE WITH TARGET ACCURACY 95",
                )
    except Exception:
        logger.exception("rag_adb ensure_indexes failed (ignored)")
    return made


def _try_ddl(cur: oracledb.Cursor, ddl: str) -> bool:
    try:
        cur.execute(ddl)
        return True
    except oracledb.DatabaseError as e:
        # ORA-00955(既存)は競合した別プロセスが先に作っただけなので正常
        logger.info("rag_adb ddl skipped: %s", str(e).splitlines()[0])
        return False


def delete_chunks(cur: oracledb.Cursor, owner: str, file_id: str) -> int:
    """チャンクを削除し、**残った最新版を現行版へ戻す**(コミットしない)。

    **呼び出し側のトランザクションで実行する**ため cursor を受け取る。RAG ファイルの
    台帳行(`rag_files`)と同じ ADB にあるので、同一トランザクションで消せば
    「API は削除成功なのにチャンクだけ残って以後の回答に混ざる」が構造的に起きない。

    現行版を消したまま何もしないと、旧版が全部 `current_version='N'` のまま残り、
    既定の検索(現行版のみ)から永久に見えなくなる。削除した文書ごとに、
    残っている最大版を現行へ昇格する。
    """
    # 「チャンク表が無い(017 未適用 = このバックエンド未導入)」だけは消すものが無いので許す。
    # それ以外の表が無い場合(部分適用)は**失敗させる** — 呼び出し側が commit すると
    # 「台帳行だけ消えてチャンクは残る」= 削除済み文書が回答に混ざり続ける。
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=TABLE.upper())
    if cur.fetchone()[0] == 0:
        return 0
    cur.execute(
        f"SELECT DISTINCT doc_file FROM {TABLE} WHERE owner_sub = :o AND file_id = :f",
        o=owner, f=file_id,
    )
    docs = [r[0] for r in cur.fetchall()]
    # **取り込みと同じロック順序**（文書レジストリ行 → チャンク）で直列化する。
    # ロックしないと、同じ文書の現行版と旧版を別接続から同時に消したとき、
    # 昇格対象に選んだ版がその隙に消えて「全部 N のまま」になりうる。
    for doc in docs:
        _lock_doc(cur, owner, doc)
    cur.execute(f"DELETE FROM {TABLE} WHERE owner_sub = :o AND file_id = :f",
                o=owner, f=file_id)
    deleted = cur.rowcount
    # 取り込み状態(019)も同じトランザクションで落とす（消したファイルが error のまま残らない）。
    # ここで表が無ければ部分適用なので、握り潰さず失敗させる。
    cur.execute(f"DELETE FROM {INGEST_TABLE} WHERE owner_sub = :o AND file_id = :f",
                o=owner, f=file_id)
    for doc in docs:
        promote_latest_version(cur, owner, doc)
    return deleted


def promote_latest_version(cur: oracledb.Cursor, owner: str, doc_file: str) -> str | None:
    """`doc_file` に現行版が 1 つも無ければ、残っている最大版を現行へ戻す。"""
    cur.execute(
        f"SELECT COUNT(*) FROM {TABLE}"
        " WHERE owner_sub = :o AND doc_file = :f AND current_version = 'Y'",
        o=owner, f=doc_file,
    )
    if cur.fetchone()[0]:
        return None
    cur.execute(
        f"SELECT doc_version FROM {TABLE} WHERE owner_sub = :o AND doc_file = :f",
        o=owner, f=doc_file,
    )
    versions = {v[0] for v in cur.fetchall()}
    if not versions:
        return None
    latest = max(versions, key=_version_key)
    cur.execute(
        f"UPDATE {TABLE} SET current_version = 'Y'"
        " WHERE owner_sub = :o AND doc_file = :f AND doc_version = :v",
        o=owner, f=doc_file, v=latest,
    )
    return latest


def _version_key(version: str) -> int:
    m = re.match(r"\d+", version or "")
    return int(m.group()) if m else 0


def errored_file_ids(owner: str) -> set[str]:
    """取り込みに失敗した file_id 集合(取り込み状態 019 の status='error')。"""
    try:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT file_id FROM {INGEST_TABLE} WHERE owner_sub = :o AND status = 'error'",
                o=owner,
            )
            return {r[0] for r in cur.fetchall()}
    except Exception:
        logger.exception("rag_adb errored_file_ids failed (ignored)")
        return set()


def indexed_file_ids(owner: str) -> set[str]:
    """取り込み済みの file_id 集合(取り込みは同期 = 即時)。"""
    try:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT file_id FROM {TABLE} WHERE owner_sub = :o", o=owner
            )
            return {r[0] for r in cur.fetchall()}
    except Exception:
        logger.exception("rag_adb indexed_file_ids failed (ignored)")
        return set()


# --- 検索(メタデータ絞り込み + ベクタ類似検索を 1 本の SQL で) ----------------

_SAFE_VALUE = re.compile(r"^[^\x00]{0,400}$")
BIND_PREFIX = "flt_"

SEARCH_COLS = """
  chunk_id, file_id, chunk_no, doc_file, doc_version, sheet_name, cells,
  SUBSTR(sha256, 1, 12) AS sha256_head, kind, current_version,
  JSON_SERIALIZE(attributes) AS attrs, body,
  VECTOR_DISTANCE(embedding, :q, COSINE) AS dist
"""


def build_where(filters: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """許可キーだけを WHERE に組み、**値はすべてバインド**して返す。

    未知のキーは黙って捨てない(誤字が静かに全件一致になるのを避ける = SPIKE-M1 ①-b の
    「存在しないキーでも 0 件でエラーにならない」を繰り返さないため)。
    """
    clauses = ["owner_sub = :owner"]
    binds: dict[str, Any] = {}
    for key, value in (filters or {}).items():
        col = FILTER_COLUMNS.get(key)
        if not col:
            raise ValueError(f"unsupported filter key: {key}")
        if value is None:
            continue
        if not isinstance(value, str) or not _SAFE_VALUE.match(value):
            raise ValueError(f"invalid filter value for {key}")
        # バインド名はキーそのものにしない。`:file` のような予約語は ORA-01745 になる(実測)
        name = f"{BIND_PREFIX}{key}"
        clauses.append(f"{col} = :{name}")
        binds[name] = value
    return "WHERE " + " AND ".join(clauses), binds


def search_sql(where: str, k: int, *, table: str = TABLE) -> str:
    """「メタデータ絞り込み + ベクタ類似検索」の 1 本の SQL。

    `FETCH APPROX FIRST` にしてベクタ索引を使う形にしてある(索引が無い / 使えないときは
    Oracle 側が厳密検索へ落ちる)。**スケール検証はこの関数が返す SQL そのもの**を
    表名だけ差し替えて測っている(測った SQL と動く SQL がずれないようにするため)。
    `TARGET ACCURACY` は索引作成時の 95 をそのまま使う(80 以上で recall@10 = 1.00 の実測)。
    """
    return f"""
WITH qvec AS (SELECT :q AS q FROM dual)
SELECT {SEARCH_COLS.replace(':q', '(SELECT q FROM qvec)')}
FROM {table}
{where}
ORDER BY VECTOR_DISTANCE(embedding, (SELECT q FROM qvec), COSINE)
FETCH APPROX FIRST {int(k)} ROWS ONLY
"""


def search(owner: str, query: str, *, k: int = TOP_K,
           filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """メタデータ絞り込み + ベクタ類似検索を 1 本の SQL で実行する。"""
    where, binds = build_where(filters)
    sql = search_sql(where, k)
    binds["owner"] = owner
    binds["q"] = _vector(embed([query], input_type="SEARCH_QUERY")[0])
    with connect() as conn:
        conn.call_timeout = INGEST_TIMEOUT_MS
        cur = conn.cursor()
        try:
            cur.execute(sql, **binds)
        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                raise AdbBackendUnavailable(
                    f"{TABLE} が無い(マイグレーション 017 未適用か 23ai 未満)"
                ) from e
            raise
        cols = [d[0].lower() for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return [_hit(r) for r in rows]


def _hit(row: dict[str, Any]) -> dict[str, Any]:
    """行を「本文 + 構造化出典」に整える(本文への埋め込みではなく列 / JSON から作る)。"""
    try:
        attrs = json.loads(row.get("attrs") or "{}")
    except (TypeError, ValueError):
        attrs = {}
    return {
        "text": row["body"],
        "score": None if row["dist"] is None else round(1 - float(row["dist"]), 4),
        "file_id": row["file_id"],
        "filename": row["doc_file"],
        "source": {
            "chunk_id": row["chunk_id"],
            "chunk_no": row["chunk_no"],
            "file": row["doc_file"],
            "version": row["doc_version"],
            "sheet": row["sheet_name"],
            "cells": row["cells"],
            "sha256": row["sha256_head"],
            "kind": row["kind"],
            "current_version": row["current_version"],
            "attributes": attrs,
        },
    }


def citations(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """引用。既存契約 `{file_id, filename, score}` を保ったまま `source` / `text` を足す。"""
    return [
        {"file_id": h["file_id"], "filename": h["filename"], "score": h["score"],
         "source": h["source"], "text": h["text"][:400]}
        for h in hits
    ]


def generate(owner: str, prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """検索した文脈で回答を生成し、(本文, citations) を返す。

    `rag_select_ai.generate` / `rag_opensearch.generate` と同一シグネチャ(チャットの
    RAG ディスパッチで共用する)。既定では現行版だけを根拠にする。
    """
    hits = search(owner, prompt, filters={"current_version": "Y"})
    if not hits:
        return ("アップロードされた文書から関連する情報が見つかりませんでした。", [])
    # 1 ヒットが巨大でもプロンプトが破裂しないよう、文脈は上限で切る
    context = "\n\n".join(
        f"[{i + 1}] ({h['source']['file']} {h['source']['sheet']} {h['source']['cells']})"
        f" {h['text'][:CONTEXT_CHARS_PER_HIT]}"
        for i, h in enumerate(hits)
    )
    r = make_inference_client().chat.completions.create(
        model=GEN_MODEL,
        messages=[
            {"role": "system", "content":
             "あなたは文書アシスタントです。以下の参考文書のみを根拠に、"
             "日本語で簡潔に回答してください。参考文書にない事項は推測せず「不明」と述べること。"},
            {"role": "user", "content": f"参考文書:\n{context}\n\n質問: {prompt}"},
        ],
        temperature=0, max_tokens=1000,
    )
    return (r.choices[0].message.content or "").strip(), citations(hits)
