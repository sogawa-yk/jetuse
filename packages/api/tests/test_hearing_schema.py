"""ヒアリング質問スキーマ(HBD-01)の検証テスト。"""

import pytest

from jetuse_core.hearing_schema import (
    ANSWERABLE_IDS,
    QUESTIONS,
    QUESTIONS_BY_ID,
    REQUIRED_IDS,
    HearingSchemaError,
    question_schema,
    validate_answer,
    validate_answers,
)


def test_question_set_shape():
    # Q1..Q6 ＋ Auto の 7 問。id は一意。
    ids = [q.id for q in QUESTIONS]
    assert ids == ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Auto"]
    assert len(ids) == len(set(ids))
    assert ANSWERABLE_IDS == ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]
    assert REQUIRED_IDS == ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]


def test_each_choice_question_has_unique_options():
    for q in QUESTIONS:
        if q.type in ("single", "multi"):
            opt_ids = [o.id for o in q.options]
            assert opt_ids, f"{q.id} に選択肢が無い"
            assert len(opt_ids) == len(set(opt_ids)), f"{q.id} の選択肢 id が重複"


def test_validate_single_ok_and_unknown():
    assert validate_answer("Q1", "support") == "support"
    with pytest.raises(HearingSchemaError):
        validate_answer("Q1", "nope")
    with pytest.raises(HearingSchemaError):
        validate_answer("Q1", ["support"])  # single にリスト


def test_validate_multi_rejects_duplicates_and_bounds():
    assert validate_answer("Q2", ["docs", "business_db"]) == ["docs", "business_db"]
    # 重複は拒否
    with pytest.raises(HearingSchemaError):
        validate_answer("Q2", ["docs", "docs"])
    # 未知 id 拒否
    with pytest.raises(HearingSchemaError):
        validate_answer("Q2", ["docs", "xxx"])
    # 文字列でない要素
    with pytest.raises(HearingSchemaError):
        validate_answer("Q2", ["docs", 1])
    # 空リストは許容(複数可だが必須選択は recommend 側で扱う)
    assert validate_answer("Q2", []) == []


def test_auto_question_rejects_answer():
    with pytest.raises(HearingSchemaError):
        validate_answer("Auto", "x")


def test_unknown_question_id():
    with pytest.raises(HearingSchemaError):
        validate_answer("Q99", "x")


def test_validate_answers_require_all():
    full = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    norm = validate_answers(full, require_all=True)
    assert norm["Q2"] == ["docs"]
    # 1 問欠けると require_all で失敗
    partial = {k: v for k, v in full.items() if k != "Q4"}
    with pytest.raises(HearingSchemaError):
        validate_answers(partial, require_all=True)
    # require_all=False なら部分回答でも通る
    assert validate_answers(partial, require_all=False)["Q1"] == "support"


def test_require_all_rejects_empty_required_multi():
    # Q2 は必須 multi。空配列は require_all で未回答扱い(min_selections)。
    answers = {
        "Q1": "support", "Q2": [], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    with pytest.raises(HearingSchemaError):
        validate_answers(answers, require_all=True)
    # require_all=False なら空 multi は許容(編集途中)。
    assert validate_answers(answers, require_all=False)["Q2"] == []


def test_question_schema_export():
    sch = question_schema()
    assert sch["version"] == "1"
    assert len(sch["questions"]) == len(QUESTIONS)
    assert sch["questions"][0]["id"] == "Q1"
    # QUESTIONS_BY_ID 索引の整合
    assert set(QUESTIONS_BY_ID) == {q.id for q in QUESTIONS}
