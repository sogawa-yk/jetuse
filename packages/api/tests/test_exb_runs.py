"""EXB-03: Action/Run API(rag.answer 限定・SSE)の検証。

Run/RunEvent/Artifact モデル + in-memory Run ストア + stub Provider seam を通した
Run/SSE 経路を、Stage 0 の標準イベント語彙(run-event.schema.json)と capability 契約
(answer-with-citations.*)への準拠込みで検証する。実 RAG 接続は EXB-04(本タスクは stub)。
"""

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from jetuse_platform.contracts import (
    validate_action_with_citations_output,
    validate_run_event,
)
from service.main import app
from service.runs import (
    RunCapacityError,
    RunStore,
    StubProvider,
    SubscriptionCapacityError,
    _Subscription,
    execute,
)

client = TestClient(app)

_EXP = "exp-demo"
_ACTION = "answer.with-citations@1"
_RUNS_BASE = f"/api/v1/experiences/{_EXP}/actions/{_ACTION}/runs"
_OWNER = "user-a"


def _parse_sse(text: str) -> list[dict]:
    """SSE 本文から RunEvent フレームを取り出す([DONE]・keepalive は除外)。"""
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)
            if "type" not in obj:  # keepalive({"ka": 1})は無視
                continue
            events.append(obj)
    return events


# --------------------------------------------------------------- エンジン(単体)
def test_engine_event_sequence_and_seq_monotonic():
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "保証期間は?"}, _OWNER)
    execute(store, run, StubProvider(), _OWNER)

    events = store.events(run.run_id, _OWNER)
    types = [e.type for e in events]
    assert types == [
        "run.started",
        "retrieval.started",
        "retrieval.completed",
        "message.delta",
        "message.delta",
        "run.completed",
    ]
    # seq は 0 起点で単調増加
    assert [e.seq for e in events] == list(range(len(events)))
    # 全イベントが標準語彙スキーマに準拠
    for e in events:
        validate_run_event(e.model_dump())
    # 状態遷移が terminal(completed)
    assert store.get(run.run_id, _OWNER).status == "completed"


def test_engine_output_conforms_to_output_schema():
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "保証期間は?"}, _OWNER)
    execute(store, run, StubProvider(), _OWNER)

    completed = [e for e in store.events(run.run_id, _OWNER) if e.type == "run.completed"][0]
    output = completed.data["output"]
    validate_action_with_citations_output(output)  # 引用付き回答の契約に準拠
    assert output["answer"]
    assert output["citations"] and output["citations"][0]["source"]


def test_engine_emits_run_failed_on_provider_error():
    class _Boom:
        def run(self, _input):
            yield {"type": "retrieval.started", "data": {}}
            raise RuntimeError("provider exploded")

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, _Boom(), _OWNER)

    types = [e.type for e in store.events(run.run_id, _OWNER)]
    assert types[-1] == "run.failed"
    assert store.get(run.run_id, _OWNER).status == "failed"
    for e in store.events(run.run_id, _OWNER):
        validate_run_event(e.model_dump())


def test_engine_run_failed_on_non_json_provider_payload():
    # F-001(review-11): 非 JSON 値(object 等)を含む Provider payload は保存前に弾き run.failed。
    class _BadJson:
        def run(self, ctx):
            yield {"type": "retrieval.started", "data": {}}
            yield {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}}
            yield {"type": "message.delta", "data": {"text": "ok", "bad": object()}}

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, _BadJson(), _OWNER)

    events = store.events(run.run_id, _OWNER)
    assert [e.type for e in events][-1] == "run.failed"
    assert store.get(run.run_id, _OWNER).status == "failed"
    # 蓄積イベント全体が SSE として直列化可能(run.failed/[DONE] を配信できる)。
    for e in events:
        json.dumps(e.model_dump(), ensure_ascii=False, allow_nan=False)


