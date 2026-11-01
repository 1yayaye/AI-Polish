"""优化会话服务：任务创建与预扣费闭环的单一拥有者（spec #15 用户故事 1-3、11-12）。

调用方（HTTP 路由、将来的 Worker）只需要 create_session / cancel_session /
查询函数，不接触定价表、切分规则、预扣费与退款的事务编排——这些都藏在本
模块内部：

- 定价：三种处理模式按计费字数（去空白字符数）预扣，费率见
  ``_PRICE_FEN_PER_10K``。预扣走 :func:`app.billing.charge` 的条件原子
  UPDATE 路径，余额不足抛 InsufficientBalance，本事务内已写的会话行一并
  回滚（零副作用）；退款走 :func:`app.billing.refund`（含 #13 金额守卫）。
- 段落切分（GLOSSARY：段落）：:func:`split_segments`，规则见其 docstring。
- 状态机（ADR-0003）：创建即 queued；取消只允许 queued/running，用条件
  UPDATE 守住并发（与 Worker/失败路径竞争时恰好一方得手）。

model_snapshot 由 Worker 认领会话时一次写入；本模块不碰模型配置（ADR-0005）。
"""

import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app import billing
from app.auth import get_current_user
from app.db import get_session
from app.models import OptimizationSession, Segment, SegmentChangeLog, User

router = APIRouter(prefix="/api/optimization")

MODE_POLISH = "paper_polish"
MODE_ENHANCE = "paper_enhance"
MODE_POLISH_ENHANCE = "paper_polish_enhance"
Mode = Literal["paper_polish", "paper_enhance", "paper_polish_enhance"]

STAGE_POLISH = "polish"
STAGE_ENHANCE = "enhance"

SESSION_QUEUED = "queued"
SESSION_RUNNING = "running"
SESSION_COMPLETED = "completed"
SESSION_FAILED = "failed"
SESSION_CANCELLED = "cancelled"
# 取消只允许未终态的会话（终态：completed/failed/cancelled）
_CANCELLABLE = (SESSION_QUEUED, SESSION_RUNNING)

SEGMENT_PENDING = "pending"
SEGMENT_COMPLETED = "completed"
SEGMENT_FAILED = "failed"

# 超长段切分上限（字符）：LLM 上下文与逐段进度粒度的折中
MAX_SEGMENT_CHARS = 1500
_SENTENCE_ENDINGS = "。！？；!?;"

# 定价表（分/万字，GLOSSARY：余额按字数计费）。当前无历史定价可循，
# 运营商调价只改这张表；两阶段全流程按低于单阶段之和的打包价收取。
_PRICE_FEN_PER_10K = {
    MODE_POLISH: 300,
    MODE_ENHANCE: 300,
    MODE_POLISH_ENHANCE: 500,
}

_STAGES_BY_MODE = {
    MODE_POLISH: (STAGE_POLISH,),
    MODE_ENHANCE: (STAGE_ENHANCE,),
    MODE_POLISH_ENHANCE: (STAGE_POLISH, STAGE_ENHANCE),
}


def stages_for_mode(mode: str) -> tuple[str, ...]:
    """处理模式的阶段推进序（GLOSSARY：处理模式）：Worker 据此逐阶段过段落。"""
    return _STAGES_BY_MODE[mode]


class SessionNotFound(Exception):
    """会话不存在或不属于当前用户（对外一律 404，不泄露他人会话存在性）。"""


class SessionNotCancellable(Exception):
    """会话已处于终态（completed/failed/cancelled），不可取消。"""


def count_billable_chars(text: str) -> int:
    """计费字数：非空白字符数（GLOSSARY：余额按字数计费的"字数"口径）。"""
    return sum(1 for ch in text if not ch.isspace())


def estimate_cost_fen(mode: str, char_count: int) -> int:
    """按模式定价折算预扣金额（分）：不足一字万按占比向上取整。"""
    return math.ceil(char_count * _PRICE_FEN_PER_10K[mode] / 10_000)


