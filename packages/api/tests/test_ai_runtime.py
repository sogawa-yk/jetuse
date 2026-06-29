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
from jetuse_core.plugins.sample_app_builtin_sba_b import sba_b_definition

# 外部埋め込み禁止＋semantic 無効化の autouse フィクスチャは tests/conftest.py に集約(BE07-025)。
# スイート全体に適用され、semantic を検証するテストは各自 _semantic_enabled/_embedder を上書きする。


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


# --- semantic / vector retrieval (BE-07) ------------------------------------


def _fake_embedder_for(query_map):
    """テスト用埋め込み器。query_map: text -> ベクトル。未知テキストは零ベクトル(=無関係)。

    retrieve は SEARCH_QUERY / SEARCH_DOCUMENT の 2 経路で `_embedder` を呼ぶ。どちらも
    同じテキスト→ベクトルの対応で答えるフェイク(OCI へ出ない)。
    """

    def fake(texts, input_type):
        return [query_map.get(t, [0.0, 0.0, 0.0]) for t in texts]

    return fake


def test_retrieve_semantic_matches_on_meaning_not_lexical(monkeypatch):
    """semantic 有効時、語彙不一致でも意味的に近い行が上位に来る(ベクトル化の本体)。"""
    # コーパス2行: 語彙的にクエリと重ならないが、1行目だけ意味的に近い。
    corpus = [
        {"question": "サインインの合言葉を失念", "answer": "再発行できます"},
        {"question": "請求書のダウンロード場所", "answer": "履歴から取得"},
    ]
    query = "パスワードを忘れた"
    # 語彙経路では意味的に正しい行(index 0)を引けない(偶発的なバイグラム一致で別行が上位化する)。
    lexical = ai_runtime._retrieve_lexical(query, corpus, top_k=3)
    assert all(h["index"] != 0 for h in lexical), "語彙経路は意味的に正しい行を引けない前提"
    # 意味の近さをベクトルで表現: query と1行目を同方向、2行目を直交に。
    vecs = {
        query: [1.0, 0.0, 0.0],
        ai_runtime._row_text(corpus[0]): [0.9, 0.1, 0.0],  # cosine ≈ 0.994
        ai_runtime._row_text(corpus[1]): [0.0, 0.0, 1.0],  # cosine = 0.0
    }
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    monkeypatch.setattr(ai_runtime, "_embedder", _fake_embedder_for(vecs))
    hits = ai_runtime.retrieve(query, corpus, top_k=3)
    assert hits, "semantic で意味的に近い行が取れること"
    assert hits[0]["row"]["question"] == "サインインの合言葉を失念"
    assert hits[0]["relevance"] >= ai_runtime.MIN_SEMANTIC_RELEVANCE
    # 直交(無関係)の行は下限未満で除外される。
    assert all(h["row"]["question"] != "請求書のダウンロード場所" for h in hits)
    # 戻り値形状は lexical と同形(後方互換)。
    assert set(hits[0]) == {"index", "score", "relevance", "label", "row"}


def test_retrieve_semantic_drops_below_threshold(monkeypatch):
    """全行が下限未満(無関係)なら semantic でも空(誤 grounded を防ぐ)。"""
    corpus = [{"question": "天気の話", "answer": "晴れ"}]
    query = "在庫数の照会"
    vecs = {
        query: [1.0, 0.0],
        ai_runtime._row_text(corpus[0]): [0.1, 1.0],  # cosine ≈ 0.099 < 0.50
    }
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    monkeypatch.setattr(ai_runtime, "_embedder", _fake_embedder_for(vecs))
    assert ai_runtime.retrieve(query, corpus, top_k=3) == []


def test_retrieve_falls_back_to_lexical_when_disabled(monkeypatch):
    """semantic 無効(ベクトル未設定)時は従来の語彙重なりスコアで動く(回帰なし)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: False)
    # 無効時は埋め込み器を一切呼ばない(呼んだら失敗させて検出)。
    monkeypatch.setattr(
        ai_runtime,
        "_embedder",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("embedder must not be called")),
    )
    corpus = knowledge_corpus()
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and "パスワード" in hits[0]["row"]["question"]


def test_retrieve_falls_back_when_embedding_raises(monkeypatch):
    """semantic 有効でも埋め込み呼び出しが失敗したターンは従来スコアへ degrade(壊れない)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)

    def boom(texts, input_type):
        raise RuntimeError("OCI embed unavailable")

    monkeypatch.setattr(ai_runtime, "_embedder", boom)
    corpus = knowledge_corpus()
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and "パスワード" in hits[0]["row"]["question"]