def test_engine_rejects_provider_injected_lifecycle_event():
    # F-002: Provider が lifecycle/未知イベントを注入したら run.failed に写像する。
    class _Injector:
        def run(self, _input):
            yield {"type": "retrieval.started", "data": {}}
            yield {"type": "run.completed", "data": {"output": {"answer": "x", "citations": []}}}

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, _Injector(), _OWNER)

    types = [e.type for e in store.events(run.run_id, _OWNER)]
    # 注入は拒否され run.failed で終端(重複 completed なし)。
    assert types == ["run.started", "retrieval.started", "run.failed"]
    assert store.get(run.run_id, _OWNER).status == "failed"


@pytest.mark.parametrize(
    "events",
    [
        [],  # 空列(検索も回答もない)
        [{"type": "message.delta", "data": {"text": "x"}}],  # retrieval なしで delta
        [  # retrieval.completed が逆順(started 前)
            {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}},
        ],
        [  # retrieval.completed 重複
            {"type": "retrieval.started", "data": {}},
            {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}},
            {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}},
        ],
        [  # message.delta 欠落(回答なし)
            {"type": "retrieval.started", "data": {}},
            {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}},
        ],
    ],
)
def test_engine_enforces_event_order(events):
    # F-001(review-2): 語彙順 retrieval.started→completed→delta の欠落/逆順/重複/空列は run.failed。
    class _P:
        def run(self, _input):
            yield from events

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, _P(), _OWNER)
    assert store.get(run.run_id, _OWNER).status == "failed"
    assert [e.type for e in store.events(run.run_id, _OWNER)][-1] == "run.failed"


def test_engine_run_failed_does_not_leak_exception_detail():
    # F-002(review-2): Provider 例外の詳細(内部 EP 等)は SSE の run.failed に出さない。
    secret = "internal-host.example.com/secret-token-XYZ"

    class _Leaky:
        def run(self, _input):
            raise RuntimeError(secret)
            yield  # pragma: no cover — generator にするだけ

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, _Leaky(), _OWNER)

    failed = [e for e in store.events(run.run_id, _OWNER) if e.type == "run.failed"][0]
    assert secret not in json.dumps(failed.data)
    assert failed.data == {"error": "run failed", "code": "provider_error"}


def test_route_scopes_by_owner_across_users():
    # F-003(review-2): user-a の Run は user-b からは state/events/artifacts すべて 404。
    from jetuse_core.auth import AuthContext, require_user

    def _as(subject):
        return lambda: AuthContext(subject=subject)

    app.dependency_overrides[require_user] = _as("user-a")
    try:
        run_id = client.post(_RUNS_BASE, json={"question": "q"}).json()["run_id"]
        # 所有者(user-a)は取得可能
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200

        app.dependency_overrides[require_user] = _as("user-b")
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404
        assert client.get(f"/api/v1/runs/{run_id}/events").status_code == 404
        assert client.get(f"/api/v1/runs/{run_id}/artifacts").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_store_seq_contiguous_after_invalid_capability_payload():
    # F-003: 不正 capability payload で検証が落ちても seq は連続(欠番なし)。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.append(run.run_id, "run.started", {})
    # retrieval.completed は citations 必須。欠くと capability 検証で ValidationError。
    with pytest.raises(ValidationError):
        store.append(run.run_id, "retrieval.completed", {})
    store.append(run.run_id, "message.delta", {"text": "ok"})

    seqs = [e.seq for e in store.events(run.run_id, _OWNER)]
    assert seqs == [0, 1]  # 失敗イベントは番号を消費していない


def test_store_scopes_by_owner():
    # F-001: 別主体は所有者でなければ get/events/artifacts が None(route は 404)。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, StubProvider(), _OWNER)

    assert store.get(run.run_id, "other-user") is None
    assert store.events(run.run_id, "other-user") is None
    assert store.artifacts(run.run_id, "other-user") is None
    # 所有者は取得できる
    assert store.get(run.run_id, _OWNER) is not None


