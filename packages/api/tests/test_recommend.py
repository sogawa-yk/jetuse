"""推薦ルールエンジン(HBD-01)の単体テスト。決定的写像を網羅的に確認する。"""

import pytest

from jetuse_core.hearing_schema import HearingSchemaError
from jetuse_core.recommend import Q1_TO_SBA, recommend


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


def test_representative_case_support_docs_ragqa():
    """§4 代表例: サポート＋文書＋RAG-QA → SBA-A ＋ {RAG-QA, 要約, 分類}。"""
    rec = recommend(_answers())
    assert rec.sample_app == "SBA-A"
    assert rec.needs_genai_nearest is False
    assert set(rec.ai_parts) == {"rag.search", "summarize", "classify"}
    assert rec.highlight == "rag.search"
    assert rec.connectors == ["slack"]
    assert rec.ui == "chat"
    assert rec.seed_strategy == "sample"
    assert rec.validation.ok is True
    assert rec.rationale  # 監査トレースが空でない


@pytest.mark.parametrize(
    "q1,expected",
    [("support", "SBA-A"), ("sales", "SBA-C"), ("inventory", "SBA-B"), ("accounting", "SBA-D")],
)
def test_q1_to_primary_sba(q1, expected):
    rec = recommend(_answers(Q1=q1, Q3="rag_qa", Q2=["docs"]))
    assert rec.sample_app == expected
    assert rec.needs_genai_nearest is False


def test_q1_other_falls_back_to_genai_nearest():
    """Q1=other は決定ルールで主 SBA 未定 → None＋needs_genai_nearest。推薦自体は成立。"""
    rec = recommend(_answers(Q1="other"))
    assert rec.sample_app is None
    assert rec.needs_genai_nearest is True
    # 部品・UI・シードは決定ルールだけで埋まる(GenAI 不在でも推薦が成立)。
    assert rec.ai_parts
    assert rec.ui == "chat"
    assert rec.seed_strategy == "sample"


def test_branch_db_plus_analysis_promotes_to_sba_b():
    """§3 分岐: Q2 業務DB＋Q3 集計分析 → 主役を SBA-B に格上げ。"""
    rec = recommend(_answers(Q1="support", Q2=["docs", "business_db"], Q3="nl2sql"))
    assert rec.sample_app == "SBA-B"
    assert "nl2sql" in rec.ai_parts
    assert "chart" in rec.ai_parts  # Q3=nl2sql は chart も素地化
    assert rec.highlight == "nl2sql"
    assert any("格上げ" in r for r in rec.rationale)


def test_branch_no_promote_when_already_sba_b():
    """既に SBA-B(inventory)なら格上げ分岐は冪等(重複格上げしない)。"""
    rec = recommend(_answers(Q1="inventory", Q2=["business_db"], Q3="nl2sql"))
    assert rec.sample_app == "SBA-B"
    assert not any("格上げ" in r for r in rec.rationale)


def test_ai_parts_union_of_q2_and_q3():
    rec = recommend(_answers(Q2=["docs", "audio"], Q3="agent"))
    # docs→{rag.search,summarize,classify}, audio→{minutes}, Q3 agent→{agent}
    assert set(rec.ai_parts) == {"rag.search", "summarize", "classify", "minutes", "agent"}
    assert rec.highlight == "agent"


def test_image_data_requests_vlm_ocr_with_warning():
    rec = recommend(_answers(Q2=["image"], Q3="ocr_extract"))
    assert "vlm.ocr" in rec.ai_parts
    assert "classify" in rec.ai_parts
    # vlm.ocr は MM-01 依存の警告が付く(部品は外さない)。
    assert any("MM-01" in w for w in rec.validation.warnings)


@pytest.mark.parametrize(
    "q4,expected", [("slack", ["slack"]), ("other_connector", []), ("none", [])]
)
def test_q4_connectors(q4, expected):
    rec = recommend(_answers(Q4=q4))
    assert rec.connectors == expected


@pytest.mark.parametrize(
    "q5,expected", [("chat_form", "chat"), ("notify", "notify"), ("report", "report")]
)
def test_q5_ui(q5, expected):
    assert recommend(_answers(Q5=q5)).ui == expected


@pytest.mark.parametrize(
    "q6,expected",
    [
        ("sample", "sample"),
        ("industry_generated", "genai_generated"),
        ("replace_later", "replace_later"),
    ],
)
def test_q6_seed(q6, expected):
    assert recommend(_answers(Q6=q6)).seed_strategy == expected


def test_ai_parts_are_deterministically_ordered():
    """同じ回答は常に同じ並び(決定的・監査可能)。"""
    a = recommend(_answers(Q2=["docs", "audio"], Q3="summarize_draft"))
    b = recommend(_answers(Q2=["audio", "docs"], Q3="summarize_draft"))
    assert a.ai_parts == b.ai_parts


def test_missing_answer_raises():
    with pytest.raises(HearingSchemaError):
        recommend({"Q1": "support"})  # 不足


def test_invalid_choice_raises():
    with pytest.raises(HearingSchemaError):
        recommend(_answers(Q3="bogus"))


def test_empty_required_multi_raises():
    # Q2=[] は素地が決まらないため recommend を成立させない(F-002)。
    with pytest.raises(HearingSchemaError):
        recommend(_answers(Q2=[]))


def test_all_q1_options_are_mapped():
    """Q1 の全選択肢が写像表に存在する(写像の網羅性)。"""
    from jetuse_core.hearing_schema import QUESTIONS_BY_ID

    for opt in QUESTIONS_BY_ID["Q1"].options:
        assert opt.id in Q1_TO_SBA
