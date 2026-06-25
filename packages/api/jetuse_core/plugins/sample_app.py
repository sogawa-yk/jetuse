"""`kind: sample-app` の contributes 詳細スキーマと合成バリデーション土台(SBA-01)。

`manifest.py` は `kind` と `contributes` のキー対応までを強制する(L1)。本モジュールは
`kind: sample-app` の `contributes["sample-app"]` ペイロード——**UI テンプレ(screens)＋
データモデル/シード(datasets)＋AI 組込スロット(aiSlots)**——を pydantic で構造検証する
(spec 出典: docs/enhance/202607-demo-platform-plan.md §6 D9 / specs/16-platform.md §5)。

設計方針:
  - sample-app は「scaffold テンプレート」であり、取込(scaffold)時にインスタンスへ展開される
    (展開ロジックは `scaffold.py`)。本モジュールは **定義の妥当性** と **必要ケイパビリティ/
    権限スコープの宣言抽出**(合成バリデーションの土台)に責務を限定する。
  - AI 組込スロット(aiSlot)は「画面のどこに JetUse のどの AI 能力(capability)を差し込むか」の
    宣言。具体的な実行時バインドは SBA-02 の非ゴール。ここでは **どの能力/スコープを要求するか**
    を宣言し、ホストインスタンスがそれを満たすかを `validate_composition` が判定できるようにする。
  - 合成バリデーション本体(許可組合せ・テナント境界等)はステージ2 HBD-04。本タスクは
    「必要ケイパビリティ不足の検出」「権限スコープの宣言整合(aiSlot ⊆ manifest.permissions)」までの
    **土台**を置く。

`contributes["sample-app"]` は JSON 配布物であり、検証済み manifest が正準 JSON 化できる範囲
(manifest._assert_json_value)に収まる。seed 行も JSON 値のみ。
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .manifest import PlatformScope, PluginManifest

# --- 語彙(仕様の正本) ----------------------------------------------------

#: AI 組込スロットが要求できる JetUse コア能力(capability)の語彙。
#: 出典: 202607-demo-platform-plan.md §6 のコア同梱サンプルアプリ表「使う JetUse 能力」。
#:   rag.search=RAG(File Search) / summarize=要約 / classify=自動分類 / nl2sql=自然言語DB照会 /
#:   chart=結果グラフ化 / agent=エージェント(ツール) / minutes=議事録要約 /
#:   draft=返信・メール下書き / vlm.ocr=VLM-OCR(マルチモーダル)。
SampleAppCapability = Literal[
    "rag.search",
    "summarize",
    "classify",
    "nl2sql",
    "chart",
    "agent",
    "minutes",
    "draft",
    "vlm.ocr",
]
SAMPLE_APP_CAPABILITIES = frozenset(get_args(SampleAppCapability))

#: データモデルの列型(scaffold が展開する最小語彙)。
FieldType = Literal["string", "text", "number", "boolean", "date", "datetime"]

#: 画面テンプレの種別(UI テンプレの最小語彙。具体描画は SBA-02 以降)。
ScreenKind = Literal["list", "detail", "form", "dashboard", "board"]

#: ホストインスタンスが既定で備えるとみなす能力(合成バリデーションの既定の母集合)。
#: 実際の保有能力は実行時にインスタンスから与える(validate_composition の引数)。ここでは
#: 「コア能力は全て利用可能」を既定とし、不足検出を呼び出し側が能力集合を絞って試せるようにする。
DEFAULT_HOST_CAPABILITIES = frozenset(SAMPLE_APP_CAPABILITIES)

#: 識別子(dataset 名・screen/slot キー・field 名)の長さ上限。
MAX_KEY_LEN = 64
MAX_TITLE_LEN = 200
#: 暴走展開・肥大な定義による DoS/storage 浪費を防ぐ件数上限(scaffold が ADB へ展開するため)。
MAX_SEED_ROWS_PER_DATASET = 1000
MAX_TOTAL_SEED_ROWS = 5000
MAX_SCREENS = 50
MAX_DATASETS = 30
MAX_AI_SLOTS = 50
MAX_FIELDS_PER_DATASET = 60
MAX_SLOTS_PER_SCREEN = 50


class SampleAppError(ValueError):
    """sample-app 定義が仕様に適合しないときに送出する。"""


def _value_matches_type(field_type: str, value: Any) -> bool:
    """seed 値が宣言したフィールド型に整合するか(scaffold 展開前の型検証)。

    null は許容(required は別途チェック)。JSON では date/datetime は文字列なので ISO 形式を要求。
    bool は number として扱わない(JSON では別型)。
    """
    if value is None:
        return True
    if field_type in ("string", "text"):
        return isinstance(value, str)
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        # 検証済み定義は JSON 安全でなければならない(plain dict 経由でも NaN/Infinity を弾く)。
        return math.isfinite(value)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type in ("date", "datetime"):
        if not isinstance(value, str):
            return False
        try:
            (_dt.date if field_type == "date" else _dt.datetime).fromisoformat(value)
        except ValueError:
            return False
        return True
    return True  # pragma: no cover - FieldType の Literal 以外は来ない


# --- サブモデル -----------------------------------------------------------


class DatasetField(BaseModel):
    """データモデルの 1 列。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    type: FieldType
    label: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    required: bool = False