def test_store_unknown_run_returns_none():
    store = RunStore()
    assert store.get("nope", _OWNER) is None
    assert store.events("nope", _OWNER) is None
    assert store.artifacts("nope", _OWNER) is None


def test_finalize_sets_terminal_status_with_event_atomically():
    # F-001(review-4): 終端イベントと終端 status を同一ロックで確定(受信時 running のまま等が無い)。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.set_status(run.run_id, "running")
    output = {"output": {"answer": "a", "citations": []}}
    store.finalize(run.run_id, "run.completed", output, "completed")

    events = store.events(run.run_id, _OWNER)
    assert events[-1].type == "run.completed"
    assert store.get(run.run_id, _OWNER).status == "completed"
    # iter_events を drain し切った直後に status が終端であること(競合が無いこと)。
    last = None
    for ev in store.iter_events(run.run_id, _OWNER):
        last = ev
    assert last.type == "run.completed"
    assert store.get(run.run_id, _OWNER).status == "completed"


def test_store_caps_retained_terminal_runs_only():
    # F-002/F-001(review-4/5): 保持上限超過は**終端済み**の最古のみ退避。実行中は消さない。
    store = RunStore(max_runs=2)
    r1 = store.create(_EXP, _ACTION, {"question": "1"}, _OWNER)
    execute(store, r1, StubProvider(), _OWNER)  # r1 を終端させる
    r2 = store.create(_EXP, _ACTION, {"question": "2"}, _OWNER)
    r3 = store.create(_EXP, _ACTION, {"question": "3"}, _OWNER)

    assert store.get(r1.run_id, _OWNER) is None  # 終端済みの最古が退避された
    assert store.get(r2.run_id, _OWNER) is not None
    assert store.get(r3.run_id, _OWNER) is not None


def test_store_does_not_evict_running_run():
    # F-001(review-5): 未終端(running)しか無い状態で上限超過しても実行中は消さない(KeyError 回避)。
    store = RunStore(max_runs=1, max_in_flight=8)
    r1 = store.create(_EXP, _ACTION, {"question": "1"}, _OWNER)
    store.set_status(r1.run_id, "running")
    r2 = store.create(_EXP, _ACTION, {"question": "2"}, _OWNER)  # 上限超過だが r1 は running

    assert store.get(r1.run_id, _OWNER) is not None  # 実行中は退避されない
    assert store.get(r2.run_id, _OWNER) is not None
    # r1 を終端まで進めても KeyError にならない
    done_output = {"output": {"answer": "a", "citations": []}}
    store.finalize(r1.run_id, "run.completed", done_output, "completed")
    assert store.get(r1.run_id, _OWNER).status == "completed"


def test_store_rejects_when_in_flight_full():
    # F-002(review-5): 未終端 Run が受付上限に達したら RunCapacityError(route は 429)。
    store = RunStore(max_in_flight=2)
    store.create(_EXP, _ACTION, {"question": "1"}, _OWNER)
    store.create(_EXP, _ACTION, {"question": "2"}, _OWNER)
    with pytest.raises(RunCapacityError):
        store.create(_EXP, _ACTION, {"question": "3"}, _OWNER)


def test_store_releases_in_flight_permit_on_finalize():
    # 終端で枠が返り、再び受付可能になる。
    store = RunStore(max_in_flight=1)
    r1 = store.create(_EXP, _ACTION, {"question": "1"}, _OWNER)
    execute(store, r1, StubProvider(), _OWNER)  # 終端 → 枠解放
    r2 = store.create(_EXP, _ACTION, {"question": "2"}, _OWNER)  # 再度受付可能
    assert store.get(r2.run_id, _OWNER) is not None


def test_set_status_rejects_terminal_transition():
    # F-002(review-6): set_status は非終端専用。終端は finalize に集約(permit 解放漏れを防ぐ)。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.set_status(run.run_id, "running")
    for terminal in ("completed", "failed", "cancelled"):
        with pytest.raises(ValueError):
            store.set_status(run.run_id, terminal)


