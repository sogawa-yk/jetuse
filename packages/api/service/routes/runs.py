"""Action/Run API ルート(EXB-03)。catalog.py の規約に合わせる(`require_user` 必須)。

`answer.with-citations@1`(rag.answer)の縦切り1本のみ。Run を開始し、標準 Run イベント語彙を
SSE で `seq` 順に配信する。実 RAG 接続は EXB-04(本タスクは stub Provider)。実装方針 §7.4。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from jsonschema import ValidationError

from jetuse_core.auth import AuthContext, require_user
from jetuse_platform.contracts import validate_action_with_citations_input

from ..runs import (
    KNOWN_ACTION,
    RunCapacityError,
    SubscriptionCapacityError,
    get_provider,
    get_store,
    submit_run,
)
from ..sse import DONE_FRAME, KEEPALIVE_FRAME, SSE_HEADERS, SSE_MEDIA_TYPE, sse_event

router = APIRouter()


class _ClosingStreamingResponse(StreamingResponse):
    """購読を確実に close する StreamingResponse。

    本文の反復が始まる前(ヘッダ送信失敗・即時切断)でも購読枠を返せるよう、`__call__` 全体を
    try/finally で包んで `subscription.close()`(冪等)を保証する。generator の finally だけでは
    本文未反復の経路で解放されないため。
    """

    def __init__(self, subscription, *args, **kwargs) -> None:
        self._subscription = subscription
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._subscription.close()


@router.post(
    "/api/v1/experiences/{experience_id}/actions/{action_id}/runs",
    status_code=202,  # Accepted: 実行はバックグラウンド、進捗は /events(SSE)で購読
)
def start_run(
    experience_id: str,
    action_id: str,
    body: dict,
    user: Annotated[AuthContext, Depends(require_user)],
):
    if action_id != KNOWN_ACTION:
        raise HTTPException(status_code=404, detail=f"unknown action: {action_id}")
    try:
        validate_action_with_citations_input(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"invalid input: {e.message}") from e

    store = get_store()
    # 未終端 Run が受付上限なら 429(実行中・待機を有界化し資源枯渇を防ぐ)。
    try:
        run = store.create(experience_id, action_id, body, user.subject)
    except RunCapacityError as e:
        raise HTTPException(status_code=429, detail="too many concurrent runs") from e
    # 実行は上限付き executor で進め、POST は即座に返す(遅延 Provider でもブロックしない)。
    # 投入不能(executor 停止)なら Run を failed に終端して 503(queued 放置=500 を防ぐ)。
    try:
        submit_run(store, run, get_provider(), user.subject)
    except RuntimeError as e:
        failed = {"error": "run failed", "code": "unavailable"}
        store.finalize(run.run_id, "run.failed", failed, "failed")
        raise HTTPException(status_code=503, detail="run executor unavailable") from e
    return {"run_id": run.run_id, "status": store.get(run.run_id, user.subject).status}


@router.get("/api/v1/runs/{run_id}")
def get_run(run_id: str, user: Annotated[AuthContext, Depends(require_user)]):
    run = get_store().get(run_id, user.subject)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run.model_dump()


@router.get("/api/v1/runs/{run_id}/events")
def get_run_events(run_id: str, user: Annotated[AuthContext, Depends(require_user)]):
    try:
        stream = get_store().iter_events(run_id, user.subject)
    except SubscriptionCapacityError as e:
        raise HTTPException(status_code=503, detail="too many concurrent subscribers") from e
    if stream is None:
        raise HTTPException(status_code=404, detail="run not found")

    def gen():
        for event in stream:  # terminal イベントまで逐次(実行中なら新規到着を待って yield)
            if event is None:
                yield KEEPALIVE_FRAME  # 待機中の keepalive(切断検知/GW アイドル対策)
            else:
                yield sse_event(event.model_dump())
        yield DONE_FRAME

    # __call__ 全体で close を保証(本文未反復の切断でも購読枠を確実に返す)。
    return _ClosingStreamingResponse(
        stream, gen(), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.get("/api/v1/runs/{run_id}/artifacts")
def get_run_artifacts(run_id: str, user: Annotated[AuthContext, Depends(require_user)]):
    artifacts = get_store().artifacts(run_id, user.subject)
    if artifacts is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"artifacts": [a.model_dump() for a in artifacts]}
