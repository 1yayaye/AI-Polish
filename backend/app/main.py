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
from app import recovery as app_recovery
from app.admin import router as admin_router
from app.api import router
from app.export import router as export_router
from app.llm import PolishLLMClient
from app.optimization import router as optimization_router
from app.stream import router as stream_router
from app.worker import PipelineWorker


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动僵尸恢复协议与单润色 Worker（模型接入三项齐备才启动，ADR-0005；
    缺项跳过等于本地开发/纯计费部署没有流水线，僵尸恢复随之不挂——没有
    流水线就没有运行中的会话）。关停时取消全部后台任务。

    启动序（T5 僵尸恢复协议，app.recovery）：先跑启动恢复（扫描上一进程
    遗留的 running 僵尸：未超续跑上限的复位 queued，超限的兜底退款；queued
    不碰，重启期间新提交的任务由新 Worker 正常拉取），再启动 Worker 与巡检
    看门狗协程。
    """
    background_tasks: list[asyncio.Task[None]] = []
    if app_llm.is_configured():
        app_recovery.recover_interrupted(app_db.SessionLocal)
        worker = PipelineWorker(app_db.SessionLocal, PolishLLMClient.from_settings())
        background_tasks.append(
            asyncio.create_task(worker.run_forever(), name="polish-worker")
        )
        background_tasks.append(
            asyncio.create_task(
                app_recovery.patrol_forever(app_db.SessionLocal), name="zombie-patrol"
            )
        )
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)


app = FastAPI(
    title="AI-Polish",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(admin_router)
app.include_router(optimization_router)
app.include_router(export_router)
app.include_router(stream_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
