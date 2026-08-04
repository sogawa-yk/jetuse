"""SPIKE-02: SSEバッファリング/タイムアウト計測用の最小FastAPI。

  /health           : ヘルスチェック
  /drip?seconds=60  : 1秒ごとにサーバー時刻入りSSEイベントを流す
                      （クライアント側で到着遅延を測ればバッファリング有無が分かる）
  /burst?count=20   : 0.2秒間隔の短いイベント列（細粒度のフラッシュ確認用）
"""
import asyncio
import json
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.get("/drip")
async def drip(seconds: int = 60):
    async def gen():
        start = time.time()
        for i in range(seconds):
            yield sse({"i": i, "server_time": time.time(), "elapsed": round(time.time() - start, 2)})
            await asyncio.sleep(1)
        yield sse({"done": True, "total": round(time.time() - start, 2)})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/burst")
async def burst(count: int = 20):
    async def gen():
        for i in range(count):
            yield sse({"i": i, "server_time": time.time()})
            await asyncio.sleep(0.2)
        yield sse({"done": True})
    return StreamingResponse(gen(), media_type="text/event-stream")
