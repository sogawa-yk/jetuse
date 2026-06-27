"""推薦ルールエンジン(HBD-01)。回答 → 「主SBA＋AI部品＋コネクタ＋UI＋シード方針」。

出典: docs/enhance/202607-hearing-flow.md §3(素材マッピング)/ §4(推薦構成)/ §6(決定ルールと
GenAI補助の境界)。本モジュールの中核 `recommend()` は **副作用の無い決定的関数**で、GenAI 不在/失敗
でも完全な推薦を返す(§6: 何を選ぶかはルール、埋める/書く/寄せるは GenAI)。

GenAI 補助(§6 の境界)は本モジュールでは扱わず(別関数/別ターン)、`recommend()` は決定ルールのみ。
ただし Q1=other(その他業務)は最近傍 SBA をルールだけでは決められないため、`sample_app=None` と
`needs_genai_nearest=True` を返してフォールバックの存在を明示する(GenAI 不在でも推薦は成立)。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .hearing_schema import HearingSchemaError, validate_answers
from .plugins import sample_app_registry as _registry
from .plugins.sample_app import (
    DEFAULT_HOST_CAPABILITIES,
    SAMPLE_APP_CAPABILITIES,
    required_capabilities,
)

#: SBA コード → コア同梱 instance_id(実装済みのみ。SBA-D は未実装で不在)。
_SBA_CODE_TO_INSTANCE: dict[str, str] = {
    "SBA-A": _registry.SBA_A_INSTANCE_ID,
    "SBA-B": _registry.SBA_B_INSTANCE_ID,
    "SBA-C": _registry.SBA_C_INSTANCE_ID,
}


def _sba_capabilities(code: str | None) -> set[str]:
    """実装済みコア SBA が組込点(aiSlot)に持つ capability 集合。未実装/None は空集合。"""
    inst = _SBA_CODE_TO_INSTANCE.get(code or "")
    resolved = _registry.resolve_app(inst) if inst else None
    return set(required_capabilities(resolved.definition)) if resolved else set()

# --- 写像表(仕様の正本・監査可能) ------------------------------------------

#: Q1(業務) → 主サンプルアプリ。other は最近傍を GenAI 補助に委ねる(None)。
#: 出典: §3 Q1 素材マッピング。
Q1_TO_SBA: dict[str, str | None] = {
    "support": "SBA-A",
    "sales": "SBA-C",
    "inventory": "SBA-B",
    "accounting": "SBA-D",
    "other": None,
}

#: Q2(データ所在) → AI 部品の素地(capability)。saas はコネクタ側で扱う(AI部品ではない)。
#: 出典: §3 Q2 / §4。docs は RAG を中心に要約・分類まで素地化する(§4 の代表例)。
Q2_TO_PARTS: dict[str, tuple[str, ...]] = {
    "docs": ("rag.search", "summarize", "classify"),
    "business_db": ("nl2sql",),
    "audio": ("minutes",),
    "image": ("vlm.ocr",),
    "saas": (),
}

#: Q3(主役 AI) → 強調する主役 capability(複数を含めることがある)。先頭を highlight にする。
#: 出典: §3 Q3 / §4。
Q3_TO_PARTS: dict[str, tuple[str, ...]] = {
    "rag_qa": ("rag.search",),
    "nl2sql": ("nl2sql", "chart"),
    "agent": ("agent",),
    "ocr_extract": ("vlm.ocr", "classify"),
    "summarize_draft": ("summarize", "draft"),
}

#: Q4(連携) → コネクタ。slack のみコア。other_connector は後段マーケット(コアでは付けない)。
#: 出典: §3 Q4。
Q4_TO_CONNECTORS: dict[str, tuple[str, ...]] = {
    "slack": ("slack",),
    "other_connector": (),
    "none": (),
}

#: Q5(利用シーン) → UI/出力テンプレ。出典: §3 Q5。
Q5_TO_UI: dict[str, str] = {
    "chat_form": "chat",
    "notify": "notify",
    "report": "report",
}

#: Q6(デモデータ) → シード戦略。出典: §3 Q6 / §4。
Q6_TO_SEED: dict[str, str] = {
    "sample": "sample",
    "industry_generated": "genai_generated",
    "replace_later": "replace_later",
}

#: AI 部品の決定的な並び順(出力の安定化と highlight 以外の整列に使う)。
PART_ORDER: list[str] = [
    "rag.search",
    "nl2sql",
    "chart",
    "agent",
    "vlm.ocr",
    "classify",
    "summarize",
    "draft",
    "minutes",
]


class ValidationReport(BaseModel):
    """Auto チェック(合成バリデーション)結果。部品は外さず警告に留める(§3: 外させない)。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    #: ホスト既定能力に無い要求 capability(致命的ではないが要注意の警告)。
    missing_capabilities: list[str]
    #: 追加能力/拡張が前提の部品に対する注意(例: vlm.ocr は MM-01 能力に依存)。
    warnings: list[str]