def test_finalize_cancelled_releases_permit_and_emits_event():
    # F-002(review-6): 終端(cancelled)を finalize すれば枠が返り、終端イベントも配信される。
    store = RunStore(max_in_flight=1)
    r1 = store.create(_EXP, _ACTION, {"question": "1"}, _OWNER)
    store.set_status(r1.run_id, "running")
    store.finalize(r1.run_id, "run.cancelled", {}, "cancelled")

    assert store.get(r1.run_id, _OWNER).status == "cancelled"
    assert [e.type for e in store.events(r1.run_id, _OWNER)][-1] == "run.cancelled"
    r2 = store.create(_EXP, _ACTION, {"question": "2"}, _OWNER)  # 枠が返り受付可能
    assert store.get(r2.run_id, _OWNER) is not None


def test_run_failed_does_not_leak_secret_to_logs(caplog):
    # F-003(review-6): Provider 例外の message/traceback はログにも出さない(型名のみ)。
    import logging

    secret = "cred-ABC-987654-internal.example.com"

    class _Leaky:
        def run(self, ctx):
            raise RuntimeError(secret)
            yield  # pragma: no cover — generator 化

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    with caplog.at_level(logging.ERROR, logger="jetuse.service"):
        execute(store, run, _Leaky(), _OWNER)

    assert secret not in caplog.text  # ログにも秘密が出ない
    failed = [e for e in store.events(run.run_id, _OWNER) if e.type == "run.failed"][0]
    assert secret not in json.dumps(failed.data)  # SSE にも出ない


def test_store_bounds_concurrent_subscribers():
    # F-001(review-6): 同時 SSE 購読数を上限で有界化(sync SSE の共有スレッド枯渇を防ぐ)。
    store = RunStore(max_subscribers=1)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.append(run.run_id, "run.started", {})  # 非終端の 1 イベント

    g1 = store.iter_events(run.run_id, _OWNER)
    assert next(g1).type == "run.started"
    with pytest.raises(SubscriptionCapacityError):
        store.iter_events(run.run_id, _OWNER)  # 枠満杯 → 拒否

    g1.close()  # 枠解放(冪等)
    g2 = store.iter_events(run.run_id, _OWNER)  # 再度受付可能
    assert next(g2).type == "run.started"
    g2.close()


def test_store_subscriber_cap_atomic_and_close_releases_unstarted():
    # F-001(review-8): チェック&確保を原子的に(開始前でも予約=上限超過不可)。未開始 close でも解放。
    store = RunStore(max_subscribers=2)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)

    s1 = store.iter_events(run.run_id, _OWNER)  # 予約1(未開始)
    s2 = store.iter_events(run.run_id, _OWNER)  # 予約2(未開始)
    with pytest.raises(SubscriptionCapacityError):
        store.iter_events(run.run_id, _OWNER)  # 3件目は開始前でも拒否(原子的予約)

    s1.close()  # 未開始でも枠を返す
    s3 = store.iter_events(run.run_id, _OWNER)  # 空いたので受付可能
    s2.close()
    s3.close()


def test_subscription_close_releases_even_if_gen_close_raises():
    # EXB03-003(review-12): _gen.close() が例外を投げても release が必ず1回走る(枠リーク防止)。
    calls = []

    class _RaisingGen:
        def close(self):
            raise RuntimeError("gen close boom")

    sub = _Subscription(_RaisingGen(), lambda: calls.append(1))
    with pytest.raises(RuntimeError):
        sub.close()
    assert calls == [1]  # 例外が伝播しても release は実行済み