def test_retrieve_falls_back_on_document_count_mismatch(monkeypatch):
    """文書埋め込みの件数が対象行数と一致しないと不正応答 → lexical フォールバック(行ずれ防止)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = knowledge_corpus()

    def short(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0, 0.0]]
        return [[1.0, 0.0, 0.0]]  # 1 本だけ(行数 < 文書数)

    monkeypatch.setattr(ai_runtime, "_embedder", short)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and "パスワード" in hits[0]["row"]["question"]


def test_retrieve_falls_back_on_dimension_mismatch(monkeypatch):
    """文書ベクトルの次元がクエリと異なれば不正応答 → lexical フォールバック。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def mismatched(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0, 0.0]]
        return [[1.0, 0.0]]  # 2 次元(クエリは 3 次元)

    monkeypatch.setattr(ai_runtime, "_embedder", mismatched)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    # lexical に退避し、語彙一致で同じ行を引ける。
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_retrieve_falls_back_on_non_finite_vector(monkeypatch):
    """零/NaN/Inf を含む埋め込みは不正応答 → lexical フォールバック(誤 no-hit を防ぐ)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def bad(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0, 0.0]]
        return [[float("nan"), 0.0, 0.0]]

    monkeypatch.setattr(ai_runtime, "_embedder", bad)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_retrieve_falls_back_on_zero_query_vector(monkeypatch):
    """零ノルムのクエリ埋め込み(縮退)も不正応答 → lexical フォールバック。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def zero_q(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[0.0, 0.0, 0.0]]
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(ai_runtime, "_embedder", zero_q)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_retrieve_falls_back_on_extra_query_vector(monkeypatch):
    """クエリ 1 件要求に対し複数ベクトルが返る破損応答 → lexical フォールバック(BE07-003)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def extra_q(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]  # 余分なクエリベクトル
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(ai_runtime, "_embedder", extra_q)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_retrieve_semantic_threshold_drops_weak_companion(monkeypatch):
    """強い1件＋下限(0.50)未満の随伴一致のとき、随伴は採用されない(BE07-006)。"""
    corpus = [
        {"question": "強い一致", "answer": "a"},
        {"question": "弱い随伴(0.50未満)", "answer": "b"},
    ]
    query = "q"
    vecs = {
        query: [1.0, 0.0],
        ai_runtime._row_text(corpus[0]): [1.0, 0.0],   # cosine = 1.0
        ai_runtime._row_text(corpus[1]): [0.48, 1.0],  # cosine ≈ 0.43 < 0.50
    }
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    monkeypatch.setattr(ai_runtime, "_embedder", _fake_embedder_for(vecs))
    hits = ai_runtime.retrieve(query, corpus, top_k=5)
    assert [h["row"]["question"] for h in hits] == ["強い一致"]


def test_retrieve_falls_back_on_denominator_overflow(monkeypatch):
    """各ノルム有限でも積がオーバーフローする極端ベクトルは破損扱い→lexical 退避(BE07-017)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def overflow(texts, input_type):
        # q=[1e200,0], doc=[0,1e200]: dot=0(有限) だが na*nb=1e400=inf → 0/inf=0.0 で黙殺される罠。
        if input_type == "SEARCH_QUERY":
            return [[1e200, 0.0]]
        return [[0.0, 1e200]]

    monkeypatch.setattr(ai_runtime, "_embedder", overflow)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_interactive_retry_strategy_defaults_to_no_retry():
    """対話的リトライ戦略の既定は再試行なし(待機をハードに頭打ち / BE07-016)。"""
    from jetuse_core import embeddings

    # max_attempts=1 で構築できること(再試行なし)。例外なく戦略が返る。
    rs = embeddings.interactive_retry_strategy()
    assert rs is not None


def test_retrieve_falls_back_on_bool_vector(monkeypatch):
    """bool 要素を含む埋め込みは不正応答として弾き lexical へ退避(BE07-008)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]

    def boolish(texts, input_type):
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0, 0.0]]
        return [[True, False, False]]  # bool は不正

    monkeypatch.setattr(ai_runtime, "_embedder", boolish)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


def test_retrieve_large_corpus_routes_to_lexical_without_dropping(monkeypatch):
    """上限超のコーパスは truncate せず lexical 全件評価へ。index>96 の正解も引ける(BE07-009)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    called = []

    def must_not_call(texts, input_type):
        called.append(input_type)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(ai_runtime, "_embedder", must_not_call)
    n = ai_runtime.MAX_SEMANTIC_CORPUS + 25
    corpus = [{"question": f"filler{i}", "answer": "zzz"} for i in range(n)]
    # 上限(96)より後ろ(index 110)にだけ語彙一致する正解を置く。
    target_idx = ai_runtime.MAX_SEMANTIC_CORPUS + 14
    corpus[target_idx] = {"question": "supercalifragilistic uniquetoken", "answer": "a"}
    hits = ai_runtime.retrieve("uniquetoken supercalifragilistic", corpus, top_k=3)
    assert called == [], "上限超では埋め込みを呼ばず lexical に振り分けること"
    assert hits and hits[0]["index"] == target_idx, "index>96 の正解も取りこぼさない"