class Recommendation(BaseModel):
    """決定ルールが返す推薦構成(§4 の 3 要素＋UI/シード＋監査トレース)。"""

    model_config = ConfigDict(extra="forbid")

    #: 主サンプルアプリ(SBA-A/B/C/D)。Q1=other で最近傍未定のときは None。
    sample_app: str | None
    #: 主＋従の従(MVP は単一 SBA に絞るため通常は空。§8 の複合は HBD 後段)。
    secondary_sample_apps: list[str]
    #: AI 部品セット(capability)。決定順に整列。主 SBA の組込点に合うもののみ(自動フィット)。
    ai_parts: list[str]
    #: 推薦されたが主 SBA に組込点が無く「対象外」として除外した部品。UI 提示・監査用。
    not_applicable_parts: list[str] = Field(default_factory=list)
    #: デモの主役 capability(Q3 由来)。SBA の組込点に優先配置する。
    highlight: str | None
    #: コネクタ(Q4)。slack のみコア。
    connectors: list[str]
    #: UI/出力テンプレ(Q5)。
    ui: str
    #: シード戦略(Q6)。
    seed_strategy: str
    #: 最近傍 SBA を GenAI 補助で決める必要(Q1=other)。フォールバックの所在を明示。
    needs_genai_nearest: bool
    #: 決定ルールのトレース(監査用・人間がブラックボックス化を避けるための説明)。
    rationale: list[str]
    #: 合成バリデーション(Auto)。
    validation: ValidationReport


def _ordered_parts(parts: set[str]) -> list[str]:
    """部品集合を決定順に整列する(未知語は末尾へ安定整列)。"""
    known = [p for p in PART_ORDER if p in parts]
    extra = sorted(parts - set(PART_ORDER))
    return known + extra


def _validate_parts(parts: list[str]) -> ValidationReport:
    """Auto: 要求 capability がホスト既定能力に収まるか・追加能力依存が無いかを点検する。

    §3 の原則「不足があれば警告＋代替提案(外させない)」に従い、部品は除去せず警告に留める。
    """
    requested = set(parts)
    missing = sorted(requested - DEFAULT_HOST_CAPABILITIES)
    warnings: list[str] = []
    if "vlm.ocr" in requested:
        warnings.append("vlm.ocr はマルチモーダル能力(MM-01)に依存。モデル可用性を Auto で要確認")
    return ValidationReport(
        ok=not missing,
        missing_capabilities=missing,
        warnings=warnings,
    )


