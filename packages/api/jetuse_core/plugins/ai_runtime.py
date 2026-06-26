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
`draft`(返信・メール下書き)。
SBA-04(SBA-C 営業案件管理)が追加束縛する能力: `minutes`(議事録要約) / `agent`(次アクション提案
エージェント) / `nl2sql`(売上集計 自然言語DB照会)。`chart`/`vlm.ocr` は他ステージで束縛する。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

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
#: classify の候補カテゴリの件数上限と1ラベルの長さ上限(プロンプト肥大/コスト暴走の予防)。
MAX_CATEGORIES = 30
MAX_CATEGORY_LABEL = 100
#: minutes(議事録要約)・agent(次アクション提案)の出力上限。
MAX_MINUTES_CHARS = 4000
MAX_AGENT_CHARS = 3000
#: agent が返す次アクションの最大件数(暴走出力の抑制)。
MAX_AGENT_ACTIONS = 12
#: nl2sql 結果プレビューの行/セル上限(過大応答の抑制。実行層 nl2sql.py の上限とは別の表示制約)。
MAX_NL2SQL_PREVIEW_ROWS = 50


class UnboundCapabilityError(SampleAppError):
    """要求された capability にハンドラが束縛されていないときに送出する。"""


class SlotInputError(SampleAppError):
    """スロット呼び出しの入力ペイロードが不正なときに送出する。"""


class SlotInferenceError(SampleAppError):
    """LLM が空応答を返す等、推論結果が成立しないときに送出する(成功偽装を防ぐ)。"""


class SlotBackendUnavailableError(SampleAppError):
    """DB 等のバックエンドが一時的に利用不可のときに送出する(ルートで 503 に写像)。

    生成SQLの不正(列不正等)や推論失敗(502)とは区別する——前者はリトライで回復しうる
    一過性の障害なので、利用者に「一時的に利用不可」(503)として伝える。
    """


@dataclass
class SlotContext:
    """1 回のスロット実行の文脈。

    `corpus` はこのスロットが根拠にできる知識行(sample-app のシード由来。例: FAQ 行)。
    RAG/draft はこれを検索して根拠にする。classify/summarize は入力本文を主に使う。
    `nl2sql_schema` は nl2sql スロットが照会する実 DB スキーマ名(例: JETUSE_SBA04)。
    sample-app が業務データを実 ADB の専用スキーマに隔離して持つ場合に与える(SBA-C 売上集計)。
    """

    owner: str
    slot: AiSlot
    definition: SampleAppDefinition
    corpus: list[dict[str, Any]] = field(default_factory=list)
    model_key: str = DEFAULT_MODEL
    nl2sql_schema: str | None = None


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
    """ドラフト(返信/メール下書き)を生成する。

    知識コーパス(FAQ 等)がある場合(SBA-A サポート返信)は FAQ を根拠にしたカスタマーサポート
    返信を生成する。コーパスが空の場合(SBA-C 営業フォローメール)は **FAQ を前提にしない**中立な
    ビジネスメール下書きとして、入力(案件情報・次アクション・売上参考等)だけを根拠に作成する
    ——営業メールに「参考FAQはございません」のような不適切な文言が混入しないようにする。
    """
    body = _require_input(payload)
    # コーパスがある時だけ FAQ 根拠を引く(関連度の低い偶発一致は除外)。空コーパスでは検索しない。
    hits = (
        _relevant_hits(retrieve(body, ctx.corpus, top_k=_top_k(payload)))
        if ctx.corpus
        else []
    )
    if ctx.corpus:
        context = (
            "\n\n".join(
                f"[{i + 1}] {h['label']}\n{_row_text(h['row'])}" for i, h in enumerate(hits)
            )
            or "(該当する参考FAQはありません)"
        )
        draft = _complete(
            ctx,
            "あなたはカスタマーサポート担当です。丁寧な日本語で、問い合わせへの返信文の下書きを"
            "作成します。参考FAQに根拠がある場合はそれに沿い、無い場合は確認する旨を書きます。"
            "宛名・挨拶・本文・結びを含めること。",
            f"参考FAQ:\n{context}\n\nお客様からの問い合わせ:\n{body}\n\n返信ドラフトを作成してください。",
            max_chars=MAX_DRAFT_CHARS,
        )
    else:
        draft = _complete(
            ctx,
            "あなたは法人営業の担当者です。丁寧な日本語のビジネスメール下書きを作成します。"
            "提示された案件情報・次アクション・参考データのみを根拠にし、無い情報は創作しないこと。"
            "件名・宛名・挨拶・本文(次アクションを自然に織り込む)・結びを含め、過度な約束はしないこと。",
            f"以下をもとに、顧客向けフォローメールの下書きを作成してください。\n\n{body}",
            max_chars=MAX_DRAFT_CHARS,
        )
    if not draft:
        raise SlotInferenceError("draft: LLM が空応答を返した")
    citations = [
        {"index": h["index"], "label": h["label"], "score": h["score"]} for h in hits
    ]
    return {"capability": "draft", "draft": draft, "citations": citations}