def test_retrieve_long_query_routes_to_lexical(monkeypatch):
    """埋め込み可能長(EMBED_MAX_CHARS)超のクエリは切り詰めず lexical 全文評価へ(BE07-013)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    called = []
    monkeypatch.setattr(ai_runtime, "_embedder", lambda *a, **k: called.append(1) or [[1.0]])
    # 関連語を 2000 字より後ろにだけ置いた長文クエリ。
    query = ("無関係な前置き。" * 400) + " uniquetoken"
    assert len(query) > ai_runtime.EMBED_MAX_CHARS
    corpus = [{"question": "uniquetoken の手順", "answer": "a"},
              {"question": "別件", "answer": "b"}]
    hits = ai_runtime.retrieve(query, corpus, top_k=3)
    assert called == [], "長文クエリでは埋め込みを呼ばないこと"
    assert hits and hits[0]["row"]["question"] == "uniquetoken の手順"


def test_retrieve_long_corpus_row_routes_to_lexical(monkeypatch):
    """対象行が埋め込み可能長を超える場合も lexical 全文評価へ(BE07-013)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    called = []
    monkeypatch.setattr(ai_runtime, "_embedder", lambda *a, **k: called.append(1) or [[1.0]])
    long_answer = ("padding " * 400) + "uniquetoken"
    corpus = [{"question": "短い行", "answer": "x"},
              {"question": "長い行", "answer": long_answer}]
    assert len(ai_runtime._row_text(corpus[1])) > ai_runtime.EMBED_MAX_CHARS
    hits = ai_runtime.retrieve("uniquetoken", corpus, top_k=3)
    assert called == [], "長文行を含む場合は埋め込みを呼ばないこと"
    assert hits and hits[0]["row"]["question"] == "長い行"


