"""sample-app 定義スキーマ＋合成バリデーション土台のテスト(SBA-01)。

DB 非依存。manifest(kind: sample-app)の contributes 構造検証と、必要ケイパビリティ/
権限スコープの宣言抽出・不足検出を網羅する。
"""

import copy

import pytest

from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest
from jetuse_core.plugins.sample_app import (
    SAMPLE_APP_CAPABILITIES,
    CompositionError,
    SampleAppError,
    required_capabilities,
    required_permissions,
    sample_app_json_schema,
    validate_composition,
    validate_sample_app,
)


def _definition() -> dict:
    return {
        "summary": "問い合わせ/サポート管理",
        "datasets": [
            {
                "name": "tickets",
                "label": "問い合わせ",
                "fields": [
                    {"name": "subject", "type": "string", "required": True},
                    {"name": "body", "type": "text"},
                    {"name": "category", "type": "string"},
                ],
                "seed": [
                    {"subject": "ログインできない", "body": "...", "category": "認証"},
                    {"subject": "請求について", "body": "...", "category": "請求"},
                ],
            }
        ],
        "aiSlots": [
            {
                "key": "faq-answer",
                "title": "FAQ回答",
                "capability": "rag.search",
                "permissions": ["platform:rag.search"],
            },
            {"key": "auto-classify", "title": "自動分類", "capability": "classify"},
        ],
        "screens": [
            {
                "key": "inbox",
                "title": "受信一覧",
                "type": "list",
                "dataset": "tickets",
                "slots": ["auto-classify"],
            },
            {
                "key": "detail",
                "title": "詳細",
                "type": "detail",
                "dataset": "tickets",
                "slots": ["faq-answer"],
            },
        ],
    }


def _manifest(definition: dict | None = None, permissions=None) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "jetuse/support-desk",
        "version": "1.0.0",
        "kind": "sample-app",
        "name": "問い合わせ管理",
        "publisher": "jetuse",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:rag.search"] if permissions is None else permissions,
        "contributes": {"sample-app": definition if definition is not None else _definition()},
    }


# --- manifest が sample-app を受理する ----------------------------------------


def test_manifest_accepts_sample_app_kind():
    m = validate_manifest(_manifest())
    assert m.kind == "sample-app"
    # contributes のキーは kind と一致する(manifest.py の cross-field 制約)。
    assert set(m.contributes) == {"sample-app"}


def test_manifest_rejects_sample_app_contributes_key_mismatch():
    from jetuse_core.plugins.manifest import ManifestError

    data = _manifest()
    data["contributes"] = {"usecase": _definition()}  # kind と不一致
    with pytest.raises(ManifestError):
        validate_manifest(data)


# --- 定義スキーマ検証 ---------------------------------------------------------


def test_valid_definition_parses_and_roundtrips():
    m = validate_manifest(_manifest())
    d = validate_sample_app(m)
    assert [s.key for s in d.screens] == ["inbox", "detail"]
    assert [ds.name for ds in d.datasets] == ["tickets"]
    assert [a.key for a in d.ai_slots] == ["faq-answer", "auto-classify"]
    # camelCase(aiSlots)で往復できる。
    dumped = d.model_dump(by_alias=True)
    assert "aiSlots" in dumped and "ai_slots" not in dumped


def test_required_capabilities_and_permissions_derived_from_slots():
    d = validate_sample_app(validate_manifest(_manifest()))
    assert required_capabilities(d) == {"rag.search", "classify"}
    assert required_permissions(d) == {"platform:rag.search"}


def test_validate_sample_app_accepts_plain_dict():
    d = validate_sample_app(_definition())
    assert d.summary.startswith("問い合わせ")


def test_validate_sample_app_rejects_non_sample_app_manifest():
    from jetuse_core.plugins.manifest import SCHEMA_VERSION as SV

    uc = validate_manifest(
        {
            "schemaVersion": SV,
            "id": "acme/uc",
            "version": "1.0.0",
            "kind": "usecase",
            "name": "uc",
            "publisher": "p",
            "jetuse": {"minVersion": "0.1.0"},
            "contributes": {"usecase": {"template": "x"}},
        }
    )
    with pytest.raises(SampleAppError):
        validate_sample_app(uc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(screens=[]),  # screens 必須(>=1)
        lambda d: d["screens"][0].update(dataset="missing"),  # 存在しない dataset 参照
        lambda d: d["screens"][0].update(slots=["nope"]),  # 存在しない slot 参照
        lambda d: d["screens"].append(d["screens"][0]),  # screen キー重複
        lambda d: d["datasets"][0]["fields"].append(
            {"name": "subject", "type": "string"}
        ),  # フィールド名重複
        lambda d: d["datasets"][0]["seed"][0].update(unknown=1),  # seed に未知フィールド
        lambda d: d["datasets"][0]["seed"].insert(0, {"body": "x"}),  # 必須 subject 欠落
        lambda d: d["aiSlots"][0].update(capability="not-a-capability"),  # 未知能力
        lambda d: d["aiSlots"][0].update(permissions=["platform:secrets.read"]),  # 不正スコープ
        lambda d: d["aiSlots"][0].update(
            permissions=["platform:rag.search", "platform:rag.search"]
        ),  # 重複スコープ
        lambda d: d["screens"][0].update(type="kanban"),  # 未知の画面種別
        lambda d: d["datasets"][0]["fields"][0].update(type="json"),  # 未知のフィールド型
        lambda d: d.update(unknown_top="x"),  # 未知トップレベル(extra=forbid)
        lambda d: d["datasets"][0].update(fields=[]),  # fields は >=1
    ],
)
def test_invalid_definition_rejected(mutate):
    d = _definition()
    mutate(d)
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


