"""ヒアリング(NL) 1 往復の LLM 呼び出しと「設計に足りる」判定(SP3-01 / specs/19 §2.2・§2.3)。

fail-closed: LLM の sufficient=true はサーバ側の決定的再検査(必須フィールドの実在確認)を
通ったときだけ通す — LLM の判定だけを信頼境界にしない。逆(必須が揃っているのに LLM が
false)は LLM に従う(追加確認したい合理的な場合がある)。
モデルは既存 chat 基盤の既定モデルを流用(specs/19 §2.4 — モデル選択 UI は持たない)。
"""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .chat import _to_responses_input
from .genai import make_inference_client
from .models import DEFAULT_MODEL, MODELS

HEARING_MODEL = DEFAULT_MODEL


class HearingError(RuntimeError):
    """LLM 出力が再試行しても構造化できなかった(ルート側で 502)。"""


class DataProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    documents: str | None = None
    tables: str | None = None


class Requirements(BaseModel):
    """要求サマリ(specs/19 §2.2)。LLM 出力の未知キーは捨てる(スキーマを閉じる)。

    プロンプトが「不明な項目は null」と指示するため、実 LLM はコンテナ型にも明示 null を
    返す(2026-07-07 プレビュー実機)。null を型不一致 502 にせず既定値へ丸める。
    """

    model_config = ConfigDict(extra="ignore")
    industry: str | None = None
    use_case: str | None = None
    capabilities_hint: list[str] = Field(default_factory=list)
    data_profile: DataProfile = Field(default_factory=DataProfile)
    notes: str | None = None

    @field_validator("capabilities_hint", "data_profile", mode="before")
    @classmethod
    def _null_to_default(cls, v, info):
        return ([] if info.field_name == "capabilities_hint" else {}) if v is None else v


class HearingTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reply: str
    requirements: Requirements = Field(default_factory=Requirements)
    sufficient: bool = False
    missing: list[str] = Field(default_factory=list)

    @field_validator("requirements", "missing", mode="before")
    @classmethod
    def _null_to_default(cls, v, info):
        return ([] if info.field_name == "missing" else {}) if v is None else v


_SYSTEM_PROMPT = """あなたは OCI デモ作成ビルダーのヒアリング担当です。フィールドSA(利用者)が
顧客向けデモの要望を自然言語で話します。対話から次の「要求サマリ」を埋めてください。

- industry: 業種(必須)
- use_case: デモで見せたい業務・ユースケース(必須)
- capabilities_hint: 使いたい能力の候補。"chat" / "rag.search" / "dbchat" から推定(任意)
- data_profile.documents: 検索対象にする文書の雰囲気(RAG 用)
- data_profile.tables: 照会対象にする表データの雰囲気(DB 用)
  ※ documents / tables のどちらか一方以上が必須
- notes: その他の要望(任意)

毎回、これまでの対話全体から要求サマリを組み立て直し、次の JSON だけを出力してください
(前後に説明文・コードフェンスを付けない):
{"reply": "<利用者への応答文。不足があれば 1〜2 個の具体的な追質問>",
 "requirements": {"industry": ..., "use_case": ..., "capabilities_hint": [...],
                  "data_profile": {"documents": ..., "tables": ...}, "notes": ...},
 "sufficient": <必須項目がすべて埋まったら true>,
 "missing": ["不足している項目名", ...]}

不明な項目は null にする。捏造しない(利用者が言っていない内容を requirements に書かない)。
sufficient=true のときの reply は要求サマリの確認文にする。"""

_RETRY_PROMPT = (
    "直前の出力は指定の JSON 形式ではありません。説明文やコードフェンスを付けず、"
    'キー reply / requirements / sufficient / missing だけを持つ JSON を 1 つ出力してください。'
)


def _complete(messages: list[dict], response_schema: dict | None = None) -> tuple[str, dict]:
    """非ストリーミング単発補完(temperature 0 — specs/19 §2.3)。戻り = (本文, usage)。

    response_schema 指定時は Responses API の json_schema 構造化出力で依頼する
    (SP3-02 review-1 F001 — 実機 gpt-oss-120b が構造的に壊れた JSON を返す揺れへの対策。
    大阪プレビュー実機で受理・6/6 合格を確認 2026-07-07)。chat API 系モデルへの適用は
    上流対応が未確認のため行わない(受け皿は呼び出し側のスキーマ検証 — fail-closed)。
    """
    model = MODELS[HEARING_MODEL]
    client = make_inference_client(with_project=model.api == "responses")
    if model.api == "responses":
        extra: dict = {}
        if response_schema is not None:
            extra["text"] = {"format": {
                "type": "json_schema", "name": "structured_output",
                "schema": response_schema, "strict": True,
            }}
        r = client.responses.create(
            model=model.oci_id, input=_to_responses_input(messages),
            temperature=0, store=False, **extra,
        )
        usage = getattr(r, "usage", None)
        return r.output_text or "", {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        }
    r = client.chat.completions.create(
        model=model.oci_id, messages=messages, temperature=0
    )
    usage = getattr(r, "usage", None)
    return r.choices[0].message.content or "", {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


def _strip_fence(raw: str) -> str:
    """LLM 出力のコードフェンス除去(docunderstand の流儀。builder_design と共用)。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


def _parse(raw: str) -> HearingTurn | None:
    """LLM 出力から HearingTurn を頑健に取り出す。"""
    try:
        return HearingTurn.model_validate(json.loads(_strip_fence(raw)))
    except (json.JSONDecodeError, ValueError, ValidationError):
        return None


def missing_required(req: Requirements) -> list[str]:
    """§2.2 の必須フィールドの決定的検査(空白のみは未充足)。"""

    def filled(v: str | None) -> bool:
        return bool(v and v.strip())

    missing = []
    if not filled(req.industry):
        missing.append("industry")
    if not filled(req.use_case):
        missing.append("use_case")
    if not (filled(req.data_profile.documents) or filled(req.data_profile.tables)):
        missing.append("data_profile")
    return missing


def run_hearing_turn(transcript: list[dict]) -> tuple[HearingTurn, dict]:
    """system + 全 transcript で LLM を 1 回呼び、決定的再検査を適用した結果を返す。

    形式不正は 1 回だけ再試行(形式エラーをフィードバック)し、なお不正なら HearingError。
    戻り = (判定済み HearingTurn, usage 合算)。
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *transcript]
    raw, usage = _complete(messages)
    turn = _parse(raw)
    if turn is None:
        raw2, usage2 = _complete(
            [*messages, {"role": "assistant", "content": raw[:4000]},
             {"role": "user", "content": _RETRY_PROMPT}]
        )
        usage = {k: usage.get(k, 0) + usage2.get(k, 0) for k in usage2}
        turn = _parse(raw2)
        if turn is None:
            raise HearingError("LLM 応答を構造化できませんでした(再試行済み)")
    # サーバ側の決定的再検査を最終判定とする(fail-closed — specs/19 §2.3)
    missing = missing_required(turn.requirements)
    if missing:
        turn.sufficient = False
        turn.missing = list(dict.fromkeys([*turn.missing, *missing]))
    return turn, usage
