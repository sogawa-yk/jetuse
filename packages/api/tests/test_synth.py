"""合成エンジン(HBD-03)の単体テスト。

推薦 → デモ構成オブジェクト＋プレビュー定義の合成を、代表構成(SBA-A/SBA-B)と
境界(未束縛 capability・組込点なし・主SBA未確定・未知SBA)で検証する。
"""

import pytest

from jetuse_core.plugins.ai_runtime import bound_capabilities
from jetuse_core.recommend import recommend
from jetuse_core.synth import (
    SBA_CODE_TO_INSTANCE,
    DemoComposition,
    SynthesisError,
    synthesize,
)


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


# --- シナリオ1: 代表構成 SBA-A + {RAG-QA, 要約, 分類} + Slack + chat + sample --


def test_scenario1_support_rag_qa_composes_and_previews():
    rec = recommend(_answers())
    comp = synthesize(rec)

    assert isinstance(comp, DemoComposition)
    assert comp.ok is True
    assert comp.sample_app == "SBA-A"
    assert comp.instance_id == SBA_CODE_TO_INSTANCE["SBA-A"]
    assert comp.app_name  # 解決した SBA 名
    assert comp.ui == "chat"
    assert comp.connectors == ["slack"]
    assert comp.highlight == "rag.search"

    # rag.search / summarize / classify は SBA-A に組込点があり ai_runtime 束縛済み → active。
    active = set(comp.active_parts)
    assert {"rag.search", "summarize", "classify"} <= active

    # プレビュー: 画面が描画でき、組込点(active slot)が画面に現れる。
    screen_keys = {s.key for s in comp.screens}
    assert {"faq", "inbox", "console"} <= screen_keys
    console = next(s for s in comp.screens if s.key == "console")
    caps_in_console = {sl["capability"] for sl in console.slots}
    assert "rag.search" in caps_in_console  # 対応コンソールに RAG 組込点
    # highlight 組込点に印が付く。
    assert any(sl["highlight"] for sl in console.slots)


def test_scenario1_seed_plan_reflects_sample_strategy():
    rec = recommend(_answers(Q6="sample"))
    comp = synthesize(rec)
    assert comp.seed.strategy == "sample"
    assert comp.seed.seeded is True
    assert comp.seed.total_seed_rows > 0  # コア同梱シードを投入
    assert {d["name"] for d in comp.seed.datasets} == {"faqs", "inquiries"}


def test_seed_replace_later_loads_no_rows():
    rec = recommend(_answers(Q6="replace_later"))
    comp = synthesize(rec)
    assert comp.seed.strategy == "replace_later"
    assert comp.seed.seeded is False
    assert comp.seed.total_seed_rows == 0
    assert all(d["seed_rows"] == 0 for d in comp.seed.datasets)
    # フィールド定義は保持(画面・データ計画は描ける)。
    assert all(d["fields"] > 0 for d in comp.seed.datasets)


def test_seed_genai_generated_marks_strategy():
    rec = recommend(_answers(Q6="industry_generated"))
    comp = synthesize(rec)
    assert comp.seed.strategy == "genai_generated"
    # プレビュー時点では未生成: 投入予定行は 0(コア同梱シードを投入扱いにしない)。
    assert comp.seed.seeded is False
    assert comp.seed.total_seed_rows == 0
    assert all(d["seed_rows"] == 0 for d in comp.seed.datasets)
    # 構造(列定義)は保持してデータ計画を描ける。
    assert all(d["fields"] > 0 for d in comp.seed.datasets)
    assert "GenAI" in comp.seed.note


# --- シナリオ2: NL2SQL を含む推薦(SBA-B)で実 capability にバインド ------------


def test_scenario2_nl2sql_binds_to_sba_b():
    rec = recommend(_answers(Q1="inventory", Q2=["business_db"], Q3="nl2sql"))
    assert rec.sample_app == "SBA-B"
    comp = synthesize(rec)

    assert comp.ok is True
    assert comp.instance_id == SBA_CODE_TO_INSTANCE["SBA-B"]
    # nl2sql/chart は SBA-B に組込点があり束縛済み → active で組込点に現れる。
    assert "nl2sql" in comp.active_parts
    nl2sql_binding = next(b for b in comp.bindings if b.capability == "nl2sql")
    assert nl2sql_binding.status == "active"
    assert nl2sql_binding.screen_keys  # 組込点がプレビューに現れる
    query_screen = next(s for s in comp.screens if s.key == "query")
    assert any(sl["capability"] == "nl2sql" for sl in query_screen.slots)


