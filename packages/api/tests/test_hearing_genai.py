"""ヒアリング GenAI 補助(HBD-01 / §6)の単体テスト。LLM はモック、フォールバックを厳密に確認。"""

import jetuse_core.hearing_genai as hg


def _patch_completer(monkeypatch, fn):
    monkeypatch.setattr(hg, "_completer", fn)


def test_suggest_parses_and_validates(monkeypatch):
    _patch_completer(
        monkeypatch,
        lambda *a, **k: '```json\n{"Q1":"support","Q2":["docs"],"Q3":"rag_qa"}\n```',
    )
    out = hg.suggest_answers_from_notes("サポート部門。社内マニュアルで回答", model_key="m")
    assert out == {"Q1": "support", "Q2": ["docs"], "Q3": "rag_qa"}


def test_suggest_drops_invalid_ids(monkeypatch):
    # 未知 id / 未知質問は黙って捨て、妥当な提案だけ残す(部分提案)。
    _patch_completer(
        monkeypatch,
        lambda *a, **k: '{"Q1":"bogus","Q2":["docs","xxx"],"Q9":"x","Q4":"slack"}',
    )
    out = hg.suggest_answers_from_notes("メモ", model_key="m")
    assert out == {"Q4": "slack"}  # Q1=bogus捨て / Q2=未知混在で検証失敗 / Q9=未知質問 / Q4のみ妥当


def test_suggest_empty_notes_returns_empty(monkeypatch):
    called = []
    _patch_completer(monkeypatch, lambda *a, **k: called.append(1) or "{}")
    assert hg.suggest_answers_from_notes("   ", model_key="m") == {}
    assert not called  # 空メモは LLM を呼ばない


def test_suggest_llm_failure_falls_back_to_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("genai down")

    _patch_completer(monkeypatch, boom)
    # 例外を投げず空(=提案なし)。決定ルールでの推薦は別途成立。
    assert hg.suggest_answers_from_notes("メモ", model_key="m") == {}


def test_suggest_unparseable_returns_empty(monkeypatch):
    _patch_completer(monkeypatch, lambda *a, **k: "申し訳ありませんが分かりません")
    assert hg.suggest_answers_from_notes("メモ", model_key="m") == {}


def test_nearest_sample_app_extracts_code(monkeypatch):
    _patch_completer(monkeypatch, lambda *a, **k: "SBA-C が最も近いです")
    assert hg.nearest_sample_app("新規事業の商談管理", model_key="m") == "SBA-C"


def test_nearest_sample_app_out_of_vocab_none(monkeypatch):
    _patch_completer(monkeypatch, lambda *a, **k: "SBA-Z")
    assert hg.nearest_sample_app("メモ", model_key="m") is None


def test_nearest_sample_app_failure_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    _patch_completer(monkeypatch, boom)
    assert hg.nearest_sample_app("メモ", model_key="m") is None
