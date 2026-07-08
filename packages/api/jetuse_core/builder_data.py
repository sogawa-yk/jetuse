"""サンプルデータ生成 + 箱への投入(SP3-04 / specs/19 §6)。

検証済みデモプラン(builder_design — §3.2/§3.3)の data 定義から、LLM でサンプルデータを
生成して demo の箱 `demo_<id>` へ投入する。投入は既存内部関数のみ(§6.1/§6.2 — 新規の
投入経路・命名・後始末部品を作らない):

- 表 = datasets.create_dataset(registry-first・VPD・完全ハッシュ命名・リース・箱上限を
  素通し。column_types でプラン列型を物理スキーマへ反映)。LLM 生成 CSV はプラン準拠の
  型・件数でサーバ側検証し、不合格は有界再試行 → だめなら DataProvisionError
  (呼び出し側 = SP3-03 の生成オーケストレーションが demo を failed にする — §1.2)。
- 文書 = rag.add_file(予約 ledger・quota・原本 put の既存経路)。LLM Markdown は
  1 文書 ≤ 64KB(§6.2 — 超過・空は再試行 → 失敗)。投入後は vector store の索引完了を
  有界待機する(rag.search が引ける状態が受け入れ条件 — §9 SP3-04)。

冪等置換(§6.3): 同名 dataset / 同名文書は「生成成功後に 既存削除(外部先行の
delete_dataset / delete_file)→ 再作成」。生成が検証を通らない限り既存データは消さない。
途中失敗の残骸は次回再生成の置換 or demo DELETE の既存後始末が回収する(専用部品なし)。

リース(§8.2): LLM 生成はリースを跨がない。生成・検証(フェーズ1)をリース外で終えてから、
箱に書く区間(フェーズ2: 置換削除+投入)だけを demo_lease.mutation(status 再確認込み)で
行う。フェーズ3(索引の有界待機)は読み取りのみ = リース外。

隔離(§6.4): owner キーは demo_<id>(DemoContext.namespace と同一導出)のみを使う。
usage は戻り値 / DataProvisionError.usage に載せ、呼び出し側が実ユーザー(owner)に
紐づけて usage_log する(§8.3 — 監査は人に紐づける)。
"""

import csv
import decimal
import io
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import datasets, demo_lease, rag
from .datasets import GEN_MODEL, _strip_fences

logger = logging.getLogger("jetuse.builder_data")

# 初回 + 再試行 2 回(§6.1/§6.2 の有界再試行 — builder_design.MAX_REGENERATIONS と同じ幅)
MAX_ATTEMPTS = 3
DOC_MAX_BYTES = 64 * 1024  # §6.2: 1 文書 ≤ 64KB(LLM 出力の有界化)
# vector store 索引完了の有界待機(§6.2 — SPIKE-03: attach 後の索引は数十秒オーダー)
RAG_WAIT_TIMEOUT_S = 300
RAG_WAIT_INTERVAL_S = 5

_VARCHAR_RE = re.compile(r"^VARCHAR2\((\d+) CHAR\)$")
_NUMBER_RE = re.compile(r"^NUMBER(?:\((\d+)(?:,(\d+))?\))?$")


class DataProvisionError(RuntimeError):
    """プラン準拠のデータを生成・投入できなかった(呼び出し側で demo failed — §1.2)。

    usage = 失敗までに消費した LLM usage(エラー経路でも usage_log から欠落させない —
    builder_design.DesignError と同じ流儀)。
    """

    def __init__(self, summary: str, usage: dict | None = None):
        super().__init__(summary)
        self.usage = usage or {"input_tokens": 0, "output_tokens": 0}


def _llm(prompt: str) -> tuple[str, dict]:
    """ビルダー共用の単発補完(usage 込み)を generate_dataset 系の GEN_MODEL で呼ぶ。"""
    from .builder_hearing import _complete

    return _complete([{"role": "user", "content": prompt}], model_key=GEN_MODEL)