# --- シナリオ3(境界): 未束縛/組込点なし/未確定/未知 SBA で安全に失敗・警告 ----


def test_scenario3_unbound_capability_excluded_with_warning():
    # 自動フィット後は recommend が no_slot 部品を ai_parts に残さないため、synth の安全縮退を
    # 直接検証する: SBA-A 構成に vlm.ocr(SBA-A に組込点なし)を注入し、no_slot として active から
    # 外れ excluded/warnings に残ることを確かめる。
    rec = recommend(_answers(Q1="support", Q2=["docs"], Q3="rag_qa"))
    rec = rec.model_copy(update={"ai_parts": [*rec.ai_parts, "vlm.ocr"]})
    assert "vlm.ocr" in rec.ai_parts

    comp = synthesize(rec)
    assert comp.ok is True  # 合成自体は成立(安全に縮退)
    assert "vlm.ocr" not in comp.active_parts  # active には載らない
    vlm = next(b for b in comp.bindings if b.capability == "vlm.ocr")
    # SBA-A は vlm.ocr の組込点(aiSlot)を持たないので、ai_runtime の束縛有無に関わらず no_slot。
    # (将来 SBA-05 等で vlm.ocr が束縛されても SBA-A 合成では no_slot のまま=このテストは安定。)
    assert vlm.status == "no_slot"
    assert any("vlm.ocr" in e["capability"] for e in comp.excluded)
    assert comp.warnings  # 黙って消さず理由を残す


def test_unbound_but_slotted_capability_marked_unbound(monkeypatch):
    """組込点はあるが ai_runtime 未束縛の capability は unbound として active から外れる。

    束縛レジストリを縮小して「組込点あり×未束縛」を再現し、no_slot とは区別されることを確かめる
    (実運用では ai_runtime は全コア能力を束縛済みだが、途中の未束縛状態を表現できること)。"""
    import jetuse_core.synth as synth_mod

    rec = recommend(_answers())  # SBA-A: rag.search/summarize/classify に組込点あり
    # summarize だけ束縛から外した集合を返すようにする。
    reduced = bound_capabilities() - {"summarize"}
    monkeypatch.setattr(synth_mod, "bound_capabilities", lambda: reduced)

    comp = synthesize(rec)
    summ = next(b for b in comp.bindings if b.capability == "summarize")
    assert summ.status == "unbound"  # 組込点はあるが未束縛
    assert summ.slot_keys  # 組込点(slot)は存在する
    assert "summarize" not in comp.active_parts


def test_scenario3_recommended_part_without_slot_is_no_slot():
    # 自動フィット後は recommend が nl2sql(SBA-A 組込点なし)を残さないため、synth の no_slot
    # 分類を直接検証する目的で nl2sql を注入する。
    rec = recommend(_answers(Q1="support", Q2=["docs"], Q3="rag_qa"))
    rec = rec.model_copy(update={"ai_parts": [*rec.ai_parts, "nl2sql"]})
    assert "nl2sql" in rec.ai_parts
    comp = synthesize(rec)
    nl = next(b for b in comp.bindings if b.capability == "nl2sql")
    assert nl.status == "no_slot"
    assert "nl2sql" not in comp.active_parts
    # RAG はちゃんと active。
    assert "rag.search" in comp.active_parts


def test_scenario3_unresolved_primary_sba_returns_failed_composition():
    rec = recommend(_answers(Q1="other"))
    assert rec.sample_app is None
    comp = synthesize(rec)
    assert comp.ok is False
    assert comp.errors  # 致命理由を明示
    assert comp.screens == []
    # strict ではエラー送出。
    with pytest.raises(SynthesisError):
        synthesize(rec, strict=True)


def test_unknown_sba_code_fails_safely():
    rec = recommend(_answers(Q1="accounting"))  # → SBA-D(未実装)
    assert rec.sample_app == "SBA-D"
    comp = synthesize(rec)
    assert comp.ok is False
    assert any("SBA-D" in e for e in comp.errors)
    with pytest.raises(SynthesisError):
        synthesize(rec, strict=True)


