"""EXB-04: RAG Provider Adapter (EXB-03 RunProvider seam) のユニット/結合テスト。

Provider は jetuse_core の RAG generate 系に委譲し、EXB-03 の Run Engine が消費する capability
イベント dict (retrieval.started / retrieval.completed / message.delta) を yield。実 OCI/DB には
触れず、generate と resolve_citation_filenames をフェイク/identity 化して、イベント順序・
schema 準拠・citation 整形・Empty・認可(fail-closed)・topK/version・壊れ出力検出を検証。
実 Run Engine との結合は execute() を用いた統合テストで確認する。
"""

from dataclasses import dataclass, field

import pytest

from jetuse_core import rag
from jetuse_platform.contracts import (
    run_event_types,
    validate_action_with_citations_event,
    validate_action_with_citations_output,
)
from jetuse_platform.contracts.validators import ValidationError
from jetuse_platform.providers.rag_answer import CoreRagAnswerProvider

PRINCIPAL = "space-alpha"  # owner_sub。config 未束縛時は space=owner_sub(自分の Knowledge)


@dataclass
class FakeCtx:
    """service.runs.RunContext の最小 duck-type (owner_sub/input/config)。"""

    owner_sub: str = PRINCIPAL
    input: dict = field(default_factory=lambda: {"question": "保証期間は?"})
    config: dict | None = None


@pytest.fixture(autouse=True)
def _no_db_resolve(monkeypatch):
    # resolve_citation_filenames は DB(list_files)を引くので identity に差し替え(実 OCI/DB 非依存)
    monkeypatch.setattr(rag, "resolve_citation_filenames", lambda owner, cites: cites)


def _fake_generate(answer_text, cites):
    """(owner, prompt, *, top_k) -> (answer, citations) の委譲先フェイク。呼び出しを記録する。"""
    seen = {}

    def gen(owner, prompt, *, top_k=None):
        seen["owner"] = owner
        seen["prompt"] = prompt
        seen["top_k"] = top_k
        return answer_text, list(cites)

    return gen, seen


def _provider(gen, **kw):
    return CoreRagAnswerProvider(generate=gen, **kw)


def _run(provider, ctx=None):
    return list(provider.run(ctx or FakeCtx()))


def test_event_order_and_schema():
    cites = [{"file_id": "f1", "filename": "manual.pdf", "score": 0.82}]
    g, seen = _fake_generate("保証期間は1年です。", cites)
    events = _run(_provider(g))

    types = [e["type"] for e in events]
    assert types[0] == "retrieval.started"
    assert types[1] == "retrieval.completed"
    assert set(types[2:]) == {"message.delta"}
    assert len(types) >= 3

    for e in events:
        assert e["type"] in run_event_types()
        validate_action_with_citations_event(e)

    assert seen["owner"] == PRINCIPAL  # config 未束縛→自分の Knowledge
    # 出力相当(engine 組立と同じ)を再構成して outputSchema 準拠を確認
    answer = "".join(e["data"]["text"] for e in events if e["type"] == "message.delta")
    citations = next(e["data"]["citations"] for e in events if e["type"] == "retrieval.completed")
    validate_action_with_citations_output({"answer": answer, "citations": citations})
    assert answer == "保証期間は1年です。"
    assert citations == [{"source": "manual.pdf", "score": 0.82}]