def _add_usage(total: dict, usage: dict) -> None:
    for k in ("input_tokens", "output_tokens"):
        total[k] = total.get(k, 0) + (usage.get(k, 0) or 0)


def _check_value(value: str, coltype: str) -> str | None:
    """セル値がプランの列型に適合するか(§6.1)。空 = NULL は create_dataset の conv と同じ扱い。"""
    v = (value or "").strip()
    if not v:
        return None
    m = _NUMBER_RE.match(coltype)
    if m:
        try:
            d = Decimal(v)
        except InvalidOperation:
            return f"NUMBER でない値 {v[:40]!r}"
        if not d.is_finite():  # NaN / Infinity は Decimal を通る — 有限値のみ許す
            return f"NUMBER でない値 {v[:40]!r}"
        if m.group(1) is not None:
            # Oracle NUMBER(p,s): scale へ丸めた後の整数部が p-s 桁を超えると ORA-01438。
            # 丸め前判定だと NUMBER(3,1) の 99.99(→100.0)を通してしまう(review-2 F004)
            p, s = int(m.group(1)), int(m.group(2) or 0)
            with decimal.localcontext() as ctx:
                ctx.prec = 99  # 38 桁 NUMBER の quantize が既定 prec=28 で落ちないように
                q = d.quantize(Decimal(1).scaleb(-s), rounding=decimal.ROUND_HALF_UP)
            if abs(q) >= Decimal(10) ** (p - s):
                return f"{coltype} の整数部桁数を超える値 {v[:40]!r}"
        return None
    if coltype == "DATE":
        try:
            date.fromisoformat(v)
        except ValueError:
            return f"DATE(YYYY-MM-DD)でない値 {v[:40]!r}"
        return None
    if coltype == "TIMESTAMP":
        try:
            datetime.fromisoformat(v)
        except ValueError:
            return f"TIMESTAMP(ISO 8601)でない値 {v[:40]!r}"
        return None
    m = _VARCHAR_RE.match(coltype)  # プランは §3.3 検証済み = 残りは VARCHAR2(n CHAR)
    if m and len(v) > int(m.group(1)):
        return f"{m.group(1)}文字を超える値"
    return None


def _validate_csv(table: dict, csv_text: str) -> str:
    """プラン準拠の型・件数のサーバ側検証(§6.1)。戻り = エラー要約(合格は空文字)。"""
    names = [c["name"] for c in table["columns"]]
    try:
        rows = [r for r in csv.reader(io.StringIO(csv_text))
                if any((c or "").strip() for c in r)]
    except csv.Error as e:  # 巨大セル(field limit)や壊れた引用符も再試行へ収束させる
        return f"CSV 解析エラー: {e}"
    if not rows:
        return "CSV が空です"
    header = [h.strip() for h in rows[0]]
    if header != names:
        return f"ヘッダ不一致: 期待 {names} / 実際 {header}"
    body = rows[1:]
    if len(body) != table["rows"]:
        return f"データ行数不一致: 期待 {table['rows']} 行 / 実際 {len(body)} 行"
    errors: list[str] = []
    for i, row in enumerate(body, start=2):
        if len(row) != len(names):
            errors.append(f"{i}行目: 列数 {len(row)}(期待 {len(names)})")
        else:
            for col, v in zip(table["columns"], row, strict=True):
                e = _check_value(v, col["type"])
                if e:
                    errors.append(f"{i}行目 {col['name']}: {e}")
        if len(errors) >= 5:  # フィードバックは有界に(全行列挙しない)
            break
    return " / ".join(errors[:5])


