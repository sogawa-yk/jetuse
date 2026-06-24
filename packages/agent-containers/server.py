"""共通FastAPIサーバ(ADR-0009)。各SDKランナーが run_fn を渡してアプリを生成する。

契約:
  GET  /health -> {"status":"ok","sdk":...}
  POST /invoke (InvokeRequest) -> InvokeResponse
"""

import inspect

from fastapi import FastAPI

from agent_common import InvokeRequest, InvokeResponse


def create_app(sdk_name, run_fn):
    app = FastAPI(title=f"jetuse-agent-{sdk_name}")

    @app.get("/health")
    async def health():  # noqa: ANN201
        return {"status": "ok", "sdk": sdk_name}

    @app.post("/invoke", response_model=InvokeResponse)
    async def invoke(req: InvokeRequest):  # noqa: ANN201
        res = run_fn(req)
        if inspect.isawaitable(res):
            res = await res
        return res

    return app