def split_segments(text: str, max_chars: int = MAX_SEGMENT_CHARS) -> list[str]:
    """把原文切分为段落序列（GLOSSARY：段落），顺序即处理顺序。

    规则：按换行拆自然段、丢弃空白段（空段边界）；超长段在限制内最后
    一个句读（。！？；等）处继续切分，无句读则硬切到上限——保证任何
    单段不超过 max_chars。全空白文本得到空列表（创建时拒绝）。
    """
    segments: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        segments.extend(_split_long_paragraph(paragraph, max_chars))
    return segments


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    chunks: list[str] = []
    while len(paragraph) > max_chars:
        cut = next(
            (i for i in range(max_chars, 0, -1) if paragraph[i - 1] in _SENTENCE_ENDINGS),
            max_chars,
        )
        chunks.append(paragraph[:cut])
        paragraph = paragraph[cut:].strip()
    if paragraph:
        chunks.append(paragraph)
    return chunks


def create_session(session: Session, user: User, *, text: str, mode: str) -> OptimizationSession:
    """创建会话：预扣费 → 段落切分落库 → queued（GLOSSARY：预扣费）。

    切分后无任何段落（全空白文本）抛 ValueError，路由层映射 422。
    余额不足抛 :class:`billing.InsufficientBalance`——此时会话行可能已
    flush，但与扣费同事务，调用方（get_session）回滚即零副作用。
    """
    segments = split_segments(text)
    if not segments:
        raise ValueError("文本切分后没有任何段落")
    word_count = count_billable_chars(text)
    cost_fen = estimate_cost_fen(mode, word_count)

    session_row = OptimizationSession(
        user_id=user.id,
        status=SESSION_QUEUED,
        mode=mode,
        original_text=text,
        word_count=word_count,
        charged_fen=cost_fen,
    )
    session.add(session_row)
    # 先 flush 拿主键：账本 charge 行的 task_id 要能追溯到本会话
    session.flush()
    billing.charge(
        session,
        user.id,
        cost_fen,
        task_id=session_row.id,
        reason=f"会话预扣费（{mode}，{word_count} 字）",
    )
    for stage in _STAGES_BY_MODE[mode]:
        for seq, segment_text in enumerate(segments):
            session.add(
                Segment(
                    session_id=session_row.id,
                    stage=stage,
                    seq=seq,
                    source_text=segment_text,
                    status=SEGMENT_PENDING,
                )
            )
    return session_row


def cancel_session(session: Session, user: User, session_id: int) -> OptimizationSession:
    """取消未完成会话并自动退款（spec #15 用户故事 3）。

    仅 queued/running 可取消：条件原子 UPDATE 守住与 Worker/失败路径的
    并发竞争，输掉（对方先到终态）抛 :class:`SessionNotCancellable`。
    退款走 :func:`app.billing.refund`：金额取创建时的 charged_fen（与
    原 charge 行必等额，#13 守卫因此恒通过）。竞争输掉退款（对方已退款，
    幂等返回 False）时本会话仍置 cancelled——钱已退回，取消照常成立。
    """
    row = session.execute(
        select(OptimizationSession).where(
            OptimizationSession.id == session_id, OptimizationSession.user_id == user.id
        )
    ).scalar_one_or_none()
    if row is None:
        raise SessionNotFound(f"会话 {session_id} 不存在")
    hit = session.execute(
        update(OptimizationSession)
        .where(
            OptimizationSession.id == session_id,
            OptimizationSession.status.in_(_CANCELLABLE),
        )
        .values(status=SESSION_CANCELLED, updated_at=func.current_timestamp())
    ).rowcount
    if hit != 1:
        raise SessionNotCancellable(f"会话 {session_id} 已完成或已取消")
    billing.refund(session, user.id, row.charged_fen, task_id=row.id, reason="会话取消退款")
    session.refresh(row)
    return row


