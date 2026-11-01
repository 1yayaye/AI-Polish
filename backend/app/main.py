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
from app import seeds as app_seeds
from app import word_jobs as app_word_jobs
from app.admin import router as admin_router
from app.api import router
from app.export import router as export_router
from app.llm import PolishLLMClient
from app.optimization import router as optimization_router
from app.stream import router as stream_router
from app.word_jobs import router as word_jobs_router
from app.word_stream import router as word_stream_router
from app.word_worker import WordJobWorker
from app.worker import PipelineWorker


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动恢复协议与常驻 Worker（润色流水线 + Word Formatter 任务队列）。
    关停时取消全部后台任务。

    Word Formatter 任务（app.word_jobs）不依赖模型接入：启动恢复（遗留
    running 任务复位续跑或超限失败释放）与任务 Worker 无条件挂载。

    润色流水线（模型接入三项齐备才启动，ADR-0005；缺项跳过等于本地开发/
    纯计费部署没有流水线，僵尸恢复随之不挂——没有流水线就没有运行中的
    会话）。启动序（T5 僵尸恢复协议，app.recovery）：先跑启动恢复（扫描
    上一进程遗留的 running 僵尸：未超续跑上限的复位 queued，超限的兜底
    退款；queued 不碰，重启期间新提交的任务由新 Worker 正常拉取），再启动
    Worker 与巡检看门狗协程。流水线未配置时清点库内 queued 会话并告警
    （不启动相关后台任务）——运营商清掉 LLM 配置重启后，queued 会话的预扣
    资金被无限期扣住，该盲区由 app.recovery.report_orphan_queued 的日志
    显性化。
    """
    background_tasks: list[asyncio.Task[None]] = []
    # Word Formatter 任务地基（spec #31）：格式化任务不依赖模型接入，启动
    # 恢复（遗留 running 复位续跑 / 超限失败释放）与任务 Worker 无条件挂载
    # ——与润色流水线"未配置即整体不启动"不同，这里没有可缺项的依赖。
    app_word_jobs.recover_interrupted(app_db.SessionLocal)
    background_tasks.append(
        asyncio.create_task(
            WordJobWorker(app_db.SessionLocal).run_forever(), name="word-job-worker"
        )
    )
    # 方案 A（issue #23/#30）：库内 is_system 行是系统提示词唯一事实源，空库
    # 在首次启动时注入种子（幂等：按 (stage, name) 判重、不覆盖已改动行）——
    # 运营商调优资亷新部署即可用，提示词回落链在空库上也完整，无需手工跑
    # 注入命令。失败即启动期硬错误（库不可用，Worker 也无从工作）。
    with app_db.SessionLocal() as session:
        app_seeds.seed_system_prompts(session)
        session.commit()
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
    else:
        app_recovery.report_orphan_queued(app_db.SessionLocal)
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
app.include_router(word_jobs_router)
app.include_router(word_stream_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