def test_subscription_double_close_releases_once():
    # EXB03-003(review-12): close 二重呼び/gen finally 重複でも枠は1回だけ返す(二重減算しない)。
    store = RunStore(max_subscribers=2)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)

    s = store.iter_events(run.run_id, _OWNER)  # 予約 → count=1
    s.close()
    s.close()  # 冪等: count は 0 のまま(-1 にしない)

    # count が正確に 0 なら、cap=2 の 3件目は必ず拒否される(二重減算していれば通ってしまう)。
    a = store.iter_events(run.run_id, _OWNER)
    b = store.iter_events(run.run_id, _OWNER)
    with pytest.raises(SubscriptionCapacityError):
        store.iter_events(run.run_id, _OWNER)
    a.close()
    b.close()


def test_append_rejects_terminal_event_types():
    # F-001(review-10): 終端イベントの直接 append を拒否(status/permit 取り残しを防ぐ)。
    store = RunStore(max_in_flight=1)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.set_status(run.run_id, "running")

    for terminal in ("run.completed", "run.failed", "run.cancelled"):
        with pytest.raises(ValueError):
            store.append(run.run_id, terminal, {})

    assert store.get(run.run_id, _OWNER).status == "running"  # status 変化なし
    with pytest.raises(RunCapacityError):  # permit も取り残されていない(枠は保持継続)
        store.create(_EXP, _ACTION, {"question": "y"}, _OWNER)


def test_execute_failure_during_start_terminates_and_releases(monkeypatch):
    # F-002(review-10): 開始処理(run.started 追記)失敗でも run.failed へ終端し permit を返す。
    store = RunStore(max_in_flight=1)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)

    orig_append = store.append

    def boom(run_id, event_type, data):
        if event_type == "run.started":
            raise RuntimeError("store down at start")
        return orig_append(run_id, event_type, data)

    monkeypatch.setattr(store, "append", boom)
    execute(store, run, StubProvider(), _OWNER)

    assert store.get(run.run_id, _OWNER).status == "failed"  # 非終端で残らない
    r2 = store.create(_EXP, _ACTION, {"question": "y"}, _OWNER)  # permit が返り再受付可能
    assert store.get(r2.run_id, _OWNER) is not None


def test_finalize_rejects_mismatched_event_status():
    # F-002(review-8): 終端 status と終端イベントの不整合(completed↔failed 等)を弾く。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    store.set_status(run.run_id, "running")
    output = {"output": {"answer": "a", "citations": []}}
    with pytest.raises(ValueError):
        store.finalize(run.run_id, "run.completed", output, "failed")


def test_append_and_finalize_rejected_after_terminal():
    # F-002(review-8): 終端後の追記・二重終端を拒否(終端不変条件)。
    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    execute(store, run, StubProvider(), _OWNER)  # 終端(completed)

    with pytest.raises(ValueError):
        store.append(run.run_id, "message.delta", {"text": "late"})
    with pytest.raises(ValueError):
        store.finalize(
            run.run_id, "run.completed", {"output": {"answer": "a", "citations": []}}, "completed"
        )


def test_route_start_run_429_when_in_flight_full(monkeypatch):
    # F-003(review-7): 受付上限到達時に POST が 429 を返す(ルート結合)。
    from service.routes import runs as runs_route

    full = RunStore(max_in_flight=1)
    full.create(_EXP, _ACTION, {"question": "held"}, "someone")  # 唯一の枠を埋める(未終端)
    monkeypatch.setattr(runs_route, "get_store", lambda: full)

    res = client.post(_RUNS_BASE, json={"question": "q"})
    assert res.status_code == 429
    assert res.json()["detail"] == "too many concurrent runs"


