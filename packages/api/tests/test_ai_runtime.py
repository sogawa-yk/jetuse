"""AI 組込スロット実行時バインド機構の単体テスト(SBA-02)。

LLM 呼び出しは `ai_runtime._completer` を差し替えて OCI へ出ずに検証する。検索(retrieval)・
カテゴリ導出・束縛解決・未束縛検出など、フレームワークの決定的部分を網羅する。
"""

import pytest

from jetuse_core.plugins import ai_runtime
from jetuse_core.plugins.sample_app import SampleAppError, validate_sample_app
from jetuse_core.plugins.sample_app_builtin import (
    knowledge_corpus,
    sba_a_definition,
)


@pytest.fixture
def fake_llm(monkeypatch):
    """_completer を差し替え、渡された messages を記録しつつ固定文字列を返す。"""
    calls: list[dict] = []

    def fake(model_key, messages, max_chars):
        calls.append({"model": model_key, "messages": messages, "max_chars": max_chars})
        # classify テストは system に「分類器」を含むので候補をそのまま返せるよう、
        # user 文を返す単純実装ではなく、テスト側が必要なら上書きする。
        return "FAKE_OUTPUT"

    monkeypatch.setattr(ai_runtime, "_completer", fake)
    return calls


# --- レジストリ / 束縛 --------------------------------------------------------


def test_sba_a_slots_all_bound():
    """SBA-A の全 aiSlot が実行時フレームワークで束縛済み(未束縛ゼロ)。"""
    assert ai_runtime.unbound_capabilities(sba_a_definition()) == []


def test_bound_capabilities_covers_sba02_set():
    caps = ai_runtime.bound_capabilities()
    assert {"rag.search", "summarize", "classify", "draft"} <= caps


def test_bind_unknown_slot_raises():
    with pytest.raises(SampleAppError):
        ai_runtime.bind_slot(sba_a_definition(), "no-such-slot")


def test_unbound_capability_error():
    """ハンドラ未登録の capability を持つ定義は UnboundCapabilityError になる。"""
    definition = validate_sample_app(
        {
            "screens": [
                {"key": "s", "title": "S", "type": "list", "slots": ["x"]}
            ],
            "aiSlots": [{"key": "x", "title": "OCR", "capability": "vlm.ocr"}],
        }
    )
    assert ai_runtime.unbound_capabilities(definition) == ["vlm.ocr"]
    with pytest.raises(ai_runtime.UnboundCapabilityError):
        ai_runtime.bind_slot(definition, "x")


# --- 検索(retrieval) --------------------------------------------------------


def test_retrieve_ranks_relevant_first():
    corpus = knowledge_corpus()
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits, "関連 FAQ が取れること"
    top = hits[0]["row"]
    assert "パスワード" in top["question"]
    assert all(h["score"] > 0 for h in hits)


def test_retrieve_empty_corpus():
    assert ai_runtime.retrieve("何か", [], top_k=3) == []


def test_retrieve_no_match_returns_empty():
    corpus = [{"question": "天気", "answer": "晴れ"}]
    assert ai_runtime.retrieve("XYZ123ZZZ", corpus, top_k=3) == []


def test_retrieve_reports_relevance():
    corpus = knowledge_corpus()
    hits = ai_runtime.retrieve("パスワードを忘れてログインできません", corpus, top_k=3)
    assert hits[0]["relevance"] >= ai_runtime.MIN_RAG_RELEVANCE


def test_rag_weak_match_is_ungrounded(monkeypatch):
    """偶発的な弱い語彙一致だけのときは grounded=False(無関係入力に引用を付けない)。"""
    called = []
    monkeypatch.setattr(
        ai_runtime, "_completer", lambda m, msgs, mc: called.append(1) or "x"
    )
    # 質問特徴数が多く、FAQ とは少数バイグラムしか一致しない無関係入力。
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "faq-answer",
        {"input": "ZZZ9999 まったく無関係なキーワード羅列テスト用文字列"},
        owner="u1",
        corpus=knowledge_corpus(),
    )
    assert res["grounded"] is False
    assert res["citations"] == []
    assert called == [], "no-hit では LLM を呼ばない"


# --- rag.search --------------------------------------------------------------