class Dataset(BaseModel):
    """業務データモデル＋初期シード。scaffold が行を展開する単位。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=r"^[a-z][a-z0-9_]*$")
    label: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    fields: list[DatasetField] = Field(min_length=1, max_length=MAX_FIELDS_PER_DATASET)
    seed: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_fields_and_seed(self) -> Dataset:
        names = [f.name for f in self.fields]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"dataset '{self.name}': フィールド名が重複: {sorted(dup)}")
        if len(self.seed) > MAX_SEED_ROWS_PER_DATASET:
            raise ValueError(
                f"dataset '{self.name}': seed 行が上限 {MAX_SEED_ROWS_PER_DATASET} を超える"
            )
        by_name = {f.name: f for f in self.fields}
        for i, row in enumerate(self.seed):
            extra = set(row) - set(by_name)
            if extra:
                raise ValueError(
                    f"dataset '{self.name}' seed[{i}]: 未知のフィールド {sorted(extra)}"
                )
            for f in self.fields:
                if f.required and row.get(f.name) in (None, ""):
                    raise ValueError(
                        f"dataset '{self.name}' seed[{i}]: 必須フィールド '{f.name}' が空"
                    )
            # seed 値を宣言した型と照合する(scaffold で ADB へ展開する前の型健全性)。
            for key, value in row.items():
                if not _value_matches_type(by_name[key].type, value):
                    raise ValueError(
                        f"dataset '{self.name}' seed[{i}]: フィールド '{key}' の値が "
                        f"型 '{by_name[key].type}' に整合しない: {value!r}"
                    )
        return self


class AiSlot(BaseModel):
    """AI 組込スロット = 画面のどこに JetUse のどの能力を差し込むかの宣言。

    `capability` で必要な JetUse コア能力を、`permissions` で必要な Platform API スコープを宣言する
    (合成バリデーションの土台)。`permissions` は manifest 全体の `permissions` の部分集合でなければ
    ならない(整合は `validate_composition` が判定)。
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=r"^[a-z][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    capability: SampleAppCapability
    permissions: list[PlatformScope] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dup_permissions(self) -> AiSlot:
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError(f"aiSlot '{self.key}': permissions に重複がある")
        return self


