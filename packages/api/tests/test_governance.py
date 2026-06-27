"""合成バリデーション(HBD-04)の単体テスト。

ガバナンス4制約(許可組合せ / 必要ケイパ束縛 / 権限スコープ / モデル可用性)について、
正常構成が PASS し、各違反種別が個別に FAIL して**代替提案**を返すことを検証する。
副作用なし(DB/GenAI 非依存)。
"""

import pytest

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


def _comp(_inject=(), **over):
    # 自動フィット後は recommend が「アプリに合わない部品」を ai_parts に残さないため、ガバナンスの
    # no_slot/model 判定を検証するテストでは、対象部品を推薦に**直接注入**して no_slot 構成を作る。
    rec = recommend(_answers(**over))
    if _inject:
        rec = rec.model_copy(update={"ai_parts": [*rec.ai_parts, *_inject]})
    return synthesize(rec)


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
    report = validate_governance(_comp(_inject=["nl2sql"], Q1="support"))
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
    report = validate_governance(_comp(_inject=["vlm.ocr"], Q1="support"))
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
    # 許可パレットを teams へ広げ、かつ teams を **active 束縛**として与えた現実的な構成は通る。
    from jetuse_core.synth import ConnectorBinding

    comp = _comp()
    comp.connectors = ["teams"]
    comp.connector_bindings = [
        ConnectorBinding(
            provider="teams", status="active", transport="mcp", actions=["post_message"],
            required_scopes=["platform:connector.invoke"], requires_secret=True,
            secret_ref="teams-token", reason=None,
        )
    ]
    report = validate_governance(comp, allowed_connectors=CORE_CONNECTORS | {"teams"})
    assert all(v.element != "teams" for v in report.violations)


def test_allowed_connector_without_active_binding_is_rejected():
    # CON03-MAJ-001: 許可しただけで active 束縛が無い connector は invoke 経路に載らない → 弾く。
    comp = _comp()
    comp.connectors = ["teams"]
    comp.connector_bindings = []  # teams の束縛が存在しない
    report = validate_governance(comp, allowed_connectors=CORE_CONNECTORS | {"teams"})
    assert report.ok is False
    v = next(
        v for v in report.violations
        if v.kind == "connector_scope_undeclared" and v.element == "teams"
    )
    assert v.element_type == "connector"
    assert report.checks["connector_scope"] is False


def test_allowed_connector_with_excluded_binding_is_rejected():
    # CON03-MAJ-001: 許可されているが excluded(合成不整合)な connector も弾く。
    from jetuse_core.synth import ConnectorBinding

    comp = _comp()
    comp.connectors = ["teams"]
    comp.connector_bindings = [
        ConnectorBinding(
            provider="teams", status="excluded", transport="mcp", actions=["x"],
            required_scopes=["platform:connector.invoke"], requires_secret=False,
            secret_ref=None, reason="action 要求スコープ未宣言",
        )
    ]
    report = validate_governance(comp, allowed_connectors=CORE_CONNECTORS | {"teams"})
    assert report.ok is False
    assert any(
        v.kind == "connector_scope_undeclared" and v.element == "teams"
        for v in report.violations
    )
    assert report.checks["connector_scope"] is False


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
    comp = _comp(_inject=["vlm.ocr"], Q1="support")
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
    comp = _comp(_inject=["vlm.ocr"], Q1="support")
    report = validate_governance(comp, available_models=_TEXT_ONLY_MODELS)
    assert report.violations
    assert all(v.alternative.strip() for v in report.violations)
    assert all(v.detail.strip() for v in report.violations)


def test_validate_governance_is_deterministic():
    comp = _comp()
    a = validate_governance(comp)
    b = validate_governance(comp)
    assert a.model_dump() == b.model_dump()


# --- (CON-03) コネクタ invoke スコープ経路 ---------------------------------


def test_active_connector_scope_path_passes():
    # 既定構成(slack 連携)は active コネクタの invoke スコープ経路が成立 → connector_scope パス。
    report = validate_governance(_comp())
    assert report.checks["connector_scope"] is True
    assert all(v.element_type != "connector" for v in report.violations)


def test_connector_scope_undeclared_for_inpalette_excluded():
    # コアコネクタ(slack)が合成不整合で excluded になった状況を擬似注入する。
    from jetuse_core.synth import ConnectorBinding

    comp = _comp()
    comp.connector_bindings = [
        ConnectorBinding(
            provider="slack",
            status="excluded",
            transport="builtin",
            actions=["post_message"],
            required_scopes=["platform:connector.invoke"],
            requires_secret=True,
            secret_ref="slack-bot-token",
            reason="action 要求スコープ未宣言",
        )
    ]
    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "connector_scope_undeclared")
    assert v.element == "slack"
    assert v.element_type == "connector"
    assert v.alternative
    assert report.checks["connector_scope"] is False


