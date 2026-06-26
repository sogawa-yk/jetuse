"""合成バリデーション(HBD-04)の単体テスト。

ガバナンス4制約(許可組合せ / 必要ケイパ束縛 / 権限スコープ / モデル可用性)について、
正常構成が PASS し、各違反種別が個別に FAIL して**代替提案**を返すことを検証する。
副作用なし(DB/GenAI 非依存)。
"""


from jetuse_core.governance import (
    CORE_CONNECTORS,
    GovernanceReport,
    available_model_features,
    validate_governance,
)
from jetuse_core.models import ModelDef
from jetuse_core.plugins.ai_runtime import bound_capabilities
from jetuse_core.recommend import recommend
from jetuse_core.synth import synthesize

# vision を持たない(text のみ)モデルレジストリ。VLM 不可なリージョンを模す。
_TEXT_ONLY_MODELS = {
    "gpt-oss-120b": ModelDef("openai.gpt-oss-120b", "responses", "GPT-OSS 120B"),
}


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


def _comp(**over):
    return synthesize(recommend(_answers(**over)))


# --- シナリオ1(正常): 妥当な構成は PASS ------------------------------------


def test_valid_composition_passes_all_gates():
    report = validate_governance(_comp())
    assert isinstance(report, GovernanceReport)
    assert report.ok is True
    assert report.violations == []
    assert report.sample_app == "SBA-A"
    assert all(report.checks.values())


def test_valid_nl2sql_composition_passes():
    # SBA-B + nl2sql/chart + slack。全部品が組込点を持ち束縛済み・スコープ宣言済み。
    report = validate_governance(_comp(Q1="inventory", Q2=["business_db"], Q3="nl2sql"))
    assert report.ok is True
    assert report.violations == []


# --- (a) 許可組合せ: 組込点なしの部品 ---------------------------------------


def test_disallowed_combination_capability_without_slot():
    # SBA-A + 業務DB → nl2sql が推薦されるが SBA-A に nl2sql 組込点は無い(許可外組合せ)。
    report = validate_governance(_comp(Q1="support", Q2=["business_db", "docs"], Q3="rag_qa"))
    assert report.ok is False
    combos = [v for v in report.violations if v.kind == "disallowed_combination"]
    nl = next(v for v in combos if v.element == "nl2sql")
    assert nl.element_type == "capability"
    # 代替提案: nl2sql を扱える SBA(SBA-B/SBA-C)を主アプリにする案を含む。
    assert "SBA-B" in nl.alternative or "SBA-C" in nl.alternative
    assert report.checks["allowed_combination"] is False
    # RAG は active なので、その分の組合せ違反は出ない。
    assert all(v.element != "rag.search" for v in combos)


def test_disallowed_combination_capability_without_any_core_sba():
    # SBA-A + 画像 → vlm.ocr が推薦されるが、コア同梱に vlm.ocr 組込点を持つ SBA は無い。
    report = validate_governance(_comp(Q1="support", Q2=["image"], Q3="ocr_extract"))
    assert report.ok is False
    vlm = next(
        v for v in report.violations
        if v.kind == "disallowed_combination" and v.element == "vlm.ocr"
    )
    assert "外す" in vlm.alternative  # 外させない代替(別 SBA が無い場合の道)を提示


# --- (a) 許可組合せ: コネクタパレット ---------------------------------------


def test_disallowed_connector_outside_palette():
    comp = _comp()
    comp.connectors = ["teams"]  # コアパレット(slack)外
    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.element == "teams")
    assert v.kind == "disallowed_combination"
    assert v.element_type == "connector"
    assert "Slack" in v.alternative
    assert report.checks["allowed_combination"] is False


def test_allowed_connectors_can_be_overridden():
    comp = _comp()
    comp.connectors = ["teams"]
    report = validate_governance(comp, allowed_connectors=CORE_CONNECTORS | {"teams"})
    assert all(v.element != "teams" for v in report.violations)


