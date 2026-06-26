"""コア同梱 sample-app SBA-B(在庫・受発注照会 / NL2SQL)定義の単体テスト(SBA-03)。"""

from jetuse_core.plugins.sample_app import (
    required_permissions,
    validate_composition,
    validate_sample_app,
)
from jetuse_core.plugins.sample_app_builtin_sba_b import (
    SBA_B_INSTANCE_ID,
    get_sba_b_sample_app,
    sba_b_definition,
    sba_b_manifest,
    sba_b_summary,
)


def test_manifest_and_definition_valid():
    m = sba_b_manifest()
    assert m.kind == "sample-app"
    d = sba_b_definition()
    assert {s.key for s in d.screens} == {"inventory", "orders", "query"}
    assert {s.key for s in d.ai_slots} == {"nl2sql-query", "result-chart"}
    assert {s.capability for s in d.ai_slots} == {"nl2sql", "chart"}


def test_composition_ok():
    """合成バリデーションが致命的不足なし(db.query スコープ宣言整合)。"""
    report = validate_composition(sba_b_manifest())
    assert report.ok, report.model_dump()
    assert report.missing_capabilities == []
    assert report.undeclared_permissions == []


def test_required_permissions_subset_of_manifest():
    m = sba_b_manifest()
    assert required_permissions(sba_b_definition()) == {"platform:db.query"}
    assert required_permissions(sba_b_definition()) <= set(m.permissions)


def test_datasets_have_seed():
    d = sba_b_definition()
    inv = next(ds for ds in d.datasets if ds.name == "inventory")
    orders = next(ds for ds in d.datasets if ds.name == "orders")
    assert len(inv.seed) >= 10
    assert len(orders.seed) >= 10


def test_order_amount_is_qty_times_price():
    """受発注の金額が数量×単価で一貫している(集計照会の検算前提)。"""
    d = sba_b_definition()
    orders = next(ds for ds in d.datasets if ds.name == "orders")
    for row in orders.seed:
        assert row["amount"] == row["quantity"] * row["unit_price"]


def test_listing_and_lookup():
    summary = sba_b_summary()
    assert summary["id"] == SBA_B_INSTANCE_ID
    assert set(summary["capabilities"]) == {"nl2sql", "chart"}

    full = get_sba_b_sample_app(SBA_B_INSTANCE_ID)
    assert full is not None
    assert full["knowledge_dataset"] is None  # NL2SQL は RAG コーパス不要
    assert "definition" in full and "screens" in full["definition"]
    # 公開 ID は URL-safe な instance id に統一。
    assert get_sba_b_sample_app(sba_b_manifest().id) is None
    assert get_sba_b_sample_app("nope") is None


def test_definition_roundtrips_through_alias():
    """by_alias dump(aiSlots camelCase)が再検証でき、形状が一致する(配布表現の往復)。"""
    full = get_sba_b_sample_app(SBA_B_INSTANCE_ID)
    assert "aiSlots" in full["definition"]
    revalidated = validate_sample_app(full["definition"])
    original = sba_b_definition()
    assert {s.key for s in revalidated.ai_slots} == {s.key for s in original.ai_slots}
    assert [ds.name for ds in revalidated.datasets] == [ds.name for ds in original.datasets]