# --- ハンドラ: minutes(議事録要約) ---------------------------------------

_MINUTES_SYSTEM = (
    "あなたは会議内容を正確に整理する日本語アシスタントです。"
    "議事録の生テキスト(発言録・メモ)から、決定事項・課題・次アクション材料を"
    "後工程(次アクション提案・メール下書き)が使いやすい形で構造化して要約します。"
    "テキストに無い事実を創作しないこと。"
)


@register_capability("minutes")
def handle_minutes(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """議事録の生テキストを構造化要約する(VOICE-01 の整形プロンプトを業務文脈へ流用)。

    出力は次アクション提案(agent)・メール下書き(draft)が連動して使える Markdown 要約。
    """
    text = _require_input(payload)
    summary = _complete(
        ctx,
        _MINUTES_SYSTEM,
        "次の会議メモ/発言録を Markdown で要約してください。"
        "構成: ## 要点 / ## 決定事項 / ## 懸念・論点 / "
        "## 次アクション候補(担当/期限が読み取れれば付記)。\n\n"
        f"{text}",
        max_chars=MAX_MINUTES_CHARS,
    )
    if not summary:
        raise SlotInferenceError("minutes: LLM が空応答を返した")
    return {"capability": "minutes", "summary": summary}


# --- ハンドラ: agent(次アクション提案エージェント) -----------------------

_AGENT_SYSTEM = (
    "あなたは営業案件の担当者を支援する次アクション提案エージェントです。"
    "案件情報と議事録要約から、案件を前進させるための具体的な次アクションを"
    "優先度順に提案します。各アクションは1行で、可能なら「[期限] 行動 — 狙い」の形式にし、"
    "実在の情報のみに基づくこと(創作しない)。"
    "**期限は入力に明示された場合のみ書き、勝手に絶対日付(YYYY-MM-DD 等)を作らないこと。"
    "明示が無ければ「次回会議まで」「今週中」「期限未定」など相対表現か未定とする。**"
)


#: 日付トークンの **単一パス** 検出。年あり(YYYY-MM-DD / YYYY/MM/DD / YYYY年M月D日)→
#: 年なし和式(M月D日)の順で alternation し、各位置で年ありを先に試すことで年あり日付の内部に
#: 年なしが二重マッチしないようにする。**年なし slash(`1/2`)は分数・比率・数量表現(「3/4ライン」
#: 「10/12件」等)と曖昧なため対象にしない**(誤って期限未定へ中和してアクションの意味を壊さない)。
_DATE_ANY_RE = re.compile(
    r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"  # 年あり
    r"|(\d{1,2})\s*月\s*(\d{1,2})\s*日"                          # 年なし(和式)
)


def _norm_date_match(m: re.Match) -> str:
    """日付マッチを比較用キーへ正規化(年あり=`YYYY-MM-DD`、年なし和式=`MM-DD`、ゼロ埋め)。"""
    g = m.groups()
    if g[0] is not None:  # 年あり
        return f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}"
    return f"{int(g[3]):02d}-{int(g[4]):02d}"  # 年なし(和式)


