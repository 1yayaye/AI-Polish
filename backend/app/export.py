"""会话导出：完成态会话产出 txt / docx（spec #15 用户故事 8-9）。

调用方（HTTP 路由）只需要 export_txt / export_docx，不接触"最终文本"的
拼装规则与渲染库的调用细节：

- 最终文本（GLOSSARY：段落，按处理顺序）：取该会话处理模式最后阶段的
  段落结果（两阶段模式取增强段——增强本就在润色产物上进行，见
  app.models.Segment 契约），按 seq 顺序以空行分段拼出（空行分段是
  渲染库纯文本解析的分段约定，txt 与 docx 因此共享同一份段落结构）。
  完成态守卫在 :func:`_completed_session`：非 completed 一律 409，
  归属失败 404。
- docx 渲染走移植的 word_formatter 纯函数库（ADR-0004）：同步编译经
  ``asyncio.to_thread`` 抛出事件循环，``Semaphore(1)`` 限制同时只编译
  一个文档（rewrite-plan §5：512MB 内存高危点，防阻塞 SSE）。字节流只在
  本次请求内瞬时驻留（万字级文档约数十 KB～数 MB），不落库不缓存；
  M4 的 Word Formatter 任务管理才引入落盘。
- 短事务纪律：先在请求会话内读出最终文本，再发起渲染——渲染期间不持
  任何数据库事务。

输入格式固定为 PLAINTEXT：导出对象是流水线产出的普通段落文本，不做
Markdown 猜测（AUTO 会因正文恰好含 ``#`` 等符号而改变结构，导出必须
确定性）。
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import optimization
from app.auth import get_current_user
from app.db import get_session
from app.models import OptimizationSession, Segment, User
from app.word_formatter import CompileOptions, InputFormat, compile_document

router = APIRouter(prefix="/api/optimization")

_TXT_MEDIA_TYPE = "text/plain; charset=utf-8"
_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_DISPOSITION = "attachment; filename=polished-{sid}.docx"
_TXT_DISPOSITION = "attachment; filename=polished-{sid}.txt"

# 同时只编译一个 docx（rewrite-plan §5）；进程级常量，进程内全局生效。
_COMPILE_SEMAPHORE = asyncio.Semaphore(1)


class DocxRenderError(Exception):
    """渲染库编译失败（对外 500；属服务端故障而非用户输入问题）。"""


def _final_text(session: Session, row: OptimizationSession) -> str:
    """完成态会话的最终文本：最后处理阶段的段落结果按 seq 空行分段。"""
    final_stage = optimization.stages_for_mode(row.mode)[-1]
    segments = session.execute(
        select(Segment)
        .where(Segment.session_id == row.id, Segment.stage == final_stage)
        .order_by(Segment.seq)
    ).scalars()
    return "\n\n".join(segment.result_text or "" for segment in segments)


def _completed_session(session: Session, user: User, session_id: int) -> OptimizationSession:
    """本人且已完成（completed）的会话行；归属失败 404，未完成 409。"""
    try:
        row = optimization.get_session_detail(session, user, session_id)
    except optimization.SessionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if row.status != optimization.SESSION_COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="会话尚未完成，无法导出",
        )
    return row


def export_txt(session: Session, user: User, session_id: int) -> tuple[str, str]:
    """txt 导出：返回（最终文本，Content-Disposition）。"""
    row = _completed_session(session, user, session_id)
    return _final_text(session, row), _TXT_DISPOSITION.format(sid=row.id)


async def export_docx(session: Session, user: User, session_id: int) -> tuple[bytes, str]:
    """docx 导出：渲染库编译出字节流，返回（docx 字节，Content-Disposition）。

    渲染是秒级同步阻塞：经 to_thread 抛给线程池并以 Semaphore(1) 排队，
    期间不持有任何数据库事务（文本已先读出）。
    """
    row = _completed_session(session, user, session_id)
    text = _final_text(session, row)
    async with _COMPILE_SEMAPHORE:
        result = await asyncio.to_thread(
            compile_document,
            text,
            CompileOptions(input_format=InputFormat.PLAINTEXT),
        )
    if not result.success:
        raise DocxRenderError(f"docx 渲染失败：{result.error}")
    return result.docx_bytes or b"", _DOCX_DISPOSITION.format(sid=row.id)


@router.get("/sessions/{session_id}/export.txt")
def export_session_txt(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """完成态会话导出 txt：内容即最终文本，按段落顺序空行分段。"""
    text, disposition = export_txt(session, user, session_id)
    return Response(
        content=text,
        media_type=_TXT_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )


@router.get("/sessions/{session_id}/export.docx")
async def export_session_docx(
    session_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """完成态会话导出 docx：最终文本经渲染库编译（同步编译排队单并发）。"""
    docx_bytes, disposition = await export_docx(session, user, session_id)
    return Response(
        content=docx_bytes,
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": disposition},
    )
