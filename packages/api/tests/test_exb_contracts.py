"""EXB-01: Experience Builder MVP 契約スキーマ(JSON Schema)の検証テスト。

スキーマ実体は packages/api/jetuse_platform/contracts/schemas/(パッケージ同梱)。
バリデータは jetuse_platform.contracts。正例は通り、必須欠落/不正 enum/未知 type は
弾かれること。import 時に FS へ触れず成功することも担保する。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from jetuse_platform.contracts import (
    RUN_EVENT_TYPES,
    get_validator,
    is_valid,
    load_schema,
    run_event_types,
    validate_action_with_citations_config,
    validate_action_with_citations_event,
    validate_action_with_citations_input,
    validate_action_with_citations_output,
    validate_demo_bundle,
    validate_demo_evidence_pack,
    validate_experience,
    validate_run_event,
)
from jetuse_platform.contracts.validators import ValidationError

# packages/api(subprocess で jetuse_platform を import 可能にするための anchor)
_API_DIR = Path(__file__).resolve().parents[1]

# 同梱が必須なスキーマファイル(present / wheel 同梱の両テストで共有)。
_REQUIRED_SCHEMAS = (
    "experience.schema.json",
    "demo-bundle.schema.json",
    "demo-evidence-pack.schema.json",
    "answer-with-citations.config.schema.json",
    "answer-with-citations.input.schema.json",
    "answer-with-citations.output.schema.json",
    "answer-with-citations.event.schema.json",
    "run-event.schema.json",
)


# ---------------------------------------------------------------- Experience
def _experience() -> dict:
    return {
        "apiVersion": "jetuse.oracle.com/v1alpha1",
        "kind": "Experience",
        "metadata": {"name": "medical-device-support", "title": "医療機器お問い合わせ管理"},
        "ui": {
            "package": "medical-device-support-ui",
            "designSystem": "jetuse-redwood",
            "entryRoute": "inbox",
        },
        "channels": {
            "web": {"enabled": True},
            "slack": {
                "adapter": "slack-reference@1",
                "messagePattern": "answer-with-citations",
                "detailRoute": "/inquiries/{inquiryId}",
            },
        },
        "resources": {
            "fixtures": {"inquiries": "medical-inquiries-v1"},
            "knowledge": {"manuals": "medical-device-manuals-v3"},
        },
        "actions": {
            "answer-customer": {
                "target": {"kind": "workflow", "ref": "support-answer-workflow@1"},
                "bindings": {"knowledge": "manuals"},
            }
        },
    }


def test_experience_ok():
    validate_experience(_experience())


def test_experience_web_only_ok():
    exp = _experience()
    del exp["channels"]["slack"]
    validate_experience(exp)


def test_experience_missing_actions_rejected():
    exp = _experience()
    del exp["actions"]
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_empty_actions_rejected():
    exp = _experience()
    exp["actions"] = {}
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_bad_target_kind_rejected():
    exp = _experience()
    exp["actions"]["answer-customer"]["target"]["kind"] = "nonsense"
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_wrong_kind_rejected():
    exp = _experience()
    exp["kind"] = "Workflow"
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_missing_metadata_title_rejected():
    exp = _experience()
    del exp["metadata"]["title"]
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_bad_api_version_rejected():
    exp = _experience()
    exp["apiVersion"] = "jetuse.oracle.com/v2"
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_empty_knowledge_rejected():
    # RAG Action が解決できる KnowledgeSpace が無い Experience を弾く(空 dict)。
    exp = _experience()
    exp["resources"]["knowledge"] = {}
    with pytest.raises(ValidationError):
        validate_experience(exp)


def test_experience_blank_knowledge_ref_rejected():
    # 空文字の KnowledgeSpace 参照を弾く。
    exp = _experience()
    exp["resources"]["knowledge"] = {"manuals": ""}
    with pytest.raises(ValidationError):
        validate_experience(exp)


# ----------------------------------------------------------------- import 健全性
def test_import_contracts_is_lazy_no_schema_load():
    # import 単体が成功し、かつ初回アクセス前はスキーマ未読込(遅延)であることを実証する。
    # validators モジュールに RUN_EVENT_TYPES が実体化しておらず、load_schema の
    # 内部キャッシュも空であること(= import 時に FS へ触れていない)を subprocess で確認。
    code = (
        "import jetuse_platform.contracts as c\n"
        "import jetuse_platform.contracts.validators as v\n"
        "import jetuse_platform.contracts.loader as ldr\n"
        # __getattr__ 由来の動的属性は __dict__ に入らない(=未実体化)。
        "assert 'RUN_EVENT_TYPES' not in v.__dict__, 'eagerly materialized'\n"
        "assert ldr._load_schema_cached.cache_info().currsize == 0, 'schema read at import'\n"
        # 初回アクセスで実体化され、キャッシュが埋まること。
        "assert len(c.RUN_EVENT_TYPES) == 11\n"
        "assert ldr._load_schema_cached.cache_info().currsize >= 1\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_API_DIR
    )
    assert r.returncode == 0, r.stderr


def test_package_data_schemas_present():
    # package-data の glob(`schemas/*.json`)が実ファイルに一致し、同梱対象が揃うことを担保。
    from importlib.resources import files

    schema_dir = files("jetuse_platform.contracts").joinpath("schemas")
    present = {p.name for p in schema_dir.iterdir() if p.name.endswith(".json")}
    required = set(_REQUIRED_SCHEMAS)
    assert required <= present, f"missing schemas: {required - present}"
    # 各スキーマが JSON として読めること(壊れた同梱物の検知)。
    import json

    for name in required:
        json.loads(schema_dir.joinpath(name).read_text(encoding="utf-8"))


def test_wheel_bundles_schemas(tmp_path):
    # 中心的主張: package-data 設定や Containerfile COPY が壊れたら検知する。
    # 実際に wheel をビルドし、schemas/*.json が成果物(zip)に同梱されることを証明する。
    # 中心的主張なので、ビルド失敗は原則 fail（pyproject/構成の不具合を skip でマスクしない）。
    # skip するのは pip 自体が起動できない真の環境不全(FileNotFoundError)のみ＝理由明示。
    import zipfile

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=_API_DIR,
        )
    except FileNotFoundError as e:  # pip/python 実行不能＝環境不全のみ skip
        pytest.skip(f"ビルドツール不在(環境不全): {e}")
    # `--no-deps` で依存取得もないため、失敗したら構成側の不具合とみなして fail（masking 防止）。
    assert r.returncode == 0, f"wheel ビルド失敗(構成不具合の可能性): {r.stderr[-2000:]}"

    wheels = list(tmp_path.glob("jetuse*api*.whl"))
    assert wheels, f"wheel が生成されない: {[p.name for p in tmp_path.iterdir()]}"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
    expected = {f"jetuse_platform/contracts/schemas/{n}" for n in _REQUIRED_SCHEMAS}
    missing = expected - names
    assert not missing, f"wheel に未同梱のスキーマ: {missing}"


def test_pyproject_declares_schema_package_data():
    # package-data 宣言の回帰検知: [tool.setuptools.package-data] から設定を消すと
    # source-tree present テストは通ってしまうため、宣言自体を tomllib で検証する。
    import tomllib

    pyproject = _API_DIR / "pyproject.toml"
    with pyproject.open("rb") as fh:
        cfg = tomllib.load(fh)
    package_data = cfg["tool"]["setuptools"]["package-data"]
    globs = package_data["jetuse_platform.contracts"]
    assert "schemas/*.json" in globs, f"package-data glob 未宣言: {globs}"


# ------------------------------------------------------ load_schema キャッシュ汚染
def test_load_schema_returns_isolated_copies():
    # 戻り値を破壊しても、内部キャッシュ・次の load_schema・RUN_EVENT_TYPES は不変。
    first = load_schema("run-event")
    first["properties"]["type"]["enum"].append("hacked.event")
    first["properties"]["seq"] = "corrupted"

    second = load_schema("run-event")
    assert "hacked.event" not in second["properties"]["type"]["enum"]
    assert second["properties"]["seq"] == {"type": "integer", "minimum": 0}
    assert "hacked.event" not in run_event_types()
    assert "hacked.event" not in RUN_EVENT_TYPES
    # 破壊後も検証器は健全(未知 type を弾き続ける)。
    with pytest.raises(ValidationError):
        validate_run_event(
            {"run_id": "r", "type": "hacked.event", "seq": 0, "ts": "2026-06-30T00:00:00Z"}
        )


def test_get_validator_is_not_shared_mutable():
    # 返ってきた検証器の schema を破壊しても、次の検証器/validate_*/is_valid は不変。
    v = get_validator("run-event")
    v.schema["properties"]["type"]["enum"].append("hacked.event")

    assert "hacked.event" not in get_validator("run-event").schema["properties"]["type"]["enum"]
    bad = {"run_id": "r", "type": "hacked.event", "seq": 0, "ts": "2026-06-30T00:00:00Z"}
    with pytest.raises(ValidationError):
        validate_run_event(bad)
    assert is_valid("run-event", bad) is False


# ------------------------------------------- answer.with-citations@1 (config)
def test_config_ok():
    validate_action_with_citations_config(
        {"knowledge": {"space": "medical-device-manuals", "version": "v3"},
         "retrieval": {"topK": 5}}
    )


def test_config_missing_knowledge_rejected():
    with pytest.raises(ValidationError):
        validate_action_with_citations_config({"retrieval": {"topK": 5}})


def test_config_topk_upper_bound():
    # EXB-04/ADR-0024(施主承認): retrieval.topK は上限 100。境界は許可、超過は拒否。
    validate_action_with_citations_config(
        {"knowledge": {"space": "s"}, "retrieval": {"topK": 100}}
    )
    with pytest.raises(ValidationError):
        validate_action_with_citations_config(
            {"knowledge": {"space": "s"}, "retrieval": {"topK": 101}}
        )


# -------------------------------------------- answer.with-citations@1 (input)
def test_input_ok():
    validate_action_with_citations_input({"question": "保証期間を教えてください"})


def test_input_empty_question_rejected():
    with pytest.raises(ValidationError):
        validate_action_with_citations_input({"question": ""})


def test_input_unknown_field_rejected():
    # config(knowledge 等)を input に混ぜない: 分離の担保。
    with pytest.raises(ValidationError):
        validate_action_with_citations_input(
            {"question": "x", "knowledge": {"space": "s"}}
        )


# ------------------------------------------- answer.with-citations@1 (output)
def test_output_ok():
    validate_action_with_citations_output(
        {
            "answer": "保証期間は1年です。",
            "citations": [{"source": "manual.pdf#p3", "score": 0.82}],
        }
    )


def test_output_citation_without_source_rejected():
    with pytest.raises(ValidationError):
        validate_action_with_citations_output(
            {"answer": "a", "citations": [{"score": 0.5}]}
        )


# -------------------------------------------- answer.with-citations@1 (event)
def test_event_message_delta_ok():
    validate_action_with_citations_event({"type": "message.delta", "data": {"text": "保"}})


def test_event_retrieval_completed_ok():
    validate_action_with_citations_event(
        {"type": "retrieval.completed", "data": {"citations": [{"source": "m.pdf"}]}}
    )


def test_event_message_delta_missing_text_rejected():
    with pytest.raises(ValidationError):
        validate_action_with_citations_event({"type": "message.delta", "data": {}})


def test_event_unknown_type_rejected():
    with pytest.raises(ValidationError):
        validate_action_with_citations_event({"type": "tool.started", "data": {}})


# --------------------------------------------------------- Run event vocabulary
def _run_event(type_: str) -> dict:
    return {"run_id": "run-1", "type": type_, "seq": 0, "ts": "2026-06-30T00:00:00Z"}


def test_run_event_vocabulary_is_complete():
    assert set(RUN_EVENT_TYPES) == {
        "run.started",
        "message.delta",
        "retrieval.started",
        "retrieval.completed",
        "tool.started",
        "tool.completed",
        "approval.required",
        "artifact.created",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }


@pytest.mark.parametrize("type_", RUN_EVENT_TYPES)
def test_run_event_each_type_ok(type_):
    validate_run_event(_run_event(type_))


def test_run_event_with_data_ok():
    ev = _run_event("retrieval.completed")
    ev["data"] = {"citations": [{"source": "m.pdf", "score": 0.9}]}
    validate_run_event(ev)


def test_run_event_unknown_type_rejected():
    with pytest.raises(ValidationError):
        validate_run_event(_run_event("custom.event"))


def test_run_event_missing_seq_rejected():
    ev = _run_event("run.started")
    del ev["seq"]
    with pytest.raises(ValidationError):
        validate_run_event(ev)


def test_run_event_seq_not_integer_rejected():
    ev = _run_event("run.started")
    ev["seq"] = "0"
    with pytest.raises(ValidationError):
        validate_run_event(ev)


def test_run_event_bad_ts_rejected():
    ev = _run_event("run.started")
    ev["ts"] = "not-a-timestamp"
    with pytest.raises(ValidationError):
        validate_run_event(ev)


@pytest.mark.parametrize(
    "ts",
    [
        "2026-06-30T00:00:00Z",
        "2026-06-30T00:00:00+09:00",
        "2026-06-30T00:00:00.123Z",
        "2026-06-30t00:00:00z",
    ],
)
def test_run_event_rfc3339_ts_accepted(ts):
    ev = _run_event("run.started")
    ev["ts"] = ts
    validate_run_event(ev)


@pytest.mark.parametrize(
    "ts",
    [
        "20260630T000000Z",        # 基本形式(区切りなし)
        "2026-W27-1T00:00:00Z",    # 週日付
        "2026-06-30T00:00:00,5Z",  # カンマ小数秒
        "2026-06-30T00:00:00",     # タイムゾーンなし
        "2026-06-30",              # date-only
        "2026-13-40T00:00:00Z",    # 構造は RFC3339 だが実在しない日付
        "2026-06-30T00:00:00+09:60",  # TZ オフセット分が 60(不正域)
        "2026-06-30T00:00:00+24:00",  # TZ オフセット時が 24(不正域)
        "2026-06-30T25:00:00Z",       # 時 25(不正域)
        "2026-06-30T00:60:00Z",       # 分 60(不正域)
    ],
)
def test_run_event_non_rfc3339_ts_rejected(ts):
    # RFC3339: 'T' 区切り＋タイムゾーン必須。順序付け/他言語クライアント互換のため厳格に弾く。
    ev = _run_event("run.started")
    ev["ts"] = ts
    with pytest.raises(ValidationError):
        validate_run_event(ev)


# ------------------------------------------------------------------ DemoBundle
def _demo_bundle() -> dict:
    return {
        "kind": "DemoBundle",
        "name": "medical-device-support-demo",
        "experience": {"ref": "medical-device-support", "version": "v1"},
        "dataVersions": {
            "fixtures": {"inquiries": "medical-inquiries-v1"},
            "knowledge": {"manuals": "medical-device-manuals-v3"},
        },
        "actionBindings": [
            {"actionId": "answer-customer", "target": "support-answer-workflow@1"}
        ],
        "qualityGate": {"passed": True},
    }


def test_demo_bundle_ok():
    validate_demo_bundle(_demo_bundle())


def test_demo_bundle_missing_quality_gate_rejected():
    b = _demo_bundle()
    del b["qualityGate"]
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_quality_gate_not_passed_rejected():
    # Gate 通過版だけが DemoBundle (§3.8): passed は const true。
    b = _demo_bundle()
    b["qualityGate"]["passed"] = False
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_missing_experience_version_rejected():
    b = _demo_bundle()
    del b["experience"]["version"]
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_missing_action_bindings_rejected():
    b = _demo_bundle()
    del b["actionBindings"]
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_empty_action_bindings_rejected():
    b = _demo_bundle()
    b["actionBindings"] = []
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_wrong_kind_rejected():
    b = _demo_bundle()
    b["kind"] = "Experience"
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_missing_knowledge_version_rejected():
    b = _demo_bundle()
    del b["dataVersions"]["knowledge"]
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_empty_knowledge_versions_rejected():
    b = _demo_bundle()
    b["dataVersions"]["knowledge"] = {}
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


def test_demo_bundle_blank_version_string_rejected():
    b = _demo_bundle()
    b["dataVersions"]["knowledge"]["manuals"] = ""
    with pytest.raises(ValidationError):
        validate_demo_bundle(b)


# ------------------------------------------------------------ DemoEvidencePack
def _demo_evidence_pack() -> dict:
    return {
        "kind": "DemoEvidencePack",
        "bundle": {
            "ref": "medical-device-support-demo",
            "referenceImplementation": "rag.answer",
            "version": "1.0.0",
        },
        "workingConfiguration": {
            "rag": {"connection": "real"},
            "slack": {"connection": "simulation"},
        },
        "customerConfirmations": ["回答精度が業務に十分"],
        "limitations": ["本番性能は未検証"],
        "unverified": ["顧客固有システム連携"],
        "handoff": {"nextVerificationItems": ["本番Knowledge投入", "SSO連携"]},
    }


def test_demo_evidence_pack_ok():
    validate_demo_evidence_pack(_demo_evidence_pack())


def test_demo_evidence_pack_missing_handoff_rejected():
    p = _demo_evidence_pack()
    del p["handoff"]
    with pytest.raises(ValidationError):
        validate_demo_evidence_pack(p)


def test_demo_evidence_pack_missing_reference_impl_rejected():
    p = _demo_evidence_pack()
    del p["bundle"]["referenceImplementation"]
    with pytest.raises(ValidationError):
        validate_demo_evidence_pack(p)


def test_demo_evidence_pack_missing_customer_confirmations_rejected():
    p = _demo_evidence_pack()
    del p["customerConfirmations"]
    with pytest.raises(ValidationError):
        validate_demo_evidence_pack(p)


def test_demo_evidence_pack_working_config_without_connection_rejected():
    # 実接続/シミュレーションの区別を必須にする (§16.5)。
    p = _demo_evidence_pack()
    p["workingConfiguration"]["rag"] = {"endpoint": "x"}
    with pytest.raises(ValidationError):
        validate_demo_evidence_pack(p)


def test_demo_evidence_pack_working_config_bad_connection_rejected():
    p = _demo_evidence_pack()
    p["workingConfiguration"]["rag"]["connection"] = "maybe"
    with pytest.raises(ValidationError):
        validate_demo_evidence_pack(p)