def test_closing_response_releases_subscription_on_prebody_failure():
    # F-001(review-9): 本文反復の開始前に送信が失敗しても購読枠が解放される(恒久リークしない)。
    import asyncio

    from service.routes.runs import _ClosingStreamingResponse

    store = RunStore(max_subscribers=1)
    run = store.create(_EXP, _ACTION, {"question": "x"}, _OWNER)
    sub = store.iter_events(run.run_id, _OWNER)  # 唯一の購読枠を予約(未開始)

    def body():
        for ev in sub:  # 送信失敗により実際には反復されない
            yield f"data: {ev.seq}\n\n"

    resp = _ClosingStreamingResponse(sub, body(), media_type="text/event-stream")

    async def failing_send(_msg):
        raise RuntimeError("client gone before first body frame")

    async def receive():
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "GET", "headers": []}
    with pytest.raises(RuntimeError):
        asyncio.run(resp(scope, receive, failing_send))

    # 枠が返り、次の購読が受付可能(503 で恒久ロックしない)。
    again = store.iter_events(run.run_id, _OWNER)
    assert again is not None
    again.close()


def test_route_events_503_when_subscribers_full(monkeypatch):
    # F-001(review-6): 購読上限超過は 503(unknown=404 と区別)。
    from service.routes import runs as runs_route

    run_id = client.post(_RUNS_BASE, json={"question": "q"}).json()["run_id"]

    class _FullStore:
        def iter_events(self, *_a):
            raise SubscriptionCapacityError("full")

    monkeypatch.setattr(runs_route, "get_store", lambda: _FullStore())
    assert client.get(f"/api/v1/runs/{run_id}/events").status_code == 503


def test_route_returns_503_when_executor_unavailable(monkeypatch):
    # F-002(review-4): executor 投入不能時は queued 放置(500)でなく 503 を返す。
    from service.routes import runs as runs_route

    def _boom(*_a, **_k):
        raise RuntimeError("executor down")

    monkeypatch.setattr(runs_route, "submit_run", _boom)
    res = client.post(_RUNS_BASE, json={"question": "q"})
    assert res.status_code == 503


def test_engine_passes_run_context_to_provider():
    # F-002(review-3): Provider は owner_sub/experience/action/input を持つ RunContext を受け取る。
    captured = {}

    class _Capture:
        def run(self, ctx):
            captured["ctx"] = ctx
            yield {"type": "retrieval.started", "data": {}}
            yield {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}}
            yield {"type": "message.delta", "data": {"text": "ok"}}

    store = RunStore()
    run = store.create("exp-42", _ACTION, {"question": "hi"}, "user-z")
    execute(store, run, _Capture(), "user-z")

    ctx = captured["ctx"]
    assert ctx.owner_sub == "user-z"
    assert ctx.experience_id == "exp-42"
    assert ctx.action_id == _ACTION
    assert ctx.input == {"question": "hi"}


# --------------------------------------------------------------- ルート(結合)
def test_route_start_run_then_get_state():
    res = client.post(_RUNS_BASE, json={"question": "保証期間を教えてください"})
    assert res.status_code == 202, res.text  # Accepted: バックグラウンド実行
    run_id = res.json()["run_id"]
    assert run_id

    # SSE を購読し切ると terminal まで進む。その後 state は completed。
    client.get(f"/api/v1/runs/{run_id}/events")
    state = client.get(f"/api/v1/runs/{run_id}")
    assert state.status_code == 200
    assert state.json()["status"] == "completed"
    assert state.json()["action_id"] == _ACTION


def test_route_sse_event_order_and_schema():
    run_id = client.post(_RUNS_BASE, json={"question": "保証期間は?"}).json()["run_id"]
    res = client.get(f"/api/v1/runs/{run_id}/events")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(res.text)
    types = [e["type"] for e in events]
    assert types[0] == "run.started"
    assert "retrieval.started" in types
    assert "retrieval.completed" in types
    assert "message.delta" in types
    assert types[-1] == "run.completed"
    # 標準語彙スキーマ + seq 単調増加
    assert [e["seq"] for e in events] == list(range(len(events)))
    for e in events:
        validate_run_event(e)
    # retrieval.completed が引用を載せている
    rc = [e for e in events if e["type"] == "retrieval.completed"][0]
    assert rc["data"]["citations"][0]["source"]