# --- (b) 必要ケイパビリティが束縛済み ---------------------------------------


def test_unbound_capability_blocks_gate(monkeypatch):
    import jetuse_core.synth as synth_mod

    # summarize を ai_runtime 束縛から外して「組込点あり×未束縛」を作る。
    reduced = bound_capabilities() - {"summarize"}
    monkeypatch.setattr(synth_mod, "bound_capabilities", lambda: reduced)
    comp = _comp()  # SBA-A: summarize 組込点あり

    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "unbound_capability")
    assert v.element == "summarize"
    assert v.element_type == "capability"
    assert "束縛" in v.alternative
    assert report.checks["capabilities_bound"] is False


# --- (c) 権限スコープが manifest 内 ----------------------------------------


def test_scope_out_of_manifest_from_composition_report():
    comp = _comp()
    assert comp.composition_report is not None
    # SBA-01 の composition_report に逸脱スコープを注入(再利用経路の検証)。
    comp.composition_report.undeclared_permissions = ["platform:db.query"]
    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "scope_out_of_manifest")
    assert v.element == "platform:db.query"
    assert v.element_type == "permission"
    assert "platform:db.query" in v.alternative
    assert report.checks["permission_scope"] is False


def test_missing_host_capability_from_composition_report():
    comp = _comp()
    assert comp.composition_report is not None
    comp.composition_report.missing_capabilities = ["rag.search"]
    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "missing_host_capability")
    assert v.element == "rag.search"
    assert report.checks["capabilities_bound"] is False


# --- (d) モデル可用性(ap-osaka-1) ---------------------------------------


def test_model_unavailable_when_vision_missing():
    # SBA-A + ocr_extract → vlm.ocr(vision 要求)。vision 無しレジストリでは可用性 NG。
    comp = _comp(Q1="support", Q2=["image"], Q3="ocr_extract")
    report = validate_governance(comp, available_models=_TEXT_ONLY_MODELS)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "model_unavailable")
    assert v.element == "vlm.ocr"
    assert "vision" in v.detail
    assert v.alternative
    assert report.checks["model_available"] is False


def test_text_capabilities_available_with_text_only_models():
    # 既定の text 部品(rag/summarize/classify)は text のみのレジストリでも可用。
    comp = _comp()
    report = validate_governance(comp, available_models=_TEXT_ONLY_MODELS)
    assert all(v.kind != "model_unavailable" for v in report.violations)


def test_available_model_features_reports_vision_from_default_registry():
    feats = available_model_features()
    assert "text" in feats
    assert "vision" in feats  # ap-osaka-1 既定レジストリには vision モデルがある
    assert "vision" not in available_model_features(_TEXT_ONLY_MODELS)


# --- 境界: 合成不能(主SBA 未解決) ------------------------------------------


def test_unresolved_composition_is_rejected():
    comp = synthesize(recommend(_answers(Q1="other")))  # 主SBA 未確定 → ok=False
    assert comp.ok is False
    report = validate_governance(comp)
    assert report.ok is False
    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.kind == "unresolved_composition"
    assert v.alternative
    assert all(val is False for val in report.checks.values())


# --- 機械可読性: 全違反が代替提案を持つ ------------------------------------


def test_all_violations_carry_alternatives():
    # 複数違反が同時に出る構成(no_slot + vision 不可)で、全件に代替提案がある。
    comp = _comp(Q1="support", Q2=["image"], Q3="ocr_extract")
    report = validate_governance(comp, available_models=_TEXT_ONLY_MODELS)
    assert report.violations
    assert all(v.alternative.strip() for v in report.violations)
    assert all(v.detail.strip() for v in report.violations)


def test_validate_governance_is_deterministic():
    comp = _comp()
    a = validate_governance(comp)
    b = validate_governance(comp)
    assert a.model_dump() == b.model_dump()
