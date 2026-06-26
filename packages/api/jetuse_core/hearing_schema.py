"""ヒアリング質問スキーマ(HBD-01)。Q1..Q6＋Auto をコード上の正本として定義する。

出典: docs/enhance/202607-hearing-flow.md §3(質問セット)を specs/16-platform.md §11 へ昇格。
本モジュールは「どの質問が・どんな型で・どの選択肢を持つか」を**決定的に**定義し、回答の
妥当性検証(`validate_answer` / `validate_answers`)を提供する。回答→素材の写像は `recommend.py`。

設計方針:
  - 質問・選択肢の **id は機械可読な安定キー**(英小文字)。表示文言(label)は別に持つ。
    推薦エンジン(recommend.py)はこの id だけに依存し、文言変更で壊れない。
  - 回答型は `single`(選択肢1つ) / `multi`(選択肢複数) / `auto`(自動チェック・回答なし)。
  - 自由記述は質問単位ではなく**セッションの input_notes**(hearing_session)に置く(§7 データモデル)。
    Q1 の「その他(自由)」は選択肢 `other` で表し、補足の自由文は input_notes / recommend の
    GenAI 補助(最近傍 SBA 提案)で扱う。
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

#: 回答型。auto は SA が回答しない自動チェック(合成バリデーション)。
QuestionType = Literal["single", "multi", "auto"]

#: 回答の出所(§7 hearing_answer.source)。SA 手入力か、GenAI 提案(メモ要点抽出)か。
AnswerSource = Literal["sa", "genai_suggested"]
ANSWER_SOURCES = frozenset(get_args(AnswerSource))

#: セッション status の許可語彙。draft(編集中)→ready(回答完了)→confirmed(推薦確定)/archived。
SessionStatus = Literal["draft", "ready", "confirmed", "archived"]
SESSION_STATUSES = frozenset(get_args(SessionStatus))

#: 1 セッションが持てる回答件数・自由記述長の上限(肥大化/DoS 防止)。
MAX_INPUT_NOTES_CHARS = 8000
MAX_MULTI_SELECTIONS = 16


class QuestionOption(BaseModel):
    """質問の選択肢 1 つ。id は安定キー、label は SA 向け表示文言。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)


