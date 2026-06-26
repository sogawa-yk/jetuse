"""AI 組込スロットの実行時バインド機構 (SBA-02)。

sample-app の `aiSlot` は「画面のどこに JetUse のどの能力(capability)を差し込むか」を
**宣言**するだけ(`sample_app.py`)。本モジュールはその宣言を **実行時に具体的なハンドラ
(JetUse コア能力の呼び出し)へ束縛(bind)** し、入力ペイロードを与えて実行する層である。
これが SBA-02 の「AI 組込フレームワーク」の中核——以降のサンプルアプリ(SBA-03..05)も
同じ機構で別 capability を束縛して組み立てる。

設計:
  - **capability → handler レジストリ**(`_HANDLERS`)。`@register_capability("rag.search")` で
    1 能力に 1 ハンドラを登録する。未登録の capability を実行しようとすると
    `UnboundCapabilityError`(その能力はこのステージでは未束縛)。
  - **ハンドラは純粋な関数**: `(SlotContext, payload: dict) -> dict`。副作用(DB 書込)を持たず、
    LLM 呼び出しは差し替え可能な `_completer`(既定=`chat.complete_once`)経由にして、単体テストが
    OCI へ出ずに検証できるようにする。
  - **知識コーパスは文脈(SlotContext.corpus)で渡す**: RAG/返信ドラフトは sample-app 自身の
    シードデータ(例: FAQ)を根拠にする。これにより「業務アプリのデータに AI を組み込む」型を
    そのまま実現する。取り出し(retrieval)は外部ベクトルストアに依存しない軽量な語彙重なり
    スコア(日本語は文字バイグラム併用)で、実環境では GenAI 推論のみで安定して動く。

SBA-02 が束縛する能力: `rag.search`(FAQ-RAG 回答) / `summarize`(要約) / `classify`(自動分類) /
`draft`(返信ドラフト)。SBA-03(SBA-B 在庫・受発注照会)で `nl2sql`(自然言語DB照会) /
`chart`(結果グラフ化)を束縛する。`agent`/`minutes`/`vlm.ocr` は SBA-04..05 で束縛する。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jetuse_shared.charting import propose_chart
from jetuse_shared.sqlguard import (
    SqlRejectedError,
    assert_tables_allowed,
    sanitize_sql,
    strip_code_fences,
)

from .manifest import PluginManifest
from .sample_app import (
    AiSlot,
    SampleAppDefinition,
    SampleAppError,
    validate_sample_app,
)

# invoke_slot を model_key 指定なしで直接呼ぶ場合の**ライブラリ既定**(models.DEFAULT_MODEL と一致)。
# API ルート経由の実効既定は `settings.sample_app_model`(既定 llama-3.3-70b / project_ocid 不要)で、
# ルートは常に model_key を明示して呼ぶためここには依存しない。両者の差は意図的(役割が異なる)。
DEFAULT_MODEL = "gpt-oss-120b"

#: 1 スロット呼び出しで取り出す知識コーパス行の既定上限。
DEFAULT_TOP_K = 3
MAX_TOP_K = 10
#: RAG の grounded 判定に要する関連度の下限。
#: 関連度 = overlap 係数 = 一致特徴数 / min(質問特徴数, FAQ特徴数)。
#: 偶発的な少数バイグラム一致（無関係入力）を grounded から除外しつつ、近い言い換え/短い質問は通す。
MIN_RAG_RELEVANCE = 0.2
#: 引用は最上位ヒットの関連度に対しこの割合以上のものだけに絞る（弱い随伴一致を引用から除外）。
RAG_CITATION_TOP_FRACTION = 0.6
#: LLM へ渡す入力本文の上限(暴走/過大入力の予防)。
MAX_INPUT_CHARS = 8000
#: ハンドラ出力(本文)の文字数上限。
MAX_ANSWER_CHARS = 4000
MAX_DRAFT_CHARS = 4000
MAX_SUMMARY_CHARS = 2000
MAX_CATEGORY_CHARS = 200
#: nl2sql 生成 SQL の文字数上限(暴走生成の予防)。
MAX_SQL_CHARS = 4000
#: chart 提案へ渡す結果列・行の上限(プロンプト肥大の予防)。
MAX_CHART_COLUMNS = 50
MAX_CHART_ROWS = 50
#: chart へ渡す 1 セルの文字数上限(巨大セルによるプロンプト膨張/コスト暴走の予防)。
MAX_CHART_CELL_CHARS = 200
#: classify の候補カテゴリの件数上限と1ラベルの長さ上限(プロンプト肥大/コスト暴走の予防)。
MAX_CATEGORIES = 30
MAX_CATEGORY_LABEL = 100


class UnboundCapabilityError(SampleAppError):
    """要求された capability にハンドラが束縛されていないときに送出する。"""


class SlotInputError(SampleAppError):
    """スロット呼び出しの入力ペイロードが不正なときに送出する。"""


class SlotInferenceError(SampleAppError):
    """LLM が空応答を返す等、推論結果が成立しないときに送出する(成功偽装を防ぐ)。"""


@dataclass
class SlotContext:
    """1 回のスロット実行の文脈。

    `corpus` はこのスロットが根拠にできる知識行(sample-app のシード由来。例: FAQ 行)。
    RAG/draft はこれを検索して根拠にする。classify/summarize は入力本文を主に使う。
    """

    owner: str
    slot: AiSlot
    definition: SampleAppDefinition
    corpus: list[dict[str, Any]] = field(default_factory=list)
    model_key: str = DEFAULT_MODEL


CapabilityHandler = Callable[[SlotContext, dict[str, Any]], dict[str, Any]]

_HANDLERS: dict[str, CapabilityHandler] = {}


def register_capability(capability: str) -> Callable[[CapabilityHandler], CapabilityHandler]:
    """capability にハンドラを束縛するデコレータ。同一能力の二重登録は禁止。"""

    def deco(fn: CapabilityHandler) -> CapabilityHandler:
        if capability in _HANDLERS:
            raise ValueError(f"capability '{capability}' は既に束縛済み")
        _HANDLERS[capability] = fn
        return fn

    return deco


def capability_handler(capability: str) -> CapabilityHandler | None:
    """capability に束縛されたハンドラを返す(無ければ None)。"""
    return _HANDLERS.get(capability)


def bound_capabilities() -> set[str]:
    """現在束縛済みの capability 集合(このステージで実行可能な AI 能力)。"""
    return set(_HANDLERS)


# --- LLM 呼び出し(差し替え可能) ------------------------------------------


def _default_completer(model_key: str, messages: list[dict[str, Any]], max_chars: int) -> str:
    # 遅延 import: openai 依存を import 時に持ち込まない(単体テストは _completer 差し替え)。
    from ..chat import complete_once

    return complete_once(model_key, messages, max_chars=max_chars)


#: テストは `ai_runtime._completer = fake` で差し替える。
_completer: Callable[[str, list[dict[str, Any]], int], str] = _default_completer


def _complete(ctx: SlotContext, system: str, user: str, *, max_chars: int) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return (_completer(ctx.model_key, messages, max_chars) or "").strip()


# --- 入力ヘルパ -----------------------------------------------------------


def _require_input(payload: dict[str, Any]) -> str:
    """ペイロードから入力本文を取り出す(`input` または `text`/`question`)。"""
    raw = payload.get("input") or payload.get("text") or payload.get("question")
    if not isinstance(raw, str) or not raw.strip():
        raise SlotInputError("input(本文)は非空の文字列でなければならない")
    return raw.strip()[:MAX_INPUT_CHARS]


def _top_k(payload: dict[str, Any]) -> int:
    k = payload.get("top_k", DEFAULT_TOP_K)
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        return DEFAULT_TOP_K
    return min(k, MAX_TOP_K)


# --- 検索(retrieval) ------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]{2,}")
# CJK(漢字・かな)を文字バイグラム化する対象。空白で区切られない日本語に効かせる。
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")


#: 日本語の丁寧表現・接続辞由来の高頻度バイグラム。これらは内容語ではなく FAQ 回答文に普遍的に
#: 出現して無関係 FAQ を不当に上位化する(例:「〜してください」の「ください」を含む別 FAQ)。
#: 検索特徴から除外し、内容語バイグラムでの一致に集中させる。
_STOP_BIGRAMS = frozenset(
    {
        "くだ", "ださ", "さい",
        "です", "ます", "ませ", "した", "すか",
        "して", "する", "でき", "きる", "され", "れる",
        "てい", "いる", "てく", "もら",
        "につ", "いて", "ても", "から", "こと", "など", "のか", "ない", "ため",
        "教え", "えて",
    }
)


def _features(text: str) -> set[str]:
    """語彙特徴集合。ASCII 単語トークン ∪ CJK 文字バイグラム。

    CJK は **1 文字単体を特徴に含めない**(「て」「い」「す」等の高頻度文字だけで無関係な
    問い合わせが FAQ にヒットし、誤って grounded 扱いになるのを防ぐ)。判別力のあるバイグラムのみ。
    """
    low = text.lower()
    feats: set[str] = set(_WORD_RE.findall(low))
    cjk = _CJK_RE.findall(low)
    feats.update(
        bg
        for a, b in zip(cjk, cjk[1:], strict=False)
        if (bg := a + b) not in _STOP_BIGRAMS
    )
    return feats


def _row_text(row: dict[str, Any]) -> str:
    """行の検索対象テキスト(文字列値のみ連結)。"""
    return " ".join(str(v) for v in row.values() if isinstance(v, str))


def _row_label(row: dict[str, Any]) -> str:
    """引用ラベル。question/title/subject/name を優先、無ければ最初の文字列値。"""
    for key in ("question", "title", "subject", "name"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in row.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _relevant_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """grounded/引用に値する hit に絞る。

    まず絶対下限(MIN_RAG_RELEVANCE)を満たすものに限り、さらに最上位の関連度に対する相対割合
    (RAG_CITATION_TOP_FRACTION)以上のものだけ残す。これにより「強い1件＋弱い随伴一致」のとき
    弱い方を引用から外し、引用精度を上げる。
    """
    strong = [h for h in hits if h["relevance"] >= MIN_RAG_RELEVANCE]
    if not strong:
        return []
    top = max(h["relevance"] for h in strong)
    floor = max(MIN_RAG_RELEVANCE, top * RAG_CITATION_TOP_FRACTION)
    return [h for h in strong if h["relevance"] >= floor]


def retrieve(
    query: str, corpus: list[dict[str, Any]], *, top_k: int = DEFAULT_TOP_K
) -> list[dict[str, Any]]:
    """コーパスから query に関連する行を上位 top_k 件返す(スコア>0 のみ)。

    返り値の各要素: `{"index", "score", "relevance", "label", "row"}`。`relevance` は overlap 係数
    (一致特徴数 / min(質問,行 の特徴数))で、語数差に左右されにくい関連度の目安。外部ベクトルストアに
    依存しない軽量スコアで、実環境では GenAI 推論のみで安定動作する。
    """
    if not corpus:
        return []
    q = _features(query)
    if not q:
        return []
    scored: list[tuple[int, float, int, dict[str, Any]]] = []
    for i, row in enumerate(corpus):
        row_feats = _features(_row_text(row))
        score = len(q & row_feats)
        denom = min(len(q), len(row_feats)) or 1
        scored.append((score, score / denom, i, row))
    # relevance を主キーに並べる(score は同 relevance 内の同点処理)。raw score だけで並べて
    # top_k で切ると、冗長な行の偶発的な多一致が枠を占め、短く高 relevance な真の一致が
    # top_k 外へ落ちて grounded=False になり得る。relevance 優先なら真の一致が枠に残る。
    scored.sort(key=lambda t: (-t[1], -t[0], t[2]))
    out: list[dict[str, Any]] = []
    for score, relevance, i, row in scored[:top_k]:
        if score <= 0:
            break
        out.append(
            {
                "index": i,
                "score": score,
                "relevance": round(relevance, 3),
                "label": _row_label(row),
                "row": row,
            }
        )
    return out


# --- ハンドラ: rag.search(FAQ-RAG 回答) ----------------------------------

_RAG_SYSTEM = (
    "あなたは社内サポート担当アシスタントです。以下の参考FAQのみを根拠に、"
    "日本語で簡潔・丁寧に回答してください。参考FAQに無い事項は推測せず、"
    "「参考情報からは判断できません」と述べてください。"
)


@register_capability("rag.search")
def handle_rag_search(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """FAQ コーパスを検索し、根拠付きの回答を生成する(RAG)。"""
    question = _require_input(payload)
    # 関連度ゲート＋相対絞り込みで、無関係入力の偶発一致や弱い随伴一致を引用・根拠から外す。
    hits = _relevant_hits(retrieve(question, ctx.corpus, top_k=_top_k(payload)))
    if not hits:
        return {
            "capability": "rag.search",
            "answer": "参考FAQから関連する情報が見つかりませんでした。",
            "citations": [],
            "grounded": False,
        }
    context = "\n\n".join(
        f"[{i + 1}] {h['label']}\n{_row_text(h['row'])}" for i, h in enumerate(hits)
    )
    answer = _complete(
        ctx,
        _RAG_SYSTEM,
        f"参考FAQ:\n{context}\n\n質問: {question}\n\n上記FAQを根拠に回答してください。",
        max_chars=MAX_ANSWER_CHARS,
    )
    if not answer:
        # 空応答を grounded=True/空 answer の「成功」に偽装しない(推論失敗として扱う)。
        raise SlotInferenceError("rag.search: LLM が空応答を返した")
    citations = [
        {"index": h["index"], "label": h["label"], "score": h["score"]} for h in hits
    ]
    return {
        "capability": "rag.search",
        "answer": answer,
        "citations": citations,
        "grounded": True,
    }


# --- ハンドラ: classify(自動分類) ----------------------------------------


def _categories(ctx: SlotContext, payload: dict[str, Any]) -> list[str]:
    """分類カテゴリ。payload.categories 優先、無ければコーパスの `category` 値から導出。"""
    given = payload.get("categories")
    if isinstance(given, list):
        # runtime を直接呼ぶ経路(ルート層のバリデーションを経ない)でも全件走査しないよう、
        # 入力リスト自体を上限の手前で切る(ルート層は max_length で 422 にする)。
        cats = [
            c.strip()[:MAX_CATEGORY_LABEL]
            for c in given[:MAX_CATEGORIES]
            if isinstance(c, str) and c.strip()
        ]
        if cats:
            # 重複排除しつつ順序保持し、件数上限で切り詰める(プロンプト肥大の予防)。
            return list(dict.fromkeys(cats))[:MAX_CATEGORIES]
    derived: list[str] = []
    for row in ctx.corpus:
        c = row.get("category")
        if isinstance(c, str) and c.strip() and c.strip() not in derived:
            derived.append(c.strip()[:MAX_CATEGORY_LABEL])
        if len(derived) >= MAX_CATEGORIES:
            break
    return derived


@register_capability("classify")
def handle_classify(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """入力本文を、与えられた/導出したカテゴリのいずれか1つに分類する。"""
    text = _require_input(payload)
    cats = _categories(ctx, payload)
    if not cats:
        raise SlotInputError(
            "categories が未指定で、コーパスからも導出できない(category 列が無い)"
        )
    cat_list = " / ".join(cats)
    raw = _complete(
        ctx,
        "あなたは問い合わせ分類器です。本文を、提示されたカテゴリの中から最も適切な1つに"
        "分類します。出力はカテゴリ名そのものだけを返し、説明や記号を付けないこと。",
        f"カテゴリ候補: {cat_list}\n\n本文:\n{text}\n\n"
        "最も適切なカテゴリ名を1つだけ出力してください。",
        max_chars=MAX_CATEGORY_CHARS,
    )
    category, matched = _match_category(raw, cats)
    return {
        "capability": "classify",
        "category": category,
        # LLM 出力が候補に一致しなかった(=先頭フォールバック)ときは matched=False で
        # 「自信のある分類」と取り違えられないようにする(UI が低信頼を示せる)。
        "matched": matched,
        "candidates": cats,
        "raw": raw,
    }


def _match_category(raw: str, cats: list[str]) -> tuple[str, bool]:
    """LLM 出力を候補カテゴリへ正規化する(完全一致→包含→先頭フォールバック)。

    返り値 `(category, matched)`。matched=False は候補に一致せず先頭へフォールバックしたこと。
    """
    s = raw.strip()
    if not s:
        # 空/空白応答は「一致」ではない(空文字は任意の文字列の部分列になり誤一致するため明示弾く)。
        return cats[0], False
    low = s.lower()
    for c in cats:
        if s == c:
            return c, True
    for c in cats:
        cl = c.lower()
        if cl and (cl in low or low in cl):
            return c, True
    return cats[0], False


# --- ハンドラ: summarize(要約) -------------------------------------------


@register_capability("summarize")
def handle_summarize(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """入力本文(長い問い合わせ・スレッド)を要約する。"""
    text = _require_input(payload)
    summary = _complete(
        ctx,
        "あなたは問い合わせ内容を正確に整理する日本語アシスタントです。"
        "要点・依頼事項・期限/緊急度が分かるよう簡潔に要約してください。",
        f"次の問い合わせ内容を3〜5行で要約してください。要約のみ出力:\n\n{text}",
        max_chars=MAX_SUMMARY_CHARS,
    )
    if not summary:
        raise SlotInferenceError("summarize: LLM が空応答を返した")
    return {"capability": "summarize", "summary": summary}


# --- ハンドラ: draft(返信ドラフト) ---------------------------------------


@register_capability("draft")
def handle_draft(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """問い合わせに対する返信ドラフトを、FAQ を根拠にしつつ生成する。"""
    inquiry = _require_input(payload)
    # 返信ドラフトも関連度の低い偶発一致・弱い随伴一致は根拠にしない(無関係 FAQ を引用しない)。
    hits = _relevant_hits(retrieve(inquiry, ctx.corpus, top_k=_top_k(payload)))
    context = (
        "\n\n".join(f"[{i + 1}] {h['label']}\n{_row_text(h['row'])}" for i, h in enumerate(hits))
        or "(該当する参考FAQはありません)"
    )
    draft = _complete(
        ctx,
        "あなたはカスタマーサポート担当です。丁寧な日本語で、問い合わせへの返信文の下書きを"
        "作成します。参考FAQに根拠がある場合はそれに沿い、無い場合は確認する旨を書きます。"
        "宛名・挨拶・本文・結びを含めること。",
        f"参考FAQ:\n{context}\n\nお客様からの問い合わせ:\n{inquiry}\n\n返信ドラフトを作成してください。",
        max_chars=MAX_DRAFT_CHARS,
    )
    if not draft:
        raise SlotInferenceError("draft: LLM が空応答を返した")
    citations = [
        {"index": h["index"], "label": h["label"], "score": h["score"]} for h in hits
    ]
    return {"capability": "draft", "draft": draft, "citations": citations}


# --- ハンドラ: nl2sql(自然言語DB照会 / SBA-B) -----------------------------

#: FieldType → 表示用 SQL 型(プロンプトの schema 文脈。実 DDL ではなく LLM への手掛かり)。
_SQL_TYPE = {
    "string": "VARCHAR2",
    "text": "CLOB",
    "number": "NUMBER",
    "boolean": "NUMBER(1)",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}

_NL2SQL_SYSTEM = (
    "あなたは熟練のデータアナリストです。提示されたテーブルスキーマだけを根拠に、"
    "ユーザーの日本語の質問に答える Oracle SQL の SELECT 文を1つだけ生成します。\n"
    "規則: (1) 読取専用の SELECT(または WITH …) 文のみ。INSERT/UPDATE/DELETE/DDL など"
    "更新系は一切禁止。(2) スキーマに存在するテーブル名・列名のみ使用する。"
    "(3) 説明・コメント・コードフェンスを付けず、SQL 本文だけを返す。"
    "(4) 集計(SUM/COUNT/AVG)・グループ化・並べ替え・期間絞り込みは質問に応じて適切に行う。"
    "(5) 文末にセミコロンを付けない。"
)


def _schema_context(definition: SampleAppDefinition) -> str:
    """sample-app の datasets を NL2SQL プロンプト用のテーブルスキーマ記述へ変換する。

    テーブル名 = dataset 名(大文字)、列 = field 名(大文字)+型+ラベル。実行時の対象 DB
    (E2E では JETUSE_SBA03)は同名のテーブルを持つ前提。スキーマ記述は LLM への文脈であり、
    生成 SQL は実行前に sanitize_sql と読取専用接続で多層ガードされる(SQL-02 を緩めない)。
    """
    lines: list[str] = []
    for ds in definition.datasets:
        cols = ", ".join(_column_desc(f) for f in ds.fields)
        label = f"  -- {ds.label}" if ds.label else ""
        lines.append(f"TABLE {ds.name.upper()} ({cols}){label}")
    return "\n".join(lines)


def _column_desc(field_def: Any) -> str:
    sql_type = _SQL_TYPE.get(field_def.type, "VARCHAR2")
    label = f' /* {field_def.label} */' if field_def.label else ""
    return f"{field_def.name.upper()} {sql_type}{label}"


@register_capability("nl2sql")
def handle_nl2sql(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """自然言語の質問から、sample-app の datasets スキーマに対する読取専用 SELECT を生成する。

    生成だけを担い実行はしない(実行は読取専用ユーザー経由の別経路 = SQL-02 のガードを流用)。
    生成 SQL は返す前に sanitize_sql で SELECT 限定ガードに通し、適合しなければ成功偽装せず
    推論失敗として扱う(SqlRejectedError → SlotInferenceError)。
    """
    question = _require_input(payload)
    if not ctx.definition.datasets:
        raise SlotInputError("nl2sql: 照会対象の dataset が定義に無い")
    schema = _schema_context(ctx.definition)
    raw = _complete(
        ctx,
        _NL2SQL_SYSTEM,
        f"テーブルスキーマ:\n{schema}\n\n質問: {question}\n\nSELECT文のみを返してください。",
        max_chars=MAX_SQL_CHARS,
    )
    sql = strip_code_fences(raw)
    if not sql:
        raise SlotInferenceError("nl2sql: LLM が空応答を返した")
    allowed = {ds.name.upper() for ds in ctx.definition.datasets}
    try:
        cleaned = sanitize_sql(sql)
        # 生成 SQL を sample-app の定義スキーマ(datasets)内に閉じる。別スキーマ/辞書ビュー
        # (例 SYS.DBA_USERS)への SELECT を、読取専用ユーザー権限に加えてコード側でも拒否する。
        # 列スコープは「テーブル粒度」で閉じる: 対象 DB のテーブルは dataset.fields から 1:1 で
        # 生成され(scaffold / E2E setup)、定義外の列が物理的に存在しない。よって許可テーブル内に
        # 留めれば定義外の列は露出しない(列単位の SQL パースは誤判定が多く採らない / M2)。
        assert_tables_allowed(cleaned, allowed)
    except SqlRejectedError as e:
        # 生成 SQL がガード(SELECT 以外/複数文/更新系/許可外テーブル)に反した。
        # 成功偽装せず推論失敗にする。
        raise SlotInferenceError(f"nl2sql: 生成SQLがガードに適合しない: {e}") from e
    return {"capability": "nl2sql", "sql": cleaned}


# --- ハンドラ: chart(結果のグラフ化 / SBA-B) ------------------------------


@register_capability("chart")
def handle_chart(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """SQL 実行結果(columns/rows)に最適なグラフ仕様(ChartSpec)を提案する。

    提案・検証ロジックは jetuse_shared.charting.propose_chart に一本化(DBチャット
    /api/dbchat/chart と同一)。列名の実在チェックで不適な提案は type="none" に落とす。
    """
    question = (payload.get("question") or payload.get("input") or "").strip()
    # 列数・行数・行幅・1セル長をすべて上限で切り、プロンプト規模を入力に依らず有界化する。
    columns = [
        str(c)[:MAX_CHART_CELL_CHARS] for c in (payload.get("columns") or [])
    ][:MAX_CHART_COLUMNS]
    rows = [
        [str(c)[:MAX_CHART_CELL_CHARS] for c in r[:MAX_CHART_COLUMNS]]
        for r in (payload.get("rows") or [])
        if isinstance(r, list)
    ][:MAX_CHART_ROWS]
    spec = propose_chart(
        lambda prompt: _completer(ctx.model_key, [{"role": "user", "content": prompt}], 1000),
        question,
        columns,
        rows,
    )
    return {"capability": "chart", **spec}


# --- 公開 API: 束縛と実行 -------------------------------------------------


def bind_slot(
    definition: SampleAppDefinition, slot_key: str
) -> tuple[AiSlot, CapabilityHandler]:
    """slot_key の aiSlot を解決し、その capability のハンドラへ束縛して返す。

    - slot_key が定義に無ければ `SampleAppError`。
    - capability にハンドラが無ければ `UnboundCapabilityError`。
    """
    slot = next((s for s in definition.ai_slots if s.key == slot_key), None)
    if slot is None:
        raise SampleAppError(f"aiSlot '{slot_key}' が定義に存在しない")
    handler = _HANDLERS.get(slot.capability)
    if handler is None:
        raise UnboundCapabilityError(
            f"capability '{slot.capability}'(slot '{slot_key}')は未束縛"
        )
    return slot, handler


def invoke_slot(
    definition: SampleAppDefinition,
    slot_key: str,
    payload: dict[str, Any],
    *,
    owner: str,
    corpus: list[dict[str, Any]] | None = None,
    model_key: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """aiSlot を実行時バインドして実行し、結果 dict を返す。

    呼び出し側(ルート層)は `corpus`(知識行=シード由来)を与える。本関数はハンドラを解決して
    `SlotContext` を組み立て、ハンドラを実行するだけの薄い実行器(maker/checker 分離の maker 側)。
    """
    slot, handler = bind_slot(definition, slot_key)
    ctx = SlotContext(
        owner=owner,
        slot=slot,
        definition=definition,
        corpus=list(corpus or []),
        model_key=model_key,
    )
    result = handler(ctx, payload)
    result.setdefault("slot", slot_key)
    return result


def unbound_capabilities(
    source: PluginManifest | SampleAppDefinition,
) -> list[str]:
    """この sample-app の aiSlots のうち、ハンドラ未束縛の capability を列挙する。

    合成バリデーションとは別に「このステージの実行時フレームワークで実際に動かせるか」を点検する。
    """
    definition = source if isinstance(source, SampleAppDefinition) else validate_sample_app(source)
    return sorted(
        {s.capability for s in definition.ai_slots if s.capability not in _HANDLERS}
    )
