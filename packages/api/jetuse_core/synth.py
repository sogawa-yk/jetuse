"""合成エンジン(HBD-03)。推薦構成 → 実行可能なデモ構成オブジェクト(＋プレビュー定義)。

HBD-01 の `recommend.Recommendation`(主SBA＋AI部品セット＋コネクタ＋UI＋シード方針)を入力に、
SBA-01/02 の素材(`sample_app_registry` のコア同梱 sample-app 定義 ＋ `ai_runtime` の capability
レジストリ)を束ねて、**デプロイ前にプレビューできるデモ構成**を**合成**する。

設計の要点(出典: docs/enhance/202607-demo-platform-plan.md §5.1 / §10「HBD-03」,
202607-hearing-flow.md §4):

  - **副作用の無い決定的関数**。`synthesize()` は DB に触れず GenAI も呼ばない(プレビューは
    「実行せずに描画」する宣言定義のレンダリング)。実行時バインドの可否は `ai_runtime` の
    束縛レジストリ(`bound_capabilities()`)を**参照するだけ**で、スロットは実行しない。
  - **AI部品は既存 capability レジストリ(`ai_runtime`)から束縛**する。推薦された capability が
    (a) 当該 SBA に組込点(aiSlot)を持ち、かつ (b) `ai_runtime` でハンドラ束縛済みのときだけ
    「実行可能な組込点(active)」として構成に含める。未束縛/組込点なしは構成の active からは
    **外し**、理由付きで `excluded`/`warnings` に残す(§4: 部品は黙って消さず説明する)。
  - **配布表現(再検証可能)を壊さない**。元の検証済み manifest/定義は一切変形しない。合成結果には
    `validate_composition` の `CompositionReport`(必要ケイパ/権限スコープの整合)をそのまま同梱し、
    プレビューから再検証できる形を保つ。
  - **境界(scenario 3)**: 主SBA未確定(Q1=other)や未知 SBA、未束縛 capability・組込点なしの
    推薦は **安全に失敗/警告**する。致命(主SBA を解決できない)は `ok=False` ＋ `errors` で
    レンダリング可能な構成を返し(`strict=True` で `SynthesisError`)、HBD-04 の前段チェックに
    渡せる形にする。

非ゴール: 厳密な合成バリデーション(許可組合せ・権限スコープの網羅判定)は HBD-04。実デプロイ
(コンテナ配備)は S4。本モジュールは「構成生成＋描画用定義」と前段の整合チェックまで。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .plugins import sample_app_registry as registry
from .plugins.ai_runtime import bound_capabilities
from .plugins.connector import validate_connector_composition
from .plugins.core_connectors import connector_invoke_scopes, core_connector
from .plugins.sample_app import (
    CompositionReport,
    SampleAppDefinition,
    validate_composition,
)
from .plugins.sample_app_builtin import sba_a_manifest
from .plugins.sample_app_builtin_c import sba_c_manifest
from .plugins.sample_app_builtin_sba_b import sba_b_manifest
from .recommend import Recommendation

# --- 推薦の SBA コード → コア同梱インスタンス ID ----------------------------

#: 推薦(`Recommendation.sample_app`)の SBA コード → `sample_app_registry` の instance_id。
#: SBA-D(経理)は未実装(コア同梱は A/B/C の3本。§10 では A/B/C/D の4本予定だが D は後段)。
#: 出典: plugins/sample_app_builtin*.py の `*_INSTANCE_ID`。
SBA_CODE_TO_INSTANCE: dict[str, str] = {
    "SBA-A": registry.SBA_A_INSTANCE_ID,
    "SBA-B": registry.SBA_B_INSTANCE_ID,
    "SBA-C": registry.SBA_C_INSTANCE_ID,
}

#: SBA コード → 検証済み manifest アクセサ(validate_composition 用。配布表現の再検証に使う)。
#: manifest を持つ SBA だけ整合チェックを同梱する(無い SBA は composition_report=None)。
_SBA_MANIFEST = {
    "SBA-A": sba_a_manifest,
    "SBA-B": sba_b_manifest,
    "SBA-C": sba_c_manifest,
}


def sba_manifest_permissions(code: str | None) -> frozenset[str] | None:
    """SBA コードの**配備主体 manifest** が宣言する Platform 権限スコープ集合を返す。

    manifest を持たない SBA(後段の SBA-D 等)は None(検証不能)。governance の方式A 担保
    (コネクタを束ねる配備主体は `platform:connector.invoke` を宣言していなければならない。
    ADR-0020 D7)で、grant/approve が閉じる先である manifest.permissions を参照するために使う。
    """
    fn = _SBA_MANIFEST.get(code or "")
    if fn is None:
        return None
    return frozenset(fn().permissions)


class SynthesisError(ValueError):
    """合成が成立しない(主SBA を解決できない等)ときに送出する(strict=True 時)。"""


# --- 構成サブモデル --------------------------------------------------------


class SlotBinding(BaseModel):
    """推薦された AI 部品(capability)を SBA の組込点へ束縛した結果(1 capability 分)。

    `status`:
      - `active`   : 当該 SBA に組込点があり、`ai_runtime` でハンドラ束縛済み(実行可能)。
      - `unbound`  : 組込点はあるが `ai_runtime` 未束縛(このステージでは実行不可。後段で束縛)。
      - `no_slot`  : 推薦されたが当該 SBA に組込点(aiSlot)が無い(別 SBA 向け部品)。
    """

    model_config = ConfigDict(extra="forbid")

    capability: str
    status: str
    #: この capability を持つ aiSlot のキー(複数画面に跨ることがある)。no_slot のとき空。
    slot_keys: list[str]
    #: 組込点が現れる画面キー(プレビューの組込点表示用)。
    screen_keys: list[str]
    #: aiSlot のタイトル(代表 1 件。表示用)。
    title: str | None
    #: 推薦の主役 capability(Q3 由来)か。
    highlight: bool
    #: aiSlot が要求する Platform スコープ(和集合)。
    permissions: list[str]
    #: active でない理由(unbound/no_slot のときに人間向け説明)。
    reason: str | None = None


class ConnectorBinding(BaseModel):
    """推薦されたコネクタを **コアコネクタ・パレットへ束縛**した結果(1 provider 分)。

    AI 部品(`SlotBinding`)と同じく「推薦を黙って消さず、束縛できたか/外したかを理由付きで残す」。

    `status`:
      - `active`   : コアパレットに在り、かつコネクタ合成バリデーション(宣言整合)が ok。
                     `required_scopes` の短期トークンを broker から得れば invoke 経路に載せられる。
      - `excluded` : パレット外(後段マーケット)、または合成不整合(action 要求スコープ未宣言)。
                     `reason` に外した理由を残す(synth の warnings にも引き継ぐ)。
    """

    model_config = ConfigDict(extra="forbid")

    #: 接続先 SaaS の安定キー(slack 等)。推薦 `connectors` の要素。
    provider: str
    status: str
    #: コアコネクタの接続方式(builtin/mcp)。excluded(パレット外)で定義が無いとき None。
    transport: str | None
    #: コネクタが公開する action 名。excluded(パレット外)で空。
    actions: list[str]
    #: このコネクタを invoke するのに要る Platform スコープ(invoke スコープ＋action 宣言スコープ)。
    required_scopes: list[str]
    #: install 時に Vault へ束ねる秘密が要るか(auth.kind!=none)。
    requires_secret: bool
    #: 束ねるべき秘密の参照名(requires_secret のとき非 None。**実値ではない**)。
    secret_ref: str | None
    #: active でない理由(excluded のときに人間向け説明)。
    reason: str | None = None


class ScreenView(BaseModel):
    """プレビューに描く 1 画面(SBA 定義の screen ＋ active な組込点)。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    type: str
    dataset: str | None
    #: この画面で実行可能(active)な組込点 = (slot_key, capability, title, highlight)。
    slots: list[dict[str, Any]]