def _table_prompt(table: dict, context: str, feedback: str) -> str:
    colspec = "\n".join(
        f"- {c['name']} ({c['type']}): {c.get('description') or ''}"
        for c in table["columns"]
    )
    retry = f"\n直前の出力は検証に不合格でした。検証エラー: {feedback}\n" if feedback else ""
    return (
        "あなたはサンプルデータ生成アシスタントです。デモ用のもっともらしいダミーデータを"
        "CSV形式だけで生成してください。\n"
        f"デモの文脈: {context}\n"
        f"表: {table['title']} ({table['name']})\n"
        f"列(この名前と順序をヘッダにそのまま使う):\n{colspec}\n"
        "ルール(厳守):\n"
        "- 1行目はヘッダ。上記の列名をこの順序で、変更せずに使う。\n"
        f"- データ行はちょうど{table['rows']}行。\n"
        "- NUMBER 列は数値のみ(単位・カンマ・空白・指数表記を付けない)。"
        "NUMBER(p,s) は整数部 p-s 桁以内。\n"
        "- DATE 列は YYYY-MM-DD、TIMESTAMP 列は YYYY-MM-DD HH:MM:SS 形式。\n"
        "- VARCHAR2(n CHAR) 列は n 文字以内。値は日本語でよい。\n"
        "- 値は現実的でばらつきのある内容にする(同じ値の単純な繰り返しを避ける)。\n"
        "- セルにカンマや改行を含めない(必要なら言い換える)。\n"
        "- CSV以外の説明文・前置き・コードブロック記号(```)は一切出力しない。\n"
        f"{retry}"
    )


def _doc_prompt(doc: dict, context: str, feedback: str) -> str:
    retry = f"\n直前の出力は不合格でした: {feedback}\n" if feedback else ""
    return (
        "あなたはデモ用ドキュメントの執筆アシスタントです。以下の文書を Markdown で"
        "生成してください(Markdown 本文のみ。前置き・コードフェンスなし)。\n"
        f"デモの文脈: {context}\n"
        f"文書タイトル: {doc['title']}\n"
        f"章立て・概要: {doc['outline']}\n"
        "ルール(厳守):\n"
        "- 日本語で、デモの文脈に即した現実的な内容にする(製品名・数値は架空でよい)。\n"
        "- 見出し(#, ##)を使い、RAG 検索で引けるよう具体的な手順・数値を含める。\n"
        "- 全体で 2000〜8000 文字程度(64KB を超えない)。\n"
        f"{retry}"
    )


def _llm_attempt(prompt: str, total: dict, label: str, attempt: int) -> str | None:
    """1 試行分の LLM 呼び出し。一時失敗(タイムアウト等)は None を返し再試行へ回す
    (codex review-1: APITimeoutError で即 failed になっていた)。usage は成功時のみ加算。"""
    try:
        raw, usage = _llm(prompt)
    except Exception as e:  # noqa: BLE001 — 上流例外の型は問わず同じ有界再試行に収束
        logger.warning("%s llm attempt %d failed: %s", label, attempt + 1, e)
        return None
    _add_usage(total, usage)
    return raw


def _truncate_surplus_rows(table: dict, csv_text: str) -> str:
    """余剰データ行を plan.rows へ決定的に切り詰める(codex review-1: LLM の行数超過が
    再試行でも収束せず failed になる)。不足・ヘッダ不一致・解析不能はそのまま返して
    _validate_csv に判定させる(切り詰めは検証を迂回しない — 型検証は切り詰め後に効く)。"""
    try:
        rows = [r for r in csv.reader(io.StringIO(csv_text))
                if any((c or "").strip() for c in r)]
    except csv.Error:
        return csv_text
    if len(rows) <= 1 + table["rows"]:
        return csv_text
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows[: 1 + table["rows"]])
    return buf.getvalue()