class Question(BaseModel):
    """ヒアリング質問 1 問。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    type: QuestionType
    text: str = Field(min_length=1, max_length=400)
    purpose: str = Field(default="", max_length=400)
    options: list[QuestionOption] = Field(default_factory=list)
    required: bool = True
    #: multi 質問が必須回答(require_all)時に求める最小選択数。single/auto では無視。
    min_selections: int = Field(default=1, ge=0)

    @property
    def option_ids(self) -> set[str]:
        return {o.id for o in self.options}


# --- 質問セット(正本) -------------------------------------------------------
# 出典: 202607-hearing-flow.md §3。選択肢 id は recommend.py の写像キー。

QUESTIONS: list[Question] = [
    Question(
        id="Q1",
        type="single",
        text="この顧客で AI を効かせたい業務は？",
        purpose="主サンプルアプリ決定",
        options=[
            QuestionOption(id="support", label="顧客対応/サポート"),
            QuestionOption(id="sales", label="営業・案件"),
            QuestionOption(id="inventory", label="在庫・受発注・データ照会"),
            QuestionOption(id="accounting", label="経理・帳票・経費"),
            QuestionOption(id="other", label="その他(自由)"),
        ],
    ),
    Question(
        id="Q2",
        type="multi",
        text="扱う主なデータはどこに？(複数可)",
        purpose="AI部品の素地決定",
        options=[
            QuestionOption(id="docs", label="社内文書/FAQ/マニュアル"),
            QuestionOption(id="business_db", label="業務DB(表・基幹)"),
            QuestionOption(id="audio", label="会議音声/通話"),
            QuestionOption(id="image", label="帳票/画像/スキャン"),
            QuestionOption(id="saas", label="SaaS上"),
        ],
    ),
    Question(
        id="Q3",
        type="single",
        text="顧客が一番見たい AI の効き所は？(デモの主役)",
        purpose="主役AIユースケース強調",
        options=[
            QuestionOption(id="rag_qa", label="質問に答える(RAG-QA)"),
            QuestionOption(id="nl2sql", label="自然言語で集計・分析(NL2SQL)"),
            QuestionOption(id="agent", label="自動化・次アクション提案(エージェント)"),
            QuestionOption(id="ocr_extract", label="読取・抽出(OCR/分類)"),
            QuestionOption(id="summarize_draft", label="要約・ドラフト生成"),
        ],
    ),
    Question(
        id="Q4",
        type="single",
        text="既存システム/SaaS連携の希望は？",
        purpose="コネクタ選定",
        options=[
            QuestionOption(id="slack", label="Slack 通知/起動"),
            QuestionOption(id="other_connector", label="Teams/Email/その他(後段)"),
            QuestionOption(id="none", label="なし(スタンドアロン)"),
        ],
    ),
    Question(
        id="Q5",
        type="single",
        text="デモの利用シーン/出力形態は？",
        purpose="UI/出力テンプレ選定",
        options=[
            QuestionOption(id="chat_form", label="画面で対話(チャット/フォーム)"),
            QuestionOption(id="notify", label="通知・自動投稿"),
            QuestionOption(id="report", label="レポート/帳票出力"),
        ],
    ),
    Question(
        id="Q6",
        type="single",
        text="デモ用データはどうする？",
        purpose="シード戦略",
        options=[
            QuestionOption(id="sample", label="サンプルシードでOK"),
            QuestionOption(id="industry_generated", label="顧客業界に寄せて生成(GenAI補助)"),
            QuestionOption(id="replace_later", label="顧客実データ風を後で差替"),
        ],
    ),
    Question(
        id="Auto",
        type="auto",
        text="(自動)ケイパビリティ/権限/モデル可用性チェック",
        purpose="合成バリデーション",
        required=False,
    ),
]

#: id → Question の索引(検証・写像で使う)。
QUESTIONS_BY_ID: dict[str, Question] = {q.id: q for q in QUESTIONS}

#: SA が回答する(auto を除く)質問 id。required かつ回答必須の判定に使う。
ANSWERABLE_IDS: list[str] = [q.id for q in QUESTIONS if q.type != "auto"]
REQUIRED_IDS: list[str] = [q.id for q in QUESTIONS if q.type != "auto" and q.required]


class HearingSchemaError(ValueError):
    """回答が質問スキーマに適合しないときに送出する。"""


def validate_answer(question_id: str, value: Any) -> Any:
    """回答 1 件を質問スキーマで検証して正規化値を返す。不正なら HearingSchemaError。

    - `single`: 値は選択肢 id 文字列。未知 id は拒否。
    - `multi`: 値は選択肢 id のリスト。空可・重複不可・未知 id 拒否・件数上限。
    - `auto`: SA は回答しない(検証対象外)→ 明示エラー。
    返り値は保存に適した正規化値(single=str / multi=重複除去済み list)。
    """
    q = QUESTIONS_BY_ID.get(question_id)
    if q is None:
        raise HearingSchemaError(f"未知の質問 id: {question_id!r}")
    if q.type == "auto":
        raise HearingSchemaError(f"質問 {question_id} は自動チェックで SA 回答を取らない")
    if q.type == "single":
        if not isinstance(value, str):
            raise HearingSchemaError(f"{question_id}: single 回答は文字列が必要")
        if value not in q.option_ids:
            raise HearingSchemaError(
                f"{question_id}: 未知の選択肢 {value!r}(候補: {sorted(q.option_ids)})"
            )
        return value
    # multi
    if not isinstance(value, list):
        raise HearingSchemaError(f"{question_id}: multi 回答はリストが必要")
    if len(value) > MAX_MULTI_SELECTIONS:
        raise HearingSchemaError(
            f"{question_id}: 選択数が上限 {MAX_MULTI_SELECTIONS} を超える"
        )
    seen: list[str] = []
    for v in value:
        if not isinstance(v, str):
            raise HearingSchemaError(f"{question_id}: multi 要素は文字列が必要: {v!r}")
        if v not in q.option_ids:
            raise HearingSchemaError(
                f"{question_id}: 未知の選択肢 {v!r}(候補: {sorted(q.option_ids)})"
            )
        if v in seen:
            raise HearingSchemaError(f"{question_id}: 選択肢が重複: {v!r}")
        seen.append(v)
    return seen


def validate_answers(answers: dict[str, Any], *, require_all: bool = False) -> dict[str, Any]:
    """回答の辞書(question_id → value)をまとめて検証し、正規化済み辞書を返す。

    `require_all=True` のとき、回答必須(REQUIRED_IDS)が揃っているかも検証する
    (recommend 前提条件)。auto 質問への回答は常にエラー。
    """
    normalized: dict[str, Any] = {}
    for qid, value in answers.items():
        normalized[qid] = validate_answer(qid, value)
    if require_all:
        missing = [qid for qid in REQUIRED_IDS if qid not in normalized]
        if missing:
            raise HearingSchemaError(f"回答必須の質問が未回答: {missing}")
        # 必須 multi は空配列を未回答とみなす(min_selections)。Q2=[] の素地未決の穴を塞ぐ。
        for qid in REQUIRED_IDS:
            q = QUESTIONS_BY_ID[qid]
            if q.type == "multi" and len(normalized[qid]) < q.min_selections:
                raise HearingSchemaError(
                    f"{qid}: 最低 {q.min_selections} 件の選択が必要(現在 {len(normalized[qid])} 件)"
                )
    return normalized


def question_schema() -> dict[str, Any]:
    """質問セットの機械可読スキーマ(UI/外部公開用)。"""
    return {
        "version": "1",
        "questions": [q.model_dump() for q in QUESTIONS],
    }
