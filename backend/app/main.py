"""应用入口：FastAPI 实例与路由装配。

路由约定：业务 API 全部挂在 /api 前缀下（Nginx 按此反代）；/health 是
进程级存活探针，供 systemd 与 Nginx 健康检查直接访问，不参与业务。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db as app_db
from app import llm as app_llm
from app.admin import router as admin_router
from app.api import router
from app.llm import PolishLLMClient
from app.optimization import router as optimization_router
from app.stream import router as stream_router
from app.worker import PipelineWorker


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动单润色 Worker（模型接入三项齐备才启动，ADR-0005；缺项跳过等于
    本地开发/纯计费部署没有流水线）。关停时取消 Worker 任务。

    这同时是 T5 僵尸恢复协议的挂载点：恢复器（扫描 queued/running 会话、
    断点续跑与兜底退款）应在 Worker 任务启动之前于本 lifespan 内执行。
    """
    worker_task: asyncio.Task[None] | None = None
    if app_llm.is_configured():
        worker = PipelineWorker(app_db.SessionLocal, PolishLLMClient.from_settings())
        worker_task = asyncio.create_task(worker.run_forever(), name="polish-worker")
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


app = FastAPI(
    title="AI-Polish",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(admin_router)
app.include_router(optimization_router)
app.include_router(stream_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