def _generate_table_csv(table: dict, context: str, total: dict) -> str:
    """LLM 生成 → サーバ側検証を有界再試行(§6.1)。合格 CSV を返す。"""
    err = ""
    for attempt in range(MAX_ATTEMPTS):
        raw = _llm_attempt(_table_prompt(table, context, err),
                           total, f"table {table['name']}", attempt)
        if raw is None:
            err = "LLM 呼び出しに失敗しました(一時エラー)"
            continue
        csv_text = _truncate_surplus_rows(table, _strip_fences(raw))
        err = _validate_csv(table, csv_text)
        if not err:
            return csv_text
        logger.warning("table %s csv attempt %d rejected: %s",
                       table["name"], attempt + 1, err)
    raise DataProvisionError(
        f"表 {table['name']} の行データがプラン検証を通りません"
        f"({MAX_ATTEMPTS}回試行): {err}", total,
    )


def _generate_document(doc: dict, context: str, total: dict) -> bytes:
    """LLM Markdown 生成(≤64KB — §6.2)。超過・空・一時失敗は有界再試行。"""
    err = ""
    for attempt in range(MAX_ATTEMPTS):
        raw = _llm_attempt(_doc_prompt(doc, context, err),
                           total, f"document {doc['filename']}", attempt)
        if raw is None:
            err = "LLM 呼び出しに失敗しました(一時エラー)"
            continue
        text = _strip_fences(raw)
        content = text.encode("utf-8")
        if text and len(content) <= DOC_MAX_BYTES:
            return content
        err = ("出力が空です" if not text
               else f"{len(content)} バイトは上限 {DOC_MAX_BYTES} バイト超過(短くする)")
        logger.warning("document %s attempt %d rejected: %s",
                       doc["filename"], attempt + 1, err)
    raise DataProvisionError(
        f"文書 {doc['filename']} を生成できません({MAX_ATTEMPTS}回試行): {err}", total,
    )


def _wait_rag_indexed(namespace: str, added_ids: list[str], total: dict) -> None:
    """投入文書の vector store 索引完了を有界待機する(rag.search が引ける状態まで —
    §9 SP3-04)。failed / タイムアウトは DataProvisionError(§1.3 の再生成で収束)。"""
    if not added_ids:
        return
    ids = set(added_ids)
    deadline = time.monotonic() + RAG_WAIT_TIMEOUT_S
    while True:
        rows = [f for f in rag.list_files(namespace) if f["id"] in ids]
        rows = rag.refresh_statuses(namespace, rows)
        bad = [f["filename"] for f in rows if f["status"] == "failed"]
        if bad:
            raise DataProvisionError(f"文書の索引化に失敗: {', '.join(bad)}", total)
        if len(rows) == len(ids) and all(f["status"] == "completed" for f in rows):
            return
        if time.monotonic() >= deadline:
            raise DataProvisionError(
                f"文書の索引化が {RAG_WAIT_TIMEOUT_S} 秒以内に完了しません", total)
        time.sleep(RAG_WAIT_INTERVAL_S)


# リース/削除競合の制御例外は型を保って伝播する(呼び出し側が 404/503・再試行へ写像する
# 既存契約 — DataProvisionError へ潰すと「demo が消えた」と「生成失敗」を区別できない)
_CONTROL_EXCEPTIONS = (
    demo_lease.DemoGoneError,
    demo_lease.LeaseUnavailableError,
    demo_lease.LeaseTimeoutError,
    demo_lease.LeaseContractError,
)