@pytest.mark.parametrize(
    "ftype, bad_value",
    [
        ("number", "ten"),  # 数値に文字列
        ("number", True),  # bool は number ではない
        ("boolean", "yes"),  # 真偽に文字列
        ("date", "2026/06/25"),  # 不正な日付形式
        ("date", "not-a-date"),
        ("datetime", "25:00"),  # 不正な日時
    ],
)
def test_seed_value_type_mismatch_rejected(ftype, bad_value):
    d = _definition()
    d["datasets"][0]["fields"] = [
        {"name": "subject", "type": "string"},
        {"name": "v", "type": ftype},
    ]
    d["datasets"][0]["seed"] = [{"subject": "x", "v": bad_value}]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


@pytest.mark.parametrize(
    "ftype, good_value",
    [
        ("number", 42),
        ("number", 3.14),
        ("boolean", True),
        ("date", "2026-06-25"),
        ("datetime", "2026-06-25T13:45:00"),
        ("string", "ok"),
        ("number", None),  # null は許容
    ],
)
def test_seed_value_type_match_accepted(ftype, good_value):
    d = _definition()
    d["datasets"][0]["fields"] = [
        {"name": "subject", "type": "string"},
        {"name": "v", "type": ftype},
    ]
    d["datasets"][0]["seed"] = [{"subject": "x", "v": good_value}]
    validate_sample_app(d)


def test_count_caps_enforced():
    from jetuse_core.plugins.sample_app import (
        MAX_AI_SLOTS,
        MAX_FIELDS_PER_DATASET,
        MAX_SCREENS,
        MAX_TOTAL_SEED_ROWS,
    )

    # screens 上限超過。
    d = _definition()
    d["screens"] = [
        {"key": f"s{i}", "title": "t", "type": "list"} for i in range(MAX_SCREENS + 1)
    ]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)

    # fields/dataset 上限超過。
    d = _definition()
    d["datasets"][0]["fields"] = [
        {"name": f"f{i}", "type": "string"} for i in range(MAX_FIELDS_PER_DATASET + 1)
    ]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)

    # aiSlots 上限超過。
    d = _definition()
    d["aiSlots"] = [
        {"key": f"a{i}", "title": "t", "capability": "classify"}
        for i in range(MAX_AI_SLOTS + 1)
    ]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)

    # seed 総数上限超過(2 dataset に分散しても合算で弾く)。
    d = _definition()
    half = MAX_TOTAL_SEED_ROWS // 2 + 1
    d["datasets"] = [
        {
            "name": "a",
            "fields": [{"name": "x", "type": "number"}],
            "seed": [{"x": i} for i in range(half)],
        },
        {
            "name": "b",
            "fields": [{"name": "x", "type": "number"}],
            "seed": [{"x": i} for i in range(half)],
        },
    ]
    d["screens"] = [{"key": "s", "title": "t", "type": "list", "dataset": "a"}]
    d["aiSlots"] = []
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


def test_screen_slots_duplicate_rejected():
    d = _definition()
    d["screens"][0]["slots"] = ["auto-classify", "auto-classify"]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


def test_screen_slots_cap_enforced():
    from jetuse_core.plugins.sample_app import MAX_SLOTS_PER_SCREEN

    d = _definition()
    # 上限超過(存在する slot を超えるが、まず件数上限で弾く)。
    d["screens"][0]["slots"] = [f"s{i}" for i in range(MAX_SLOTS_PER_SCREEN + 1)]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_seed_number_rejects_non_finite(bad):
    """plain dict 経由でも number seed の NaN/Infinity を弾く(検証済み定義は JSON 安全)。"""
    d = _definition()
    d["datasets"][0]["fields"] = [
        {"name": "subject", "type": "string"},
        {"name": "v", "type": "number"},
    ]
    d["datasets"][0]["seed"] = [{"subject": "x", "v": bad}]
    with pytest.raises(SampleAppError):
        validate_sample_app(d)


def test_screen_without_dataset_is_allowed():
    d = _definition()
    d["screens"].append({"key": "home", "title": "ホーム", "type": "dashboard"})
    parsed = validate_sample_app(d)
    assert parsed.screens[-1].dataset is None