def test_rag_search_grounded(monkeypatch):
    corpus = knowledge_corpus()
    captured = {}

    def fake(model_key, messages, max_chars):
        captured["user"] = messages[-1]["content"]
        return "再設定リンクから変更できます。"

    monkeypatch.setattr(ai_runtime, "_completer", fake)
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "faq-answer",
        {"input": "ログインのパスワードを忘れた"},
        owner="u1",
        corpus=corpus,
    )
    assert res["capability"] == "rag.search"
    assert res["grounded"] is True
    assert res["citations"], "引用が付くこと"
    # コンテキストに検索した FAQ 本文が含まれていること(grounding)。
    assert "パスワード" in captured["user"]
    assert res["slot"] == "faq-answer"


def test_rag_citations_drop_weak_companions(monkeypatch):
    """強い一致1件＋弱い随伴一致のとき、引用は強い方に絞られる(相対絞り込み)。"""
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "回答")
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "faq-answer",
        {"input": "パスワードを忘れてログインできません。どうすればいいですか？", "top_k": 5},
        owner="u1",
        corpus=knowledge_corpus(),
    )
    assert res["grounded"] is True
    # 入力は password FAQ の質問とほぼ一致 → 引用は password FAQ に絞られる(随伴一致は除外)。
    assert len(res["citations"]) == 1
    assert "パスワード" in res["citations"][0]["label"]


def test_rag_search_no_hit_returns_ungrounded(fake_llm):
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "faq-answer",
        {"input": "ZZZQQQ無関係キーワード"},
        owner="u1",
        corpus=[{"question": "天気", "answer": "晴れ"}],
    )
    assert res["grounded"] is False
    assert res["citations"] == []
    # ヒット 0 のときは LLM を呼ばない(コスト節約・ハルシネーション防止)。
    assert fake_llm == []


def test_rag_search_requires_input(fake_llm):
    with pytest.raises(ai_runtime.SlotInputError):
        ai_runtime.invoke_slot(
            sba_a_definition(), "faq-answer", {"input": "  "}, owner="u1",
            corpus=knowledge_corpus(),
        )


# --- classify ----------------------------------------------------------------


def test_classify_uses_corpus_categories(monkeypatch):
    seen = {}

    def fake(model_key, messages, max_chars):
        seen["user"] = messages[-1]["content"]
        return "アカウント"

    monkeypatch.setattr(ai_runtime, "_completer", fake)
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "auto-classify",
        {"input": "ログインできずロックされた"},
        owner="u1",
        corpus=knowledge_corpus(),
    )
    assert res["capability"] == "classify"
    assert res["category"] == "アカウント"
    assert "アカウント" in res["candidates"]


def test_classify_normalizes_noisy_output(monkeypatch):
    monkeypatch.setattr(
        ai_runtime, "_completer", lambda m, msgs, mc: "カテゴリは「請求」です。"
    )
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "auto-classify",
        {"input": "請求書がほしい", "categories": ["アカウント", "請求", "障害"]},
        owner="u1",
        corpus=[],
    )
    assert res["category"] == "請求"


def test_classify_unmatched_output_flags_low_confidence(monkeypatch):
    """LLM 出力が候補に一致しないときは matched=False（先頭フォールバックを自信ありに見せない）。"""
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "全く別の文字列")
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "auto-classify",
        {"input": "本文", "categories": ["アカウント", "請求"]},
        owner="u1",
        corpus=[],
    )
    assert res["matched"] is False
    assert res["category"] == "アカウント"  # 先頭フォールバック


def test_classify_empty_output_is_unmatched(monkeypatch):
    """空/空白の LLM 応答は matched=False（空文字の部分列誤一致を弾く）。"""
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "   ")
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "auto-classify",
        {"input": "本文", "categories": ["アカウント", "請求"]},
        owner="u1",
        corpus=[],
    )
    assert res["matched"] is False
    assert res["category"] == "アカウント"


def test_retrieve_ignores_polite_filler(monkeypatch):
    """丁寧表現のバイグラム雑音に引きずられず、内容語で関連 FAQ を上位化する。"""
    corpus = knowledge_corpus()
    hits = ai_runtime.retrieve("APIのレート制限について教えてください", corpus, top_k=3)
    assert hits, "関連 FAQ が取れること"
    assert "API" in hits[0]["row"]["question"]


def test_classify_without_categories_errors(fake_llm):
    with pytest.raises(ai_runtime.SlotInputError):
        ai_runtime.invoke_slot(
            sba_a_definition(),
            "auto-classify",
            {"input": "本文"},
            owner="u1",
            corpus=[{"question": "q", "answer": "a"}],  # category 列なし
        )