def test_connector_scope_unknown_scope_is_rejected():
    from jetuse_core.synth import ConnectorBinding

    comp = _comp()
    comp.connector_bindings = [
        ConnectorBinding(
            provider="slack",
            status="active",
            transport="builtin",
            actions=["post_message"],
            # 既知 PLATFORM_SCOPES 外のスコープを要求(語彙逸脱)。
            required_scopes=["platform:connector.invoke", "platform:bogus.scope"],
            requires_secret=True,
            secret_ref="slack-bot-token",
            reason=None,
        )
    ]
    report = validate_governance(comp)
    assert report.ok is False
    v = next(v for v in report.violations if v.kind == "connector_scope_unknown")
    assert "platform:bogus.scope" in v.detail
    assert report.checks["connector_scope"] is False


def test_excluded_outside_palette_connector_is_not_scope_violation():
    # パレット外(teams・既定パレットは slack のみ)の excluded コネクタは connector_scope ではなく
    # connectors リストの disallowed_combination が担当する(二重計上しない)。
    from jetuse_core.synth import ConnectorBinding

    comp = _comp()
    comp.connectors = ["teams"]
    comp.connector_bindings = [
        ConnectorBinding(
            provider="teams", status="excluded", transport=None, actions=[],
            required_scopes=[], requires_secret=False, secret_ref=None,
            reason="コアパレット外",
        )
    ]
    report = validate_governance(comp)  # 既定パレット(slack のみ)
    assert all(v.kind != "connector_scope_undeclared" for v in report.violations)
    assert any(
        v.kind == "disallowed_combination" and v.element == "teams"
        for v in report.violations
    )
    assert report.checks["connector_scope"] is True


def test_synth_governance_invoke_wiring(monkeypatch):
    """合成 → ガバナンス → broker 経由 invoke の結線(mock transport・実 DB 非依存)。"""
    from jetuse_core import platform_broker as pb
    from jetuse_core.plugins import connector_runtime as cr
    from jetuse_core.plugins.core_connectors import resolve_active_connector
    from jetuse_core.settings import Settings

    # 監査(DB 書込)を no-op 化して実 ADB 非依存にする(invoke 経路の検証が目的)。
    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: None)
    settings = Settings(platform_broker_secret="test-secret-please-rotate")

    comp = _comp()
    gov = validate_governance(comp)
    assert gov.ok is True  # デプロイ前ゲート通過

    # active コネクタを解決して broker 経由で invoke(短期 JWT・connector.invoke 強制)。
    defn = resolve_active_connector(comp, "slack")
    assert defn is not None
    tenant = "ocid1.tenancy.oc1..project-test"
    token = pb.issue_broker_token(
        "jetuse/slack-connector", tenant, ["platform:connector.invoke"], settings=settings
    )

    calls = []

    def _mock_http(url, headers, body):
        calls.append({"url": url, "has_auth": "Authorization" in headers})
        return {"ok": True, "channel": body.get("channel"), "ts": "1.2"}

    result = cr.invoke_connector_action(
        defn, "post_message", {"channel": "#demo", "text": "wired"},
        broker_token=token, tenant=tenant, settings=settings,
        secret_resolver=lambda ref: "xoxb-mock-not-real", http_caller=_mock_http,
    )
    assert result.ok is True
    assert len(calls) == 1 and calls[0]["has_auth"] is True
    # 戻り値に実トークンが出ない(redact 契約)。
    assert "xoxb-mock-not-real" not in str(result.output)


def test_invoke_fail_closed_without_invoke_scope(monkeypatch):
    """connector.invoke 未付与トークンは外部到達前に拒否(mock 不呼出)。"""
    from jetuse_core import platform_broker as pb
    from jetuse_core.plugins import connector_runtime as cr
    from jetuse_core.plugins.core_connectors import resolve_active_connector
    from jetuse_core.settings import Settings

    monkeypatch.setattr(pb, "record_broker_access", lambda **kw: None)
    settings = Settings(platform_broker_secret="test-secret-please-rotate")
    comp = _comp()
    defn = resolve_active_connector(comp, "slack")
    tenant = "ocid1.tenancy.oc1..project-test"
    # invoke スコープを含まないトークン。
    token = pb.issue_broker_token(
        "jetuse/slack-connector", tenant, ["platform:rag.search"], settings=settings
    )
    calls = []
    with pytest.raises(cr.ConnectorInvokeDenied) as ei:
        cr.invoke_connector_action(
            defn, "post_message", {"channel": "#x", "text": "y"},
            broker_token=token, tenant=tenant, settings=settings,
            secret_resolver=lambda ref: "xoxb-mock", http_caller=lambda *a: calls.append(a),
        )
    assert ei.value.reason == "scope_denied"
    assert calls == []  # 外部副作用ゼロ
