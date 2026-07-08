"""デモ設計(SP3-02 / specs/19 §3)。要求サマリ + 能力カタログ → 検証済みデモプラン。

fail-closed(§3.3): LLM 出力は pydantic strict / extra=forbid のプランスキーマを通った
ものだけを受け入れる。配線はブロック型で固定 — URL・パス・HTTP の自由記述フィールドは
スキーマに存在しない(§3.2。extra=forbid が範囲外呼び出しの構造的防止の一部)。
語彙(§3.4)は capabilities.demo_plan_vocabulary でカタログから構造的に導出し、
能力 id をコードに固定しない。検証不合格は同一リクエスト内で最大 2 回まで再生成
(検証エラーをフィードバック — §3.1)。
"""

import json
import logging
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from . import builder_hearing
from .datasets import explicit_type_error
from .models import DEFAULT_MODEL

logger = logging.getLogger("jetuse.builder_design")

DESIGN_MODEL = DEFAULT_MODEL  # 既存 chat 基盤の既定モデルを流用(ヒアリングと同じ)
PROMPT_VERSION = 1  # 固定プラン産出プロンプトの版数(§3.1 — §4.2 N6 の再現性)
MAX_REGENERATIONS = 2  # 検証不合格の再生成上限(§3.1 — 同一リクエスト内)

# §3.3 の上限(既定値 = 仕様の上表)。環境で変える必要が出たら settings へ昇格(residual)
MAX_PLAN_BYTES = 256 * 1024

# 能力⇔データ定義の整合(§3.3)。語彙のハードコードではなく「この能力を採るならこの
# データ定義が必須(逆も真)」という仕様上の結合の宣言。plan.capabilities ⊆ 語彙のため、
# カタログに居ない能力の行は事実上効かない。
DATA_REQUIREMENTS = {"dbchat": "tables", "rag.search": "documents"}

# 保守的な識別子のみ(§3.3 — SQL/命名機構への信頼境界)
_IDENT = r"^[a-z][a-z0-9_]{0,29}$"
_FILENAME = r"^[a-z0-9_-]{1,64}\.(md|txt)$"
# 列型の許可リストは datasets.explicit_type_error が単一の正(§3.3 「datasets 機構が
# 投入可能な型に閉じる」— 投入側 create_dataset(column_types) と同一検証)

Ident = Annotated[str, StringConstraints(pattern=_IDENT)]
Prompt = Annotated[str, StringConstraints(max_length=200)]


class PlanValidationError(ValueError):
    """プラン検証不合格(§3.3)。文字列表現 = LLM フィードバック / 422 detail 用の有界要約。"""


class DesignError(RuntimeError):
    """再生成上限まで検証合格プランを得られなかった(ルート側 422)。usage は消費分の合算。"""

    def __init__(self, summary: str, usage: dict):
        super().__init__(summary)
        self.usage = usage


class DesignUpstreamError(RuntimeError):
    """LLM 呼び出しの通信例外(ルート側 502)。途中試行で消費した usage を保持する
    (review-1 F004 — エラー経路でも usage_log から欠落させない)。"""

    def __init__(self, summary: str, usage: dict):
        super().__init__(summary)
        self.usage = usage


class _PlanBase(BaseModel):
    # strict: 型強制なし(bool→int 等の黙認をしない) / extra=forbid: 未知フィールド 422
    model_config = ConfigDict(extra="forbid", strict=True)


class PlanColumn(_PlanBase):
    name: Ident
    type: str
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("type")
    @classmethod
    def _type_allowlist(cls, v: str) -> str:
        err = explicit_type_error(v)  # fullmatch 内包(末尾改行の受理も防ぐ — review-1 F005)
        if err:
            raise ValueError(err)
        return v


class PlanTable(_PlanBase):
    name: Ident
    title: str = Field(min_length=1, max_length=200)
    rows: int = Field(ge=1, le=500)
    columns: list[PlanColumn] = Field(min_length=1, max_length=20)


class PlanDocument(_PlanBase):
    filename: str = Field(pattern=_FILENAME)
    title: str = Field(min_length=1, max_length=200)
    outline: str = Field(min_length=1, max_length=1000)


class PlanBlock(_PlanBase):
    type: str  # capability id。配線はブロック型で固定(§3.2 — capabilities との包含は下で検証)
    title: str = Field(min_length=1, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=4000)
    suggested_prompts: list[Prompt] = Field(default_factory=list, max_length=5)


class PlanScreen(_PlanBase):
    id: Ident  # 生成コードのルーティング/ファイル名に使われうる — 識別子に閉じる
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    blocks: list[PlanBlock] = Field(min_length=1, max_length=8)


class PlanData(_PlanBase):
    tables: list[PlanTable] = Field(default_factory=list, max_length=5)
    documents: list[PlanDocument] = Field(default_factory=list, max_length=10)