# --- 配布表現(再検証可能)を壊さない -----------------------------------------


def test_composition_report_attached_and_reverifiable():
    rec = recommend(_answers())
    comp = synthesize(rec)
    assert comp.composition_report is not None
    # 必要ケイパ/権限の整合チェックが同梱される(配布表現は再検証可能)。
    assert "rag.search" in comp.composition_report.required_capabilities
    assert comp.composition_report.ok is True


def test_available_capabilities_narrowing_surfaces_missing():
    rec = recommend(_answers())
    # ホストが rag.search を持たないと仮定 → composition_report に不足が出る。
    comp = synthesize(rec, available_capabilities=frozenset({"summarize", "classify"}))
    assert comp.composition_report is not None
    assert "rag.search" in comp.composition_report.missing_capabilities
    assert any("rag.search" in w for w in comp.warnings)


def test_synthesize_is_deterministic_and_side_effect_free():
    rec = recommend(_answers())
    a = synthesize(rec)
    b = synthesize(rec)
    assert a.model_dump() == b.model_dump()


# --- CON-03: コネクタ束縛(active/excluded) --------------------------------


def test_connector_binding_slack_active():
    comp = synthesize(recommend(_answers(Q4="slack")))
    # 後方互換: 生の推薦リストは維持。
    assert comp.connectors == ["slack"]
    # 束縛: slack はコアパレットに在り合成整合 → active。
    assert comp.active_connectors == ["slack"]
    sb = next(b for b in comp.connector_bindings if b.provider == "slack")
    assert sb.status == "active"
    assert sb.transport == "builtin"
    assert "post_message" in sb.actions
    assert "platform:connector.invoke" in sb.required_scopes
    assert sb.requires_secret is True  # oauth2
    assert sb.secret_ref == "slack-bot-token"  # 参照名(実値ではない)
    assert sb.reason is None


def test_connector_binding_excludes_outside_palette():
    rec = recommend(_answers(Q4="slack"))
    rec = rec.model_copy(update={"connectors": ["slack", "teams"]})
    comp = synthesize(rec)
    assert comp.active_connectors == ["slack"]  # teams は active にならない
    teams = next(b for b in comp.connector_bindings if b.provider == "teams")
    assert teams.status == "excluded"
    assert teams.transport is None
    assert teams.required_scopes == []
    assert teams.reason and "パレット外" in teams.reason
    # 黙って消さず warnings に理由が残る(§4)。
    assert any("teams" in w for w in comp.warnings)


def test_connector_binding_empty_when_no_connector():
    comp = synthesize(recommend(_answers(Q4="none")))
    assert comp.connectors == []
    assert comp.connector_bindings == []
    assert comp.active_connectors == []


def test_connector_binding_dedupes_providers():
    rec = recommend(_answers(Q4="slack"))
    rec = rec.model_copy(update={"connectors": ["slack", "slack"]})
    comp = synthesize(rec)
    providers = [b.provider for b in comp.connector_bindings]
    assert providers == ["slack"]  # 重複 provider は 1 件だけ束縛


def test_connector_binding_no_real_secret_in_definition():
    comp = synthesize(recommend(_answers(Q4="slack")))
    # 束縛結果に持つのは参照名のみ。実トークンらしき値を含まない。
    dump = comp.model_dump_json()
    assert "slack-bot-token" in dump  # 参照名(非機密)
    assert "xoxb-" not in dump  # 実 Bot トークンの prefix は出ない


def test_demo_composition_back_compat_old_payload_validates():
    # CON03-MAJ-002: connector_bindings / active_connectors を持たない旧 payload も検証できる
    # （新フィールドは default_factory=list。公開モデルの後方互換を維持）。
    comp = synthesize(recommend(_answers()))
    payload = comp.model_dump()
    payload.pop("connector_bindings")
    payload.pop("active_connectors")
    restored = DemoComposition.model_validate(payload)
    assert restored.connector_bindings == []
    assert restored.active_connectors == []
    assert restored.connectors == comp.connectors  # 旧フィールドは保持
