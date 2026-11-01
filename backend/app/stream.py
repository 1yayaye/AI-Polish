"""SSE 进度推送（spec #15 用户故事 5-6）：会话进度流端点与事件协议的唯一拥有者。

外部契约（GET /api/optimization/sessions/{id}/stream）：

- **认证**：卡密只走 ``X-Card-Key`` 请求头（复用 :func:`app.auth.get_current_user`），
  缺失/无效 401；本人会话之外 404。卡密绝不进 URL——浏览器 EventSource 不能带
  header，前端一律用 @microsoft/fetch-event-source（rewrite-plan §5）。
- **事件序**：连接即推 ``snapshot``（当前全部段落状态）→ 逐段 ``segment`` 增量
  → 终态 ``done`` 后关流。每条 segment 事件带单调递增 id（处理序：阶段序 × 段序）。
- **断线续传**：客户端重连带 ``Last-Event-ID``，服务端跳过 id ≤ 该值的增量；
  断线期间的缺口状态由每次连接必发的 snapshot 补齐。不丢不重由"id 从数据库
  状态确定性推导"保证——服务端不保存任何流状态，天然可重启。

实现纪律：

- 任务表状态是唯一事实源（ADR-0003）：本模块不缓存业务状态，事件全部由
  数据库短事务现读现推导；内存只做"状态提交了就叫醒订阅者"的通知
  （:class:`_EventBus`），通知本身不携带数据，唤醒后一律重读数据库。
- 流可能存活整个任务时长：路由内完成认证/鉴权/首读后立即提交释放读事务，
  生成器内一切读库都另起独立短连接（app.db 短事务纪律）。
- LLM 慢调用期间本流零阻塞：生成器只 await 通知队列（超时即发心跳注释行），
  Worker 在段落短事务提交后经 :func:`notify` 叫醒——这是 Worker 与本模块的
  唯一耦合点。
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as app_db
from app import optimization
from app.auth import get_current_user
from app.db import get_session
from app.models import OptimizationSession, Segment, User

router = APIRouter(prefix="/api/optimization")

# 心跳间隔（秒）：注释行保活，穿过 Nginx/代理的空闲超时；也是取消等不经
# Worker 的状态变更被流感知的最大延迟。测试注入更小值驱动。
_HEARTBEAT_SECONDS = 15.0

_TERMINAL_STATUSES = frozenset(
    {
        optimization.SESSION_COMPLETED,
        optimization.SESSION_FAILED,
        optimization.SESSION_CANCELLED,
    }
)


# ---- 通知总线：内存只做叫醒，不做状态（ADR-0003） ----


class _EventBus:
    """每会话一组订阅队列；notify 只投递唤醒信号（None），不携带任何业务数据。"""

    def __init__(self) -> None:
        self._queues: dict[int, set[asyncio.Queue[None]]] = defaultdict(set)

    def subscribe(self, session_id: int) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue()
        self._queues[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: int, queue: asyncio.Queue[None]) -> None:
        self._queues[session_id].discard(queue)
        if not self._queues[session_id]:
            del self._queues[session_id]

    def notify(self, session_id: int) -> None:
        for queue in self._queues.get(session_id, ()):
            queue.put_nowait(None)


_bus = _EventBus()


def notify(session_id: int) -> None:
    """状态写入方（Worker 等）在短事务提交后调用：叫醒该会话的全部进度流。

    幂等且无订阅者时零开销；生成器被唤醒后重读数据库推导事件，因此重复
    通知与丢失通知都不影响正确性（只影响唤醒延迟，心跳兜底）。
    """
    _bus.notify(session_id)


# ---- 状态推导：事件由数据库状态现算，id 确定性可重放 ----


class _SegmentState(NamedTuple):
    stage: str
    seq: int
    status: str


class _StreamState(NamedTuple):
    """一次短事务读出的会话流状态；segments 为处理序（阶段序 × 段序）全量段落。"""

    status: str
    segments: list[_SegmentState]


def _read_state(db: Session, row: OptimizationSession) -> _StreamState:
    rows = db.execute(
        select(Segment.stage, Segment.seq, Segment.status).where(Segment.session_id == row.id)
    ).all()
    stage_order = {stage: i for i, stage in enumerate(optimization.stages_for_mode(row.mode))}
    segments = sorted(
        (_SegmentState(stage, seq, seg_status) for stage, seq, seg_status in rows),
        key=lambda s: (stage_order[s.stage], s.seq),
    )
    return _StreamState(row.status, segments)


def _read_state_short(session_id: int) -> _StreamState | None:
    """生成器内的状态重读：独立短连接，随读随关，绝不跨 await 持有事务。"""
    session = app_db.SessionLocal()
    try:
        row = session.get(OptimizationSession, session_id)
        return None if row is None else _read_state(session, row)
    finally:
        session.close()


def _ordinal(mode: str, segments_total: int, stage: str, seq: int) -> int:
    """segment 事件 id：处理序（阶段序 × 段序）上的 1 起连续序号。

    只由模式与段落数决定，与连接时刻无关——续传跳过、测试断言都依赖这
    个确定性。同会话各阶段段落数恒等（创建时同一序列复制），整除即每段
    阶段的段落总数。
    """
    stages = optimization.stages_for_mode(mode)
    stage_index = stages.index(stage)
    per_stage = segments_total // len(stages)
    return stage_index * per_stage + seq + 1


# ---- SSE 编码 ----


def _format_event(event: str, data: dict, *, id: int | None = None) -> str:
    prefix = f"id: {id}\n" if id is not None else ""
    payload = json.dumps(data, ensure_ascii=False)
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


def _snapshot_payload(state: _StreamState) -> dict:
    return {
        "status": state.status,
        "segments": [seg._asdict() for seg in state.segments],
    }


def _parse_last_event_id(raw: str | None) -> int:
    """Last-Event-ID 解析：缺失或非数字按 0 处理，语义等于全新连接——
    snapshot 会补齐缺口，坏头永远不会导致丢状态。"""
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def _progress_stream(
    session_id: int,
    mode: str,
    initial: _StreamState,
    queue: asyncio.Queue[None],
    last_event_id: int,
) -> AsyncIterator[str]:
    emitted = last_event_id
    try:
        yield _format_event("snapshot", _snapshot_payload(initial))
        if initial.status in _TERMINAL_STATUSES:
            yield _format_event("done", _snapshot_payload(initial))
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
            state = _read_state_short(session_id)
            if state is None:
                return  # 会话行不存在：关流，不虚构状态
            for seg in state.segments:
                if seg.status != optimization.SEGMENT_COMPLETED:
                    continue
                ordinal = _ordinal(mode, len(state.segments), seg.stage, seg.seq)
                if ordinal <= emitted:
                    continue
                emitted = ordinal
                yield _format_event(
                    "segment",
                    {"stage": seg.stage, "seq": seg.seq, "status": seg.status},
                    id=ordinal,
                )
            if state.status in _TERMINAL_STATUSES:
                # done 不带 id：终态重连时随 snapshot 重发，不算重复事件
                yield _format_event("done", _snapshot_payload(state))
                return
    finally:
        _bus.unsubscribe(session_id, queue)


# ---- HTTP 端点 ----


@router.get("/sessions/{session_id}/stream")
async def stream_session_progress(
    session_id: int,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """建立进度流。认证/鉴权/首读在响应前完成（401/404 由此给出）；
    先订阅后读库，订阅到首读之间提交的状态不会漏。"""
    queue = _bus.subscribe(session_id)
    try:
        try:
            row = optimization.get_session_detail(session, user, session_id)
        except optimization.SessionNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        initial = _read_state(session, row)
    except BaseException:
        _bus.unsubscribe(session_id, queue)
        raise
    # 流可能存活整个任务时长：首读后立即提交释放读事务（短事务纪律）；
    # 生成器内一切读库都走 _read_state_short 的独立短连接
    session.commit()
    return StreamingResponse(
        _progress_stream(
            session_id, row.mode, initial, queue, _parse_last_event_id(last_event_id)
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