def get_session_detail(session: Session, user: User, session_id: int) -> OptimizationSession:
    """本人会话行；不存在或不属于当前用户抛 :class:`SessionNotFound`
    （对外一律 404，不泄露他人会话存在性）。"""
    row = session.execute(
        select(OptimizationSession).where(
            OptimizationSession.id == session_id, OptimizationSession.user_id == user.id
        )
    ).scalar_one_or_none()
    if row is None:
        raise SessionNotFound(f"会话 {session_id} 不存在")
    return row


def list_user_sessions(session: Session, user: User) -> list[OptimizationSession]:
    """本人全部会话，最新在前。"""
    return list(
        session.execute(
            select(OptimizationSession)
            .where(OptimizationSession.user_id == user.id)
            .order_by(OptimizationSession.id.desc())
        ).scalars()
    )


# ---- HTTP 层：请求/响应模型与错误映射（402/404/409/422） ----


class SegmentOut(BaseModel):
    seq: int
    stage: str
    status: str


class SessionOut(BaseModel):
    id: int
    mode: str
    status: str
    word_count: int
    charged_fen: int
    segments: list[SegmentOut]


class SessionSummaryOut(BaseModel):
    id: int
    mode: str
    status: str
    word_count: int
    charged_fen: int


class CreateSessionRequest(BaseModel):
    text: str
    mode: Mode


def _to_summary(row: OptimizationSession) -> SessionSummaryOut:
    return SessionSummaryOut(
        id=row.id, mode=row.mode, status=row.status,
        word_count=row.word_count, charged_fen=row.charged_fen,
    )


# 段落展示顺序：润色阶段先于增强阶段（字典序相反，显式排序）。
# 段落表与变更记录表共用这一排序契约，由 _stage_order_for 施加到各自的 stage 列。


def _stage_order_for(column):  # type: ignore[no-untyped-def]
    return case((column == STAGE_ENHANCE, 1), else_=0)


def _session_out(session: Session, row: OptimizationSession) -> SessionOut:
    """会话 + 段落状态的响应模型（润色段在前，段内按序号）。"""
    segments = session.execute(
        select(Segment)
        .where(Segment.session_id == row.id)
        .order_by(_stage_order_for(Segment.stage), Segment.seq)
    ).scalars()
    return SessionOut(
        id=row.id,
        mode=row.mode,
        status=row.status,
        word_count=row.word_count,
        charged_fen=row.charged_fen,
        segments=[SegmentOut(seq=s.seq, stage=s.stage, status=s.status) for s in segments],
    )


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_optimization_session(
    request: CreateSessionRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SessionOut:
    """提交文本与处理模式：预扣费（余额不足 402）→ 切分 → queued。"""
    try:
        row = create_session(session, user, text=request.text, mode=request.mode)
    except billing.InsufficientBalance as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _session_out(session, row)


@router.get("/sessions", response_model=list[SessionSummaryOut])
def list_my_sessions(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[SessionSummaryOut]:
    return [_to_summary(row) for row in list_user_sessions(session, user)]


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_my_session(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SessionOut:
    try:
        row = get_session_detail(session, user, session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _session_out(session, row)


@router.post("/sessions/{session_id}/cancel", response_model=SessionOut)
def cancel_my_session(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SessionOut:
    try:
        row = cancel_session(session, user, session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SessionNotCancellable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _session_out(session, row)


class ChangeOut(BaseModel):
    """一段变更记录（GLOSSARY）：before 是该次调用实际发给 LLM 的输入。"""

    stage: str
    seq: int
    before_text: str
    after_text: str


@router.get("/sessions/{session_id}/changes", response_model=list[ChangeOut])
def my_session_changes(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[ChangeOut]:
    """本人会话的全部段落变更记录（spec #15 用户故事 10），润色段在前。"""
    try:
        row = get_session_detail(session, user, session_id)
    except SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    logs = session.execute(
        select(SegmentChangeLog)
        .where(SegmentChangeLog.session_id == row.id)
        .order_by(_stage_order_for(SegmentChangeLog.stage), SegmentChangeLog.seq)
    ).scalars()
    return [
        ChangeOut(stage=log.stage, seq=log.seq, before_text=log.before_text, after_text=log.after_text)
        for log in logs
    ]