class Screen(BaseModel):
    """UI テンプレの 1 画面。`dataset` は datasets を、`slots` は aiSlots のキーを参照する。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=MAX_KEY_LEN, pattern=r"^[a-z][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=MAX_TITLE_LEN)
    type: ScreenKind
    #: 主に表示する dataset(なければ None=データ非依存の画面)。
    dataset: str | None = Field(default=None, max_length=MAX_KEY_LEN)
    #: この画面に差し込む aiSlot のキー(件数上限あり・重複不可。肥大化と多重バインドを防ぐ)。
    slots: list[str] = Field(default_factory=list, max_length=MAX_SLOTS_PER_SCREEN)

    @model_validator(mode="after")
    def _no_dup_slots(self) -> Screen:
        if len(set(self.slots)) != len(self.slots):
            dup = sorted({k for k in self.slots if self.slots.count(k) > 1})
            raise ValueError(f"screen '{self.key}': slots に重複がある: {dup}")
        return self


# --- ルート定義 -----------------------------------------------------------


class SampleAppDefinition(BaseModel):
    """`contributes["sample-app"]` のルート。UI テンプレ＋データモデル＋AI 組込スロット。"""

    model_config = ConfigDict(extra="forbid")

    screens: list[Screen] = Field(min_length=1, max_length=MAX_SCREENS)
    datasets: list[Dataset] = Field(default_factory=list, max_length=MAX_DATASETS)
    ai_slots: list[AiSlot] = Field(
        default_factory=list, alias="aiSlots", max_length=MAX_AI_SLOTS
    )
    #: 表示用の説明(任意)。
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _check_references(self) -> SampleAppDefinition:
        # sample-app 全体の seed 総数上限(多数 dataset による展開肥大を防ぐ)。
        total_seed = sum(len(d.seed) for d in self.datasets)
        if total_seed > MAX_TOTAL_SEED_ROWS:
            raise ValueError(
                f"seed 総数 {total_seed} が上限 {MAX_TOTAL_SEED_ROWS} を超える"
            )

        # キー一意性。
        for label, keys in (
            ("screen", [s.key for s in self.screens]),
            ("dataset", [d.name for d in self.datasets]),
            ("aiSlot", [a.key for a in self.ai_slots]),
        ):
            dup = {k for k in keys if keys.count(k) > 1}
            if dup:
                raise ValueError(f"{label} のキーが重複: {sorted(dup)}")

        dataset_names = {d.name for d in self.datasets}
        slot_keys = {a.key for a in self.ai_slots}
        for s in self.screens:
            if s.dataset is not None and s.dataset not in dataset_names:
                raise ValueError(
                    f"screen '{s.key}': 参照する dataset '{s.dataset}' が datasets に無い"
                )
            missing = [k for k in s.slots if k not in slot_keys]
            if missing:
                raise ValueError(
                    f"screen '{s.key}': 参照する aiSlot {missing} が aiSlots に無い"
                )
        return self


# --- 公開 API: 定義検証 ----------------------------------------------------


def _coerce_definition(source: PluginManifest | dict[str, Any]) -> dict[str, Any]:
    """manifest または contributes["sample-app"] dict から定義 dict を取り出す。"""
    if isinstance(source, PluginManifest):
        if source.kind != "sample-app":
            raise SampleAppError(
                f"kind が 'sample-app' でない manifest を検証できない: {source.kind}"
            )
        try:
            return source.contributes["sample-app"]
        except KeyError as e:  # pragma: no cover - manifest 検証済みなら起きない
            raise SampleAppError("contributes['sample-app'] が無い") from e
    return source


def validate_sample_app(source: PluginManifest | dict[str, Any]) -> SampleAppDefinition:
    """sample-app 定義を検証して返す。不正なら SampleAppError。

    引数は検証済み `PluginManifest`(kind=sample-app)か、`contributes["sample-app"]` 相当の dict。
    """
    data = _coerce_definition(source)
    try:
        return SampleAppDefinition.model_validate(data)
    except ValidationError as e:
        raise SampleAppError(str(e)) from e


def sample_app_json_schema() -> dict[str, Any]:
    """sample-app 定義(contributes["sample-app"])の JSON Schema(camelCase 別名)。"""
    return SampleAppDefinition.model_json_schema(by_alias=True)


# --- 公開 API: 合成バリデーション土台 --------------------------------------


def required_capabilities(definition: SampleAppDefinition) -> set[str]:
    """この sample-app が要求する JetUse コア能力の集合(aiSlots から導出)。"""
    return {slot.capability for slot in definition.ai_slots}


def required_permissions(definition: SampleAppDefinition) -> set[str]:
    """この sample-app が要求する Platform API スコープの集合(aiSlots の和集合)。"""
    perms: set[str] = set()
    for slot in definition.ai_slots:
        perms.update(slot.permissions)
    return perms


class CompositionReport(BaseModel):
    """合成バリデーション結果。`ok` は致命的不足が無いことを表す。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    required_capabilities: list[str]
    required_permissions: list[str]
    #: ホストが備えていない必要能力(致命: scaffold 展開を拒否すべき)。
    missing_capabilities: list[str]
    #: aiSlot が要求するが manifest.permissions で宣言されていないスコープ(致命: 宣言整合違反)。
    undeclared_permissions: list[str]
    #: manifest.permissions のうち実際にどの aiSlot からも使われないスコープ(警告)。
    unused_permissions: list[str]


class CompositionError(SampleAppError):
    """合成バリデーションで致命的不足を検出したときに送出する。`report` に詳細を持つ。"""

    def __init__(self, report: CompositionReport):
        self.report = report
        super().__init__(
            "合成バリデーション失敗: "
            f"missing_capabilities={report.missing_capabilities}, "
            f"undeclared_permissions={report.undeclared_permissions}"
        )


def validate_composition(
    manifest: PluginManifest,
    *,
    available_capabilities: frozenset[str] | set[str] | None = None,
    definition: SampleAppDefinition | None = None,
) -> CompositionReport:
    """sample-app をホストインスタンスへ合成可能か判定する(土台)。

    - `available_capabilities`: ホストが備える JetUse 能力集合。None なら全コア能力
      (DEFAULT_HOST_CAPABILITIES)。能力が足りなければ `missing_capabilities` に並ぶ。
    - aiSlot が要求するスコープが manifest.permissions に宣言されていなければ
      `undeclared_permissions`(宣言整合違反)。
    - `ok` は missing_capabilities と undeclared_permissions が共に空のとき True。

    本関数は副作用を持たない(DB に触れない)。展開は `scaffold.py` が ok を確認してから行う。
    """
    # definition を渡された経路でも kind を必ず確認する(別 kind の manifest と sample-app 定義を
    # 取り違えて合成レポートを返すのを防ぐ)。
    if manifest.kind != "sample-app":
        raise SampleAppError(
            f"kind が 'sample-app' でない manifest は合成できない: {manifest.kind}"
        )
    if definition is None:
        definition = validate_sample_app(manifest)
    avail = (
        DEFAULT_HOST_CAPABILITIES
        if available_capabilities is None
        else frozenset(available_capabilities)
    )
    req_caps = required_capabilities(definition)
    req_perms = required_permissions(definition)
    declared = set(manifest.permissions)

    missing_caps = sorted(req_caps - avail)
    undeclared = sorted(req_perms - declared)
    unused = sorted(declared - req_perms)
    return CompositionReport(
        ok=not missing_caps and not undeclared,
        required_capabilities=sorted(req_caps),
        required_permissions=sorted(req_perms),
        missing_capabilities=missing_caps,
        undeclared_permissions=undeclared,
        unused_permissions=unused,
    )