def test_route_streams_live_without_blocking_post(monkeypatch):
    # F-001(review-3): POST は Provider 完了前に返り、message.delta は実行中に逐次配信される。
    import threading as _t

    from service import runs as runs_mod

    gate = _t.Event()

    class _Slow:
        def run(self, ctx):
            yield {"type": "retrieval.started", "data": {}}
            yield {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}}
            assert gate.wait(5), "gate not released"  # POST 返却後に解放されるまで delta を出さない
            yield {"type": "message.delta", "data": {"text": "late"}}

    monkeypatch.setattr(runs_mod, "_PROVIDER", _Slow())

    res = client.post(_RUNS_BASE, json={"question": "q"})
    assert res.status_code == 202
    run_id = res.json()["run_id"]
    # POST は Provider 完了前に返っている(まだ completed ではない)。
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] in ("queued", "running")

    gate.set()  # Provider を解放
    events = _parse_sse(client.get(f"/api/v1/runs/{run_id}/events").text)
    types = [e["type"] for e in events]
    assert types[-1] == "run.completed"
    assert any(e["type"] == "message.delta" and e["data"]["text"] == "late" for e in events)


def test_iter_events_yields_incrementally():
    # F-004(review-5): iter_events(route と同じ生成器)は delta を生成器レベルで逐次 yield する。
    import threading

    from service import runs as runs_mod

    g1 = threading.Event()
    g2 = threading.Event()

    class _Steps:
        def run(self, ctx):
            yield {"type": "retrieval.started", "data": {}}
            yield {"type": "retrieval.completed", "data": {"citations": [{"source": "s"}]}}
            assert g1.wait(5), "g1"
            yield {"type": "message.delta", "data": {"text": "d1"}}
            assert g2.wait(5), "g2"
            yield {"type": "message.delta", "data": {"text": "d2"}}

    store = RunStore()
    run = store.create(_EXP, _ACTION, {"question": "q"}, _OWNER)
    threading.Thread(
        target=runs_mod.execute, args=(store, run, _Steps(), _OWNER), daemon=True
    ).start()

    observed = []
    for ev in store.iter_events(run.run_id, _OWNER):
        if ev is None:  # keepalive
            continue
        observed.append(ev.type)
        if ev.type == "retrieval.completed":
            assert "message.delta" not in observed  # delta はまだ来ていない(逐次の証拠)
            g1.set()
        elif ev.type == "message.delta" and ev.data["text"] == "d1":
            g2.set()

    assert observed[-1] == "run.completed"
    assert observed.count("message.delta") == 2


def test_route_artifacts_empty_list_mvp():
    run_id = client.post(_RUNS_BASE, json={"question": "q"}).json()["run_id"]
    res = client.get(f"/api/v1/runs/{run_id}/artifacts")
    assert res.status_code == 200
    assert res.json() == {"artifacts": []}


def test_route_unknown_action_404():
    base = f"/api/v1/experiences/{_EXP}/actions/nope.action@9/runs"
    res = client.post(base, json={"question": "q"})
    assert res.status_code == 404


def test_route_invalid_input_422():
    # question 欠落 / 空文字 / 未知フィールド は input スキーマ違反
    assert client.post(_RUNS_BASE, json={}).status_code == 422
    assert client.post(_RUNS_BASE, json={"question": ""}).status_code == 422
    assert client.post(_RUNS_BASE, json={"question": "q", "x": 1}).status_code == 422


def test_route_unknown_run_404():
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404
    assert client.get("/api/v1/runs/does-not-exist/events").status_code == 404
    assert client.get("/api/v1/runs/does-not-exist/artifacts").status_code == 404


@pytest.mark.parametrize("path", ["", "/events", "/artifacts"])
def test_route_requires_auth_when_enabled(monkeypatch, path):
    from jetuse_core.settings import get_settings

    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        assert client.get(f"/api/v1/runs/some-id{path}").status_code == 401
    finally:
        get_settings.cache_clear()