class SeedPlan(BaseModel):
    """シード方針(Q6)を反映したデータ計画。プレビューの「使うデータ」表示に使う。"""

    model_config = ConfigDict(extra="forbid")

    #: sample | genai_generated | replace_later。
    strategy: str
    #: 人間向けの方針説明(プレビュー表示用)。
    note: str
    #: 実際にプレビュー/取込へ載せるシード行を持つか(replace_later/genai は載せない)。
    seeded: bool
    #: データセットごとの計画(name/label/fields件数/seed行数)。
    datasets: list[dict[str, Any]]
    #: 載せるシード総行数(seeded=False なら 0)。
    total_seed_rows: int


class DemoComposition(BaseModel):
    """合成したデモ構成オブジェクト(プレビュー定義の正本)。

    「画面・組込点・使う AI・データ」を実行せずに描画できる宣言表現。元の検証済み定義は
    変形せず、`composition_report` に整合チェック結果を同梱する(配布表現は再検証可能)。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    #: 推薦の主 SBA コード(SBA-A/B/C/D)。
    sample_app: str | None
    #: コア同梱インスタンス ID(解決できたとき)。
    instance_id: str | None
    app_name: str | None
    summary: str
    icon: str
    #: UI/出力テンプレ(chat | notify | report)。
    ui: str | None
    #: 推薦されたコネクタ(provider 名)。**後方互換**で生の推薦リストを保持(summary.py 等が参照)。
    connectors: list[str]
    #: 推薦コネクタの束縛結果(active/excluded・理由付き)。CON-03 で追加。**後方互換**: 旧 payload
    #: (本フィールド以前の DemoComposition)を `model_validate` できるよう既定空リストにする。
    connector_bindings: list[ConnectorBinding] = Field(default_factory=list)
    #: 束縛済み(active)=デプロイ後に invoke 経路へ載せられるコネクタ provider(既定空・後方互換)。
    active_connectors: list[str] = Field(default_factory=list)
    #: 主役 capability(Q3 由来)。
    highlight: str | None
    #: 描画する画面(active 組込点付き)。
    screens: list[ScreenView]
    #: 推薦された全 AI 部品の束縛結果(active/unbound/no_slot)。
    bindings: list[SlotBinding]
    #: 実行可能(active)な capability。
    active_parts: list[str]
    #: 構成から外した部品(capability → 理由)。
    excluded: list[dict[str, str]]
    #: シード計画(Q6 反映)。
    seed: SeedPlan
    #: 合成バリデーション(必要ケイパ/権限スコープの整合)。再検証可能な配布表現。
    composition_report: CompositionReport | None
    #: 非致命の注意(部品を外した理由・依存能力など)。
    warnings: list[str]
    #: 致命(合成不能の理由。ok=False のとき非空)。
    errors: list[str]


# --- シード方針の説明文(Q6) -----------------------------------------------

_SEED_NOTES: dict[str, tuple[str, bool]] = {
    # strategy: (note, seeded=実シードを載せるか)
    "sample": ("コア同梱のサンプルシードをそのまま投入する(すぐ動くデモ)。", True),
    "genai_generated": (
        "業種に寄せたデータを GenAI で生成して投入する(生成は取込時の別ターン)。"
        "プレビュー時点では未生成のため、データ計画は構造のみで投入予定行は 0 行。",
        False,
    ),
    "replace_later": (
        "シードは投入せず、後で顧客の実データに差し替える(プレビューは空のデータ計画)。",
        False,
    ),
}


def _seed_plan(definition: SampleAppDefinition, strategy: str) -> SeedPlan:
    """シード方針を構成へ反映する。replace_later は実行を載せない(空のデータ計画)。"""
    note, seeded = _SEED_NOTES.get(strategy, (f"未知のシード方針 '{strategy}'。", False))
    datasets: list[dict[str, Any]] = []
    total = 0
    for d in definition.datasets:
        rows = len(d.seed) if seeded else 0
        total += rows
        datasets.append(
            {
                "name": d.name,
                "label": d.label or d.name,
                "fields": len(d.fields),
                "seed_rows": rows,
            }
        )
    return SeedPlan(
        strategy=strategy,
        note=note,
        seeded=seeded,
        datasets=datasets,
        total_seed_rows=total,
    )


def _bindings(
    definition: SampleAppDefinition,
    recommendation: Recommendation,
) -> tuple[list[SlotBinding], list[ScreenView]]:
    """推薦の ai_parts を SBA の組込点へ束縛し、binding と画面ビューを作る。

    capability ごとに当該 SBA の aiSlot を探し、`ai_runtime` の束縛有無で active/unbound を分ける。
    組込点が無い推薦部品は no_slot。SBA 固有だが推薦に無い部品は構成に含めない(推薦が正)。
    """
    bound = bound_capabilities()
    slots_by_cap: dict[str, list[Any]] = {}
    for slot in definition.ai_slots:
        slots_by_cap.setdefault(slot.capability, []).append(slot)
    # capability → その capability を載せる画面キー(定義の screen.slots を辿る)。
    screens_by_slot: dict[str, list[str]] = {}
    for screen in definition.screens:
        for slot_key in screen.slots:
            screens_by_slot.setdefault(slot_key, []).append(screen.key)

    bindings: list[SlotBinding] = []
    active_slot_keys: set[str] = set()
    for cap in recommendation.ai_parts:
        cap_slots = slots_by_cap.get(cap, [])
        is_highlight = cap == recommendation.highlight
        if not cap_slots:
            bindings.append(
                SlotBinding(
                    capability=cap,
                    status="no_slot",
                    slot_keys=[],
                    screen_keys=[],
                    title=None,
                    highlight=is_highlight,
                    permissions=[],
                    reason=f"推薦部品 '{cap}' は主SBA に組込点(aiSlot)が無い(別SBA 向け部品)",
                )
            )
            continue
        slot_keys = [s.key for s in cap_slots]
        screen_keys: list[str] = []
        for sk in slot_keys:
            screen_keys.extend(screens_by_slot.get(sk, []))
        perms = sorted({p for s in cap_slots for p in s.permissions})
        if cap in bound:
            active_slot_keys.update(slot_keys)
            bindings.append(
                SlotBinding(
                    capability=cap,
                    status="active",
                    slot_keys=slot_keys,
                    screen_keys=sorted(set(screen_keys)),
                    title=cap_slots[0].title,
                    highlight=is_highlight,
                    permissions=perms,
                    reason=None,
                )
            )
        else:
            bindings.append(
                SlotBinding(
                    capability=cap,
                    status="unbound",
                    slot_keys=slot_keys,
                    screen_keys=sorted(set(screen_keys)),
                    title=cap_slots[0].title,
                    highlight=is_highlight,
                    permissions=perms,
                    reason=f"capability '{cap}' は ai_runtime で未束縛(このステージでは実行不可)",
                )
            )

    # 画面ビュー: active な組込点だけを各画面に配置する(実行できないスロットは描かない)。
    slot_meta = {s.key: s for s in definition.ai_slots}
    cap_highlight = recommendation.highlight
    screens: list[ScreenView] = []
    for screen in definition.screens:
        view_slots: list[dict[str, Any]] = []
        for slot_key in screen.slots:
            if slot_key not in active_slot_keys:
                continue
            slot = slot_meta[slot_key]
            view_slots.append(
                {
                    "slot_key": slot.key,
                    "capability": slot.capability,
                    "title": slot.title,
                    "highlight": slot.capability == cap_highlight,
                }
            )
        screens.append(
            ScreenView(
                key=screen.key,
                title=screen.title,
                type=screen.type,
                dataset=screen.dataset,
                slots=view_slots,
            )
        )
    return bindings, screens


def _connector_bindings(
    recommendation: Recommendation,
) -> tuple[list[ConnectorBinding], list[str]]:
    """推薦 `connectors` をコアコネクタ・パレットへ束縛し、binding と警告を作る。

    provider ごとに `core_connector()` で引き当て、コネクタ合成バリデーション(宣言整合)で active/
    excluded を分ける。重複 provider は最初の 1 件のみ束縛する(順序を保ちつつ二重束縛を避ける)。
    パレット外・合成不整合は黙って消さず excluded＋理由を残し、warnings にも引き継ぐ(§4)。
    元の検証済み定義は変形しない。
    """
    bindings: list[ConnectorBinding] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for provider in recommendation.connectors:
        if provider in seen:
            continue
        seen.add(provider)
        core = core_connector(provider)
        if core is None:
            reason = (
                f"コネクタ '{provider}' はコアパレット外(コアコネクタは Slack のみ)。"
                "後段マーケット(S3+)で追加するか連携なし(none)にする"
            )
            bindings.append(
                ConnectorBinding(
                    provider=provider,
                    status="excluded",
                    transport=None,
                    actions=[],
                    required_scopes=[],
                    requires_secret=False,
                    secret_ref=None,
                    reason=reason,
                )
            )
            warnings.append(reason)
            continue
        definition = core.definition()
        report = validate_connector_composition(core.manifest(), definition=definition)
        scopes = connector_invoke_scopes(definition)
        action_names = [a.name for a in definition.actions]
        if not report.ok:
            reason = (
                f"コネクタ '{provider}' は合成不整合(action 要求スコープ未宣言: "
                f"{report.undeclared_permissions})。manifest.permissions に宣言してから束縛する"
            )
            bindings.append(
                ConnectorBinding(
                    provider=provider,
                    status="excluded",
                    transport=definition.transport,
                    actions=action_names,
                    required_scopes=scopes,
                    requires_secret=report.requires_secret,
                    secret_ref=report.secret_ref,
                    reason=reason,
                )
            )
            warnings.append(reason)
            continue
        bindings.append(
            ConnectorBinding(
                provider=provider,
                status="active",
                transport=definition.transport,
                actions=action_names,
                required_scopes=scopes,
                requires_secret=report.requires_secret,
                secret_ref=report.secret_ref,
                reason=None,
            )
        )
    return bindings, warnings


def _empty_composition(
    recommendation: Recommendation, errors: list[str], warnings: list[str]
) -> DemoComposition:
    """主SBA を解決できないときの、描画可能な失敗構成(ok=False)。"""
    return DemoComposition(
        ok=False,
        sample_app=recommendation.sample_app,
        instance_id=None,
        app_name=None,
        summary="",
        icon="🧩",
        ui=recommendation.ui,
        connectors=list(recommendation.connectors),
        connector_bindings=[],
        active_connectors=[],
        highlight=recommendation.highlight,
        screens=[],
        bindings=[],
        active_parts=[],
        excluded=[],
        seed=SeedPlan(
            strategy=recommendation.seed_strategy,
            note=_SEED_NOTES.get(recommendation.seed_strategy, ("", False))[0],
            seeded=False,
            datasets=[],
            total_seed_rows=0,
        ),
        composition_report=None,
        warnings=warnings,
        errors=errors,
    )


def synthesize(
    recommendation: Recommendation,
    *,
    available_capabilities: frozenset[str] | set[str] | None = None,
    strict: bool = False,
) -> DemoComposition:
    """推薦構成 → デモ構成オブジェクトを**決定的に**合成する(副作用なし)。

    - `available_capabilities`: ホストが備える JetUse 能力集合(None なら全コア能力)。
      合成バリデーション(`validate_composition`)の母集合に渡す。
    - `strict`: True なら主SBA を解決できないとき `SynthesisError`。False(既定)なら描画可能な
      失敗構成(ok=False)を返す(プレビューで「合成不能」を安全に表示するため)。

    AI 部品は `ai_runtime` の束縛レジストリから束縛し、未束縛/組込点なしは active から外して
    理由を残す。元の検証済み定義は変形しない(配布表現は再検証可能)。
    """
    code = recommendation.sample_app
    warnings: list[str] = []
    errors: list[str] = []

    if code is None:
        errors.append(
            "主SBA が未確定(Q1=その他)。最近傍 SBA を確定してから合成してください"
        )
        if strict:
            raise SynthesisError(errors[0])
        return _empty_composition(recommendation, errors, warnings)

    instance_id = SBA_CODE_TO_INSTANCE.get(code)
    resolved = registry.resolve_app(instance_id) if instance_id else None
    if resolved is None:
        errors.append(
            f"主SBA '{code}' に対応するコア同梱 sample-app が無い(未実装 SBA か未知コード)"
        )
        if strict:
            raise SynthesisError(errors[0])
        return _empty_composition(recommendation, errors, warnings)

    definition = resolved.definition
    summary_row = next(
        (s for s in registry.list_sample_apps() if s["id"] == instance_id), {}
    )

    bindings, screens = _bindings(definition, recommendation)

    # コネクタ束縛(CON-03): 推薦コネクタをコアパレットへ束縛し active/excluded を理由付きで残す。
    connector_bindings, connector_warnings = _connector_bindings(recommendation)
    active_connectors = [c.provider for c in connector_bindings if c.status == "active"]
    warnings.extend(connector_warnings)

    active_parts = [b.capability for b in bindings if b.status == "active"]
    excluded = [
        {"capability": b.capability, "status": b.status, "reason": b.reason or ""}
        for b in bindings
        if b.status != "active"
    ]
    for b in bindings:
        if b.status == "unbound":
            warnings.append(b.reason or f"capability '{b.capability}' は未束縛")
        elif b.status == "no_slot":
            warnings.append(b.reason or f"推薦部品 '{b.capability}' は組込点なし")
    # 推薦の Auto バリデーション(ホスト既定能力との照合)の警告も引き継ぐ。
    warnings.extend(recommendation.validation.warnings)

    # 配布表現(再検証可能): 元 manifest があれば validate_composition を同梱する。
    composition_report: CompositionReport | None = None
    manifest_fn = _SBA_MANIFEST.get(code)
    if manifest_fn is not None:
        composition_report = validate_composition(
            manifest_fn(),
            available_capabilities=available_capabilities,
            definition=definition,
        )
        if not composition_report.ok:
            # 必要ケイパ不足/権限未宣言は前段の整合違反として警告に上げる(致命判定は HBD-04)。
            if composition_report.missing_capabilities:
                warnings.append(
                    "ホストに無い必要 capability: "
                    f"{composition_report.missing_capabilities}"
                )
            if composition_report.undeclared_permissions:
                warnings.append(
                    "manifest 未宣言の権限スコープ: "
                    f"{composition_report.undeclared_permissions}"
                )

    if not active_parts:
        warnings.append(
            "実行可能な AI 組込点が 1 つも無い(全推薦部品が未束縛/組込点なし)"
        )

    return DemoComposition(
        ok=True,
        sample_app=code,
        instance_id=instance_id,
        app_name=summary_row.get("name"),
        summary=definition.summary,
        icon=summary_row.get("icon", "🧩"),
        ui=recommendation.ui,
        connectors=list(recommendation.connectors),
        connector_bindings=connector_bindings,
        active_connectors=active_connectors,
        highlight=recommendation.highlight,
        screens=screens,
        bindings=bindings,
        active_parts=active_parts,
        excluded=excluded,
        seed=_seed_plan(definition, recommendation.seed_strategy),
        composition_report=composition_report,
        warnings=warnings,
        errors=errors,
    )
