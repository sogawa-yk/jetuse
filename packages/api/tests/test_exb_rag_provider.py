"""EXB-04: RAG Provider Adapter (§8.1 CapabilityProvider) のユニット/結合テスト。

Adapter は jetuse_core の RAG generate 系に委譲し、Stage 0 契約 (answer-with-citations.*)
準拠のイベント/出力へ整形する。実 OCI/DB には触れず、委譲先 generate と
resolve_citation_filenames をフェイク/identity に差し替えて、イベント順序・schema 準拠・
citation 整形・Empty 経路・入力/config バリデーション・認可境界(fail-closed)・topK/version の
扱い・cancel/resume・壊れた委譲出力の検出を検証する。
"""

import asyncio

import pytest

from jetuse_core import rag
from jetuse_platform.contracts import (
    is_valid,
    run_event_types,
    validate_action_with_citations_event,
    validate_action_with_citations_output,
)
from jetuse_platform.contracts.validators import ValidationError
from jetuse_platform.providers.rag_answer import (
    CoreRagAnswerProvider,
    RunContext,
    drive,
)

PRINCIPAL = "space-alpha"  # principal == knowledge.space (自分の Knowledge。既定で許可)
CONFIG = {"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 3}}


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


def _run(provider, question="保証期間は?", principal=PRINCIPAL, **kw):
    return drive(provider, {"question": question}, principal=principal, **kw)


def _provider(gen, config=CONFIG, **kw):
    return CoreRagAnswerProvider(config, generate=gen, **kw)


def test_event_order_and_schema():
    cites = [{"file_id": "f1", "filename": "manual.pdf", "score": 0.82}]
    g, seen = _fake_generate("保証期間は1年です。", cites)
    events, handle = _run(_provider(g))
    output = handle.output

    types = [e["type"] for e in events]
    assert types[0] == "retrieval.started"
    assert types[1] == "retrieval.completed"
    assert set(types[2:]) == {"message.delta"}
    assert len(types) >= 3

    for e in events:
        assert e["type"] in run_event_types()
        validate_action_with_citations_event(e)

    assert seen["owner"] == "space-alpha"  # owner=解決済み(自分の space)
    assert seen["top_k"] == 3              # retrieval.topK が委譲先に伝播

    assert handle.status == "completed"
    validate_action_with_citations_output(output)
    delta_text = "".join(e["data"]["text"] for e in events if e["type"] == "message.delta")
    assert delta_text == output["answer"] == "保証期間は1年です。"
    assert output["citations"] == [{"source": "manual.pdf", "score": 0.82}]


def test_citation_mapping():
    cites = [
        {"file_id": "f1", "filename": "doc.pdf", "score": 0.5, "text": "抜粋"},
        {"file_id": "f2", "filename": "", "score": None},          # filename無→file_id
        {"file_id": "", "filename": "", "score": 0.1},              # source無→除外(他が残るので可)
    ]
    g, _ = _fake_generate("回答", cites)
    _, handle = _run(_provider(g))
    got = handle.output["citations"]
    assert [c["source"] for c in got] == ["doc.pdf", "f2"]
    assert got[0]["score"] == 0.5
    assert got[0]["snippet"] == "抜粋"
    assert "score" not in got[1]  # None は載せない


def test_empty_path_no_exception():
    g, _ = _fake_generate("", [])
    events, handle = _run(_provider(g))
    completed = next(e for e in events if e["type"] == "retrieval.completed")
    assert completed["data"]["citations"] == []
    assert handle.output["citations"] == []
    assert handle.output["answer"].strip()  # 「該当なし」系の非空回答
    validate_action_with_citations_output(handle.output)


def test_empty_answer_from_backend_message_preserved():
    g, _ = _fake_generate("関連する情報が見つかりませんでした。", [])
    _, handle = _run(_provider(g))
    assert handle.output["answer"] == "関連する情報が見つかりませんでした。"
    assert handle.output["citations"] == []


def test_malformed_citations_not_masked_as_empty():
    # 委譲先が citation を返したのに source を1つも作れない = 壊れた出力。Empty と混同せず raise
    g, _ = _fake_generate("本文あり", [{"file_id": "", "filename": ""}])
    with pytest.raises(RuntimeError):
        _run(_provider(g))


def test_non_str_answer_rejected():
    # delegate 契約は (str, list)。0/False/bytes 等の壊れた戻り値を Empty として通さない
    for bad in (0, False, b"bytes", None):
        g, _ = _fake_generate(bad, [])
        with pytest.raises(RuntimeError):
            _run(_provider(g))


def test_citations_but_empty_answer_raises():
    # citations があるのに本文が空 = 生成失敗/壊れた出力。Empty(空 citations)と混同せず raise
    g, _ = _fake_generate("", [{"file_id": "f", "filename": "doc.pdf", "score": 0.5}])
    with pytest.raises(RuntimeError):
        _run(_provider(g))


def test_config_is_deep_copied_at_construction():
    # 構築後に呼び出し元が元 config を変更しても Provider の束縛設定は不変 (認可・設定境界)
    g, seen = _fake_generate("ans", [])
    cfg = {"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": 3}}
    p = _provider(g, config=cfg)
    cfg["knowledge"]["space"] = "hijacked"
    cfg["retrieval"]["topK"] = 9999
    _run(p)
    assert seen["owner"] == PRINCIPAL  # 変更前の space
    assert seen["top_k"] == 3          # 変更前の topK


# ---- 認可境界 ----

def test_shared_space_denied_without_resolver():
    g, _ = _fake_generate("x", [])
    p = _provider(g, config={"knowledge": {"space": "someone-elses-space"}})
    with pytest.raises(PermissionError):
        _run(p, principal="me")


