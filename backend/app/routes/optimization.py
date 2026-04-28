from fastapi import APIRouter, Depends, Header, HTTPException, BackgroundTasks, Query, Request
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, and_, case
from typing import List, Optional
import json
from app.database import get_db, SessionLocal
from app.models.models import User, OptimizationSession, OptimizationSegment, ChangeLog, ModelProfile
from app.schemas import (
    OptimizationCreate, SessionResponse, SessionDetailResponse,
    QueueStatusResponse, ProgressUpdate, ChangeLogResponse, ExportConfirmation,
    ModelProfilePublic, WorkspaceBillingResponse,
)
from app.services.optimization_service import OptimizationService
from app.services.billing_service import precharge_workspace_session, refund_workspace_charge
from app.services.concurrency import concurrency_manager
from app.services.resource_guard import ensure_memory_available
from app.services.resource_access import authorize_resource_access
from app.services.stream_manager import stream_manager
from app.utils.auth import generate_session_id
from datetime import datetime
import asyncio
from app.config import settings
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/optimization", tags=["optimization"])


def get_current_user(
    x_card_key: Optional[str] = Header(None, alias="X-Card-Key"),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate normal APIs with X-Card-Key only."""
    key = x_card_key
    if not key:
        raise HTTPException(status_code=401, detail="缺少 X-Card-Key 请求头")
    user = db.query(User).filter(
        User.card_key == key,
        User.is_active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="无效的卡密")
    user.last_used = datetime.utcnow()
    db.commit()
    return user


async def run_optimization(session_id: int):
    """后台运行优化任务"""
    db = SessionLocal()
    try:
        session_obj = db.query(OptimizationSession).filter(
            OptimizationSession.id == session_id
        ).first()

        if not session_obj:
            return

        service = OptimizationService(db, session_obj)
        await service.start_optimization()
    finally:
        db.close()


@router.get("/model-profiles", response_model=List[ModelProfilePublic])
async def list_active_model_profiles(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[ModelProfile]:
    """获取可用的模型列表（供用户选择）"""
    return db.query(ModelProfile).filter(
        ModelProfile.is_active.is_(True)
    ).order_by(ModelProfile.sort_order, ModelProfile.id).all()


@router.get("/billing", response_model=WorkspaceBillingResponse)
async def get_workspace_billing(
    user: User = Depends(get_current_user),
) -> WorkspaceBillingResponse:
    price_cents = max(settings.WORKSPACE_PRICE_PER_10K_CENTS, 0)
    return WorkspaceBillingResponse(
        workspace_balance_cents=user.workspace_balance_cents or 0,
        workspace_total_spent_cents=user.workspace_total_spent_cents or 0,
        workspace_price_per_10k_cents=price_cents,
        price_configured=price_cents > 0,
    )


@router.post("/start", response_model=SessionResponse)
async def start_optimization(
    data: OptimizationCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """开始优化任务"""

    if not data.original_text.strip():
        raise HTTPException(status_code=400, detail="请输入要优化的文本")

    ensure_memory_available("optimization")

    price_per_10k_cents = settings.WORKSPACE_PRICE_PER_10K_CENTS
    if price_per_10k_cents <= 0:
        raise HTTPException(status_code=400, detail="Workspace 每万字价格未配置")

    # 验证处理模式
    valid_modes = ['paper_polish', 'paper_enhance', 'paper_polish_enhance', 'emotion_polish']
    if data.processing_mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"无效的处理模式。支持的模式: {', '.join(valid_modes)}"
        )

    # 根据处理模式设置初始阶段
    if data.processing_mode == 'emotion_polish':
        initial_stage = 'emotion_polish'
    elif data.processing_mode == 'paper_enhance':
        initial_stage = 'enhance'
    else:
        initial_stage = 'polish'
    
    # 解析模型配置
    model_name = None
    api_key = None
    base_url = None

    if data.model_profile_id:
        profile = db.query(ModelProfile).filter(
            ModelProfile.id == data.model_profile_id,
            ModelProfile.is_active.is_(True),
        ).first()
        if not profile:
            raise HTTPException(status_code=404, detail="Model profile not found")
        model_name = profile.model
        api_key = profile.api_key
        base_url = profile.base_url
    else:
        # 使用默认 profile
        default_profile = db.query(ModelProfile).filter(
            ModelProfile.is_default.is_(True),
            ModelProfile.is_active.is_(True),
        ).first()
        if default_profile:
            model_name = default_profile.model
            api_key = default_profile.api_key
            base_url = default_profile.base_url

    # 创建会话
    session_id = generate_session_id()
    session = OptimizationSession(
        user_id=user.id,
        session_id=session_id,
        original_text=data.original_text,
        processing_mode=data.processing_mode,
        current_stage=initial_stage,
        status="queued",
        progress=0.0,
        polish_model=model_name,
        polish_api_key=api_key,
        polish_base_url=base_url,
        enhance_model=model_name,
        enhance_api_key=api_key,
        enhance_base_url=base_url,
        emotion_model=model_name,
        emotion_api_key=api_key,
        emotion_base_url=base_url,
    )
    
    db.add(session)
    db.flush()
    precharge_workspace_session(
        db,
        user=user,
        session=session,
        char_count=len(data.original_text),
        price_per_10k_cents=price_per_10k_cents,
    )
    db.commit()
    db.refresh(session)
    
    # 添加后台任务
    background_tasks.add_task(run_optimization, session.id)
    
    return session


@router.get("/status", response_model=QueueStatusResponse)
async def get_queue_status(
    session_id: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取队列状态"""
    
    status = await concurrency_manager.get_status(session_id)
    return QueueStatusResponse(**status)


@router.get("/sessions", response_model=List[SessionResponse])
async def list_sessions(
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出用户的所有会话（支持分页）"""
    
    # 限制最大返回数量为100，避免一次性加载过多数据
    limit = min(limit, 100)
    
    # 查询会话及其原始文本长度和预览文本
    results = db.query(
        OptimizationSession,
        func.length(OptimizationSession.original_text).label('original_char_count'),
        func.substring(OptimizationSession.original_text, 1, 50).label('preview_text')
    ).options(
        defer(OptimizationSession.original_text),
        defer(OptimizationSession.error_message)
    ).filter(
        OptimizationSession.user_id == user.id
    ).order_by(OptimizationSession.created_at.desc()).limit(limit).offset(offset).all()

    # 构造响应，手动注入 original_char_count 和 preview_text
    sessions = []
    for session, char_count, preview_text in results:
        session.original_char_count = char_count or 0
        session.preview_text = preview_text or ""
        sessions.append(session)
        
    return sessions


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session_detail(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取会话详情"""
    
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    # 获取段落
    segments = db.query(OptimizationSegment).filter(
        OptimizationSegment.session_id == session.id
    ).order_by(OptimizationSegment.segment_index).all()
    
    return SessionDetailResponse(
        **session.__dict__,
        segments=[seg.__dict__ for seg in segments]
    )


@router.get("/sessions/{session_id}/progress", response_model=ProgressUpdate)
async def get_session_progress(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取会话进度"""
    
    # 查询完整会话对象，但避免急切加载关联对象
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    return ProgressUpdate(
        session_id=session.session_id,
        status=session.status,
        progress=session.progress,
        current_position=session.current_position,
        total_segments=session.total_segments,
        current_stage=session.current_stage,
        error_message=session.error_message
    )


@router.get("/sessions/{session_id}/stream")
async def stream_session_progress(
    session_id: str,
    request: Request,
    access_token: str = Query(...),
    db: Session = Depends(get_db),
):
    authorize_resource_access(
        db,
        access_token=access_token,
        resource_type="optimization_session",
        resource_id=session_id,
        action="stream",
    )
    """流式获取会话进度和内容"""
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    async def event_generator():
        queue = await stream_manager.connect(session_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                
                # 从队列获取消息，设置超时以便检查连接状态
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield message
                except asyncio.TimeoutError:
                    # 发送心跳注释以保持连接活跃
                    yield ": keep-alive\n\n"
                    
        finally:
            await stream_manager.disconnect(session_id, queue)

    return EventSourceResponse(event_generator())


@router.get("/sessions/{session_id}/changes", response_model=List[ChangeLogResponse])
async def get_session_changes(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取会话的变更对照"""
    
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    latest_log_subquery = db.query(
        ChangeLog.segment_index,
        ChangeLog.stage,
        func.max(ChangeLog.id).label("latest_id")
    ).filter(
        ChangeLog.session_id == session.id
    ).group_by(
        ChangeLog.segment_index,
        ChangeLog.stage
    ).subquery()

    change_logs = db.query(ChangeLog).join(
        latest_log_subquery,
        and_(
            ChangeLog.segment_index == latest_log_subquery.c.segment_index,
            ChangeLog.stage == latest_log_subquery.c.stage,
            ChangeLog.id == latest_log_subquery.c.latest_id
        )
    ).filter(
        ChangeLog.session_id == session.id
    ).order_by(
        ChangeLog.segment_index,
        case((ChangeLog.stage == "polish", 0), else_=1)
    ).all()

    parsed_changes = []
    for change in change_logs:
        detail = None
        if change.changes_detail:
            try:
                detail = json.loads(change.changes_detail)
            except json.JSONDecodeError:
                detail = {"raw": change.changes_detail}

        parsed_changes.append(
            ChangeLogResponse(
                id=change.id,
                segment_index=change.segment_index,
                stage=change.stage,
                before_text=change.before_text,
                after_text=change.after_text,
                changes_detail=detail,
                created_at=change.created_at
            )
        )

    return parsed_changes


@router.post("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    confirmation: ExportConfirmation,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """导出优化结果"""
    if not confirmation.acknowledge_academic_integrity:
        raise HTTPException(
            status_code=400,
            detail="必须确认学术诚信承诺"
        )
    
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    if session.status != "completed":
        raise HTTPException(status_code=400, detail="会话未完成")
    
    # 获取所有段落
    segments = db.query(OptimizationSegment).filter(
        OptimizationSegment.session_id == session.id
    ).order_by(OptimizationSegment.segment_index).all()
    
    # 组合最终文本
    final_text = "\n\n".join([
        seg.enhanced_text or seg.polished_text or seg.original_text
        for seg in segments
    ])
    
    # 根据格式返回
    if confirmation.export_format == "txt":
        return {
            "format": "txt",
            "content": final_text,
            "filename": f"optimized_{session_id}.txt"
        }
    else:
        # TODO: 实现 docx 和 pdf 导出
        raise HTTPException(status_code=501, detail="暂不支持此格式")


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除会话"""
    
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    db.delete(session)
    db.commit()
    
    return {"message": "会话已删除"}


@router.post("/sessions/{session_id}/retry")
async def retry_session(
    session_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重新尝试处理失败的会话，继续未完成的段落"""

    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session.status not in ["failed", "stopped"]:
        raise HTTPException(status_code=400, detail="仅可对失败或已停止的会话执行重试")

    ensure_memory_available("optimization retry")

    if session.billing_status not in ["precharged", "charged"]:
        price_per_10k_cents = settings.WORKSPACE_PRICE_PER_10K_CENTS
        if price_per_10k_cents <= 0:
            raise HTTPException(status_code=400, detail="Workspace 每万字价格未配置")
        precharge_workspace_session(
            db,
            user=user,
            session=session,
            char_count=session.billing_char_count or len(session.original_text or ""),
            price_per_10k_cents=price_per_10k_cents,
        )

    # 保留历史错误信息
    old_error = session.error_message or "未知错误"
    session.status = "queued"
    session.error_message = f"[重试中] 上次失败原因: {old_error}"
    db.commit()

    background_tasks.add_task(run_optimization, session.id)

    return {"message": "已重新排队处理未完成段落"}


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """停止正在进行中的会话"""

    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id,
        OptimizationSession.user_id == user.id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session.status not in ["queued", "processing"]:
        raise HTTPException(status_code=400, detail="只能停止排队中或处理中的会话")

    # 更新状态为 stopped
    session.status = "stopped"
    refund_workspace_charge(db, session=session, reason="user stopped workspace task")
    session.error_message = "用户手动停止"
    db.commit()

    return {"message": "会话已停止"}
