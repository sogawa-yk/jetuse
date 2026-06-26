"""コア同梱 sample-app SBA-A 定義の単体テスト(SBA-02)。"""

from jetuse_core.plugins.sample_app import (
    required_permissions,
    validate_composition,
    validate_sample_app,
)
from jetuse_core.plugins.sample_app_builtin import (
    SBA_A_INSTANCE_ID,
    SBA_A_KNOWLEDGE_DATASET,
    builtin_sample_apps,
    get_builtin_sample_app,
    knowledge_corpus,
    sba_a_definition,
    sba_a_manifest,
)


def test_manifest_and_definition_valid():
    m = sba_a_manifest()
    assert m.kind == "sample-app"
    d = sba_a_definition()
    assert {s.key for s in d.screens} == {"faq", "inbox", "console"}
    assert {s.key for s in d.ai_slots} == {
        "faq-answer",
        "auto-classify",
        "summarize-thread",
        "reply-draft",
    }


def test_composition_ok():
    """合成バリデーションが致命的不足なし(必要能力充足・権限宣言整合)。"""
    report = validate_composition(sba_a_manifest())
    assert report.ok, report.model_dump()
    assert report.missing_capabilities == []
    assert report.undeclared_permissions == []


def test_required_permissions_subset_of_manifest():
    m = sba_a_manifest()
    assert required_permissions(sba_a_definition()) <= set(m.permissions)


def test_knowledge_corpus_is_faq_seed():
    corpus = knowledge_corpus()
    assert len(corpus) >= 5
    assert all("question" in r and "answer" in r for r in corpus)
    d = sba_a_definition()
    faqs = next(ds for ds in d.datasets if ds.name == SBA_A_KNOWLEDGE_DATASET)
    assert len(corpus) == len(faqs.seed)


def test_builtin_listing_and_lookup():
    apps = builtin_sample_apps()
    assert len(apps) == 1
    a = apps[0]
    assert a["id"] == SBA_A_INSTANCE_ID
    assert set(a["capabilities"]) == {"rag.search", "classify", "summarize", "draft"}

    full = get_builtin_sample_app(SBA_A_INSTANCE_ID)
    assert full is not None
    assert "definition" in full and "screens" in full["definition"]
    # 公開 ID は URL-safe な instance id に統一(plugin_id は path にマッチしないため受けない)。
    assert get_builtin_sample_app(sba_a_manifest().id) is None
    assert get_builtin_sample_app("nope") is None


def test_definition_roundtrips_through_alias():
    """by_alias dump(aiSlots camelCase)が実際に再検証でき、形状が一致する(配布表現の往復)。"""
    full = get_builtin_sample_app(SBA_A_INSTANCE_ID)
    assert "aiSlots" in full["definition"]
    # 単なるキー存在でなく validate_sample_app を通して往復健全性を確認する。
    revalidated = validate_sample_app(full["definition"])
    original = sba_a_definition()
    assert {s.key for s in revalidated.ai_slots} == {s.key for s in original.ai_slots}
    assert [ds.name for ds in revalidated.datasets] == [
        ds.name for ds in original.datasets
    ]
    assert [len(ds.seed) for ds in revalidated.datasets] == [
        len(ds.seed) for ds in original.datasets
    ]