class DemoPlan(_PlanBase):
    plan_version: Literal[1]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    capabilities: list[str] = Field(min_length=1)
    screens: list[PlanScreen] = Field(min_length=1, max_length=5)
    data: PlanData = Field(default_factory=PlanData)

    @field_validator("capabilities")
    @classmethod
    def _vocabulary_subset(cls, v: list[str], info) -> list[str]:
        # 語彙は検証コンテキストで受け取る(実行時にカタログから導出 — §3.4)。
        # コンテキストなしの検証は許さない(fail-closed)。
        vocab = (info.context or {}).get("vocabulary")
        if vocab is None:
            raise ValueError("語彙コンテキストなしでは検証できません")
        if len(set(v)) != len(v):
            raise ValueError("capabilities に重複があります")
        unknown = [c for c in v if c not in vocab]
        if unknown:
            raise ValueError(f"語彙外の能力: {unknown}(使える語彙: {vocab})")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "DemoPlan":
        caps = set(self.capabilities)
        bad = sorted({b.type for s in self.screens for b in s.blocks if b.type not in caps})
        if bad:
            raise ValueError(f"blocks[].type が plan.capabilities に含まれません: {bad}")
        names = [t.name for t in self.data.tables]
        if len(set(names)) != len(names):
            raise ValueError("data.tables の表名が重複しています")
        sids = [s.id for s in self.screens]
        if len(set(sids)) != len(sids):
            raise ValueError("screens[].id が重複しています")
        for cap, kind in DATA_REQUIREMENTS.items():  # 能力⇔データ定義の整合(§3.3)
            have = len(getattr(self.data, kind))
            if cap in caps and have == 0:
                raise ValueError(f"能力 {cap} を採用する場合 data.{kind} が 1 件以上必要です")
            if cap not in caps and have > 0:
                raise ValueError(
                    f"data.{kind} が定義されているのに能力 {cap} が capabilities にありません"
                )
        return self


def _summarize(e: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:10]
    ]
    return " / ".join(parts)[:2000]


def validate_plan(data: Any, vocabulary: list[str]) -> dict:
    """§3.3 の fail-closed 検証。合格したら正規化済み dict(保存形)を返す。

    正規化 = model_dump(exclude_none=True)。保存形を再検証しても同形(べき等)。
    """
    try:
        plan = DemoPlan.model_validate(data, context={"vocabulary": vocabulary})
    except ValidationError as e:
        raise PlanValidationError(_summarize(e)) from e
    out = plan.model_dump(exclude_none=True)
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_PLAN_BYTES:
        raise PlanValidationError(
            "プランの直列化が 256KB を超えています。画面・データ定義を減らしてください"
        )
    return out


_SYSTEM_TEMPLATE = """あなたは OCI デモ作成ビルダーのデモ設計担当です。利用者の要求サマリ
(JSON)と下記の能力カタログから、顧客向けデモの「デモプラン」JSON を 1 つだけ出力して
ください(前後に説明文・コードフェンスを付けない)。UI 文言はすべて日本語。

## 使える能力(この id 以外は使えない)
{catalog}

## プランのスキーマ(plan_version=1。ここに無いフィールドを追加しない)
{{"plan_version": 1,
 "title": "<デモ名 ≤200文字>", "description": "<デモの説明 ≤1000文字>",
 "capabilities": ["<採用する能力 id>", ...],
 "screens": [{{"id": "<小文字英数字と_の識別子>", "title": "<画面名>",
              "description": "<画面説明(任意) ≤1000文字>",
              "blocks": [{{"type": "<採用能力の id>", "title": "<ブロック名>",
                          "system_prompt": "<チャットの指示(任意) ≤4000文字>",
                          "suggested_prompts": ["<例示プロンプト ≤200文字>", ...]}}]}}],
 "data": {{"tables": [{{"name": "<識別子>", "title": "<表示名>", "rows": <1..500>,
                      "columns": [{{"name": "<識別子>", "type": "<型>",
                                   "description": "<説明(任意)>"}}]}}],
          "documents": [{{"filename": "<小文字英数字-_ の名前>.md|.txt",
                         "title": "<表示名>", "outline": "<章立ての概要 ≤1000文字>"}}]}}}}

## 制約(検証で機械的に落とされる)
- capabilities は上記カタログの id のみ・重複なし。blocks[].type は capabilities の要素のみ。
- URL・パス・エンドポイントは書かない(呼び出し先はブロック型が決める)。
- screens 1〜5 / 画面あたり blocks 1〜8 / tables 0〜5(列 1〜20・rows 1〜500)/ documents 0〜10。
- 表・列名と screens[].id は ^[a-z][a-z0-9_]{{0,29}}$。表名・画面 id は重複禁止。
- columns[].type は VARCHAR2(n CHAR)(n≤1000) / NUMBER / NUMBER(p[,s]) / DATE / TIMESTAMP のみ。
{data_rules}- 要求サマリに無い要望を捏造しない。suggested_prompts は業務文脈に即した具体例にする。
"""