def test_shared_space_allowed_with_access_checked_resolver():
    g, seen = _fake_generate("ans", [{"file_id": "f", "filename": "kb.pdf", "score": 0.4}])
    p = _provider(g, config={"knowledge": {"space": "curated-kb", "version": "2026-07"}})
    calls = {}

    def resolver(space, version, principal):
        calls["args"] = (space, version, principal)
        return "resolved-owner"

    _, handle = _run(p, principal="demo-user", resolve_owner=resolver)
    assert calls["args"] == ("curated-kb", "2026-07", "demo-user")
    assert seen["owner"] == "resolved-owner"
    assert handle.output["citations"][0]["source"] == "kb.pdf"


def test_own_space_allowed_without_resolver():
    g, seen = _fake_generate("ans", [])
    _run(_provider(g))  # space == principal
    assert seen["owner"] == PRINCIPAL


def test_missing_principal_rejected():
    g, _ = _fake_generate("x", [])
    with pytest.raises(ValueError):
        _run(_provider(g), principal="")


def test_resolver_returning_none_is_denied():
    g, _ = _fake_generate("x", [])
    p = _provider(g, config={"knowledge": {"space": "curated-kb"}})
    for bad in (lambda s, v, pr: None, lambda s, v, pr: "", lambda s, v, pr: "  "):
        with pytest.raises(PermissionError):
            _run(p, principal="demo", resolve_owner=bad)


# ---- topK / version ----

def test_version_rejected_without_resolver():
    g, _ = _fake_generate("x", [])
    p = _provider(g, config={"knowledge": {"space": PRINCIPAL, "version": "v1"}})
    with pytest.raises(ValueError):
        _run(p)


def test_topk_honored_as_is_no_clamp():
    # topK は Builder 束縛の信頼値。アプリ層で丸めず値どおり委譲先へ渡す
    for given in (1, 3, 51, 9999):
        g, seen = _fake_generate("ans", [])
        p = _provider(g, config={"knowledge": {"space": PRINCIPAL}, "retrieval": {"topK": given}})
        _run(p)
        assert seen["top_k"] == given


def test_topk_rejected_for_backend_without_support():
    # select_ai(topK 非対応)を明示選択し topK を渡すと黙殺せず拒否 (実 backend に到達しない)
    p = CoreRagAnswerProvider(CONFIG, backend="select_ai")
    with pytest.raises(ValueError):
        _run(p)


def test_topk_not_passed_when_absent():
    g, seen = _fake_generate("ans", [])
    p = _provider(g, config={"knowledge": {"space": PRINCIPAL}})  # retrieval 無し
    _run(p)
    assert seen["top_k"] is None


# ---- 構築・schema ----

def test_config_validation_rejects_missing_space_at_construction():
    g, _ = _fake_generate("x", [])
    with pytest.raises(ValidationError):
        _provider(g, config={"retrieval": {"topK": 1}})


def test_construction_requires_exactly_one_of_backend_or_generate():
    g = _fake_generate("x", [])[0]
    with pytest.raises(ValueError):
        CoreRagAnswerProvider(CONFIG)                        # neither
    with pytest.raises(ValueError):
        CoreRagAnswerProvider(CONFIG, backend="select_ai", generate=g)  # both
    with pytest.raises(ValueError):
        CoreRagAnswerProvider(CONFIG, backend="nonexistent")
    assert callable(CoreRagAnswerProvider(CONFIG, backend="select_ai")._generate)


def test_non_list_citations_rejected():
    # delegate 契約は (str, list[dict])。壊れた citations 型は Empty として通さない
    for bad in ("notalist", ["notadict"], [123]):
        g, _ = _fake_generate("本文", bad)
        with pytest.raises(RuntimeError):
            _run(_provider(g))


def test_backend_capability_flags():
    assert CoreRagAnswerProvider(CONFIG, backend="opensearch")._supports_topk is True
    assert CoreRagAnswerProvider(CONFIG, backend="select_ai")._supports_topk is False


def test_input_validation_rejects_empty_question():
    g, _ = _fake_generate("x", [])
    with pytest.raises(ValidationError):
        _run(_provider(g), question="")


def test_input_schema_rejects_config_leakage():
    assert not is_valid(
        "answer-with-citations.input", {"question": "q", "knowledge": {"space": "x"}}
    )


# ---- CapabilityProvider seam (§8.1) ----

def test_descriptor_is_rag_answer():
    p = _provider(_fake_generate("x", [])[0])
    assert p.descriptor["id"] == "rag.answer"
    assert p.descriptor["action"] == "answer.with-citations@1"


def test_resume_not_supported():
    p = _provider(_fake_generate("x", [])[0])
    ctx = RunContext(run_id="r", principal=PRINCIPAL, emit=_noop_emit)
    with pytest.raises(NotImplementedError):
        asyncio.run(p.resume(ctx, {}))


def test_cancel_stops_streaming():
    g, _ = _fake_generate("A" * 500, [])  # 複数 delta になる長さ
    p = _provider(g)

    async def scenario():
        collected = []

        async def emit(e):
            collected.append(e)
        ctx = RunContext(run_id="r", principal=PRINCIPAL, emit=emit)
        await p.cancel(ctx)          # 事前に cancel
        handle = await p.start(ctx, {"question": "q"})
        return collected, handle

    events, handle = asyncio.run(scenario())
    assert handle.status == "cancelled"
    assert handle.output is None
    # retrieval まではイベントが出るが message.delta は cancel で打ち切られる
    assert not any(e["type"] == "message.delta" for e in events)


def test_delegate_exception_propagates():
    def boom(owner, prompt, *, top_k=None):
        raise RuntimeError("backend down")
    with pytest.raises(RuntimeError, match="backend down"):
        _run(_provider(boom))


async def _noop_emit(event):
    return None
