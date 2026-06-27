"""合成バリデーション(HBD-04)。デモ構成をデプロイ前にガバナンス4制約で検証する。

HBD-03 の `synth.DemoComposition`(合成済みデモ構成)を入力に、§4 のガバナンスモデル
(「リファレンスから外さない」)を**デプロイ前ゲート**として機械的に判定する。外れた構成は
弾き、各違反に**代替提案(外させない)**を添える(出典: docs/enhance/202607-demo-platform-plan.md
§4 / §4-3 / §10「HBD-04」, 202607-hearing-flow.md §3 Auto)。

判定する4制約(§4-3「組み立て結果(sample-app × AI部品 × connector)を、許可された組合せ・
必要ケイパビリティ・権限スコープでチェックしてからデプロイ」):

  (a) **許可組合せ**: 推薦された AI 部品が主 SBA の組込点(aiSlot)に対応するか
      (= synth が `no_slot` と判定した部品は許可外組合せ)、コネクタがコアパレット
      (Slack 1本)に収まるか。
  (b) **必要ケイパビリティが束縛済み**: 組込点はあるが `ai_runtime` 未束縛(synth の `unbound`)は
      実行不可 → デプロイ前ゲートで弾く。
  (c) **権限スコープが manifest 内**: aiSlot が要求するスコープが manifest.permissions に宣言
      済みか。これは SBA-01 の `validate_composition`(同梱の `composition_report`)を**再利用**して
      判定し、二重定義しない。
  (d) **モデル可用性(ap-osaka-1)**: 部品が要求するモデル能力(例: vlm.ocr=マルチモーダル/vision)が
      実行リージョンで利用可能か(MM-01 依存。hearing-flow §3 Auto「モデル可用性チェック」)。

設計方針:
  - **副作用なしの決定的関数**。DB/GenAI に触れない。synth が記述した束縛状態(`status`)と
    SBA-01 の `composition_report` を**ポリシー判定に翻訳**する(記述は synth、強制は governance:
    関心の分離)。
  - **二重定義しない**: 権限スコープの整合は `validate_composition` の結果(`composition_report`)を
    そのまま使う。capability/permission 集合の再計算はしない。
  - **外させない**: 各違反は機械可読(`kind`/`element`/`detail`/`alternative`)で、必ず代替提案
    を持つ。

非ゴール: 実トークン発行・Platform API のスコープ実行時強制(S3)。本モジュールは**構成段階の
静的検証**(デプロイ前ゲート)に限定する。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import MODELS, ModelDef
from .plugins import sample_app_registry as registry
from .plugins.core_connectors import core_connector_providers
from .plugins.manifest import (
    PLATFORM_SCOPE_CONNECTOR_INVOKE,
    PLATFORM_SCOPES,
)
from .plugins.sample_app import required_capabilities
from .synth import SBA_CODE_TO_INSTANCE, DemoComposition

# --- 許可パレット(§4 制約2: 制約付きパレット) ------------------------------

#: コア同梱コネクタのパレット。コアは Slack 1本(§6 D9)。これ以外は後段マーケット(S3+)で、
#: 現段階の合成では許可外。空(=連携なし)は当然許可。**正本は `core_connectors` レジストリ**から
#: 導出する(governance とレジストリで二重定義しない。後段で provider を足せば自動追従する)。
CORE_CONNECTORS = core_connector_providers()

#: 部品(capability) → 実行に必要なモデル能力(feature)。既定は "text"(任意の chat/responses
#: モデルで動く)。vlm.ocr はマルチモーダル(vision)モデルを要求する(MM-01 依存)。
#: 出典: 202607-hearing-flow.md §3 Auto / Q2「帳票/画像→VLM-OCR」, models.py(ModelDef.vision)。
CAPABILITY_MODEL_FEATURE: dict[str, str] = {
    "vlm.ocr": "vision",
}
DEFAULT_MODEL_FEATURE = "text"

ViolationKind = Literal[
    "unresolved_composition",
    "disallowed_combination",
    "unbound_capability",
    "missing_host_capability",
    "scope_out_of_manifest",
    "model_unavailable",
    # CON-03: コネクタ invoke スコープ経路の検証。
    "connector_scope_undeclared",  # パレット内なのに合成不整合(action 要求スコープ未宣言)。
    "connector_scope_unknown",  # 束縛が要求する Platform スコープが既知語彙(PLATFORM_SCOPES)外。
]

ElementType = Literal["composition", "capability", "connector", "permission"]


class GovernanceViolation(BaseModel):
    """ガバナンス違反 1 件(機械可読)。`alternative` で必ず代替提案を返す(外させない)。"""

    model_config = ConfigDict(extra="forbid")

    #: 違反種別(どの制約に反したか)。
    kind: ViolationKind
    #: 違反対象の要素(capability 名 / connector 名 / スコープ / "composition")。
    element: str
    #: 要素の種類。
    element_type: ElementType
    #: 人間向けの違反理由。
    detail: str
    #: 代替提案(§4: 部品を黙って消さず、外さずに済む道を示す)。
    alternative: str


class GovernanceReport(BaseModel):
    """合成バリデーション結果。`ok` は違反ゼロ(=デプロイ前ゲート通過)を表す。"""

    model_config = ConfigDict(extra="forbid")

    #: 違反ゼロ(デプロイ可)か。
    ok: bool
    #: 検証対象の主 SBA コード(分かるとき)。
    sample_app: str | None
    #: 違反の一覧(機械可読・代替提案つき)。
    violations: list[GovernanceViolation]
    #: 制約ごとの合否(true=その制約に違反なし)。プレビュー/監査の俯瞰用。
    checks: dict[str, bool]


# --- 内部ヘルパ ------------------------------------------------------------


def _capability_to_sbas() -> dict[str, list[str]]:
    """capability → それを組込点に持つコア同梱 SBA コードの一覧(代替提案用)。"""
    out: dict[str, list[str]] = {}
    for code, instance_id in SBA_CODE_TO_INSTANCE.items():
        resolved = registry.resolve_app(instance_id)
        if resolved is None:
            continue
        for cap in required_capabilities(resolved.definition):
            out.setdefault(cap, []).append(code)
    return {cap: sorted(set(codes)) for cap, codes in out.items()}


def available_model_features(
    models: dict[str, ModelDef] | None = None,
) -> frozenset[str]:
    """実行リージョン(既定 ap-osaka-1 の `MODELS`)で利用可能なモデル能力の集合。

    モデルが 1 つでもあれば "text" は可用。`vision=True` のモデルがあれば "vision" も可用。
    呼び出し側はリージョン差(例: VLM 不可な環境)を `models` 引数で注入して試せる。
    """
    registry_models = MODELS if models is None else models
    features: set[str] = set()
    for m in registry_models.values():
        features.add("text")
        if m.vision:
            features.add("vision")
    return frozenset(features)


# --- 公開 API: 合成バリデーション ------------------------------------------


def validate_governance(
    composition: DemoComposition,
    *,
    available_models: dict[str, ModelDef] | None = None,
    allowed_connectors: frozenset[str] | set[str] | None = None,
) -> GovernanceReport:
    """デモ構成をガバナンス4制約で検証する(デプロイ前ゲート。副作用なし)。

    - `available_models`: 実行リージョンのモデルレジストリ(既定 `MODELS`=ap-osaka-1)。VLM 等の
      可用性を環境で切り替えて検証するために注入できる。
    - `allowed_connectors`: 許可するコネクタパレット(既定 `CORE_CONNECTORS`=Slack のみ)。

    判定は synth が記述した束縛状態(`bindings[].status`)と SBA-01 の `composition_report` を
    ポリシーへ翻訳する(二重定義しない)。各違反は代替提案つきで返す。
    """
    connectors_palette = (
        CORE_CONNECTORS if allowed_connectors is None else frozenset(allowed_connectors)
    )
    model_features = available_model_features(available_models)
    cap_to_sbas = _capability_to_sbas()

    violations: list[GovernanceViolation] = []

    # 主 SBA を解決できない構成(synth が ok=False)は、これ以上の判定が無意味。致命として弾く。
    if not composition.ok:
        reason = "; ".join(composition.errors) or "合成不能(主SBA 未解決)"
        violations.append(
            GovernanceViolation(
                kind="unresolved_composition",
                element="composition",
                element_type="composition",
                detail=f"デモ構成が成立していない: {reason}",
                alternative=(
                    "ヒアリング Q1 で主業務を確定し(その他→最近傍 SBA を選択)、"
                    "解決可能な主 SBA を選んでから再合成する"
                ),
            )
        )
        return GovernanceReport(
            ok=False,
            sample_app=composition.sample_app,
            violations=violations,
            checks={
                "allowed_combination": False,
                "capabilities_bound": False,
                "permission_scope": False,
                "model_available": False,
                "connector_scope": False,
            },
        )

    # (a) 許可組合せ: 主 SBA に組込点が無い推薦部品(synth の no_slot)は許可外組合せ。
    for b in composition.bindings:
        if b.status == "no_slot":
            others = [s for s in cap_to_sbas.get(b.capability, []) if s != composition.sample_app]
            if others:
                alt = (
                    f"'{b.capability}' を活かすには主アプリを {others} のいずれかにする"
                    f"(または '{b.capability}' を構成から外す)"
                )
            else:
                alt = (
                    f"'{b.capability}' を組込点に持つコア同梱 SBA が現状無い。"
                    f"'{b.capability}' を外す(主役は active な部品へ)か、当該能力を持つ "
                    "sample-app をマーケットから追加する"
                )
            violations.append(
                GovernanceViolation(
                    kind="disallowed_combination",
                    element=b.capability,
                    element_type="capability",
                    detail=(
                        f"主SBA '{composition.sample_app}' に '{b.capability}' の組込点(aiSlot)が"
                        "無い(許可外の sample-app × AI部品 組合せ)"
                    ),
                    alternative=alt,
                )
            )

    # (a) 許可組合せ: コネクタはコアパレット(Slack)に収める。
    for connector in composition.connectors:
        if connector not in connectors_palette:
            violations.append(
                GovernanceViolation(
                    kind="disallowed_combination",
                    element=connector,
                    element_type="connector",
                    detail=(
                        f"コネクタ '{connector}' は許可パレット外"
                        f"(コアコネクタは {sorted(connectors_palette)} のみ)"
                    ),
                    alternative=(
                        "Slack に置き換えるか連携なし(none)にする。Teams/Email 等は"
                        "後段(S3+)のマーケット拡張で追加する"
                    ),
                )
            )

    # (b) 必要ケイパビリティが束縛済み: 組込点はあるが ai_runtime 未束縛(synth の unbound)。
    for b in composition.bindings:
        if b.status == "unbound":
            alt_caps = [c for c in composition.active_parts]
            alt = (
                f"'{b.capability}' のハンドラを ai_runtime に束縛してから合成する(後段)。"
                + (f"当面は束縛済みの {alt_caps} で代替する" if alt_caps else "")
            ).rstrip("。") + "。"
            violations.append(
                GovernanceViolation(
                    kind="unbound_capability",
                    element=b.capability,
                    element_type="capability",
                    detail=(
                        f"'{b.capability}' は組込点はあるが ai_runtime 未束縛"
                        "(このままではデプロイしても実行できない)"
                    ),
                    alternative=alt,
                )
            )

    # (c) 権限スコープが manifest 内: SBA-01 の composition_report を再利用(二重定義しない)。
    report = composition.composition_report
    if report is not None:
        for scope in report.undeclared_permissions:
            violations.append(
                GovernanceViolation(
                    kind="scope_out_of_manifest",
                    element=scope,
                    element_type="permission",
                    detail=(
                        f"スコープ '{scope}' を aiSlot が要求するが manifest.permissions に未宣言"
                        "(権限スコープ逸脱)"
                    ),
                    alternative=(
                        f"manifest.permissions に '{scope}' を宣言する"
                        "(宣言なしにスコープを使わせない)"
                    ),
                )
            )
        # ホストが備えていない必要能力(composition_report.missing_capabilities)。
        for cap in report.missing_capabilities:
            violations.append(
                GovernanceViolation(
                    kind="missing_host_capability",
                    element=cap,
                    element_type="capability",
                    detail=(
                        f"必要能力 '{cap}' をホストインスタンスが備えていない"
                        "(合成バリデーション土台 SBA-01 が検出)"
                    ),
                    alternative=(
                        f"ホストで '{cap}' を有効化するか、'{cap}' を要求しない構成にする"
                    ),
                )
            )

    # (d) モデル可用性(ap-osaka-1): 部品が要求するモデル能力が実行リージョンで使えるか。
    seen_caps: set[str] = set()
    for b in composition.bindings:
        cap = b.capability
        if cap in seen_caps:
            continue
        seen_caps.add(cap)
        feature = CAPABILITY_MODEL_FEATURE.get(cap, DEFAULT_MODEL_FEATURE)
        if feature not in model_features:
            violations.append(
                GovernanceViolation(
                    kind="model_unavailable",
                    element=cap,
                    element_type="capability",
                    detail=(
                        f"'{cap}' は {feature} 対応モデルを要求するが実行リージョンで利用不可"
                        "(モデル可用性チェック / MM-01 依存)"
                    ),
                    alternative=(
                        f"{feature} 対応モデルを利用可能にする(例: VLM の有効化)か、"
                        f"'{cap}' を外して利用可能なモデルで動く能力に置き換える"
                    ),
                )
            )

    # (CON-03) コネクタ invoke スコープ経路: 構成が使う各コネクタ(`composition.connectors`)のうち
    # **パレットが許可するもの**は、必ず **active な束縛**を持ち、その束縛が (1) 既知 Platform 語彙
    # (PLATFORM_SCOPES)の部分集合で、(2) `connector.invoke` を含むことを検証する(invoke 経路が成立
    # する構成だけデプロイ可)。パレット外は上の disallowed_combination が担当(二重計上しない)。
    # **許可しただけで束縛できない/excluded のコネクタはここで弾く**(非コアを許可しても active
    # 束縛が無ければ invoke 経路に載らないため。CON03-MAJ-001)。
    binding_by_provider = {b.provider: b for b in composition.connector_bindings}
    for provider in composition.connectors:
        if provider not in connectors_palette:
            continue  # パレット外 = disallowed_combination で既出。ここでは扱わない。
        cb = binding_by_provider.get(provider)
        if cb is None or cb.status != "active":
            reason = cb.reason if (cb is not None and cb.reason) else "active な束縛が無い"
            violations.append(
                GovernanceViolation(
                    kind="connector_scope_undeclared",
                    element=provider,
                    element_type="connector",
                    detail=(
                        f"コネクタ '{provider}' は許可パレット内だが束縛できない({reason})。"
                        "invoke 経路に載らない構成はデプロイ不可"
                    ),
                    alternative=(
                        f"'{provider}' のコネクタ定義を用意し合成不整合(スコープ未宣言)を解消して"
                        "active に束縛する。当面は連携なし(none)にする"
                    ),
                )
            )
            continue
        # active 束縛: invoke スコープ経路の健全性を検証する。
        unknown_scopes = [s for s in cb.required_scopes if s not in PLATFORM_SCOPES]
        if unknown_scopes:
            violations.append(
                GovernanceViolation(
                    kind="connector_scope_unknown",
                    element=provider,
                    element_type="connector",
                    detail=(
                        f"コネクタ '{provider}' が要求する Platform スコープ "
                        f"{unknown_scopes} は既知語彙(PLATFORM_SCOPES)外(未知は信じない)"
                    ),
                    alternative=(
                        "コネクタ定義の action.permissions を既知 Platform スコープに直す"
                        "(語彙の正本は manifest.PlatformScope)"
                    ),
                )
            )
        if PLATFORM_SCOPE_CONNECTOR_INVOKE not in cb.required_scopes:
            violations.append(
                GovernanceViolation(
                    kind="connector_scope_undeclared",
                    element=provider,
                    element_type="connector",
                    detail=(
                        f"active コネクタ '{provider}' の required_scopes に "
                        f"'{PLATFORM_SCOPE_CONNECTOR_INVOKE}' が無い(invoke 経路が成立しない)"
                    ),
                    alternative=(
                        f"コネクタ束縛に '{PLATFORM_SCOPE_CONNECTOR_INVOKE}' を含める"
                        "(呼ぶ権利そのもの。invoke 層が常に強制する)"
                    ),
                )
            )

    kinds = {v.kind for v in violations}
    checks = {
        "allowed_combination": "disallowed_combination" not in kinds,
        "capabilities_bound": "unbound_capability" not in kinds
        and "missing_host_capability" not in kinds,
        "permission_scope": "scope_out_of_manifest" not in kinds,
        "model_available": "model_unavailable" not in kinds,
        "connector_scope": "connector_scope_undeclared" not in kinds
        and "connector_scope_unknown" not in kinds,
    }
    return GovernanceReport(
        ok=not violations,
        sample_app=composition.sample_app,
        violations=violations,
        checks=checks,
    )