# --- summarize / draft -------------------------------------------------------


def test_summarize(monkeypatch):
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "要約結果")
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "summarize-thread",
        {"input": "長い問い合わせ本文。" * 10},
        owner="u1",
    )
    assert res == {"capability": "summarize", "summary": "要約結果", "slot": "summarize-thread"}


def test_draft_grounds_on_faq(monkeypatch):
    seen = {}

    def fake(model_key, messages, max_chars):
        seen["user"] = messages[-1]["content"]
        return "お世話になっております。…(返信ドラフト)"

    monkeypatch.setattr(ai_runtime, "_completer", fake)
    res = ai_runtime.invoke_slot(
        sba_a_definition(),
        "reply-draft",
        {"input": "請求書はどこからダウンロードできますか"},
        owner="u1",
        corpus=knowledge_corpus(),
    )
    assert res["capability"] == "draft"
    assert res["draft"]
    assert res["citations"], "FAQ を根拠に引用が付くこと"
    assert "請求" in seen["user"]


@pytest.mark.parametrize("slot_key", ["faq-answer", "summarize-thread", "reply-draft"])
def test_empty_llm_response_raises_inference_error(monkeypatch, slot_key):
    """LLM 空応答を grounded=True/空本文の「成功」に偽装せず、推論失敗として送出する。"""
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "   ")
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            sba_a_definition(),
            slot_key,
            {"input": "ログインのパスワードを忘れた"},
            owner="u1",
            corpus=knowledge_corpus(),
        )


def test_retrieve_high_relevance_survives_topk_over_verbose_rows():
    """冗長な高 score・低 relevance 行が枠を占めても、高 relevance の真の一致が top_k に残る。

    旧実装(raw score 主キー)では落ちるデータにしてある(=relevance 主キーの回帰を弁別する)。
    """
    # query は4特徴。target は短く2特徴一致(score=2, relevance=2/2=1.0)。
    # verbose は3特徴一致だが長いため低 relevance(score=3, relevance=3/4=0.75)。
    # verbose を top_k 件数ぶん置くと、raw-score 順では verbose が枠を占め target(score=2)が落ちる。
    # row_text は question+answer+category を連結するため、特徴を増やさないよう ASCII の
    # query 語のみで構成する(CJK を混ぜると bigram 特徴が増えて relevance が下がり弁別が崩れる)。
    query = "alpha bravo charlie delta"
    target = {"question": "alpha bravo", "answer": "alpha", "category": "bravo"}  # 2特徴・rel高
    corpus = [target]
    for n in range(3):  # top_k と同数の verbose(3特徴一致だが長く低 relevance)
        corpus.append(
            {
                "question": f"alpha bravo charlie f{n}1 f{n}2 f{n}3 f{n}4",
                "answer": "charlie",
                "category": "alpha",
            }
        )

    # 前提確認: raw-score では target(2) が verbose 群(3)より低い(=旧実装なら top_k から落ちる)。
    qf = ai_runtime._features(query)
    tfeat = ai_runtime._features(ai_runtime._row_text(target))
    target_score = len(qf & tfeat)
    verbose_score = len(qf & ai_runtime._features(ai_runtime._row_text(corpus[1])))
    target_rel = target_score / min(len(qf), len(tfeat))
    assert verbose_score > target_score, "verbose の raw score が target より高いこと(弁別の前提)"
    assert target_rel == 1.0, f"target は高 relevance であること: {target_rel}"

    hits = ai_runtime.retrieve(query, corpus, top_k=3)
    labels = [h["row"]["question"] for h in hits]
    assert "alpha bravo" in labels, f"高 relevance の真の一致が top_k に残ること: {labels}"
    assert hits[0]["row"]["question"] == "alpha bravo"  # relevance 最大が先頭


def test_input_truncated_to_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ai_runtime,
        "_completer",
        lambda m, msgs, mc: seen.update(user=msgs[-1]["content"]) or "ok",
    )
    huge = "あ" * (ai_runtime.MAX_INPUT_CHARS + 500)
    ai_runtime.invoke_slot(
        sba_a_definition(), "summarize-thread", {"input": huge}, owner="u1"
    )
    # 入力上限で切り詰められている(MAX_INPUT_CHARS を超えない)。
    assert ("あ" * (ai_runtime.MAX_INPUT_CHARS + 1)) not in seen["user"]