def test_topk_from_bound_config_propagates():
    g, seen = _fake_generate("ans", [])
    ctx = FakeCtx(config={"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 7}})
    _run(_provider(g), ctx)
    assert seen["top_k"] == 7


def test_citation_mapping():
    cites = [
        {"file_id": "f1", "filename": "doc.pdf", "score": 0.5, "text": "抜粋"},
        {"file_id": "f2", "filename": "", "score": None},          # filename無→file_id
        {"file_id": "", "filename": "", "score": 0.1},              # source無→除外(他が残るので可)
    ]
    g, _ = _fake_generate("回答", cites)
    events = _run(_provider(g))
    got = next(e["data"]["citations"] for e in events if e["type"] == "retrieval.completed")
    assert [c["source"] for c in got] == ["doc.pdf", "f2"]
    assert got[0]["score"] == 0.5
    assert got[0]["snippet"] == "抜粋"
    assert "score" not in got[1]


def test_empty_path_no_exception():
    g, _ = _fake_generate("", [])
    events = _run(_provider(g))
    completed = next(e for e in events if e["type"] == "retrieval.completed")
    assert completed["data"]["citations"] == []
    answer = "".join(e["data"]["text"] for e in events if e["type"] == "message.delta")
    assert answer.strip()  # 「該当なし」系の非空回答(engine の incomplete-run 検証を満たす)


def test_empty_answer_from_backend_message_preserved():
    g, _ = _fake_generate("関連する情報が見つかりませんでした。", [])
    events = _run(_provider(g))
    answer = "".join(e["data"]["text"] for e in events if e["type"] == "message.delta")
    assert answer == "関連する情報が見つかりませんでした。"


def test_malformed_citations_not_masked_as_empty():
    g, _ = _fake_generate("本文あり", [{"file_id": "", "filename": ""}])
    with pytest.raises(RuntimeError):
        _run(_provider(g))


def test_non_str_answer_rejected():
    for bad in (0, False, b"bytes", None):
        g, _ = _fake_generate(bad, [])
        with pytest.raises(RuntimeError):
            _run(_provider(g))


def test_non_list_citations_rejected():
    for bad in ("notalist", ["notadict"], [123]):
        g, _ = _fake_generate("本文", bad)
        with pytest.raises(RuntimeError):
            _run(_provider(g))


def test_citations_but_empty_answer_raises():
    g, _ = _fake_generate("", [{"file_id": "f", "filename": "doc.pdf", "score": 0.5}])
    with pytest.raises(RuntimeError):
        _run(_provider(g))


# ---- 認可境界 (ADR-0024 Accepted) ----

def test_shared_space_denied_without_resolver():
    g, _ = _fake_generate("x", [])
    ctx = FakeCtx(owner_sub="me", config={"knowledge": {"space": "someone-elses-space"}})
    with pytest.raises(PermissionError):
        _run(_provider(g), ctx)


def test_shared_space_allowed_with_access_checked_resolver():
    g, seen = _fake_generate("ans", [{"file_id": "f", "filename": "kb.pdf", "score": 0.4}])
    calls = {}

    def resolver(space, version, principal):
        calls["args"] = (space, version, principal)
        return "resolved-owner"

    p = _provider(g, resolve_owner=resolver)
    ctx = FakeCtx(owner_sub="demo-user",
                  config={"knowledge": {"space": "curated-kb", "version": "2026-07"}})
    events = _run(p, ctx)
    assert calls["args"] == ("curated-kb", "2026-07", "demo-user")
    assert seen["owner"] == "resolved-owner"
    citations = next(e["data"]["citations"] for e in events if e["type"] == "retrieval.completed")
    assert citations[0]["source"] == "kb.pdf"


def test_own_space_allowed_without_resolver():
    g, seen = _fake_generate("ans", [])
    _run(_provider(g))  # config 未束縛→space=owner_sub
    assert seen["owner"] == PRINCIPAL


def test_missing_owner_sub_rejected():
    g, _ = _fake_generate("x", [])
    with pytest.raises(ValueError):
        _run(_provider(g), FakeCtx(owner_sub=""))


def test_resolver_returning_none_is_denied():
    g, _ = _fake_generate("x", [])
    ctx = FakeCtx(owner_sub="demo", config={"knowledge": {"space": "curated-kb"}})
    for bad in (lambda s, v, pr: None, lambda s, v, pr: "", lambda s, v, pr: "  "):
        with pytest.raises(PermissionError):
            _run(_provider(g, resolve_owner=bad), ctx)


# ---- topK / version ----

def test_version_rejected_without_resolver():
    g, _ = _fake_generate("x", [])
    ctx = FakeCtx(config={"knowledge": {"space": PRINCIPAL, "version": "v1"}})
    with pytest.raises(ValueError):
        _run(_provider(g), ctx)


def test_topk_upper_bound_rejected_by_schema():
    # 施主承認の契約上限(100)。境界は許可、超過は configSchema が弾く(暗黙クランプしない)
    g, seen = _fake_generate("ans", [])
    ctx_ok = FakeCtx(config={"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 100}})
    _run(_provider(g), ctx_ok)
    assert seen["top_k"] == 100
    ctx_bad = FakeCtx(config={"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 101}})
    with pytest.raises(ValidationError):
        _run(_provider(_fake_generate("ans", [])[0]), ctx_bad)


def test_call_delegate_rejects_over_max_defense_in_depth():
    # Provider/backend 境界の上限ガード(schema を迂回する直接呼び出し等への防御)。明示エラー。
    g, _ = _fake_generate("ans", [])
    p = _provider(g)  # 注入 delegate は topK 対応扱い
    with pytest.raises(ValueError, match="exceeds max"):
        p._call_delegate("owner", "q", 101)


def test_topk_rejected_for_backend_without_support():
    p = CoreRagAnswerProvider(backend="select_ai")  # topK 非対応・実 backend に到達しない
    ctx = FakeCtx(config={"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 3}})
    with pytest.raises(ValueError):
        _run(p, ctx)


# ---- 構築 ----

def test_construction_requires_exactly_one_of_backend_or_generate():
    g = _fake_generate("x", [])[0]
    with pytest.raises(ValueError):
        CoreRagAnswerProvider()
    with pytest.raises(ValueError):
        CoreRagAnswerProvider(backend="select_ai", generate=g)
    with pytest.raises(ValueError):
        CoreRagAnswerProvider(backend="nonexistent")
    assert callable(CoreRagAnswerProvider(backend="select_ai")._generate)


def test_backend_capability_flags():
    assert CoreRagAnswerProvider(backend="opensearch")._supports_topk is True
    assert CoreRagAnswerProvider(backend="select_ai")._supports_topk is False


def test_bad_config_rejected():
    g, _ = _fake_generate("x", [])
    ctx = FakeCtx(config={"retrieval": {"topK": 1}})  # knowledge 欠落
    with pytest.raises(ValidationError):
        _run(_provider(g), ctx)


def test_empty_bound_config_rejected_not_defaulted():
    # 明示束縛の空 dict {} は「壊れた Experience 設定」→ 既定で隠さず schema で弾く(None と区別)
    g, _ = _fake_generate("x", [])
    with pytest.raises(ValidationError):
        _run(_provider(g), FakeCtx(config={}))


def test_delegate_exception_propagates():
    def boom(owner, prompt, *, top_k=None):
        raise RuntimeError("backend down")
    with pytest.raises(RuntimeError, match="backend down"):
        _run(_provider(boom))


# ---- 実 Run Engine (EXB-03) との結合: RunProvider 契約適合を実エンジンで検証 ----

def test_integrates_with_exb03_run_engine():
    from service.runs import RunStore, execute

    g, _ = _fake_generate("保証期間は1年です。",
                          [{"file_id": "f1", "filename": "manual.pdf", "score": 0.82}])
    store = RunStore()
    run = store.create("exp-x", "answer.with-citations@1",
                       {"question": "保証期間は?"}, PRINCIPAL)
    execute(store, run, _provider(g), PRINCIPAL)  # 実 engine が lifecycle/出力組立/順序検証を担う

    events = store.events(run.run_id, PRINCIPAL)
    assert [e.type for e in events] == [
        "run.started", "retrieval.started", "retrieval.completed",
        "message.delta", "run.completed",
    ]
    assert store.get(run.run_id, PRINCIPAL).status == "completed"
    output = next(e for e in events if e.type == "run.completed").data["output"]
    validate_action_with_citations_output(output)
    assert output["answer"] == "保証期間は1年です。"
    assert output["citations"] == [{"source": "manual.pdf", "score": 0.82}]


def test_engine_maps_provider_failure_to_run_failed():
    from service.runs import RunStore, execute

    def boom(owner, prompt, *, top_k=None):
        raise RuntimeError("backend down")
    store = RunStore()
    run = store.create("exp-x", "answer.with-citations@1", {"question": "q"}, PRINCIPAL)
    execute(store, run, _provider(boom), PRINCIPAL)
    assert store.get(run.run_id, PRINCIPAL).status == "failed"
    assert [e.type for e in store.events(run.run_id, PRINCIPAL)][-1] == "run.failed"


# ---- config-gated 配線 (_select_provider) ----

def test_select_provider_gated_by_explicit_backend(monkeypatch):
    from types import SimpleNamespace as NS

    import jetuse_core.settings as settings_mod
    from service import runs as runs_mod
    from service.runs import StubProvider

    def _settings(backend):
        return lambda: NS(rag_answer_backend=backend)

    monkeypatch.setattr(settings_mod, "get_settings", _settings(""))  # 空=stub(既定)
    assert isinstance(runs_mod._select_provider(), StubProvider)
    monkeypatch.setattr(settings_mod, "get_settings", _settings("opensearch"))
    assert isinstance(runs_mod._select_provider(), CoreRagAnswerProvider)
    monkeypatch.setattr(settings_mod, "get_settings", _settings("select_ai"))
    assert isinstance(runs_mod._select_provider(), CoreRagAnswerProvider)


def test_select_provider_bad_backend_fails_loud(monkeypatch):
    # 明示指定が不正なら stub に握り潰さず送出(実 RAG 失敗を架空回答で隠さない。EXB04-045)
    from types import SimpleNamespace as NS

    import jetuse_core.settings as settings_mod
    from service import runs as runs_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: NS(rag_answer_backend="bogus"))
    with pytest.raises(ValueError):
        runs_mod._select_provider()


def test_engine_empty_run_completes_with_empty_citations():
    from service.runs import RunStore, execute

    g, _ = _fake_generate("", [])
    store = RunStore()
    run = store.create("exp-x", "answer.with-citations@1",
                       {"question": "無関係な質問"}, PRINCIPAL)
    execute(store, run, _provider(g), PRINCIPAL)
    assert store.get(run.run_id, PRINCIPAL).status == "completed"  # Empty は run.failed にしない
    output = next(e for e in store.events(run.run_id, PRINCIPAL)
                  if e.type == "run.completed").data["output"]
    assert output["citations"] == []
    assert output["answer"].strip()