def test_retrieve_semantic_validates_query_before_documents(monkeypatch):
    """破損クエリ埋め込み時は文書埋め込みを呼ばずに lexical へ退避(BE07-011)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    corpus = [{"question": "パスワードを忘れた", "answer": "再設定"}]
    seen = []

    def bad_query(texts, input_type):
        seen.append(input_type)
        if input_type == "SEARCH_QUERY":
            return [[0.0, 0.0, 0.0]]  # 零ノルム(破損)
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(ai_runtime, "_embedder", bad_query)
    hits = ai_runtime.retrieve("パスワードを忘れた", corpus, top_k=3)
    assert "SEARCH_DOCUMENT" not in seen, "破損クエリ時に文書埋め込みを呼ばないこと"
    assert hits and hits[0]["row"]["question"] == "パスワードを忘れた"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_retrieve_blank_query_skips_oci(monkeypatch, blank):
    """空/空白クエリは semantic 有効でも OCI を呼ばず [](BE07-012)。"""
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    called = []
    monkeypatch.setattr(
        ai_runtime, "_embedder", lambda *a, **k: called.append(1) or [[1.0]]
    )
    assert ai_runtime.retrieve(blank, knowledge_corpus(), top_k=3) == []
    assert called == [], "空クエリで埋め込みを呼ばないこと"


def test_rag_search_grounded_via_semantic(monkeypatch):
    """semantic 経路でも rag.search の契約(grounded/citations/answer)が後方互換で成立する。"""
    corpus = [
        {"question": "サインインの合言葉を失念", "answer": "再発行リンクから再設定できます"},
        {"question": "請求書のダウンロード場所", "answer": "履歴から取得"},
    ]
    vecs = {
        "パスワードを忘れた": [1.0, 0.0, 0.0],
        ai_runtime._row_text(corpus[0]): [0.95, 0.05, 0.0],
        ai_runtime._row_text(corpus[1]): [0.0, 0.0, 1.0],
    }
    monkeypatch.setattr(ai_runtime, "_semantic_enabled", lambda: True)
    monkeypatch.setattr(ai_runtime, "_embedder", _fake_embedder_for(vecs))
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "再発行できます。")
    defn = sba_a_definition()
    res = ai_runtime.invoke_slot(
        defn, "faq-answer", {"input": "パスワードを忘れた"}, owner="u1", corpus=corpus
    )
    assert res["capability"] == "rag.search"
    assert res["grounded"] is True
    assert res["citations"]
    assert res["citations"][0]["label"] == "サインインの合言葉を失念"


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


# --- nl2sql / chart ハンドラ(SBA-03 / SBA-B) -------------------------------


def test_sba_b_slots_all_bound():
    """SBA-B の全 aiSlot(nl2sql / chart)が実行時フレームワークで束縛済み。"""
    assert ai_runtime.unbound_capabilities(sba_b_definition()) == []


def test_nl2sql_generates_select(monkeypatch):
    """nl2sql ハンドラはスキーマ文脈付きで LLM を呼び、生成 SELECT を返す。"""
    seen = {}
    monkeypatch.setattr(
        ai_runtime,
        "_completer",
        lambda m, msgs, mc: seen.update(user=msgs[-1]["content"]) or
        "SELECT warehouse, SUM(quantity) FROM INVENTORY GROUP BY warehouse",
    )
    out = ai_runtime.invoke_slot(
        sba_b_definition(), "nl2sql-query",
        {"input": "倉庫別の在庫数を教えて"}, owner="u1",
    )
    assert out["capability"] == "nl2sql"
    assert out["sql"].upper().startswith("SELECT")
    # スキーマ文脈(テーブル名)がプロンプトに含まれること。
    assert "INVENTORY" in seen["user"] and "ORDERS" in seen["user"]


def test_nl2sql_strips_code_fences(monkeypatch):
    monkeypatch.setattr(
        ai_runtime,
        "_completer",
        lambda m, msgs, mc: "```sql\nSELECT * FROM ORDERS\n```",
    )
    out = ai_runtime.invoke_slot(
        sba_b_definition(), "nl2sql-query", {"input": "全件"}, owner="u1",
    )
    assert out["sql"] == "SELECT * FROM ORDERS"


def test_nl2sql_rejects_non_select_as_inference_failure(monkeypatch):
    """生成 SQL が更新系ならガードで弾き、成功偽装せず SlotInferenceError。"""
    monkeypatch.setattr(
        ai_runtime, "_completer", lambda m, msgs, mc: "DELETE FROM INVENTORY",
    )
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            sba_b_definition(), "nl2sql-query", {"input": "全部消して"}, owner="u1",
        )


def test_nl2sql_empty_response_is_inference_failure(monkeypatch):
    monkeypatch.setattr(ai_runtime, "_completer", lambda m, msgs, mc: "")
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            sba_b_definition(), "nl2sql-query", {"input": "x"}, owner="u1",
        )


def test_chart_proposes_spec(monkeypatch):
    """chart ハンドラは columns/rows を文脈に ChartSpec を返す(列名検証込み)。"""
    monkeypatch.setattr(
        ai_runtime,
        "_completer",
        lambda m, msgs, mc: '{"type":"bar","x":"warehouse","y":["qty"],'
        '"title":"倉庫別在庫","reason":"カテゴリ比較"}',
    )
    out = ai_runtime.invoke_slot(
        sba_b_definition(), "result-chart",
        {
            "question": "倉庫別の在庫数",
            "columns": ["warehouse", "qty"],
            "rows": [["東京DC", "320"], ["大阪DC", "140"]],
        },
        owner="u1",
    )
    assert out["capability"] == "chart"
    assert out["type"] == "bar"
    assert out["x"] == "warehouse" and out["y"] == ["qty"]


def test_chart_none_when_no_data(monkeypatch):
    """columns/rows が空なら LLM を呼ばず type=none(成功偽装しない)。"""
    called = {"n": 0}

    def fake(m, msgs, mc):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(ai_runtime, "_completer", fake)
    out = ai_runtime.invoke_slot(
        sba_b_definition(), "result-chart", {"question": "x"}, owner="u1",
    )
    assert out["type"] == "none"
    assert called["n"] == 0


def test_nl2sql_rejects_out_of_schema_table(monkeypatch):
    """生成 SQL が定義スキーマ外(別スキーマ/辞書ビュー)を参照したら成功偽装せず拒否。"""
    monkeypatch.setattr(
        ai_runtime, "_completer", lambda m, msgs, mc: "SELECT * FROM SYS.DBA_USERS",
    )
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            sba_b_definition(), "nl2sql-query", {"input": "ユーザー一覧"}, owner="u1",
        )


def test_nl2sql_rejects_unknown_bare_table(monkeypatch):
    monkeypatch.setattr(
        ai_runtime, "_completer", lambda m, msgs, mc: "SELECT * FROM secret_table",
    )
    with pytest.raises(ai_runtime.SlotInferenceError):
        ai_runtime.invoke_slot(
            sba_b_definition(), "nl2sql-query", {"input": "秘密"}, owner="u1",
        )
