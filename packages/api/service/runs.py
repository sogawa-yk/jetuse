"""Run/RunEvent/Artifact モデル + Run 実行エンジン(EXB-03)。

実装方針 §7.4 の Action/Run API 最小版。Stage 0 の標準 Run イベント語彙
(`run-event.schema.json`)を単一の真実源とし、`rag.answer`(`answer.with-citations@1`)の
縦切り1本だけを stub Provider + seam で実装する。実 RAG 接続は EXB-04 が Provider を差す。

- Run ストアは in-memory(MVP は永続化しない)。`run_id` 採番・`seq` 単調増加・状態遷移。
- Provider seam(§3.5): engine が lifecycle(`run.started`/`run.completed`/`run.failed`)と
  出力組み立て(delta 累積 → answer, retrieval → citations)を担い、Provider は capability 固有の
  イベント列(`retrieval.*`/`message.delta`)を yield するだけ。OCI は直叩きしない。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from jetuse_platform.contracts import (
    validate_action_with_citations_event,
    validate_action_with_citations_output,
    validate_run_event,
)

logger = logging.getLogger("jetuse.service")

# MVP で受理する action(未知は route が 404)。
KNOWN_ACTION = "answer.with-citations@1"

# capability 固有イベント(answer-with-citations.event の enum)。engine が個別に契約検証する対象。
_CAPABILITY_EVENT_TYPES = frozenset({"message.delta", "retrieval.started", "retrieval.completed"})

# lifecycle の終端(engine のみが発行)。SSE の tail はこれを見たら購読を閉じる。
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# in-memory MVP の可用性バックストップ。永続 Run/ワーカー基盤は非ゴール(実装方針 §15)。
_MAX_CONCURRENCY = 8      # executor のワーカースレッド数。
_MAX_IN_FLIGHT = 32       # 未終端 Run(queued+running)の受付上限。超過は 429。キューも有界化。
_MAX_RUNS = 1000          # 保持 Run 数の上限。超過で**終端済み**の最古のみ退避(実行中は消さない)。
# 同時 SSE 購読数の上限。超過は 503。sync SSE は購読ごとに anyio 共有スレッド(既定 ~40)を占有する。
# 通常の同期ルート用に予約枠を残す保守値(最小対応。非同期 SSE 化は非ゴール)。
_MAX_SUBSCRIBERS = 16
_SSE_WAIT_SECONDS = 15    # SSE 待機タイムアウト。keepalive で切断検知/GW アイドル対策(chat 同値)。


class RunCapacityError(Exception):
    """未終端 Run が受付上限に達した(route は 429 に変換)。"""


class SubscriptionCapacityError(Exception):
    """同時 SSE 購読数が上限に達した(route は 503 に変換)。"""


class RunEvent(BaseModel):
    """標準 Run イベント。`run-event.schema.json` に準拠(`_append` で検証)。"""

    run_id: str
    type: str
    seq: int
    ts: str
    data: dict = Field(default_factory=dict)


class Artifact(BaseModel):
    """Run が生成する成果物。MVP では未生成(空配列)。EXB-04 以降で実体化。"""

    id: str
    type: str
    data: dict = Field(default_factory=dict)


class Run(BaseModel):
    run_id: str
    experience_id: str
    action_id: str
    status: str  # queued | running | completed | failed | cancelled
    input: dict


@dataclass(frozen=True)
class RunContext:
    """Provider seam に渡す実行文脈。EXB-04 の実 RAG Adapter が利用者固有の RAG ストア/設定を
    安全に選ぶのに必要な最小情報を運ぶ(owner_sub・experience/action・検証済み input)。
    """

    run_id: str
    owner_sub: str
    experience_id: str
    action_id: str
    input: dict
    # Experience 束縛の capability config。MVP は binding 未実装のため未束縛(None)。
    # EXB(Experience 束縛)側で解決して渡す。ponytail: 今は None、binding 実装時に充填。
    config: dict | None = None


# 状態遷移(queued→running→completed/failed/cancelled)。不正遷移を弾く。
# queued→failed は「実行開始前の失敗」(executor 投入不能など)を許容する。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

# 終端 status と終端イベントの対応(finalize が不整合な組み合わせを弾く単一の真実源)。
_TERMINAL_EVENT_FOR_STATUS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
}


def _ensure_json_serializable(payload: dict) -> None:
    """SSE で json.dumps できる値だけを許す(trust boundary)。

    JSON Schema の additionalProperties は JSON 直列化可能性を保証しない。Provider(EXB-04 seam)が
    NaN/bytes/任意オブジェクトを混ぜても保存前に弾いて run.failed に写像する(でないと保存後に SSE の
    json.dumps が失敗し run.failed/[DONE] すら配信できない)。sse_event と同じ既定で検証。
    """
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


class _Subscription:
    """SSE 購読イテレータ。`close()` は gen を閉じ枠を**冪等に**返す(未開始 close でも解放)。"""

    def __init__(self, gen: Iterator, release) -> None:
        self._gen = gen
        self._release = release

    def __iter__(self) -> _Subscription:
        return self

    def __next__(self):
        return next(self._gen)

    def close(self) -> None:
        self._gen.close()  # 開始済みなら finally が走る。未開始でも下の release で確実に解放。
        self._release()


class _RunRecord:
    def __init__(self, run: Run, owner_sub: str) -> None:
        self.run = run
        self.owner_sub = owner_sub  # 認可スコープ(認証主体)。他主体には run を漏らさない。
        self.events: list[RunEvent] = []
        self.artifacts: list[Artifact] = []
        self._seq = 0
        # SSE の逐次配信用。単一の execute スレッドが append/set_status、複数 reader が待機する。
        self.cond = threading.Condition()
        self.done = False  # terminal(completed/failed/cancelled)到達。tail の終了条件。
        self.holds_permit = True  # 未終端の間 in-flight セマフォ枠を1つ保持(finalize で解放)。


class RunStore:
    """in-process/in-memory な Run ストア(MVP)。永続化しない。

    公開の読取(get/events/artifacts/iter_events)は `owner_sub` でスコープする。run_id を知る別の
    認証主体でも所有者でなければ None を返す(route が 404 に変換=存在を漏らさない)。lifecycle 追記
    (set_status/append)は engine 内部専用で run_id キー直参照(認可済み経路からのみ呼ばれる)。
    実行はバックグラウンドスレッドで進み、append/set_status が Condition で reader を起こす。
    """

    def __init__(
        self,
        max_runs: int = _MAX_RUNS,
        max_in_flight: int = _MAX_IN_FLIGHT,
        max_subscribers: int = _MAX_SUBSCRIBERS,
    ) -> None:
        self._runs: dict[str, _RunRecord] = {}
        self._max_runs = max_runs
        self._lock = threading.Lock()  # create の採番/退避を直列化
        # 未終端 Run の受付上限。実行中(+待機)を有界化し、executor キューの無制限増加も防ぐ。
        self._in_flight = threading.BoundedSemaphore(max_in_flight)
        # 同時 SSE 購読数の上限。iter_events で受理した購読を(本文反復前でも)直ちに予約カウントし、
        # 解放は _Subscription.close()/gen finally/route の __call__ finally で冪等に確実に返す。
        self._max_subscribers = max_subscribers
        self._sub_lock = threading.Lock()
        self._sub_count = 0

    def create(self, experience_id: str, action_id: str, run_input: dict, owner_sub: str) -> Run:
        # 未終端 Run 枠を確保(finalize で解放)。満杯なら受付拒否 → route が 429。
        if not self._in_flight.acquire(blocking=False):
            raise RunCapacityError("too many in-flight runs")
        run_id = uuid.uuid4().hex
        run = Run(
            run_id=run_id,
            experience_id=experience_id,
            action_id=action_id,
            status="queued",
            input=run_input,
        )
        with self._lock:
            self._runs[run_id] = _RunRecord(run, owner_sub)
            # 上限超過で**終端済み**の最古のみ退避。実行中を消すと execute が KeyError になるため。
            # 終端が無ければ退避しない(soft cap。未終端は in_flight セマフォ側で有界)。
            while len(self._runs) > self._max_runs:
                victim = next((rid for rid, r in self._runs.items() if r.done), None)
                if victim is None:
                    break
                del self._runs[victim]
        return run

    def _authorized(self, run_id: str, owner_sub: str) -> _RunRecord | None:
        rec = self._runs.get(run_id)
        if rec is None or rec.owner_sub != owner_sub:
            return None
        return rec

    def get(self, run_id: str, owner_sub: str) -> Run | None:
        rec = self._authorized(run_id, owner_sub)
        return rec.run if rec else None

    def events(self, run_id: str, owner_sub: str) -> list[RunEvent] | None:
        rec = self._authorized(run_id, owner_sub)
        return list(rec.events) if rec else None

    def artifacts(self, run_id: str, owner_sub: str) -> list[Artifact] | None:
        rec = self._authorized(run_id, owner_sub)
        return list(rec.artifacts) if rec else None

    def iter_events(self, run_id: str, owner_sub: str) -> _Subscription | None:
        """SSE 用の逐次イテレータ。既存イベントを再生し、terminal まで新規到着を待って yield する。

        未知/他主体は None(route は 404)。実行完了後の購読は蓄積分の即時再生になる。
        待機は `_SSE_WAIT_SECONDS` で区切り、無イベントなら `None`(keepalive sentinel)を yield し
        → route が keepalive フレームを送る(切断検知/GW アイドル対策。chat の SSE 規約に踏襲)。

        購読枠は**チェックと確保を同一ロックで原子的に**行い(上限を超えて占有しない)、解放は
        `_Subscription.close()` と gen の finally の**冪等**な二経路で行う(未開始 close・正常終了・
        切断・例外のいずれでも一度だけ返す)。
        ponytail: 既存 sync-generator SSE 規約(chat.py)に踏襲。非同期 SSE 化は非ゴール。
        既知の狭め: フレームワークが未開始イテレータを close しない切断は解放が GC 依存になりうる。
        """
        rec = self._authorized(run_id, owner_sub)
        if rec is None:
            return None
        # チェック&確保を原子的に(同一ロック)。超過は 503。
        with self._sub_lock:
            if self._sub_count >= self._max_subscribers:
                raise SubscriptionCapacityError("too many concurrent subscribers")
            self._sub_count += 1

        released = threading.Event()

        def release() -> None:
            # 冪等: close / 正常終了 / 切断 のうち最初の一回だけ枠を返す。
            if not released.is_set():
                released.set()
                with self._sub_lock:
                    self._sub_count -= 1

        def gen() -> Iterator[RunEvent | None]:
            cursor = 0
            try:
                while True:
                    with rec.cond:
                        if cursor >= len(rec.events) and not rec.done:
                            rec.cond.wait(timeout=_SSE_WAIT_SECONDS)
                        pending = rec.events[cursor:]
                        cursor += len(pending)
                        done = rec.done
                    if not pending:
                        if done:
                            return
                        yield None  # keepalive(ロック外で yield)
                        continue
                    for event in pending:
                        yield event
                        if event.type in _TERMINAL_EVENT_TYPES:
                            return
                    if done and cursor >= len(rec.events):
                        return
            finally:
                release()

        return _Subscription(gen(), release)

    def set_status(self, run_id: str, status: str) -> None:
        """非終端遷移(queued→running)専用。終端は finalize()(permit 解放・イベント発行)に集約。"""
        if status in _TERMINAL_STATUSES:
            raise ValueError("terminal transitions must go through finalize()")
        rec = self._runs[run_id]
        with rec.cond:
            current = rec.run.status
            if status not in _TRANSITIONS[current]:
                raise ValueError(f"invalid transition {current!r} -> {status!r}")
            rec.run.status = status
            rec.cond.notify_all()

    def finalize(self, run_id: str, event_type: str, data: dict, status: str) -> RunEvent:
        """終端イベント追記・status 更新・done 設定を**単一ロックで原子的に**確定してから通知する。

        append→set_status を分けると、終端イベントで起きた購読者が status 更新前に running を見る
        競合が生じる。一括確定で「終端イベント受信時 status は必ず対応する終端値」を保証する。
        """
        rec = self._runs[run_id]
        event = RunEvent(
            run_id=run_id,
            type=event_type,
            seq=rec._seq,
            ts=datetime.now(UTC).isoformat(),
            data=data,
        )
        # 終端 status と終端イベントが対応していること(completed↔run.completed 等)を強制する。
        if _TERMINAL_EVENT_FOR_STATUS.get(status) != event_type:
            raise ValueError(f"terminal event {event_type!r} does not match status {status!r}")
        payload = event.model_dump()
        validate_run_event(payload)  # 終端は capability イベントではないので標準検証のみ
        _ensure_json_serializable(payload)  # SSE で確実に配信できる値に限る
        with rec.cond:
            if rec.done:
                raise ValueError("run already terminal")
            if status not in _TRANSITIONS[rec.run.status]:
                raise ValueError(f"invalid transition {rec.run.status!r} -> {status!r}")
            rec.events.append(event)
            rec._seq += 1
            rec.run.status = status
            rec.done = True
            release_permit = rec.holds_permit
            rec.holds_permit = False  # 二重解放防止(BoundedSemaphore は過剰解放で例外)
            rec.cond.notify_all()
        if release_permit:
            self._in_flight.release()  # 未終端枠を返す(ロック外)
        return event

    def append(self, run_id: str, event_type: str, data: dict) -> RunEvent:
        """RunEvent を採番・検証して追記し、SSE reader を起こす。標準語彙 + capability 契約に準拠。

        seq は**検証成功後にのみ**進める。不正イベントで検証が落ちても保存済み seq は連続を保つ。
        検証はロック外(単一 execute スレッドのみが append するため seq は競合しない)。
        """
        rec = self._runs[run_id]
        event = RunEvent(
            run_id=run_id,
            type=event_type,
            seq=rec._seq,
            ts=datetime.now(UTC).isoformat(),
            data=data,
        )
        if event_type in _TERMINAL_EVENT_TYPES:
            # 終端イベントは status/done/permit を伴う finalize() 専用。直接 append を禁じる
            # (でないと SSE だけ終了して status・permit が取り残される)。
            raise ValueError("terminal events must go through finalize()")
        payload = event.model_dump()
        validate_run_event(payload)  # 標準イベント語彙(Stage 0)への準拠
        if event_type in _CAPABILITY_EVENT_TYPES:
            validate_action_with_citations_event({"type": event_type, "data": data})
        _ensure_json_serializable(payload)  # Provider 由来の非 JSON 値を保存前に弾く(SSE 破綻防止)
        with rec.cond:
            if rec.done:
                raise ValueError("cannot append to a terminal run")  # 終端後追記を拒否(不変条件)
            rec.events.append(event)
            rec._seq += 1
            rec.cond.notify_all()
        return event


class RunProvider(Protocol):
    """Run 実行の seam。`RunContext` を受け取り capability 固有イベント(`{type,data}`)を yield。

    engine が lifecycle と出力組み立てを担うので、Provider は検索状況・逐次回答だけを流せばよい。
    ctx は owner_sub/experience/action/input を運ぶので、EXB-04 の実 RAG Adapter は利用者固有の
    RAG ストア/設定を安全に選べる。
    """

    def run(self, ctx: RunContext) -> Iterator[dict]: ...


class StubProvider:
    """既知 question にダミー回答 + 引用を返す stub(実 RAG は EXB-04 が差す)。"""

    def run(self, ctx: RunContext) -> Iterator[dict]:
        question = ctx.input["question"]
        yield {"type": "retrieval.started", "data": {}}
        yield {
            "type": "retrieval.completed",
            "data": {"citations": [{"source": "stub-doc.pdf#p1", "score": 0.9}]},
        }
        chunks = (f"「{question}」への回答(スタブ): ", "EXB-03 の Run/SSE 経路検証用ダミー回答。")
        for chunk in chunks:
            yield {"type": "message.delta", "data": {"text": chunk}}


def execute(store: RunStore, run: Run, provider: RunProvider, owner_sub: str) -> None:
    """Run を実行して RunEvent を store に追記する(route はこれを別スレッドで起動)。

    POST は即座に run_id を返し、実行はバックグラウンドで進む。SSE(iter_events)が append を逐次
    受け取るので、実 RAG が遅延しても POST はブロックせず message.delta も逐次配信される。

    lifecycle イベント(run.started/completed/failed)は **engine のみ**が発行する。Provider が
    yield してよいのは capability 固有イベントだけで、lifecycle/未知イベントや語彙順違反(欠落/逆順/
    重複)は Provider 障害として run.failed に写像する(イベント順序・重複の不変条件を守る)。
    """
    run_id = run.run_id
    ctx = RunContext(
        run_id=run_id,
        owner_sub=owner_sub,
        experience_id=run.experience_id,
        action_id=run.action_id,
        input=run.input,
    )
    answer_parts: list[str] = []
    citations: list[dict] = []
    started = completed = delta = False  # capability イベント順序の状態機械
    try:
        # 開始処理も try 内に置く。set_status/append("run.started") が失敗しても run.failed へ終端し
        # permit を返す(でないと queued/running のまま・SSE 未終了・受付枠リークで残る)。
        store.set_status(run_id, "running")
        store.append(run_id, "run.started", {})
        for event in provider.run(ctx):
            event_type = event["type"]
            if event_type not in _CAPABILITY_EVENT_TYPES:
                raise ValueError(f"provider emitted non-capability event: {event_type!r}")
            # 受け入れ条件の語彙順 retrieval.started→retrieval.completed→message.delta を強制し、
            # 欠落・逆順・重複を Provider 障害として弾く(order 不正は下の except で run.failed)。
            if event_type == "retrieval.started":
                if started:
                    raise ValueError("duplicate retrieval.started")
                started = True
            elif event_type == "retrieval.completed":
                if not started or completed:
                    raise ValueError("retrieval.completed out of order")
                completed = True
            elif event_type == "message.delta":
                if not completed:
                    raise ValueError("message.delta before retrieval.completed")
                delta = True
            data = event.get("data", {})
            store.append(run_id, event_type, data)
            if event_type == "message.delta":
                answer_parts.append(data["text"])
            elif event_type == "retrieval.completed":
                citations = data["citations"]
        if not (completed and delta):
            raise ValueError("incomplete run: retrieval.completed と message.delta が必要")
        output = {"answer": "".join(answer_parts), "citations": citations}
        validate_action_with_citations_output(output)  # 出力契約(引用付き回答)に準拠
        store.finalize(run_id, "run.completed", {"output": output}, "completed")
    except Exception as exc:  # Provider の任意失敗を run.failed に写像する
        # 例外 message/traceback は機密(認証情報・署名 URL・内部 EP)を含みうるため外部にもログにも
        # 出さない。診断は run_id + 例外クラス名のみ(詳細診断は EXB-04 で redaction 済みを出す)。
        logger.error("run %s failed: %s", run_id, type(exc).__name__)
        failed = {"error": "run failed", "code": "provider_error"}
        store.finalize(run_id, "run.failed", failed, "failed")


# ルート横断で共有する in-memory シングルトン(MVP)。テストは Provider を差し替え可能。
_STORE = RunStore()
_PROVIDER: RunProvider = StubProvider()
# 同時実行を上限付きで捌く(POST ごとの無制限スレッド生成を防ぐ)。超過分はキューで待機。
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY, thread_name_prefix="run-exec")


def get_store() -> RunStore:
    return _STORE


def get_provider() -> RunProvider:
    return _PROVIDER


def submit_run(store: RunStore, run: Run, provider: RunProvider, owner_sub: str) -> None:
    """execute を上限付き executor へ投入する。投入不能(executor 停止)は RuntimeError を送出。"""
    _EXECUTOR.submit(execute, store, run, provider, owner_sub)