def _allowed_date_keys(source: str) -> set[str]:
    """入力に出現する日付の正規化キー集合(これらは創作でないので保持を許す)。"""
    return {_norm_date_match(m) for m in _DATE_ANY_RE.finditer(source)}


def _strip_invented_dates(actions: list[str], source: str) -> list[str]:
    """入力に存在しない日付(年あり/年なし)を次アクションから除去する(LLM の期限創作を防ぐ)。

    入力(案件情報＋議事録要約)に出現する日付は許可し、それ以外の日付トークンは「(期限未定)」に
    置換する(単一パス・非重複の span 処理)。相対表現(今週中/次回会議まで 等)は対象外。「創作しない」
    主張を決定的に裏づける後処理ガード(プロンプト指示と二重化)。表記揺れはゼロ埋め正規化で同一視。
    """
    allowed = _allowed_date_keys(source)
    return [
        _DATE_ANY_RE.sub(
            lambda m: m.group(0) if _norm_date_match(m) in allowed else "(期限未定)", a
        )
        for a in actions
    ]


def _parse_actions(raw: str) -> list[str]:
    """LLM 出力(箇条書き/番号付き)を次アクション行の配列へ寛容にパースする。

    先頭の番号・記号(`1.` `-` `・` `*`)を除いた非空行を上限件数まで採る。1 行も取れなければ
    全体を 1 アクションとして返す(空応答は handle_agent 側が推論失敗として扱う)。
    """
    actions: list[str] = []
    for line in raw.splitlines():
        s = re.sub(r"^\s*(?:\d+[.)]|[-*・●▪])\s*", "", line).strip()
        if s:
            actions.append(s[:300])
        if len(actions) >= MAX_AGENT_ACTIONS:
            break
    if not actions:
        flat = raw.strip()
        return [flat[:300]] if flat else []
    return actions


