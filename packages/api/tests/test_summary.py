"""構成サマリ生成(HBD-05)の単体テスト。決定的導出(構成図/OCIサービス/手順)＋エクスポート。

合成(synthesize)・推薦(recommend)は副作用なしの純関数なので、DB/GenAI なしでサマリ生成まで
通しで検証できる。想定効果(④)の GenAI 補助は narrative 引数で注入し、フォールバックも確認する。
"""

from jetuse_core.recommend import recommend
from jetuse_core.summary import (
    BASE_OCI_SERVICE,
    build_summary,
    capability_label,
    summary_to_markdown,
)
from jetuse_core.synth import synthesize

FULL_SUPPORT = {
    "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
    "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
}
FULL_INVENTORY = {
    "Q1": "inventory", "Q2": ["business_db"], "Q3": "nl2sql",
    "Q4": "none", "Q5": "report", "Q6": "sample",
}


def _composition(answers):
    return synthesize(recommend(answers))


def test_summary_derives_diagram_from_active_bindings():
    """①構成図: active な組込点から「どのデータに何の AI が効くか」を決定的に導出する。"""
    comp = _composition(FULL_SUPPORT)
    summary = build_summary(comp)
    assert summary.sample_app == "SBA-A"
    # 主役 rag.search が構成図に現れ、highlight される。
    rag = [f for f in summary.diagram if f.capability == "rag.search"]
    assert rag and rag[0].highlight is True
    assert rag[0].capability_label == capability_label("rag.search")
    # 主役経路が先頭に寄る(安定整列)。
    assert summary.diagram[0].highlight is True
    # 構成図の各経路は active な capability のみ(捏造しない)。
    assert set(f.capability for f in summary.diagram) <= set(comp.active_parts)


def test_summary_oci_services_are_deterministic_reference_subset():
    """②使う OCI サービス: 基盤 ADB を先頭に、active 部品・コネクタの該当サービスを集約する。"""
    comp = _composition(FULL_SUPPORT)
    summary = build_summary(comp)
    services = [s.service for s in summary.oci_services]
    assert services[0] == BASE_OCI_SERVICE  # 基盤(アプリ/業務データ)は常に先頭
    # rag.search → Generative AI(埋め込み/File Search)が現れる。
    assert any("File Search" in s for s in services)
    # Slack 連携 → API Gateway が現れる。
    assert any("API Gateway" in s for s in services)
    # used_for は理由(capability ラベル/連携)を持つ。
    base = next(s for s in summary.oci_services if s.service == BASE_OCI_SERVICE)
    assert base.used_for


def test_summary_steps_open_app_run_highlight_and_export():
    """③デモ手順: アプリ起動→主役実行→…→サマリエクスポートの順で並ぶ。"""
    comp = _composition(FULL_SUPPORT)
    summary = build_summary(comp)
    titles = [s.title for s in summary.steps]
    assert "起動環境で開く" in titles[0]
    assert any("主役 AI 機能" in t for t in titles)
    assert "エクスポート" in titles[-1]
    # order は 1 始まりの連番。
    assert [s.order for s in summary.steps] == list(range(1, len(summary.steps) + 1))


def test_summary_impact_uses_narrative_then_falls_back():
    """④想定効果: narrative があれば genai 出所、無ければ決定的フォールバック。"""
    comp = _composition(FULL_SUPPORT)
    with_narr = build_summary(comp, narrative="顧客効果の文章。")
    assert with_narr.impact_source == "genai"
    assert with_narr.impact == "顧客効果の文章。"
    without = build_summary(comp, narrative=None)
    assert without.impact_source == "deterministic"
    assert without.impact  # 非空のテンプレ文


def test_deterministic_impact_omits_inactive_capabilities():
    """④決定的フォールバックは active な部品の効果だけを述べる(未組込機能を約束しない / F-001)。

    SBA-A(FULL_SUPPORT)の active は rag.search/classify/summarize で draft は非 active。
    旧実装は「ドラフト作成」を常に書いていたが、新実装では未 active の draft 効果を出さない。
    """
    comp = _composition(FULL_SUPPORT)
    assert "draft" not in comp.active_parts
    impact = build_summary(comp, narrative=None).impact  # フォールバック文
    assert "ドラフト" not in impact  # 未組込(draft)の効果を書かない
    assert "根拠付き回答" in impact  # active な rag.search の効果は書く
    # export(常に narrative=None)も同様に未組込機能を載せない。
    assert "ドラフト" not in summary_to_markdown(build_summary(comp, narrative=None))


def test_summary_markdown_export_has_all_four_sections():
    comp = _composition(FULL_SUPPORT)
    summary = build_summary(comp)
    md = summary.markdown
    assert md == summary_to_markdown(summary)
    for heading in ("## ① 構成図", "## ② 使う OCI サービス", "## ③ デモ手順", "## ④ 想定効果"):
        assert heading in md


def test_summary_inventory_nl2sql_uses_adb_select_ai():
    """SBA-B(NL2SQL)では OCI サービスに ADB Select AI が現れる(構成依存で決定的に変わる)。"""
    comp = _composition(FULL_INVENTORY)
    assert comp.sample_app == "SBA-B"
    summary = build_summary(comp)
    assert any("Select AI" in s.service for s in summary.oci_services)
    assert any(f.capability == "nl2sql" for f in summary.diagram)