# --- 合成バリデーション土台 ---------------------------------------------------


def test_composition_ok_when_all_capabilities_available():
    m = validate_manifest(_manifest())
    report = validate_composition(m)  # 既定: 全コア能力あり
    assert report.ok is True
    assert set(report.required_capabilities) == {"rag.search", "classify"}
    assert report.missing_capabilities == []
    assert report.undeclared_permissions == []


def test_composition_detects_missing_capability():
    """E2E シナリオの核: ホストが必要能力を欠くと不足を検出し ok=False。"""
    m = validate_manifest(_manifest())
    report = validate_composition(m, available_capabilities={"rag.search"})  # classify 欠如
    assert report.ok is False
    assert report.missing_capabilities == ["classify"]


def test_composition_detects_undeclared_permission():
    """aiSlot が要求するスコープが manifest.permissions に無ければ宣言整合違反。"""
    # manifest.permissions から rag.search を外す → faq-answer slot のスコープが未宣言。
    m = validate_manifest(_manifest(permissions=[]))
    report = validate_composition(m)
    assert report.ok is False
    assert report.undeclared_permissions == ["platform:rag.search"]


def test_composition_reports_unused_permissions():
    m = validate_manifest(
        _manifest(permissions=["platform:rag.search", "platform:db.query"])
    )
    report = validate_composition(m)
    assert report.ok is True  # 未使用は致命ではない
    assert report.unused_permissions == ["platform:db.query"]


def test_composition_rejects_non_sample_app_even_with_definition():
    """definition を渡しても kind 不一致なら SampleAppError(取り違え防止)。"""
    uc = validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": "acme/uc",
            "version": "1.0.0",
            "kind": "usecase",
            "name": "uc",
            "publisher": "p",
            "jetuse": {"minVersion": "0.1.0"},
            "contributes": {"usecase": {"template": "x"}},
        }
    )
    sample_def = validate_sample_app(_definition())
    with pytest.raises(SampleAppError):
        validate_composition(uc, definition=sample_def)


def test_composition_error_carries_report():
    m = validate_manifest(_manifest())
    report = validate_composition(m, available_capabilities=set())
    err = CompositionError(report)
    assert err.report is report
    assert "missing_capabilities" in str(err)


# --- JSON Schema --------------------------------------------------------------


def test_sample_app_json_schema_uses_camelcase_and_enums():
    schema = sample_app_json_schema()
    # aiSlots は camelCase 別名で出る。
    assert "aiSlots" in schema["properties"]
    assert "screens" in schema["required"]
    # capability enum がコア能力語彙を反映する。
    cap_enum = set(schema["$defs"]["AiSlot"]["properties"]["capability"]["enum"])
    assert cap_enum == set(SAMPLE_APP_CAPABILITIES)


def test_definition_is_json_safe_for_manifest():
    """sample-app 定義を含む manifest が正準 JSON 化できる(署名往復の前提)。"""
    from jetuse_core.plugins.manifest import canonical_signing_payload

    m = validate_manifest(_manifest())
    payload = canonical_signing_payload(m)
    assert isinstance(payload, bytes) and payload
    # 改ざんに依存しない健全性: 同じ manifest からは同じバイト列。
    assert payload == canonical_signing_payload(validate_manifest(copy.deepcopy(_manifest())))


# --- validate_manifest() の公開入口で sample-app 詳細を強制する(MKT-01 / Codex F-001) ---


def test_validate_manifest_enforces_sample_app_detail_empty_screens():
    from jetuse_core.plugins.manifest import ManifestError

    bad = _definition()
    bad["screens"] = []  # screens は min_length=1。
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(definition=bad))


def test_validate_manifest_enforces_sample_app_detail_bad_seed_type():
    from jetuse_core.plugins.manifest import ManifestError

    bad = _definition()
    # number 型フィールドに文字列 seed → SampleAppDefinition 検証で弾かれる。
    bad["datasets"][0]["fields"].append({"name": "score", "type": "number"})
    bad["datasets"][0]["seed"][0]["score"] = "not-a-number"
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(definition=bad))


def test_validate_manifest_sample_app_detail_enforced_without_importing_sample_app():
    # import 順非依存の回帰: sample_app を import せず validate_manifest だけの新規プロセスでも、
    # 構造不正(screens 空)の sample-app manifest が ManifestError になる(遅延 import dispatch)。
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        from jetuse_core.plugins.manifest import validate_manifest, ManifestError
        bad = {
            "schemaVersion": "1", "id": "jetuse/s", "version": "1.0.0",
            "kind": "sample-app", "name": "s", "publisher": "jetuse",
            "jetuse": {"minVersion": "0.3.0"},
            "contributes": {"sample-app": {"screens": []}},
        }
        try:
            validate_manifest(bad)
            print("NO_RAISE")
        except ManifestError:
            print("RAISED")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.stdout.strip() == "RAISED", out.stderr