@register_capability("agent")
def handle_agent(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """案件情報＋議事録要約から次アクションを提案する(AGT 系の宣言型エージェントを組込点に流用)。"""
    context = _require_input(payload)
    raw = _complete(
        ctx,
        _AGENT_SYSTEM,
        "次の案件情報・議事録要約をもとに、優先度の高い順に次アクションを箇条書き"
        f"(最大{MAX_AGENT_ACTIONS}件)で提案してください。提案のみ出力:\n\n{context}",
        max_chars=MAX_AGENT_CHARS,
    )
    if not raw:
        raise SlotInferenceError("agent: LLM が空応答を返した")
    actions = _strip_invented_dates(_parse_actions(raw), context)
    # 公開レスポンスは **sanitize 済み actions のみ**。`text` も actions から再構成する(raw を
    # そのまま返すと創作絶対日付が date-strip を素通りして表に出るため)。raw は公開面に載せない
    # (必要ならサーバ側 audit/log にだけ残す設計とし、外部応答には含めない)。
    return {
        "capability": "agent",
        "actions": actions,
        "text": "\n".join(actions),
    }


# --- ハンドラ: nl2sql(売上集計 自然言語DB照会) ---------------------------


def _default_nl2sql(
    question: str, *, schema: str, tables: list[str], model_key: str
) -> dict[str, Any]:
    """既定の NL2SQL 実行器。実 ADB の指定スキーマに対し Select AI で SQL 生成→読取専用実行。

    遅延 import(oracledb/httpx 依存を import 時に持ち込まない)。単体テストは
    `ai_runtime._nl2sql_runner` を差し替えて DB に出ずに検証する。`tables` で参照可能表を絞る。
    `model_key`(スロットのモデル選択)は Select AI へ伝播する(NL2SQL だけモデル指定を無視しない。
    Select AI 側は `resolve_select_ai_model` で自前 allowlist に正規化し、未知キーは既定へ戻す)。
    """
    from .. import nl2sql

    return nl2sql.run_nl2sql_for_schema(
        question, schema=schema, tables=tables, model=model_key
    )


#: テストは `ai_runtime._nl2sql_runner = fake` で差し替える。
_nl2sql_runner: Callable[..., dict[str, Any]] = _default_nl2sql

#: DB 接続/可用性に起因するエラーの目印(これは隔離破りでなく一過性のため 503 へ通す)。
_DB_CONNECTION_MARKERS = (
    "DPY-6005", "DPY-4011", "DPY-4005", "ORA-12541", "ORA-12170",
    "ORA-03113", "ORA-03114", "ORA-01017", "ORA-12514", "ORA-12537",
)


def _slot_tables(ctx: SlotContext) -> list[str]:
    """このスロットが参照を許可される dataset(テーブル)名を、載っている screen から導出する。

    スロット別に面を絞る(売上集計スロットは売上 dataset のみ等)。複数 screen に載る場合は和集合。
    """
    names: list[str] = []
    for screen in ctx.definition.screens:
        if ctx.slot.key in screen.slots and screen.dataset:
            if screen.dataset not in names:
                names.append(screen.dataset)
    return names


def _is_oracledb_error(e: BaseException) -> bool:
    """oracledb 由来の例外か(モジュール名で判定)。oracledb を import 時依存にしないための proxy。"""
    return (type(e).__module__ or "").startswith("oracledb")


def _has_connection_marker(msg: str) -> bool:
    """DB 接続/可用性に起因する一過性エラーの目印を含むか。"""
    return any(mark in msg for mark in _DB_CONNECTION_MARKERS)


def _is_backend_unavailable_oracle(e: BaseException, msg: str) -> bool:
    """oracledb の **DB 不可用** 例外か(503 相当)。

    marker 付き(DPY-/ORA-12541 等)に加え、`oracledb.OperationalError`(プール初期化失敗・
    ウォレット取得失敗 `db init failed` 等)も一過性のバックエンド不可用として 503 に倒す。
    列不正・構文(`DatabaseError`/ORA-00942 等)は実行失敗(502)のままにする。
    """
    if _has_connection_marker(msg):
        return True
    return type(e).__name__ == "OperationalError" or "db init failed" in msg


#: Select AI が SQL を生成しなかった想定内の失敗を示すメッセージ目印(差し替え runner が
#: 専用例外でなく素の RuntimeError を投げても 502 に正規化できるようにする保険)。
_SQLGEN_FAILURE_MARKERS = ("SQLを返しませんでした", "sql generation", "no SQL")


def _is_select_ai_no_sql(e: BaseException) -> bool:
    """Select AI の SQL 未生成専用例外(nl2sql.SelectAiNoSqlError)か。遅延 import で判定。"""
    try:
        from ..nl2sql import SelectAiNoSqlError
    except Exception:  # noqa: BLE001
        return False
    return isinstance(e, SelectAiNoSqlError)


def _reraise_nl2sql_error(e: BaseException) -> NoReturn:
    """NL2SQL 実行で起きた **想定する** 例外だけを HTTP 意味へ正規化する(想定外は握りつぶさない)。

    - 生成SQL拒否(SqlRejectedError) → `SlotInferenceError`(ルートで 502)。
    - oracledb のエラー: 接続/可用性マーカー付きは `SlotBackendUnavailableError`(503)、
      それ以外(生成SQLの列不正・ORA-00942 等の実行失敗)は `SlotInferenceError`(502)。
    - Select AI が SQL を返さない等の `RuntimeError`: 接続マーカー付きなら 503、無ければ 502。
    - 上記以外(`TypeError`/`ValueError` 等の実装バグ)は **そのまま再送出** し、ルートで 500 に
      露出させる——本物のバグを 502 に丸めて隠さない(以前は `*Error` 全捕捉で隠蔽していた)。
    """
    from jetuse_shared.sqlguard import SqlRejectedError

    if isinstance(e, SqlRejectedError):
        raise SlotInferenceError(f"nl2sql: 生成SQLが許可範囲外: {e}") from e
    msg = str(e)
    if _is_oracledb_error(e):
        if _is_backend_unavailable_oracle(e, msg):
            raise SlotBackendUnavailableError(
                f"nl2sql: DB が一時的に利用できません: {msg[:200]}"
            ) from e
        # 列不正・構文・権限不足(ORA-00942)等は生成SQL起因の実行失敗 → 推論失敗扱い。
        raise SlotInferenceError(f"nl2sql: SQL 実行に失敗: {msg[:200]}") from e
    if isinstance(e, RuntimeError):
        if _has_connection_marker(msg):
            raise SlotBackendUnavailableError(
                f"nl2sql: DB が一時的に利用できません: {msg[:200]}"
            ) from e
        # Select AI の SQL 未生成という **想定内** の失敗だけを推論失敗(502)に正規化する。
        # 専用例外 SelectAiNoSqlError(型)か既知メッセージに限定し、未知の RuntimeError は
        # 握りつぶさず再送出する(実装バグを 502 に丸めない)。
        if _is_select_ai_no_sql(e) or any(m in msg for m in _SQLGEN_FAILURE_MARKERS):
            raise SlotInferenceError(f"nl2sql: SQL 生成に失敗: {e}") from e
        raise e
    # 想定外(実装バグ)は正規化せず再送出 → ルートで 500。502 で握りつぶさない。
    raise e


@register_capability("nl2sql")
def handle_nl2sql(ctx: SlotContext, payload: dict[str, Any]) -> dict[str, Any]:
    """自然言語の売上集計依頼を、専用スキーマに対する SQL 照会へ束ねて実行する(SBA-C)。

    照会先スキーマは `ctx.nl2sql_schema`(sample-app が宣言する実 DB 専用スキーマ)。参照可能表は
    **このスロット専用**に限定する(`_slot_tables`: スロットを載せる screen の dataset のみ)——
    売上集計スロットから案件詳細や議事録まで照会できる「面の広げすぎ」を防ぐ。生成SQLを対象スキーマ
    ＋このスロットの許可表に制限する。実行器は `_nl2sql_runner`(既定=実 ADB へ Select AI 照会)。
    生成SQL拒否(他スキーマ等)・SQL未生成・列不正等の実行失敗は `SlotInferenceError`(502)、
    DB 接続/可用性障害は `SlotBackendUnavailableError`(503)に振り分ける(`_reraise_nl2sql_error`)。
    想定外の例外(実装バグ)は丸めず再送出して 500 に露出させる。一次的な隔離保証は読取専用
    ユーザーの最小権限が担う。
    """
    question = _require_input(payload)
    if not ctx.nl2sql_schema:
        raise SlotInputError("nl2sql: 照会先スキーマ(nl2sql_schema)が未設定")
    tables = _slot_tables(ctx)
    if not tables:
        raise SlotInputError("nl2sql: このスロットが参照できる dataset(screen 経由)が無い")
    try:
        result = _nl2sql_runner(
            question, schema=ctx.nl2sql_schema, tables=tables, model_key=ctx.model_key
        )
    except Exception as e:  # noqa: BLE001
        _reraise_nl2sql_error(e)
    rows = result.get("rows") or []
    return {
        "capability": "nl2sql",
        "schema": ctx.nl2sql_schema,
        "sql": result.get("sql", ""),
        "columns": result.get("columns") or [],
        "rows": rows[:MAX_NL2SQL_PREVIEW_ROWS],
        "row_count": result.get("row_count", len(rows)),
        "truncated": bool(result.get("truncated")) or len(rows) > MAX_NL2SQL_PREVIEW_ROWS,
    }


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
    nl2sql_schema: str | None = None,
) -> dict[str, Any]:
    """aiSlot を実行時バインドして実行し、結果 dict を返す。

    呼び出し側(ルート層)は `corpus`(知識行=シード由来)を与える。本関数はハンドラを解決して
    `SlotContext` を組み立て、ハンドラを実行するだけの薄い実行器(maker/checker 分離の maker 側)。
    `nl2sql_schema` は nl2sql スロットの照会先実 DB スキーマ(SBA-C の JETUSE_SBA04 等)。
    """
    slot, handler = bind_slot(definition, slot_key)
    ctx = SlotContext(
        owner=owner,
        slot=slot,
        definition=definition,
        corpus=list(corpus or []),
        model_key=model_key,
        nl2sql_schema=nl2sql_schema,
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