_RETRY_PROMPT = (
    "直前のプランは検証に不合格でした。検証エラー: {errors}\n"
    "同じスキーマ・制約で修正したプラン JSON を 1 つだけ出力してください"
    "(説明文・コードフェンスなし)。"
)


def _system_prompt(catalog: list[dict], vocabulary: list[str]) -> str:
    """固定プロンプト(PROMPT_VERSION)にカタログ由来の語彙・説明を差し込む。

    カタログはルートと同じ生成関数の出力(§3.1)を語彙でフィルタしたもの。プロンプトには
    設計判断に効く記述部分(summary / when_to_use)だけを入れ、LLM 入力を有界化する。
    """
    lines = [
        f"- {c['capability']}: {c.get('summary', '')} — {c.get('when_to_use', '')}"
        for c in catalog
        if c["capability"] in vocabulary
    ]
    data_rules = "".join(
        f"- 能力 {cap} を採用するなら data.{kind} を 1 件以上定義する"
        f"(採用しないなら data.{kind} は空にする)。\n"
        for cap, kind in DATA_REQUIREMENTS.items()
        if cap in vocabulary
    )
    return _SYSTEM_TEMPLATE.format(catalog="\n".join(lines), data_rules=data_rules)


def _extract_plan_json(raw: str) -> tuple[dict | None, str]:
    """LLM 出力から最初の JSON オブジェクトを頑健に取り出す(検証への入口整形)。

    実機で観測した揺れ(2026-07-07 プレビュー・gpt-oss-120b): 完全な JSON の後に余分な
    データが続く(json.loads 全文パースは Extra data で不合格) / 前置きの説明文が付く /
    構造的に壊れた JSON(オブジェクト間に余分な閉じ括弧)。fence 除去 → 最初の '{' から
    raw_decode で最初のオブジェクトだけを取り、前後は捨てる。構文エラーは位置と周辺を
    返し、再生成フィードバックが場所を特定できるようにする(review-1 F001)。
    受け入れ判定は §3.3 の strict スキーマ検証が担うため fail-closed は弱まらない。
    戻り = (オブジェクト or None, エラー要約)。
    """
    s = builder_hearing._strip_fence(raw)
    start = s.find("{")
    if start < 0:
        return None, "出力に JSON オブジェクトがありません"
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
        return obj, ""
    except json.JSONDecodeError as e:
        ctx = s[start:][max(0, e.pos - 60):e.pos + 40]
        return None, f"JSON 構文エラー: {e.msg} (位置 {e.pos}): …{ctx}…"


def run_design(
    requirements: dict, catalog: list[dict], vocabulary: list[str]
) -> tuple[dict, dict]:
    """要求サマリ → 検証合格プラン(正規化 dict)と usage 合算(temperature 0 — §3.1)。

    生成は json_schema 構造化出力で依頼する(F001 — サーバ側で JSON 文法が概ね強制され、
    実機で構造壊れが消えることを確認済み。強制が効かない場合の受け皿は抽出+検証+再生成)。
    検証不合格(JSON 不正含む)はエラーをフィードバックして最大 MAX_REGENERATIONS 回
    再生成。なお不合格なら DesignError、通信例外は DesignUpstreamError
    (どちらも消費 usage 込み — エラー経路でも usage_log できるように)。
    """
    messages = [
        {"role": "system", "content": _system_prompt(catalog, vocabulary)},
        {"role": "user", "content": json.dumps(requirements, ensure_ascii=False)},
    ]
    schema = DemoPlan.model_json_schema()
    total = {"input_tokens": 0, "output_tokens": 0}
    errors = ""
    for _ in range(1 + MAX_REGENERATIONS):
        try:
            raw, usage = builder_hearing._complete(messages, response_schema=schema)
        except Exception as e:
            raise DesignUpstreamError(str(e)[:500], total) from e
        for k in total:
            total[k] += usage.get(k, 0)
        data, parse_err = _extract_plan_json(raw)
        if data is None:
            errors = parse_err
            # 失敗形を観測可能にする(F001 — 有界・LLM 出力のみで秘密は含まれない)
            logger.warning("design plan parse failed: %s / head=%r / tail=%r",
                           parse_err, raw[:200], raw[-100:])
        else:
            try:
                return validate_plan(data, vocabulary), total
            except PlanValidationError as e:
                errors = str(e)
        messages = [
            *messages,
            {"role": "assistant", "content": raw[:8000]},
            {"role": "user", "content": _RETRY_PROMPT.format(errors=errors)},
        ]
    raise DesignError(errors, total)