def recommend(answers: dict[str, Any]) -> Recommendation:
    """回答(question_id → value)から推薦構成を**決定的に**生成する。

    入力は `hearing_schema.validate_answers(require_all=True)` を通す(未回答/不正は弾く)。
    GenAI には一切依存しない(§6: 決定ルールのみで推薦が成立。Q1=other は最近傍を None で示す)。
    """
    norm = validate_answers(answers, require_all=True)
    q1, q2, q3, q4, q5, q6 = (
        norm["Q1"], norm["Q2"], norm["Q3"], norm["Q4"], norm["Q5"], norm["Q6"]
    )
    rationale: list[str] = []

    # 1) 主 SBA: Q1 を基点に、Q3×Q2 の分岐で補正(§3 分岐例)。
    sample_app = Q1_TO_SBA[q1]
    needs_genai = sample_app is None
    if needs_genai:
        rationale.append("Q1=other: 主 SBA は決定ルールで未定 → 最近傍 SBA を GenAI 補助に委ねる")
    else:
        rationale.append(f"Q1={q1} → 主 SBA {sample_app}")
        # 分岐: Q2 に業務DB かつ Q3 が集計・分析 → SBA-B(NL2SQL)を主役に格上げ(§3)。
        if "business_db" in q2 and q3 == "nl2sql" and sample_app != "SBA-B":
            rationale.append(
                f"分岐(§3): Q2 に業務DB＋Q3=集計分析 → 主役を {sample_app}→SBA-B に格上げ"
            )
            sample_app = "SBA-B"

    # 2) AI 部品セット: Q2(データ素地) ∪ Q3(主役)。
    parts: set[str] = set()
    for sel in q2:
        parts.update(Q2_TO_PARTS.get(sel, ()))
    parts.update(Q3_TO_PARTS[q3])
    # 主役 highlight = Q3 の先頭 capability。SBA 組込点に優先配置する。
    highlight = Q3_TO_PARTS[q3][0]
    parts.add(highlight)
    ai_parts = _ordered_parts(parts)
    rationale.append(f"AI部品(候補) = Q2{sorted(q2)} ∪ Q3({q3}) → {ai_parts}(主役 {highlight})")

    # 自動フィット: 主 SBA の組込点に合う部品のみへ限定し、合わない部品は「対象外」に退避する。
    # ガバナンスが弾く no_slot を作らず、どの選択でも成立するデモにする(§4: 部品は黙って消さず
    # not_applicable_parts と rationale に残す)。候補が全滅したらアプリ本来の AI で成立させる。
    not_applicable: list[str] = []
    app_caps = _sba_capabilities(sample_app)
    if app_caps:
        fitting = [p for p in ai_parts if p in app_caps]
        not_applicable = [p for p in ai_parts if p not in app_caps]
        if not fitting:
            fitting = _ordered_parts(app_caps)
            rationale.append(
                f"主役/データが {sample_app} の組込点に該当せず → 本来の組込AI {fitting} で成立"
            )
        if not_applicable:
            rationale.append(f"{sample_app} 対象外の部品を除外(自動フィット): {not_applicable}")
        if highlight not in app_caps:
            new_hl = fitting[0] if fitting else None
            rationale.append(f"主役 {highlight} は組込点なし → 主役を {new_hl} へ変更")
            highlight = new_hl
        ai_parts = fitting

    # 3) コネクタ / UI / シード。
    connectors = list(Q4_TO_CONNECTORS[q4])
    if q4 == "other_connector":
        rationale.append("Q4=other_connector → コネクタは後段マーケット(コアでは付与せず)")
    ui = Q5_TO_UI[q5]
    seed = Q6_TO_SEED[q6]
    rationale.append(f"Q4={q4}→connectors{connectors} / Q5={q5}→UI {ui} / Q6={q6}→seed {seed}")

    validation = _validate_parts(ai_parts)

    return Recommendation(
        sample_app=sample_app,
        secondary_sample_apps=[],
        ai_parts=ai_parts,
        not_applicable_parts=not_applicable,
        highlight=highlight,
        connectors=connectors,
        ui=ui,
        seed_strategy=seed,
        needs_genai_nearest=needs_genai,
        rationale=rationale,
        validation=validation,
    )


# 写像表が sample_app の能力語彙と矛盾しないことを import 時に保証する(語彙ドリフト検知)。
_ALL_MAPPED_PARTS = {
    p for parts in (*Q2_TO_PARTS.values(), *Q3_TO_PARTS.values()) for p in parts
}
_UNKNOWN_PARTS = _ALL_MAPPED_PARTS - SAMPLE_APP_CAPABILITIES
if _UNKNOWN_PARTS:  # pragma: no cover - 開発時の不整合を import 時に弾く
    raise HearingSchemaError(
        f"recommend の写像表に sample-app 能力語彙に無い部品: {sorted(_UNKNOWN_PARTS)}"
    )