def provision_data(demo_id: str, plan: dict) -> dict:
    """検証済みプランの data 定義を demo の箱へ投入する(§6)。

    フェーズ1(リース外): 全表 CSV・全文書 Markdown を LLM 生成しサーバ側検証。
    フェーズ2(demo_lease.mutation — 行なし/deleting は DemoGoneError): 同名置換 + 投入。
    フェーズ3(リース外): 文書索引の有界待機。
    失敗は DataProvisionError(usage 込み)へ正規化する(LLM 通信例外・投入先の外部失敗も
    含む — review-2 F002。消費済み usage をエラー経路でも呼び出し側が記録できるように)。
    例外は _CONTROL_EXCEPTIONS のみ型を保って伝播。呼び出し側(SP3-03 ③a)は demo を
    failed にし、usage を実ユーザーに紐づけて記録する。部分投入の残骸は §6.3 の置換 /
    DELETE が回収。
    """
    namespace = f"demo_{demo_id}"  # DemoContext.namespace と同一導出(§6.4)
    total = {"input_tokens": 0, "output_tokens": 0}
    try:
        return _provision(demo_id, namespace, plan, total)
    except DataProvisionError:
        raise
    except _CONTROL_EXCEPTIONS as e:
        # 型は保って伝播しつつ、リース取得前の LLM 生成で消費済みの usage を添付する
        # (codex review-1: DemoGoneError 中止経路で usage_log から欠落していた)
        e.usage = dict(total)
        raise
    except Exception as e:
        raise DataProvisionError(
            f"データ投入に失敗({type(e).__name__}): {str(e)[:400]}", total
        ) from e


def _provision(demo_id: str, namespace: str, plan: dict, total: dict) -> dict:
    context = f"{plan.get('title', '')}: {plan.get('description', '')}"
    data = plan.get("data") or {}
    tables = data.get("tables") or []
    documents = data.get("documents") or []
    result: dict = {"datasets": [], "documents": [], "replaced": 0, "usage": total}

    # フェーズ1: LLM 生成はリースを跨がない(§8.2)
    table_csvs = [(t, _generate_table_csv(t, context, total)) for t in tables]
    doc_contents = [(d, _generate_document(d, context, total)) for d in documents]

    # フェーズ2: 箱に書く区間だけリース保持(mutation = status 再確認込み — §8.2)
    added_ids: list[str] = []
    with demo_lease.mutation(demo_id) as lease:
        if table_csvs:
            # 同名置換(§6.3)。dataset の「名前」= display_name(既存機構の唯一の名前)に
            # プラン表名をそのまま使う(置換キーを決定的にする)。
            existing: dict[str, list[str]] = {}
            for d in datasets.list_datasets(namespace):
                existing.setdefault(d["display_name"], []).append(d["id"])
            for i, (table, csv_text) in enumerate(table_csvs):
                for ds_id in existing.pop(table["name"], []):
                    datasets.delete_dataset(namespace, ds_id, lease=lease)
                    result["replaced"] += 1
                out = datasets.create_dataset(
                    namespace, table["name"], csv_text.encode("utf-8"),
                    warmup=(i == len(table_csvs) - 1), lease=lease,
                    column_types=[c["type"] for c in table["columns"]],
                )
                if i == len(table_csvs) - 1 and not out.get("ready", True):
                    # dbchat が引ける状態が受け入れ条件(§9)。warmup 不達は fail-closed
                    raise DataProvisionError(
                        "dbchat プロファイルの準備が時間内に完了しません(再生成で収束)",
                        total,
                    )
                result["datasets"].append(
                    {"id": out["id"], "name": table["name"],
                     "table_name": out["table_name"], "rows": table["rows"]}
                )
        if doc_contents:
            existing_files: dict[str, list[str]] = {}
            for f in rag.list_files(namespace):
                existing_files.setdefault(f["filename"], []).append(f["id"])
            for doc, content in doc_contents:
                for file_id in existing_files.pop(doc["filename"], []):
                    rag.delete_file(namespace, file_id)  # 外部先行の既存削除(§6.3)
                    result["replaced"] += 1
                out = rag.add_file(namespace, doc["filename"], content, lease=lease)
                added_ids.append(out["id"])
                result["documents"].append(
                    {"id": out["id"], "filename": doc["filename"],
                     "bytes": len(content)}
                )

    # フェーズ3: 索引完了の有界待機(読み取りのみ — リース外)
    _wait_rag_indexed(namespace, added_ids, total)

    logger.info("provisioned data for %s: %d datasets, %d documents (%d replaced)",
                namespace[:48], len(result["datasets"]), len(result["documents"]),
                result["replaced"])
    return result
