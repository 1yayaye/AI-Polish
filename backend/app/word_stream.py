"""Word Formatter 任务进度推送（issue #34）：任务进度流端点与事件协议的唯一拥有者。

外部契约（GET /api/word-formatter/jobs/{id}/stream 与
GET /api/word-formatter/preprocess/{id}/stream——预处理页（issue #35）用
后者，同一协议同一实现）：

- **认证**：卡密只走 ``X-Card-Key`` 请求头（复用 :func:`app.auth.get_current_user`），
  缺失/无效 401；本人任务之外 404（不泄露他人任务存在性）。卡密绝不进 URL——
  浏览器 EventSource 不能带 header，前端一律用 @microsoft/fetch-event-source。
- **事件序**：连接即推 ``snapshot``（当前任务状态全量）→ 逐次 ``progress`` 增量
  → 终态 ``completed``/``failed`` 后关流。progress 事件带单调递增 id。
- **断线续传**：客户端重连带 ``Last-Event-ID``，服务端跳过 id ≤ 该值的增量；
  断线期间的缺口状态由每次连接必发的 snapshot 补齐。不丢不重由"id 从数据库
  状态确定性推导"保证——服务端不保存任何流状态，天然可重启。

实现纪律（与润色进度流 app/stream.py 同一模式，独立成模块因为二者的领域
状态与事件推导各归各的票/所有者）：

- 任务表状态是唯一事实源（ADR-0003）：本模块不缓存业务状态，事件全部由
  数据库短事务现读现推导；内存只做"状态提交了就叫醒订阅者"的通知
  （:class:`_EventBus`），通知本身不携带数据，唤醒后一律重读数据库。
- 流可能存活整个任务时长：路由内完成认证/鉴权/首读后立即提交释放读事务，
  生成器内一切读库都另起独立短连接（app.db 短事务纪律）。
- 编译慢调用期间本流零阻塞：生成器只 await 通知队列（超时即发心跳注释行），
  Worker 在状态/进度短事务提交后经 :func:`notify` 叫醒——这是 Worker 与本
  模块的唯一耦合点。
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import db as app_db
from app import word_jobs
from app.auth import get_current_user
from app.db import get_session
from app.models import FormatJob, User

router = APIRouter(prefix="/api/word-formatter")

# 心跳间隔（秒）：注释行保活，穿过 Nginx/代理的空闲超时；也是不经 Worker 的
# 状态变更被流感知的最大延迟。测试注入更小值驱动。
_HEARTBEAT_SECONDS = 15.0

_TERMINAL_STATUSES = frozenset({word_jobs.JOB_COMPLETED, word_jobs.JOB_FAILED})


# ---- 通知总线：内存只做叫醒，不做状态（ADR-0003） ----


class _EventBus:
    """每任务一组订阅队列；notify 只投递唤醒信号（None），不携带任何业务数据。"""

    def __init__(self) -> None:
        self._queues: dict[int, set[asyncio.Queue[None]]] = defaultdict(set)

    def subscribe(self, job_id: int) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue()
        self._queues[job_id].add(queue)
        return queue

    def unsubscribe(self, job_id: int, queue: asyncio.Queue[None]) -> None:
        self._queues[job_id].discard(queue)
        if not self._queues[job_id]:
            del self._queues[job_id]

    def notify(self, job_id: int) -> None:
        for queue in self._queues.get(job_id, ()):
            queue.put_nowait(None)


_bus = _EventBus()


def notify(job_id: int) -> None:
    """状态写入方（Worker 等）在短事务提交后调用：叫醒该任务的全部进度流。

    幂等且无订阅者时零开销；生成器被唤醒后重读数据库推导事件，因此重复
    通知与丢失通知都不影响正确性（只影响唤醒延迟，心跳兜底）。
    """
    _bus.notify(job_id)


# ---- 状态推导：事件由数据库状态现算，id 确定性可重放 ----


def _job_payload(job: FormatJob) -> dict:
    """任务流状态的统一载荷（snapshot / progress / 终态同形）：进度列全量加
    失败原因与交付文件名，前端一次合并即得完整展示态。分块两列只有预处理
    任务在标记段有值，格式化任务恒 None。"""
    return {
        "status": job.status,
        "phase": job.phase,
        "progress": job.progress,
        "message": job.message,
        "chunk_index": job.chunk_index,
        "chunk_total": job.chunk_total,
        "error": job.error,
        "output_filename": job.output_filename,
    }


def _read_state_short(job_id: int) -> dict | None:
    """生成器内的状态重读：独立短连接，随读随关，绝不跨 await 持有事务。"""
    session = app_db.SessionLocal()
    try:
        job = session.get(FormatJob, job_id)
        return None if job is None else _job_payload(job)
    finally:
        session.close()


def _progress_event_id(progress: float) -> int:
    """progress 事件 id：千分进度 + 1，1 起连续。

    只由数据库进度列确定性推导，与连接时刻无关——续传跳过、测试断言都依赖
    这个确定性。Worker 纪律：任务存活期内进度列单调不减（认领 0.0 起步），
    故 id 单调；若未来出现"同进度不同文案"的重复写，后一次被跳过，缺口由
    终态事件（全量载荷）与快照兜底——进度只是展示态，不是状态机的一部分。
    """
    return int(round(progress * 1000)) + 1


# ---- SSE 编码 ----


def _format_event(event: str, data: dict, *, id: int | None = None) -> str:
    prefix = f"id: {id}\n" if id is not None else ""
    payload = json.dumps(data, ensure_ascii=False)
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


def _parse_last_event_id(raw: str | None) -> int:
    """Last-Event-ID 解析：缺失或非数字按 0 处理，语义等于全新连接——
    snapshot 会补齐缺口，坏头永远不会导致丢状态。"""
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def _job_stream(
    job_id: int,
    initial: dict,
    queue: asyncio.Queue[None],
    last_event_id: int,
) -> AsyncIterator[str]:
    emitted = last_event_id
    try:
        yield _format_event("snapshot", initial)
        if initial["status"] in _TERMINAL_STATUSES:
            yield _format_event(initial["status"], initial)
            return
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # wait_for 超时与 get 完成的竞态可能吞掉已入队的通知：非阻塞补取
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    yield ": ping\n\n"
                    continue
            state = _read_state_short(job_id)
            if state is None:
                return  # 任务行不存在（已被删除）：关流，不虚构状态
            if state["status"] in _TERMINAL_STATUSES:
                # 终态不带 id：重连时随 snapshot 重发，不算重复事件
                yield _format_event(state["status"], state)
                return
            event_id = _progress_event_id(state["progress"] or 0.0)
            if event_id <= emitted:
                continue
            emitted = event_id
            yield _format_event("progress", state, id=event_id)
    finally:
        _bus.unsubscribe(job_id, queue)


# ---- HTTP 端点 ----


@router.get("/jobs/{job_id}/stream")
@router.get("/preprocess/{job_id}/stream")
async def stream_job_progress(
    job_id: int,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """建立任务进度流（/jobs/… 与 /preprocess/… 两个路径同一协议同一实现：
    预处理页的链路词汇是"预处理任务"，页面不必知道自己走的是任务流）。
    认证/鉴权/首读在响应前完成（401/404 由此给出）；先订阅后读库，订阅到
    首读之间提交的状态不会漏。"""
    queue = _bus.subscribe(job_id)
    try:
        try:
            job = word_jobs.get_job_detail(session, user, job_id)
        except word_jobs.JobNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        initial = _job_payload(job)
    except BaseException:
        _bus.unsubscribe(job_id, queue)
        raise
    # 流可能存活整个任务时长：首读后立即提交释放读事务（短事务纪律）；
    # 生成器内一切读库都走 _read_state_short 的独立短连接
    session.commit()
    return StreamingResponse(
        _job_stream(job_id, initial, queue, _parse_last_event_id(last_event_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
