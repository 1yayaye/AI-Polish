from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func
from sqlalchemy.orm import Session, defer, joinedload

from app.database import get_db
from app.models.models import OptimizationSegment, OptimizationSession, User

from .dependencies import get_admin_from_token

router = APIRouter()


@router.post("/sessions/{session_id}/stop")
async def admin_stop_session(
    session_id: str,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db)
):
    """管理员停止会话"""
    session = db.query(OptimizationSession).filter(
        OptimizationSession.session_id == session_id
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session.status not in ["queued", "processing"]:
        raise HTTPException(status_code=400, detail="只能停止排队中或处理中的会话")

    session.status = "stopped"
    session.error_message = "管理员手动停止"
    db.commit()

    return {"message": "会话已停止"}


@router.get("/sessions")
async def get_all_sessions(
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db),
    limit: int = 100,
    status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """获取所有会话历史"""
    query = db.query(OptimizationSession).options(
        joinedload(OptimizationSession.user),
        defer(OptimizationSession.original_text),
        defer(OptimizationSession.error_message)
    ).order_by(OptimizationSession.created_at.desc())

    if status:
        query = query.filter(OptimizationSession.status == status)

    sessions = query.limit(limit).all()

    if not sessions:
        return []

    # 批量获取段落统计信息
    session_ids = [s.id for s in sessions]
    # 批量获取会话的原始文本长度
    original_lengths = db.query(
        OptimizationSession.id,
        func.length(OptimizationSession.original_text).label('length')
    ).filter(
        OptimizationSession.id.in_(session_ids)
    ).all()

    original_length_map = {item.id: (item.length or 0) for item in original_lengths}

    stats_query = db.query(
        OptimizationSegment.session_id,
        func.count(OptimizationSegment.id).label('total'),
        func.sum(case((OptimizationSegment.status == 'completed', 1), else_=0)).label('completed'),
        func.sum(func.length(func.coalesce(OptimizationSegment.polished_text, ''))).label('polished_chars'),
        func.sum(func.length(func.coalesce(OptimizationSegment.enhanced_text, ''))).label('enhanced_chars')
    ).filter(
        OptimizationSegment.session_id.in_(session_ids)
    ).group_by(OptimizationSegment.session_id).all()

    stats_map = {
        stat.session_id: {
            'total': stat.total,
            'completed': stat.completed,
            'polished_chars': stat.polished_chars or 0,
            'enhanced_chars': stat.enhanced_chars or 0
        }
        for stat in stats_query
    }

    result = []
    for session in sessions:
        # 计算处理时间
        processing_time = None
        if session.completed_at and session.created_at:
            processing_time = (session.completed_at - session.created_at).total_seconds()
        elif session.status == 'processing' and session.created_at:
            processing_time = (datetime.utcnow() - session.created_at).total_seconds()

        # 获取统计信息
        stats = stats_map.get(session.id, {
            'total': 0, 'completed': 0, 'polished_chars': 0, 'enhanced_chars': 0
        })

        result.append({
            "session_id": session.id,
            "user_id": session.user_id,
            "card_key": session.user.card_key if session.user else None,
            "status": session.status,
            "processing_mode": session.processing_mode,
            "original_char_count": original_length_map.get(session.id, 0),
            "polished_char_count": int(stats['polished_chars']),
            "enhanced_char_count": int(stats['enhanced_chars']),
            "total_segments": stats['total'],
            "completed_segments": stats['completed'],
            "progress": round((stats['completed'] / stats['total'] * 100) if stats['total'] > 0 else 0, 1),
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
            "processing_time": processing_time,
            "error_message": None, # 列表页不返回详细错误信息
        })

    return result


@router.get("/sessions/active")
async def get_active_sessions(
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """获取所有活跃会话（处理中和排队中）- 优化版本，使用批量查询避免N+1问题"""
    # 使用 joinedload 预加载用户信息，避免N+1查询
    active_sessions = db.query(OptimizationSession).options(
        joinedload(OptimizationSession.user),
        defer(OptimizationSession.original_text)  # 延迟加载大文本字段
    ).filter(
        OptimizationSession.status.in_(["processing", "queued"])
    ).order_by(OptimizationSession.created_at.desc()).all()

    if not active_sessions:
        return []

    # 批量获取会话ID
    session_ids = [s.id for s in active_sessions]

    # 批量查询原文长度和预览（避免加载完整文本）
    text_info = db.query(
        OptimizationSession.id,
        func.length(OptimizationSession.original_text).label('length'),
        func.substring(OptimizationSession.original_text, 1, 200).label('preview')
    ).filter(
        OptimizationSession.id.in_(session_ids)
    ).all()
    text_info_map = {
        item.id: {'length': item.length or 0, 'preview': item.preview or ""}
        for item in text_info
    }

    # 批量查询已完成段落数
    segments_stats = db.query(
        OptimizationSegment.session_id,
        func.sum(case((OptimizationSegment.status == 'completed', 1), else_=0)).label('completed')
    ).filter(
        OptimizationSegment.session_id.in_(session_ids)
    ).group_by(OptimizationSegment.session_id).all()

    segments_map = {stat.session_id: int(stat.completed or 0) for stat in segments_stats}

    result = []
    now = datetime.utcnow()
    for session in active_sessions:
        # 计算处理时间
        processing_time = None
        if session.status == "processing" and session.created_at:
            processing_time = (now - session.created_at).total_seconds()

        text_data = text_info_map.get(session.id, {'length': 0, 'preview': ""})

        result.append({
            "id": session.id,
            "session_id": session.session_id,
            "user_id": session.user_id,
            "card_key": session.user.card_key if session.user else "未知",
            "status": session.status,
            "progress": session.progress,
            "current_stage": session.current_stage,
            "current_position": session.current_position,
            "total_segments": session.total_segments,
            "processed_segments": segments_map.get(session.id, 0),
            "original_text": text_data['preview'],
            "original_char_count": text_data['length'],
            "processing_mode": session.processing_mode,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "processing_time": processing_time,
            "error_message": session.error_message
        })

    return result


@router.get("/users/{user_id}/sessions")
async def get_user_sessions(
    user_id: int,
    _: str = Depends(get_admin_from_token),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """获取指定用户的所有会话历史"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    sessions = db.query(OptimizationSession).options(
        defer(OptimizationSession.original_text),
        defer(OptimizationSession.error_message)
    ).filter(
        OptimizationSession.user_id == user_id
    ).order_by(OptimizationSession.created_at.desc()).limit(50).all()

    if not sessions:
        return []

    session_ids = [s.id for s in sessions]

    # 批量获取会话的原始文本长度和预览
    original_info = db.query(
        OptimizationSession.id,
        func.length(OptimizationSession.original_text).label('length'),
        func.substring(OptimizationSession.original_text, 1, 100).label('preview')
    ).filter(
        OptimizationSession.id.in_(session_ids)
    ).all()

    original_info_map = {
        item.id: {'length': item.length or 0, 'preview': item.preview or ""}
        for item in original_info
    }

    stats_query = db.query(
        OptimizationSegment.session_id,
        func.count(OptimizationSegment.id).label('total'),
        func.sum(case((OptimizationSegment.status == 'completed', 1), else_=0)).label('completed'),
        func.sum(func.length(func.coalesce(OptimizationSegment.polished_text, ''))).label('polished_chars'),
        func.sum(func.length(func.coalesce(OptimizationSegment.enhanced_text, ''))).label('enhanced_chars')
    ).filter(
        OptimizationSegment.session_id.in_(session_ids)
    ).group_by(OptimizationSegment.session_id).all()

    stats_map = {
        stat.session_id: {
            'total': stat.total,
            'completed': stat.completed,
            'polished_chars': stat.polished_chars or 0,
            'enhanced_chars': stat.enhanced_chars or 0
        }
        for stat in stats_query
    }

    result = []
    for session in sessions:
        # 计算处理时间
        processing_time = None
        if session.completed_at and session.created_at:
            processing_time = (session.completed_at - session.created_at).total_seconds()
        elif session.status == "processing" and session.created_at:
            processing_time = (datetime.utcnow() - session.created_at).total_seconds()

        stats = stats_map.get(session.id, {
            'total': 0, 'completed': 0, 'polished_chars': 0, 'enhanced_chars': 0
        })

        orig_info = original_info_map.get(session.id, {'length': 0, 'preview': ""})

        result.append({
            "id": session.id,
            "session_id": session.session_id,
            "status": session.status,
            "processing_mode": session.processing_mode,
            "original_text": orig_info['preview'],
            "original_char_count": orig_info['length'],
            "polished_char_count": int(stats['polished_chars']),
            "enhanced_char_count": int(stats['enhanced_chars']),
            "total_segments": stats['total'],
            "completed_segments": stats['completed'],
            "progress": session.progress,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
            "processing_time": processing_time,
            "error_message": None # 列表页不返回详细错误信息
        })

    return result
